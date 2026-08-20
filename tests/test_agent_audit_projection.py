from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from typing import Any

import pytest

from yonerai_discord.agent_audit_projection import (
    AgentAuditCursor,
    AgentAuditEvent,
    AgentAuditPage,
    AgentAuditProjection,
    AgentAuditScope,
    AuditProjectionError,
    AuditProjectionFailureCode,
)
from yonerai_discord.db import Database


_STORE_A = "a" * 64
_STORE_B = "b" * 64


@dataclass(frozen=True, slots=True)
class _Record:
    id: int
    event: str = "agent.completed"
    plugin: str | None = "agent"
    guild_id: int | None = 10
    actor_id: int | None = 20
    created_at: str = "2026-08-08T00:00:00Z"

    @property
    def details(self) -> object:
        raise AssertionError("details must never be accessed")


class _Source:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self.rows = rows
        self.calls: list[tuple[int, int]] = []
        self.on_read: Any = None

    def list_audit(self, *, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
        self.calls.append((limit, after_id))
        if self.on_read is not None:
            self.on_read()
        return self.rows


def _scope(*, request: str = "request-a", session: str = "session-a") -> AgentAuditScope:
    return AgentAuditScope(guild_id=10, actor_id=20, request_binding=request, session_binding=session)


def _projection(
    source: _Source,
    *,
    current: list[object] | None = None,
    allowed: Any = None,
) -> AgentAuditProjection:
    current = [source] if current is None else current
    allowed = (lambda _scope: True) if allowed is None else allowed
    return AgentAuditProjection(
        source,
        source_current=lambda: current[0],
        authorization_current=allowed,
        store_binding_digest=_STORE_A,
    )


def _assert_code(error: pytest.ExceptionInfo[AuditProjectionError], code: AuditProjectionFailureCode) -> None:
    assert error.value.code is code
    assert str(error.value) == code.value


def test_projection_filters_exact_real_scope_without_reading_details() -> None:
    source = _Source(
        (
            _Record(id=1, guild_id=10, actor_id=99),
            _Record(id=2, guild_id=99, actor_id=20),
            _Record(id=3),
            _Record(id=4, plugin=None),
        )
    )
    scope = _scope()

    page = _projection(source).read_page(scope=scope, cursor=AgentAuditCursor.start(scope))

    assert [(event.id, event.event, event.plugin, event.created_at) for event in page.events] == [
        (3, "agent.completed", "agent", "2026-08-08T00:00:00Z"),
        (4, "agent.completed", None, "2026-08-08T00:00:00Z"),
    ]
    assert page.next_after_id == 4
    assert page.next_cursor.store_binding_digest == _STORE_A
    assert page.scanned_count == 4
    assert page.exhausted is True
    assert source.calls == [(1_000, 0)]


def test_empty_scope_page_advances_past_other_scope_rows() -> None:
    source = _Source(tuple(_Record(id=index, guild_id=99) for index in range(1, 8)))
    scope = _scope()

    page = _projection(source).read_page(scope=scope, cursor=AgentAuditCursor.start(scope))

    assert page.events == ()
    assert page.next_after_id == 7
    assert page.scanned_count == 7
    assert page.exhausted is True


def test_projection_returns_at_most_fifty_and_resumes_without_skipping() -> None:
    source = _Source(tuple(_Record(id=index) for index in range(1, 61)))
    scope = _scope()
    projection = _projection(source)

    page = projection.read_page(scope=scope, cursor=AgentAuditCursor.start(scope))

    assert len(page.events) == 50
    assert page.events[-1].id == 50
    assert page.next_after_id == 50
    assert page.scanned_count == 50
    assert page.exhausted is False


def test_cursor_replay_is_rejected_across_request_or_session_binding() -> None:
    source = _Source((_Record(id=1),))
    first = _scope()
    projection = _projection(source)

    for other in (_scope(request="request-b"), _scope(session="session-b")):
        with pytest.raises(AuditProjectionError) as error:
            projection.read_page(scope=other, cursor=AgentAuditCursor.start(first))
        _assert_code(error, AuditProjectionFailureCode.BINDING_MISMATCH)
    assert source.calls == []


def test_scope_only_cursor_is_only_a_start_cursor_and_next_cursor_is_composite() -> None:
    source = _Source((_Record(id=1),))
    scope = _scope()
    projection = _projection(source)

    page = projection.read_page(scope=scope, cursor=AgentAuditCursor.start(scope))

    assert page.next_cursor.binding_digest == scope.binding_digest
    assert page.next_cursor.store_binding_digest == _STORE_A

    legacy_resume = AgentAuditCursor(after_id=1, binding_digest=scope.binding_digest)
    with pytest.raises(AuditProjectionError) as error:
        projection.read_page(scope=scope, cursor=legacy_resume)
    _assert_code(error, AuditProjectionFailureCode.BINDING_MISMATCH)
    assert source.calls == [(1_000, 0)]


def test_cursor_cannot_cross_store_binding() -> None:
    source = _Source((_Record(id=1),))
    scope = _scope()
    cursor = _projection(source).read_page(scope=scope, cursor=AgentAuditCursor.start(scope)).next_cursor
    source.calls.clear()

    with pytest.raises(AuditProjectionError) as error:
        AgentAuditProjection(
            source,
            source_current=lambda: source,
            authorization_current=lambda _scope: True,
            store_binding_digest=_STORE_B,
        ).read_page(scope=scope, cursor=cursor)

    _assert_code(error, AuditProjectionFailureCode.BINDING_MISMATCH)
    assert source.calls == []


def test_fresh_authorization_receives_the_exact_requested_scope_before_read() -> None:
    source = _Source((_Record(id=1),))
    allowed_scope = _scope()
    denied_scope = AgentAuditScope(
        guild_id=10,
        actor_id=21,
        request_binding="request-b",
        session_binding="session-b",
    )
    seen: list[AgentAuditScope] = []

    def authorization_current(scope: AgentAuditScope) -> bool:
        seen.append(scope)
        return scope == allowed_scope

    with pytest.raises(AuditProjectionError) as error:
        _projection(source, allowed=authorization_current).read_page(
            scope=denied_scope,
            cursor=AgentAuditCursor.start(denied_scope),
        )

    _assert_code(error, AuditProjectionFailureCode.AUTHORIZATION_DENIED)
    assert seen == [denied_scope]
    assert source.calls == []


@pytest.mark.parametrize("revoke_at", [1, 2, 3])
def test_authorization_is_checked_before_after_and_immediately_before_return(revoke_at: int) -> None:
    source = _Source((_Record(id=1),))
    calls = 0

    def authorization_current(_scope: AgentAuditScope) -> bool:
        nonlocal calls
        calls += 1
        return calls < revoke_at

    scope = _scope()
    with pytest.raises(AuditProjectionError) as error:
        _projection(source, allowed=authorization_current).read_page(scope=scope, cursor=AgentAuditCursor.start(scope))
    _assert_code(error, AuditProjectionFailureCode.AUTHORIZATION_DENIED)
    assert len(source.calls) == (0 if revoke_at == 1 else 1)


@pytest.mark.parametrize("replace_at", ["before", "during", "final"])
def test_source_identity_replacement_fails_closed(replace_at: str) -> None:
    source = _Source((_Record(id=1),))
    replacement = _Source(())
    current: list[object] = [source]
    current_checks = 0

    if replace_at == "before":
        current[0] = replacement
    elif replace_at == "during":
        source.on_read = lambda: current.__setitem__(0, replacement)

    def source_current() -> object:
        nonlocal current_checks
        current_checks += 1
        if replace_at == "final" and current_checks == 3:
            current[0] = replacement
        return current[0]

    projection = AgentAuditProjection(
        source,
        source_current=source_current,
        authorization_current=lambda _scope: True,
        store_binding_digest=_STORE_A,
    )
    scope = _scope()
    with pytest.raises(AuditProjectionError) as error:
        projection.read_page(scope=scope, cursor=AgentAuditCursor.start(scope))
    _assert_code(error, AuditProjectionFailureCode.SOURCE_REPLACED)


@pytest.mark.parametrize(
    "rows",
    [
        (_Record(id=2), _Record(id=2)),
        (_Record(id=0),),
        (_Record(id=1, event="BAD EVENT"),),
        tuple(_Record(id=index) for index in range(1, 1_002)),
    ],
)
def test_malformed_source_is_a_fixed_content_free_failure(rows: tuple[object, ...]) -> None:
    source = _Source(rows)
    scope = _scope()

    with pytest.raises(AuditProjectionError) as error:
        _projection(source).read_page(scope=scope, cursor=AgentAuditCursor.start(scope))

    _assert_code(error, AuditProjectionFailureCode.MALFORMED_SOURCE)


def test_source_exception_is_not_exposed() -> None:
    class _FailedSource(_Source):
        def list_audit(self, *, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
            raise RuntimeError("secret query and private path")

    source = _FailedSource(())
    scope = _scope()
    with pytest.raises(AuditProjectionError) as error:
        _projection(source).read_page(scope=scope, cursor=AgentAuditCursor.start(scope))
    _assert_code(error, AuditProjectionFailureCode.SOURCE_READ_FAILED)
    assert "secret" not in repr(error.value)
    assert "path" not in repr(error.value)


def test_authorization_exception_is_a_fixed_denial() -> None:
    source = _Source((_Record(id=1),))

    def failed_authorization(_scope: AgentAuditScope) -> bool:
        raise RuntimeError("private authorization state")

    scope = _scope()
    with pytest.raises(AuditProjectionError) as error:
        _projection(source, allowed=failed_authorization).read_page(
            scope=scope,
            cursor=AgentAuditCursor.start(scope),
        )
    _assert_code(error, AuditProjectionFailureCode.AUTHORIZATION_DENIED)
    assert "private" not in repr(error.value)


def test_contracts_are_frozen_and_repr_omits_ids_bindings_and_event_values() -> None:
    scope = _scope(request="private-request", session="private-session")
    cursor = AgentAuditCursor.start(scope)
    event = AgentAuditEvent(
        id=42,
        event="private.event",
        plugin="private.plugin",
        created_at="2026-08-08T00:00:00Z",
    )
    page = AgentAuditPage(events=(event,), next_cursor=cursor, scanned_count=1, exhausted=True)

    rendered = " ".join(repr(value) for value in (scope, cursor, event, page))
    for forbidden in ("10", "20", "42", "private", scope.binding_digest):
        assert forbidden not in rendered
    with pytest.raises(FrozenInstanceError):
        scope.guild_id = 99  # type: ignore[misc]


def test_real_database_cursor_resumes_after_reopen_without_duplicates(tmp_path) -> None:
    path = tmp_path / "control.sqlite3"
    scope = _scope()

    first = Database(path)
    first.open()
    first.migrate()
    first.append_audit("agent.started", actor_id=20, guild_id=10, details={"private": "not projected"})
    first.append_audit("other.started", actor_id=99, guild_id=10)
    page = AgentAuditProjection(
        first,
        source_current=lambda: first,
        authorization_current=lambda _scope: True,
        store_binding_digest=first.agent_audit_store_binding_digest,
    ).read_page(scope=scope, cursor=AgentAuditCursor.start(scope))
    first.close()

    assert [event.event for event in page.events] == ["agent.started"]
    assert page.events[0].created_at.endswith("Z") and " " not in page.events[0].created_at
    assert page.next_after_id == 2
    assert page.exhausted is True

    reopened = Database(path)
    reopened.open()
    reopened.migrate()
    try:
        reopened.append_audit("agent.completed", actor_id=20, guild_id=10)
        resumed = AgentAuditProjection(
            reopened,
            source_current=lambda: reopened,
            authorization_current=lambda _scope: True,
            store_binding_digest=reopened.agent_audit_store_binding_digest,
        ).read_page(scope=scope, cursor=page.next_cursor)
    finally:
        reopened.close()

    assert [event.event for event in resumed.events] == ["agent.completed"]
    assert resumed.next_after_id == 3
    assert resumed.exhausted is True


def test_timezone_less_timestamp_is_rejected_as_malformed_source() -> None:
    source = _Source((_Record(id=1, created_at="2026-08-08T00:00:00"),))
    scope = _scope()

    with pytest.raises(AuditProjectionError) as error:
        _projection(source).read_page(scope=scope, cursor=AgentAuditCursor.start(scope))
    _assert_code(error, AuditProjectionFailureCode.MALFORMED_SOURCE)
