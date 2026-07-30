from __future__ import annotations

import discord
from discord import app_commands

from .client import (
    MinecraftConfigurationError,
    MinecraftProtocolError,
    MinecraftStatusClient,
    MinecraftUnavailableError,
)


class MinecraftGroup(app_commands.Group):
    def __init__(self, client: MinecraftStatusClient) -> None:
        super().__init__(name="minecraft", description="Minecraft Javaサーバー連携（read-only）")
        self.client = client

    @app_commands.command(name="status", description="設定済みMinecraft Javaサーバーの状態を表示します")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.client.query()
        except MinecraftConfigurationError:
            message = "Minecraft: 安全な接続先が設定されていません。"
        except (MinecraftProtocolError, MinecraftUnavailableError):
            message = "Minecraft: 接続できません（停止中・設定・ネットワークを確認）。"
        else:
            message = "\n".join(
                (
                    "Minecraft Java: オンライン",
                    f"バージョン: `{result.version_name}`",
                    f"人数: **{result.players_online} / {result.players_max}**",
                    f"応答: `{result.latency_ms} ms`",
                    f"MOTD: {result.description}",
                )
            )[:1_900]
        await interaction.followup.send(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
