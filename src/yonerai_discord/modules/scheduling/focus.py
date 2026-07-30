from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from yonerai_discord.modules.jobs.domain import (
    Attempt,
    ExecutionContext,
    Job,
    JobStatus,
    Outcome,
    Receipt,
)
from yonerai_discord.modules.jobs.service import DurableJobService


FOCUS_TIMER_JOB_KIND = "scheduling.focus-timer"
FOCUS_TIMER_PAYLOAD_SCHEMA = "yonerai.scheduling.focus-timer.job.v1"
FOCUS_TIMER_COMPLETION_MESSAGE = "集中タイマーが終了しました。"
_TIMER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MIN_DURATION = timedelta(minutes=1)
_MAX_DURATION = timedelta(hours=4)
_PAYLOAD_KEYS = frozenset(
    {
        "schema",
        "timer_id",
        "owner_id",
        "guild_id",
        "source_channel_id",
        "destination_channel_id",
        "expires_at",
        "revision",
        "speak_on_complete",
    }
)


def _positive_id(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _revision(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("revision must be a positive integer")
    return value


def _utc_seconds(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0)


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


@dataclass(frozen=True, slots=True)
class FocusTimerBinding:
    timer_id: str
    owner_id: int
    guild_id: int
    source_channel_id: int
    destination_channel_id: int
    revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.timer_id, str):
            raise TypeError("timer_id must be a string")
        normalized = self.timer_id.strip().lower()
        if not _TIMER_ID_RE.fullmatch(normalized):
            raise ValueError("timer_id must match [a-z0-9][a-z0-9_-]{0,63}")
        object.__setattr__(self, "timer_id", normalized)
        _positive_id(self.owner_id, "owner_id")
        _positive_id(self.guild_id, "guild_id")
        _positive_id(self.source_channel_id, "source_channel_id")
        _positive_id(self.destination_channel_id, "destination_channel_id")
        _revision(self.revision)

    def to_mapping(self) -> dict[str, object]:
        return {
            "timer_id": self.timer_id,
            "owner_id": self.owner_id,
            "guild_id": self.guild_id,
            "source_channel_id": self.source_channel_id,
            "destination_channel_id": self.destination_channel_id,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class FocusReadAloudOverlay:
    binding: FocusTimerBinding
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "expires_at", _utc_seconds(self.expires_at, "expires_at"))


@dataclass(frozen=True, slots=True)
class FocusTimerRequest:
    binding: FocusTimerBinding
    expires_at: datetime
    speak_on_complete: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "expires_at", _utc_seconds(self.expires_at, "expires_at"))
        _strict_bool(self.speak_on_complete, "speak_on_complete")

    @property
    def overlay(self) -> FocusReadAloudOverlay:
        return FocusReadAloudOverlay(self.binding, self.expires_at)

    @property
    def binding_digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_payload()).encode("utf-8")).hexdigest()

    @property
    def timer_identity_digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.binding.to_mapping()).encode("utf-8")).hexdigest()

    @property
    def action_key(self) -> str:
        return f"focus.timer:{self.timer_identity_digest}"

    @property
    def job_id(self) -> str:
        return f"focus-timer-{self.timer_identity_digest[:32]}"

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": FOCUS_TIMER_PAYLOAD_SCHEMA,
            **self.binding.to_mapping(),
            "expires_at": self.expires_at.isoformat(),
            "speak_on_complete": self.speak_on_complete,
        }

    @classmethod
    def from_job(cls, job: Job) -> FocusTimerRequest:
        if job.kind != FOCUS_TIMER_JOB_KIND:
            raise ValueError("focus timer job kind mismatch")
        if set(job.payload) != _PAYLOAD_KEYS:
            raise ValueError("focus timer payload fields mismatch")
        payload = job.payload
        if payload.get("schema") != FOCUS_TIMER_PAYLOAD_SCHEMA:
            raise ValueError("focus timer payload schema mismatch")
        timer_id = payload.get("timer_id")
        expires_at = payload.get("expires_at")
        if not isinstance(timer_id, str) or not isinstance(expires_at, str):
            raise ValueError("focus timer payload identifiers are invalid")
        try:
            parsed_expires_at = datetime.fromisoformat(expires_at)
        except ValueError as exc:
            raise ValueError("focus timer expires_at is invalid") from exc
        request = cls(
            binding=FocusTimerBinding(
                timer_id=timer_id,
                owner_id=_payload_int(payload, "owner_id"),
                guild_id=_payload_int(payload, "guild_id"),
                source_channel_id=_payload_int(payload, "source_channel_id"),
                destination_channel_id=_payload_int(payload, "destination_channel_id"),
                revision=_payload_int(payload, "revision"),
            ),
            expires_at=parsed_expires_at,
            speak_on_complete=_strict_bool(payload.get("speak_on_complete"), "speak_on_complete"),
        )
        if (
            job.guild_id != request.binding.guild_id
            or job.revision.value != request.binding.revision
            or job.available_at < request.expires_at
            or job.action_key != request.action_key
            or job.id != request.job_id
        ):
            raise ValueError("focus timer job binding mismatch")
        return request


