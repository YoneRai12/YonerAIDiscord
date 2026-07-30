from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.provider_registry import (
    AITextInput,
    AliasBinding,
    ArtifactKind,
    ArtifactRef,
    AuditWriteError,
    AuditOutcome,
    AuditRecord,
    BrowserActionType,
    BrowserStep,
    CapabilityPolicy,
    CapabilityRoute,
    HealthStatus,
    IsolatedBrowserInput,
    LogicalCapability,
    ModelBinding,
    ModelLoadPolicy,
    OffloadPolicy,
    OwnerRouteOverride,
    ProviderCatalogManifest,
    ProviderContractError,
    ProviderExecutionDeniedError,
    ProviderHealth,
    ProviderKind,
    ProviderInvocation,
    ProviderManifest,
    ProviderRegistry,
    ProviderRequest,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
    QualityTier,
    ReadinessCode,
    ResourceProfile,
    ResourceTarget,
    SecretReference,
    SettingReference,
    TimeoutPolicy,
    TierRoute,
    require_execution_allowed,
)


@dataclass
class MemoryAuditSink:
    records: list[AuditRecord] = field(default_factory=list)

    async def append(self, record: AuditRecord) -> None:
        self.records.append(record)


class FakeAdapter:
    def __init__(
        self,
        provider_id: str,
        *,
        adapter_id: str = "test.adapter",
        provider_model: str | None = "local-model-v1",
        health_status: HealthStatus = HealthStatus.READY,
    ) -> None:
        self.provider_id = provider_id
        self.adapter_id = adapter_id
        self.provider_model = provider_model
        self.health_status = health_status
        self.calls = 0
        self.closed = False

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status=self.health_status,
            checked_at=datetime.now(timezone.utc),
            probed_model_aliases=("ai.fast", "ai.balanced", "ai.quality"),
        )

    async def execute(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        execution_allowed=None,
    ) -> ProviderResult:
        self.calls += 1
        await require_execution_allowed(execution_allowed)
        return ProviderResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            provider_model=invocation.provider_model,
            text="ok",
        )

    async def close(self) -> None:
        self.closed = True


def _catalog(
    *,
    kind: ProviderKind = ProviderKind.LOCAL,
    capability: LogicalCapability = LogicalCapability.AI_TEXT,
    required_rbac: RbacLevel = RbacLevel.EVERYONE,
    requires_consent: bool = True,
    requires_confirmation: bool = False,
    provider_id: str = "test-provider",
    provider_model: str | None = "local-model-v1",
    request_timeout_seconds: float = 10.0,
) -> ProviderCatalogManifest:
    tier_aliases = {
        "fast": "ai.fast",
        "balanced": "ai.balanced",
        "quality": "ai.quality",
    }
    resources = (
        ResourceProfile.remote(max_concurrency=8)
        if kind is ProviderKind.API
        else ResourceProfile(target=ResourceTarget.CPU, max_concurrency=2, system_ram_budget_mb=8_192)
    )
    return ProviderCatalogManifest(
        schema_version=1,
        module_id="integration.provider-registry",
        capabilities=(
            CapabilityPolicy(
                capability=capability,
                default_enabled=True,
                required_rbac=required_rbac,
                risk=RiskLevel.HIGH if requires_confirmation else RiskLevel.MEDIUM,
                requires_consent=requires_consent,
                requires_confirmation=requires_confirmation,
                audit_required=True,
            ),
        ),
        providers=(
            ProviderManifest(
                provider_id=provider_id,
                kind=kind,
                adapter_id="test.adapter",
                capabilities=(capability,),
                resources=resources,
                enabled=True,
                models=()
                if provider_model is None
                else tuple(ModelBinding(alias=alias, provider_model=provider_model) for alias in tier_aliases.values()),
                secret_refs=() if kind is ProviderKind.LOCAL else (SecretReference.parse("env:PROVIDER_API_KEY"),),
                setting_refs=(SettingReference.parse("env:PROVIDER_BASE_URL"),),
                timeouts=TimeoutPolicy(request_seconds=request_timeout_seconds, health_seconds=1),
            ),
        ),
        routes=(
            CapabilityRoute(
                capability,
                tuple(
                    TierRoute(tier, (provider_id,), None if provider_model is None else alias)
                    for tier, alias in tier_aliases.items()
                ),
            ),
        ),
        compatibility_aliases=()
        if provider_model is None
        else (
            AliasBinding("gpt-5.6-terra", "ai.balanced"),
            AliasBinding("gpt-5.6-sol", "ai.quality"),
            AliasBinding("gpt-5.6-luna", "ai.fast"),
        ),
    )


