from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace

from yonerai_discord.control_plane import RbacLevel

from .domain import (
    AuditOutcome,
    AuditRecord,
    HealthStatus,
    LogicalCapability,
    OwnerRouteOverride,
    ProviderAttempt,
    ProviderHealth,
    ProviderInvocation,
    ProviderKind,
    ProviderRequest,
    ProviderResolution,
    ProviderResult,
    QualityTier,
    ReadinessCode,
    ResourceProfile,
    normalize_identifier,
    utc_now,
)
from .manifest import ProviderCatalogManifest
from .ports import ExecutionAuthorizationCheck, ProviderAdapter, ProviderAuditSink


class ProviderRegistryError(RuntimeError):
    pass


class ProviderUnavailableError(ProviderRegistryError):
    def __init__(self, resolution: ProviderResolution) -> None:
        self.resolution = resolution
        super().__init__(f"provider unavailable: {resolution.code.value}")


class ProviderTimeoutError(ProviderRegistryError):
    pass


class ProviderContractError(ProviderRegistryError):
    pass


class ProviderExecutionDeniedError(ProviderRegistryError):
    pass


class _TrackedExecutionAuthorization:
    """adapterがcommit直前認可portを実際に使用したかだけをprocess内で追跡する。"""

    def __init__(self, check: ExecutionAuthorizationCheck | None) -> None:
        self._check = check
        self.successful_calls = 0
        self.failed = False

    async def __call__(self) -> bool:
        try:
            await require_execution_allowed(self._check)
        except BaseException:
            self.failed = True
            raise
        self.successful_calls += 1
        return True


async def require_execution_allowed(execution_allowed: ExecutionAuthorizationCheck | None) -> None:
    """副作用直前の認可をfail closedで再評価するadapter契約。"""

    if execution_allowed is None:
        return
    try:
        allowed = execution_allowed()
        if inspect.isawaitable(allowed):
            allowed = await allowed
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ProviderExecutionDeniedError("provider execution authorization failed") from exc
    if allowed is not True:
        raise ProviderExecutionDeniedError("provider execution authorization changed")


class AuditWriteError(ProviderRegistryError):
    def __init__(self, message: str, *, outcome_uncertain: bool) -> None:
        self.outcome_uncertain = outcome_uncertain
        super().__init__(message)


