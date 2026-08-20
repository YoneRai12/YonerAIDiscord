from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.agent_audit_projection import (
    AgentAuditCursor,
    AgentAuditScope,
    AuditProjectionError,
    AuditProjectionFailureCode,
)
from yonerai_discord.db import Database
from yonerai_discord.modules.ai import (
    AIPlugin,
    AIService,
    PackagingDependencyClass,
    _memory_context_recall_allowed,
)
from yonerai_discord.modules.ai.agent_audit_port import AgentAuditReadBinding
from yonerai_discord.modules.ai.orchestration_planner_adapter import AIServicePlannerPort
from yonerai_discord.plugin import PluginManager, PluginStatus
from yonerai_discord.runtime_manifests.ai_memory import MEMORY_CONTEXT_RECALL_CAPABILITY_ID


class BlockingAttachment:
    id = 700
    filename = "blocking.txt"
    content_type = "text/plain"
    size = 8

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def read(self, **_kwargs: object) -> bytes:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return b"unreachable"


class Message:
    def __init__(self, attachment: BlockingAttachment) -> None:
        self.id = 40
        self.content = "<@99> このファイルを説明して"
        self.guild = SimpleNamespace(id=10, owner_id=1)
        self.channel = SimpleNamespace(id=30)
        self.author = SimpleNamespace(id=20, bot=False)
        self.mentions = [SimpleNamespace(id=99)]
        self.webhook_id = None
        self.reference = None
        self.attachments = [attachment]
        self.replies: list[str] = []

    def is_system(self) -> bool:
        return False

    async def reply(self, content: str, **_kwargs: object) -> object:
        self.replies.append(content)
        return SimpleNamespace(id=1_040)


class Guard:
    def event_allowed(self, _capability_id: str, **_kwargs: object) -> bool:
        return True

    def currently_allowed(self, _capability_id: str, **_kwargs: object) -> bool:
        return True


class Service:
    available = True

    def __init__(self) -> None:
        self.calls = 0

    async def ask(self, _request: object) -> None:
        self.calls += 1
        raise AssertionError("provider must not run after attachment cancellation")


class RecordingGateway:
    async def start(self, _request: object) -> object:
        raise AssertionError("gateway must not run during composition test")

    def events(self, _run_id: str) -> object:
        raise AssertionError("gateway must not run during composition test")

    async def submit_result(self, _run_id: str, _result: object) -> None:
        raise AssertionError("gateway must not run during composition test")

    async def cancel(self, _run_id: str) -> None:
        raise AssertionError("gateway must not run during composition test")


def _planner_lifecycle_bot() -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=99),
        settings=SimpleNamespace(
            ai_conversation_ttl_seconds=7_200,
            ai_conversation_max_turns=12,
            ai_conversation_max_sessions=128,
            ai_conversation_max_total_binary_bytes=64 * 1024 * 1024,
            ai_attachment_max_file_bytes=8 * 1024 * 1024,
            ai_attachment_max_total_bytes=16 * 1024 * 1024,
            ai_attachment_max_files=4,
            ai_base_url="",
            ai_mention_enabled=True,
            ai_mention_guild_ids=frozenset({10}),
            ai_mention_allow_all_guilds=False,
            ai_reply_continuation_enabled=False,
            ai_attachments_enabled=False,
            ai_timeout_seconds=30.0,
            ai_admission_global_concurrency=4,
            ai_admission_max_waiters=32,
            ai_admission_wait_timeout_seconds=0.1,
            ai_admission_drain_timeout_seconds=0.1,
        ),
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
        capability_guard=Guard(),
        add_listener=lambda _listener, _name: None,
        remove_listener=lambda _listener, _name: None,
    )


