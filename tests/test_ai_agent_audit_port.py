from __future__ import annotations

import asyncio
import threading
import traceback
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass, field

import pytest

from yonerai_discord.agent_audit_projection import (
    AgentAuditCursor,
    AgentAuditPage,
    AgentAuditScope,
    AuditProjectionError,
    AuditProjectionFailureCode,
)
from yonerai_discord.db import Database
from yonerai_discord.modules.ai.agent_audit_port import (
    AgentAuditReadBinding,
    BoundAgentAuditReadPort,
)


@dataclass
class _PortState:
    database_current: object
    active_port: object | None = None
    closing: bool = False
    sync_allowed: bool = True
    runtime_current: bool = True
    request_current: bool = True
    fresh_revoke_at: int | None = None
    fresh_hook: Callable[[int], None] | None = None
    fresh_calls: int = 0
    seen_scopes: list[AgentAuditScope] = field(default_factory=list)

    def authorization_current(self, scope: AgentAuditScope) -> bool:
        self.seen_scopes.append(scope)
        return self.sync_allowed

    async def fresh_authorization_current(self, scope: AgentAuditScope) -> bool:
        self.seen_scopes.append(scope)
        self.fresh_calls += 1
        if self.fresh_hook is not None:
            self.fresh_hook(self.fresh_calls)
        return self.fresh_revoke_at is None or self.fresh_calls < self.fresh_revoke_at


@dataclass(frozen=True, slots=True)
class _RecordWithoutDetails:
    id: int
    event: str = "agent.completed"
    plugin: str | None = "agent"
    guild_id: int | None = 10
    actor_id: int | None = 20
    created_at: str = "2026-08-20T00:00:00Z"

    @property
    def details(self) -> object:
        raise AssertionError("audit details must never be accessed")


@pytest.fixture
def audit_database(tmp_path) -> Database:
    database = Database(tmp_path / "control.sqlite3")
    database.open()
    database.migrate()
    database.append_audit(
        "agent.other-actor",
        actor_id=99,
        guild_id=10,
        details={"trace": "private-other-actor"},
    )
    database.append_audit(
        "agent.other-guild",
        actor_id=20,
        guild_id=99,
        details={"trace": "private-other-guild"},
    )
    database.append_audit(
        "agent.completed",
        actor_id=20,
        guild_id=10,
        plugin="ai",
        details={"trace": "private-matching-details"},
    )
    yield database
    database.close()


def _scope(*, request: str = "request-a", session: str = "session-a") -> AgentAuditScope:
    return AgentAuditScope(
        guild_id=10,
        actor_id=20,
        request_binding=request,
        session_binding=session,
    )


def _binding(scope: AgentAuditScope, state: _PortState) -> AgentAuditReadBinding:
    return AgentAuditReadBinding(
        scope=scope,
        authorization_current=state.authorization_current,
        fresh_authorization_current=state.fresh_authorization_current,
        runtime_binding_current=lambda: state.runtime_current,
        request_binding_current=lambda: state.request_current,
    )


def _bound_port(database: Database, state: _PortState) -> BoundAgentAuditReadPort:
    port = BoundAgentAuditReadPort(
        database,
        database_current=lambda: state.database_current,
        port_current=lambda: state.active_port,
        closing_current=lambda: state.closing,
    )
    state.active_port = port
    return port


def _assert_code(error: pytest.ExceptionInfo[AuditProjectionError], code: AuditProjectionFailureCode) -> None:
    assert error.value.code is code
    assert str(error.value) == code.value


