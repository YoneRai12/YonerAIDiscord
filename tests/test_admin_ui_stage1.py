from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web

import yonerai_discord.modules.admin_ui.web_adapter as admin_ui_web_adapter
from yonerai_discord.capabilities import (
    COMMAND_CAPABILITIES,
    EVENT_CAPABILITIES,
    PLUGIN_MODULES,
)
from yonerai_discord.config import SAFE_DEFAULT_PLUGINS
from yonerai_discord.control_plane import (
    CapabilitySpec,
    ModuleSpec,
    RbacLevel,
    Registry,
    RiskLevel,
)
from yonerai_discord.db import GuildAuditSummaryRecord
from yonerai_discord.deployment_current_truth import M10SourceTruthV1, build_m10_current_truth
from yonerai_discord.modules.ai.execution_profiles import (
    ExecutionTopology,
    HostingProfile,
    PackagingCandidate,
)
from yonerai_discord.modules.admin_ui import (
    ADMIN_UI_CAPABILITY_ID,
    AdminUiConfig,
    AdminUiConfigurationError,
    AdminUiPlugin,
    AdminUiProjectionError,
    AdminUiRequestHandler,
    AdminUiWebServer,
    build_admin_ui_projection,
    render_admin_ui,
    setup,
)
from yonerai_discord.plugin import PluginManager
from yonerai_discord.runtime_manifests.admin_ui import CAPABILITIES as ADMIN_UI_CAPABILITIES
from yonerai_discord.runtime_manifests.modules import MODULES


class _AuditSource:
    def __init__(self, rows: tuple[GuildAuditSummaryRecord, ...] = ()) -> None:
        self.rows = rows
        self.calls: list[tuple[int, int]] = []

    def list_guild_audit_summary(
        self,
        guild_id: int,
        *,
        limit: int = 50,
    ) -> tuple[GuildAuditSummaryRecord, ...]:
        self.calls.append((guild_id, limit))
        return self.rows


class _ReadinessRegistry:
    def __init__(self) -> None:
        self.values: dict[str, bool] = {}

    def set_runtime_availability(self, capability_id: str, ready: bool) -> None:
        self.values[capability_id] = ready


class _Authenticator:
    def __init__(self, values: list[int | None]) -> None:
        self.values = values
        self.calls = 0

    async def current_user_id(self, _request: object) -> int | None:
        self.calls += 1
        if not self.values:
            return None
        return self.values.pop(0)


class _Guild:
    def __init__(self, guild_id: int, user_id: int) -> None:
        self.id = guild_id
        self.user_id = user_id
        self.fetch_calls = 0
        self.error: Exception | None = None

    async def fetch_member(self, user_id: int) -> Any:
        self.fetch_calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=self.user_id if user_id == self.user_id else -1)


class _Guard:
    def __init__(
        self,
        allowed: list[object],
        *,
        decision_allowed: list[object] | None = None,
    ) -> None:
        self.allowed = allowed
        self.decision_allowed = decision_allowed or []
        self.evaluate_calls = 0
        self.current_calls = 0

    async def evaluate_fresh_member(self, capability_id: str, *, guild: Any, member: Any) -> Any:
        assert capability_id == ADMIN_UI_CAPABILITY_ID
        assert guild.id > 0 and member.id > 0
        self.evaluate_calls += 1
        allowed = self.decision_allowed.pop(0) if self.decision_allowed else True
        return SimpleNamespace(allowed=allowed, actor_level=RbacLevel.GUILD_ADMIN)

    def currently_allowed(self, capability_id: str, **values: Any) -> object:
        assert capability_id == ADMIN_UI_CAPABILITY_ID
        assert values["floor"] is RbacLevel.GUILD_ADMIN
        assert values["actor_level"] is RbacLevel.GUILD_ADMIN
        self.current_calls += 1
        return self.allowed.pop(0)


