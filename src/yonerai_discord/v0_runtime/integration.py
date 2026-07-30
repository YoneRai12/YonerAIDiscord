"""v0 pure portsを既存SQLite/ConversationStoreへ接続するcomposition adapters。"""

from __future__ import annotations

from collections.abc import Callable

from yonerai_discord.modules.ai.conversation import ConversationStore
from yonerai_discord.modules.ai.provider_dispatch import PreferenceAwareAIProviderSelector
from yonerai_discord.provider_registry import LogicalCapability
from yonerai_discord.v0_contracts import MemoryVisibility, Scope

from .command_service import (
    CommandActor,
    CommandPreference,
    CommandScope,
    EffectiveRoute,
)
from .memory_repository import V0ExplicitMemoryRepository
from .provider_router import (
    ExistingDefaultRoute,
    PreferenceLevel,
    ProviderPreference,
    ProviderPreferenceRepository,
    ProviderPreferenceRouter,
    ProviderRouteRequest,
)


def command_scope_to_memory_scope(scope: CommandScope, user_id: int) -> Scope:
    if scope.guild_id is None:
        return Scope(
            None,
            user_id,
            dm_channel_id=scope.dm_channel_id,
            visibility=MemoryVisibility.DIRECT_MESSAGE,
        )
    return Scope(scope.guild_id, user_id, visibility=MemoryVisibility.USER_PRIVATE)


class SQLiteCommandPreferencePort:
    def __init__(self, repository: ProviderPreferenceRepository) -> None:
        self._repository = repository

    def get(self, scope: CommandScope, user_id: int) -> CommandPreference:
        stored = self._repository.get_user(command_scope_to_memory_scope(scope, user_id))
        return CommandPreference() if stored is None else CommandPreference(stored.model_alias, stored.provider_id)

    def set_model(self, scope: CommandScope, user_id: int, model_alias: str | None) -> None:
        resolved_scope = command_scope_to_memory_scope(scope, user_id)
        current = self._repository.get_user(resolved_scope)
        provider_id = None if current is None else current.provider_id
        self._save_or_delete(resolved_scope, model_alias, provider_id)

    def set_provider(self, scope: CommandScope, user_id: int, provider_id: str | None) -> None:
        resolved_scope = command_scope_to_memory_scope(scope, user_id)
        current = self._repository.get_user(resolved_scope)
        model_alias = None if current is None else current.model_alias
        self._save_or_delete(resolved_scope, model_alias, provider_id)

    def _save_or_delete(self, scope: Scope, model_alias: str | None, provider_id: str | None) -> None:
        if model_alias is None and provider_id is None:
            self._repository.delete(PreferenceLevel.USER, scope)
            return
        self._repository.save(ProviderPreference(PreferenceLevel.USER, scope, model_alias, provider_id))


class RouterAvailabilityPort:
    def __init__(
        self,
        router: ProviderPreferenceRouter,
        *,
        default_model_alias: str = "ai.auto",
        default_provider_id: str = "legacy.openai-compatible",
        existing_default_available: bool = True,
    ) -> None:
        self._router = router
        self._existing_default = (
            ExistingDefaultRoute(default_model_alias, default_provider_id) if existing_default_available else None
        )

    def resolve(self, scope: CommandScope, user_id: int, preference: CommandPreference) -> EffectiveRoute:
        memory_scope = command_scope_to_memory_scope(scope, user_id)
        resolution = self._router.route(
            ProviderRouteRequest(
                memory_scope,
                LogicalCapability.AI_TEXT,
                {},
                existing_default=self._existing_default,
                privacy_allowed=True,
                consent_verified=True,
            )
        )
        reason = resolution.reasons[-1].value
        return EffectiveRoute(
            resolution.effective_model_alias,
            resolution.effective_provider_id,
            resolution.ready,
            reason,
        )


class RuntimeRouterAvailabilityPort:
    """Expose the same runtime route decision that ``AIService`` will execute."""

    def __init__(
        self,
        selector: PreferenceAwareAIProviderSelector,
        *,
        consent_verified: Callable[[CommandScope, int], bool],
    ) -> None:
        if not isinstance(selector, PreferenceAwareAIProviderSelector):
            raise TypeError("selector must be a PreferenceAwareAIProviderSelector")
        if not callable(consent_verified):
            raise TypeError("consent_verified must be callable")
        self._selector = selector
        self._consent_verified = consent_verified

    def resolve(self, scope: CommandScope, user_id: int, preference: CommandPreference) -> EffectiveRoute:
        del preference
        memory_scope = command_scope_to_memory_scope(scope, user_id)
        try:
            consent = self._consent_verified(scope, user_id) is True
        except Exception:
            consent = False
        resolution = self._selector.resolve_scope(
            memory_scope,
            privacy_allowed=True,
            consent_verified=consent,
        )
        reason = resolution.reasons[-1].value if resolution.reasons else "route_unconfigured"
        return EffectiveRoute(
            resolution.effective_model_alias,
            resolution.effective_provider_id,
            resolution.ready,
            reason,
        )


