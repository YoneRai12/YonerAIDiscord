"""Preference-aware AI dispatch without a second provider registry.

The canonical provider catalog and the existing v0 preference router remain the
route authorities.  This adapter only binds their resolved provider ID to the
runtime ``AIProvider`` object that ``AIService`` invokes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import replace

from yonerai_discord.provider_registry import (
    DEFAULT_CATALOG,
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
from yonerai_discord.provider_registry.manifest import CapabilityRoute, TierRoute
from yonerai_discord.v0_contracts import MemoryVisibility, Scope
from yonerai_discord.v0_runtime.provider_router import (
    ExistingDefaultRoute,
    PreferenceReason,
    ProviderPreferenceRouter,
    ProviderReadiness,
    ProviderRouteRequest,
    ProviderRouteResolution,
)

from .models import AIRequest, DataBoundary
from .ports import AIProvider
from .service import AIProviderSelection, ProviderSelectionError


class ProviderReadinessTracker:
    """成功した既定経路を、型付きの到達性証拠へ昇格する小さな状態器。"""

    def __init__(self, provider_ids: tuple[str, ...]) -> None:
        if not provider_ids or any(not isinstance(item, str) or not item.strip() for item in provider_ids):
            raise ValueError("provider_ids must contain configured provider IDs")
        self._values = {provider_id: ProviderReadiness(True, HealthStatus.UNKNOWN) for provider_id in provider_ids}

    def snapshot(self) -> Mapping[str, ProviderReadiness]:
        return dict(self._values)

    def record_success(self, provider_id: str) -> None:
        if provider_id in self._values:
            self._values[provider_id] = ProviderReadiness(True, HealthStatus.READY)

    def record_failure(self, provider_id: str) -> None:
        if provider_id in self._values:
            self._values[provider_id] = ProviderReadiness(True, HealthStatus.UNAVAILABLE)


class PreferenceAwareAIProviderSelector:
    """Resolve the stored preference and bind it to one configured adapter.

    Auto mode is the sole compatibility path: it keeps the already configured
    default provider and leaves model choice to the existing TaskProfile
    router.  Once a user stores an explicit model or provider, every missing
    catalog/readiness/consent fact fails closed and never falls back to auto.
    """

    def __init__(
        self,
        router: ProviderPreferenceRouter,
        *,
        providers: Mapping[str, AIProvider],
        readiness: Callable[[], Mapping[str, ProviderReadiness]],
        readiness_updates: ProviderReadinessTracker | None = None,
        default_provider_id: str | None,
        default_model_alias: str = "ai.auto",
    ) -> None:
        if not isinstance(router, ProviderPreferenceRouter):
            raise TypeError("router must be a ProviderPreferenceRouter")
        if not callable(readiness):
            raise TypeError("readiness must be callable")
        copied = dict(providers)
        if any(not isinstance(key, str) or not key.strip() for key in copied):
            raise ValueError("provider IDs must be non-empty strings")
        if default_provider_id is not None and default_provider_id not in copied:
            raise ValueError("default provider must be present in providers")
        self._router = router
        self._providers = copied
        self._readiness = readiness
        self._readiness_updates = readiness_updates
        self._default_provider_id = default_provider_id
        self._default_model_alias = default_model_alias

    @property
    def available(self) -> bool:
        return self._default_provider_id is not None

    @property
    def catalog_revision(self) -> str:
        return self._router.catalog.content_revision

    def route(self, request: AIRequest) -> ProviderRouteResolution:
        scope = _request_scope(request)
        consent_verified = request.boundary is DataBoundary.REMOTE_OPT_IN
        privacy_allowed = request.provider_input is not None
        return self.resolve_scope(
            scope,
            privacy_allowed=privacy_allowed,
            consent_verified=consent_verified,
            required_model_alias=request.required_model_alias,
        )

    def resolve_scope(
        self,
        scope: Scope,
        *,
        privacy_allowed: bool,
        consent_verified: bool,
        required_model_alias: str | None = None,
    ) -> ProviderRouteResolution:
        existing_default = self._existing_default(
            privacy_allowed=privacy_allowed,
            consent_verified=consent_verified,
        )
        return self._router.route(
            ProviderRouteRequest(
                scope,
                LogicalCapability.AI_TEXT,
                dict(self._readiness()),
                existing_default=existing_default,
                privacy_allowed=privacy_allowed,
                consent_verified=consent_verified,
                required_model_alias=required_model_alias,
            )
        )

    def select(self, request: AIRequest) -> AIProviderSelection:
        resolution = self.route(request)
        if not resolution.ready or resolution.effective_provider_id is None:
            reason = resolution.reasons[-1].value if resolution.reasons else "route_unconfigured"
            raise ProviderSelectionError(reason)
        provider = self._providers.get(resolution.effective_provider_id)
        if provider is None:
            raise ProviderSelectionError(PreferenceReason.ADAPTER_MISSING.value)
        if request.required_model_id is not None:
            manifest = self._router.catalog.provider(resolution.effective_provider_id)
            binding = (
                next(
                    (
                        item
                        for item in manifest.models
                        if self._router.catalog.canonical_model_alias(item.alias) == resolution.effective_model_alias
                    ),
                    None,
                )
                if manifest is not None
                else None
            )
            if binding is None or binding.provider_model != request.required_model_id:
                raise ProviderSelectionError("required_model_unavailable")
        auto = PreferenceReason.AUTO_PREFERENCE in resolution.reasons
        model_alias = None if auto else resolution.effective_model_alias
        reason = (
            PreferenceReason.EXISTING_DEFAULT_PATH.value
            if PreferenceReason.EXISTING_DEFAULT_PATH in resolution.reasons
            else resolution.reasons[-1].value
        )
        manifest = self._router.catalog.provider(resolution.effective_provider_id)
        model_bindings = {item.alias: item.provider_model for item in manifest.models} if manifest is not None else {}
        token_payload = {
            "catalog_revision": self.catalog_revision,
            "conversation_key": resolution.conversation_key.value,
            "effective_model_alias": resolution.effective_model_alias,
            "effective_provider_id": resolution.effective_provider_id,
            "model_bindings": model_bindings,
            "preferred_model_alias": resolution.preferred_model_alias,
            "preferred_provider_id": resolution.preferred_provider_id,
            "reasons": [item.value for item in resolution.reasons],
        }
        token = hashlib.sha256(
            json.dumps(
                token_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return AIProviderSelection(
            provider_id=resolution.effective_provider_id,
            provider=provider,
            model_alias=model_alias,
            reason=reason,
            token=token,
        )

    def record_success(self, provider_id: str) -> None:
        if self._readiness_updates is not None:
            self._readiness_updates.record_success(provider_id)

    def record_failure(self, provider_id: str) -> None:
        if self._readiness_updates is not None:
            self._readiness_updates.record_failure(provider_id)

    def _existing_default(
        self,
        *,
        privacy_allowed: bool,
        consent_verified: bool,
    ) -> ExistingDefaultRoute | None:
        provider_id = self._default_provider_id
        if provider_id is None or not privacy_allowed:
            return None
        provider = self._providers[provider_id]
        if not provider.is_local and not consent_verified:
            return None
        return ExistingDefaultRoute(self._default_model_alias, provider_id)


def _request_scope(request: AIRequest) -> Scope:
    if request.guild_id is None:
        if request.channel_id is None:
            raise ProviderSelectionError("dm_channel_missing")
        return Scope(
            None,
            request.user_id,
            dm_channel_id=request.channel_id,
            visibility=MemoryVisibility.DIRECT_MESSAGE,
        )
    return Scope(
        request.guild_id,
        request.user_id,
        channel_id=request.channel_id,
        visibility=MemoryVisibility.USER_PRIVATE,
    )


def build_runtime_ai_catalog(
    *,
    provider_id: str,
    is_local: bool,
    model_bindings: Mapping[str, str],
    supports_web_search: bool = False,
    supports_attachments: bool = False,
) -> ProviderCatalogManifest:
    """Bind the configured adapter into the canonical manifest schema.

    This is a composition-time catalog instance, not another registry.  Every
    non-AI capability, policy, compatibility alias and existing provider stays
    exactly as declared by ``DEFAULT_CATALOG``.
    """

    required_aliases = ("ai.fast", "ai.balanced", "ai.quality")
    if set(model_bindings) != set(required_aliases):
        raise ValueError("AI runtime model bindings must define fast, balanced and quality aliases")
    resources = ResourceProfile(
        ResourceTarget.CPU if is_local else ResourceTarget.REMOTE,
        1,
        load_policy=ModelLoadPolicy.PROVIDER_MANAGED,
        offload_policy=OffloadPolicy.DISABLED,
        exclusive_gpu_lease=False,
    )
    provider_capabilities = [LogicalCapability.AI_TEXT]
    if supports_web_search:
        provider_capabilities.append(LogicalCapability.WEB_SEARCH_PAID)
    if supports_attachments:
        provider_capabilities.append(LogicalCapability.VISION_UNDERSTANDING)
    provider_models = [ModelBinding(alias, model_bindings[alias], probe_required=False) for alias in required_aliases]
    if supports_web_search:
        provider_models.extend(
            ModelBinding(
                f"web.{tier}",
                model_bindings[f"ai.{tier if tier != 'balanced' else 'balanced'}"],
                probe_required=False,
            )
            for tier in ("fast", "balanced", "quality")
        )
    if supports_attachments:
        provider_models.extend(
            ModelBinding(
                f"vision.{tier}",
                model_bindings[f"ai.{tier}"],
                probe_required=False,
            )
            for tier in ("fast", "balanced", "quality")
        )
    runtime_provider = ProviderManifest(
        provider_id,
        ProviderKind.LOCAL if is_local else ProviderKind.API,
        "adapter.ai.openai-compatible",
        tuple(provider_capabilities),
        resources,
        enabled=True,
        models=tuple(provider_models),
    )
    providers = tuple(item for item in DEFAULT_CATALOG.providers if item.provider_id != provider_id) + (
        runtime_provider,
    )
    routes = tuple(
        (
            CapabilityRoute(
                route.capability,
                tuple(TierRoute(tier.tier, (provider_id,), tier.model_alias) for tier in route.tiers),
                route.default_tier,
            )
            if route.capability is LogicalCapability.AI_TEXT
            or (supports_web_search and route.capability is LogicalCapability.WEB_SEARCH_PAID)
            or (supports_attachments and route.capability is LogicalCapability.VISION_UNDERSTANDING)
            else route
        )
        for route in DEFAULT_CATALOG.routes
    )
    capabilities = tuple(
        replace(policy, default_enabled=True)
        if supports_attachments and policy.capability is LogicalCapability.VISION_UNDERSTANDING
        else policy
        for policy in DEFAULT_CATALOG.capabilities
    )
    return ProviderCatalogManifest(
        DEFAULT_CATALOG.schema_version,
        DEFAULT_CATALOG.module_id,
        capabilities,
        providers,
        routes,
        DEFAULT_CATALOG.compatibility_aliases,
    )


__all__ = [
    "PreferenceAwareAIProviderSelector",
    "ProviderReadinessTracker",
    "build_runtime_ai_catalog",
]
