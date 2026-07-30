from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.config import ConfigurationError, Settings
from yonerai_discord.modules.ai import (
    AIPlugin,
    AIService,
    ExecutionProfileError,
    ExecutionTopology,
    HostingProfile,
    HybridExecutionGateway,
    PackagingDependencyClass,
    PackagingCandidate,
    resolve_runtime_execution_profile,
)
from yonerai_discord.modules.ai.core_surface import DiscordCoreSurfaceGateway


class _Guard:
    def event_allowed(self, _capability_id: str, **_kwargs: object) -> bool:
        return True

    def currently_allowed(self, _capability_id: str, **_kwargs: object) -> bool:
        return True


class _Gateway:
    async def start(self, _request: object) -> object:
        raise AssertionError("gateway execution is outside this composition test")

    async def events(self, _run_id: str) -> Any:
        if False:
            yield None

    async def submit_result(self, _run_id: str, _result: object) -> None:
        raise AssertionError("gateway execution is outside this composition test")

    async def cancel(self, _run_id: str) -> None:
        raise AssertionError("gateway execution is outside this composition test")


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "ai_execution_topology": None,
        "ai_hosting_profile": None,
        "ai_packaging_candidate": None,
        "ai_conversation_ttl_seconds": 7_200,
        "ai_conversation_max_turns": 12,
        "ai_conversation_max_sessions": 128,
        "ai_conversation_max_total_binary_bytes": 64 * 1024 * 1024,
        "ai_attachment_max_file_bytes": 8 * 1024 * 1024,
        "ai_attachment_max_total_bytes": 16 * 1024 * 1024,
        "ai_attachment_max_files": 4,
        "ai_base_url": "",
        "openai_api_key": "",
        "ai_api_key": "",
        "ai_allow_remote": False,
        "ai_allow_luna": True,
        "ai_web_search_enabled": False,
        "ai_model_fast": "gpt-5.6-luna",
        "ai_model_balanced": "gpt-5.6-terra",
        "ai_model_quality": "gpt-5.6-sol",
        "ai_safety_identifier_secret": "",
        "ai_max_output_tokens": 2_048,
        "ai_max_response_bytes": 2 * 1024 * 1024,
        "ai_mention_enabled": True,
        "ai_mention_guild_ids": frozenset({10}),
        "ai_mention_allow_all_guilds": False,
        "ai_reply_continuation_enabled": False,
        "ai_attachments_enabled": False,
        "ai_timeout_seconds": 30.0,
        "ai_admission_global_concurrency": 4,
        "ai_admission_max_waiters": 32,
        "ai_admission_wait_timeout_seconds": 0.1,
        "ai_admission_drain_timeout_seconds": 0.1,
        "yonerai_enabled": False,
        "yonerai_allow_remote": False,
        "yonerai_remote_status_opt_in": False,
        "yonerai_auth_token": "",
        "yonerai_core_origin": "",
        "yonerai_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _bot(settings: object) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=99),
        settings=settings,
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
        capability_guard=_Guard(),
        add_listener=lambda _listener, _name: None,
        remove_listener=lambda _listener, _name: None,
    )


def test_settings_default_to_unselected_local_profile_and_require_atomic_override() -> None:
    settings = Settings.from_env({"DISCORD_TOKEN": "offline-test-token"})

    assert settings.ai_execution_topology is None
    assert settings.ai_hosting_profile is None
    assert settings.ai_packaging_candidate is None

    with pytest.raises(ConfigurationError, match="3項目すべて"):
        Settings.from_env(
            {
                "DISCORD_TOKEN": "offline-test-token",
                "AI_EXECUTION_TOPOLOGY": "direct_core",
            }
        )
    with pytest.raises(ConfigurationError, match="定義済み"):
        Settings.from_env(
            {
                "DISCORD_TOKEN": "offline-test-token",
                "AI_EXECUTION_TOPOLOGY": "secret-like-unknown-value",
                "AI_HOSTING_PROFILE": "official_managed",
                "AI_PACKAGING_CANDIDATE": "official_private",
            }
        )


