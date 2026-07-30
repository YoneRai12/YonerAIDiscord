import sqlite3

from yonerai_discord.provider_registry import (
    HealthStatus,
    LogicalCapability,
    ModelBinding,
    ProviderCatalogManifest,
    ProviderKind,
    ProviderManifest,
    ResourceProfile,
    ResourceTarget,
    ModelLoadPolicy,
    OffloadPolicy,
)
from yonerai_discord.provider_registry.manifest import AliasBinding, CapabilityPolicy, CapabilityRoute, TierRoute
from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.ai.state_repository import migrate_v0_provider_preferences
from yonerai_discord.v0_contracts import Scope
from yonerai_discord.v0_runtime import (
    ConversationKey,
    PreferenceLevel,
    PreferenceReason,
    ProviderPreference,
    ProviderPreferenceRepository,
    ProviderPreferenceRouter,
    ProviderReadiness,
    ProviderRouteRequest,
)
from yonerai_discord.v0_runtime.provider_router import ExistingDefaultRoute


def _catalog() -> ProviderCatalogManifest:
    resources = ResourceProfile(
        ResourceTarget.REMOTE,
        1,
        load_policy=ModelLoadPolicy.PROVIDER_MANAGED,
        offload_policy=OffloadPolicy.DISABLED,
        exclusive_gpu_lease=False,
    )
    provider = ProviderManifest(
        "provider.remote",
        ProviderKind.API,
        "test.adapter",
        (LogicalCapability.AI_TEXT,),
        resources,
        models=(ModelBinding("ai.balanced", "remote-model", probe_required=False),),
    )
    return ProviderCatalogManifest(
        1,
        "test.provider-catalog",
        (
            CapabilityPolicy(
                LogicalCapability.AI_TEXT, True, RbacLevel.EVERYONE, RiskLevel.MEDIUM, requires_consent=True
            ),
        ),
        (provider,),
        (
            CapabilityRoute(
                LogicalCapability.AI_TEXT,
                (
                    TierRoute("fast", (), "ai.fast"),
                    TierRoute("balanced", ("provider.remote",), "ai.balanced"),
                    TierRoute("quality", (), "ai.quality"),
                ),
            ),
        ),
        (AliasBinding("ai.standard", "ai.balanced"),),
    )


def _router() -> tuple[ProviderPreferenceRepository, ProviderPreferenceRouter]:
    connection = sqlite3.connect(":memory:")
    migrate_v0_provider_preferences(connection)
    repository = ProviderPreferenceRepository(connection)
    return repository, ProviderPreferenceRouter(repository, _catalog())


def _request(scope: Scope, **kwargs: object) -> ProviderRouteRequest:
    defaults: dict[str, object] = {
        "capability": LogicalCapability.AI_TEXT,
        "readiness": {"provider.remote": ProviderReadiness(True, HealthStatus.READY)},
        "privacy_allowed": True,
        "consent_verified": True,
    }
    defaults.update(kwargs)
    return ProviderRouteRequest(scope, **defaults)  # type: ignore[arg-type]


def test_conversation_key_is_neutral_when_model_or_provider_preference_changes() -> None:
    scope = Scope(1, 10, channel_id=20)
    key = ConversationKey.from_scope(scope)
    before = ProviderPreference(PreferenceLevel.CONVERSATION, scope, "ai.balanced", "provider.remote", key)
    after = ProviderPreference(PreferenceLevel.CONVERSATION, scope, "ai.balanced", None, key)

    assert before.conversation_key == after.conversation_key == key
    assert "provider" not in key.value and "ai.balanced" not in key.value


def test_conversation_preference_overrides_user_and_alias_is_canonicalized() -> None:
    repository, router = _router()
    scope = Scope(1, 10, channel_id=20)
    repository.save(ProviderPreference(PreferenceLevel.USER, scope, "ai.balanced"))
    repository.save(ProviderPreference(PreferenceLevel.CONVERSATION, scope, "ai.standard"))

    result = router.route(_request(scope))

    assert result.preferred_model_alias == result.effective_model_alias == "ai.balanced"
    assert result.preference_level is PreferenceLevel.CONVERSATION
    assert result.reasons == (PreferenceReason.CONVERSATION_PREFERENCE, PreferenceReason.READY)