def _registry(*, unsafe_name: bool = False) -> Registry:
    registry = Registry()
    registry.register_module(ModuleSpec("security.admin-ui", default_enabled=True))
    registry.register_capability(
        CapabilitySpec(
            ADMIN_UI_CAPABILITY_ID,
            "security.admin-ui",
            name="<script>alert(1)</script>" if unsafe_name else "Admin UI read",
            default_enabled=True,
            required_level=RbacLevel.GUILD_ADMIN,
            minimum_level=RbacLevel.GUILD_ADMIN,
            risk=RiskLevel.HIGH,
        )
    )
    registry.set_runtime_availability(ADMIN_UI_CAPABILITY_ID, True)
    return registry


def test_manifest_is_default_off_high_admin_and_has_no_discord_surface() -> None:
    definition = ADMIN_UI_CAPABILITIES[0]
    assert definition.capability_id == ADMIN_UI_CAPABILITY_ID
    assert definition.module_id == "security.admin-ui"
    assert definition.plugin == "admin_ui"
    assert definition.level is RbacLevel.GUILD_ADMIN
    assert definition.risk is RiskLevel.HIGH
    assert definition.default_enabled is False
    assert definition.command_paths == ()
    assert definition.event_names == ()
    assert ADMIN_UI_CAPABILITY_ID not in COMMAND_CAPABILITIES.values()
    assert ADMIN_UI_CAPABILITY_ID not in EVENT_CAPABILITIES.values()
    assert PLUGIN_MODULES["admin_ui"] == ("security.admin-ui",)
    assert next(item for item in MODULES if item.module_id == "security.admin-ui").default_enabled is False
    assert "admin_ui" not in SAFE_DEFAULT_PLUGINS
    with pytest.raises(AdminUiConfigurationError, match="loopback"):
        AdminUiConfig(enabled=True, bind_host="0.0.0.0")

    manager = PluginManager()
    setup(manager)
    assert manager.snapshots()[0].name == "admin_ui"


@dataclass
class _FakeServer:
    start_error: BaseException | None = None
    stop_error: BaseException | None = None
    starts: int = 0
    stops: int = 0

    async def start(self) -> None:
        self.starts += 1
        if self.start_error is not None:
            raise self.start_error

    async def stop(self) -> None:
        self.stops += 1
        if self.stop_error is not None:
            raise self.stop_error


async def test_plugin_requires_injected_config_auth_and_cleans_up_failures() -> None:
    readiness = _ReadinessRegistry()
    factory_calls = 0

    def unused_factory(_handler: Any, _config: AdminUiConfig) -> _FakeServer:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeServer()

    missing_bots = (
        SimpleNamespace(
            admin_ui_config=AdminUiConfig(enabled=True),
            capability_registry=readiness,
            database=_AuditSource(),
        ),
        SimpleNamespace(
            admin_ui_authenticator=_Authenticator([1]),
            capability_registry=_ReadinessRegistry(),
            database=_AuditSource(),
        ),
    )
    for missing_bot in missing_bots:
        missing = AdminUiPlugin(server_factory=unused_factory)
        await missing.start(missing_bot)
        assert missing_bot.runtime_capability_readiness == {ADMIN_UI_CAPABILITY_ID: False}
        await missing.stop()
        assert missing_bot.runtime_capability_readiness == {}
    assert factory_calls == 0

    configured_server = _FakeServer(stop_error=RuntimeError("fixed stop failure"))
    configured_bot = SimpleNamespace(
        admin_ui_config=AdminUiConfig(enabled=True),
        admin_ui_authenticator=_Authenticator([1]),
        database=_AuditSource(),
        capability_registry=_ReadinessRegistry(),
    )
    configured = AdminUiPlugin(server_factory=lambda _handler, _config: configured_server)
    await configured.start(configured_bot)
    assert configured_server.starts == 1
    assert configured_bot.runtime_capability_readiness == {ADMIN_UI_CAPABILITY_ID: True}
    with pytest.raises(RuntimeError, match="fixed stop failure"):
        await configured.stop()
    assert configured_server.stops == 1
    assert configured_bot.runtime_capability_readiness == {}
    assert not hasattr(configured_bot, "admin_ui_server")
    assert configured.server is configured_server
    configured_server.stop_error = None
    await configured.stop()
    assert configured_server.stops == 2
    assert configured.server is None

    factory_bot = SimpleNamespace(
        admin_ui_config=AdminUiConfig(enabled=True),
        admin_ui_authenticator=_Authenticator([1]),
        database=_AuditSource(),
        capability_registry=_ReadinessRegistry(),
    )

    def broken_factory(_handler: Any, _config: AdminUiConfig) -> _FakeServer:
        raise RuntimeError("fixed factory failure")

    broken = AdminUiPlugin(server_factory=broken_factory)
    with pytest.raises(RuntimeError, match="fixed factory failure"):
        await broken.start(factory_bot)
    assert factory_bot.runtime_capability_readiness == {ADMIN_UI_CAPABILITY_ID: False}
    assert not hasattr(factory_bot, "admin_ui_server")
    await broken.stop()

    cancelled_server = _FakeServer(
        start_error=asyncio.CancelledError(),
        stop_error=asyncio.CancelledError(),
    )
    cancelled_bot = SimpleNamespace(
        admin_ui_config=AdminUiConfig(enabled=True),
        admin_ui_authenticator=_Authenticator([1]),
        database=_AuditSource(),
        capability_registry=_ReadinessRegistry(),
    )
    cancelled = AdminUiPlugin(server_factory=lambda _handler, _config: cancelled_server)
    with pytest.raises(asyncio.CancelledError):
        await cancelled.start(cancelled_bot)
    assert cancelled_server.starts == cancelled_server.stops == 1
    assert cancelled_bot.runtime_capability_readiness == {ADMIN_UI_CAPABILITY_ID: False}
    assert cancelled.server is cancelled_server
    cancelled_server.stop_error = None
    await cancelled.stop()
    assert cancelled_server.stops == 2
    assert cancelled.server is None