def _ai_request(*, model_alias: str | None = "gpt-5.6-terra") -> ProviderRequest:
    return ProviderRequest(
        request_id="req-1",
        trace_id="trace-1",
        capability=LogicalCapability.AI_TEXT,
        actor_ref="actor-hash-1",
        payload=AITextInput("this prompt must never enter audit"),
        model_alias=model_alias,
    )


@pytest.mark.parametrize("kind", [ProviderKind.API, ProviderKind.LOCAL])
async def test_api_and_local_providers_use_the_same_adapter_contract(kind: ProviderKind) -> None:
    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(kind=kind), audit_sink=sink)
    adapter = FakeAdapter("test-provider")
    registry.register_adapter(adapter)

    assert registry.resolve(LogicalCapability.AI_TEXT).code is ReadinessCode.HEALTH_UNKNOWN
    await registry.refresh_health("test-provider")
    result = await registry.execute(
        _ai_request(),
        consent_verified=kind is ProviderKind.API,
    )

    assert result.text == "ok"
    assert adapter.calls == 1
    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.SUCCEEDED]
    assert [record.outcome_uncertain for record in sink.records] == [False, False]


async def test_execution_callback_denies_after_resolution_before_adapter() -> None:
    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    adapter = FakeAdapter("test-provider")
    registry.register_adapter(adapter)
    await registry.refresh_health("test-provider")
    checks = 0

    def execution_allowed() -> bool:
        nonlocal checks
        checks += 1
        return False

    with pytest.raises(ProviderExecutionDeniedError, match="authorization changed"):
        await registry.execute(_ai_request(), execution_allowed=execution_allowed)

    assert checks == 1
    assert adapter.calls == 0
    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.FAILED]
    assert sink.records[-1].failure_code == "execution_denied"
    assert sink.records[-1].outcome_uncertain is False


async def test_execution_denial_audit_failure_is_not_outcome_uncertain() -> None:
    class FailTerminalAuditSink:
        def __init__(self) -> None:
            self.calls = 0

        async def append(self, _record: AuditRecord) -> None:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("audit unavailable")

    sink = FailTerminalAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    adapter = FakeAdapter("test-provider")
    registry.register_adapter(adapter)
    await registry.refresh_health("test-provider")

    with pytest.raises(AuditWriteError) as captured:
        await registry.execute(_ai_request(), execution_allowed=lambda: False)

    assert captured.value.outcome_uncertain is False
    assert adapter.calls == 0


async def test_external_cancellation_appends_terminal_audit_and_reraises_original() -> None:
    entered = asyncio.Event()

    class BlockingAdapter(FakeAdapter):
        async def execute(
            self,
            request: ProviderRequest,
            invocation: ProviderInvocation,
            *,
            execution_allowed=None,
        ) -> ProviderResult:
            self.calls += 1
            await require_execution_allowed(execution_allowed)
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled execution resumed")

    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    adapter = BlockingAdapter("test-provider")
    registry.register_adapter(adapter)
    await registry.refresh_health("test-provider")

    task = asyncio.create_task(registry.execute(_ai_request()))
    await entered.wait()
    task.cancel("external-stop")

    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert captured.value.args == ("external-stop",)
    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.CANCELLED]
    assert sink.records[-1].failure_code is None
    assert sink.records[-1].outcome_uncertain is True


async def test_cancelled_error_subclass_is_not_replaced() -> None:
    class RemoteOutcomeUncertainCancelledError(asyncio.CancelledError):
        pass

    cancellation = RemoteOutcomeUncertainCancelledError("remote-outcome-uncertain")
    entered = asyncio.Event()

    class CancellingAdapter(FakeAdapter):
        async def execute(
            self,
            request: ProviderRequest,
            invocation: ProviderInvocation,
            *,
            execution_allowed=None,
        ) -> ProviderResult:
            self.calls += 1
            await require_execution_allowed(execution_allowed)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise cancellation from None
            raise AssertionError("cancelled execution resumed")

    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    registry.register_adapter(CancellingAdapter("test-provider"))
    await registry.refresh_health("test-provider")

    task = asyncio.create_task(registry.execute(_ai_request()))
    await entered.wait()
    task.cancel()

    with pytest.raises(RemoteOutcomeUncertainCancelledError) as captured:
        await task

    assert captured.value is cancellation
    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.CANCELLED]
    assert sink.records[-1].outcome_uncertain is True