def test_runtime_selection_uses_code_owned_local_default_and_exact_enums() -> None:
    default = resolve_runtime_execution_profile(_settings())
    explicit = resolve_runtime_execution_profile(
        _settings(
            ai_execution_topology="direct_core",
            ai_hosting_profile="official_managed",
            ai_packaging_candidate="official_private",
        )
    )

    assert default.explicit is False
    assert default.topology is ExecutionTopology.LOCAL_STANDALONE
    assert default.hosting_profile is HostingProfile.FULL_PRIVATE_SELF_HOST
    assert default.packaging is PackagingCandidate.LOCAL_ONLY
    assert default.to_mapping()["live_readiness_claim"] is False
    assert explicit.explicit is True
    assert explicit.topology is ExecutionTopology.DIRECT_CORE

    with pytest.raises(ExecutionProfileError, match="invalid"):
        resolve_runtime_execution_profile(
            _settings(
                ai_execution_topology="unknown",
                ai_hosting_profile="official_managed",
                ai_packaging_candidate="official_private",
            )
        )


@pytest.mark.asyncio
async def test_plugin_default_remains_local_and_legacy_factory_is_preserved() -> None:
    gateway = _Gateway()
    services: list[AIService] = []

    def factory(service: AIService) -> _Gateway:
        services.append(service)
        return gateway

    bot = _bot(_settings())
    plugin = AIPlugin(
        execution_gateway_factory=factory,
        execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
    )
    await plugin.start(bot)

    assert services == [plugin.service]
    assert bot.ai_execution_gateway is gateway
    selection = bot.ai_execution_profile_selection
    assert selection.explicit is False
    assert selection.topology is ExecutionTopology.LOCAL_STANDALONE
    assert selection.to_mapping()["live_readiness_claim"] is False
    truth = bot.deployment_current_truth
    assert truth.selected_topology is None
    assert truth.selected_hosting_profile is None
    assert truth.selected_packaging is None
    assert truth.effective_topology is ExecutionTopology.LOCAL_STANDALONE
    assert truth.available_ports == ("local",)
    assert truth.provider_source.ready is False
    assert truth.provider_source.live_success is None

    await plugin.stop()
    assert not hasattr(bot, "ai_execution_gateway")
    assert not hasattr(bot, "ai_execution_profile_selection")
    assert not hasattr(bot, "deployment_current_truth")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "topology",
    (
        ExecutionTopology.LOCAL_STANDALONE,
        ExecutionTopology.DIRECT_CORE,
        ExecutionTopology.DISCORD_PROCESSING,
        ExecutionTopology.HYBRID,
    ),
)
async def test_injected_gateway_factories_require_dependency_evidence_before_invocation(
    topology: ExecutionTopology,
) -> None:
    calls = 0

    def factory(_service: AIService) -> _Gateway:
        nonlocal calls
        calls += 1
        return _Gateway()

    kwargs: dict[str, object]
    if topology is ExecutionTopology.LOCAL_STANDALONE:
        kwargs = {"execution_gateway_factory": factory}
    elif topology is ExecutionTopology.DIRECT_CORE:
        kwargs = {"direct_core_gateway_factory": factory}
    elif topology is ExecutionTopology.DISCORD_PROCESSING:
        kwargs = {"discord_processing_gateway_factory": factory}
    else:
        kwargs = {
            "hybrid_core_gateway_factory": factory,
            "hybrid_selector": lambda _request: False,
        }
    bot = _bot(
        _settings(
            ai_execution_topology=topology.value,
            ai_hosting_profile="official_managed",
            ai_packaging_candidate="official_private",
        )
    )

    with pytest.raises(ExecutionProfileError, match="dependency evidence"):
        await AIPlugin(**kwargs).start(bot)  # type: ignore[arg-type]

    assert calls == 0
    assert not hasattr(bot, "ai_execution_gateway")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "packaging",
    (
        PackagingCandidate.PUBLIC_SAFE_SHARED,
        PackagingCandidate.UNDECIDED,
    ),
)
async def test_injected_factory_private_dependency_is_rejected_before_invocation(
    packaging: PackagingCandidate,
) -> None:
    calls = 0

    def factory(_service: AIService) -> _Gateway:
        nonlocal calls
        calls += 1
        return _Gateway()

    bot = _bot(
        _settings(
            ai_execution_topology="local_standalone",
            ai_hosting_profile="official_managed",
            ai_packaging_candidate=packaging.value,
        )
    )
    plugin = AIPlugin(
        execution_gateway_factory=factory,
        execution_gateway_dependency_classes=(PackagingDependencyClass.OFFICIAL_SECRET,),
    )

    with pytest.raises(ExecutionProfileError, match="forbids"):
        await plugin.start(bot)

    assert calls == 0
    assert not hasattr(bot, "ai_execution_gateway")


