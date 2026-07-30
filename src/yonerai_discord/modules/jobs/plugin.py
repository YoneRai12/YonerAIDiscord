from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

import discord

from .adapter import JobsGroup
from .executors import ExplicitExecutorRegistry
from .repository import SqliteJobRepository
from .service import DurableJobService
from .worker import DurableJobWorker


logger = logging.getLogger(__name__)
JOBS_EXECUTE_CAPABILITY_ID = "cap-run-jobs-execute"


class JobsPlugin:
    def __init__(self) -> None:
        self.repository: SqliteJobRepository | None = None
        self.service: DurableJobService | None = None
        self.worker: DurableJobWorker | None = None
        self.bot: Any | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, bot: Any) -> None:
        self.bot = bot
        repository = SqliteJobRepository(Path(bot.settings.database_path))
        repository.open()
        self.repository = repository
        try:
            explicit_executors = getattr(bot, "durable_job_executors", {})
            executor_registry = ExplicitExecutorRegistry(explicit_executors)

            def execution_policy(job: Any) -> bool:
                guard = getattr(bot, "capability_guard", None)
                currently_allowed = getattr(guard, "currently_allowed", None)
                if not callable(currently_allowed):
                    return False
                try:
                    return bool(
                        currently_allowed(
                            JOBS_EXECUTE_CAPABILITY_ID,
                            guild_id=job.guild_id,
                            user_id=0,
                        )
                    )
                except Exception:
                    return False

            lease_seconds = int(getattr(bot.settings, "jobs_lease_seconds", 60))
            execution_timeout = float(getattr(bot.settings, "jobs_execution_timeout_seconds", 45.0))
            service = DurableJobService(
                repository,
                executor_registry,
                lease_seconds=lease_seconds,
                backoff_base_seconds=int(getattr(bot.settings, "jobs_backoff_base_seconds", 5)),
                backoff_max_seconds=int(getattr(bot.settings, "jobs_backoff_max_seconds", 3600)),
                execution_timeout_seconds=execution_timeout,
                disabled_defer_seconds=int(getattr(bot.settings, "jobs_disabled_defer_seconds", 300)),
                execution_policy=execution_policy,
            )
            worker = DurableJobWorker(
                service,
                poll_seconds=float(getattr(bot.settings, "jobs_poll_seconds", 5.0)),
                batch_size=int(getattr(bot.settings, "jobs_batch_size", 10)),
            )
            self.service = service
            self.worker = worker
            bot.tree.add_command(JobsGroup(repository, worker, bot))
            bot.durable_jobs = service
            bot.durable_job_worker = worker
            bot.durable_job_executor_registry = executor_registry
            self._task = asyncio.create_task(worker.run(), name="durable-jobs-worker")
        except BaseException:
            with suppress(Exception):
                bot.tree.remove_command("jobs", type=discord.AppCommandType.chat_input)
            for attribute in (
                "durable_jobs",
                "durable_job_worker",
                "durable_job_executor_registry",
            ):
                if hasattr(bot, attribute):
                    setattr(bot, attribute, None)
            repository.close()
            self.repository = None
            self.service = None
            self.worker = None
            self.bot = None
            raise

    async def begin_close(self) -> None:
        """新規claimと副作用開始を止め、実行中contextへcancelを通知する。"""

        if self.worker is not None:
            self.worker.request_stop()

    async def stop(self) -> None:
        await self.begin_close()
        if self._task is not None:
            shutdown_timeout = float(getattr(getattr(self.bot, "settings", None), "shutdown_timeout_seconds", 15.0))
            drain_timeout = float(
                getattr(
                    getattr(self.bot, "settings", None),
                    "jobs_drain_timeout_seconds",
                    max(1.0, shutdown_timeout - 2.0),
                )
            )
            drain_timeout = min(drain_timeout, max(1.0, shutdown_timeout - 1.0))
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=drain_timeout)
            except TimeoutError:
                logger.warning("durable job worker drain timed out; cancelling current executor")
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
            finally:
                self._task = None

        bot = self.bot
        if bot is not None:
            bot.tree.remove_command("jobs", type=discord.AppCommandType.chat_input)
            if getattr(bot, "durable_jobs", None) is self.service:
                bot.durable_jobs = None
            if getattr(bot, "durable_job_worker", None) is self.worker:
                bot.durable_job_worker = None
            if getattr(bot, "durable_job_executor_registry", None) is not None:
                bot.durable_job_executor_registry = None
        if self.repository is not None:
            self.repository.close()
        self.repository = None
        self.service = None
        self.worker = None
        self.bot = None