def test_preferred_state_is_preserved_but_consent_privacy_adapter_and_health_fail_closed() -> None:
    repository, router = _router()
    scope = Scope(1, 10)
    repository.save(ProviderPreference(PreferenceLevel.USER, scope, "ai.balanced", "provider.remote"))

    cases = (
        ({"privacy_allowed": False}, PreferenceReason.PRIVACY_DENIED),
        ({"consent_verified": False}, PreferenceReason.CONSENT_REQUIRED),
        ({"readiness": {}}, PreferenceReason.ADAPTER_MISSING),
        (
            {"readiness": {"provider.remote": ProviderReadiness(True, HealthStatus.UNKNOWN)}},
            PreferenceReason.HEALTH_UNKNOWN,
        ),
    )
    for overrides, expected in cases:
        result = router.route(_request(scope, **overrides))
        assert result.preferred_model_alias == "ai.balanced"
        assert result.preferred_provider_id == "provider.remote"
        assert result.effective_provider_id is None
        assert expected in result.reasons
        assert result.ready is False


def test_repository_isolated_by_user_and_conversation_and_reopens_schema() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_v0_provider_preferences(connection)
    first = ProviderPreferenceRepository(connection)
    scope = Scope(1, 10, channel_id=20)
    other = Scope(1, 11, channel_id=20)
    first.save(ProviderPreference(PreferenceLevel.USER, scope, "ai.balanced"))
    first.save(ProviderPreference(PreferenceLevel.CONVERSATION, scope, "ai.balanced"))
    reopened = ProviderPreferenceRepository(connection)

    assert reopened.get_user(other) is None
    assert reopened.get_conversation(other) is None
    assert reopened.get_user(scope) is not None
    assert reopened.get_conversation(scope) is not None


def test_auto_keeps_explicit_existing_default_when_canonical_catalog_has_no_providers() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_v0_provider_preferences(connection)
    repository = ProviderPreferenceRepository(connection)
    router = ProviderPreferenceRouter(repository)
    scope = Scope(1, 10, channel_id=20)

    result = router.route(
        ProviderRouteRequest(
            scope,
            LogicalCapability.AI_TEXT,
            {},
            existing_default=ExistingDefaultRoute("legacy.ai.default", "legacy.openai-compatible"),
            privacy_allowed=True,
            consent_verified=True,
        )
    )

    assert result.preferred_model_alias is None
    assert result.preferred_provider_id is None
    assert (result.effective_model_alias, result.effective_provider_id) == (
        "legacy.ai.default",
        "legacy.openai-compatible",
    )
    assert result.reasons == (
        PreferenceReason.AUTO_PREFERENCE,
        PreferenceReason.EXISTING_DEFAULT_PATH,
        PreferenceReason.READY,
    )
    assert result.ready is True


def test_explicit_preference_never_falls_back_to_existing_default_when_catalog_is_empty() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_v0_provider_preferences(connection)
    repository = ProviderPreferenceRepository(connection)
    router = ProviderPreferenceRouter(repository)
    scope = Scope(1, 10, channel_id=20)
    repository.save(ProviderPreference(PreferenceLevel.USER, scope, "ai.balanced", "provider.remote"))

    result = router.route(
        ProviderRouteRequest(
            scope,
            LogicalCapability.AI_TEXT,
            {},
            existing_default=ExistingDefaultRoute("legacy.ai.default", "legacy.openai-compatible"),
            privacy_allowed=True,
            consent_verified=True,
        )
    )

    assert (result.preferred_model_alias, result.preferred_provider_id) == ("ai.balanced", "provider.remote")
    assert result.effective_model_alias is None
    assert result.effective_provider_id is None
    assert result.reasons == (PreferenceReason.USER_PREFERENCE, PreferenceReason.PROVIDER_NOT_IN_CATALOG)
    assert result.ready is False