class ProviderRegistry:
    """Logical capabilityをprovider adapterへ解決するfail-closedなruntime registry。"""

    def __init__(
        self,
        manifest: ProviderCatalogManifest,
        *,
        audit_sink: ProviderAuditSink | None = None,
    ) -> None:
        if not isinstance(manifest, ProviderCatalogManifest):
            raise TypeError("manifest must be a ProviderCatalogManifest")
        self.manifest = manifest
        self._audit_sink = audit_sink
        self._adapters: dict[str, ProviderAdapter] = {}
        self._health: dict[str, ProviderHealth] = {}
        self._capability_overrides: dict[LogicalCapability, bool] = {}
        self._provider_overrides: dict[str, bool] = {}
        self._resource_slots: dict[tuple[str, str, int], asyncio.Semaphore] = {}
        self._gpu_leases: dict[str, asyncio.Lock] = {}

    def register_adapter(self, adapter: ProviderAdapter) -> None:
        provider_id = normalize_identifier(adapter.provider_id, label="adapter provider_id")
        adapter_id = normalize_identifier(adapter.adapter_id, label="adapter_id")
        try:
            execute_signature = inspect.signature(adapter.execute)
        except (TypeError, ValueError) as exc:
            raise ProviderContractError("adapter execute signature is not inspectable") from exc
        execution_parameter = execute_signature.parameters.get("execution_allowed")
        if execution_parameter is None or execution_parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            raise ProviderContractError("adapter execute must explicitly accept keyword-only execution_allowed")
        provider = self.manifest.provider(provider_id)
        if provider is None:
            raise ProviderContractError(f"adapter references an unknown provider: {provider_id}")
        if provider.adapter_id != adapter_id:
            raise ProviderContractError(
                f"adapter_id mismatch for {provider_id}: expected={provider.adapter_id}, actual={adapter_id}"
            )
        if provider_id in self._adapters:
            raise ProviderContractError(f"adapter is already registered: {provider_id}")
        self._adapters[provider_id] = adapter
        self._health[provider_id] = ProviderHealth(
            provider_id=provider_id,
            status=HealthStatus.UNKNOWN,
            checked_at=utc_now(),
            detail_code="health_not_checked",
        )

    def unregister_adapter(self, provider_id: str) -> ProviderAdapter | None:
        normalized = normalize_identifier(provider_id, label="provider_id")
        self._health.pop(normalized, None)
        return self._adapters.pop(normalized, None)

    def unregister_adapter_if_current(self, provider_id: str, expected: ProviderAdapter) -> bool:
        normalized = normalize_identifier(provider_id, label="provider_id")
        if self._adapters.get(normalized) is not expected:
            return False
        self._health.pop(normalized, None)
        self._adapters.pop(normalized)
        return True

    def set_capability_enabled(self, capability: LogicalCapability | str, enabled: bool | None) -> None:
        normalized = LogicalCapability(capability)
        if enabled is None:
            self._capability_overrides.pop(normalized, None)
            return
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool or None")
        self._capability_overrides[normalized] = enabled

    def set_provider_enabled(self, provider_id: str, enabled: bool | None) -> None:
        normalized = normalize_identifier(provider_id, label="provider_id")
        if self.manifest.provider(normalized) is None:
            raise KeyError(normalized)
        if enabled is None:
            self._provider_overrides.pop(normalized, None)
            return
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool or None")
        self._provider_overrides[normalized] = enabled

    def capability_enabled(self, capability: LogicalCapability | str) -> bool:
        normalized = LogicalCapability(capability)
        policy = self.manifest.capability_policy(normalized)
        if policy is None:
            return False
        return self._capability_overrides.get(normalized, policy.default_enabled)

    def provider_enabled(self, provider_id: str) -> bool:
        normalized = normalize_identifier(provider_id, label="provider_id")
        provider = self.manifest.provider(normalized)
        if provider is None:
            return False
        return self._provider_overrides.get(normalized, provider.enabled)

    def health_snapshot(self, provider_id: str) -> ProviderHealth | None:
        normalized = normalize_identifier(provider_id, label="provider_id")
        return self._health.get(normalized)

    async def refresh_health(self, provider_id: str) -> ProviderHealth:
        normalized = normalize_identifier(provider_id, label="provider_id")
        provider = self.manifest.provider(normalized)
        if provider is None:
            raise KeyError(normalized)
        adapter = self._adapters.get(normalized)
        if adapter is None:
            health = ProviderHealth(
                provider_id=normalized,
                status=HealthStatus.UNAVAILABLE,
                checked_at=utc_now(),
                detail_code="adapter_missing",
            )
            self._health[normalized] = health
            return health

        started = time.monotonic()
        try:
            reported = await asyncio.wait_for(adapter.health(), timeout=provider.timeouts.health_seconds)
            if not isinstance(reported, ProviderHealth) or reported.provider_id != normalized:
                raise ProviderContractError("health response does not match the provider")
            latency_ms = reported.latency_ms
            if latency_ms is None:
                latency_ms = round((time.monotonic() - started) * 1_000)
            health = replace(reported, latency_ms=latency_ms)
        except TimeoutError:
            health = ProviderHealth(
                provider_id=normalized,
                status=HealthStatus.UNAVAILABLE,
                checked_at=utc_now(),
                latency_ms=round((time.monotonic() - started) * 1_000),
                detail_code="health_timeout",
            )
        except Exception:
            health = ProviderHealth(
                provider_id=normalized,
                status=HealthStatus.UNAVAILABLE,
                checked_at=utc_now(),
                latency_ms=round((time.monotonic() - started) * 1_000),
                detail_code="health_error",
            )
        self._health[normalized] = health
        return health

    async def refresh_all_health(self) -> tuple[ProviderHealth, ...]:
        provider_ids = tuple(provider.provider_id for provider in self.manifest.providers)
        return tuple(await asyncio.gather(*(self.refresh_health(provider_id) for provider_id in provider_ids)))

    def resolve(
        self,
        capability: LogicalCapability | str,
        *,
        actor_level: RbacLevel | str | int = RbacLevel.EVERYONE,
        model_alias: str | None = None,
        quality_tier: QualityTier | str | None = None,
        owner_override: OwnerRouteOverride | None = None,
        consent_verified: bool = False,
        confirmation_verified: bool = False,
    ) -> ProviderResolution:
        normalized = LogicalCapability(capability)
        policy = self.manifest.capability_policy(normalized)
        if policy is None:
            return ProviderResolution(False, normalized, ReadinessCode.UNKNOWN_CAPABILITY)
        if not self.capability_enabled(normalized):
            return ProviderResolution(False, normalized, ReadinessCode.CAPABILITY_DISABLED)
        parsed_actor_level = RbacLevel.parse(actor_level)
        if parsed_actor_level < policy.required_rbac:
            return ProviderResolution(False, normalized, ReadinessCode.INSUFFICIENT_RBAC)

        if owner_override is not None:
            if not isinstance(owner_override, OwnerRouteOverride):
                raise TypeError("owner_override must be an OwnerRouteOverride")
            if parsed_actor_level < RbacLevel.BOT_OWNER:
                return ProviderResolution(False, normalized, ReadinessCode.OWNER_OVERRIDE_REQUIRES_OWNER)

        route = self.manifest.route(normalized)
        if route is None:
            return ProviderResolution(False, normalized, ReadinessCode.ROUTE_UNCONFIGURED)

        explicit_alias = owner_override.model_alias if owner_override and owner_override.model_alias else model_alias
        canonical_alias = self.manifest.canonical_model_alias(explicit_alias) if explicit_alias is not None else None
        selected_tier = self._select_tier(
            route.default_tier,
            canonical_alias=canonical_alias,
            requested_tier=quality_tier,
            owner_override=owner_override,
        )
        tier_route = route.tier_route(selected_tier)
        provider_ids = tier_route.provider_ids
        if owner_override is not None and owner_override.provider_id is not None:
            override_provider = self.manifest.provider(owner_override.provider_id)
            if override_provider is None or normalized not in override_provider.capabilities:
                return ProviderResolution(
                    False,
                    normalized,
                    ReadinessCode.OVERRIDE_PROVIDER_INVALID,
                    quality_tier=selected_tier,
                )
            provider_ids = (owner_override.provider_id,)
        if not provider_ids:
            return ProviderResolution(
                False,
                normalized,
                ReadinessCode.ROUTE_UNCONFIGURED,
                quality_tier=selected_tier,
            )
        requested_alias = canonical_alias
        if requested_alias is None and tier_route.model_alias is not None:
            requested_alias = self.manifest.canonical_model_alias(tier_route.model_alias)

        attempts: list[ProviderAttempt] = []
        for provider_id in provider_ids:
            provider = self.manifest.provider(provider_id)
            if provider is None:
                continue
            if not self.provider_enabled(provider_id):
                attempts.append(ProviderAttempt(provider_id, ReadinessCode.PROVIDER_DISABLED))
                continue
            provider_model = None
            if requested_alias is not None:
                provider_model = provider.model_for(requested_alias)
                if provider_model is None:
                    attempts.append(ProviderAttempt(provider_id, ReadinessCode.MODEL_ALIAS_UNCONFIGURED))
                    continue
            if provider_id not in self._adapters:
                attempts.append(ProviderAttempt(provider_id, ReadinessCode.ADAPTER_MISSING))
                continue
            health = self._health.get(provider_id)
            if health is None or health.status is HealthStatus.UNKNOWN:
                attempts.append(ProviderAttempt(provider_id, ReadinessCode.HEALTH_UNKNOWN))
                continue
            if not health.usable:
                attempts.append(ProviderAttempt(provider_id, ReadinessCode.PROVIDER_UNHEALTHY))
                continue
            model_binding = provider.model_binding(requested_alias) if requested_alias is not None else None
            if (
                model_binding is not None
                and model_binding.probe_required
                and requested_alias not in health.probed_model_aliases
            ):
                attempts.append(ProviderAttempt(provider_id, ReadinessCode.MODEL_PROBE_REQUIRED))
                continue
            if policy.requires_consent and provider.kind is ProviderKind.API and not consent_verified:
                attempts.append(ProviderAttempt(provider_id, ReadinessCode.CONSENT_REQUIRED))
                continue
            if policy.requires_confirmation and not confirmation_verified:
                attempts.append(ProviderAttempt(provider_id, ReadinessCode.CONFIRMATION_REQUIRED))
                continue
            if policy.audit_required and self._audit_sink is None:
                attempts.append(ProviderAttempt(provider_id, ReadinessCode.AUDIT_SINK_MISSING))
                continue
            return ProviderResolution(
                True,
                normalized,
                ReadinessCode.READY,
                provider_id=provider_id,
                quality_tier=selected_tier,
                model_alias=requested_alias,
                provider_model=provider_model,
                resources=provider.resources_for(requested_alias),
                attempts=tuple(attempts),
            )

        code = attempts[-1].code if attempts else ReadinessCode.ROUTE_UNCONFIGURED
        return ProviderResolution(
            False,
            normalized,
            code,
            quality_tier=selected_tier,
            model_alias=requested_alias,
            attempts=tuple(attempts),
        )

    async def execute(
        self,
        request: ProviderRequest,
        *,
        actor_level: RbacLevel | str | int = RbacLevel.EVERYONE,
        owner_override: OwnerRouteOverride | None = None,
        consent_verified: bool = False,
        confirmation_verified: bool = False,
        execution_allowed: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> ProviderResult:
        if execution_allowed is not None and not callable(execution_allowed):
            raise TypeError("execution_allowed must be callable or None")
        resolution = self.resolve(
            request.capability,
            actor_level=actor_level,
            model_alias=request.model_alias,
            quality_tier=request.quality_tier,
            owner_override=owner_override,
            consent_verified=consent_verified,
            confirmation_verified=confirmation_verified,
        )
        if not resolution.ready or resolution.provider_id is None or resolution.resources is None:
            raise ProviderUnavailableError(resolution)
        provider = self.manifest.provider(resolution.provider_id)
        adapter = self._adapters[resolution.provider_id]
        if provider is None:  # manifest validation guarantees this; keep the execution boundary fail-closed.
            raise ProviderContractError("resolved provider disappeared")

        invocation = ProviderInvocation(
            provider_id=provider.provider_id,
            quality_tier=resolution.quality_tier,
            model_alias=resolution.model_alias,
            provider_model=resolution.provider_model,
            timeout_seconds=provider.timeouts.request_seconds,
            resources=resolution.resources,
        )
        await self._append_audit(
            AuditRecord(
                request_id=request.request_id,
                trace_id=request.trace_id,
                actor_ref=request.actor_ref,
                capability=request.capability,
                outcome=AuditOutcome.STARTED,
                occurred_at=utc_now(),
                quality_tier=resolution.quality_tier,
                provider_id=provider.provider_id,
                model_alias=resolution.model_alias,
            ),
            outcome_uncertain=False,
        )
        started = time.monotonic()
        timeout_scope: asyncio.Timeout | None = None
        try:
            async with asyncio.timeout(provider.timeouts.request_seconds) as timeout_scope:
                result = await self._execute_with_resource_lease(
                    adapter,
                    request,
                    invocation,
                    execution_allowed=execution_allowed,
                )
            if timeout_scope is not None and timeout_scope.expired():
                raise TimeoutError
            self._validate_result(request, invocation, result)
        except asyncio.CancelledError as exc:
            duration_ms = round((time.monotonic() - started) * 1_000)
            if timeout_scope is not None and timeout_scope.expired():
                await self._append_terminal_audit(
                    self._terminal_audit(
                        request,
                        resolution,
                        AuditOutcome.TIMED_OUT,
                        duration_ms=duration_ms,
                        failure_code="provider_timeout",
                        outcome_uncertain=True,
                    ),
                    write_failure_outcome_uncertain=True,
                )
                raise ProviderTimeoutError(f"provider timed out after {provider.timeouts.request_seconds:g}s") from exc
            try:
                await self._append_terminal_audit(
                    self._terminal_audit(
                        request,
                        resolution,
                        AuditOutcome.CANCELLED,
                        duration_ms=duration_ms,
                        outcome_uncertain=True,
                    ),
                    write_failure_outcome_uncertain=True,
                )
            except AuditWriteError:
                # Preserve cancellation control flow even when its terminal audit cannot be written.
                pass
            raise
        except ProviderExecutionDeniedError:
            duration_ms = round((time.monotonic() - started) * 1_000)
            await self._append_terminal_audit(
                self._terminal_audit(
                    request,
                    resolution,
                    AuditOutcome.FAILED,
                    duration_ms=duration_ms,
                    failure_code="execution_denied",
                ),
                write_failure_outcome_uncertain=False,
            )
            raise
        except TimeoutError as exc:
            duration_ms = round((time.monotonic() - started) * 1_000)
            await self._append_terminal_audit(
                self._terminal_audit(
                    request,
                    resolution,
                    AuditOutcome.TIMED_OUT,
                    duration_ms=duration_ms,
                    failure_code="provider_timeout",
                    outcome_uncertain=True,
                ),
                write_failure_outcome_uncertain=True,
            )
            raise ProviderTimeoutError(f"provider timed out after {provider.timeouts.request_seconds:g}s") from exc
        except Exception:
            duration_ms = round((time.monotonic() - started) * 1_000)
            await self._append_terminal_audit(
                self._terminal_audit(
                    request,
                    resolution,
                    AuditOutcome.FAILED,
                    duration_ms=duration_ms,
                    failure_code="provider_error",
                    outcome_uncertain=True,
                ),
                write_failure_outcome_uncertain=True,
            )
            raise

        await self._append_terminal_audit(
            self._terminal_audit(
                request,
                resolution,
                AuditOutcome.SUCCEEDED,
                duration_ms=round((time.monotonic() - started) * 1_000),
                artifact_ids=tuple(ref.artifact_id for ref in result.artifacts),
            ),
            write_failure_outcome_uncertain=True,
        )
        return result

    @staticmethod
    def _select_tier(
        default_tier: QualityTier,
        *,
        canonical_alias: str | None,
        requested_tier: QualityTier | str | None,
        owner_override: OwnerRouteOverride | None,
    ) -> QualityTier:
        if owner_override is not None and owner_override.quality_tier is not None:
            return owner_override.quality_tier
        if canonical_alias is not None:
            suffix = canonical_alias.rsplit(".", maxsplit=1)[-1]
            if suffix in {tier.value for tier in QualityTier}:
                return QualityTier(suffix)
        if requested_tier is not None:
            return QualityTier(requested_tier)
        return default_tier

    async def _execute_with_resource_lease(
        self,
        adapter: ProviderAdapter,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        execution_allowed: Callable[[], bool | Awaitable[bool]] | None,
    ) -> ProviderResult:
        async with self._resource_lease(
            invocation.provider_id,
            invocation.model_alias,
            invocation.resources,
        ):
            await require_execution_allowed(execution_allowed)
            tracked_execution_allowed = _TrackedExecutionAuthorization(execution_allowed)
            result = await adapter.execute(
                request,
                invocation,
                execution_allowed=tracked_execution_allowed,
            )
            if tracked_execution_allowed.successful_calls < 1 or tracked_execution_allowed.failed:
                raise ProviderContractError("adapter did not evaluate execution_allowed")
            return result

    @asynccontextmanager
    async def _resource_lease(
        self,
        provider_id: str,
        model_alias: str | None,
        resources: ResourceProfile,
    ) -> AsyncIterator[None]:
        slot_key = (provider_id, model_alias or "no-model", resources.max_concurrency)
        slots = self._resource_slots.setdefault(slot_key, asyncio.Semaphore(resources.max_concurrency))
        async with slots:
            gpu_key = resources.gpu_lease_key
            if gpu_key is None:
                yield
                return
            gpu_lock = self._gpu_leases.setdefault(gpu_key, asyncio.Lock())
            async with gpu_lock:
                yield

    async def close(self) -> None:
        adapters = tuple(self._adapters.values())
        self._adapters.clear()
        self._health.clear()
        self._resource_slots.clear()
        self._gpu_leases.clear()
        if adapters:
            await asyncio.gather(*(adapter.close() for adapter in adapters), return_exceptions=True)

    async def _append_audit(self, record: AuditRecord, *, outcome_uncertain: bool) -> None:
        if self._audit_sink is None:
            raise AuditWriteError("audit sink is not configured", outcome_uncertain=outcome_uncertain)
        try:
            await self._audit_sink.append(record)
        except Exception as exc:
            raise AuditWriteError("provider audit write failed", outcome_uncertain=outcome_uncertain) from exc

    async def _append_terminal_audit(
        self,
        record: AuditRecord,
        *,
        write_failure_outcome_uncertain: bool,
    ) -> None:
        task = asyncio.create_task(
            self._append_audit(
                record,
                outcome_uncertain=write_failure_outcome_uncertain,
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            try:
                task.result()
            except (AuditWriteError, asyncio.CancelledError):
                pass
            raise

    @staticmethod
    def _validate_result(
        request: ProviderRequest,
        invocation: ProviderInvocation,
        result: ProviderResult,
    ) -> None:
        if not isinstance(result, ProviderResult):
            raise ProviderContractError("provider returned an invalid result type")
        if result.request_id != request.request_id:
            raise ProviderContractError("provider result request_id mismatch")
        if result.provider_id != invocation.provider_id:
            raise ProviderContractError("provider result provider_id mismatch")
        if result.provider_model != invocation.provider_model:
            raise ProviderContractError("provider result model mismatch")

    @staticmethod
    def _terminal_audit(
        request: ProviderRequest,
        resolution: ProviderResolution,
        outcome: AuditOutcome,
        *,
        duration_ms: int,
        artifact_ids: tuple[str, ...] = (),
        failure_code: str | None = None,
        outcome_uncertain: bool = False,
    ) -> AuditRecord:
        return AuditRecord(
            request_id=request.request_id,
            trace_id=request.trace_id,
            actor_ref=request.actor_ref,
            capability=request.capability,
            outcome=outcome,
            occurred_at=utc_now(),
            quality_tier=resolution.quality_tier,
            provider_id=resolution.provider_id,
            model_alias=resolution.model_alias,
            duration_ms=duration_ms,
            artifact_ids=artifact_ids,
            failure_code=failure_code,
            outcome_uncertain=outcome_uncertain,
        )
