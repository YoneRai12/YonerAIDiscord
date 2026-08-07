from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


_MAX_SQLITE_ID = 9_223_372_036_854_775_807
_MAX_SCAN = 1_000
_MAX_EVENTS = 50
_MAX_BINDING_LENGTH = 256
_MAX_TIMESTAMP_LENGTH = 64
_SUBJECT_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_LEGACY_SQLITE_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")


class AuditProjectionFailureCode(StrEnum):
    AUTHORIZATION_DENIED = "authorization_denied"
    SOURCE_REPLACED = "source_replaced"
    SOURCE_READ_FAILED = "source_read_failed"
    MALFORMED_SOURCE = "malformed_source"
    BINDING_MISMATCH = "binding_mismatch"


class AuditProjectionError(RuntimeError):
    """Content-free failure returned by the agent audit projection boundary."""

    __slots__ = ("code",)

    def __init__(self, code: AuditProjectionFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class AuditProjectionSource(Protocol):
    def list_audit(self, *, limit: int = 100, after_id: int = 0) -> tuple[object, ...]: ...


@dataclass(frozen=True, slots=True, repr=False)
class AgentAuditScope:
    guild_id: int
    actor_id: int
    request_binding: str
    session_binding: str

    def __post_init__(self) -> None:
        _require_positive_id(self.guild_id, "guild_id")
        _require_positive_id(self.actor_id, "actor_id")
        _require_opaque_binding(self.request_binding, "request_binding")
        _require_opaque_binding(self.session_binding, "session_binding")

    @property
    def binding_digest(self) -> str:
        payload = {
            "actor_id": str(self.actor_id),
            "guild_id": str(self.guild_id),
            "request_binding": self.request_binding,
            "schema": "yonerai.agent-audit-scope.v1",
            "session_binding": self.session_binding,
        }
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def __repr__(self) -> str:
        return "AgentAuditScope()"


@dataclass(frozen=True, slots=True, repr=False)
class AgentAuditCursor:
    after_id: int
    binding_digest: str

    def __post_init__(self) -> None:
        _require_non_negative_id(self.after_id, "after_id")
        if not isinstance(self.binding_digest, str) or len(self.binding_digest) != 64:
            raise ValueError("binding_digest must be a SHA-256 digest")
        if any(character not in "0123456789abcdef" for character in self.binding_digest):
            raise ValueError("binding_digest must be a SHA-256 digest")

    @classmethod
    def start(cls, scope: AgentAuditScope) -> AgentAuditCursor:
        if not isinstance(scope, AgentAuditScope):
            raise TypeError("scope must be an AgentAuditScope")
        return cls(after_id=0, binding_digest=scope.binding_digest)

    def __repr__(self) -> str:
        return "AgentAuditCursor()"


@dataclass(frozen=True, slots=True, repr=False)
class AgentAuditEvent:
    id: int
    event: str
    plugin: str | None
    created_at: str

    def __post_init__(self) -> None:
        _require_positive_id(self.id, "id")
        _require_subject(self.event, "event", maximum=100)
        if self.plugin is not None:
            _require_subject(self.plugin, "plugin", maximum=128)
        _require_timestamp(self.created_at)

    def __repr__(self) -> str:
        return "AgentAuditEvent()"


@dataclass(frozen=True, slots=True, repr=False)
class AgentAuditPage:
    events: tuple[AgentAuditEvent, ...]
    next_cursor: AgentAuditCursor
    scanned_count: int
    exhausted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple) or len(self.events) > _MAX_EVENTS:
            raise ValueError("events must be a tuple containing at most 50 items")
        if any(not isinstance(event, AgentAuditEvent) for event in self.events):
            raise TypeError("events must contain AgentAuditEvent values")
        if not isinstance(self.next_cursor, AgentAuditCursor):
            raise TypeError("next_cursor must be an AgentAuditCursor")
        if isinstance(self.scanned_count, bool) or not isinstance(self.scanned_count, int):
            raise TypeError("scanned_count must be an integer")
        if not 0 <= self.scanned_count <= _MAX_SCAN:
            raise ValueError("scanned_count must be between 0 and 1000")
        if not isinstance(self.exhausted, bool):
            raise TypeError("exhausted must be a boolean")

    @property
    def next_after_id(self) -> int:
        return self.next_cursor.after_id

    def __repr__(self) -> str:
        return "AgentAuditPage()"


