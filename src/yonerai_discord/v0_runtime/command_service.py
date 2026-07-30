"""Discord I/O を持たない v0 typed command 境界。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Protocol

from yonerai_discord.provider_registry import DEFAULT_CATALOG, LogicalCapability, ProviderCatalogManifest
from yonerai_discord.provider_registry.domain import normalize_identifier


class AICommand(StrEnum):
    MODEL_LIST = "model_list"
    MODEL_SET = "model_set"
    MODEL_AUTO = "model_auto"
    PROVIDER_LIST = "provider_list"
    PROVIDER_SET = "provider_set"
    ROUTE = "route"
    RESET = "reset"


class MemoryCommand(StrEnum):
    REMEMBER = "remember"
    LIST = "list"
    FORGET = "forget"
    CLEAR = "clear"
    PRIVACY = "privacy"
    PREVIEW = "preview"


@dataclass(frozen=True, slots=True)
class CommandScope:
    """guild と DM を混同しない永続化スコープ。"""

    guild_id: int | None
    channel_id: int | None = None
    dm_channel_id: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("guild_id", self.guild_id),
            ("channel_id", self.channel_id),
            ("dm_channel_id", self.dm_channel_id),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive integer or None")
        if self.guild_id is None:
            if self.dm_channel_id is None or self.channel_id is not None:
                raise ValueError("DM scope requires only dm_channel_id")
        elif self.channel_id is None or self.dm_channel_id is not None:
            raise ValueError("guild scope requires channel_id and no dm_channel_id")


@dataclass(frozen=True, slots=True)
class CommandActor:
    user_id: int
    scope: CommandScope
    actor_authorized: bool
    can_share_memory: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int) or self.user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        if not isinstance(self.scope, CommandScope):
            raise TypeError("scope must be a CommandScope")
        if not isinstance(self.actor_authorized, bool):
            raise TypeError("actor_authorized must be a boolean")
        if not isinstance(self.can_share_memory, bool):
            raise TypeError("can_share_memory must be a boolean")


@dataclass(frozen=True, slots=True)
class AICommandInput:
    actor: CommandActor
    command: AICommand
    value: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", AICommand(self.command))
        if self.value is not None and not isinstance(self.value, str):
            raise TypeError("value must be a string or None")


@dataclass(frozen=True, slots=True)
class MemoryCommandInput:
    actor: CommandActor
    command: MemoryCommand
    value: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", MemoryCommand(self.command))
        if not isinstance(self.value, str):
            raise TypeError("value must be a string")


@dataclass(frozen=True, slots=True)
class CommandPreference:
    model_alias: str | None = None
    provider_id: str | None = None


@dataclass(frozen=True, slots=True)
class EffectiveRoute:
    """実行可否を含む公開可能な route 判断。reason は固定コードのみ。"""

    model_alias: str | None
    provider_id: str | None
    executable: bool
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.executable, bool) or not isinstance(self.reason, str) or not self.reason:
            raise ValueError("route status is invalid")


@dataclass(frozen=True, slots=True)
class CommandResult:
    ok: bool
    code: str
    data: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code or not isinstance(self.data, dict):
            raise ValueError("command result is invalid")
        forbidden = {"prompt", "memory", "content", "score", "secret", "chain_of_thought", "cot", "reasoning"}
        if forbidden.intersection(self.data):
            raise ValueError("command result contains a non-public field")


class PreferencePort(Protocol):
    def get(self, scope: CommandScope, user_id: int) -> CommandPreference: ...
    def set_model(self, scope: CommandScope, user_id: int, model_alias: str | None) -> None: ...
    def set_provider(self, scope: CommandScope, user_id: int, provider_id: str | None) -> None: ...


class ConversationResetPort(Protocol):
    def reset_conversation(self, scope: CommandScope, user_id: int) -> Awaitable[None] | None: ...


class RouteAvailabilityPort(Protocol):
    def resolve(self, scope: CommandScope, user_id: int, preference: CommandPreference) -> EffectiveRoute: ...


class DurableMemoryPort(Protocol):
    """Explicit memoryだけを扱い、本文をCommandResultへ返さないport。"""

    def remember(self, actor: CommandActor, text: str) -> str: ...
    def list_metadata(self, actor: CommandActor) -> tuple[dict[str, Any], ...]: ...
    def forget(self, actor: CommandActor, memory_id: str) -> bool: ...
    def clear(self, actor: CommandActor) -> int: ...
    def privacy(self, actor: CommandActor, mode: str | None) -> str: ...
    def preview_metadata(self, actor: CommandActor) -> dict[str, Any]: ...


class V0CommandService:
    def __init__(
        self,
        *,
        preferences: PreferencePort | None = None,
        conversation_reset: ConversationResetPort | None = None,
        route_availability: RouteAvailabilityPort | None = None,
        memory: DurableMemoryPort | None = None,
        catalog: ProviderCatalogManifest = DEFAULT_CATALOG,
        actor_check: Callable[[CommandActor], bool] | None = None,
    ) -> None:
        self._preferences = preferences
        self._conversation_reset = conversation_reset
        self._route_availability = route_availability
        self._memory = memory
        self._catalog = catalog
        self._actor_check = actor_check or (lambda actor: actor.actor_authorized)

    async def execute_ai(
        self,
        request: AICommandInput,
        *,
        commit_check: Callable[[CommandActor, str], bool] | None = None,
    ) -> CommandResult:
        if not self._authorized(request.actor):
            return CommandResult(False, "actor_not_authorized", {})
        if self._preferences is None or self._conversation_reset is None or self._route_availability is None:
            return CommandResult(False, "ai_unavailable", {})
        scope, user_id = request.actor.scope, request.actor.user_id
        preference = self._preferences.get(scope, user_id)
        if request.command is AICommand.MODEL_LIST:
            return CommandResult(
                True, "model_list", {"models": self._model_aliases(), "selected": preference.model_alias}
            )
        if request.command is AICommand.MODEL_SET:
            alias = self._model_alias(request.value)
            if alias is None:
                return self._invalid("model")
            if not self._commit_allowed(request.actor, request.command.value, commit_check):
                return CommandResult(False, "authorization_changed", {})
            self._preferences.set_model(scope, user_id, alias)
            return CommandResult(True, "model_set", {"selected": alias})
        if request.command is AICommand.MODEL_AUTO:
            if not self._commit_allowed(request.actor, request.command.value, commit_check):
                return CommandResult(False, "authorization_changed", {})
            self._preferences.set_model(scope, user_id, None)
            return CommandResult(True, "model_auto", {"selected": None})
        if request.command is AICommand.PROVIDER_LIST:
            return CommandResult(
                True, "provider_list", {"providers": self._provider_ids(), "selected": preference.provider_id}
            )
        if request.command is AICommand.PROVIDER_SET:
            provider_id = self._provider_id(request.value)
            if provider_id is None:
                return self._invalid("provider")
            if not self._commit_allowed(request.actor, request.command.value, commit_check):
                return CommandResult(False, "authorization_changed", {})
            self._preferences.set_provider(scope, user_id, provider_id)
            return CommandResult(True, "provider_set", {"selected": provider_id})
        if request.command is AICommand.ROUTE:
            effective = self._route_availability.resolve(scope, user_id, preference)
            return CommandResult(
                True,
                "route",
                {
                    "preferred_model": preference.model_alias,
                    "preferred_provider": preference.provider_id,
                    "effective_model": effective.model_alias,
                    "effective_provider": effective.provider_id,
                    "executable": effective.executable,
                    "reason": effective.reason,
                },
            )
        if request.command is AICommand.RESET:
            if not self._commit_allowed(request.actor, request.command.value, commit_check):
                return CommandResult(False, "authorization_changed", {})
            outcome = self._conversation_reset.reset_conversation(scope, user_id)
            if outcome is not None:
                await outcome
            return CommandResult(True, "reset", {"reset": "conversation_history_only"})
        return self._invalid("command")

    def execute_memory(
        self,
        request: MemoryCommandInput,
        *,
        commit_check: Callable[[CommandActor, str], bool] | None = None,
    ) -> CommandResult:
        if not self._authorized(request.actor):
            return CommandResult(False, "actor_not_authorized", {})
        if self._memory is None:
            return CommandResult(False, "memory_unavailable", {})
        try:
            if request.command is MemoryCommand.REMEMBER:
                text = request.value.strip()
                if not text or len(text) > 1_000:
                    return self._invalid("memory")
                if not self._commit_allowed(request.actor, request.command.value, commit_check):
                    return CommandResult(False, "authorization_changed", {})
                memory_id = self._memory.remember(request.actor, text)
                return CommandResult(True, "memory_remembered", {"memory_id": memory_id})
            if request.command is MemoryCommand.LIST:
                items = self._memory.list_metadata(request.actor)
                return CommandResult(True, "memory_list", {"items": items, "count": len(items)})
            if request.command is MemoryCommand.FORGET:
                memory_id = request.value.strip()
                if not memory_id:
                    return self._invalid("memory_id")
                if not self._commit_allowed(request.actor, request.command.value, commit_check):
                    return CommandResult(False, "authorization_changed", {})
                deleted = self._memory.forget(request.actor, memory_id)
                return CommandResult(deleted, "memory_forgotten" if deleted else "memory_not_found", {})
            if request.command is MemoryCommand.CLEAR:
                if request.value.strip() != "CLEAR_MY_MEMORY":
                    return self._invalid("confirmation")
                if not self._commit_allowed(request.actor, request.command.value, commit_check):
                    return CommandResult(False, "authorization_changed", {})
                return CommandResult(True, "memory_cleared", {"deleted": self._memory.clear(request.actor)})
            if request.command is MemoryCommand.PRIVACY:
                value = request.value.strip().lower() or None
                if value is not None and not self._commit_allowed(request.actor, request.command.value, commit_check):
                    return CommandResult(False, "authorization_changed", {})
                mode = self._memory.privacy(request.actor, value)
                return CommandResult(True, "memory_privacy", {"visibility": mode})
            if request.command is MemoryCommand.PREVIEW:
                return CommandResult(True, "memory_preview", self._memory.preview_metadata(request.actor))
        except (PermissionError, ValueError):
            return CommandResult(False, "memory_rejected", {})
        return self._invalid("command")

    def _authorized(self, actor: CommandActor) -> bool:
        try:
            return self._actor_check(actor) is True
        except Exception:
            return False

    def _commit_allowed(
        self,
        actor: CommandActor,
        operation: str,
        commit_check: Callable[[CommandActor, str], bool] | None,
    ) -> bool:
        if not self._authorized(actor):
            return False
        if commit_check is None:
            return False
        try:
            return commit_check(actor, operation) is True
        except Exception:
            return False

    @staticmethod
    def _invalid(field: str) -> CommandResult:
        return CommandResult(False, "invalid_input", {"field": field})

    def _model_aliases(self) -> tuple[str, ...]:
        route = self._catalog.route(LogicalCapability.AI_TEXT)
        return tuple(item.model_alias for item in route.tiers if item.model_alias is not None) if route else ()

    def _model_alias(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            alias = self._catalog.canonical_model_alias(value)
        except (TypeError, ValueError):
            return None
        return alias if alias in self._model_aliases() else None

    def _provider_ids(self) -> tuple[str, ...]:
        return tuple(
            provider.provider_id
            for provider in self._catalog.providers
            if LogicalCapability.AI_TEXT in provider.capabilities
        )

    def _provider_id(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            provider_id = normalize_identifier(value, label="provider_id")
        except (TypeError, ValueError):
            return None
        return provider_id if provider_id in self._provider_ids() else None
