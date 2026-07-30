from __future__ import annotations

from pathlib import Path
from typing import Any

from yonerai_discord.capabilities import COMMAND_CAPABILITIES, COMMAND_RBAC_FLOORS
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.ai.bounded_tools import (
    EMPTY_CAPABILITY_SNAPSHOT,
    StaticCapabilitySnapshot,
    build_static_capability_snapshot,
)
from yonerai_discord.modules.ai.models import TaskModelRequirement
from yonerai_discord.runtime_readiness import publish_runtime_readiness, withdraw_runtime_readiness

from .adapter import EvolutionGroup
from .service import EvolutionService


class EvolutionPlugin:
    def __init__(self) -> None:
        self.service: EvolutionService | None = None
        self._bot: Any | None = None

    async def start(self, bot: Any) -> None:
        artifact_dir = Path(bot.settings.database_path).parent / "evolution-proposals"

        def current_policy(operation: str, guild_id: int, actor_id: int) -> bool:
            command_path = f"evolution {operation}"
            capability_id = COMMAND_CAPABILITIES.get(command_path)
            guard = getattr(bot, "capability_guard", None)
            currently_allowed = getattr(guard, "currently_allowed", None)
            if capability_id is None or not callable(currently_allowed):
                return False
            try:
                owner_ids = {int(owner_id) for owner_id in bot.settings.bot_owner_ids}
                application_owner_id = getattr(bot, "owner_id", None)
                if application_owner_id is not None:
                    owner_ids.add(int(application_owner_id))
                application_owner_ids = getattr(bot, "owner_ids", None)
                if application_owner_ids is not None:
                    owner_ids.update(int(owner_id) for owner_id in application_owner_ids)
                if actor_id not in owner_ids:
                    return False
                return bool(
                    currently_allowed(
                        capability_id,
                        guild_id=guild_id,
                        user_id=actor_id,
                        actor_level=RbacLevel.BOT_OWNER,
                        floor=COMMAND_RBAC_FLOORS.get(command_path, RbacLevel.BOT_OWNER),
                    )
                )
            except Exception:
                return False

        provider_is_local = getattr(bot, "ai_provider_is_local", None) is True
        required_provider_model = None if provider_is_local else bot.settings.ai_model_quality
        capability_snapshot = _bounded_capability_snapshot(bot)
        self.service = EvolutionService(
            bot.database,
            artifact_dir,
            ai_service=getattr(bot, "ai_service", None),
            enabled=bot.settings.self_evolution_enabled,
            current_policy=current_policy,
            provider_is_local=provider_is_local,
            remote_consent_active=lambda user_id: bool(
                getattr(getattr(bot, "ai_remote_consent_store", None), "active_user", lambda _user_id: False)(user_id)
            ),
            model_requirement=TaskModelRequirement(
                "ai.quality",
                required_provider_model,
            ),
            capability_snapshot=capability_snapshot,
            provider_catalog_revision=getattr(
                getattr(bot, "ai_service", None),
                "provider_catalog_revision",
                None,
            ),
        )
        self._bot = bot
        setattr(bot, "evolution_service", self.service)
        gated_capabilities = {
            "cap-run-evolution-propose": self.service.enabled,
            "cap-run-evolution-review": self.service.enabled,
            "cap-run-evolution-approve": self.service.enabled,
            "cap-run-evolution-reject": self.service.enabled,
            "cap-run-evolution-show": self.service.enabled,
        }
        publish_runtime_readiness(bot, gated_capabilities)
        bot.tree.add_command(EvolutionGroup(self.service))

    async def begin_close(self) -> None:
        if self.service is not None:
            self.service.close()

    async def stop(self) -> None:
        if self.service is not None:
            self.service.close()
        if self._bot is not None:
            withdraw_runtime_readiness(
                self._bot,
                (
                    "cap-run-evolution-propose",
                    "cap-run-evolution-review",
                    "cap-run-evolution-approve",
                    "cap-run-evolution-reject",
                    "cap-run-evolution-show",
                ),
            )
            self._bot.tree.remove_command("evolution")
            if getattr(self._bot, "evolution_service", None) is self.service:
                delattr(self._bot, "evolution_service")
        self.service = None
        self._bot = None


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("evolution", EvolutionPlugin)


def _bounded_capability_snapshot(bot: Any) -> StaticCapabilitySnapshot:
    registry = getattr(bot, "capability_registry", None)
    if registry is None:
        return EMPTY_CAPABILITY_SNAPSHOT
    try:
        return build_static_capability_snapshot(registry)
    except Exception:
        return EMPTY_CAPABILITY_SNAPSHOT


__all__ = ["EvolutionPlugin", "EvolutionService", "setup"]
