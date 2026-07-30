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
    GENERATED_MUSIC_FILENAME,
    MUSIC_GENERATION_CAPABILITY_ID,
    MusicGenerationError,
    MusicGenerationRequest,
)
from .service import MusicGenerationService
from .ports import AuthorizationCheck

_UNAVAILABLE = "音楽生成は現在利用できません。権限、同意、provider と保存先の設定を確認してください。"


class MusicGenGroup(app_commands.Group):
    def __init__(self, adapter: DiscordMusicGenerationAdapter) -> None:
        self.adapter = adapter
        super().__init__(
            name="musicgen", description="オリジナルのインストゥルメンタル preview を生成します", guild_only=True
        )

    @app_commands.command(name="generate", description="オリジナルのインストゥルメンタル WAV preview を生成します")
    @app_commands.describe(
        prompt="音楽の説明（歌詞・声・カバー・リミックス不可）",
        duration_seconds="1〜30秒",
        tier="品質 tier",
        rights_confirmed="権利確認済みであること",
    )
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
        rights_confirmed: bool,
        duration_seconds: app_commands.Range[int, 1, 30] = 15,
        tier: app_commands.Choice[str] | None = None,
    ) -> None:
        await self.adapter.generate(
            interaction,
            prompt,
            rights_confirmed,
            duration_seconds=int(duration_seconds),
            tier="balanced" if tier is None else tier.value,
        )


class DiscordMusicGenerationAdapter:
    def __init__(
        self,
        service: MusicGenerationService,
        *,
        capability_check: Callable[[str, discord.Interaction], bool | Awaitable[bool]],
    ) -> None:
        if not isinstance(service, MusicGenerationService):
            raise TypeError("service must be a MusicGenerationService")
        if not callable(capability_check):
            raise TypeError("capability_check is required")
        self.service, self.capability_check, self.group, self._closing = (
            service,
            capability_check,
            MusicGenGroup(self),
            False,
        )

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
        rights_confirmed: bool,
        duration_seconds: int = 15,
        tier: str = "balanced",
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "音楽生成はサーバー内でのみ利用できます。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._allowed(interaction):
            await interaction.response.send_message(
                "この機能は現在の権限では利用できません。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            request = MusicGenerationRequest(
                request_id=f"music-{int(interaction.id)}",
                guild_id=int(interaction.guild_id),
                channel_id=int(interaction.channel_id),
                actor_id=int(interaction.user.id),
                prompt=prompt,
                duration_seconds=duration_seconds,
                tier=QualityTier(tier),
                rights_confirmed=rights_confirmed,
            )
            generated = await self.service.generate(request, authorization_current=self._authorization(interaction))
        except (MusicGenerationError, TypeError, ValueError):
            await self._send_if_allowed(interaction, _UNAVAILABLE)
            return
        except Exception:
            await self._send_if_allowed(interaction, _UNAVAILABLE)
            return
        if not await self._allowed(interaction):
            return
        embed = discord.Embed(
            title="生成済み音楽",
            description=(
                "構造検証済みPCM16 WAVのオリジナル・インストゥルメンタルpreviewです。\n"
                f"[添付を開く](attachment://{GENERATED_MUSIC_FILENAME})"
            ),
            color=0x5865F2,
        )
        file = discord.File(BytesIO(generated.wav), filename=GENERATED_MUSIC_FILENAME)
        if not await self.service.delivery_current(
            request, generated, authorization_current=self._authorization(interaction)
        ):
            return
        await interaction.followup.send(
            embed=embed,
            file=file,
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
        """@mention 用の薄い入口。既存serviceのrights・認可・artifact境界を再利用する。"""

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
            request = MusicGenerationRequest(
                request_id=f"music-mention-{message_id}",
                guild_id=int(guild.id),
                channel_id=int(channel.id),
                actor_id=int(author.id),
                prompt=prompt,
                duration_seconds=15,
                tier=QualityTier.BALANCED,
                rights_confirmed=True,
            )
            generated = await self.service.generate(request, authorization_current=authorization_current)
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
                title="生成済み音楽",
                description=(
                    "構造検証済みPCM16 WAVのオリジナル・インストゥルメンタルpreviewです。\n"
                    f"[添付を開く](attachment://{GENERATED_MUSIC_FILENAME})"
                ),
                color=0x5865F2,
            )
            await reply(
                embed=embed,
                file=discord.File(BytesIO(generated.wav), filename=GENERATED_MUSIC_FILENAME),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except asyncio.CancelledError:
            raise
        except (MusicGenerationError, TypeError, ValueError):
            return False
        except Exception:
            return False

    def _authorization(self, interaction: discord.Interaction):
        async def current() -> bool:
            return await self._allowed(interaction)

        return current

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if self._closing or interaction.guild_id is None:
            return False
        try:
            value = self.capability_check(MUSIC_GENERATION_CAPABILITY_ID, interaction)
            value = await value if inspect.isawaitable(value) else value
            return not self._closing and value is True
        except Exception:
            return False

    async def _send_if_allowed(self, interaction: discord.Interaction, content: str) -> None:
        if await self._allowed(interaction):
            await interaction.followup.send(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
