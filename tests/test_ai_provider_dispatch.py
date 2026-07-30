from __future__ import annotations

import sqlite3
import asyncio
import time
from dataclasses import dataclass

import pytest

from yonerai_discord.ai_control import RiskLevel as AIRiskLevel
from yonerai_discord.ai_control import TaskComplexity, TaskKind
from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.ai.models import (
    AIReply,
    AIRequest,
    DataBoundary,
    MessageRole,
    Turn,
    provider_facing_envelope_digest,
)
from yonerai_discord.modules.ai.bounded_tools import (
    BoundedToolSet,
    EMPTY_CAPABILITY_SNAPSHOT,
    ToolScopeBinding,
)
from yonerai_discord.modules.ai.provider_dispatch import (
    PreferenceAwareAIProviderSelector,
    ProviderReadinessTracker,
    build_runtime_ai_catalog,
)
from yonerai_discord.modules.ai.ports import _verify_service_sink
from yonerai_discord.modules.ai.service import AIService, ProviderSelectionError
from yonerai_discord.provider_registry import (
    HealthStatus,
    LogicalCapability,
    ModelBinding,
    ModelLoadPolicy,
    OffloadPolicy,
    ProviderCatalogManifest,
    ProviderKind,
    ProviderManifest,
    ResourceProfile,
    ResourceTarget,
)
from yonerai_discord.provider_registry.manifest import CapabilityPolicy, CapabilityRoute, TierRoute
from yonerai_discord.v0_contracts import (
    FORMAL_PROVIDER_INPUT_DIRECTIVE,
    ContextBuildInput,
    MemoryVisibility,
    Scope,
)
from yonerai_discord.v0_runtime.context_builder import RuntimeContextBuilder
from yonerai_discord.v0_runtime.command_service import CommandPreference, CommandScope
from yonerai_discord.v0_runtime.integration import RuntimeRouterAvailabilityPort
from yonerai_discord.v0_runtime.provider_router import (
    PreferenceLevel,
    ProviderPreference,
    ProviderPreferenceRepository,
    ProviderPreferenceRouter,
    ProviderReadiness,
)
from yonerai_discord.modules.ai.state_repository import migrate_v0_provider_preferences


@dataclass
class _FakeProvider:
    provider_id: str
    is_local: bool
    calls: list[AIRequest]

    async def complete(self, request: AIRequest) -> AIReply:
        self.calls.append(request)
        return AIReply(text="ok", model=request.effective_model_alias or "auto-model", provider=self.provider_id)

    async def complete_authorized(
        self,
        request: AIRequest,
        provider_sink_verifier: object,
    ) -> AIReply:
        if not _verify_service_sink(
            provider_sink_verifier,
            request=request,
            provider=self,
        ):
            raise PermissionError("authorization changed")
        return await self.complete(request)


class _BlockingProvider(_FakeProvider):
    def __init__(self, provider_id: str, *, is_local: bool) -> None:
        super().__init__(provider_id, is_local, [])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: AIRequest) -> AIReply:
        self.calls.append(request)
        self.started.set()
        await self.release.wait()
        return AIReply(text="ok", model=request.effective_model_alias or "auto-model", provider=self.provider_id)