@pytest.mark.asyncio
async def test_plugin_shares_one_neutral_gateway_and_context_builder_across_ai_surfaces() -> None:
    bot = _planner_lifecycle_bot()
    gateway = RecordingGateway()
    services: list[AIService] = []

    def gateway_factory(service: AIService) -> RecordingGateway:
        services.append(service)
        return gateway

    plugin = AIPlugin(
        execution_gateway_factory=gateway_factory,
        execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
    )

    await plugin.start(bot)

    assert services == [plugin.service]
    assert bot.ai_execution_gateway is gateway
    assert plugin._ai_group is not None
    assert plugin._mention_listener is not None
    assert plugin._ai_group._execution_gateway is gateway
    assert plugin._mention_listener.execution_gateway is gateway
    assert plugin._ai_group._context_builder is plugin._context_builder
    assert plugin._mention_listener.context_builder is plugin._context_builder
    assert plugin._ai_group._admission is plugin._admission
    assert plugin._mention_listener.admission is plugin._admission

    await plugin.stop()

    assert not hasattr(bot, "ai_execution_gateway")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_gateway", [RuntimeError("fixed factory failure"), object()])
async def test_plugin_manager_cleans_partial_start_when_gateway_factory_fails(
    invalid_gateway: object,
) -> None:
    bot = _planner_lifecycle_bot()
    should_fail = True

    def gateway_factory(_service: AIService) -> RecordingGateway:
        nonlocal should_fail
        if should_fail:
            if isinstance(invalid_gateway, BaseException):
                raise invalid_gateway
            return invalid_gateway  # type: ignore[return-value]
        return RecordingGateway()

    manager = PluginManager()
    manager.register(
        "ai",
        lambda: AIPlugin(
            execution_gateway_factory=gateway_factory,
            execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
        ),
    )

    assert await manager.enable("ai", bot) is False
    assert manager.status("ai") is PluginStatus.FAILED
    assert not hasattr(bot, "ai_execution_gateway")
    assert not hasattr(bot, "ai_conversation_store")
    assert not hasattr(bot, "ai_display_preferences")

    should_fail = False
    assert await manager.enable("ai", bot) is True
    assert manager.status("ai") is PluginStatus.RUNNING
    assert hasattr(bot, "ai_execution_gateway")

    assert await manager.disable("ai") is True
    assert not hasattr(bot, "ai_execution_gateway")


@pytest.mark.asyncio
async def test_plugin_publishes_only_owned_ready_planner_adapter_and_cleans_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AIService, "available", property(lambda _self: True))
    bot = _planner_lifecycle_bot()
    plugin = AIPlugin()

    await plugin.start(bot)

    assert bot.ai_orchestration_planner_ready is True
    assert isinstance(bot.ai_orchestration_planner_port, AIServicePlannerPort)
    assert bot.ai_orchestration_planner.model_port is bot.ai_orchestration_planner_port

    await plugin.stop()

    assert not hasattr(bot, "ai_orchestration_planner_port")
    assert not hasattr(bot, "ai_orchestration_planner")
    assert not hasattr(bot, "ai_orchestration_planner_ready")


@pytest.mark.asyncio
async def test_plugin_opt_in_uses_one_durable_engine_and_cleans_publications(tmp_path: Path) -> None:
    bot = _planner_lifecycle_bot()
    bot.settings.database_path = tmp_path / "bot.sqlite3"
    bot.settings.ai_orchestration_durable_enabled = True
    plugin = AIPlugin()

    await plugin.start(bot)

    runtime = bot.ai_orchestration_runtime
    assert plugin._orchestration_runtime is runtime
    assert plugin._orchestration_consumer is not None
    assert plugin._orchestration_engine is runtime.engine
    assert bot.ai_orchestration_engine is runtime.engine
    assert bot.ai_orchestration_repository is runtime.repository
    assert runtime.repository.path == tmp_path / "ai-orchestration.sqlite3"
    assert plugin._mention_listener is not None
    assert plugin._mention_listener.orchestration_engine is runtime.engine

    await plugin.stop()

    assert runtime.closed is True
    assert not hasattr(bot, "ai_orchestration_runtime")
    assert not hasattr(bot, "ai_orchestration_engine")
    assert not hasattr(bot, "ai_orchestration_repository")


