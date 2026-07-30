from __future__ import annotations

from datetime import UTC
import secrets
from typing import Literal

import discord
from discord import app_commands

from yonerai_discord.discord_markdown import numbered_link

from .domain import color_from_hex, discord_timestamp, parse_choices, parse_dice, sha256_text, snowflake_created_at


def _safe(text: str, limit: int = 1_900) -> str:
    return text.replace("@", "＠")[:limit]


class InfoGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="info", description="Discord情報を安全に表示")

    @app_commands.command(name="user", description="ユーザー情報を表示します")
    async def user(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("メンバー情報を取得できません。", ephemeral=True)
            return
        roles = [role.name for role in target.roles if not role.is_default()]
        lines = [
            f"表示名: {_safe(target.display_name, 100)}",
            f"ユーザーID: `{target.id}`",
            f"アカウント作成: <t:{int(target.created_at.timestamp())}:F>",
            "サーバー参加: " + (f"<t:{int(target.joined_at.timestamp())}:F>" if target.joined_at else "不明"),
            f"Bot: {'はい' if target.bot else 'いいえ'}",
            "ロール: " + (", ".join(_safe(name, 50) for name in roles[-10:]) or "なし"),
        ]
        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="server", description="サーバー情報を表示します")
    async def server(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内でのみ利用できます。", ephemeral=True)
            return
        lines = [
            f"名前: {_safe(guild.name, 100)}",
            f"サーバーID: `{guild.id}`",
            f"作成: <t:{int(guild.created_at.timestamp())}:F>",
            f"メンバー数: {guild.member_count or '不明'}",
            f"チャンネル数: {len(guild.channels)}",
            f"ロール数: {len(guild.roles)}",
            f"ブースト: {guild.premium_subscription_count or 0}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="avatar", description="ユーザーのアバターを番号リンクで表示します")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        await interaction.response.send_message(
            f"アバター: {numbered_link(1, str(target.display_avatar.url))}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="role", description="ロール情報を表示します")
    async def role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        lines = [
            f"名前: {_safe(role.name, 100)}",
            f"ロールID: `{role.id}`",
            f"色: `#{role.color.value:06X}`",
            f"メンバー数: {len(role.members)}",
            f"位置: {role.position}",
            f"管理ロール: {'はい' if role.managed else 'いいえ'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="channel", description="テキストチャンネル情報を表示します")
    async def channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        lines = [
            f"名前: {_safe(channel.name, 100)}",
            f"チャンネルID: `{channel.id}`",
            f"カテゴリ: {_safe(channel.category.name, 100) if channel.category else 'なし'}",
            f"Slowmode: {channel.slowmode_delay}秒",
            f"NSFW: {'はい' if channel.is_nsfw() else 'いいえ'}",
            f"作成: <t:{int(channel.created_at.timestamp())}:F>",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="permissions", description="メンバーの主要権限を表示します")
    async def permissions(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("メンバー情報を取得できません。", ephemeral=True)
            return
        permissions = target.guild_permissions
        names = [
            label
            for attr, label in (
                ("administrator", "Administrator"),
                ("manage_guild", "サーバー管理"),
                ("manage_channels", "チャンネル管理"),
                ("manage_roles", "ロール管理"),
                ("manage_messages", "メッセージ管理"),
                ("moderate_members", "タイムアウト"),
                ("kick_members", "Kick"),
                ("ban_members", "Ban"),
                ("mention_everyone", "全体メンション"),
            )
            if getattr(permissions, attr)
        ]
        await interaction.response.send_message("、".join(names) or "特別な権限はありません。", ephemeral=True)


class ToolsGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="tools", description="日時・抽選・変換ツール")

    @app_commands.command(name="timestamp", description="ISO日時をDiscord timestampへ変換します")
    async def timestamp(
        self,
        interaction: discord.Interaction,
        datetime_text: str,
        style: Literal["t", "T", "d", "D", "f", "F", "R"] = "F",
    ) -> None:
        try:
            result = discord_timestamp(datetime_text, style)
        except ValueError:
            result = "日時が不正です。例: `2026-07-21T20:00+09:00`"
        await interaction.response.send_message(result, ephemeral=True)

    @app_commands.command(name="choose", description="カンマ区切りの候補から1つ選びます")
    async def choose(self, interaction: discord.Interaction, choices: str) -> None:
        try:
            result = secrets.choice(parse_choices(choices))
        except ValueError:
            result = "候補をカンマ区切りで2～20個指定してください。"
        await interaction.response.send_message(_safe(result), allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="dice", description="ダイスを振ります（例: 2d6+1）")
    async def dice(self, interaction: discord.Interaction, expression: str = "1d6") -> None:
        try:
            parsed = parse_dice(expression)
        except ValueError:
            await interaction.response.send_message("形式が不正です。例: `2d6+1`", ephemeral=True)
            return
        rolls = [secrets.randbelow(parsed.sides) + 1 for _ in range(parsed.count)]
        total = sum(rolls) + parsed.modifier
        suffix = f" {parsed.modifier:+d}" if parsed.modifier else ""
        await interaction.response.send_message(f"{rolls}{suffix} = **{total}**")

    @app_commands.command(name="random", description="指定範囲から整数を1つ選びます")
    async def random_number(self, interaction: discord.Interaction, minimum: int, maximum: int) -> None:
        if minimum > maximum or maximum - minimum > 10_000_000:
            await interaction.response.send_message("範囲が不正または広すぎます。", ephemeral=True)
            return
        result = minimum + secrets.randbelow(maximum - minimum + 1)
        await interaction.response.send_message(str(result))

    @app_commands.command(name="sha256", description="入力のSHA-256を計算します")
    async def sha256(self, interaction: discord.Interaction, text: str) -> None:
        try:
            digest = sha256_text(text)
        except ValueError:
            digest = "1～4000文字で指定してください。"
        await interaction.response.send_message(f"`{digest}`", ephemeral=True)

    @app_commands.command(name="snowflake", description="Discord IDの作成日時を表示します")
    async def snowflake(self, interaction: discord.Interaction, discord_id: str) -> None:
        try:
            created = snowflake_created_at(int(discord_id))
            result = f"<t:{int(created.replace(tzinfo=UTC).timestamp())}:F>"
        except (ValueError, OverflowError):
            result = "有効なDiscord IDを指定してください。"
        await interaction.response.send_message(result, ephemeral=True)

    @app_commands.command(name="color", description="HEXカラーを確認します")
    async def color(self, interaction: discord.Interaction, hex_color: str) -> None:
        try:
            value = color_from_hex(hex_color)
        except ValueError:
            await interaction.response.send_message("例: `#5865F2`", ephemeral=True)
            return
        embed = discord.Embed(title=f"#{value:06X}", color=value)
        await interaction.response.send_message(embed=embed, ephemeral=True)
