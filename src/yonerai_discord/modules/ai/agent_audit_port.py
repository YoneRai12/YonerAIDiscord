"""Request-bound internal read port for the redacted agent audit projection."""

from __future__ import annotations

import asyncio
import hmac
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from yonerai_discord.agent_audit_projection import (
    AgentAuditCursor,
    AgentAuditPage,
    AgentAuditProjection,
    AgentAuditScope,
    AuditProjectionError,
    AuditProjectionFailureCode,
)
from yonerai_discord.db import Database


SyncAuthorizationCurrent = Callable[[AgentAuditScope], bool]
FreshAuthorizationCurrent = Callable[[AgentAuditScope], Awaitable[bool]]
BindingCurrent = Callable[[], bool]
IdentityCurrent = Callable[[], object | None]
ClosingCurrent = Callable[[], bool]


@dataclass(frozen=True, slots=True, repr=False)
class AgentAuditReadBinding:
    """One request/session authorization envelope; never stored globally."""

    scope: AgentAuditScope
    authorization_current: SyncAuthorizationCurrent
    fresh_authorization_current: FreshAuthorizationCurrent
    runtime_binding_current: BindingCurrent
    request_binding_current: BindingCurrent

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AgentAuditScope):
            raise TypeError("scope must be an AgentAuditScope")
        callbacks = (
            self.authorization_current,
            self.fresh_authorization_current,
            self.runtime_binding_current,
            self.request_binding_current,
        )
        if any(not callable(callback) for callback in callbacks):
            raise TypeError("agent audit authorization callbacks must be callable")

    def __repr__(self) -> str:
        return "AgentAuditReadBinding()"


class AgentAuditReadPort(Protocol):
    async def read_page(
        self,
        *,
        binding: AgentAuditReadBinding,
        cursor: AgentAuditCursor,
    ) -> AgentAuditPage: ...