async def test_registry_deadline_on_generic_hang_is_timed_out() -> None:
    class HangingAdapter(FakeAdapter):
        async def execute(
            self,
            request: ProviderRequest,
            invocation: ProviderInvocation,
            *,
            execution_allowed=None,
        ) -> ProviderResult:
            self.calls += 1
            await require_execution_allowed(execution_allowed)
            await asyncio.Event().wait()
            raise AssertionError("timed out execution resumed")

    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(request_timeout_seconds=1.0), audit_sink=sink)
    registry.register_adapter(HangingAdapter("test-provider"))
    await registry.refresh_health("test-provider")

    with pytest.raises(ProviderTimeoutError):
        await registry.execute(_ai_request())

    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.TIMED_OUT]
    assert sink.records[-1].failure_code == "provider_timeout"
    assert sink.records[-1].outcome_uncertain is True


async def test_registry_deadline_stays_timed_out_when_adapter_specializes_cancellation() -> None:
    class RemoteOutcomeUncertainCancelledError(asyncio.CancelledError):
        pass

    class HangingAdapter(FakeAdapter):
        async def execute(
            self,
            request: ProviderRequest,
            invocation: ProviderInvocation,
            *,
            execution_allowed=None,
        ) -> ProviderResult:
            self.calls += 1
            await require_execution_allowed(execution_allowed)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RemoteOutcomeUncertainCancelledError("deadline-cancelled") from None
            raise AssertionError("timed out execution resumed")

    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(request_timeout_seconds=1.0), audit_sink=sink)
    registry.register_adapter(HangingAdapter("test-provider"))
    await registry.refresh_health("test-provider")

    with pytest.raises(ProviderTimeoutError) as captured:
        await registry.execute(_ai_request())

    assert isinstance(captured.value.__cause__, RemoteOutcomeUncertainCancelledError)
    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.TIMED_OUT]
    assert sink.records[-1].failure_code == "provider_timeout"
    assert sink.records[-1].outcome_uncertain is True


async def test_registry_deadline_cannot_be_swallowed_into_success() -> None:
    class CancellationSwallowingAdapter(FakeAdapter):
        async def execute(
            self,
            request: ProviderRequest,
            invocation: ProviderInvocation,
            *,
            execution_allowed=None,
        ) -> ProviderResult:
            self.calls += 1
            await require_execution_allowed(execution_allowed)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return ProviderResult(
                    request_id=request.request_id,
                    provider_id=self.provider_id,
                    provider_model=invocation.provider_model,
                    text="must-not-succeed",
                )
            raise AssertionError("timed out execution resumed")

    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(request_timeout_seconds=1.0), audit_sink=sink)
    adapter = CancellationSwallowingAdapter("test-provider")
    registry.register_adapter(adapter)
    await registry.refresh_health("test-provider")

    with pytest.raises(ProviderTimeoutError):
        await registry.execute(_ai_request())

    assert adapter.calls == 1
    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.TIMED_OUT]
    assert sink.records[-1].failure_code == "provider_timeout"
    assert sink.records[-1].outcome_uncertain is True


