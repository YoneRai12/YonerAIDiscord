from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

import discord

from yonerai_discord.capability_forge.discord_adapter import (
    DiscordForgeAdapter,
    DiscordForgeAuthorizer,
    DiscordOwnerDmPort,
    ForgeDecisionButton,
)
from yonerai_discord.capability_forge.hyperv_disposable import (
    HyperVDisposableNamedPipeBrokerPort,
    HyperVDisposableSandboxPort,
    build_hyperv_disposable_sandbox_service,
)
from yonerai_discord.capability_forge.lifecycle import SqliteForgeLifecycleRepository
from yonerai_discord.capability_forge.named_pipe_exchange import FixedNamedPipeExchange
from yonerai_discord.capability_forge.owner_notification import OwnerNotificationService
from yonerai_discord.capability_forge.production_lifecycle import ProductionRecipeLifecycleBridge
from yonerai_discord.capability_forge.sandbox_lifecycle import SandboxProposalLifecycleBridge
from yonerai_discord.capability_forge.sandbox_runtime import DiscordSandboxRuntime
from yonerai_discord.capability_forge.static_templates import (
    ProductionRecipeRunner,
    available_code_owned_templates,
    build_production_registry,
)
from yonerai_discord.runtime_manifests.capability_forge import (
    FORGE_OWNER_NOTIFICATION_CAPABILITY_ID,
    SANDBOX_CANCEL_CAPABILITY_ID,
    SANDBOX_DOCTOR_CAPABILITY_ID,
    SANDBOX_JOBS_CAPABILITY_ID,
    SANDBOX_PLAN_CAPABILITY_ID,
    SANDBOX_RECEIPT_CAPABILITY_ID,
    SANDBOX_RUN_TEMPLATE_CAPABILITY_ID,
    SANDBOX_STATUS_CAPABILITY_ID,
)
from yonerai_discord.runtime_readiness import (
    publish_runtime_readiness,
    publish_runtime_readiness_probe,
    refresh_runtime_readiness,
    withdraw_runtime_readiness,
)
from yonerai_discord.sandbox_doctor import run_sandbox_doctor
from yonerai_discord.sandbox_operator_cli import SandboxCliDependencies

from .sandbox_adapter import SandboxGroup


_POLL_SECONDS = 1.0
_SANDBOX_READ_CAPABILITY_IDS = (
    SANDBOX_STATUS_CAPABILITY_ID,
    SANDBOX_DOCTOR_CAPABILITY_ID,
    SANDBOX_PLAN_CAPABILITY_ID,
    SANDBOX_JOBS_CAPABILITY_ID,
    SANDBOX_RECEIPT_CAPABILITY_ID,
)
_SANDBOX_BACKEND_CAPABILITY_IDS = (
    SANDBOX_RUN_TEMPLATE_CAPABILITY_ID,
    SANDBOX_CANCEL_CAPABILITY_ID,
)
_ALL_CAPABILITY_IDS = (
    FORGE_OWNER_NOTIFICATION_CAPABILITY_ID,
    *_SANDBOX_READ_CAPABILITY_IDS,
    *_SANDBOX_BACKEND_CAPABILITY_IDS,
)