def _catalog() -> ProviderCatalogManifest:
    local_resources = ResourceProfile(
        ResourceTarget.CPU,
        1,
        load_policy=ModelLoadPolicy.PROVIDER_MANAGED,
        offload_policy=OffloadPolicy.DISABLED,
        exclusive_gpu_lease=False,
    )
    remote_resources = ResourceProfile(
        ResourceTarget.REMOTE,
        1,
        load_policy=ModelLoadPolicy.PROVIDER_MANAGED,
        offload_policy=OffloadPolicy.DISABLED,
        exclusive_gpu_lease=False,
    )
    aliases = ("ai.fast", "ai.balanced", "ai.quality")
    local = ProviderManifest(
        "provider.local",
        ProviderKind.LOCAL,
        "adapter.local",
        (LogicalCapability.AI_TEXT,),
        local_resources,
        enabled=True,
        models=tuple(ModelBinding(alias, f"local-{alias}", probe_required=False) for alias in aliases),
    )
    remote = ProviderManifest(
        "provider.remote",
        ProviderKind.API,
        "adapter.remote",
        (LogicalCapability.AI_TEXT,),
        remote_resources,
        enabled=True,
        models=tuple(ModelBinding(alias, f"remote-{alias}", probe_required=False) for alias in aliases),
    )
    return ProviderCatalogManifest(
        1,
        "test.ai-dispatch",
        (
            CapabilityPolicy(
                LogicalCapability.AI_TEXT,
                True,
                RbacLevel.EVERYONE,
                RiskLevel.MEDIUM,
                requires_consent=True,
                audit_required=False,
            ),
        ),
        (local, remote),
        (
            CapabilityRoute(
                LogicalCapability.AI_TEXT,
                (
                    TierRoute("fast", ("provider.local", "provider.remote"), "ai.fast"),
                    TierRoute("balanced", ("provider.local", "provider.remote"), "ai.balanced"),
                    TierRoute("quality", ("provider.remote", "provider.local"), "ai.quality"),
                ),
            ),
        ),
    )


def test_runtime_catalog_binds_vision_only_when_attachment_input_is_enabled() -> None:
    model_bindings = {
        "ai.fast": "configured-fast",
        "ai.balanced": "configured-balanced",
        "ai.quality": "configured-quality",
    }

    disabled = build_runtime_ai_catalog(
        provider_id="provider.local.attachments",
        is_local=True,
        model_bindings=model_bindings,
    )
    enabled = build_runtime_ai_catalog(
        provider_id="provider.local.attachments",
        is_local=True,
        model_bindings=model_bindings,
        supports_attachments=True,
    )

    disabled_provider = disabled.provider("provider.local.attachments")
    enabled_provider = enabled.provider("provider.local.attachments")
    assert disabled_provider is not None
    assert enabled_provider is not None
    assert LogicalCapability.VISION_UNDERSTANDING not in disabled_provider.capabilities
    assert LogicalCapability.VISION_UNDERSTANDING in enabled_provider.capabilities
    assert all(disabled_provider.model_for(f"vision.{tier}") is None for tier in ("fast", "balanced", "quality"))
    assert {
        alias: enabled_provider.model_for(alias) for alias in ("vision.fast", "vision.balanced", "vision.quality")
    } == {
        "vision.fast": "configured-fast",
        "vision.balanced": "configured-balanced",
        "vision.quality": "configured-quality",
    }
    disabled_route = disabled.route(LogicalCapability.VISION_UNDERSTANDING)
    enabled_route = enabled.route(LogicalCapability.VISION_UNDERSTANDING)
    assert disabled_route is not None
    assert enabled_route is not None
    assert all(not tier.provider_ids for tier in disabled_route.tiers)
    assert all(tier.provider_ids == ("provider.local.attachments",) for tier in enabled_route.tiers)
    assert disabled.capability_policy(LogicalCapability.VISION_UNDERSTANDING).default_enabled is False  # type: ignore[union-attr]
    assert enabled.capability_policy(LogicalCapability.VISION_UNDERSTANDING).default_enabled is True  # type: ignore[union-attr]


def _runtime(
    *,
    readiness: dict[str, ProviderReadiness] | None = None,
) -> tuple[
    AIService,
    ProviderPreferenceRepository,
    _FakeProvider,
    _FakeProvider,
    dict[str, ProviderReadiness],
]:
    connection = sqlite3.connect(":memory:")
    migrate_v0_provider_preferences(connection)
    repository = ProviderPreferenceRepository(connection)
    local = _FakeProvider("provider.local", True, [])
    remote = _FakeProvider("provider.remote", False, [])
    current_readiness = readiness or {
        "provider.local": ProviderReadiness(True, HealthStatus.READY),
        "provider.remote": ProviderReadiness(True, HealthStatus.READY),
    }
    selector = PreferenceAwareAIProviderSelector(
        ProviderPreferenceRouter(repository, _catalog()),
        providers={"provider.local": local, "provider.remote": remote},
        readiness=lambda: current_readiness,
        default_provider_id="provider.local",
    )
    return (
        AIService(
            local,
            provider_selector=selector,
            require_prepared_context=True,
            require_authorization=True,
        ),
        repository,
        local,
        remote,
        current_readiness,
    )