def test_memory_context_recall_uses_atomic_capability_in_dm_without_guild_sentinel() -> None:
    class RecordingGuard:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def currently_allowed(self, capability_id: str, **kwargs: object) -> bool:
            self.calls.append((capability_id, kwargs))
            return True

    guard = RecordingGuard()
    bot = SimpleNamespace(
        is_closing=False,
        settings=SimpleNamespace(
            ai_dm_enabled=True,
            bot_owner_ids=frozenset(),
            moderator_role_ids=frozenset(),
            trusted_role_ids=frozenset(),
        ),
        personal_memory_service=SimpleNamespace(),
        capability_guard=guard,
    )
    subject = SimpleNamespace(
        guild=None,
        guild_id=None,
        user=SimpleNamespace(id=20, bot=False, roles=(), guild_permissions=None),
    )

    assert _memory_context_recall_allowed(bot, subject, repository=object()) is True  # type: ignore[arg-type]
    assert guard.calls[0][0] == MEMORY_CONTEXT_RECALL_CAPABILITY_ID
    assert guard.calls[0][1]["guild_id"] is None


def test_memory_context_recall_respects_legacy_guild_disable_and_plugin_stop() -> None:
    guard = Guard()
    service = SimpleNamespace(is_enabled=lambda _guild_id, _user_id: False)
    bot = SimpleNamespace(
        is_closing=False,
        settings=SimpleNamespace(
            ai_dm_enabled=True,
            bot_owner_ids=frozenset(),
            moderator_role_ids=frozenset(),
            trusted_role_ids=frozenset(),
        ),
        personal_memory_service=service,
        capability_guard=guard,
    )
    subject = SimpleNamespace(
        guild=SimpleNamespace(id=10, owner_id=1),
        guild_id=10,
        user=SimpleNamespace(id=20, bot=False, roles=(), guild_permissions=None),
    )

    assert _memory_context_recall_allowed(bot, subject, repository=object()) is False  # type: ignore[arg-type]
    delattr(bot, "personal_memory_service")
    assert _memory_context_recall_allowed(bot, subject, repository=object()) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_plugin_stop_cancels_blocked_attachment_task_after_drain_timeout() -> None:
    listeners: list[tuple[Any, str]] = []
    settings = SimpleNamespace(
        ai_conversation_ttl_seconds=7_200,
        ai_conversation_max_turns=12,
        ai_conversation_max_sessions=128,
        ai_conversation_max_total_binary_bytes=64 * 1024 * 1024,
        ai_attachment_max_file_bytes=8 * 1024 * 1024,
        ai_attachment_max_total_bytes=16 * 1024 * 1024,
        ai_attachment_max_files=4,
        ai_base_url="",
        ai_mention_enabled=True,
        ai_mention_guild_ids=frozenset({10}),
        ai_mention_allow_all_guilds=False,
        ai_reply_continuation_enabled=False,
        ai_attachments_enabled=True,
        ai_timeout_seconds=30.0,
        ai_admission_global_concurrency=4,
        ai_admission_max_waiters=32,
        ai_admission_wait_timeout_seconds=0.1,
        ai_admission_drain_timeout_seconds=0.1,
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=99),
        settings=settings,
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
        capability_guard=Guard(),
        ai_orchestration_planner_port=SimpleNamespace(generate=lambda _request: "{}"),
        add_listener=lambda listener, name: listeners.append((listener, name)),
        remove_listener=lambda listener, name: listeners.remove((listener, name)),
    )
    plugin = AIPlugin()
    await plugin.start(bot)
    listener = plugin._mention_listener
    assert listener is not None
    assert bot.ai_orchestration_planner_ready is False
    service = Service()
    listener.service = service  # type: ignore[assignment]
    listener.provider_available = True
    listener.provider_is_local = True
    listener.attachments_available = True
    attachment = BlockingAttachment()
    task = asyncio.create_task(listener.on_message(Message(attachment)))  # type: ignore[arg-type]
    await asyncio.wait_for(attachment.started.wait(), timeout=1.0)
    assert listener.admission.stats().active == 1

    await plugin.stop()
    result = (await asyncio.gather(task, return_exceptions=True))[0]

    assert isinstance(result, asyncio.CancelledError)
    assert attachment.cancelled.is_set()
    assert task.done()
    assert service.calls == 0
    assert listener.active_task_count == 0
    assert listener.admission.stats().active == 0
    assert listeners == []
    assert not hasattr(bot, "ai_orchestration_planner_ready")