@pytest.mark.asyncio
async def test_remote_provider_dependencies_are_rejected_before_provider_or_factory_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0
    factory_calls = 0

    def provider_factory(**_kwargs: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        return object()

    def gateway_factory(_service: AIService) -> _Gateway:
        nonlocal factory_calls
        factory_calls += 1
        return _Gateway()

    monkeypatch.setattr("yonerai_discord.modules.ai.OpenAICompatibleProvider", provider_factory)
    bot = _bot(
        _settings(
            ai_execution_topology="local_standalone",
            ai_hosting_profile="official_managed",
            ai_packaging_candidate="public_safe_shared",
            ai_base_url="https://api.openai.com/v1",
            ai_allow_remote=True,
            openai_api_key="offline-only-provider-key",
        )
    )
    plugin = AIPlugin(
        execution_gateway_factory=gateway_factory,
        execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
    )

    with pytest.raises(ExecutionProfileError, match="forbids"):
        await plugin.start(bot)

    assert provider_calls == 0
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_official_private_accepts_declared_remote_provider_dependencies() -> None:
    factory_calls = 0

    def gateway_factory(_service: AIService) -> _Gateway:
        nonlocal factory_calls
        factory_calls += 1
        return _Gateway()

    bot = _bot(
        _settings(
            ai_execution_topology="local_standalone",
            ai_hosting_profile="official_managed",
            ai_packaging_candidate="official_private",
            ai_base_url="https://api.openai.com/v1",
            ai_allow_remote=True,
            openai_api_key="offline-only-provider-key",
        )
    )
    plugin = AIPlugin(
        execution_gateway_factory=gateway_factory,
        execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
    )

    await plugin.start(bot)
    try:
        assert factory_calls == 1
        assert bot.ai_provider_is_local is False
    finally:
        await plugin.stop()


@pytest.mark.asyncio
async def test_disabled_remote_provider_does_not_block_public_safe_local_gateway() -> None:
    factory_calls = 0

    def gateway_factory(_service: AIService) -> _Gateway:
        nonlocal factory_calls
        factory_calls += 1
        return _Gateway()

    bot = _bot(
        _settings(
            ai_execution_topology="local_standalone",
            ai_hosting_profile="official_managed",
            ai_packaging_candidate="public_safe_shared",
            ai_base_url="https://api.openai.com/v1",
            ai_allow_remote=False,
            openai_api_key="offline-only-provider-key",
        )
    )
    plugin = AIPlugin(
        execution_gateway_factory=gateway_factory,
        execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
    )

    await plugin.start(bot)
    try:
        assert factory_calls == 1
        assert plugin.service is not None
        assert plugin.service.available is False
    finally:
        await plugin.stop()


@pytest.mark.asyncio
async def test_loopback_provider_is_classified_as_host_local_resource_before_factory() -> None:
    factory_calls = 0

    def gateway_factory(_service: AIService) -> _Gateway:
        nonlocal factory_calls
        factory_calls += 1
        return _Gateway()

    bot = _bot(
        _settings(
            ai_execution_topology="local_standalone",
            ai_hosting_profile="official_managed",
            ai_packaging_candidate="public_safe_shared",
            ai_base_url="http://127.0.0.1:1234/v1",
        )
    )
    plugin = AIPlugin(
        execution_gateway_factory=gateway_factory,
        execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
    )

    with pytest.raises(ExecutionProfileError, match="forbids"):
        await plugin.start(bot)

    assert factory_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("topology", "expected"),
    [
        ("direct_core", "runtime configuration is unavailable"),
        ("discord_processing", "requires an injected"),
        ("hybrid", "requires an injected"),
    ],
)
async def test_non_local_selection_without_exact_injected_port_fails_before_publication(
    topology: str,
    expected: str,
) -> None:
    bot = _bot(
        _settings(
            ai_execution_topology=topology,
            ai_hosting_profile="official_managed",
            ai_packaging_candidate="official_private",
        )
    )
    plugin = AIPlugin()

    with pytest.raises(ExecutionProfileError, match=expected):
        await plugin.start(bot)

    assert plugin.service is None
    assert not hasattr(bot, "ai_service")
    assert not hasattr(bot, "ai_execution_gateway")
    assert not hasattr(bot, "ai_execution_profile_selection")
    assert not hasattr(bot, "deployment_current_truth")


