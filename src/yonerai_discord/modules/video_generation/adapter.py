from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import Any

import discord
from discord import app_commands

from yonerai_discord.provider_registry import QualityTier

from .domain import (
    GENERATED_VIDEO_FILENAME,
    VIDEO_GENERATION_CAPABILITY_ID,
    VideoGenerationAuthorizationError,
    VideoGenerationError,
    VideoGenerationRequest,
)
from .ports import AuthorizationCheck
from .service import VideoGenerationService


_UNAVAILABLE_MESSAGE = "動画生成は現在利用できません。provider設定と安全条件を確認してください。"


class VideoGroup(app_commands.Group):
    def __init__(self, adapter: DiscordVideoGenerationAdapter) -> None:
        self.adapter = adapter
        super().__init__(name="video", description="明示的に動画生成を実行します", guild_only=True)

    @app_commands.command(name="generate", description="設定済みproviderでMP4動画を1件生成します")
    @app_commands.describe(prompt="生成内容（外部provider利用時は永続同意が必要です）", tier="品質tier")
    @app_commands.choices(
        tier=[
            app_commands.Choice(name="fast", value="fast"),
            app_commands.Choice(name="balanced", value="balanced"),
            app_commands.Choice(name="quality", value="quality"),
        ]
    )
    async def generate(
        self,
        interaction: discord.Interaction,
        prompt: str,
        tier: app_commands.Choice[str] | None = None,
    ) -> None:
        await self.adapter.generate(interaction, prompt, "balanced" if tier is None else tier.value)


class DiscordVideoGenerationAdapter:
    def __init__(
        self,
        service: VideoGenerationService,
        *,
        capability_check: Callable[[str, discord.Interaction], bool | Awaitable[bool]],
    ) -> None:
        if not isinstance(service, VideoGenerationService):
            raise TypeError("service must be a VideoGenerationService")
        if not callable(capability_check):
            raise TypeError("capability_check is required")
        self.service = service
        self.capability_check = capability_check
        self.group = VideoGroup(self)
        self._closing = False

    def install(self, tree: Any) -> None:
        tree.add_command(self.group)

    def uninstall(self, tree: Any) -> None:
        tree.remove_command(self.group.name)

    def begin_close(self) -> None:
        self._closing = True
        self.service.begin_close()

    async def generate(
        self,
        interaction: discord.Interaction,
        prompt: str,
        tier: str = "balanced",
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "動画生成Stage 1はサーバー内でのみ利用できます。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._allowed(interaction):
            await interaction.response.send_message(
                "この機能は現在のRegistryポリシーでは利用できません。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            request = VideoGenerationRequest(
                request_id=f"video-{int(interaction.id)}",
                guild_id=int(interaction.guild_id),
                channel_id=int(interaction.channel_id),
                actor_id=int(interaction.user.id),
                prompt=prompt,
                tier=QualityTier(tier),
            )
            generated = await self.service.generate(
                request,
                authorization_current=self._authorization(interaction),
            )
        except (VideoGenerationAuthorizationError, VideoGenerationError, TypeError, ValueError):
            await self._send_if_allowed(interaction, content=_UNAVAILABLE_MESSAGE)
            return
        except Exception:
            await self._send_if_allowed(interaction, content=_UNAVAILABLE_MESSAGE)
            return
        if not await self.service.delivery_current(
            request,
            generated,
            authorization_current=self._authorization(interaction),
        ):
            return
        embed = discord.Embed(
            title="生成動画",
            description=f"[構造検証済みMP4を添付しました。](attachment://{GENERATED_VIDEO_FILENAME})",
            color=0x5865F2,
        )
        await interaction.followup.send(
            embed=embed,
            file=discord.File(BytesIO(generated.mp4), filename=GENERATED_VIDEO_FILENAME),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def generate_for_message(
        self,
        message: discord.Message,
        *,
        prompt: str,
        authorization_current: AuthorizationCheck,
    ) -> bool:
        """@mention 用の薄い入口。既存serviceの認可・artifact・delivery境界を再利用する。"""

        if self._closing or not callable(authorization_current):
            return False
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        message_id = getattr(message, "id", None)
        ids = (
            getattr(guild, "id", None),
            getattr(channel, "id", None),
            getattr(author, "id", None),
            message_id,
        )
        if not all(isinstance(value, int) and value > 0 for value in ids):
            return False
        try:
            request = VideoGenerationRequest(
                request_id=f"video-mention-{message_id}",
                guild_id=int(guild.id),
                channel_id=int(channel.id),
                actor_id=int(author.id),
                prompt=prompt,
                tier=QualityTier.BALANCED,
            )
            generated = await self.service.generate(
                request,
                authorization_current=authorization_current,
            )
            if not await self.service.delivery_current(
                request,
                generated,
                authorization_current=authorization_current,
            ):
                return False
            allowed = authorization_current()
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if self._closing or allowed is not True:
                return False
            reply = getattr(message, "reply", None)
            if not callable(reply):
                return False
            embed = discord.Embed(
                title="生成動画",
                description=f"[構造検証済みMP4を添付しました。](attachment://{GENERATED_VIDEO_FILENAME})",
                color=0x5865F2,
            )
            await reply(
                embed=embed,
                file=discord.File(BytesIO(generated.mp4), filename=GENERATED_VIDEO_FILENAME),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except asyncio.CancelledError:
            raise
        except (VideoGenerationAuthorizationError, VideoGenerationError, TypeError, ValueError):
            return False
        except Exception:
            return False

    def _authorization(self, interaction: discord.Interaction) -> AuthorizationCheck:
        async def current() -> bool:
            return await self._allowed(interaction)

        return current

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if self._closing or interaction.guild_id is None:
            return False
        try:
            result = self.capability_check(VIDEO_GENERATION_CAPABILITY_ID, interaction)
            if inspect.isawaitable(result):
                result = await result
            return not self._closing and result is True
        except Exception:
            return False

    async def _send_if_allowed(self, interaction: discord.Interaction, *, content: str) -> None:
        if await self._allowed(interaction):
            await interaction.followup.send(
                content,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


__all__ = ["DiscordVideoGenerationAdapter", "VideoGroup"]