@pytest.mark.asyncio
async def test_actual_database_read_is_request_bound_and_checks_fresh_authorization_three_times(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()

    def unsafe_general_read(*, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
        raise AssertionError("agent audit port must not fall back to the details-bearing audit query")

    monkeypatch.setattr(audit_database, "list_audit", unsafe_general_read)

    page = await port.read_page(binding=_binding(scope, state), cursor=AgentAuditCursor.start(scope))

    assert [(event.id, event.event, event.plugin) for event in page.events] == [(3, "agent.completed", "ai")]
    assert page.scanned_count == 1
    assert page.next_after_id == 3
    assert state.fresh_calls == 3
    assert state.seen_scopes and all(seen is scope for seen in state.seen_scopes)


@pytest.mark.asyncio
async def test_cursor_cannot_cross_request_or_session_binding(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    first = _scope()
    reads = 0
    original = audit_database.list_agent_audit_projection

    def counted_read(*, guild_id: int, actor_id: int, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
        nonlocal reads
        reads += 1
        return original(guild_id=guild_id, actor_id=actor_id, limit=limit, after_id=after_id)

    monkeypatch.setattr(audit_database, "list_agent_audit_projection", counted_read)
    cursor = AgentAuditCursor.start(first)
    for other in (_scope(request="request-b"), _scope(session="session-b")):
        with pytest.raises(AuditProjectionError) as error:
            await port.read_page(binding=_binding(other, state), cursor=cursor)
        _assert_code(error, AuditProjectionFailureCode.BINDING_MISMATCH)

    assert reads == 0
    assert state.fresh_calls == 0


@pytest.mark.asyncio
async def test_concurrent_request_bindings_do_not_mix_scope_or_cursor(
    audit_database: Database,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    first_scope = _scope(request="request-a", session="session-a")
    second_scope = AgentAuditScope(
        guild_id=10,
        actor_id=99,
        request_binding="request-b",
        session_binding="session-b",
    )
    release = asyncio.Event()
    first_fresh_calls = 0
    second_fresh_calls = 0
    initial_waiters = 0

    async def first_fresh(current_scope: AgentAuditScope) -> bool:
        nonlocal first_fresh_calls, initial_waiters
        assert current_scope is first_scope
        first_fresh_calls += 1
        if first_fresh_calls == 1:
            initial_waiters += 1
            if initial_waiters == 2:
                release.set()
            await release.wait()
        return True

    async def second_fresh(current_scope: AgentAuditScope) -> bool:
        nonlocal second_fresh_calls, initial_waiters
        assert current_scope is second_scope
        second_fresh_calls += 1
        if second_fresh_calls == 1:
            initial_waiters += 1
            if initial_waiters == 2:
                release.set()
            await release.wait()
        return True

    first_binding = AgentAuditReadBinding(
        scope=first_scope,
        authorization_current=lambda scope: scope is first_scope,
        fresh_authorization_current=first_fresh,
        runtime_binding_current=lambda: True,
        request_binding_current=lambda: True,
    )
    second_binding = AgentAuditReadBinding(
        scope=second_scope,
        authorization_current=lambda scope: scope is second_scope,
        fresh_authorization_current=second_fresh,
        runtime_binding_current=lambda: True,
        request_binding_current=lambda: True,
    )

    first_page, second_page = await asyncio.gather(
        port.read_page(binding=first_binding, cursor=AgentAuditCursor.start(first_scope)),
        port.read_page(binding=second_binding, cursor=AgentAuditCursor.start(second_scope)),
    )

    assert [event.id for event in first_page.events] == [3]
    assert [event.id for event in second_page.events] == [1]
    assert first_fresh_calls == second_fresh_calls == 3
    assert first_page.next_cursor.binding_digest == first_scope.binding_digest
    assert second_page.next_cursor.binding_digest == second_scope.binding_digest


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_at", [1, 2, 3])
async def test_fresh_authorization_revoke_at_each_required_point_fails_closed(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    revoke_at: int,
) -> None:
    state = _PortState(database_current=audit_database, fresh_revoke_at=revoke_at)
    port = _bound_port(audit_database, state)
    scope = _scope()
    reads = 0
    original = audit_database.list_agent_audit_projection

    def counted_read(*, guild_id: int, actor_id: int, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
        nonlocal reads
        reads += 1
        return original(guild_id=guild_id, actor_id=actor_id, limit=limit, after_id=after_id)

    monkeypatch.setattr(audit_database, "list_agent_audit_projection", counted_read)
    with pytest.raises(AuditProjectionError) as error:
        await port.read_page(binding=_binding(scope, state), cursor=AgentAuditCursor.start(scope))

    _assert_code(error, AuditProjectionFailureCode.AUTHORIZATION_DENIED)
    assert state.fresh_calls == revoke_at
    assert reads == (0 if revoke_at == 1 else 1)


@pytest.mark.asyncio
async def test_source_replacement_takes_precedence_over_simultaneous_fresh_denial(
    audit_database: Database,
) -> None:
    state = _PortState(database_current=audit_database, fresh_revoke_at=1)
    port = _bound_port(audit_database, state)
    scope = _scope()
    state.fresh_hook = lambda _call: setattr(state, "active_port", None)

    with pytest.raises(AuditProjectionError) as error:
        await port.read_page(binding=_binding(scope, state), cursor=AgentAuditCursor.start(scope))

    _assert_code(error, AuditProjectionFailureCode.SOURCE_REPLACED)
    assert state.fresh_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["sync_allowed", "runtime_current", "request_current"])
@pytest.mark.parametrize("stage", ["before", "during", "after", "final"])
async def test_sync_and_runtime_binding_revoke_fails_closed(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    stage: str,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()
    reads = 0
    original = audit_database.list_agent_audit_projection
    if stage == "before":
        setattr(state, field_name, False)
    elif stage in {"after", "final"}:
        revoke_at = 2 if stage == "after" else 3
        state.fresh_hook = lambda call: setattr(state, field_name, False) if call == revoke_at else None

    def revoking_read(*, guild_id: int, actor_id: int, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
        nonlocal reads
        reads += 1
        rows = original(guild_id=guild_id, actor_id=actor_id, limit=limit, after_id=after_id)
        if stage == "during":
            setattr(state, field_name, False)
        return rows

    monkeypatch.setattr(audit_database, "list_agent_audit_projection", revoking_read)
    with pytest.raises(AuditProjectionError) as error:
        await port.read_page(binding=_binding(scope, state), cursor=AgentAuditCursor.start(scope))

    expected = (
        AuditProjectionFailureCode.SOURCE_REPLACED
        if field_name == "runtime_current"
        else AuditProjectionFailureCode.AUTHORIZATION_DENIED
    )
    _assert_code(error, expected)
    assert reads == (0 if stage == "before" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["replace", "close"])
@pytest.mark.parametrize("stage", ["before", "during", "after", "final"])
async def test_database_replace_or_close_before_during_or_final_fails_as_source_replaced(
    audit_database: Database,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    stage: str,
) -> None:
    replacement = Database(tmp_path / "replacement.sqlite3")
    replacement.open()
    replacement.migrate()
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()
    original = audit_database.list_agent_audit_projection

    def mutate_source() -> None:
        if mutation == "replace":
            state.database_current = replacement
        else:
            audit_database.close()

    if stage == "before":
        mutate_source()
    elif stage == "during":

        def mutating_read(*, guild_id: int, actor_id: int, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
            mutate_source()
            return original(guild_id=guild_id, actor_id=actor_id, limit=limit, after_id=after_id)

        monkeypatch.setattr(audit_database, "list_agent_audit_projection", mutating_read)
    else:
        mutate_at = 2 if stage == "after" else 3
        state.fresh_hook = lambda call: mutate_source() if call == mutate_at else None

    try:
        with pytest.raises(AuditProjectionError) as error:
            await port.read_page(binding=_binding(scope, state), cursor=AgentAuditCursor.start(scope))
    finally:
        replacement.close()

    _assert_code(error, AuditProjectionFailureCode.SOURCE_REPLACED)
    rendered = repr(error.value) + str(error.value)
    assert "replacement.sqlite3" not in rendered
    assert "control.sqlite3" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("withdraw", AuditProjectionFailureCode.SOURCE_REPLACED),
        ("closing", AuditProjectionFailureCode.SOURCE_REPLACED),
    ],
)
@pytest.mark.parametrize("stage", ["before", "during", "after", "final"])
async def test_port_identity_and_closing_are_rechecked_through_return_boundary(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected: AuditProjectionFailureCode,
    stage: str,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()
    original = audit_database.list_agent_audit_projection

    def mutate_lifecycle() -> None:
        if mutation == "withdraw":
            state.active_port = None
        else:
            state.closing = True

    if stage == "before":
        mutate_lifecycle()
    elif stage == "during":

        def mutating_read(*, guild_id: int, actor_id: int, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
            rows = original(guild_id=guild_id, actor_id=actor_id, limit=limit, after_id=after_id)
            mutate_lifecycle()
            return rows

        monkeypatch.setattr(audit_database, "list_agent_audit_projection", mutating_read)
    else:
        mutate_at = 2 if stage == "after" else 3
        state.fresh_hook = lambda call: mutate_lifecycle() if call == mutate_at else None

    with pytest.raises(AuditProjectionError) as error:
        await port.read_page(binding=_binding(scope, state), cursor=AgentAuditCursor.start(scope))

    _assert_code(error, expected)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.asyncio
async def test_foreign_event_loop_rejects_before_lifecycle_auth_or_database_callbacks(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_calls: list[str] = []
    active_port: BoundAgentAuditReadPort | None = None

    def database_current() -> object:
        callback_calls.append("database")
        return audit_database

    def port_current() -> object | None:
        callback_calls.append("port")
        return active_port

    def closing_current() -> bool:
        callback_calls.append("closing")
        return False

    port = BoundAgentAuditReadPort(
        audit_database,
        database_current=database_current,
        port_current=port_current,
        closing_current=closing_current,
    )
    active_port = port
    state = _PortState(database_current=audit_database)
    scope = _scope()
    database_reads = 0

    def counted_read(*, guild_id: int, actor_id: int, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
        nonlocal database_reads
        database_reads += 1
        return ()

    monkeypatch.setattr(audit_database, "list_agent_audit_projection", counted_read)

    def foreign_read() -> AuditProjectionError:
        try:
            asyncio.run(port.read_page(binding=_binding(scope, state), cursor=AgentAuditCursor.start(scope)))
        except AuditProjectionError as exc:
            return exc
        raise AssertionError("foreign event loop read unexpectedly succeeded")

    error = await asyncio.to_thread(foreign_read)

    assert error.code is AuditProjectionFailureCode.SOURCE_REPLACED
    assert error.__cause__ is None
    assert error.__context__ is None
    assert callback_calls == []
    assert state.seen_scopes == []
    assert state.fresh_calls == 0
    assert database_reads == 0


@pytest.mark.asyncio
async def test_projection_read_and_all_bound_callbacks_stay_on_event_loop_thread(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()
    event_loop_thread = threading.get_ident()
    observed_threads: list[int] = []
    original = audit_database.list_agent_audit_projection

    def sync_authorization(current_scope: AgentAuditScope) -> bool:
        assert current_scope is scope
        observed_threads.append(threading.get_ident())
        return True

    async def fresh_authorization(current_scope: AgentAuditScope) -> bool:
        assert current_scope is scope
        observed_threads.append(threading.get_ident())
        return True

    def recording_current() -> bool:
        observed_threads.append(threading.get_ident())
        return True

    def recording_read(*, guild_id: int, actor_id: int, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
        observed_threads.append(threading.get_ident())
        return original(guild_id=guild_id, actor_id=actor_id, limit=limit, after_id=after_id)

    monkeypatch.setattr(audit_database, "list_agent_audit_projection", recording_read)
    binding = AgentAuditReadBinding(
        scope=scope,
        authorization_current=sync_authorization,
        fresh_authorization_current=fresh_authorization,
        runtime_binding_current=recording_current,
        request_binding_current=recording_current,
    )

    await port.read_page(binding=binding, cursor=AgentAuditCursor.start(scope))

    assert observed_threads
    assert set(observed_threads) == {event_loop_thread}


@pytest.mark.asyncio
async def test_short_lived_projection_never_reads_details_property(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()
    monkeypatch.setattr(
        audit_database,
        "list_agent_audit_projection",
        lambda *, guild_id, actor_id, limit=100, after_id=0: (_RecordWithoutDetails(id=after_id + 1),),
    )

    page = await port.read_page(binding=_binding(scope, state), cursor=AgentAuditCursor.start(scope))

    assert [(event.id, event.event) for event in page.events] == [(1, "agent.completed")]


@pytest.mark.asyncio
async def test_binding_port_and_callback_failure_do_not_expose_ids_bindings_or_private_error(
    audit_database: Database,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope(request="private-request-binding", session="private-session-binding")

    def failed_authorization(_scope: AgentAuditScope) -> bool:
        raise RuntimeError("private SQL path and owner token")

    async def fresh_authorization(_scope: AgentAuditScope) -> bool:
        return True

    binding = AgentAuditReadBinding(
        scope=scope,
        authorization_current=failed_authorization,
        fresh_authorization_current=fresh_authorization,
        runtime_binding_current=lambda: True,
        request_binding_current=lambda: True,
    )

    with pytest.raises(AuditProjectionError) as error:
        await port.read_page(binding=binding, cursor=AgentAuditCursor.start(scope))

    _assert_code(error, AuditProjectionFailureCode.AUTHORIZATION_DENIED)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    rendered = " ".join((repr(binding), repr(port), repr(error.value), str(error.value)))
    for forbidden in ("10", "20", "private", "owner token", scope.binding_digest):
        assert forbidden not in rendered
    with pytest.raises(FrozenInstanceError):
        binding.scope = _scope(request="changed")  # type: ignore[misc]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_origin",
    ["fresh_authorization", "database_read", "database_current", "sync_authorization", "row_property"],
)
async def test_private_callback_and_source_exceptions_are_removed_from_exception_chain(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    failure_origin: str,
) -> None:
    state = _PortState(database_current=audit_database)
    scope = _scope()
    port = _bound_port(audit_database, state)

    async def failed_fresh_authorization(_scope: AgentAuditScope) -> bool:
        raise RuntimeError("private fresh authorization token")

    binding = _binding(scope, state)
    if failure_origin == "fresh_authorization":
        binding = AgentAuditReadBinding(
            scope=scope,
            authorization_current=state.authorization_current,
            fresh_authorization_current=failed_fresh_authorization,
            runtime_binding_current=lambda: state.runtime_current,
            request_binding_current=lambda: state.request_current,
        )
        expected = AuditProjectionFailureCode.AUTHORIZATION_DENIED
    elif failure_origin == "database_read":

        def failed_read(*, guild_id: int, actor_id: int, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
            raise RuntimeError("private database path and owner token")

        monkeypatch.setattr(audit_database, "list_agent_audit_projection", failed_read)
        expected = AuditProjectionFailureCode.SOURCE_READ_FAILED
    elif failure_origin == "database_current":

        def failed_database_current() -> object:
            raise RuntimeError("private replacement database path")

        port = BoundAgentAuditReadPort(
            audit_database,
            database_current=failed_database_current,
            port_current=lambda: state.active_port,
            closing_current=lambda: state.closing,
        )
        state.active_port = port
        expected = AuditProjectionFailureCode.SOURCE_REPLACED
    elif failure_origin == "sync_authorization":

        def failed_sync_authorization(_scope: AgentAuditScope) -> bool:
            raise RuntimeError("private synchronous authorization state")

        binding = AgentAuditReadBinding(
            scope=scope,
            authorization_current=failed_sync_authorization,
            fresh_authorization_current=state.fresh_authorization_current,
            runtime_binding_current=lambda: state.runtime_current,
            request_binding_current=lambda: state.request_current,
        )
        expected = AuditProjectionFailureCode.AUTHORIZATION_DENIED
    else:

        class SecretRow:
            id = 1

            @property
            def guild_id(self) -> int:
                raise RuntimeError("private malformed row path")

        monkeypatch.setattr(
            audit_database,
            "list_agent_audit_projection",
            lambda *, guild_id, actor_id, limit=100, after_id=0: (SecretRow(),),
        )
        expected = AuditProjectionFailureCode.MALFORMED_SOURCE

    with pytest.raises(AuditProjectionError) as error:
        await port.read_page(binding=binding, cursor=AgentAuditCursor.start(scope))

    _assert_code(error, expected)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    rendered = repr(error.value) + "".join(traceback.format_exception(error.value))
    for forbidden in (
        "fresh authorization token",
        "database path and owner token",
        "replacement database path",
        "synchronous authorization state",
        "malformed row path",
    ):
        assert forbidden not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("not_literal_true", [1, None, object()], ids=["integer", "none", "truthy-object"])
async def test_fresh_authorization_requires_literal_true(
    audit_database: Database,
    not_literal_true: object,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()

    async def non_boolean_authorization(_scope: AgentAuditScope) -> object:
        return not_literal_true

    binding = AgentAuditReadBinding(
        scope=scope,
        authorization_current=state.authorization_current,
        fresh_authorization_current=non_boolean_authorization,
        runtime_binding_current=lambda: True,
        request_binding_current=lambda: True,
    )

    with pytest.raises(AuditProjectionError) as error:
        await port.read_page(binding=binding, cursor=AgentAuditCursor.start(scope))

    _assert_code(error, AuditProjectionFailureCode.AUTHORIZATION_DENIED)


@pytest.mark.asyncio
async def test_fresh_authorization_cancellation_is_content_free_and_remains_cancellation(
    audit_database: Database,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()

    async def cancelled_authorization(_scope: AgentAuditScope) -> bool:
        try:
            raise RuntimeError("private cancellation context")
        except RuntimeError:
            raise asyncio.CancelledError("private cancellation token")

    binding = AgentAuditReadBinding(
        scope=scope,
        authorization_current=state.authorization_current,
        fresh_authorization_current=cancelled_authorization,
        runtime_binding_current=lambda: True,
        request_binding_current=lambda: True,
    )

    with pytest.raises(asyncio.CancelledError) as error:
        await port.read_page(binding=binding, cursor=AgentAuditCursor.start(scope))

    assert str(error.value) == ""
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    rendered = "".join(traceback.format_exception(error.value))
    assert "private cancellation" not in rendered
    assert "cancelled_authorization" not in rendered


@pytest.mark.asyncio
async def test_task_cancellation_message_is_sanitized_and_task_remains_cancelled(
    audit_database: Database,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()
    authorization_started = asyncio.Event()
    wait_forever = asyncio.Event()

    async def blocked_authorization(_scope: AgentAuditScope) -> bool:
        authorization_started.set()
        await wait_forever.wait()
        return True

    binding = AgentAuditReadBinding(
        scope=scope,
        authorization_current=state.authorization_current,
        fresh_authorization_current=blocked_authorization,
        runtime_binding_current=lambda: True,
        request_binding_current=lambda: True,
    )
    task = asyncio.create_task(port.read_page(binding=binding, cursor=AgentAuditCursor.start(scope)))
    await authorization_started.wait()
    task.cancel("private task cancellation token")

    with pytest.raises(asyncio.CancelledError) as error:
        await task

    assert task.cancelled()
    assert str(error.value) == ""
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    rendered = "".join(traceback.format_exception(error.value))
    assert "private task cancellation" not in rendered
    assert "blocked_authorization" not in rendered


@pytest.mark.asyncio
async def test_database_cancellation_is_content_free_and_remains_cancellation(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()

    def cancelled_read(*, guild_id: int, actor_id: int, limit: int = 100, after_id: int = 0) -> tuple[object, ...]:
        try:
            raise RuntimeError("private database cancellation context")
        except RuntimeError:
            raise asyncio.CancelledError("private database cancellation token")

    monkeypatch.setattr(audit_database, "list_agent_audit_projection", cancelled_read)

    with pytest.raises(asyncio.CancelledError) as error:
        await port.read_page(binding=_binding(scope, state), cursor=AgentAuditCursor.start(scope))

    assert str(error.value) == ""
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    rendered = "".join(traceback.format_exception(error.value))
    assert "private database cancellation" not in rendered
    assert "cancelled_read" not in rendered


@pytest.mark.asyncio
async def test_projection_page_binding_postcondition_is_rechecked(
    audit_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yonerai_discord.modules.ai import agent_audit_port as port_module

    state = _PortState(database_current=audit_database)
    port = _bound_port(audit_database, state)
    scope = _scope()
    malformed_page = AgentAuditPage(
        events=(),
        next_cursor=AgentAuditCursor(after_id=0, binding_digest="0" * 64),
        scanned_count=0,
        exhausted=True,
    )

    class MalformedProjection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def read_page(self, *, scope: AgentAuditScope, cursor: AgentAuditCursor) -> AgentAuditPage:
            return malformed_page

    monkeypatch.setattr(port_module, "AgentAuditProjection", MalformedProjection)

    with pytest.raises(AuditProjectionError) as error:
        await port.read_page(binding=_binding(scope, state), cursor=AgentAuditCursor.start(scope))

    _assert_code(error, AuditProjectionFailureCode.MALFORMED_SOURCE)
    assert state.fresh_calls == 2