async def test_repeated_cancel_during_success_audit_persists_terminal_once_before_reraising() -> None:
    class ArtifactAdapter(FakeAdapter):
        async def execute(
            self,
            request: ProviderRequest,
            invocation: ProviderInvocation,
            *,
            execution_allowed=None,
        ) -> ProviderResult:
            self.calls += 1
            await require_execution_allowed(execution_allowed)
            return ProviderResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                provider_model=invocation.provider_model,
                artifacts=(ArtifactRef("artifact-1", ArtifactKind.TEXT, "text/plain"),),
            )

    class BlockingSuccessAuditSink(MemoryAuditSink):
        def __init__(self) -> None:
            super().__init__()
            self.terminal_started = asyncio.Event()
            self.release_terminal = asyncio.Event()

        async def append(self, record: AuditRecord) -> None:
            if record.outcome is AuditOutcome.SUCCEEDED:
                self.terminal_started.set()
                await self.release_terminal.wait()
            self.records.append(record)

    sink = BlockingSuccessAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    adapter = ArtifactAdapter("test-provider")
    registry.register_adapter(adapter)
    await registry.refresh_health("test-provider")

    task = asyncio.create_task(registry.execute(_ai_request()))
    await sink.terminal_started.wait()
    task.cancel("cancel-during-success-audit")
    await asyncio.sleep(0)
    assert task.done() is False
    task.cancel("cancel-during-success-audit-again")
    await asyncio.sleep(0)
    assert task.done() is False
    sink.release_terminal.set()

    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    assert captured.value.args == ("cancel-during-success-audit",)
    assert adapter.calls == 1
    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.SUCCEEDED]
    assert sink.records[-1].artifact_ids == ("artifact-1",)
    assert sink.records[-1].outcome_uncertain is False


@pytest.mark.parametrize(
    ("provider_error", "expected_error", "outcome", "failure_code"),
    [
        (TimeoutError("provider deadline"), ProviderTimeoutError, AuditOutcome.TIMED_OUT, "provider_timeout"),
        (RuntimeError("provider failed"), RuntimeError, AuditOutcome.FAILED, "provider_error"),
    ],
)
async def test_timeout_and_provider_failure_audits_are_outcome_uncertain(
    provider_error: Exception,
    expected_error: type[Exception],
    outcome: AuditOutcome,
    failure_code: str,
) -> None:
    class FailingAdapter(FakeAdapter):
        async def execute(
            self,
            request: ProviderRequest,
            invocation: ProviderInvocation,
            *,
            execution_allowed=None,
        ) -> ProviderResult:
            self.calls += 1
            await require_execution_allowed(execution_allowed)
            raise provider_error

    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    registry.register_adapter(FailingAdapter("test-provider"))
    await registry.refresh_health("test-provider")

    with pytest.raises(expected_error):
        await registry.execute(_ai_request())

    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, outcome]
    assert sink.records[-1].failure_code == failure_code
    assert sink.records[-1].outcome_uncertain is True


async def test_legacy_terra_alias_maps_to_a_local_provider_model() -> None:
    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    registry.register_adapter(FakeAdapter("test-provider"))
    await registry.refresh_health("test-provider")

    resolution = registry.resolve(LogicalCapability.AI_TEXT, model_alias="gpt-5.6-terra")
    assert resolution.ready
    assert resolution.model_alias == "ai.balanced"
    assert resolution.provider_model == "local-model-v1"


async def test_every_capability_route_has_fast_balanced_quality_and_defaults_to_balanced() -> None:
    registry = ProviderRegistry(_catalog(), audit_sink=MemoryAuditSink())
    registry.register_adapter(FakeAdapter("test-provider"))
    await registry.refresh_health("test-provider")

    default = registry.resolve(LogicalCapability.AI_TEXT)
    quality = registry.resolve(LogicalCapability.AI_TEXT, model_alias="gpt-5.6-sol")
    fast = registry.resolve(LogicalCapability.AI_TEXT, quality_tier=QualityTier.FAST)
    assert (default.quality_tier, default.model_alias) == (QualityTier.BALANCED, "ai.balanced")
    assert (quality.quality_tier, quality.model_alias) == (QualityTier.QUALITY, "ai.quality")
    assert (fast.quality_tier, fast.model_alias) == (QualityTier.FAST, "ai.fast")


async def test_only_bot_owner_can_apply_explicit_route_override() -> None:
    registry = ProviderRegistry(_catalog(), audit_sink=MemoryAuditSink())
    registry.register_adapter(FakeAdapter("test-provider"))
    await registry.refresh_health("test-provider")
    override = OwnerRouteOverride(quality_tier=QualityTier.QUALITY, provider_id="test-provider")

    denied = registry.resolve(LogicalCapability.AI_TEXT, owner_override=override)
    allowed = registry.resolve(
        LogicalCapability.AI_TEXT,
        actor_level=RbacLevel.BOT_OWNER,
        owner_override=override,
    )
    assert denied.code is ReadinessCode.OWNER_OVERRIDE_REQUIRES_OWNER
    assert allowed.ready
    assert allowed.quality_tier is QualityTier.QUALITY
    assert allowed.model_alias == "ai.quality"


