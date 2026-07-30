"""Discord message scopeとSQLite監査へCapability Brokerを接続する境界。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .broker import CapabilityBroker
from .contract import (
    AuditRecord,
    CapabilityBinding,
    CapabilityKind,
    CapabilityRequest,
    MediaCapabilityInput,
    PermissionClass,
)


CurrentCheck = Callable[[], bool | Awaitable[bool]]


class AuditDatabase(Protocol):
    def append_audit(
        self,
        event: str,
        *,
        actor_id: int,
        details: dict[str, object],
        plugin: str,
        guild_id: int,
    ) -> int: ...


class SQLiteCapabilityAuditSink:
    """本文・URLを保存せず、既存append-only auditへ固定metadataだけを書く。"""

    def __init__(self, database: AuditDatabase, *, database_current: CurrentCheck) -> None:
        if not callable(getattr(database, "append_audit", None)) or not callable(database_current):
            raise TypeError("durable capability audit is unavailable")
        self._database = database
        self._database_current = database_current

    async def append(self, record: AuditRecord) -> None:
        if not isinstance(record, AuditRecord):
            raise TypeError("record must be an AuditRecord")
        if not await _current(self._database_current):
            raise RuntimeError("durable capability audit changed")
        details: dict[str, object] = {
            "request_id": record.request_id,
            "request_digest": record.request_digest,
            "conversation_id": record.binding.conversation_id,
            "capability": record.capability.value,
            "outcome": record.outcome.value,
            "backend_id": record.backend_id,
            "identity_digest": record.identity_digest,
            "artifact_id": record.artifact_id,
            "output_sha256": record.output_sha256,
            "failure_code": record.failure_code,
        }
        await asyncio.to_thread(
            self._database.append_audit,
            f"capability_broker.{record.outcome.value}",
            actor_id=record.binding.actor_id,
            guild_id=record.binding.guild_id,
            plugin="media_inspection",
            details=details,
        )
        if not await _current(self._database_current):
            raise RuntimeError("durable capability audit changed")


class BrokeredDiscordMediaInspectionAdapter:
    """既存Discord adapter surfaceを保ち、Hyper-V実行だけBrokerへ委譲する。"""

    requires_external_ai_consent = False

    def __init__(self, broker: CapabilityBroker, *, broker_current: CurrentCheck) -> None:
        if not isinstance(broker, CapabilityBroker) or not callable(broker_current):
            raise TypeError("managed capability broker is unavailable")
        self._broker = broker
        self._broker_current = broker_current
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    def begin_close(self) -> None:
        self._closing = True

    async def inspect_for_message(
        self,
        message: Any,
        url: str,
        instruction: str,
        authorization_current: CurrentCheck,
    ) -> str | None:
        scope = _message_scope(message)
        if self._closing or scope is None or not callable(authorization_current):
            return None
        guild_id, channel_id, actor_id, message_id = scope
        binding = CapabilityBinding(
            actor_id=actor_id,
            guild_id=guild_id,
            conversation_id=channel_id,
        )
        try:
            request = CapabilityRequest(
                request_id=f"discord:{message_id}",
                idempotency_key=f"discord:{guild_id}:{channel_id}:{message_id}",
                binding=binding,
                capability=CapabilityKind.MEDIA_INSPECTION,
                payload=MediaCapabilityInput(source_url=url, instruction=instruction),
            )

            async def permission_current(
                current_binding: CapabilityBinding,
                permission: PermissionClass,
            ) -> bool:
                return (
                    not self._closing
                    and current_binding == binding
                    and permission is PermissionClass.READ_PUBLIC_MEDIA
                    and _message_scope(message) == scope
                    and await _current(self._broker_current)
                )

            async def broker_authorization_current() -> bool:
                return (
                    not self._closing
                    and _message_scope(message) == scope
                    and await _current(self._broker_current)
                    and await _current(authorization_current)
                )

            if not await broker_authorization_current():
                return None
            result = await self._broker.execute(
                request,
                permission_current=permission_current,
                authorization_current=broker_authorization_current,
            )
            if not await broker_authorization_current():
                return None
            return result.artifact.read_text(binding=binding)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None


async def _current(check: CurrentCheck) -> bool:
    try:
        value = check()
        if inspect.isawaitable(value):
            value = await value
        return value is True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


def _message_scope(message: object) -> tuple[int, int, int, int] | None:
    values = (
        getattr(getattr(message, "guild", None), "id", None),
        getattr(getattr(message, "channel", None), "id", None),
        getattr(getattr(message, "author", None), "id", None),
        getattr(message, "id", None),
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        return None
    guild_id, channel_id, actor_id, message_id = values
    return guild_id, channel_id, actor_id, message_id


__all__ = [
    "AuditDatabase",
    "BrokeredDiscordMediaInspectionAdapter",
    "SQLiteCapabilityAuditSink",
]
