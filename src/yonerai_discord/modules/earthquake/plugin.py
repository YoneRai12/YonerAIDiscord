from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import discord

from .adapter import DiscordEarthquakeNotifier, EarthquakeGroup
from .client import P2PQuakeClient
from .repository import SqliteEarthquakeRepository
from .service import EarthquakeFeedWorker, EarthquakeService, ExponentialBackoff


EARTHQUAKE_DELIVERY_CAPABILITY_ID = "cap-run-earthquake-delivery"
_EARTHQUAKE_POLICY_MODULE_IDS = frozenset(
    {
        "operations.earthquake",
        "operations.scheduling-notification",
    }
)
_PROCESS_GUARD = threading.Lock()
_PROCESS_OWNER: object | None = None


class EarthquakePlugin:
    def __init__(self) -> None:
        self.repository: SqliteEarthquakeRepository | None = None
        self.client: P2PQuakeClient | None = None
        self.service: EarthquakeService | None = None
        self.worker: EarthquakeFeedWorker | None = None
        self.bot: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._claimed = False
        self._command_registered = False
        self._worker_factory: Callable[[], EarthquakeFeedWorker] | None = None
        self._worker_lock = asyncio.Lock()
        self._stopping = False

    async def start(self, bot: Any) -> None:
        if self.bot is not None:
            return
        self._stopping = False
        self.bot = bot
        try:
            settings = getattr(bot, "settings", None)
            database_path = _database_path(settings)
            repository = SqliteEarthquakeRepository(database_path)
            client = P2PQuakeClient(
                session=getattr(bot, "earthquake_http_session", None),
                timeout_seconds=_float_setting(settings, "earthquake_http_timeout_seconds", 10.0, minimum=0.1),
                heartbeat_seconds=_optional_positive_float_setting(
                    settings,
                    "earthquake_ws_heartbeat_seconds",
                    default=30.0,
                ),
                max_history_response_bytes=_int_setting(
                    settings,
                    "earthquake_max_history_response_bytes",
                    1_048_576,
                    minimum=1_024,
                    maximum=8_388_608,
                ),
            )
            self.repository = repository
            self.client = client
            repository.open()
            await client.start()
            delivery_policy = getattr(bot, "earthquake_delivery_policy", None)
            if not callable(delivery_policy):
                delivery_policy = self._central_delivery_policy
            notifier = DiscordEarthquakeNotifier(bot, delivery_policy=delivery_policy)
            service = EarthquakeService(
                client,
                repository,
                notifier,
                dedupe_capacity=_int_setting(settings, "earthquake_dedupe_capacity", 4096, minimum=1, maximum=100_000),
                dedupe_retention_seconds=_float_setting(
                    settings,
                    "earthquake_dedupe_retention_seconds",
                    604_800.0,
                    minimum=60.0,
                    maximum=31_536_000.0,
                ),
                eew_max_age_seconds=_float_setting(
                    settings,
                    "earthquake_eew_max_age_seconds",
                    120.0,
                    minimum=1.0,
                ),
                latest_cache_seconds=_float_setting(
                    settings,
                    "earthquake_latest_cache_seconds",
                    5.0,
                    minimum=1.0,
                ),
            )
            backoff_base = _float_setting(
                settings,
                "earthquake_reconnect_base_seconds",
                1.0,
                minimum=0.01,
            )
            backoff_max = _float_setting(
                settings,
                "earthquake_reconnect_max_seconds",
                60.0,
                minimum=backoff_base,
            )
            gap_fill_limit = _int_setting(
                settings,
                "earthquake_gap_fill_limit",
                25,
                minimum=1,
                maximum=100,
            )
            jitter_ratio = _float_setting(
                settings,
                "earthquake_reconnect_jitter_ratio",
                0.2,
                minimum=0.0,
                maximum=1.0,
            )
            self.service = service
            self._worker_factory = lambda: EarthquakeFeedWorker(
                client,
                service,
                gap_fill_limit=gap_fill_limit,
                backoff=ExponentialBackoff(
                    base_seconds=backoff_base,
                    maximum_seconds=backoff_max,
                    jitter_ratio=jitter_ratio,
                ),
            )
            bot.tree.add_command(
                EarthquakeGroup(
                    repository,
                    service,
                    on_subscriptions_changed=self.reconcile_subscriptions,
                )
            )
            self._command_registered = True
            bot.earthquake_repository = repository
            bot.earthquake_service = service
            bot.earthquake_feed_worker = None
            await self.reconcile_subscriptions()
        except BaseException:
            await self._cleanup(remove_command=True)
            raise

    async def stop(self) -> None:
        await self._cleanup(remove_command=True)

    async def begin_close(self) -> None:
        self._stopping = True
        async with self._worker_lock:
            await self._stop_worker_locked()

    async def on_module_policy_changed(
        self,
        module_id: str,
        _enabled: bool,
        _guild_id: int | None,
    ) -> None:
        if module_id in _EARTHQUAKE_POLICY_MODULE_IDS:
            await self.reconcile_subscriptions()

    async def on_capability_policy_changed(
        self,
        capability_id: str,
        _enabled: bool,
        _guild_id: int | None,
    ) -> None:
        if capability_id == EARTHQUAKE_DELIVERY_CAPABILITY_ID:
            await self.reconcile_subscriptions()

    async def reconcile_subscriptions(self) -> None:
        repository = self.repository
        if repository is None:
            raise RuntimeError("earthquake repository is unavailable")
        async with self._worker_lock:
            if self._stopping:
                await self._stop_worker_locked()
                return
            subscriptions = await asyncio.to_thread(repository.list_enabled)
            if any(self._central_delivery_policy(subscription, None) for subscription in subscriptions):
                await self._start_worker_locked()
            else:
                await self._stop_worker_locked()

    async def _start_worker_locked(self) -> None:
        task = self._task
        if task is not None and not task.done():
            return
        if task is not None:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                if self.service is not None:
                    self.service.record_error(exc)
            self._task = None
            self.worker = None
            if self._claimed:
                _release_process_connection(self)
                self._claimed = False

        factory = self._worker_factory
        if factory is None:
            raise RuntimeError("earthquake worker factory is unavailable")
        _claim_process_connection(self)
        self._claimed = True
        try:
            worker = factory()
            task = asyncio.create_task(worker.run(), name="p2pquake-websocket-feed")
        except BaseException:
            _release_process_connection(self)
            self._claimed = False
            raise
        self.worker = worker
        self._task = task
        if self.bot is not None:
            self.bot.earthquake_feed_worker = worker

    async def _stop_worker_locked(self) -> None:
        worker = self.worker
        task = self._task
        if worker is not None:
            worker.request_stop()
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        bot = self.bot
        if bot is not None and getattr(bot, "earthquake_feed_worker", None) is worker:
            bot.earthquake_feed_worker = None
        self._task = None
        self.worker = None
        if self._claimed:
            _release_process_connection(self)
            self._claimed = False

    async def _cleanup(self, *, remove_command: bool) -> None:
        self._stopping = True
        async with self._worker_lock:
            await self._stop_worker_locked()

        bot = self.bot
        if bot is not None:
            if remove_command and self._command_registered:
                with suppress(Exception):
                    bot.tree.remove_command("earthquake", type=discord.AppCommandType.chat_input)
            for attribute, expected in (
                ("earthquake_repository", self.repository),
                ("earthquake_service", self.service),
                ("earthquake_feed_worker", None),
            ):
                with suppress(Exception):
                    if getattr(bot, attribute, None) is expected:
                        delattr(bot, attribute)
        if self.client is not None:
            with suppress(Exception):
                await self.client.close()
        if self.repository is not None:
            with suppress(Exception):
                self.repository.close()
        self.repository = None
        self.client = None
        self.service = None
        self.worker = None
        self.bot = None
        self._command_registered = False
        self._worker_factory = None
        if self._claimed:
            _release_process_connection(self)
            self._claimed = False

    def _central_delivery_policy(self, subscription: Any, _event: Any) -> bool:
        bot = self.bot
        if bot is None or bool(getattr(bot, "is_closing", False)) or self._stopping:
            return False
        registry = getattr(bot, "capability_registry", None)
        if registry is None:
            return False
        try:
            status = registry.capability_status(EARTHQUAKE_DELIVERY_CAPABILITY_ID, subscription.guild_id)
        except Exception:
            return False
        return bool(getattr(status, "executable", False))


def _claim_process_connection(owner: object) -> None:
    global _PROCESS_OWNER
    with _PROCESS_GUARD:
        if _PROCESS_OWNER is not None and _PROCESS_OWNER is not owner:
            raise RuntimeError("another process-wide P2PQuake websocket is already active")
        _PROCESS_OWNER = owner


def _release_process_connection(owner: object) -> None:
    global _PROCESS_OWNER
    with _PROCESS_GUARD:
        if _PROCESS_OWNER is owner:
            _PROCESS_OWNER = None


def _float_setting(
    settings: Any,
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        return default
    return value


def _optional_positive_float_setting(settings: Any, name: str, *, default: float | None = None) -> float | None:
    value = getattr(settings, name, default)
    if value in (None, "", 0, 0.0, "0"):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) and normalized > 0 else None


def _int_setting(
    settings: Any,
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return normalized if minimum <= normalized <= maximum else default


def _database_path(settings: Any) -> Path:
    value = getattr(settings, "database_path", "data/yonerai.sqlite3")
    try:
        return Path(value)
    except TypeError:
        return Path("data/yonerai.sqlite3")
