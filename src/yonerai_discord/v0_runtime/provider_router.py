"""論理 model/provider preference を canonical registry へ解決する最小 runtime。

provider adapter の呼出し、health refresh、Discord I/O は行わない。呼出し元は
readiness snapshot と privacy/consent 判定を渡し、この module は不足情報を許可と
みなさない。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import sqlite3
from typing import Mapping

from yonerai_discord.provider_registry import (
    DEFAULT_CATALOG,
    HealthStatus,
    LogicalCapability,
    ProviderCatalogManifest,
    ProviderKind,
)
from yonerai_discord.provider_registry.domain import normalize_identifier
from yonerai_discord.v0_contracts import Scope


class PreferenceLevel(StrEnum):
    USER = "user"
    CONVERSATION = "conversation"


class PreferenceReason(StrEnum):
    READY = "ready"
    AUTO_PREFERENCE = "auto_preference"
    DEFAULT_PREFERENCE = "default_preference"
    EXISTING_DEFAULT_PATH = "existing_default_path"
    USER_PREFERENCE = "user_preference"
    CONVERSATION_PREFERENCE = "conversation_preference"
    MODEL_ALIAS_INVALID = "model_alias_invalid"
    PROVIDER_NOT_IN_CATALOG = "provider_not_in_catalog"
    ROUTE_UNCONFIGURED = "route_unconfigured"
    MODEL_ALIAS_UNCONFIGURED = "model_alias_unconfigured"
    PRIVACY_DENIED = "privacy_denied"
    CONSENT_REQUIRED = "consent_required"
    ADAPTER_MISSING = "adapter_missing"
    HEALTH_UNKNOWN = "health_unknown"
    PROVIDER_UNHEALTHY = "provider_unhealthy"
    TASK_REQUIRED_MODEL = "task_required_model"


@dataclass(frozen=True, slots=True)
class ConversationKey:
    """model/provider に依存しない会話識別子。

    preference を切り替えても同じ Scope から常に同じ key が得られるため、会話本文や
    durable memory の key と provider の実装名を混在させない。
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 256:
            raise ValueError("conversation key must contain 1 to 256 characters")
        object.__setattr__(self, "value", self.value.strip())

    @classmethod
    def from_scope(cls, scope: Scope) -> "ConversationKey":
        if not isinstance(scope, Scope):
            raise TypeError("scope must be a Scope")
        if scope.dm_channel_id is not None:
            return cls(f"dm:{scope.dm_channel_id}:user:{scope.user_id}:visibility:{scope.visibility.value}")
        return cls(
            "guild:"
            f"{scope.guild_id}:channel:{scope.channel_id or 0}:user:{scope.user_id}:visibility:{scope.visibility.value}"
        )


@dataclass(frozen=True, slots=True)
class ProviderPreference:
    level: PreferenceLevel
    scope: Scope
    model_alias: str | None
    provider_id: str | None = None
    conversation_key: ConversationKey | None = None

    def __post_init__(self) -> None:
        level = PreferenceLevel(self.level)
        if not isinstance(self.scope, Scope):
            raise TypeError("scope must be a Scope")
        alias = None if self.model_alias is None else normalize_identifier(self.model_alias, label="model_alias")
        provider_id = None if self.provider_id is None else normalize_identifier(self.provider_id, label="provider_id")
        if level is PreferenceLevel.CONVERSATION:
            key = self.conversation_key or ConversationKey.from_scope(self.scope)
            if key != ConversationKey.from_scope(self.scope):
                raise ValueError("conversation preference key must match its scope")
        elif self.conversation_key is not None:
            raise ValueError("user preference must not declare a conversation key")
        else:
            key = None
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "model_alias", alias)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "conversation_key", key)


@dataclass(frozen=True, slots=True)
class ProviderReadiness:
    """adapter registry が作る非秘密 snapshot。未登録・未確認は既定で拒否する。"""

    adapter_registered: bool = False
    health: HealthStatus = HealthStatus.UNKNOWN

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_registered, bool):
            raise TypeError("adapter_registered must be a boolean")
        object.__setattr__(self, "health", HealthStatus(self.health))