@pytest.mark.asyncio
async def test_plugin_begin_close_rejects_new_mention_before_attachment_or_provider() -> None:
    listeners: list[tuple[Any, str]] = []
    settings = SimpleNamespace(
        ai_conversation_ttl_seconds=7_200,
        ai_conversation_max_turns=12,
        ai_conversation_max_sessions=128,
        ai_conversation_max_total_binary_bytes=64 * 1024 * 1024,
        ai_attachment_max_file_bytes=8 * 1024 * 1024,
        ai_attachment_max_total_bytes=16 * 1024 * 1024,
        ai_attachment_max_files=4,
        ai_base_url="",
        ai_mention_enabled=True,
        ai_mention_guild_ids=frozenset({10}),
        ai_mention_allow_all_guilds=False,
        ai_reply_continuation_enabled=False,
        ai_attachments_enabled=True,
        ai_timeout_seconds=30.0,
        ai_admission_global_concurrency=4,
        ai_admission_max_waiters=32,
        ai_admission_wait_timeout_seconds=0.1,
        ai_admission_drain_timeout_seconds=0.1,
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=99),
        settings=settings,
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
        capability_guard=Guard(),
        add_listener=lambda listener, name: listeners.append((listener, name)),
        remove_listener=lambda listener, name: listeners.remove((listener, name)),
    )
    plugin = AIPlugin()
    await plugin.start(bot)
    listener = plugin._mention_listener
    assert listener is not None
    service = Service()
    listener.service = service  # type: ignore[assignment]
    listener.provider_available = True
    attachment = BlockingAttachment()

    await plugin.begin_close()
    await listener.on_message(Message(attachment))  # type: ignore[arg-type]

    assert listener.closing
    assert listener.admission.closing
    assert not attachment.started.is_set()
    assert service.calls == 0
    await plugin.stop()


@pytest.mark.asyncio
async def test_plugin_restart_reopens_durable_consent_and_reply_context(tmp_path) -> None:
    settings = SimpleNamespace(
        database_path=tmp_path / "suite.sqlite3",
        ai_conversation_ttl_seconds=7_200,
        ai_conversation_max_turns=12,
        ai_conversation_max_sessions=128,
        ai_conversation_max_total_binary_bytes=64 * 1024 * 1024,
        ai_attachment_max_file_bytes=8 * 1024 * 1024,
        ai_attachment_max_total_bytes=16 * 1024 * 1024,
        ai_attachment_max_files=4,
        ai_base_url="",
        ai_mention_enabled=True,
        ai_mention_guild_ids=frozenset({10}),
        ai_mention_allow_all_guilds=False,
        ai_reply_continuation_enabled=True,
        ai_attachments_enabled=True,
        ai_timeout_seconds=30.0,
        ai_admission_global_concurrency=4,
        ai_admission_max_waiters=32,
        ai_admission_wait_timeout_seconds=0.1,
        ai_admission_drain_timeout_seconds=0.1,
    )

    def make_bot() -> SimpleNamespace:
        listeners: list[tuple[Any, str]] = []
        return SimpleNamespace(
            user=SimpleNamespace(id=99),
            settings=settings,
            tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
            capability_guard=Guard(),
            add_listener=lambda listener, name: listeners.append((listener, name)),
            remove_listener=lambda listener, name: listeners.remove((listener, name)),
        )

    first_bot = make_bot()
    first = AIPlugin()
    await first.start(first_bot)
    first_bot.ai_remote_consent_store.grant(guild_id=10, channel_id=20, user_id=30)
    session = await first_bot.ai_conversation_store.start(guild_id=10, channel_id=20, user_id=30)
    await first_bot.ai_conversation_store.append_exchange(
        session_id=session.session_id,
        guild_id=10,
        channel_id=20,
        user_id=30,
        user_text="before restart",
        assistant_text="durable answer",
        bot_message_id=1234,
    )
    await first.stop()

    second_bot = make_bot()
    second = AIPlugin()
    await second.start(second_bot)
    assert second_bot.ai_remote_consent_store.active(guild_id=999, channel_id=888, user_id=30) is True
    resolved = await second_bot.ai_conversation_store.resolve(
        bot_message_id=1234,
        guild_id=10,
        channel_id=20,
        user_id=30,
    )
    assert resolved is not None
    assert [turn.text for turn in resolved.history] == ["before restart", "durable answer"]
    await second.stop()