class CapabilityForgePlugin:
    """Stage 2b owner-notification service の最小 Discord lifecycle。"""

    def __init__(self) -> None:
        self.repository: SqliteForgeLifecycleRepository | None = None
        self.service: OwnerNotificationService | None = None
        self.adapter: DiscordForgeAdapter | None = None
        self.recipe_lifecycle: ProductionRecipeLifecycleBridge | None = None
        self.sandbox_lifecycle: SandboxProposalLifecycleBridge | None = None
        self.sandbox_exchange: FixedNamedPipeExchange | None = None
        self.sandbox_broker: HyperVDisposableNamedPipeBrokerPort | None = None
        self.sandbox_port: HyperVDisposableSandboxPort | None = None
        self.sandbox_runtime: DiscordSandboxRuntime | None = None
        self.sandbox_group: SandboxGroup | None = None
        self._bot: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._dynamic_registered = False
        self._sandbox_group_registered = False

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            raise RuntimeError("capability forge plugin is already started")
        self._bot = bot
        self._closing = False
        try:
            publish_runtime_readiness(bot, dict.fromkeys(_ALL_CAPABILITY_IDS, False))
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
            sandbox_exchange: FixedNamedPipeExchange | None = None
            sandbox_broker: HyperVDisposableNamedPipeBrokerPort | None = None
            sandbox_port: HyperVDisposableSandboxPort | None = None
            sandbox_runtime: DiscordSandboxRuntime | None = None
            sandbox_group: SandboxGroup | None = None
            command_tree = getattr(bot, "tree", None)
            expected_guard = getattr(bot, "capability_guard", None)
            expected_registry = getattr(bot, "capability_registry", None)

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
                    and self.sandbox_runtime is sandbox_runtime
                    and getattr(bot, "capability_forge_sandbox_runtime", None) is sandbox_runtime
                )

            def backend_composition_current() -> bool:
                return (
                    self._bot is bot
                    and self.sandbox_exchange is sandbox_exchange
                    and self.sandbox_broker is sandbox_broker
                    and self.sandbox_port is sandbox_port
                    and self.sandbox_lifecycle is sandbox_lifecycle
                    and self.sandbox_runtime is sandbox_runtime
                    and getattr(bot, "capability_forge_sandbox_lifecycle", None) is sandbox_lifecycle
                    and getattr(bot, "capability_forge_sandbox_runtime", None) is sandbox_runtime
                )

            def sandbox_group_current() -> bool:
                get_current_command = getattr(command_tree, "get_command", None)
                if not callable(get_current_command) or sandbox_group is None:
                    return False
                try:
                    registered = get_current_command(
                        sandbox_group.name,
                        type=discord.AppCommandType.chat_input,
                    )
                except Exception:
                    return False
                return (
                    lifecycle_current()
                    and expected_guard is not None
                    and expected_registry is not None
                    and callable(getattr(expected_guard, "currently_allowed", None))
                    and callable(getattr(bot, "is_owner", None))
                    and getattr(expected_guard, "registry", None) is expected_registry
                    and self.sandbox_group is sandbox_group
                    and getattr(bot, "capability_forge_sandbox_group", None) is sandbox_group
                    and getattr(bot, "tree", None) is command_tree
                    and getattr(bot, "capability_guard", None) is expected_guard
                    and getattr(bot, "capability_registry", None) is expected_registry
                    and registered is sandbox_group
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
            sandbox_exchange = FixedNamedPipeExchange()
            sandbox_broker = HyperVDisposableNamedPipeBrokerPort(
                exchange=sandbox_exchange,
                exchange_identity_current=lambda: sandbox_exchange if backend_composition_current() else None,
            )
            sandbox_service, sandbox_port = build_hyperv_disposable_sandbox_service(
                broker=sandbox_broker,
                broker_identity_current=lambda: sandbox_broker if backend_composition_current() else None,
            )
            sandbox_lifecycle = SandboxProposalLifecycleBridge(
                sandbox=sandbox_service,
                repository=repository,
                current=lifecycle_current,
            )
            append_audit = getattr(getattr(bot, "database", None), "append_audit", None)
            sandbox_runtime = DiscordSandboxRuntime(
                database_path=database_path,
                lifecycle=sandbox_lifecycle,
                backend_ready=lambda: sandbox_service.containment_current,
                current=lifecycle_current,
                append_audit=append_audit if callable(append_audit) else None,
            )
            sandbox_runtime.open()
            sandbox_group = SandboxGroup(
                bot=bot,
                dependencies=SandboxCliDependencies(doctor=run_sandbox_doctor),
                current=sandbox_group_current,
                bind_dependencies=sandbox_runtime.bind_dependencies,
            )
            self.repository = repository
            self.service = service
            self.adapter = adapter
            self.recipe_lifecycle = recipe_lifecycle
            self.sandbox_lifecycle = sandbox_lifecycle
            self.sandbox_exchange = sandbox_exchange
            self.sandbox_broker = sandbox_broker
            self.sandbox_port = sandbox_port
            self.sandbox_runtime = sandbox_runtime
            self.sandbox_group = sandbox_group
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
            bot.capability_forge_sandbox_runtime = sandbox_runtime
            add_command = getattr(command_tree, "add_command", None)
            get_command = getattr(command_tree, "get_command", None)
            remove_command = getattr(command_tree, "remove_command", None)
            if not callable(add_command) or not callable(get_command) or not callable(remove_command):
                raise TypeError("bot.tree must provide add_command(), get_command(), and remove_command()")
            if get_command(sandbox_group.name, type=discord.AppCommandType.chat_input) is not None:
                raise RuntimeError("sandbox command group is already registered")
            self._sandbox_group_registered = True
            add_command(sandbox_group)
            bot.capability_forge_sandbox_group = sandbox_group
            self._task = asyncio.create_task(self._poll(), name="capability-forge-owner-notifications")
            # owner count is also checked before every side effect. Here it avoids advertising an unusable worker.
            owner_ids = getattr(settings, "bot_owner_ids", ())
            owner_configured = (
                isinstance(owner_ids, (frozenset, set, tuple, list))
                and len(owner_ids) == 1
                and isinstance(next(iter(owner_ids)), int)
                and not isinstance(next(iter(owner_ids)), bool)
                and next(iter(owner_ids)) > 0
            )
            publish_runtime_readiness(
                bot,
                {
                    FORGE_OWNER_NOTIFICATION_CAPABILITY_ID: owner_configured,
                    **dict.fromkeys(
                        _SANDBOX_READ_CAPABILITY_IDS,
                        owner_configured and sandbox_group_current(),
                    ),
                    **dict.fromkeys(_SANDBOX_BACKEND_CAPABILITY_IDS, False),
                },
            )
            publish_runtime_readiness_probe(
                bot,
                SANDBOX_RUN_TEMPLATE_CAPABILITY_ID,
                lambda: owner_configured and sandbox_group_current() and sandbox_runtime.ready,
            )
            publish_runtime_readiness_probe(
                bot,
                SANDBOX_CANCEL_CAPABILITY_ID,
                lambda: owner_configured and sandbox_group_current() and sandbox_runtime.cancellation_ready,
            )
        except BaseException:
            with suppress(BaseException):
                await self._cleanup()
            raise

    async def begin_close(self) -> None:
        first_error: BaseException | None = None
        self._closing = True
        if self.sandbox_group is not None:
            try:
                self.sandbox_group.begin_close()
            except BaseException as exc:
                first_error = exc
        if self.sandbox_runtime is not None:
            try:
                await self.sandbox_runtime.begin_close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if self.sandbox_lifecycle is not None:
            try:
                await self.sandbox_lifecycle.begin_close()
            except BaseException as exc:
                first_error = exc
        if self.sandbox_port is not None:
            try:
                self.sandbox_port.begin_close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if self.sandbox_exchange is not None:
            try:
                self.sandbox_exchange.begin_close()
            except BaseException as exc:
                if first_error is None:
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
            bot = self._bot
            if bot is not None:
                for capability_id in _SANDBOX_BACKEND_CAPABILITY_IDS:
                    with suppress(Exception):
                        refresh_runtime_readiness(bot, capability_id)
            await asyncio.sleep(_POLL_SECONDS)

    async def _cleanup(self) -> None:
        first_error: BaseException | None = None
        bot = self._bot
        repository = self.repository
        service = self.service
        adapter = self.adapter
        recipe_lifecycle = self.recipe_lifecycle
        sandbox_lifecycle = self.sandbox_lifecycle
        sandbox_runtime = self.sandbox_runtime
        sandbox_group = self.sandbox_group
        try:
            try:
                await self.begin_close()
            except BaseException as exc:
                first_error = exc
            if bot is not None:
                try:
                    withdraw_runtime_readiness(bot, _ALL_CAPABILITY_IDS)
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
                if self._sandbox_group_registered and sandbox_group is not None:
                    try:
                        tree = getattr(bot, "tree", None)
                        get_command = getattr(tree, "get_command", None)
                        remove_command = getattr(tree, "remove_command", None)
                        if callable(get_command) and callable(remove_command):
                            if get_command(sandbox_group.name, type=discord.AppCommandType.chat_input) is sandbox_group:
                                remove_command(sandbox_group.name, type=discord.AppCommandType.chat_input)
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                for name, expected in (
                    ("capability_forge_repository", repository),
                    ("capability_forge_service", service),
                    ("capability_forge_recipe_lifecycle", recipe_lifecycle),
                    ("capability_forge_sandbox_lifecycle", sandbox_lifecycle),
                    ("capability_forge_sandbox_runtime", sandbox_runtime),
                    ("capability_forge_sandbox_group", sandbox_group),
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
            self.sandbox_exchange = None
            self.sandbox_broker = None
            self.sandbox_port = None
            self.sandbox_runtime = None
            self.sandbox_group = None
            self._bot = None
            self._task = None
            self._dynamic_registered = False
            self._sandbox_group_registered = False
        if first_error is not None:
            raise first_error


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if not callable(register):
        raise TypeError("manager must provide register_plugin() or register()")
    register("capability_forge", CapabilityForgePlugin)


__all__ = ["CapabilityForgePlugin", "FORGE_OWNER_NOTIFICATION_CAPABILITY_ID", "SandboxGroup", "setup"]