def _request(
    *,
    remote: bool = False,
    required_model_alias: str | None = None,
    required_model_id: str | None = None,
) -> AIRequest:
    scope = Scope(10, 30, channel_id=20, visibility=MemoryVisibility.USER_PRIVATE)
    issued_at = time.monotonic()
    toolset = BoundedToolSet.issue(
        scope=ToolScopeBinding(10, 20, 30),
        intent="conversation",
        complexity="standard",
        snapshot=EMPTY_CAPABILITY_SNAPSHOT,
        provider_catalog_revision=_catalog().content_revision,
        web_search=False,
        issued_at=issued_at,
    )
    boundary = DataBoundary.REMOTE_OPT_IN if remote else DataBoundary.LOCAL_ONLY
    provider_envelope_sha256 = provider_facing_envelope_digest(
        prompt="current user input",
        provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
        history=(
            Turn(MessageRole.USER, "before"),
            Turn(MessageRole.ASSISTANT, "answer"),
        ),
        attachments=(),
        metadata={},
        task_kind=TaskKind.GENERAL,
        complexity=TaskComplexity.STANDARD,
        risk=AIRiskLevel.NORMAL,
        uses_tools=False,
        web_search=False,
        has_side_effects=False,
        boundary=boundary,
        required_model_alias=required_model_alias,
        required_model_id=required_model_id,
    )
    context = RuntimeContextBuilder().build(
        ContextBuildInput(
            scope,
            "current user input",
            (),
            ("before", "answer"),
            request_channel_id=20,
            intent="conversation",
            complexity="standard",
            bounded_toolset_digest=toolset.digest,
            capability_catalog_revision=toolset.capability_catalog_revision,
            provider_catalog_revision=toolset.provider_catalog_revision,
            provider_envelope_sha256=provider_envelope_sha256,
        )
    )
    return AIRequest(
        prompt="current user input",
        provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
        context_authorization=context.context_authorization,
        system_prompt=context.prompt,
        guild_id=10,
        channel_id=20,
        user_id=30,
        boundary=boundary,
        bounded_toolset=toolset,
        required_model_alias=required_model_alias,
        required_model_id=required_model_id,
        history=(
            Turn(MessageRole.USER, "before"),
            Turn(MessageRole.ASSISTANT, "answer"),
        ),
    )


@pytest.mark.asyncio
async def test_ai_service_rejects_selector_provider_id_runtime_id_mismatch_before_provider_call() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_v0_provider_preferences(connection)
    repository = ProviderPreferenceRepository(connection)
    mismatched = _FakeProvider("provider.other", True, [])
    selector = PreferenceAwareAIProviderSelector(
        ProviderPreferenceRouter(repository, _catalog()),
        providers={"provider.local": mismatched},
        readiness=lambda: {
            "provider.local": ProviderReadiness(True, HealthStatus.READY),
        },
        default_provider_id="provider.local",
    )
    service = AIService(
        mismatched,
        provider_selector=selector,
        require_prepared_context=True,
        require_authorization=True,
    )

    with pytest.raises(ProviderSelectionError, match="provider_identity_mismatch"):
        await service.ask(_request(), provider_call_allowed=lambda: True)

    assert mismatched.calls == []


@pytest.mark.asyncio
async def test_explicit_provider_and_model_are_applied_to_real_dispatch() -> None:
    service, repository, local, remote, _readiness = _runtime()
    scope = Scope(10, 30, channel_id=20, visibility=MemoryVisibility.USER_PRIVATE)
    repository.save(ProviderPreference(PreferenceLevel.USER, scope, "ai.quality", "provider.remote"))

    reply = await service.ask(_request(remote=True), provider_call_allowed=lambda: True)

    assert local.calls == []
    assert len(remote.calls) == 1
    assert remote.calls[0].effective_model_alias == "ai.quality"
    assert reply.provider == "provider.remote"
    assert reply.model == "ai.quality"