@dataclass(frozen=True, slots=True)
class ExistingDefaultRoute:
    """既存 AI runtime がすでに許可した default dispatch の非秘密な表示値。

    catalog 未設定時の互換経路だけに使う。これは preference の fallback ではなく、
    caller が既存 policy/consent を通過した後に明示して渡す値である。
    """

    model_alias: str
    provider_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_alias", normalize_identifier(self.model_alias, label="model_alias"))
        object.__setattr__(self, "provider_id", normalize_identifier(self.provider_id, label="provider_id"))


@dataclass(frozen=True, slots=True)
class ProviderRouteRequest:
    scope: Scope
    capability: LogicalCapability
    readiness: Mapping[str, ProviderReadiness]
    conversation_key: ConversationKey | None = None
    existing_default: ExistingDefaultRoute | None = None
    privacy_allowed: bool = False
    consent_verified: bool = False
    required_model_alias: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            raise TypeError("scope must be a Scope")
        if not isinstance(self.privacy_allowed, bool) or not isinstance(self.consent_verified, bool):
            raise TypeError("privacy_allowed and consent_verified must be booleans")
        object.__setattr__(self, "capability", LogicalCapability(self.capability))
        key = self.conversation_key or ConversationKey.from_scope(self.scope)
        if key != ConversationKey.from_scope(self.scope):
            raise ValueError("conversation key must match its scope")
        normalized: dict[str, ProviderReadiness] = {}
        for provider_id, item in self.readiness.items():
            normalized[normalize_identifier(provider_id, label="provider_id")] = (
                item if isinstance(item, ProviderReadiness) else ProviderReadiness(**item)
            )
        object.__setattr__(self, "conversation_key", key)
        object.__setattr__(self, "readiness", normalized)
        if self.existing_default is not None and not isinstance(self.existing_default, ExistingDefaultRoute):
            raise TypeError("existing_default must be an ExistingDefaultRoute or None")
        if self.required_model_alias is not None:
            object.__setattr__(
                self,
                "required_model_alias",
                normalize_identifier(self.required_model_alias, label="required_model_alias"),
            )


@dataclass(frozen=True, slots=True)
class ProviderRouteResolution:
    conversation_key: ConversationKey
    preferred_model_alias: str | None
    preferred_provider_id: str | None
    preference_level: PreferenceLevel | None
    effective_model_alias: str | None
    effective_provider_id: str | None
    reasons: tuple[PreferenceReason, ...]

    @property
    def ready(self) -> bool:
        return self.effective_provider_id is not None and PreferenceReason.READY in self.reasons


