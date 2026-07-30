from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import DecisionCode, PolicyDecision, RbacLevel, reason_for
from .registry import Registry
from .state import GuildId, normalize_guild_id


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Discord objectを持ち込まない認可入力。adapter側で事実を確定する。"""

    actor_id: int | str
    guild_id: GuildId | None = None
    level: RbacLevel = RbacLevel.EVERYONE

    def __post_init__(self) -> None:
        if isinstance(self.actor_id, bool) or not isinstance(self.actor_id, (int, str)):
            raise TypeError("actor_id must be an int or string")
        actor_id = str(self.actor_id).strip()
        if not actor_id:
            raise ValueError("actor_id must not be empty")
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "guild_id", normalize_guild_id(self.guild_id))
        object.__setattr__(self, "level", RbacLevel.parse(self.level))

    @property
    def is_bot_owner(self) -> bool:
        return self.level >= RbacLevel.BOT_OWNER

    @property
    def is_guild_owner(self) -> bool:
        return self.level >= RbacLevel.GUILD_OWNER

    @property
    def is_guild_admin(self) -> bool:
        return self.level >= RbacLevel.GUILD_ADMIN


class ConfigScope(StrEnum):
    GLOBAL = "global"
    GUILD = "guild"


class ConfigTarget(StrEnum):
    MODULE = "module"
    CAPABILITY = "capability"
    CAPABILITY_LEVEL = "capability_level"


class PolicyEngine:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def evaluate(self, capability_id: str, actor: ActorContext) -> PolicyDecision:
        if not isinstance(actor, ActorContext):
            raise TypeError("actor must be ActorContext")
        availability = self.registry.capability_status(capability_id, actor.guild_id)
        if not availability.executable:
            return PolicyDecision(
                allowed=False,
                code=availability.code,
                reason=availability.reason,
                capability_id=availability.subject_id,
                actor_level=actor.level,
            )

        required = self.registry.required_level(capability_id, actor.guild_id)
        if actor.level < required:
            return PolicyDecision(
                allowed=False,
                code=DecisionCode.INSUFFICIENT_LEVEL,
                reason=reason_for(
                    DecisionCode.INSUFFICIENT_LEVEL,
                    f"required={required.name.lower()}, actual={actor.level.name.lower()}",
                ),
                capability_id=availability.subject_id,
                required_level=required,
                actor_level=actor.level,
            )
        return PolicyDecision(
            allowed=True,
            code=DecisionCode.ALLOWED,
            reason=reason_for(DecisionCode.ALLOWED),
            capability_id=availability.subject_id,
            required_level=required,
            actor_level=actor.level,
        )

    authorize = evaluate

    def evaluate_config_change(
        self,
        actor: ActorContext,
        *,
        scope: ConfigScope | str,
        target: ConfigTarget | str,
        target_id: str,
        guild_id: GuildId | None = None,
        requested_level: RbacLevel | str | int | None = None,
    ) -> PolicyDecision:
        if not isinstance(actor, ActorContext):
            raise TypeError("actor must be ActorContext")
        scope = ConfigScope(scope)
        target = ConfigTarget(target)

        # targetの誤字や未登録値もdeny理由として返す。
        try:
            if target == ConfigTarget.MODULE:
                subject_id = self.registry.module(target_id).module_id
            else:
                subject_id = self.registry.capability(target_id).capability_id
        except KeyError:
            code = DecisionCode.UNKNOWN_MODULE if target == ConfigTarget.MODULE else DecisionCode.UNKNOWN_CAPABILITY
            return PolicyDecision(False, code, reason_for(code, target_id), actor_level=actor.level)

        if scope == ConfigScope.GLOBAL:
            if guild_id is not None:
                raise ValueError("guild_id must be omitted for global configuration")
            if not actor.is_bot_owner:
                return PolicyDecision(
                    False,
                    DecisionCode.GLOBAL_CONFIG_REQUIRES_BOT_OWNER,
                    reason_for(DecisionCode.GLOBAL_CONFIG_REQUIRES_BOT_OWNER),
                    capability_id=subject_id if target != ConfigTarget.MODULE else None,
                    required_level=RbacLevel.BOT_OWNER,
                    actor_level=actor.level,
                )
        else:
            normalized_guild = normalize_guild_id(guild_id)
            if normalized_guild is None:
                return PolicyDecision(
                    False,
                    DecisionCode.GUILD_CONTEXT_REQUIRED,
                    reason_for(DecisionCode.GUILD_CONTEXT_REQUIRED),
                    actor_level=actor.level,
                )
            if not actor.is_bot_owner and actor.guild_id != normalized_guild:
                return PolicyDecision(
                    False,
                    DecisionCode.GUILD_MISMATCH,
                    reason_for(DecisionCode.GUILD_MISMATCH),
                    actor_level=actor.level,
                )
            if actor.level < RbacLevel.GUILD_ADMIN:
                return PolicyDecision(
                    False,
                    DecisionCode.GUILD_CONFIG_REQUIRES_ADMIN,
                    reason_for(DecisionCode.GUILD_CONFIG_REQUIRES_ADMIN),
                    required_level=RbacLevel.GUILD_ADMIN,
                    actor_level=actor.level,
                )

        if target == ConfigTarget.CAPABILITY_LEVEL:
            if requested_level is None:
                raise ValueError("requested_level is required for capability_level configuration")
            spec = self.registry.capability(subject_id)
            requested = RbacLevel.parse(requested_level)
            if scope == ConfigScope.GUILD and requested < spec.safety_floor:
                return PolicyDecision(
                    False,
                    DecisionCode.SAFETY_FLOOR,
                    reason_for(
                        DecisionCode.SAFETY_FLOOR,
                        f"minimum={spec.safety_floor.name.lower()}, requested={requested.name.lower()}",
                    ),
                    capability_id=subject_id,
                    required_level=spec.safety_floor,
                    actor_level=actor.level,
                )

        return PolicyDecision(
            True,
            DecisionCode.ALLOWED,
            reason_for(DecisionCode.ALLOWED),
            capability_id=subject_id if target != ConfigTarget.MODULE else None,
            actor_level=actor.level,
        )

    authorize_config_change = evaluate_config_change


RbacPolicy = PolicyEngine