@pytest.mark.asyncio
async def test_same_plugin_instance_does_not_reuse_stale_provider_route_after_restart(tmp_path) -> None:
    settings = SimpleNamespace(
        database_path=tmp_path / "suite.sqlite3",
        ai_conversation_ttl_seconds=7_200,
        ai_conversation_max_turns=12,
        ai_conversation_max_sessions=128,
        ai_conversation_max_total_binary_bytes=64 * 1024 * 1024,
        ai_attachment_max_file_bytes=8 * 1024 * 1024,
        ai_attachment_max_total_bytes=16 * 1024 * 1024,
        ai_attachment_max_files=4,
        ai_base_url="http://127.0.0.1:65534/v1",
        openai_api_key="",
        ai_api_key="",
        ai_allow_remote=False,
        ai_allow_luna=True,
        ai_web_search_enabled=False,
        ai_attachments_enabled=True,
        ai_model_fast="gpt-5.6-luna",
        ai_model_balanced="gpt-5.6-terra",
        ai_model_quality="gpt-5.6-sol",
        ai_safety_identifier_secret="",
        ai_timeout_seconds=5.0,
        ai_max_output_tokens=512,
        ai_max_response_bytes=65_536,
        ai_mention_enabled=False,
    )
    bot = SimpleNamespace(
        settings=settings,
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
    )
    plugin = AIPlugin()

    await plugin.start(bot)
    assert plugin.service is not None and plugin.service.available is True
    assert plugin._provider_selector is not None
    assert plugin._provider_readiness is not None
    assert bot.ai_attachments_available is True
    await plugin.stop()
    assert plugin._provider_selector is None
    assert plugin._provider_readiness is None
    assert plugin._context_builder is None
    assert not hasattr(bot, "ai_attachments_available")

    settings.ai_base_url = ""
    await plugin.start(bot)
    assert plugin.service is not None and plugin.service.available is False
    assert plugin._provider_selector is None
    assert plugin._provider_readiness is None
    assert bot.ai_provider_is_local is None
    assert bot.ai_attachments_available is False
    await plugin.stop()


def _agent_audit_binding() -> tuple[AgentAuditReadBinding, AgentAuditCursor]:
    scope = AgentAuditScope(
        guild_id=10,
        actor_id=20,
        request_binding="lifecycle-request",
        session_binding="lifecycle-session",
    )

    async def fresh_authorization(_scope: AgentAuditScope) -> bool:
        return True

    return (
        AgentAuditReadBinding(
            scope=scope,
            authorization_current=lambda _scope: True,
            fresh_authorization_current=fresh_authorization,
            runtime_binding_current=lambda: True,
            request_binding_current=lambda: True,
        ),
        AgentAuditCursor.start(scope),
    )


@pytest.mark.asyncio
async def test_agent_audit_port_is_not_composed_from_closed_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "closed.sqlite3")
    bot = _planner_lifecycle_bot()
    bot.database = database
    plugin = AIPlugin()

    await plugin.start(bot)

    assert plugin._agent_audit_port is None
    assert plugin._active_agent_audit_port is None
    binding, cursor = _agent_audit_binding()
    with pytest.raises(AuditProjectionError) as error:
        await plugin.read_agent_audit_page(binding=binding, cursor=cursor)
    assert error.value.code is AuditProjectionFailureCode.AUTHORIZATION_DENIED
    await plugin.stop()


