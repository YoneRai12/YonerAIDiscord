from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from yonerai_discord.capability_forge.discord_adapter import (
    DiscordForgeAdapter,
    DiscordForgeAuthorizer,
    DiscordOwnerDmPort,
    ForgeDecisionButton,
)
from yonerai_discord.capability_forge.lifecycle import SqliteForgeLifecycleRepository
from yonerai_discord.capability_forge.owner_notification import OwnerNotificationService
from yonerai_discord.capability_forge.production_lifecycle import ProductionRecipeLifecycleBridge
from yonerai_discord.capability_forge.sandbox_lifecycle import SandboxProposalLifecycleBridge
from yonerai_discord.capability_forge.sandbox_service import ExternalSandboxService
from yonerai_discord.capability_forge.static_templates import (
    ProductionRecipeRunner,
    available_code_owned_templates,
    build_production_registry,
)
from yonerai_discord.runtime_manifests.capability_forge import FORGE_OWNER_NOTIFICATION_CAPABILITY_ID
from yonerai_discord.runtime_readiness import publish_runtime_readiness, withdraw_runtime_readiness


_POLL_SECONDS = 1.0


class CapabilityForgePlugin:
    """Stage 2b owner-notification service の最小 Discord lifecycle。"""

    def __init__(self) -> None:
        self.repository: SqliteForgeLifecycleRepository | None = None
        self.service: OwnerNotificationService | None = None
        self.adapter: DiscordForgeAdapter | None = None
        self.recipe_lifecycle: ProductionRecipeLifecycleBridge | None = None
        self.sandbox_lifecycle: SandboxProposalLifecycleBridge | None = None
        self._bot: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._dynamic_registered = False

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            raise RuntimeError("capability forge plugin is already started")
        self._bot = bot
        self._closing = False
        try:
            publish_runtime_readiness(bot, {FORGE_OWNER_NOTIFICATION_CAPABILITY_ID: False})
            settings = getattr(bot, "settings", None)
            database_path = getattr(settings, "database_path", None)
            if not isinstance(database_path, Path):
                raise TypeError("settings.database_path must be a Path")
            repository = SqliteForgeLifecycleRepository(database_path)
            repository.open()
            registry = build_production_registry(available_code_owned_templates())
            runner = ProductionRecipeRunner(registry)
            recipe_lifecycle: ProductionRecipeLifecycleBridge | None = None
            sandbox_lifecycle: SandboxProposalLifecycleBridge | None = None

            def lifecycle_current() -> bool:
                return (
                    self._closing is False
                    and self._bot is bot
                    and self.repository is repository
                    and getattr(bot, "capability_forge_repository", None) is repository
                    and self.service is service
                    and getattr(bot, "capability_forge_service", None) is service
                    and self.recipe_lifecycle is recipe_lifecycle
                    and getattr(bot, "capability_forge_recipe_lifecycle", None) is recipe_lifecycle
                    and self.sandbox_lifecycle is sandbox_lifecycle
                    and getattr(bot, "capability_forge_sandbox_lifecycle", None) is sandbox_lifecycle
                )

            recipe_lifecycle = ProductionRecipeLifecycleBridge(
                runner=runner,
                repository=repository,
                current=lifecycle_current,
            )
            authorizer = DiscordForgeAuthorizer(bot)
            service = OwnerNotificationService(
                repository=repository,
                owner_resolver=authorizer,
                dm_port=DiscordOwnerDmPort(authorizer),
            )
            adapter = DiscordForgeAdapter(authorizer=authorizer, service=service)
            sandbox_lifecycle = SandboxProposalLifecycleBridge(
                sandbox=ExternalSandboxService(port=None, containment_current=None),
                repository=repository,
                current=lifecycle_current,
            )
            self.repository = repository
            self.service = service
            self.adapter = adapter
            self.recipe_lifecycle = recipe_lifecycle
            self.sandbox_lifecycle = sandbox_lifecycle
            ForgeDecisionButton.adapter = adapter
            add_dynamic_items = getattr(bot, "add_dynamic_items", None)
            if not callable(add_dynamic_items):
                raise TypeError("bot must provide add_dynamic_items()")
            add_dynamic_items(ForgeDecisionButton)
            self._dynamic_registered = True
            bot.capability_forge_repository = repository
            bot.capability_forge_service = service
            bot.capability_forge_recipe_lifecycle = recipe_lifecycle
            bot.capability_forge_sandbox_lifecycle = sandbox_lifecycle
            self._task = asyncio.create_task(self._poll(), name="capability-forge-owner-notifications")
            # owner count is also checked before every side effect. Here it avoids advertising an unusable worker.
            owner_ids = getattr(settings, "bot_owner_ids", ())
            publish_runtime_readiness(
                bot,
                {
                    FORGE_OWNER_NOTIFICATION_CAPABILITY_ID: isinstance(owner_ids, (frozenset, set, tuple, list))
                    and len(owner_ids) == 1
                },
            )
        except BaseException:
            with suppress(BaseException):
                await self._cleanup()
            raise

    async def begin_close(self) -> None:
        first_error: BaseException | None = None
        self._closing = True
        if self.sandbox_lifecycle is not None:
            try:
                await self.sandbox_lifecycle.begin_close()
            except BaseException as exc:
                first_error = exc
        if self.recipe_lifecycle is not None:
            try:
                await self.recipe_lifecycle.begin_close()
            except BaseException as exc:
                first_error = exc
        if self.adapter is not None:
            try:
                self.adapter.begin_close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            self._task = None
        if first_error is not None:
            raise first_error

    async def stop(self) -> None:
        await self._cleanup()

    async def _poll(self) -> None:
        while not self._closing:
            service = self.service
            if service is not None:
                with suppress(Exception):
                    await service.deliver_next()
            await asyncio.sleep(_POLL_SECONDS)

    async def _cleanup(self) -> None:
        first_error: BaseException | None = None
        bot = self._bot
        repository = self.repository
        service = self.service
        adapter = self.adapter
        recipe_lifecycle = self.recipe_lifecycle
        sandbox_lifecycle = self.sandbox_lifecycle
        try:
            try:
                await self.begin_close()
            except BaseException as exc:
                first_error = exc
            if bot is not None:
                try:
                    withdraw_runtime_readiness(bot, (FORGE_OWNER_NOTIFICATION_CAPABILITY_ID,))
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                if self._dynamic_registered:
                    try:
                        remove_dynamic_items = getattr(bot, "remove_dynamic_items", None)
                        if callable(remove_dynamic_items):
                            remove_dynamic_items(ForgeDecisionButton)
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                for name, expected in (
                    ("capability_forge_repository", repository),
                    ("capability_forge_service", service),
                    ("capability_forge_recipe_lifecycle", recipe_lifecycle),
                    ("capability_forge_sandbox_lifecycle", sandbox_lifecycle),
                ):
                    try:
                        if getattr(bot, name, None) is expected:
                            delattr(bot, name)
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
            try:
                if ForgeDecisionButton.adapter is adapter:
                    ForgeDecisionButton.adapter = None
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            if repository is not None:
                try:
                    repository.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        finally:
            self.repository = None
            self.service = None
            self.adapter = None
            self.recipe_lifecycle = None
            self.sandbox_lifecycle = None
            self._bot = None
            self._task = None
            self._dynamic_registered = False
        if first_error is not None:
            raise first_error


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if not callable(register):
        raise TypeError("manager must provide register_plugin() or register()")
    register("capability_forge", CapabilityForgePlugin)


__all__ = ["CapabilityForgePlugin", "FORGE_OWNER_NOTIFICATION_CAPABILITY_ID", "setup"]