async def test_request_rechecks_session_member_and_capability_before_render() -> None:
    guild = _Guild(42, 7)
    guard = _Guard([True, True])
    authenticator = _Authenticator([7, 7])
    audit = _AuditSource((GuildAuditSummaryRecord(3, "policy.checked", "admin_ui", 7, "2026-07-24T00:00:00Z"),))
    bot = SimpleNamespace(
        capability_registry=_registry(),
        capability_guard=guard,
        get_guild=lambda guild_id: guild if guild_id == 42 else None,
    )
    response = await AdminUiRequestHandler(
        bot=bot,
        authenticator=authenticator,
        audit_source=audit,
    ).handle(object(), method="GET", guild_id_text="42")

    assert response.status == 200
    assert "policy.checked" in response.body
    assert authenticator.calls == guild.fetch_calls == guard.evaluate_calls == guard.current_calls == 2
    assert audit.calls == [(42, 50)]

    revoked_guard = _Guard([True, False])
    revoked_audit = _AuditSource((GuildAuditSummaryRecord(4, "must.not.render", None, None, "2026-07-24T00:00:00Z"),))
    revoked_bot = SimpleNamespace(
        capability_registry=_registry(),
        capability_guard=revoked_guard,
        get_guild=lambda _guild_id: guild,
    )
    denied = await AdminUiRequestHandler(
        bot=revoked_bot,
        authenticator=_Authenticator([7, 7]),
        audit_source=revoked_audit,
    ).handle(object(), method="GET", guild_id_text="42")
    assert denied.status == 403
    assert "must.not.render" not in denied.body
    assert revoked_guard.current_calls == 2


@pytest.mark.parametrize(
    ("guard", "expected_audit_calls"),
    [
        (_Guard([True], decision_allowed=[1]), []),
        (_Guard([1]), []),
        (_Guard([True, "allowed"]), [(42, 50)]),
    ],
)
async def test_authorization_requires_exact_boolean_true_at_entry_and_late_recheck(
    guard: _Guard,
    expected_audit_calls: list[tuple[int, int]],
) -> None:
    guild = _Guild(42, 7)
    audit = _AuditSource((GuildAuditSummaryRecord(5, "must.not.render", None, None, "2026-07-24T00:00:00Z"),))
    bot = SimpleNamespace(
        capability_registry=_registry(),
        capability_guard=guard,
        get_guild=lambda _guild_id: guild,
    )
    response = await AdminUiRequestHandler(
        bot=bot,
        authenticator=_Authenticator([7, 7]),
        audit_source=audit,
    ).handle(object(), method="GET", guild_id_text="42")

    assert response.status == 403
    assert "must.not.render" not in response.body
    assert audit.calls == expected_audit_calls