def _payload_int(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"focus timer {name} is invalid")
    return value


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class FocusTimerCurrentState:
    binding: FocusTimerBinding
    authorized: bool
    closing: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.binding, FocusTimerBinding):
            raise TypeError("binding must be a FocusTimerBinding")
        _strict_bool(self.authorized, "authorized")
        _strict_bool(self.closing, "closing")


class FocusVoiceState(StrEnum):
    NOT_REQUESTED = "not_requested"
    DELIVERED = "delivered"
    FAILED = "failed"
    REVOKED = "revoked"


class FocusDeliveryDisposition(StrEnum):
    DELIVERED = "delivered"
    DUPLICATE = "duplicate"


class FocusTextPreparationDisposition(StrEnum):
    READY = "ready"
    RETRYABLE_NOT_SENT = "retryable_not_sent"
    REJECTED = "rejected"


class FocusOverlayInstallDisposition(StrEnum):
    INSTALLED = "installed"
    EXISTING = "existing"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class FocusTimerReceipt:
    binding_digest: str = field(repr=False)
    text_delivered: bool
    voice_state: FocusVoiceState

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.binding_digest):
            raise ValueError("binding_digest must be a SHA-256 hex digest")
        _strict_bool(self.text_delivered, "text_delivered")

    @property
    def partial(self) -> bool:
        return self.text_delivered and self.voice_state in {
            FocusVoiceState.FAILED,
            FocusVoiceState.REVOKED,
        }

    def to_job_receipt(self) -> Receipt:
        return Receipt(
            details={
                "binding_digest": self.binding_digest,
                "text_delivered": self.text_delivered,
                "voice_state": self.voice_state.value,
                "partial": self.partial,
            }
        )


@dataclass(frozen=True, slots=True)
class FocusTimerScheduleReceipt:
    job_id: str = field(repr=False)
    binding_digest: str = field(repr=False)
    deduplicated: bool = False

    def __post_init__(self) -> None:
        _strict_bool(self.deduplicated, "deduplicated")


@dataclass(frozen=True, slots=True)
class FocusTimerCancelReceipt:
    binding_digest: str = field(repr=False)
    overlay_disabled: bool
    job_cancelled: bool


class FocusReadAloudOverlayStore(Protocol):
    @property
    def is_open(self) -> bool: ...

    def install(self, overlay: FocusReadAloudOverlay) -> FocusOverlayInstallDisposition: ...

    def stored(self, guild_id: int, timer_id: str) -> FocusReadAloudOverlay | None: ...

    def active(self, guild_id: int, timer_id: str) -> FocusReadAloudOverlay | None: ...

    def active_for_source(
        self,
        guild_id: int,
        source_channel_id: int,
    ) -> FocusReadAloudOverlay | None: ...

    def matches(self, overlay: FocusReadAloudOverlay) -> bool: ...

    def deactivate(self, overlay: FocusReadAloudOverlay) -> bool: ...


_OVERLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduling_focus_overlay_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_version INTEGER NOT NULL CHECK(schema_version = 1)
);
CREATE TABLE IF NOT EXISTS scheduling_focus_read_aloud_overlays (
    timer_id TEXT NOT NULL,
    owner_id INTEGER NOT NULL CHECK(owner_id > 0),
    guild_id INTEGER NOT NULL CHECK(guild_id > 0),
    source_channel_id INTEGER NOT NULL CHECK(source_channel_id > 0),
    destination_channel_id INTEGER NOT NULL CHECK(destination_channel_id > 0),
    revision INTEGER NOT NULL CHECK(revision > 0),
    expires_at TEXT NOT NULL,
    PRIMARY KEY(guild_id, timer_id)
);
"""


class SqliteFocusReadAloudOverlayStore:
    """Persistent, exact-CAS storage for temporary focus read-aloud routing."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = str(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                raise RuntimeError("focus overlay store is already open")
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(self._path, check_same_thread=False)
                connection.row_factory = sqlite3.Row
                connection.executescript(_OVERLAY_SCHEMA)
                connection.execute(
                    """INSERT OR IGNORE INTO scheduling_focus_overlay_metadata
                    (singleton, schema_version) VALUES (1, 1)"""
                )
                row = connection.execute(
                    """SELECT schema_version FROM scheduling_focus_overlay_metadata
                    WHERE singleton=1"""
                ).fetchone()
                if row is None or type(row["schema_version"]) is not int or row["schema_version"] != 1:
                    raise RuntimeError("focus overlay schema mismatch")
                connection.commit()
            except RuntimeError:
                if connection is not None:
                    connection.close()
                raise
            except Exception:
                if connection is not None:
                    connection.close()
                raise RuntimeError("focus overlay store unavailable") from None
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._connection is not None

    def install(self, overlay: FocusReadAloudOverlay) -> FocusOverlayInstallDisposition:
        connection = self._required()
        with self._lock:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """SELECT * FROM scheduling_focus_read_aloud_overlays
                    WHERE guild_id=? AND timer_id=?""",
                    (overlay.binding.guild_id, overlay.binding.timer_id),
                ).fetchone()
                if row is not None:
                    try:
                        current = self._row_to_overlay(row)
                    except (TypeError, ValueError):
                        connection.rollback()
                        return FocusOverlayInstallDisposition.CONFLICT
                    connection.rollback()
                    return (
                        FocusOverlayInstallDisposition.EXISTING
                        if current == overlay
                        else FocusOverlayInstallDisposition.CONFLICT
                    )
                connection.execute(
                    """INSERT INTO scheduling_focus_read_aloud_overlays
                    (timer_id, owner_id, guild_id, source_channel_id,
                     destination_channel_id, revision, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        overlay.binding.timer_id,
                        overlay.binding.owner_id,
                        overlay.binding.guild_id,
                        overlay.binding.source_channel_id,
                        overlay.binding.destination_channel_id,
                        overlay.binding.revision,
                        overlay.expires_at.isoformat(),
                    ),
                )
                connection.commit()
                return FocusOverlayInstallDisposition.INSTALLED
            except Exception:
                connection.rollback()
                raise RuntimeError("focus overlay store unavailable") from None

    def active(self, guild_id: int, timer_id: str) -> FocusReadAloudOverlay | None:
        try:
            overlay = self.stored(guild_id, timer_id)
        except RuntimeError:
            return None
        if overlay is None:
            return None
        try:
            now = _utc_seconds(self._clock(), "clock")
        except (TypeError, ValueError):
            return None
        return overlay if now < overlay.expires_at else None

    def stored(self, guild_id: int, timer_id: str) -> FocusReadAloudOverlay | None:
        try:
            _positive_id(guild_id, "guild_id")
        except (TypeError, ValueError):
            return None
        if not isinstance(timer_id, str) or not _TIMER_ID_RE.fullmatch(timer_id):
            return None
        try:
            return self._read(guild_id, timer_id)
        except RuntimeError:
            raise RuntimeError("focus overlay store unavailable") from None

    def active_for_source(
        self,
        guild_id: int,
        source_channel_id: int,
    ) -> FocusReadAloudOverlay | None:
        """Return the sole current overlay for one guild text source.

        The primary key intentionally remains ``(guild_id, timer_id)`` so a
        source can contain stale or competing rows after a crash.  Routing must
        never guess which one is authoritative: any ambiguity, corruption, or
        expiry is a fail-closed miss.
        """

        try:
            _positive_id(guild_id, "guild_id")
            _positive_id(source_channel_id, "source_channel_id")
            now = _utc_seconds(self._clock(), "clock")
        except (TypeError, ValueError):
            raise RuntimeError("focus overlay query is invalid") from None
        connection: sqlite3.Connection
        try:
            connection = self._required()
            with self._lock:
                rows = connection.execute(
                    """SELECT * FROM scheduling_focus_read_aloud_overlays
                    WHERE guild_id=? AND source_channel_id=?""",
                    (guild_id, source_channel_id),
                ).fetchall()
        except Exception:
            raise RuntimeError("focus overlay store unavailable") from None
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError("focus overlay source is ambiguous")
        try:
            overlay = self._row_to_overlay(rows[0])
        except (TypeError, ValueError):
            raise RuntimeError("focus overlay source is corrupt") from None
        return overlay if now < overlay.expires_at else None

    def matches(self, overlay: FocusReadAloudOverlay) -> bool:
        return self._read(overlay.binding.guild_id, overlay.binding.timer_id) == overlay

    def deactivate(self, overlay: FocusReadAloudOverlay) -> bool:
        binding = overlay.binding
        connection = self._required()
        with self._lock:
            try:
                cursor = connection.execute(
                    """DELETE FROM scheduling_focus_read_aloud_overlays
                    WHERE timer_id=? AND owner_id=? AND guild_id=?
                    AND source_channel_id=? AND destination_channel_id=?
                    AND revision=? AND expires_at=?""",
                    (
                        binding.timer_id,
                        binding.owner_id,
                        binding.guild_id,
                        binding.source_channel_id,
                        binding.destination_channel_id,
                        binding.revision,
                        overlay.expires_at.isoformat(),
                    ),
                )
                connection.commit()
                return cursor.rowcount == 1
            except Exception:
                connection.rollback()
                raise RuntimeError("focus overlay store unavailable") from None

    def _read(self, guild_id: int, timer_id: str) -> FocusReadAloudOverlay | None:
        connection = self._required()
        with self._lock:
            try:
                row = connection.execute(
                    """SELECT * FROM scheduling_focus_read_aloud_overlays
                    WHERE guild_id=? AND timer_id=?""",
                    (guild_id, timer_id),
                ).fetchone()
            except Exception:
                raise RuntimeError("focus overlay store unavailable") from None
        if row is None:
            return None
        try:
            return self._row_to_overlay(row)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _row_to_overlay(row: sqlite3.Row) -> FocusReadAloudOverlay:
        timer_id = row["timer_id"]
        expires_at = row["expires_at"]
        if type(timer_id) is not str or type(expires_at) is not str:
            raise ValueError("focus overlay row is corrupt")
        overlay = FocusReadAloudOverlay(
            binding=FocusTimerBinding(
                timer_id=timer_id,
                owner_id=_stored_int(row, "owner_id"),
                guild_id=_stored_int(row, "guild_id"),
                source_channel_id=_stored_int(row, "source_channel_id"),
                destination_channel_id=_stored_int(row, "destination_channel_id"),
                revision=_stored_int(row, "revision"),
            ),
            expires_at=datetime.fromisoformat(expires_at),
        )
        if overlay.expires_at.isoformat() != expires_at:
            raise ValueError("focus overlay row is corrupt")
        return overlay

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("focus overlay store is not open")
        return self._connection


def _stored_int(row: sqlite3.Row, name: str) -> int:
    value = row[name]
    if type(value) is not int:
        raise ValueError("focus overlay row is corrupt")
    return value


class FocusTextDeliveryPort(Protocol):
    async def prepare(
        self,
        binding: FocusTimerBinding,
        *,
        message_code: str,
        idempotency_key: str,
    ) -> FocusTextPreparationDisposition: ...

    async def deliver(
        self,
        binding: FocusTimerBinding,
        *,
        message_code: str,
        idempotency_key: str,
    ) -> FocusDeliveryDisposition: ...


class FocusVoiceDeliveryPort(Protocol):
    async def speak(
        self,
        binding: FocusTimerBinding,
        *,
        message_code: str,
        idempotency_key: str,
    ) -> FocusDeliveryDisposition: ...


FocusTimerCurrent = Callable[[FocusTimerBinding], Awaitable[FocusTimerCurrentState | None]]


class FocusTimerExecutorRegistryPort(Protocol):
    def register(self, kind: str, executor: object) -> None: ...

    def unregister_if_current(self, kind: str, executor: object) -> bool: ...


class FocusTimerExecutor:
    def __init__(
        self,
        *,
        overlays: FocusReadAloudOverlayStore,
        text_delivery: FocusTextDeliveryPort,
        current: FocusTimerCurrent,
        voice_delivery: FocusVoiceDeliveryPort | None = None,
        voice_current: FocusTimerCurrent | None = None,
    ) -> None:
        self._overlays = overlays
        self._text_delivery = text_delivery
        self._current = current
        self._voice_delivery = voice_delivery
        self._voice_current = current if voice_current is None else voice_current

    async def execute(self, job: Job, _attempt: Attempt, context: ExecutionContext) -> Outcome:
        try:
            request = FocusTimerRequest.from_job(job)
        except (TypeError, ValueError):
            if not context.complete_without_side_effect():
                return Outcome.skipped("focus_timer_not_allowed")
            return Outcome.nonretryable_failure("FocusTimerPayloadInvalid")

        try:
            overlay_matches = self._overlays.matches(request.overlay)
        except Exception:
            if not context.complete_without_side_effect():
                return Outcome.skipped("focus_timer_not_allowed")
            return Outcome.retryable_failure("FocusTimerOverlayStoreUnavailable")
        if not overlay_matches:
            if not context.complete_without_side_effect():
                return Outcome.skipped("focus_timer_not_allowed")
            return Outcome.skipped("focus_timer_cancelled")

        if not await self._is_current(request.binding):
            if not context.complete_without_side_effect():
                return Outcome.skipped("focus_timer_not_allowed")
            return Outcome.retryable_failure("FocusTimerAuthorizationUnavailable")

        try:
            preparation = await self._text_delivery.prepare(
                request.binding,
                message_code="focus_timer.completed",
                idempotency_key=request.binding_digest,
            )
        except Exception:
            if not context.complete_without_side_effect():
                return Outcome.skipped("focus_timer_not_allowed")
            return Outcome.retryable_failure("FocusTimerTextPreparationUnavailable")
        if preparation is FocusTextPreparationDisposition.RETRYABLE_NOT_SENT:
            if not context.complete_without_side_effect():
                return Outcome.skipped("focus_timer_not_allowed")
            return Outcome.retryable_failure("FocusTimerTextPreparationUnavailable")
        if preparation is not FocusTextPreparationDisposition.READY:
            if not context.complete_without_side_effect():
                return Outcome.skipped("focus_timer_not_allowed")
            return Outcome.nonretryable_failure("FocusTimerTextPreparationRejected")

        # The second check is the last await before the durable delivery boundary.
        if not await self._is_current(request.binding):
            if not context.complete_without_side_effect():
                return Outcome.skipped("focus_timer_not_allowed")
            return Outcome.retryable_failure("FocusTimerAuthorizationUnavailable")
        try:
            overlay_matches = self._overlays.matches(request.overlay)
        except Exception:
            if not context.complete_without_side_effect():
                return Outcome.skipped("focus_timer_not_allowed")
            return Outcome.retryable_failure("FocusTimerOverlayStoreUnavailable")
        if not overlay_matches:
            if not context.complete_without_side_effect():
                return Outcome.skipped("focus_timer_not_allowed")
            return Outcome.skipped("focus_timer_cancelled")
        if not context.begin_side_effect():
            return Outcome.skipped("focus_timer_not_allowed")

        try:
            text_state = await self._text_delivery.deliver(
                request.binding,
                message_code="focus_timer.completed",
                idempotency_key=request.binding_digest,
            )
        except Exception:
            return Outcome.uncertain("FocusTimerTextDeliveryUncertain")
        if (
            text_state is not FocusDeliveryDisposition.DELIVERED
            and text_state is not FocusDeliveryDisposition.DUPLICATE
        ):
            return Outcome.uncertain("FocusTimerTextDeliveryUncertain")
        try:
            overlay_disabled = self._overlays.deactivate(request.overlay)
        except Exception:
            return Outcome.uncertain("FocusTimerOverlayFinalizationUncertain")
        if not overlay_disabled:
            return Outcome.uncertain("FocusTimerOverlayFinalizationUncertain")

        voice_state = FocusVoiceState.NOT_REQUESTED
        if request.speak_on_complete:
            voice_state = await self._deliver_optional_voice(request, context)
        receipt = FocusTimerReceipt(
            binding_digest=request.binding_digest,
            text_delivered=True,
            voice_state=voice_state,
        )
        return Outcome.succeeded(receipt.to_job_receipt())

    async def _deliver_optional_voice(
        self,
        request: FocusTimerRequest,
        context: ExecutionContext,
    ) -> FocusVoiceState:
        if self._voice_delivery is None:
            return FocusVoiceState.FAILED
        if not await self._is_voice_current(request.binding):
            return FocusVoiceState.REVOKED
        if not context.still_allowed():
            return FocusVoiceState.REVOKED
        try:
            result = await self._voice_delivery.speak(
                request.binding,
                message_code="focus_timer.completed",
                idempotency_key=request.binding_digest,
            )
        except Exception:
            return FocusVoiceState.FAILED
        if result is not FocusDeliveryDisposition.DELIVERED and result is not FocusDeliveryDisposition.DUPLICATE:
            return FocusVoiceState.FAILED
        return FocusVoiceState.DELIVERED

    async def _is_current(self, binding: FocusTimerBinding) -> bool:
        return await self._callback_is_current(self._current, binding)

    async def _is_voice_current(self, binding: FocusTimerBinding) -> bool:
        return await self._callback_is_current(self._voice_current, binding)

    @staticmethod
    async def _callback_is_current(
        callback: FocusTimerCurrent,
        binding: FocusTimerBinding,
    ) -> bool:
        try:
            value = await callback(binding)
        except Exception:
            return False
        return (
            isinstance(value, FocusTimerCurrentState)
            and value.binding == binding
            and value.authorized is True
            and value.closing is False
        )


class FocusTimerService:
    def __init__(
        self,
        *,
        jobs: DurableJobService,
        overlays: FocusReadAloudOverlayStore,
        current: FocusTimerCurrent,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._jobs = jobs
        self._overlays = overlays
        self._current = current
        self._clock = clock or (lambda: datetime.now(UTC))

    async def schedule(self, request: FocusTimerRequest) -> FocusTimerScheduleReceipt:
        if not await self._is_current(request.binding):
            raise PermissionError("focus_timer_not_allowed")
        now = _utc_seconds(self._clock(), "clock")
        duration = request.expires_at - now
        if not _MIN_DURATION <= duration <= _MAX_DURATION:
            raise ValueError("focus timer duration must be between 1 minute and 4 hours")
        self._bound_job(request)
        if not await self._is_current(request.binding):
            raise PermissionError("focus_timer_not_allowed")
        self._cleanup_expired_terminal_overlay(request, now)
        existing = self._existing_nonterminal_job(request, cleanup_terminal_overlay=True)
        try:
            overlay_matches = self._overlays.matches(request.overlay)
        except Exception:
            raise RuntimeError("focus overlay store unavailable") from None
        if existing is not None and overlay_matches:
            return FocusTimerScheduleReceipt(
                job_id=existing.id,
                binding_digest=request.binding_digest,
                deduplicated=True,
            )
        if existing is not None and existing.status is JobStatus.CLAIMED:
            raise RuntimeError("focus timer is already running")
        if existing is not None:
            try:
                installed = self._overlays.install(request.overlay)
            except Exception:
                raise RuntimeError("focus overlay store unavailable") from None
            if (
                installed is not FocusOverlayInstallDisposition.INSTALLED
                and installed is not FocusOverlayInstallDisposition.EXISTING
            ):
                raise RuntimeError("focus timer overlay conflict")
            return FocusTimerScheduleReceipt(
                job_id=existing.id,
                binding_digest=request.binding_digest,
                deduplicated=True,
            )
        job = self._jobs.submit(
            action_key=request.action_key,
            revision=request.binding.revision,
            kind=FOCUS_TIMER_JOB_KIND,
            payload=request.to_payload(),
            guild_id=request.binding.guild_id,
            available_at=request.expires_at,
            max_attempts=5,
            job_id=request.job_id,
        )
        try:
            persisted = FocusTimerRequest.from_job(job)
        except (TypeError, ValueError):
            raise RuntimeError("focus timer job binding mismatch") from None
        if persisted != request or job.status not in {JobStatus.PENDING, JobStatus.CLAIMED}:
            raise RuntimeError("focus timer job is already terminal")
        try:
            installed = self._overlays.install(request.overlay)
        except Exception:
            self._best_effort_cancel_pending(request)
            raise RuntimeError("focus overlay store unavailable") from None
        if (
            installed is not FocusOverlayInstallDisposition.INSTALLED
            and installed is not FocusOverlayInstallDisposition.EXISTING
        ):
            self._best_effort_cancel_pending(request)
            raise RuntimeError("focus timer overlay conflict")
        return FocusTimerScheduleReceipt(
            job_id=job.id,
            binding_digest=request.binding_digest,
            deduplicated=False,
        )

    async def cancel(
        self,
        request: FocusTimerRequest,
        *,
        actor_id: int,
    ) -> FocusTimerCancelReceipt:
        _positive_id(actor_id, "actor_id")
        if actor_id != request.binding.owner_id or not await self._is_current(request.binding):
            return FocusTimerCancelReceipt(
                binding_digest=request.binding_digest,
                overlay_disabled=False,
                job_cancelled=False,
            )
        try:
            overlay_matches = self._overlays.matches(request.overlay)
        except Exception:
            overlay_matches = False
        if not overlay_matches:
            return FocusTimerCancelReceipt(
                binding_digest=request.binding_digest,
                overlay_disabled=False,
                job_cancelled=False,
            )
        if not await self._is_current(request.binding):
            return FocusTimerCancelReceipt(
                binding_digest=request.binding_digest,
                overlay_disabled=False,
                job_cancelled=False,
            )
        # Ordering is intentional: routing is revoked before the pending job is
        # cancelled, so a cancellation race cannot leave temporary read-aloud on.
        try:
            overlay_disabled = self._overlays.deactivate(request.overlay)
        except Exception:
            overlay_disabled = False
        if not overlay_disabled:
            return FocusTimerCancelReceipt(
                binding_digest=request.binding_digest,
                overlay_disabled=False,
                job_cancelled=False,
            )
        try:
            job_cancelled = self._jobs.repository.cancel_pending(
                request.job_id,
                _utc_seconds(self._clock(), "clock"),
                guild_id=request.binding.guild_id,
                allow_global=False,
            )
        except Exception:
            job_cancelled = False
        return FocusTimerCancelReceipt(
            binding_digest=request.binding_digest,
            overlay_disabled=True,
            job_cancelled=job_cancelled,
        )

    def _cleanup_expired_terminal_overlay(
        self,
        request: FocusTimerRequest,
        now: datetime,
    ) -> None:
        try:
            stored = self._overlays.stored(
                request.binding.guild_id,
                request.binding.timer_id,
            )
        except Exception:
            raise RuntimeError("focus overlay store unavailable") from None
        if stored is None or stored == request.overlay or now < stored.expires_at:
            return
        candidate = FocusTimerRequest(stored.binding, stored.expires_at)
        try:
            job = self._jobs.repository.get(candidate.job_id)
            persisted = None if job is None else FocusTimerRequest.from_job(job)
        except Exception:
            raise RuntimeError("focus timer stale overlay is not verifiable") from None
        if (
            job is None
            or persisted is None
            or persisted.overlay != stored
            or job.status in {JobStatus.PENDING, JobStatus.CLAIMED}
        ):
            return
        try:
            removed = self._overlays.deactivate(stored)
        except Exception:
            raise RuntimeError("focus overlay store unavailable") from None
        if not removed:
            raise RuntimeError("focus timer stale overlay changed")

    def resolve_active(
        self,
        guild_id: int,
        source_channel_id: int,
    ) -> FocusTimerRequest | None:
        """Reconstruct one cancellable focus timer without trusting UI state.

        ``FocusReadAloudOverlay`` deliberately contains only routing facts.  A
        restart-safe cancellation therefore rehydrates the request from its
        durable job and accepts it only when the job payload and active overlay
        are the exact same binding and expiry.
        """

        try:
            _positive_id(guild_id, "guild_id")
            _positive_id(source_channel_id, "source_channel_id")
            overlay = self._overlays.active_for_source(guild_id, source_channel_id)
        except Exception:
            return None
        if overlay is None:
            return None
        try:
            # ``job_id`` is derived from the binding only.  This candidate is
            # never authoritative; the persisted payload below is.
            candidate = FocusTimerRequest(overlay.binding, overlay.expires_at)
            job = self._jobs.repository.get(candidate.job_id)
            if job is None or job.status not in {JobStatus.PENDING, JobStatus.CLAIMED}:
                return None
            request = FocusTimerRequest.from_job(job)
        except Exception:
            return None
        if request.binding != overlay.binding or request.overlay != overlay:
            return None
        return request

    async def cancel_active(
        self,
        guild_id: int,
        source_channel_id: int,
        *,
        actor_id: int,
    ) -> FocusTimerCancelReceipt | None:
        """Cancel the sole active source route, or fail closed on uncertainty."""

        try:
            _positive_id(actor_id, "actor_id")
        except (TypeError, ValueError):
            return None
        request = self.resolve_active(guild_id, source_channel_id)
        if request is None:
            return None
        try:
            return await self.cancel(request, actor_id=actor_id)
        except Exception:
            return None

    def _existing_nonterminal_job(
        self,
        request: FocusTimerRequest,
        *,
        cleanup_terminal_overlay: bool,
    ) -> Job | None:
        existing = self._bound_job(request)
        if existing is not None and existing.status not in {JobStatus.PENDING, JobStatus.CLAIMED}:
            if cleanup_terminal_overlay:
                try:
                    if self._overlays.matches(request.overlay):
                        self._overlays.deactivate(request.overlay)
                except Exception:
                    raise RuntimeError("focus overlay store unavailable") from None
            raise RuntimeError("focus timer job is already terminal")
        return existing

    def _bound_job(self, request: FocusTimerRequest) -> Job | None:
        existing = self._jobs.repository.get(request.job_id)
        if existing is None:
            return None
        try:
            persisted = FocusTimerRequest.from_job(existing)
        except (TypeError, ValueError):
            raise RuntimeError("focus timer job binding mismatch") from None
        if persisted != request:
            raise RuntimeError("focus timer job binding mismatch")
        return existing

    def _best_effort_cancel_pending(self, request: FocusTimerRequest) -> None:
        try:
            self._jobs.repository.cancel_pending(
                request.job_id,
                _utc_seconds(self._clock(), "clock"),
                guild_id=request.binding.guild_id,
                allow_global=False,
            )
        except Exception:
            return

    async def _is_current(self, binding: FocusTimerBinding) -> bool:
        try:
            value = await self._current(binding)
        except Exception:
            return False
        return (
            isinstance(value, FocusTimerCurrentState)
            and value.binding == binding
            and value.authorized is True
            and value.closing is False
        )


class FocusTimerExecutorRegistration:
    """Exact-identity registration lease for later plugin composition."""

    def __init__(
        self,
        registry: FocusTimerExecutorRegistryPort,
        executor: FocusTimerExecutor,
    ) -> None:
        self._registry = registry
        self._executor = executor
        self._closed = False
        registry.register(FOCUS_TIMER_JOB_KIND, executor)

    def close(self) -> bool:
        if self._closed:
            return True
        removed = self._registry.unregister_if_current(FOCUS_TIMER_JOB_KIND, self._executor)
        if removed:
            self._closed = True
        return removed