@pytest.mark.asyncio
async def test_route_preview_and_actual_sink_share_the_same_resolution() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_v0_provider_preferences(connection)
    repository = ProviderPreferenceRepository(connection)
    local = _FakeProvider("provider.local", True, [])
    remote = _FakeProvider("provider.remote", False, [])
    selector = PreferenceAwareAIProviderSelector(
        ProviderPreferenceRouter(repository, _catalog()),
        providers={"provider.local": local, "provider.remote": remote},
        readiness=lambda: {
            "provider.local": ProviderReadiness(True, HealthStatus.READY),
            "provider.remote": ProviderReadiness(True, HealthStatus.READY),
        },
        default_provider_id="provider.local",
    )
    service = AIService(
        local,
        provider_selector=selector,
        require_prepared_context=True,
        require_authorization=True,
    )
    memory_scope = Scope(10, 30, channel_id=20, visibility=MemoryVisibility.USER_PRIVATE)
    repository.save(ProviderPreference(PreferenceLevel.USER, memory_scope, "ai.quality", "provider.remote"))
    preview = RuntimeRouterAvailabilityPort(
        selector,
        consent_verified=lambda _scope, user_id: user_id == 30,
    ).resolve(
        CommandScope(10, channel_id=20),
        30,
        CommandPreference("ai.quality", "provider.remote"),
    )

    reply = await service.ask(_request(remote=True), provider_call_allowed=lambda: True)

    assert preview.executable is True
    assert preview.model_alias == remote.calls[0].effective_model_alias
    assert preview.provider_id == reply.provider == "provider.remote"
    assert local.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "readiness", "reason"),
    (
        (
            DataBoundary.REMOTE_OPT_IN,
            {"provider.remote": ProviderReadiness(False, HealthStatus.UNKNOWN)},
            "adapter_missing",
        ),
        (
            DataBoundary.REMOTE_OPT_IN,
            {"provider.remote": ProviderReadiness(True, HealthStatus.UNKNOWN)},
            "health_unknown",
        ),
        (
            DataBoundary.LOCAL_ONLY,
            {"provider.remote": ProviderReadiness(True, HealthStatus.READY)},
            "consent_required",
        ),
    ),
)
async def test_explicit_unavailable_route_fails_closed_without_default_fallback(
    boundary: DataBoundary,
    readiness: dict[str, ProviderReadiness],
    reason: str,
) -> None:
    service, repository, local, remote, _current = _runtime(readiness=readiness)
    scope = Scope(10, 30, channel_id=20, visibility=MemoryVisibility.USER_PRIVATE)
    repository.save(ProviderPreference(PreferenceLevel.USER, scope, "ai.quality", "provider.remote"))
    request = _request(remote=boundary is DataBoundary.REMOTE_OPT_IN)

    with pytest.raises(ProviderSelectionError, match=reason):
        await service.ask(request, provider_call_allowed=lambda: True)

    assert local.calls == []
    assert remote.calls == []


@pytest.mark.asyncio
async def test_successful_auto_call_promotes_typed_readiness_for_later_explicit_route() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_v0_provider_preferences(connection)
    repository = ProviderPreferenceRepository(connection)
    local = _FakeProvider("provider.local", True, [])
    tracker = ProviderReadinessTracker(("provider.local",))
    selector = PreferenceAwareAIProviderSelector(
        ProviderPreferenceRouter(repository, _catalog()),
        providers={"provider.local": local},
        readiness=tracker.snapshot,
        readiness_updates=tracker,
        default_provider_id="provider.local",
    )
    service = AIService(
        local,
        provider_selector=selector,
        require_prepared_context=True,
        require_authorization=True,
    )
    scope = Scope(10, 30, channel_id=20, visibility=MemoryVisibility.USER_PRIVATE)
    repository.save(ProviderPreference(PreferenceLevel.USER, scope, "ai.balanced", "provider.local"))

    with pytest.raises(ProviderSelectionError, match="health_unknown"):
        await service.ask(_request(), provider_call_allowed=lambda: True)

    repository.delete(PreferenceLevel.USER, scope)
    await service.ask(_request(), provider_call_allowed=lambda: True)
    repository.save(ProviderPreference(PreferenceLevel.USER, scope, "ai.balanced", "provider.local"))
    reply = await service.ask(_request(), provider_call_allowed=lambda: True)

    assert tracker.snapshot()["provider.local"].health is HealthStatus.READY
    assert reply.provider == "provider.local"
    assert local.calls[-1].effective_model_alias == "ai.balanced"


