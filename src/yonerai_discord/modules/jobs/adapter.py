from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Literal

import discord
from discord import app_commands

from .domain import JobStatus
from .repository import SqliteJobRepository
from .worker import DurableJobWorker


logger = logging.getLogger(__name__)


class JobsGroup(app_commands.Group):
    """payload/receipt/error本文を表示しないdurable queue運用surface。"""

    def __init__(self, repository: SqliteJobRepository, worker: DurableJobWorker, bot: Any) -> None:
        super().__init__(name="jobs", description="永続ジョブの運用状態")
        self.repository = repository
        self.worker = worker
        self.bot = bot

    @app_commands.command(name="status", description="workerとqueueの状態を表示します")
    async def status(self, interaction: discord.Interaction) -> None:
        scope = await self._operator_scope(interaction)
        if scope is None:
            await self._deny(interaction)
            return
        guild_id, allow_global = scope
        counts = await asyncio.to_thread(
            self.repository.count_by_status,
            guild_id=guild_id,
            allow_global=allow_global,
        )
        snapshot = self.worker.snapshot()
        lines = [
            f"worker: `{'running' if snapshot.running else 'stopped'}` / draining: `{snapshot.draining}`",
            f"cycles: `{snapshot.cycles}` / processed: `{snapshot.processed}`",
            "queue: " + " / ".join(f"{status.value}={counts[status]}" for status in JobStatus),
            f"last_error_type: `{snapshot.last_error_type or 'none'}`",
        ]
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="list", description="payloadを隠してjob一覧を表示します")
    async def list_jobs(
        self,
        interaction: discord.Interaction,
        status: Literal[
            "all",
            "pending",
            "claimed",
            "succeeded",
            "failed",
            "uncertain",
            "skipped",
        ] = "all",
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        scope = await self._operator_scope(interaction)
        if scope is None:
            await self._deny(interaction)
            return
        guild_id, allow_global = scope
        selected_status = None if status == "all" else JobStatus(status)
        rows = await asyncio.to_thread(
            self.repository.list_summaries,
            guild_id=guild_id,
            status=selected_status,
            limit=limit,
            allow_global=allow_global,
        )
        if not rows:
            await interaction.response.send_message("jobはありません。", ephemeral=True)
            return
        lines = [
            f"`{row.id}` {row.kind} / **{row.status.value}** / "
            f"attempt {row.attempts}/{row.max_attempts} / <t:{int(row.available_at.timestamp())}:R>"
            for row in rows
        ]
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="retry", description="failed/skipped jobを監査付きで再queueします")
    async def retry(self, interaction: discord.Interaction, job_id: str) -> None:
        await self._mutate(interaction, "retry", job_id)

    @app_commands.command(name="cancel", description="未実行jobを監査付きでcancelします")
    async def cancel(self, interaction: discord.Interaction, job_id: str) -> None:
        await self._mutate(interaction, "cancel", job_id)

    async def _mutate(self, interaction: discord.Interaction, operation: str, job_id: str) -> None:
        scope = await self._operator_scope(interaction)
        if scope is None:
            await self._deny(interaction)
            return
        normalized_id = job_id.strip()
        if not normalized_id or len(normalized_id) > 128 or any(ord(char) < 33 for char in normalized_id):
            await interaction.response.send_message("job IDが不正です。", ephemeral=True)
            return
        guild_id, allow_global = scope
        if not self._audit_before(interaction, operation, normalized_id):
            await interaction.response.send_message(
                "監査logに記録できないため、変更しませんでした。",
                ephemeral=True,
            )
            return
        now = datetime.now(UTC)
        if operation == "retry":
            changed = await asyncio.to_thread(
                self.repository.retry_terminal,
                normalized_id,
                now,
                guild_id=guild_id,
                allow_global=allow_global,
            )
            message = (
                "jobを再queueしました。" if changed else "対象がないか、failed/skipped以外のため再queueしていません。"
            )
        else:
            changed = await asyncio.to_thread(
                self.repository.cancel_pending,
                normalized_id,
                now,
                guild_id=guild_id,
                allow_global=allow_global,
            )
            message = (
                "未実行jobをcancelしました。"
                if changed
                else "対象がないか、すでに実行開始済みのためcancelしていません。"
            )
        self._audit_result(interaction, operation, normalized_id, changed)
        await interaction.response.send_message(message, ephemeral=True)

    async def _operator_scope(self, interaction: discord.Interaction) -> tuple[int | None, bool] | None:
        checker = getattr(self.bot, "is_owner", None)
        if callable(checker):
            try:
                if bool(await checker(interaction.user)):
                    return None, True
            except Exception:
                pass
        permissions = getattr(interaction.user, "guild_permissions", None)
        if interaction.guild_id is not None and permissions is not None:
            if bool(getattr(permissions, "administrator", False) or getattr(permissions, "manage_guild", False)):
                return interaction.guild_id, False
        return None

    def _audit_before(self, interaction: discord.Interaction, operation: str, job_id: str) -> bool:
        database = getattr(self.bot, "database", None)
        append = getattr(database, "append_audit", None)
        if not callable(append):
            return False
        try:
            append(
                "jobs_operator_request",
                actor_id=interaction.user.id,
                guild_id=interaction.guild_id,
                plugin="jobs",
                details={"operation": operation, "job_id": job_id},
            )
        except Exception as exc:
            logger.error(
                "jobs_operator_audit_precondition_failed",
                extra={"error_type": type(exc).__name__},
            )
            return False
        return True

    def _audit_result(
        self,
        interaction: discord.Interaction,
        operation: str,
        job_id: str,
        changed: bool,
    ) -> None:
        database = getattr(self.bot, "database", None)
        append = getattr(database, "append_audit", None)
        if not callable(append):
            return
        try:
            append(
                "jobs_operator_result",
                actor_id=interaction.user.id,
                guild_id=interaction.guild_id,
                plugin="jobs",
                details={"operation": operation, "job_id": job_id, "changed": changed},
            )
        except Exception as exc:
            logger.error(
                "jobs_operator_result_audit_failed",
                extra={"error_type": type(exc).__name__},
            )

    @staticmethod
    async def _deny(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Bot所有者またはサーバー管理者専用です。",
            ephemeral=True,
        )