@pytest.mark.asyncio
async def test_direct_core_production_config_composes_without_local_provider_or_consent_bypass() -> None:
    bot = _bot(
        _settings(
            ai_execution_topology="direct_core",
            ai_hosting_profile="official_managed",
            ai_packaging_candidate="official_private",
            ai_base_url="http://127.0.0.1:1234/v1",
            yonerai_enabled=True,
            yonerai_allow_remote=True,
            yonerai_remote_status_opt_in=True,
            yonerai_auth_token="offline-direct-core-token",
            yonerai_core_origin="https://core.example.test",
            yonerai_timeout_seconds=7.0,
        )
    )
    plugin = AIPlugin()

    await plugin.start(bot)

    assert isinstance(bot.ai_execution_gateway, DiscordCoreSurfaceGateway)
    assert plugin.service is not None
    assert plugin.service.available is False
    assert bot.ai_provider_is_local is False
    assert bot.ai_web_search_available is False
    assert bot.ai_attachments_available is False
    assert plugin._ai_group is not None
    assert plugin._ai_group._provider_available is True
    assert plugin._ai_group._provider_is_local is False
    assert plugin._mention_listener is not None
    assert plugin._mention_listener.provider_available is True
    assert plugin._mention_listener.provider_is_local is False
    assert plugin._orchestration_planner_port is None

    await plugin.stop()


@pytest.mark.asyncio
async def test_direct_core_uses_only_its_explicit_injection_without_local_fallback() -> None:
    remote = _Gateway()
    local_calls = 0
    remote_calls = 0

    def local_factory(_service: AIService) -> _Gateway:
        nonlocal local_calls
        local_calls += 1
        return _Gateway()

    def remote_factory(_service: AIService) -> _Gateway:
        nonlocal remote_calls
        remote_calls += 1
        return remote

    bot = _bot(
        _settings(
            ai_execution_topology="direct_core",
            ai_hosting_profile="official_managed",
            ai_packaging_candidate="official_private",
        )
    )
    plugin = AIPlugin(
        execution_gateway_factory=local_factory,
        execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
        direct_core_gateway_factory=remote_factory,
        direct_core_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
    )
    await plugin.start(bot)

    assert bot.ai_execution_gateway is remote
    assert bot.ai_execution_profile_selection.topology is ExecutionTopology.DIRECT_CORE
    assert local_calls == 0
    assert remote_calls == 1
    truth = bot.deployment_current_truth
    assert truth.selected_topology is ExecutionTopology.DIRECT_CORE
    assert truth.selected_hosting_profile is HostingProfile.OFFICIAL_MANAGED
    assert truth.selected_packaging is PackagingCandidate.OFFICIAL_PRIVATE
    assert truth.available_ports == ("direct_core",)
    assert truth.provider_source.configured is True
    assert truth.provider_source.ready is False
    assert truth.provider_source.live_success is None

    await plugin.stop()