@pytest.mark.asyncio
async def test_agent_audit_port_is_internal_and_withdrawn_before_consumer_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "control.sqlite3")
    database.open()
    database.migrate()
    database.append_audit("agent.completed", actor_id=20, guild_id=10)
    bot = _planner_lifecycle_bot()
    bot.database = database
    plugin = AIPlugin()
    await plugin.start(bot)
    port = plugin._agent_audit_port
    assert port is not None
    assert plugin._active_agent_audit_port is port
    assert all(value is not port for value in vars(bot).values())
    assert not any("agent_audit" in name for name in vars(bot))
    binding, cursor = _agent_audit_binding()
    page = await plugin.read_agent_audit_page(binding=binding, cursor=cursor)
    assert [event.event for event in page.events] == ["agent.completed"]

    admission = plugin._admission
    assert admission is not None
    original_begin_close = admission.begin_close

    async def assert_withdrawn_before_drain() -> None:
        assert plugin._active_agent_audit_port is None
        await original_begin_close()

    monkeypatch.setattr(admission, "begin_close", assert_withdrawn_before_drain)
    await plugin.begin_close()
    with pytest.raises(AuditProjectionError) as stale_error:
        await port.read_page(binding=binding, cursor=cursor)
    assert stale_error.value.code is AuditProjectionFailureCode.SOURCE_REPLACED
    with pytest.raises(AuditProjectionError) as plugin_error:
        await plugin.read_agent_audit_page(binding=binding, cursor=cursor)
    assert plugin_error.value.code is AuditProjectionFailureCode.AUTHORIZATION_DENIED

    await plugin.stop()
    await plugin.stop()
    assert plugin._agent_audit_port is None
    assert plugin._active_agent_audit_port is None
    database.close()


@pytest.mark.asyncio
async def test_agent_audit_port_restart_never_revives_old_identity(tmp_path: Path) -> None:
    database = Database(tmp_path / "control.sqlite3")
    database.open()
    database.migrate()
    database.append_audit("agent.completed", actor_id=20, guild_id=10)
    bot = _planner_lifecycle_bot()
    bot.database = database
    plugin = AIPlugin()
    binding, cursor = _agent_audit_binding()

    await plugin.start(bot)
    old_port = plugin._agent_audit_port
    assert old_port is not None
    first_page = await plugin.read_agent_audit_page(binding=binding, cursor=cursor)
    assert [event.event for event in first_page.events] == ["agent.completed"]
    await plugin.stop()
    database.append_audit("agent.resumed", actor_id=20, guild_id=10)
    await plugin.start(bot)
    new_port = plugin._agent_audit_port
    assert new_port is not None and new_port is not old_port
    with pytest.raises(AuditProjectionError) as stale_error:
        await old_port.read_page(binding=binding, cursor=cursor)
    assert stale_error.value.code is AuditProjectionFailureCode.SOURCE_REPLACED
    page = await plugin.read_agent_audit_page(binding=binding, cursor=first_page.next_cursor)
    assert [event.event for event in page.events] == ["agent.resumed"]

    await plugin.stop()
    database.close()


@pytest.mark.asyncio
async def test_agent_audit_port_partial_start_is_withdrawn_and_cleared(tmp_path: Path) -> None:
    database = Database(tmp_path / "control.sqlite3")
    database.open()
    database.migrate()
    bot = _planner_lifecycle_bot()
    bot.database = database
    instances: list[AIPlugin] = []
    stale_ports: list[object] = []

    def failing_gateway(_service: AIService) -> RecordingGateway:
        stale_ports.append(instances[0]._agent_audit_port)
        raise RuntimeError("private partial-start failure")

    def factory() -> AIPlugin:
        plugin = AIPlugin(
            execution_gateway_factory=failing_gateway,
            execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
        )
        instances.append(plugin)
        return plugin

    manager = PluginManager()
    manager.register("ai", factory)

    assert await manager.enable("ai", bot) is False
    plugin = instances[0]
    assert plugin._agent_audit_port is None
    assert plugin._active_agent_audit_port is None
    assert stale_ports and stale_ports[0] is not None
    binding, cursor = _agent_audit_binding()
    with pytest.raises(AuditProjectionError) as stale_error:
        await stale_ports[0].read_page(binding=binding, cursor=cursor)  # type: ignore[union-attr]
    assert stale_error.value.code is AuditProjectionFailureCode.SOURCE_REPLACED
    assert not any("agent_audit" in name for name in vars(bot))
    database.close()