@pytest.mark.parametrize(
    ("guild_id_text", "auth_values", "expected"),
    [
        ("01", [7], 404),
        ("42", [None], 401),
        ("42", [7, None], 403),
    ],
)
async def test_invalid_path_or_session_fails_with_fixed_body(
    guild_id_text: str,
    auth_values: list[int | None],
    expected: int,
) -> None:
    guild = _Guild(42, 7)
    bot = SimpleNamespace(
        capability_registry=_registry(),
        capability_guard=_Guard([True, True]),
        get_guild=lambda _guild_id: guild,
    )
    response = await AdminUiRequestHandler(
        bot=bot,
        authenticator=_Authenticator(auth_values),
        audit_source=_AuditSource(),
    ).handle(object(), method="GET", guild_id_text=guild_id_text)
    assert response.status == expected
    assert "token" not in response.body.lower()


def test_projection_is_sorted_bounded_redacted_and_html_escaped() -> None:
    registry = _registry(unsafe_name=True)
    audit = _AuditSource((GuildAuditSummaryRecord(2, "<event>", "<plugin>", 9, "<created>"),))
    projection = build_admin_ui_projection(
        guild_id=42,
        registry=registry,
        audit_source=audit,
    )
    assert tuple(row.module_id for row in projection.modules) == ("security.admin-ui",)
    assert tuple(row.capability_id for row in projection.capabilities) == (ADMIN_UI_CAPABILITY_ID,)
    assert not hasattr(projection.audit[0], "details")
    html = render_admin_ui(projection)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;event&gt;" in html
    assert "<form" not in html and "<script" not in html

    too_many = _AuditSource(tuple(GuildAuditSummaryRecord(index, "event", None, None, "date") for index in range(51)))
    with pytest.raises(AdminUiProjectionError, match="fixed bound"):
        build_admin_ui_projection(guild_id=42, registry=registry, audit_source=too_many)


def test_projection_renders_typed_m10_current_truth_without_claiming_live_success() -> None:
    truth = build_m10_current_truth(
        selected_topology=ExecutionTopology.DIRECT_CORE,
        selected_hosting_profile=HostingProfile.FULL_PRIVATE_SELF_HOST,
        selected_packaging=PackagingCandidate.LOCAL_ONLY,
        available_ports=("direct_core",),
        provider_source=M10SourceTruthV1(configured=True, ready=True, live_success=None, blocker=None),
    )
    projection = build_admin_ui_projection(
        guild_id=42,
        registry=_registry(),
        audit_source=_AuditSource(),
        deployment_truth=truth,
    )

    assert projection.deployment.effective_topology == "direct_core"
    assert projection.deployment.missing_ports == ()
    assert projection.deployment.sources[0].name == "provider"
    assert projection.deployment.sources[0].live_success == "unknown"
    html = render_admin_ui(projection)
    assert "Deployment current truth" in html
    assert "direct_core" in html
    assert "live success" in html
    assert "推測しません" in html


async def test_missing_or_untyped_deployment_truth_uses_safe_unconfigured_fallback() -> None:
    guild = _Guild(42, 7)
    secret_like_value = "not-a-token-value"
    bot = SimpleNamespace(
        capability_registry=_registry(),
        capability_guard=_Guard([True, True]),
        deployment_current_truth=SimpleNamespace(blocker=secret_like_value),
        get_guild=lambda _guild_id: guild,
    )
    response = await AdminUiRequestHandler(
        bot=bot,
        authenticator=_Authenticator([7, 7]),
        audit_source=_AuditSource(),
    ).handle(object(), method="GET", guild_id_text="42")

    assert response.status == 200
    assert "deployment_selection_not_configured" in response.body
    assert secret_like_value not in response.body