@dataclass(frozen=True, slots=True, repr=False)
class _ScopeBoundAuditSource:
    """Projection-shaped adapter over the Database's details-free scope query."""

    database: Database
    scope: AgentAuditScope

    def list_audit(self, *, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
        return self.database.list_agent_audit_projection(
            guild_id=self.scope.guild_id,
            actor_id=self.scope.actor_id,
            limit=limit,
            after_id=after_id,
        )

    def __repr__(self) -> str:
        return "_ScopeBoundAuditSource()"


class BoundAgentAuditReadPort:
    """AIPlugin-owned port that creates one short-lived projection per read."""

    __slots__ = (
        "_closing_current",
        "_database",
        "_database_current",
        "_owner_loop",
        "_port_current",
    )

    def __init__(
        self,
        database: Database,
        *,
        database_current: IdentityCurrent,
        port_current: IdentityCurrent,
        closing_current: ClosingCurrent,
    ) -> None:
        if not isinstance(database, Database) or database.is_open is not True:
            raise TypeError("agent audit source must be an open Database")
        if any(not callable(callback) for callback in (database_current, port_current, closing_current)):
            raise TypeError("agent audit lifecycle callbacks must be callable")
        try:
            owner_loop = asyncio.get_running_loop()
        except RuntimeError:
            owner_loop = None
        if owner_loop is None:
            raise RuntimeError("agent audit port requires a running event loop")
        self._database = database
        self._database_current = database_current
        self._owner_loop = owner_loop
        self._port_current = port_current
        self._closing_current = closing_current

    async def read_page(
        self,
        *,
        binding: AgentAuditReadBinding,
        cursor: AgentAuditCursor,
    ) -> AgentAuditPage:
        try:
            return await self._read_page(binding=binding, cursor=cursor)
        except asyncio.CancelledError:
            pass

        # Raise outside the except suite so a secret-bearing cancellation
        # message, exception chain, and callback traceback are unreachable.
        raise asyncio.CancelledError()

    async def _read_page(
        self,
        *,
        binding: AgentAuditReadBinding,
        cursor: AgentAuditCursor,
    ) -> AgentAuditPage:
        if not self._loop_identity_current():
            raise AuditProjectionError(AuditProjectionFailureCode.SOURCE_REPLACED)
        if not isinstance(binding, AgentAuditReadBinding):
            raise AuditProjectionError(AuditProjectionFailureCode.AUTHORIZATION_DENIED)
        if not isinstance(cursor, AgentAuditCursor) or not hmac.compare_digest(
            cursor.binding_digest,
            binding.scope.binding_digest,
        ):
            raise AuditProjectionError(AuditProjectionFailureCode.BINDING_MISMATCH)

        await self._require_fresh_authorization(binding)
        failure: AuditProjectionFailureCode | None = None
        try:
            source = _ScopeBoundAuditSource(self._database, binding.scope)
            projection = AgentAuditProjection(
                source,
                source_current=lambda: source if self._projection_source_current() is self._database else None,
                authorization_current=lambda scope: self._projection_authorization_current(binding, scope),
            )
            page = projection.read_page(scope=binding.scope, cursor=cursor)
        except AuditProjectionError as exc:
            if not self._source_identity_current() or not self._runtime_binding_current(binding):
                failure = AuditProjectionFailureCode.SOURCE_REPLACED
            elif isinstance(exc.code, AuditProjectionFailureCode):
                failure = exc.code
            else:
                failure = AuditProjectionFailureCode.MALFORMED_SOURCE
        except Exception:
            failure = (
                AuditProjectionFailureCode.SOURCE_READ_FAILED
                if self._source_identity_current() and self._runtime_binding_current(binding)
                else AuditProjectionFailureCode.SOURCE_REPLACED
            )
        if failure is not None:
            # Raise outside the except suite so secret-bearing source exceptions
            # cannot remain reachable through __context__ despite `from None`.
            raise AuditProjectionError(failure)

        await self._require_fresh_authorization(binding)
        if not self._page_is_bound(page, binding=binding, cursor=cursor):
            raise AuditProjectionError(AuditProjectionFailureCode.MALFORMED_SOURCE)
        await self._require_fresh_authorization(binding)
        return page

    def __repr__(self) -> str:
        return "BoundAgentAuditReadPort()"

    async def _require_fresh_authorization(self, binding: AgentAuditReadBinding) -> None:
        self._require_current(binding)
        try:
            value = binding.fresh_authorization_current(binding.scope)
            if not inspect.isawaitable(value):
                raise TypeError("fresh authorization must be awaitable")
            allowed = await value
        except Exception:
            allowed = False
        self._require_current(binding)
        if allowed is not True:
            raise AuditProjectionError(AuditProjectionFailureCode.AUTHORIZATION_DENIED)

    def _require_current(self, binding: AgentAuditReadBinding) -> None:
        if not self._source_identity_current():
            raise AuditProjectionError(AuditProjectionFailureCode.SOURCE_REPLACED)
        if not self._runtime_binding_current(binding):
            raise AuditProjectionError(AuditProjectionFailureCode.SOURCE_REPLACED)
        authorized = self._authorization_identity_current(binding)
        if not self._source_identity_current():
            raise AuditProjectionError(AuditProjectionFailureCode.SOURCE_REPLACED)
        if not self._runtime_binding_current(binding):
            raise AuditProjectionError(AuditProjectionFailureCode.SOURCE_REPLACED)
        if not authorized:
            raise AuditProjectionError(AuditProjectionFailureCode.AUTHORIZATION_DENIED)

    def _source_identity_current(self) -> bool:
        try:
            return bool(
                self._loop_identity_current()
                and self._closing_current() is False
                and self._port_current() is self
                and self._database_current() is self._database
                and self._database.is_open is True
            )
        except Exception:
            return False

    def _loop_identity_current(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._owner_loop and self._owner_loop.is_closed() is False
        except RuntimeError:
            return False

    @staticmethod
    def _runtime_binding_current(binding: AgentAuditReadBinding) -> bool:
        try:
            return binding.runtime_binding_current() is True
        except Exception:
            return False

    def _authorization_identity_current(self, binding: AgentAuditReadBinding) -> bool:
        try:
            return bool(
                binding.request_binding_current() is True
                and binding.authorization_current(binding.scope) is True
                and binding.request_binding_current() is True
            )
        except Exception:
            return False

    def _projection_source_current(self) -> Database | None:
        return self._database if self._source_identity_current() else None

    def _projection_authorization_current(
        self,
        binding: AgentAuditReadBinding,
        scope: AgentAuditScope,
    ) -> bool:
        return bool(
            scope is binding.scope
            and self._runtime_binding_current(binding)
            and self._authorization_identity_current(binding)
            and self._runtime_binding_current(binding)
        )

    @staticmethod
    def _page_is_bound(
        page: object,
        *,
        binding: AgentAuditReadBinding,
        cursor: AgentAuditCursor,
    ) -> bool:
        try:
            if not isinstance(page, AgentAuditPage) or not isinstance(page.next_cursor, AgentAuditCursor):
                return False
            if not hmac.compare_digest(page.next_cursor.binding_digest, cursor.binding_digest):
                return False
            if not hmac.compare_digest(page.next_cursor.binding_digest, binding.scope.binding_digest):
                return False
            if page.next_cursor.after_id < cursor.after_id:
                return False
            if not 0 <= page.scanned_count <= 1_000 or len(page.events) > 50:
                return False
            if page.scanned_count < len(page.events):
                return False
            previous_id = cursor.after_id
            for event in page.events:
                if event.id <= previous_id or event.id > page.next_cursor.after_id:
                    return False
                previous_id = event.id
            return True
        except Exception:
            return False


__all__ = [
    "AgentAuditReadBinding",
    "AgentAuditReadPort",
    "BoundAgentAuditReadPort",
]
