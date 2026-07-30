from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from ...capabilities import COMMAND_CAPABILITIES, COMMAND_PLUGIN_BY_ROOT, COMMAND_RBAC_FLOORS
from ...control_plane import ActorContext, Registry
from ...surface_inventory import command_paths_from_tree
from .domain import MAX_PAGE, MAX_QUERY_LENGTH, DiscoveryInputError, DiscoveryUnavailableError
from .service import DiscoveryService, render_command_page


logger = logging.getLogger(__name__)


class DiscoveryPlugin:
    def __init__(self) -> None:
        self._bot: Any | None = None
        self._command: app_commands.Command[Any, ..., Any] | None = None

    async def start(self, bot: Any) -> None:
        self._bot = bot

        @app_commands.guild_only()
        async def help_callback(
            interaction: discord.Interaction,
            query: app_commands.Range[str, 1, MAX_QUERY_LENGTH] | None = None,
            page: app_commands.Range[int, 1, MAX_PAGE] = 1,
        ) -> None:
            await self._respond(interaction, query=query, page=page)

        self._command = app_commands.Command(
            name="help",
            description="中央policy上、自分が利用できるコマンドを検索します",
            callback=help_callback,
        )
        bot.tree.add_command(self._command)

    async def stop(self) -> None:
        if self._bot is not None:
            self._bot.tree.remove_command("help", type=discord.AppCommandType.chat_input)
        self._command = None
        self._bot = None

    async def _respond(
        self,
        interaction: discord.Interaction,
        *,
        query: str | None,
        page: int,
    ) -> None:
        try:
            bot = self._bot
            if bot is None or getattr(bot, "surface_inventory", None) is None:
                raise DiscoveryUnavailableError("runtime inventory is not ready")
            registry = bot.require_registry()
            if not isinstance(registry, Registry):
                raise DiscoveryUnavailableError("registry is not ready")
            guard = getattr(bot, "capability_guard", None)
            actor_method = getattr(guard, "actor", None)
            if not callable(actor_method):
                raise DiscoveryUnavailableError("actor resolver is not ready")
            actor = await actor_method(interaction)
            if not isinstance(actor, ActorContext):
                raise DiscoveryUnavailableError("actor context is invalid")
            live_paths = command_paths_from_tree(bot.tree)
            service = DiscoveryService(
                registry,
                command_capabilities=COMMAND_CAPABILITIES,
                command_plugins=COMMAND_PLUGIN_BY_ROOT,
                command_floors=COMMAND_RBAC_FLOORS,
                plugin_is_running=bot.plugins.is_running,
            )
            content = render_command_page(
                service.search(
                    actor,
                    live_command_paths=live_paths,
                    query=query,
                    page=page,
                )
            )
        except DiscoveryInputError:
            content = f"検索語は{MAX_QUERY_LENGTH}文字以内、ページは1～{MAX_PAGE}で指定してください。"
        except Exception as exc:
            logger.error("discovery_help_failed", extra={"error_type": type(exc).__name__})
            content = "コマンド一覧の安全な取得に失敗したため、現在は表示できません。"
        await _send_ephemeral(interaction, content)


async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    kwargs = {
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)