async def test_deployment_truth_swap_after_projection_fails_closed() -> None:
    guild = _Guild(42, 7)
    first_truth = build_m10_current_truth()
    second_truth = build_m10_current_truth(available_ports=("local",))

    class _SwappingAudit(_AuditSource):
        def list_guild_audit_summary(self, guild_id: int, *, limit: int = 50) -> tuple[GuildAuditSummaryRecord, ...]:
            bot.deployment_current_truth = second_truth
            return super().list_guild_audit_summary(guild_id, limit=limit)

    bot = SimpleNamespace(
        capability_registry=_registry(),
        capability_guard=_Guard([True, True]),
        deployment_current_truth=first_truth,
        get_guild=lambda _guild_id: guild,
    )
    response = await AdminUiRequestHandler(
        bot=bot,
        authenticator=_Authenticator([7, 7]),
        audit_source=_SwappingAudit(),
    ).handle(object(), method="GET", guild_id_text="42")

    assert response.status == 503
    assert "deployment_selection_not_configured" not in response.body


async def test_get_only_middleware_adds_security_headers_to_all_responses() -> None:
    server = AdminUiWebServer(
        AdminUiRequestHandler(
            bot=SimpleNamespace(),
            authenticator=None,
            audit_source=_AuditSource(),
        ),
        AdminUiConfig(enabled=True),
    )
    application = server.create_application()
    assert [(route.method, route.resource.canonical) for route in application.router.routes()] == [
        ("GET", "/admin/guild/{guild_id}")
    ]

    async def missing(_request: object) -> web.Response:
        raise web.HTTPNotFound()

    async def broken(_request: object) -> web.Response:
        raise RuntimeError("sensitive parser value")

    not_found = await server._security_middleware(SimpleNamespace(method="GET"), missing)
    method_denied = await server._security_middleware(SimpleNamespace(method="POST"), missing)
    unavailable_response = await server._security_middleware(SimpleNamespace(method="GET"), broken)
    for response in (not_found, method_denied, unavailable_response):
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "Access-Control-Allow-Origin" not in response.headers
    assert not_found.status == 404
    assert method_denied.status == 405
    assert unavailable_response.status == 503
    unavailable = await server._handler.handle(
        object(),
        method="GET",
        guild_id_text="42",
    )
    assert unavailable.status == 503


async def test_web_server_keeps_failed_cleanup_handle_for_retry_and_uses_private_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runner:
        def __init__(self) -> None:
            self.cleanup_calls = 0
            self.cleanup_error: BaseException | None = asyncio.CancelledError()

        async def setup(self) -> None:
            raise RuntimeError("fixed setup failure")

        async def cleanup(self) -> None:
            self.cleanup_calls += 1
            if self.cleanup_error is not None:
                raise self.cleanup_error

    runner = _Runner()
    runner_kwargs: dict[str, object] = {}

    def app_runner(_application: object, **kwargs: object) -> _Runner:
        runner_kwargs.update(kwargs)
        return runner

    monkeypatch.setattr(web, "AppRunner", app_runner)
    server = AdminUiWebServer(
        AdminUiRequestHandler(
            bot=SimpleNamespace(),
            authenticator=None,
            audit_source=_AuditSource(),
        ),
        AdminUiConfig(enabled=True),
    )

    with pytest.raises(RuntimeError, match="fixed setup failure"):
        await server.start()
    assert runner.cleanup_calls == 1
    assert server._runner is runner
    assert runner_kwargs["access_log"] is None
    assert runner_kwargs["logger"] is admin_ui_web_adapter._PROTOCOL_LOGGER
    assert admin_ui_web_adapter._PROTOCOL_LOGGER.propagate is False
    assert any(isinstance(handler, logging.NullHandler) for handler in admin_ui_web_adapter._PROTOCOL_LOGGER.handlers)

    runner.cleanup_error = None
    await server.stop()
    assert runner.cleanup_calls == 2
    assert server._runner is None