class ProviderPreferenceRepository:
    """親所有migrationにより作成済みのtableだけを使うrepository。"""

    _TABLE = "v0_provider_preferences"

    def __init__(self, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        self._connection = connection
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (self._TABLE,)
        ).fetchone()
        if row is None:
            raise RuntimeError("v0 provider preference migration is not applied")

    def save(self, preference: ProviderPreference) -> None:
        key = self._storage_key(preference.level, preference.scope, preference.conversation_key)
        self._connection.execute(
            f"""
            INSERT INTO {self._TABLE}
                (preference_key, level, guild_id, user_id, conversation_key, scope_json, model_alias, provider_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(preference_key) DO UPDATE SET
                model_alias = excluded.model_alias,
                provider_id = excluded.provider_id,
                scope_json = excluded.scope_json
            """,
            (
                key,
                preference.level.value,
                preference.scope.guild_id,
                preference.scope.user_id,
                None if preference.conversation_key is None else preference.conversation_key.value,
                self._scope_json(preference.scope),
                preference.model_alias or "auto",
                preference.provider_id,
            ),
        )
        self._connection.commit()

    def get_user(self, scope: Scope) -> ProviderPreference | None:
        return self._get(PreferenceLevel.USER, scope, None)

    def get_conversation(
        self, scope: Scope, conversation_key: ConversationKey | None = None
    ) -> ProviderPreference | None:
        key = conversation_key or ConversationKey.from_scope(scope)
        if key != ConversationKey.from_scope(scope):
            raise ValueError("conversation key must match its scope")
        return self._get(PreferenceLevel.CONVERSATION, scope, key)

    def delete(self, level: PreferenceLevel, scope: Scope, conversation_key: ConversationKey | None = None) -> bool:
        level = PreferenceLevel(level)
        if level is PreferenceLevel.CONVERSATION:
            key = conversation_key or ConversationKey.from_scope(scope)
            if key != ConversationKey.from_scope(scope):
                raise ValueError("conversation key must match its scope")
        else:
            if conversation_key is not None:
                raise ValueError("user preference must not declare a conversation key")
            key = None
        cursor = self._connection.execute(
            f"DELETE FROM {self._TABLE} WHERE preference_key = ?",
            (self._storage_key(level, scope, key),),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def _get(
        self, level: PreferenceLevel, scope: Scope, conversation_key: ConversationKey | None
    ) -> ProviderPreference | None:
        row = self._connection.execute(
            f"SELECT model_alias, provider_id FROM {self._TABLE} WHERE preference_key = ?",
            (self._storage_key(level, scope, conversation_key),),
        ).fetchone()
        if row is None:
            return None
        return ProviderPreference(level, scope, None if row[0] == "auto" else row[0], row[1], conversation_key)

    @staticmethod
    def _storage_key(level: PreferenceLevel, scope: Scope, conversation_key: ConversationKey | None) -> str:
        if level is PreferenceLevel.CONVERSATION:
            if conversation_key is None:
                raise ValueError("conversation preference requires a conversation key")
            return f"conversation:{conversation_key.value}"
        return f"user:guild:{scope.guild_id or 0}:user:{scope.user_id}"

    @staticmethod
    def _scope_json(scope: Scope) -> str:
        # JSON libraryを使わず、値は Scope の整数/enum検証済み値のみを保存する。
        return (
            f"guild={scope.guild_id};user={scope.user_id};channel={scope.channel_id};dm={scope.dm_channel_id};"
            f"visibility={scope.visibility.value};residency={scope.residency.value}"
        )


class ProviderPreferenceRouter:
    """preference を canonical catalog と supplied readiness から fail-closed 解決する。"""

    def __init__(
        self, repository: ProviderPreferenceRepository, catalog: ProviderCatalogManifest = DEFAULT_CATALOG
    ) -> None:
        if not isinstance(repository, ProviderPreferenceRepository):
            raise TypeError("repository must be a ProviderPreferenceRepository")
        if not isinstance(catalog, ProviderCatalogManifest):
            raise TypeError("catalog must be a ProviderCatalogManifest")
        self._repository = repository
        self._catalog = catalog

    @property
    def catalog(self) -> ProviderCatalogManifest:
        return self._catalog

    def route(self, request: ProviderRouteRequest) -> ProviderRouteResolution:
        task_required = request.required_model_alias is not None
        if task_required:
            preference = ProviderPreference(
                PreferenceLevel.USER,
                request.scope,
                request.required_model_alias,
            )
            source_reason = PreferenceReason.TASK_REQUIRED_MODEL
        else:
            preference = self._repository.get_conversation(request.scope, request.conversation_key)
            source_reason = PreferenceReason.CONVERSATION_PREFERENCE
            if preference is None:
                preference = self._repository.get_user(request.scope)
                source_reason = PreferenceReason.USER_PREFERENCE
        if preference is None:
            # auto は既存の許可済み dispatch を表示可能にするだけであり、explicit
            # preference の不成立時にこの経路へ落とすことは絶対にしない。
            if request.existing_default is not None:
                return ProviderRouteResolution(
                    request.conversation_key,
                    None,
                    None,
                    None,
                    request.existing_default.model_alias,
                    request.existing_default.provider_id,
                    (
                        PreferenceReason.AUTO_PREFERENCE,
                        PreferenceReason.EXISTING_DEFAULT_PATH,
                        PreferenceReason.READY,
                    ),
                )
            default_alias = self._default_alias(request.capability)
            if default_alias is None:
                return self._failure(
                    request,
                    None,
                    None,
                    None,
                    (PreferenceReason.AUTO_PREFERENCE, PreferenceReason.ROUTE_UNCONFIGURED),
                )
            preference = ProviderPreference(PreferenceLevel.USER, request.scope, default_alias)
            source_reason = PreferenceReason.DEFAULT_PREFERENCE

        reported_level = None if task_required else preference.level
        if preference.model_alias is None:
            alias = self._default_alias(request.capability)
            if alias is None:
                return self._failure(
                    request,
                    None,
                    preference.provider_id,
                    reported_level,
                    (source_reason, PreferenceReason.ROUTE_UNCONFIGURED),
                )
        else:
            try:
                alias = self._catalog.canonical_model_alias(preference.model_alias)
            except (TypeError, ValueError):
                return self._failure(
                    request, None, preference.provider_id, reported_level, (PreferenceReason.MODEL_ALIAS_INVALID,)
                )

        if preference.provider_id is not None and self._catalog.provider(preference.provider_id) is None:
            return self._failure(
                request,
                alias,
                preference.provider_id,
                reported_level,
                (source_reason, PreferenceReason.PROVIDER_NOT_IN_CATALOG),
            )
        candidates = self._candidates(request.capability, alias, preference.provider_id)
        if not candidates:
            return self._failure(
                request,
                alias,
                preference.provider_id,
                reported_level,
                (source_reason, PreferenceReason.ROUTE_UNCONFIGURED),
            )

        failures: list[PreferenceReason] = [source_reason]
        for provider_id in candidates:
            reason = self._provider_reason(request, provider_id, alias)
            if reason is None:
                return ProviderRouteResolution(
                    request.conversation_key,
                    alias,
                    preference.provider_id,
                    reported_level,
                    alias,
                    provider_id,
                    tuple(failures + [PreferenceReason.READY]),
                )
            if reason not in failures:
                failures.append(reason)
        return self._failure(request, alias, preference.provider_id, reported_level, tuple(failures))

    def _default_alias(self, capability: LogicalCapability) -> str | None:
        route = self._catalog.route(capability)
        if route is None:
            return None
        tier = next((item for item in route.tiers if item.tier is route.default_tier), None)
        return None if tier is None or tier.model_alias is None else tier.model_alias

    def _candidates(self, capability: LogicalCapability, alias: str, provider_id: str | None) -> tuple[str, ...]:
        route = self._catalog.route(capability)
        if route is None:
            return ()
        if provider_id is not None:
            return (provider_id,)
        for tier in route.tiers:
            if tier.model_alias is not None and self._catalog.canonical_model_alias(tier.model_alias) == alias:
                return tier.provider_ids
        return ()

    def _provider_reason(self, request: ProviderRouteRequest, provider_id: str, alias: str) -> PreferenceReason | None:
        provider = self._catalog.provider(provider_id)
        if provider is None:
            return PreferenceReason.PROVIDER_NOT_IN_CATALOG
        if request.capability not in provider.capabilities:
            return PreferenceReason.ROUTE_UNCONFIGURED
        if not any(self._catalog.canonical_model_alias(item.alias) == alias for item in provider.models):
            return PreferenceReason.MODEL_ALIAS_UNCONFIGURED
        if not request.privacy_allowed:
            return PreferenceReason.PRIVACY_DENIED
        policy = self._catalog.capability_policy(request.capability)
        if (
            provider.kind is ProviderKind.API
            and policy is not None
            and policy.requires_consent
            and not request.consent_verified
        ):
            return PreferenceReason.CONSENT_REQUIRED
        readiness = request.readiness.get(provider_id, ProviderReadiness())
        if not readiness.adapter_registered:
            return PreferenceReason.ADAPTER_MISSING
        if readiness.health is HealthStatus.UNKNOWN:
            return PreferenceReason.HEALTH_UNKNOWN
        if readiness.health not in {HealthStatus.READY, HealthStatus.DEGRADED}:
            return PreferenceReason.PROVIDER_UNHEALTHY
        return None

    @staticmethod
    def _failure(
        request: ProviderRouteRequest,
        alias: str | None,
        provider_id: str | None,
        level: PreferenceLevel | None,
        reasons: tuple[PreferenceReason, ...],
    ) -> ProviderRouteResolution:
        return ProviderRouteResolution(request.conversation_key, alias, provider_id, level, None, None, reasons)