class AgentAuditProjection:
    """Scope-bound, content-free projection over the existing append-only audit log."""

    __slots__ = ("_authorization_current", "_source", "_source_current")

    def __init__(
        self,
        source: AuditProjectionSource,
        *,
        source_current: Callable[[], object | None],
        authorization_current: Callable[[AgentAuditScope], bool],
    ) -> None:
        if not callable(getattr(source, "list_audit", None)):
            raise TypeError("source must expose list_audit")
        if not callable(source_current):
            raise TypeError("source_current must be callable")
        if not callable(authorization_current):
            raise TypeError("authorization_current must be callable")
        self._source = source
        self._source_current = source_current
        self._authorization_current = authorization_current

    def read_page(self, *, scope: AgentAuditScope, cursor: AgentAuditCursor) -> AgentAuditPage:
        if not isinstance(scope, AgentAuditScope):
            raise TypeError("scope must be an AgentAuditScope")
        if not isinstance(cursor, AgentAuditCursor):
            raise TypeError("cursor must be an AgentAuditCursor")
        if not hmac.compare_digest(cursor.binding_digest, scope.binding_digest):
            raise AuditProjectionError(AuditProjectionFailureCode.BINDING_MISMATCH)

        self._require_current(scope)
        try:
            rows = self._source.list_audit(limit=_MAX_SCAN, after_id=cursor.after_id)
        except Exception:
            raise AuditProjectionError(AuditProjectionFailureCode.SOURCE_READ_FAILED) from None
        self._require_current(scope)

        if not isinstance(rows, tuple) or len(rows) > _MAX_SCAN:
            raise AuditProjectionError(AuditProjectionFailureCode.MALFORMED_SOURCE)

        events: list[AgentAuditEvent] = []
        scanned_count = 0
        next_after_id = cursor.after_id
        previous_id = cursor.after_id
        try:
            for row in rows:
                row_id = _record_id(row)
                if row_id <= previous_id:
                    raise ValueError("audit rows must be strictly monotonic")
                previous_id = row_id
                next_after_id = row_id
                scanned_count += 1

                guild_id = _record_optional_id(row, "guild_id")
                actor_id = _record_optional_id(row, "actor_id")
                event = _record_subject(row, "event", maximum=100)
                plugin = _record_optional_subject(row, "plugin", maximum=128)
                created_at = _record_timestamp(row)

                if guild_id == scope.guild_id and actor_id == scope.actor_id:
                    events.append(
                        AgentAuditEvent(
                            id=row_id,
                            event=event,
                            plugin=plugin,
                            created_at=created_at,
                        )
                    )
                    if len(events) == _MAX_EVENTS:
                        break
        except Exception:
            raise AuditProjectionError(AuditProjectionFailureCode.MALFORMED_SOURCE) from None

        self._require_current(scope)
        exhausted = scanned_count == len(rows) and len(rows) < _MAX_SCAN
        return AgentAuditPage(
            events=tuple(events),
            next_cursor=AgentAuditCursor(after_id=next_after_id, binding_digest=cursor.binding_digest),
            scanned_count=scanned_count,
            exhausted=exhausted,
        )

    def _require_current(self, scope: AgentAuditScope) -> None:
        try:
            current_source = self._source_current()
        except Exception:
            raise AuditProjectionError(AuditProjectionFailureCode.SOURCE_REPLACED) from None
        if current_source is not self._source:
            raise AuditProjectionError(AuditProjectionFailureCode.SOURCE_REPLACED)
        try:
            allowed = self._authorization_current(scope)
        except Exception:
            raise AuditProjectionError(AuditProjectionFailureCode.AUTHORIZATION_DENIED) from None
        if allowed is not True:
            raise AuditProjectionError(AuditProjectionFailureCode.AUTHORIZATION_DENIED)
        try:
            current_source = self._source_current()
        except Exception:
            raise AuditProjectionError(AuditProjectionFailureCode.SOURCE_REPLACED) from None
        if current_source is not self._source:
            raise AuditProjectionError(AuditProjectionFailureCode.SOURCE_REPLACED)


def _require_positive_id(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not 1 <= value <= _MAX_SQLITE_ID:
        raise ValueError(f"{label} must be a positive 64-bit integer")


def _require_non_negative_id(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not 0 <= value <= _MAX_SQLITE_ID:
        raise ValueError(f"{label} must be a non-negative 64-bit integer")


def _require_opaque_binding(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not 1 <= len(value) <= _MAX_BINDING_LENGTH:
        raise ValueError(f"{label} must contain 1 to 256 characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{label} must not contain control characters")


def _require_subject(value: object, label: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{label} is malformed")
    if value != value.strip().lower() or any(character not in _SUBJECT_CHARACTERS for character in value):
        raise ValueError(f"{label} is malformed")


def _require_timestamp(value: object) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_TIMESTAMP_LENGTH:
        raise ValueError("created_at is malformed")
    if _TIMESTAMP.fullmatch(value) is None:
        raise ValueError("created_at is malformed")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    except ValueError:
        raise ValueError("created_at is malformed") from None


def _record_id(row: object) -> int:
    value = getattr(row, "id")
    _require_positive_id(value, "id")
    return value


def _record_optional_id(row: object, name: str) -> int | None:
    value = getattr(row, name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_SQLITE_ID:
        raise ValueError(f"{name} is malformed")
    return value


def _record_subject(row: object, name: str, *, maximum: int) -> str:
    value = getattr(row, name)
    _require_subject(value, name, maximum=maximum)
    return value


def _record_optional_subject(row: object, name: str, *, maximum: int) -> str | None:
    value = getattr(row, name)
    if value is None:
        return None
    _require_subject(value, name, maximum=maximum)
    return value


def _record_timestamp(row: object) -> str:
    value = getattr(row, "created_at")
    if isinstance(value, str) and _LEGACY_SQLITE_TIMESTAMP.fullmatch(value) is not None:
        try:
            datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise ValueError("created_at is malformed") from None
        return value.replace(" ", "T") + "Z"
    _require_timestamp(value)
    return value