def test_rtx_5090_profile_can_declare_tier_vram_and_exclusive_gpu_lease() -> None:
    profile = ResourceProfile(
        target=ResourceTarget.CUDA,
        max_concurrency=1,
        vram_budget_mb=28_672,
        system_ram_budget_mb=32_768,
        load_policy=ModelLoadPolicy.IDLE_UNLOAD,
        idle_unload_seconds=300,
        offload_policy=OffloadPolicy.CPU_ALLOWED,
        exclusive_gpu_lease=True,
        device_ref=SettingReference.parse("env:YONERAI_GPU_DEVICE"),
    )
    binding = ModelBinding("ai.quality", "local-quality-model", resources=profile)

    assert binding.resources is not None
    assert binding.resources.vram_budget_mb == 28_672
    assert binding.resources.gpu_lease_key == "env:YONERAI_GPU_DEVICE"


async def test_remote_provider_requires_consent_but_local_provider_does_not() -> None:
    for kind, expected in (
        (ProviderKind.API, ReadinessCode.CONSENT_REQUIRED),
        (ProviderKind.LOCAL, ReadinessCode.READY),
    ):
        registry = ProviderRegistry(_catalog(kind=kind), audit_sink=MemoryAuditSink())
        registry.register_adapter(FakeAdapter("test-provider"))
        await registry.refresh_health("test-provider")
        assert registry.resolve(LogicalCapability.AI_TEXT).code is expected


async def test_browser_provider_is_owner_only_and_requires_confirmation() -> None:
    manifest = _catalog(
        capability=LogicalCapability.ISOLATED_BROWSER,
        required_rbac=RbacLevel.BOT_OWNER,
        requires_consent=False,
        requires_confirmation=True,
        provider_model=None,
    )
    registry = ProviderRegistry(manifest, audit_sink=MemoryAuditSink())
    registry.register_adapter(FakeAdapter("test-provider", provider_model=None))
    await registry.refresh_health("test-provider")

    assert registry.resolve(LogicalCapability.ISOLATED_BROWSER).code is ReadinessCode.INSUFFICIENT_RBAC
    assert (
        registry.resolve(LogicalCapability.ISOLATED_BROWSER, actor_level=RbacLevel.BOT_OWNER).code
        is ReadinessCode.CONFIRMATION_REQUIRED
    )
    assert registry.resolve(
        LogicalCapability.ISOLATED_BROWSER,
        actor_level=RbacLevel.BOT_OWNER,
        confirmation_verified=True,
    ).ready

    request = ProviderRequest(
        request_id="req-browser",
        trace_id="trace-browser",
        capability=LogicalCapability.ISOLATED_BROWSER,
        actor_ref="owner-hash",
        payload=IsolatedBrowserInput(
            "https://example.com",
            (BrowserStep(BrowserActionType.SCREENSHOT),),
        ),
    )
    result = await registry.execute(
        request,
        actor_level=RbacLevel.BOT_OWNER,
        confirmation_verified=True,
    )
    assert result.text == "ok"


async def test_capability_and_provider_can_be_turned_off_independently() -> None:
    registry = ProviderRegistry(_catalog(), audit_sink=MemoryAuditSink())
    registry.register_adapter(FakeAdapter("test-provider"))
    await registry.refresh_health("test-provider")

    registry.set_provider_enabled("test-provider", False)
    assert registry.resolve(LogicalCapability.AI_TEXT).code is ReadinessCode.PROVIDER_DISABLED
    registry.set_provider_enabled("test-provider", None)
    registry.set_capability_enabled(LogicalCapability.AI_TEXT, False)
    assert registry.resolve(LogicalCapability.AI_TEXT).code is ReadinessCode.CAPABILITY_DISABLED


async def test_audit_sink_is_required_and_never_receives_prompt_or_response() -> None:
    registry_without_audit = ProviderRegistry(_catalog())
    registry_without_audit.register_adapter(FakeAdapter("test-provider"))
    await registry_without_audit.refresh_health("test-provider")
    resolution = registry_without_audit.resolve(LogicalCapability.AI_TEXT)
    assert resolution.code is ReadinessCode.AUDIT_SINK_MISSING
    with pytest.raises(ProviderUnavailableError):
        await registry_without_audit.execute(_ai_request())

    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    registry.register_adapter(FakeAdapter("test-provider"))
    await registry.refresh_health("test-provider")
    await registry.execute(_ai_request())
    audit_text = repr(sink.records)
    assert "this prompt must never enter audit" not in audit_text
    assert "ok" not in audit_text


