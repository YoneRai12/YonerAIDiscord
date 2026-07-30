from __future__ import annotations

from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import Any

import discord
from discord import app_commands

from yonerai_discord.capabilities import COMMAND_CAPABILITIES, COMMAND_RBAC_FLOORS
from yonerai_discord.control_plane import RbacLevel

from .models import SpeechRequest
from .service import SpeechQueue, SpeechUnavailableError


class VoiceGroup(app_commands.Group):
    def __init__(self, bot: Any, queue: SpeechQueue) -> None:
        super().__init__(name="voice", description="VOICEVOX音声生成")
        self.bot = bot
        self.queue = queue

    @app_commands.command(name="status", description="VOICEVOX接続の設定状態を確認します")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "音声: 利用可能" if self.queue.available else "音声: 無効（VOICE_ENABLED=false）",
            ephemeral=True,
        )

    @app_commands.command(name="synthesize", description="VOICEVOXでWAV音声を作成します")
    @app_commands.describe(text="読み上げる日本語（500文字以内）", speaker_id="VOICEVOX話者ID")
    async def synthesize(self, interaction: discord.Interaction, text: str, speaker_id: int = 3) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("サーバー内でのみ利用できます。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        current_policy = await _capability_policy_probe(self.bot, interaction, "voice synthesize")
        if current_policy is None or not self.queue.available:
            await interaction.followup.send(
                "処理中に音声機能の設定が変更されたため、音声生成を開始しませんでした。",
                ephemeral=True,
            )
            return
        try:
            speech = await self.queue.synthesize(
                SpeechRequest(
                    text=text,
                    guild_id=interaction.guild_id,
                    channel_id=interaction.channel_id,
                    speaker_id=speaker_id,
                ),
                current_policy=current_policy,
            )
        except (SpeechUnavailableError, ValueError):
            await interaction.followup.send("音声生成は未設定か、一時的に利用できません。", ephemeral=True)
            return
        if not await current_policy() or not self.queue.available:
            await interaction.followup.send(
                "処理中に音声機能の設定が変更されたため、生成済み音声は送信しませんでした。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "生成しました。",
            file=discord.File(BytesIO(speech.wav), filename="voicevox.wav"),
            ephemeral=True,
        )


async def _capability_policy_probe(
    bot: Any,
    interaction: discord.Interaction,
    command_path: str,
) -> Callable[[], Awaitable[bool]] | None:
    guard = getattr(bot, "capability_guard", None)
    checker = getattr(guard, "currently_allowed", None)
    evaluate = getattr(guard, "evaluate_fresh_member", None)
    capability_id = COMMAND_CAPABILITIES.get(command_path)
    guild = getattr(interaction, "guild", None)
    fetch_member = getattr(guild, "fetch_member", None)
    if not callable(checker) or not callable(evaluate) or not callable(fetch_member) or capability_id is None:
        return None
    if interaction.guild_id is None or getattr(interaction.user, "id", None) is None:
        return None
    try:
        guild_id = int(interaction.guild_id)
        user_id = int(interaction.user.id)
        if getattr(guild, "id", None) != guild_id:
            return None
        floor = COMMAND_RBAC_FLOORS.get(command_path, RbacLevel.EVERYONE)

        async def current_policy() -> bool:
            try:
                if getattr(bot, "capability_guard", None) is not guard:
                    return False
                member = await fetch_member(user_id)
                if getattr(bot, "capability_guard", None) is not guard or getattr(member, "id", None) != user_id:
                    return False
                decision = await evaluate(capability_id, guild=guild, member=member)
                if getattr(bot, "capability_guard", None) is not guard:
                    return False
                actor_level = getattr(decision, "actor_level", None)
                current = checker(
                    capability_id,
                    guild_id=guild_id,
                    user_id=user_id,
                    actor_level=actor_level,
                    floor=floor,
                )
                return (
                    getattr(bot, "capability_guard", None) is guard
                    and getattr(decision, "allowed", None) is True
                    and current is True
                )
            except Exception:
                return False

        return current_policy if await current_policy() else None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
