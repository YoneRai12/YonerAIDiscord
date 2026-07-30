from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord

from .focus import (
    FocusDeliveryDisposition,
    FOCUS_TIMER_COMPLETION_MESSAGE,
    FocusReadAloudOverlayStore,
    FocusTextPreparationDisposition,
    FocusTimerBinding,
    FocusTimerCurrentState,
    FocusTimerExecutor,
    FocusTimerExecutorRegistration,
    FocusTimerService,
    SqliteFocusReadAloudOverlayStore,
)


_FOCUS_COMPLETION_CODE = "focus_timer.completed"
_FOCUS_CAPABILITY_ID = "cap-run-music-read-aloud-message"
_JOBS_EXECUTE_CAPABILITY_ID = "cap-run-jobs-execute"
_DELIVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduling_focus_delivery_intents (
    idempotency_key TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL CHECK(guild_id > 0),
    source_channel_id INTEGER NOT NULL CHECK(source_channel_id > 0),
    state TEXT NOT NULL CHECK(state IN ('prepared', 'sending', 'delivered')),
    prepared_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at TEXT
);
"""


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


class FocusTimerDiscordTextDelivery:
    """Durable, content-free completion intent plus the final Discord send seam."""

    def __init__(
        self,
        path: str | Path,
        *,
        source_channel: Callable[[FocusTimerBinding], Any | None],
    ) -> None:
        self._path = str(path)
        self._source_channel = source_channel
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                raise RuntimeError("focus text delivery is already open")
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(self._path, check_same_thread=False)
                connection.execute(_DELIVERY_SCHEMA)
                self._migrate_delivery_state(connection)
                connection.commit()
            except Exception:
                if connection is not None:
                    connection.close()
                raise RuntimeError("focus text delivery unavailable") from None
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.close()

    async def prepare(
        self,
        binding: FocusTimerBinding,
        *,
        message_code: str,
        idempotency_key: str,
    ) -> FocusTextPreparationDisposition:
        if message_code != _FOCUS_COMPLETION_CODE or not _valid_digest(idempotency_key):
            return FocusTextPreparationDisposition.REJECTED
        if self._source_channel(binding) is None:
            return FocusTextPreparationDisposition.REJECTED
        try:
            with self._lock:
                connection = self._required()
                row = connection.execute(
                    """SELECT guild_id, source_channel_id, state
                    FROM scheduling_focus_delivery_intents WHERE idempotency_key=?""",
                    (idempotency_key,),
                ).fetchone()
                expected = (binding.guild_id, binding.source_channel_id)
                if row is not None:
                    if tuple(row[:2]) != expected or row[2] == "sending":
                        return FocusTextPreparationDisposition.REJECTED
                    return (
                        FocusTextPreparationDisposition.READY
                        if row[2] in {"prepared", "delivered"}
                        else FocusTextPreparationDisposition.REJECTED
                    )
                connection.execute(
                    """INSERT INTO scheduling_focus_delivery_intents
                    (idempotency_key, guild_id, source_channel_id, state)
                    VALUES (?, ?, ?, 'prepared')""",
                    (idempotency_key, *expected),
                )
                connection.commit()
        except Exception:
            return FocusTextPreparationDisposition.RETRYABLE_NOT_SENT
        return FocusTextPreparationDisposition.READY

    async def deliver(
        self,
        binding: FocusTimerBinding,
        *,
        message_code: str,
        idempotency_key: str,
    ) -> FocusDeliveryDisposition:
        if message_code != _FOCUS_COMPLETION_CODE or not _valid_digest(idempotency_key):
            raise RuntimeError("focus text delivery rejected")
        with self._lock:
            connection = self._required()
            row = connection.execute(
                """SELECT guild_id, source_channel_id, state
                FROM scheduling_focus_delivery_intents WHERE idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
            if row is None or tuple(row[:2]) != (binding.guild_id, binding.source_channel_id):
                raise RuntimeError("focus text delivery binding changed")
            if row[2] == "delivered":
                return FocusDeliveryDisposition.DUPLICATE
            if row[2] != "prepared":
                raise RuntimeError("focus text delivery state invalid")
            updated = connection.execute(
                """UPDATE scheduling_focus_delivery_intents SET state='sending'
                WHERE idempotency_key=? AND guild_id=? AND source_channel_id=?
                AND state='prepared'""",
                (idempotency_key, binding.guild_id, binding.source_channel_id),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("focus text delivery claim unavailable")
            connection.commit()
        channel = self._source_channel(binding)
        if channel is None:
            raise RuntimeError("focus text delivery source unavailable")
        send = getattr(channel, "send", None)
        if not callable(send):
            raise RuntimeError("focus text delivery source is not messageable")
        await send(
            FOCUS_TIMER_COMPLETION_MESSAGE,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        with self._lock:
            connection = self._required()
            updated = connection.execute(
                """UPDATE scheduling_focus_delivery_intents
                SET state='delivered', delivered_at=CURRENT_TIMESTAMP
                WHERE idempotency_key=? AND guild_id=? AND source_channel_id=? AND state='sending'""",
                (idempotency_key, binding.guild_id, binding.source_channel_id),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("focus text delivery finalization unavailable")
            connection.commit()
        return FocusDeliveryDisposition.DELIVERED

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("focus text delivery unavailable")
        return self._connection

    @staticmethod
    def _migrate_delivery_state(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            """SELECT sql FROM sqlite_master
            WHERE type='table' AND name='scheduling_focus_delivery_intents'"""
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise RuntimeError("focus text delivery schema unavailable")
        if "'sending'" in row[0]:
            return
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("scheduling_focus_delivery_intents_v1_migration",),
            ).fetchone()
            is not None
        ):
            raise RuntimeError("focus text delivery migration unavailable")
        connection.execute(
            """ALTER TABLE scheduling_focus_delivery_intents
            RENAME TO scheduling_focus_delivery_intents_v1_migration"""
        )
        connection.execute(_DELIVERY_SCHEMA)
        connection.execute(
            """INSERT INTO scheduling_focus_delivery_intents
            (idempotency_key, guild_id, source_channel_id, state, prepared_at, delivered_at)
            SELECT idempotency_key, guild_id, source_channel_id, state, prepared_at, delivered_at
            FROM scheduling_focus_delivery_intents_v1_migration"""
        )
        connection.execute("DROP TABLE scheduling_focus_delivery_intents_v1_migration")


@dataclass(slots=True, repr=False)
class FocusTimerRuntime:
    """Scheduling-owned composition lease for the durable focus timer executor."""

    plugin: Any
    bot: Any
    jobs: Any
    executor_registry: Any
    guard: Any
    overlays: FocusReadAloudOverlayStore
    text_delivery: FocusTimerDiscordTextDelivery
    voice_delivery: Any
    service: FocusTimerService
    executor: FocusTimerExecutor
    registration: FocusTimerExecutorRegistration
    _guard_registry: Any
    _source_leases: dict[FocusTimerBinding, Any]
    _closed: bool = False

    @classmethod
    def open(cls, *, plugin: Any, bot: Any, path: str | Path) -> FocusTimerRuntime | None:
        jobs = getattr(bot, "durable_jobs", None)
        executor_registry = getattr(bot, "durable_job_executor_registry", None)
        guard = getattr(bot, "capability_guard", None)
        guard_registry = getattr(guard, "registry", None)
        voice_delivery = getattr(bot, "music_read_aloud_service", None)
        if (
            jobs is None
            or not callable(getattr(jobs, "submit", None))
            or getattr(jobs, "repository", None) is None
            or executor_registry is None
            or not callable(getattr(executor_registry, "register", None))
            or not callable(getattr(executor_registry, "unregister_if_current", None))
            or not callable(getattr(guard, "evaluate_fresh_member", None))
            or not callable(getattr(guard, "currently_allowed", None))
            or guard_registry is None
            or not callable(getattr(voice_delivery, "speak", None))
            or getattr(voice_delivery, "available", False) is not True
        ):
            return None
        if any(
            getattr(bot, attribute, None) is not None
            for attribute in (
                "scheduling_focus_timer_runtime",
                "scheduling_focus_timer_service",
                "scheduling_focus_overlay_store",
                "scheduling_focus_executor",
            )
        ):
            return None

        overlays = SqliteFocusReadAloudOverlayStore(path)
        text_delivery = FocusTimerDiscordTextDelivery(path, source_channel=lambda binding: None)
        registration: FocusTimerExecutorRegistration | None = None
        try:
            overlays.open()
            runtime_ref: list[FocusTimerRuntime] = []
            text_delivery = FocusTimerDiscordTextDelivery(
                path,
                source_channel=lambda binding: runtime_ref[0]._take_source_lease(binding) if runtime_ref else None,
            )
            text_delivery.open()
            runtime = cls.__new__(cls)
            executor = FocusTimerExecutor(
                overlays=overlays,
                text_delivery=text_delivery,
                current=runtime._delivery_current,
                voice_delivery=voice_delivery,
                voice_current=runtime.authorization_current,
            )
            registration = FocusTimerExecutorRegistration(executor_registry, executor)
            service = FocusTimerService(
                jobs=jobs,
                overlays=overlays,
                current=runtime.authorization_current,
            )
            cls.__init__(
                runtime,
                plugin=plugin,
                bot=bot,
                jobs=jobs,
                executor_registry=executor_registry,
                guard=guard,
                overlays=overlays,
                text_delivery=text_delivery,
                voice_delivery=voice_delivery,
                service=service,
                executor=executor,
                registration=registration,
                _guard_registry=guard_registry,
                _source_leases={},
            )
            runtime_ref.append(runtime)
            return runtime
        except Exception:
            if registration is not None:
                try:
                    registration.close()
                except Exception:
                    pass
            text_delivery.close()
            overlays.close()
            return None

    @property
    def ready(self) -> bool:
        return not self._closed and self._base_identities_current()

    async def authorization_current(
        self,
        binding: FocusTimerBinding,
    ) -> FocusTimerCurrentState | None:
        return await self._evaluate(binding, lease_source=False)

    def publish(self) -> bool:
        if not self.ready:
            return False
        setattr(self.bot, "scheduling_focus_timer_runtime", self)
        setattr(self.bot, "scheduling_focus_timer_service", self.service)
        setattr(self.bot, "scheduling_focus_overlay_store", self.overlays)
        setattr(self.bot, "scheduling_focus_executor", self.executor)
        return self._identities_current()

    def begin_close(self) -> None:
        self._source_leases.clear()
        try:
            self.registration.close()
        except Exception:
            pass
        self.unpublish()

    def unpublish(self) -> None:
        for attribute, expected in (
            ("scheduling_focus_timer_runtime", self),
            ("scheduling_focus_timer_service", self.service),
            ("scheduling_focus_overlay_store", self.overlays),
            ("scheduling_focus_executor", self.executor),
        ):
            if getattr(self.bot, attribute, None) is expected:
                delattr(self.bot, attribute)

    def close(self) -> None:
        if self._closed:
            return
        self.begin_close()
        self._closed = True
        self.text_delivery.close()
        try:
            self.overlays.close()
        finally:
            self._source_leases.clear()

    async def _delivery_current(
        self,
        binding: FocusTimerBinding,
    ) -> FocusTimerCurrentState | None:
        return await self._evaluate(binding, lease_source=True)

    async def _evaluate(
        self,
        binding: FocusTimerBinding,
        *,
        lease_source: bool,
    ) -> FocusTimerCurrentState | None:
        if not self._identities_current():
            return None
        guild = self._guild(binding.guild_id)
        if guild is None:
            return None
        source = await self._fetch_channel(binding.source_channel_id, guild)
        destination = await self._fetch_channel(binding.destination_channel_id, guild)
        if source is None or destination is None or not callable(getattr(source, "send", None)):
            return None
        member = await self._fetch_member(guild, binding.owner_id)
        if member is None or getattr(member, "bot", False) is True:
            return None
        bot_member = await self._fetch_bot_member(guild)
        if bot_member is None or not self._bot_channel_permissions_current(
            source=source,
            destination=destination,
            bot_member=bot_member,
        ):
            return None
        try:
            decisions = [
                await self.guard.evaluate_fresh_member(
                    capability_id,
                    guild=guild,
                    member=member,
                )
                for capability_id in (
                    _FOCUS_CAPABILITY_ID,
                    _JOBS_EXECUTE_CAPABILITY_ID,
                )
            ]
            allowed = all(
                bool(getattr(decision, "allowed", False))
                and bool(
                    self.guard.currently_allowed(
                        capability_id,
                        guild_id=binding.guild_id,
                        user_id=binding.owner_id,
                        actor_level=getattr(decision, "actor_level", None),
                        floor=getattr(decision, "required_level", None),
                    )
                )
                for capability_id, decision in zip(
                    (_FOCUS_CAPABILITY_ID, _JOBS_EXECUTE_CAPABILITY_ID),
                    decisions,
                    strict=True,
                )
            )
        except Exception:
            return None
        if not allowed or not self._identities_current():
            return None
        if lease_source:
            try:
                stored = self.overlays.stored(
                    binding.guild_id,
                    binding.timer_id,
                )
            except Exception:
                return None
            if stored is None or stored.binding != binding:
                return None
            self._source_leases[binding] = source
        return FocusTimerCurrentState(binding=binding, authorized=True)

    def _identities_current(self) -> bool:
        return bool(
            self._base_identities_current()
            and getattr(self.bot, "scheduling_focus_timer_runtime", None) is self
            and getattr(self.bot, "scheduling_focus_timer_service", None) is self.service
            and getattr(self.bot, "scheduling_focus_overlay_store", None) is self.overlays
            and getattr(self.bot, "scheduling_focus_executor", None) is self.executor
        )

    def _base_identities_current(self) -> bool:
        return bool(
            not self._closed
            and not bool(getattr(self.plugin, "closing", True))
            and not bool(getattr(self.bot, "is_closing", False))
            and getattr(self.plugin, "bot", None) is self.bot
            and getattr(self.plugin, "focus_service", None) is self.service
            and getattr(self.plugin, "focus_overlay_store", None) is self.overlays
            and getattr(self.plugin, "focus_executor", None) is self.executor
            and getattr(self.bot, "scheduling_plugin", None) is self.plugin
            and getattr(self.bot, "durable_jobs", None) is self.jobs
            and getattr(self.bot, "durable_job_executor_registry", None) is self.executor_registry
            and getattr(self.bot, "capability_guard", None) is self.guard
            and getattr(self.guard, "registry", None) is self._guard_registry
            and getattr(self.bot, "music_read_aloud_service", None) is self.voice_delivery
            and getattr(self.voice_delivery, "available", False) is True
            and getattr(self.overlays, "is_open", False) is True
            and self.text_delivery.is_open
        )

    def _guild(self, guild_id: int) -> Any | None:
        getter = getattr(self.bot, "get_guild", None)
        try:
            guild = getter(guild_id) if callable(getter) else None
        except Exception:
            return None
        return guild if getattr(guild, "id", None) == guild_id else None

    async def _fetch_member(self, guild: Any, owner_id: int) -> Any | None:
        fetch = getattr(guild, "fetch_member", None)
        if not callable(fetch):
            return None
        try:
            member = await fetch(owner_id)
        except Exception:
            return None
        if getattr(member, "id", None) != owner_id or getattr(member, "guild", None) is not guild:
            return None
        return member

    async def _fetch_bot_member(self, guild: Any) -> Any | None:
        bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
        if isinstance(bot_user_id, bool) or not isinstance(bot_user_id, int) or bot_user_id <= 0:
            return None
        fetch = getattr(guild, "fetch_member", None)
        if not callable(fetch):
            return None
        try:
            member = await fetch(bot_user_id)
        except Exception:
            return None
        if (
            getattr(member, "id", None) != bot_user_id
            or getattr(member, "guild", None) is not guild
            or getattr(member, "bot", False) is not True
        ):
            return None
        return member

    async def _fetch_channel(self, channel_id: int, guild: Any) -> Any | None:
        fetch = getattr(self.bot, "fetch_channel", None)
        if not callable(fetch):
            return None
        try:
            channel = await fetch(channel_id)
        except Exception:
            return None
        if getattr(channel, "id", None) != channel_id or getattr(channel, "guild", None) is not guild:
            return None
        return channel

    def _take_source_lease(self, binding: FocusTimerBinding) -> Any | None:
        if not self._identities_current():
            return None
        return self._source_leases.pop(binding, None)

    @staticmethod
    def _bot_channel_permissions_current(
        *,
        source: Any,
        destination: Any,
        bot_member: Any,
    ) -> bool:
        source_permissions_for = getattr(source, "permissions_for", None)
        destination_permissions_for = getattr(destination, "permissions_for", None)
        if not callable(source_permissions_for) or not callable(destination_permissions_for):
            return False
        try:
            source_permissions = source_permissions_for(bot_member)
            destination_permissions = destination_permissions_for(bot_member)
        except Exception:
            return False
        return bool(
            getattr(source_permissions, "view_channel", False)
            and getattr(source_permissions, "read_message_history", False)
            and getattr(source_permissions, "send_messages", False)
            and getattr(destination_permissions, "view_channel", False)
            and getattr(destination_permissions, "connect", False)
            and getattr(destination_permissions, "speak", False)
        )