def test_adapter_id_mismatch_is_rejected_without_dynamic_loading() -> None:
    registry = ProviderRegistry(_catalog(), audit_sink=MemoryAuditSink())
    with pytest.raises(ProviderContractError, match="adapter_id mismatch"):
        registry.register_adapter(FakeAdapter("test-provider", adapter_id="wrong.adapter"))


@pytest.mark.parametrize("use_kwargs", [False, True])
def test_adapter_registration_requires_explicit_keyword_only_execution_check(use_kwargs: bool) -> None:
    class InvalidAdapter(FakeAdapter):
        if use_kwargs:

            async def execute(self, request, invocation, **kwargs):
                return await super().execute(request, invocation, **kwargs)

        else:

            async def execute(self, request, invocation):
                return await super().execute(request, invocation)

    registry = ProviderRegistry(_catalog(), audit_sink=MemoryAuditSink())
    with pytest.raises(ProviderContractError, match="keyword-only execution_allowed"):
        registry.register_adapter(InvalidAdapter("test-provider"))


async def test_adapter_that_ignores_execution_check_cannot_return_success() -> None:
    class IgnoringAdapter(FakeAdapter):
        async def execute(
            self,
            request: ProviderRequest,
            invocation: ProviderInvocation,
            *,
            execution_allowed=None,
        ) -> ProviderResult:
            self.calls += 1
            return ProviderResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                provider_model=invocation.provider_model,
                text="must-not-succeed",
            )

    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    adapter = IgnoringAdapter("test-provider")
    registry.register_adapter(adapter)
    await registry.refresh_health("test-provider")

    with pytest.raises(ProviderContractError, match="did not evaluate execution_allowed"):
        await registry.execute(_ai_request())

    assert adapter.calls == 1
    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.FAILED]
    assert sink.records[-1].failure_code == "provider_error"


async def test_adapter_that_swallows_execution_denial_cannot_return_success() -> None:
    class SwallowingAdapter(FakeAdapter):
        async def execute(
            self,
            request: ProviderRequest,
            invocation: ProviderInvocation,
            *,
            execution_allowed=None,
        ) -> ProviderResult:
            self.calls += 1
            try:
                await execution_allowed()
            except ProviderExecutionDeniedError:
                pass
            return ProviderResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                provider_model=invocation.provider_model,
                text="must-not-succeed",
            )

    decisions = iter((True, False))
    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    adapter = SwallowingAdapter("test-provider")
    registry.register_adapter(adapter)
    await registry.refresh_health("test-provider")

    with pytest.raises(ProviderContractError, match="did not evaluate execution_allowed"):
        await registry.execute(_ai_request(), execution_allowed=lambda: next(decisions))

    assert adapter.calls == 1
    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.FAILED]


async def test_adapter_cannot_hide_denial_after_one_successful_execution_check() -> None:
    class LateSwallowingAdapter(FakeAdapter):
        async def execute(
            self,
            request: ProviderRequest,
            invocation: ProviderInvocation,
            *,
            execution_allowed=None,
        ) -> ProviderResult:
            self.calls += 1
            assert await execution_allowed()
            try:
                await execution_allowed()
            except ProviderExecutionDeniedError:
                pass
            return ProviderResult(
                request_id=request.request_id,
                provider_id=self.provider_id,
                provider_model=invocation.provider_model,
                text="must-not-succeed",
            )

    decisions = iter((True, True, False))
    sink = MemoryAuditSink()
    registry = ProviderRegistry(_catalog(), audit_sink=sink)
    adapter = LateSwallowingAdapter("test-provider")
    registry.register_adapter(adapter)
    await registry.refresh_health("test-provider")

    with pytest.raises(ProviderContractError, match="did not evaluate execution_allowed"):
        await registry.execute(_ai_request(), execution_allowed=lambda: next(decisions))

    assert adapter.calls == 1
    assert [record.outcome for record in sink.records] == [AuditOutcome.STARTED, AuditOutcome.FAILED]
