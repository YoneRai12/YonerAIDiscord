from __future__ import annotations

import inspect
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import app_commands

from yonerai_discord.discord_markdown import numbered_reference

from .domain import ApodItem
from .errors import ApodDateError, NasaApodError
from .service import NasaApodService


NASA_APOD_CAPABILITY_ID = "cap-run-nasa-apod-read"
CapabilityCheck = Callable[[str, discord.Interaction], bool | Awaitable[bool]]


def render_apod_text(item: ApodItem) -> str:
    # 長い外部media URLを途中切断しない。自然文本文では丸ごと省略し、
    # 固定長のNASA source pageを必ず残す。
    links = _links(item, maximum_media_link_chars=900)
    copyright_text = (
        _safe_text(item.copyright, 180) if item.copyright is not None else "記載なし（public domainとは断定しません）"
    )
    prefix = (
        f"**NASA APOD: {_safe_text(item.title, 220)}**\n日付: `{item.day.isoformat()}` / 種別: `{item.media_type}`\n"
    )
    suffix = (
        f"\n著作権: {copyright_text}\n" + "\n".join(links) + "\n画像・動画のdownload、再upload、proxyは行っていません。"
    )
    explanation_budget = max(1, 1_900 - len(prefix) - len(suffix))
    return f"{prefix}{_safe_text(item.explanation, explanation_budget)}{suffix}"


def render_apod_embed(item: ApodItem) -> discord.Embed:
    links = "\n".join(_links(item))
    description_prefix = f"日付: `{item.day.isoformat()}` / 種別: `{item.media_type}`\n\n"
    description_suffix = f"\n\n{links}"
    explanation_budget = max(1, 4_096 - len(description_prefix) - len(description_suffix))
    embed = discord.Embed(
        title=f"NASA APOD: {_safe_text(item.title, 240)}"[:256],
        description=f"{description_prefix}{_safe_text(item.explanation, explanation_budget)}{description_suffix}"[
            :4_096
        ],
        color=0x0B3D91,
    )
    embed.add_field(
        name="著作権",
        value=(
            _safe_text(item.copyright, 300)
            if item.copyright is not None
            else "記載なし（public domainとは断定しません）"
        ),
        inline=False,
    )
    embed.set_footer(text="出典: NASA APOD。NASAによるYonerAIの承認を示すものではありません。")
    if item.media_type == "image" and item.url is not None:
        embed.set_image(url=item.url)
    return embed


class NasaGroup(app_commands.Group):
    def __init__(self, adapter: DiscordNasaApodAdapter) -> None:
        self.adapter = adapter
        super().__init__(name="nasa", description="NASAの公開情報を明示取得します")

    @app_commands.command(name="apod", description="NASA Astronomy Picture of the Dayを表示します")
    @app_commands.describe(date="省略またはYYYY-MM-DD（1995-06-16以降）")
    async def apod(self, interaction: discord.Interaction, date: str | None = None) -> None:
        await self.adapter.apod(interaction, date)


class DiscordNasaApodAdapter:
    def __init__(
        self,
        service: NasaApodService,
        *,
        capability_check: CapabilityCheck,
    ) -> None:
        if not callable(capability_check):
            raise TypeError("capability_check is required")
        self.service = service
        self.capability_check = capability_check
        self.group = NasaGroup(self)
        self._closing = False

    def install(self, tree: Any) -> None:
        tree.add_command(self.group)

    def uninstall(self, tree: Any) -> None:
        tree.remove_command(self.group.name)

    def begin_close(self) -> None:
        self._closing = True

    async def apod(self, interaction: discord.Interaction, value: str | None = None) -> None:
        if not await self._allowed(interaction):
            await interaction.response.send_message(
                "この機能は現在のRegistryポリシーでは利用できません。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            item = await self.service.get(value)
        except ApodDateError:
            await self._followup_if_allowed(
                interaction,
                content="日付は1995-06-16から今日までの `YYYY-MM-DD` で指定してください。",
            )
            return
        except NasaApodError:
            await self._followup_if_allowed(
                interaction,
                content="NASA APODを安全に取得できませんでした。詳細な応答や認証情報は表示しません。",
            )
            return
        except Exception:
            await self._followup_if_allowed(
                interaction,
                content="NASA APODの処理に失敗しました。詳細は表示されません。",
            )
            return
        await self._followup_if_allowed(interaction, embed=render_apod_embed(item))

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if self._closing:
            return False
        try:
            result = self.capability_check(NASA_APOD_CAPABILITY_ID, interaction)
            if inspect.isawaitable(result):
                result = await result
            return not self._closing and bool(result)
        except Exception:
            return False

    async def _followup_if_allowed(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ) -> None:
        if not await self._allowed(interaction):
            return
        await interaction.followup.send(
            content,
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _links(item: ApodItem, *, maximum_media_link_chars: int | None = None) -> tuple[str, ...]:
    links: list[str] = []
    if item.url is not None:
        label = "画像（外部リンク）" if item.media_type == "image" else "動画（外部リンク）"
        media_link = numbered_reference(len(links) + 1, item.url, label)
        if maximum_media_link_chars is None or len(media_link) <= maximum_media_link_chars:
            links.append(media_link)
    links.append(numbered_reference(len(links) + 1, item.source_page_url, "NASA APOD source page"))
    return tuple(links)


def _safe_text(value: object, maximum: int) -> str:
    text = str(value or "")[: maximum * 2]
    text = "".join(" " if unicodedata.category(character).startswith("C") else character for character in text)
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text)).strip()[:maximum]
    return text or "不明"


__all__ = [
    "NASA_APOD_CAPABILITY_ID",
    "DiscordNasaApodAdapter",
    "NasaGroup",
    "render_apod_embed",
    "render_apod_text",
]