class ConversationResetAdapter:
    def __init__(self, store: ConversationStore) -> None:
        self._store = store

    async def reset_conversation(self, scope: CommandScope, user_id: int) -> None:
        channel_id = scope.dm_channel_id if scope.guild_id is None else scope.channel_id
        if channel_id is None:
            return
        await self._store.drop(guild_id=scope.guild_id, channel_id=channel_id, user_id=user_id)


class ExplicitMemoryCommandAdapter:
    def __init__(self, repository: V0ExplicitMemoryRepository) -> None:
        self._repository = repository

    def remember(self, actor: CommandActor, text: str) -> str:
        return self._repository.remember(self._active_scope(actor), text).memory_id

    def list_metadata(self, actor: CommandActor) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "memory_id": record.memory_id,
                "visibility": record.scope.visibility.value,
                "created_at": record.created_at,
            }
            for record in self._repository.list(self._active_scope(actor))
        )

    def forget(self, actor: CommandActor, memory_id: str) -> bool:
        return self._repository.forget(self._active_scope(actor), memory_id)

    def clear(self, actor: CommandActor) -> int:
        return self._repository.clear_all_user_scopes(command_scope_to_memory_scope(actor.scope, actor.user_id))

    def privacy(self, actor: CommandActor, mode: str | None) -> str:
        if actor.scope.guild_id is None:
            if mode not in {None, "dm", "direct_message"}:
                raise ValueError("DM visibility is fixed")
            return MemoryVisibility.DIRECT_MESSAGE.value
        guild_id = actor.scope.guild_id
        if mode is not None:
            parsed = {
                "private": MemoryVisibility.USER_PRIVATE,
                "user_private": MemoryVisibility.USER_PRIVATE,
                "channel": MemoryVisibility.CHANNEL_SHARED,
                "channel_shared": MemoryVisibility.CHANNEL_SHARED,
                "guild": MemoryVisibility.GUILD_PUBLIC,
                "guild_public": MemoryVisibility.GUILD_PUBLIC,
            }.get(mode)
            if parsed is None:
                raise ValueError("unknown privacy mode")
            if parsed is not MemoryVisibility.USER_PRIVATE and not actor.can_share_memory:
                raise PermissionError("shared memory requires elevated permission")
            self._repository.set_privacy(
                guild_id=guild_id,
                user_id=actor.user_id,
                visibility=parsed,
                channel_id=actor.scope.channel_id,
            )
        return self._repository.privacy(guild_id=guild_id, user_id=actor.user_id)[0].value

    def preview_metadata(self, actor: CommandActor) -> dict[str, object]:
        scope = self._active_scope(actor)
        return {
            "count": len(self._repository.list(scope)),
            "visibility": scope.visibility.value,
        }

    def _active_scope(self, actor: CommandActor) -> Scope:
        scope = actor.scope
        if scope.guild_id is None:
            return command_scope_to_memory_scope(scope, actor.user_id)
        visibility, selected_channel = self._repository.privacy(guild_id=scope.guild_id, user_id=actor.user_id)
        if visibility is MemoryVisibility.USER_PRIVATE:
            return Scope(scope.guild_id, actor.user_id, visibility=visibility)
        if visibility is MemoryVisibility.GUILD_PUBLIC:
            return Scope(scope.guild_id, actor.user_id, visibility=visibility)
        if selected_channel != scope.channel_id:
            raise PermissionError("channel-shared memory is bound to a different channel")
        return Scope(
            scope.guild_id,
            actor.user_id,
            channel_id=scope.channel_id,
            visibility=MemoryVisibility.CHANNEL_SHARED,
        )


__all__ = [
    "ConversationResetAdapter",
    "ExplicitMemoryCommandAdapter",
    "RouterAvailabilityPort",
    "RuntimeRouterAvailabilityPort",
    "SQLiteCommandPreferencePort",
    "command_scope_to_memory_scope",
]