@pytest.mark.asyncio
async def test_direct_core_files_ports_are_composed_only_for_direct_core_and_withdrawn_on_close() -> None:
    class RegistrationPort:
        async def register(self, _request: object) -> object:
            raise AssertionError("registration is outside this composition test")

    class ReadPort:
        async def read_for_delivery(self, _request: object) -> object:
            raise AssertionError("delivery read is outside this composition test")

    registration = RegistrationPort()
    read_port = ReadPort()
    surface = DiscordCoreSurfaceGateway(_Gateway(), files=registration)  # type: ignore[arg-type]
    direct_bot = _bot(
        _settings(
            ai_execution_topology="direct_core",
            ai_hosting_profile="official_managed",
            ai_packaging_candidate="official_private",
        )
    )
    direct_plugin = AIPlugin(
        direct_core_gateway_factory=lambda _service: surface,
        direct_core_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
        core_files_read_port=read_port,  # type: ignore[arg-type]
    )
    await direct_plugin.start(direct_bot)

    preparer = direct_plugin._core_artifact_delivery
    assert surface.files_registration_available is True
    assert direct_bot.ai_attachments_available is True
    assert preparer is not None
    assert direct_plugin._ai_group is not None
    assert direct_plugin._ai_group._core_artifact_delivery is preparer
    assert direct_plugin._mention_listener is not None
    assert direct_plugin._mention_listener.core_artifact_delivery is preparer
    assert await preparer.currently_available(lambda: True)

    await direct_plugin.begin_close()
    assert not await preparer.currently_available(lambda: True)
    await direct_plugin.stop()
    assert direct_plugin._core_artifact_delivery is None

    local_bot = _bot(_settings())
    local_plugin = AIPlugin(
        execution_gateway_factory=lambda _service: _Gateway(),
        execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
        core_files_read_port=read_port,  # type: ignore[arg-type]
    )
    await local_plugin.start(local_bot)
    try:
        assert local_bot.ai_attachments_available is False
        assert local_plugin._core_artifact_delivery is None
        assert local_plugin._ai_group is not None
        assert local_plugin._ai_group._core_artifact_delivery is None
        assert local_plugin._mention_listener is not None
        assert local_plugin._mention_listener.core_artifact_delivery is None
    finally:
        await local_plugin.stop()


@pytest.mark.asyncio
async def test_hybrid_requires_and_composes_both_explicit_ports() -> None:
    local = _Gateway()
    remote = _Gateway()
    bot = _bot(
        _settings(
            ai_execution_topology="hybrid",
            ai_hosting_profile="official_hybrid_private",
            ai_packaging_candidate="local_only",
        )
    )
    plugin = AIPlugin(
        execution_gateway_factory=lambda _service: local,
        execution_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
        hybrid_core_gateway_factory=lambda _service: remote,
        hybrid_core_gateway_dependency_classes=(PackagingDependencyClass.PUBLIC_CODE,),
        hybrid_selector=lambda _request: False,
    )
    await plugin.start(bot)

    gateway = bot.ai_execution_gateway
    assert isinstance(gateway, HybridExecutionGateway)
    assert gateway._local is local
    assert gateway._remote is remote
    assert bot.ai_execution_profile_selection.topology is ExecutionTopology.HYBRID

    await plugin.stop()