@pytest.mark.asyncio
async def test_auto_keeps_existing_allowed_default_with_explicit_reason() -> None:
    service, _repository, local, remote, _readiness = _runtime()

    reply = await service.ask(_request(), provider_call_allowed=lambda: True)

    assert len(local.calls) == 1
    assert remote.calls == []
    assert local.calls[0].effective_model_alias is None
    assert reply.provider == "provider.local"


@pytest.mark.asyncio
async def test_switching_provider_preserves_context_history_and_conversation_identity() -> None:
    service, repository, local, remote, _readiness = _runtime()
    original = _request()

    await service.ask(original, provider_call_allowed=lambda: True)
    scope = Scope(10, 30, channel_id=20, visibility=MemoryVisibility.USER_PRIVATE)
    repository.save(ProviderPreference(PreferenceLevel.USER, scope, "ai.balanced", "provider.remote"))
    await service.ask(_request(remote=True), provider_call_allowed=lambda: True)

    assert len(local.calls) == len(remote.calls) == 1
    before = local.calls[0]
    after = remote.calls[0]
    assert (before.prompt, before.system_prompt, before.history) == (
        after.prompt,
        after.system_prompt,
        after.history,
    )
    assert (before.guild_id, before.channel_id, before.user_id) == (
        after.guild_id,
        after.channel_id,
        after.user_id,
    )
    assert before.effective_model_alias is None
    assert after.effective_model_alias == "ai.balanced"


@pytest.mark.asyncio
async def test_task_required_quality_route_ignores_user_preference_and_uses_required_binding() -> None:
    service, repository, local, remote, _readiness = _runtime()
    scope = Scope(10, 30, channel_id=20, visibility=MemoryVisibility.USER_PRIVATE)
    repository.save(
        ProviderPreference(
            PreferenceLevel.USER,
            scope,
            "ai.balanced",
            "provider.remote",
        )
    )
    request = _request(
        required_model_alias="ai.quality",
        required_model_id="local-ai.quality",
    )

    reply = await service.ask(request, provider_call_allowed=lambda: True)

    assert reply.provider == "provider.local"
    assert len(local.calls) == 1
    assert remote.calls == []
    assert local.calls[0].effective_model_alias == "ai.quality"


@pytest.mark.asyncio
async def test_route_change_while_waiting_is_rejected_before_provider_call() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_v0_provider_preferences(connection)
    repository = ProviderPreferenceRepository(connection)
    local = _BlockingProvider("provider.local", is_local=True)
    remote = _FakeProvider("provider.remote", False, [])
    readiness = {
        "provider.local": ProviderReadiness(True, HealthStatus.READY),
        "provider.remote": ProviderReadiness(True, HealthStatus.READY),
    }
    selector = PreferenceAwareAIProviderSelector(
        ProviderPreferenceRouter(repository, _catalog()),
        providers={"provider.local": local, "provider.remote": remote},
        readiness=lambda: readiness,
        default_provider_id="provider.local",
    )
    service = AIService(
        local,
        provider_selector=selector,
        require_prepared_context=True,
        require_authorization=True,
        concurrency=1,
        max_pending=1,
        queue_timeout_seconds=1.0,
    )
    scope = Scope(10, 30, channel_id=20, visibility=MemoryVisibility.USER_PRIVATE)

    active = asyncio.create_task(service.ask(_request(), provider_call_allowed=lambda: True))
    await asyncio.wait_for(local.started.wait(), timeout=1.0)
    repository.save(ProviderPreference(PreferenceLevel.USER, scope, "ai.quality", "provider.remote"))
    queued = asyncio.create_task(service.ask(_request(remote=True), provider_call_allowed=lambda: True))
    await asyncio.sleep(0)
    readiness["provider.remote"] = ProviderReadiness(True, HealthStatus.UNKNOWN)
    local.release.set()

    with pytest.raises(ProviderSelectionError, match="health_unknown"):
        await queued

    assert (await active).provider == "provider.local"
    assert len(local.calls) == 1
    assert remote.calls == []
