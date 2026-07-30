from __future__ import annotations

import html
import inspect
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import discord
from discord import app_commands

from yonerai_discord.discord_markdown import numbered_link

from .domain import Holiday, HolidayYear, WarningReport, WeatherForecast
from .errors import JpInformationError, PublishedRangeError, UnknownRegionError
from .service import JST, JpInformationService


WEATHER_CAPABILITY_ID = "cap-run-weather"
WARNING_CAPABILITY_ID = "cap-run-warning"
HOLIDAY_NEXT_CAPABILITY_ID = "cap-run-holiday-next"
HOLIDAY_YEAR_CAPABILITY_ID = "cap-run-holiday-year"

_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~])")
CapabilityCheck = Callable[[str, discord.Interaction], bool | Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class JpInformationCapabilityIds:
    weather: str = WEATHER_CAPABILITY_ID
    warning: str = WARNING_CAPABILITY_ID
    holiday_next: str = HOLIDAY_NEXT_CAPABILITY_ID
    holiday_year: str = HOLIDAY_YEAR_CAPABILITY_ID


DEFAULT_CAPABILITY_IDS = JpInformationCapabilityIds()


def escape_discord_text(value: str, *, maximum: int = 800) -> str:
    """provider 由来文字列を HTML・Markdown・mention として解釈させない。"""

    if not isinstance(value, str):
        return ""
    without_controls = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value[:maximum])
    escaped = html.escape(without_controls, quote=True).replace("@", "＠")
    return _MARKDOWN.sub(r"\\\1", escaped)


def render_weather(forecast: WeatherForecast) -> str:
    lines = [
        f"**天気予報: {escape_discord_text(forecast.region.name, maximum=120)}**",
        f"地域コード: `{forecast.region.code}`",
    ]
    if forecast.headline:
        lines.append(f"概要: {escape_discord_text(forecast.headline, maximum=500)}")
    if forecast.periods:
        lines.append("予報:")
        for period in forecast.periods[:8]:
            lines.append(
                "- "
                f"{_format_datetime(period.starts_at)} "
                f"{escape_discord_text(period.area_name, maximum=100)}: "
                f"{escape_discord_text(period.weather, maximum=260)}"
            )
    footer = [
        f"発表時刻: {_format_datetime(forecast.issued_at)}",
        f"取得時刻: {_format_datetime(forecast.retrieved_at)}",
        f"公式情報: {numbered_link(1, forecast.source_url)}",
        f"出典: {escape_discord_text(forecast.publishing_office, maximum=160)} / 気象庁",
        "加工: 気象庁の公開JSONを要約・整形しています。",
    ]
    return _bounded_message(lines, footer)


def render_warning(report: WarningReport) -> str:
    lines = [
        f"**警報・注意報: {escape_discord_text(report.region.name, maximum=120)}**",
        f"地域コード: `{report.region.code}`",
    ]
    if report.headline:
        lines.append(f"概要: {escape_discord_text(report.headline, maximum=500)}")
    if report.warnings:
        lines.append("発表中:")
        for item in report.warnings[:16]:
            lines.append(
                "- "
                f"{escape_discord_text(item.area_name, maximum=100)}: "
                f"{escape_discord_text(item.name, maximum=140)} "
                f"（{escape_discord_text(item.status, maximum=60)}）"
            )
    else:
        lines.append("公式データ上、発表中の警報・注意報は見つかりませんでした。")
    footer = [
        f"発表時刻: {_format_datetime(report.issued_at)}",
        f"取得時刻: {_format_datetime(report.retrieved_at)}",
        f"公式情報: {numbered_link(1, report.source_url)}",
        f"出典: {escape_discord_text(report.publishing_office, maximum=160)} / 気象庁",
        "加工: 気象庁の公開JSONを要約・整形しています。",
        "注意: これは公式発表の転載・要約であり、Botによる独自予報ではありません。",
    ]
    return _bounded_message(lines, footer)


def render_holiday_year(result: HolidayYear) -> str:
    lines = [f"**{result.year}年の国民の祝日・休日（内閣府掲載分）**"]
    if result.holidays:
        lines.extend(
            f"- {item.day.isoformat()} {escape_discord_text(item.name, maximum=120)}" for item in result.holidays
        )
    else:
        lines.append("この年に掲載された祝日はありません。")
    footer = [
        f"公式掲載範囲: {result.published_from.isoformat()} ～ {result.published_through.isoformat()}",
        f"取得時刻: {_format_datetime(result.retrieved_at)}",
        f"公式CSV: {numbered_link(1, result.source_url)}",
        "出典: 内閣府（掲載CSVを整形。未掲載の将来日を計算していません）",
    ]
    return _bounded_message(lines, footer)


def render_next_holiday(item: Holiday, *, source_url: str, retrieved_at: datetime) -> str:
    lines = [
        "**次の国民の祝日・休日（内閣府掲載分）**",
        f"{item.day.isoformat()} {escape_discord_text(item.name, maximum=120)}",
    ]
    footer = [
        f"取得時刻: {_format_datetime(retrieved_at)}",
        f"公式CSV: {numbered_link(1, source_url)}",
        "出典: 内閣府（掲載CSVを参照。未掲載の将来日を計算していません）",
    ]
    return _bounded_message(lines, footer)


class HolidayGroup(app_commands.Group):
    def __init__(self, adapter: DiscordJpInformationAdapter, *, name: str = "holiday") -> None:
        self.adapter = adapter
        super().__init__(name=name, description="内閣府掲載の国民の祝日・休日を表示します")

    @app_commands.command(name="next", description="内閣府CSV掲載範囲内の次の祝日を表示します")
    async def next(self, interaction: discord.Interaction) -> None:
        await self.adapter.holiday_next(interaction)

    @app_commands.command(name="year", description="指定年の内閣府掲載祝日を表示します")
    @app_commands.describe(year="西暦（内閣府CSVの掲載範囲内）")
    async def year(self, interaction: discord.Interaction, year: int) -> None:
        await self.adapter.holiday_year(interaction, year)


class DiscordJpInformationAdapter:
    def __init__(
        self,
        service: JpInformationService,
        *,
        capability_check: CapabilityCheck | None = None,
        capability_ids: JpInformationCapabilityIds = DEFAULT_CAPABILITY_IDS,
        weather_command_name: str = "weather",
        warning_command_name: str = "warning",
        holiday_command_name: str = "holiday",
    ) -> None:
        self.service = service
        self.capability_check = capability_check
        self.capability_ids = capability_ids
        self._closing = False
        self.weather_command = app_commands.Command(
            name=weather_command_name,
            description="気象庁の天気予報を地域コードまたは正式名で表示します",
            callback=self.weather,
        )
        self.warning_command = app_commands.Command(
            name=warning_command_name,
            description="気象庁の警報・注意報を地域コードまたは正式名で表示します",
            callback=self.warning,
        )
        self.holiday_group = HolidayGroup(self, name=holiday_command_name)

    @property
    def command_names(self) -> tuple[str, str, str]:
        return (self.weather_command.name, self.warning_command.name, self.holiday_group.name)

    def install(self, tree: Any) -> None:
        tree.add_command(self.weather_command)
        tree.add_command(self.warning_command)
        tree.add_command(self.holiday_group)

    def uninstall(self, tree: Any) -> None:
        for name in reversed(self.command_names):
            tree.remove_command(name)

    async def weather(self, interaction: discord.Interaction, region: str) -> None:
        if not await self._allowed(self.capability_ids.weather, interaction):
            await self._send_denied(interaction)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            forecast = await self.service.get_weather(region)
            content = render_weather(forecast)
        except UnknownRegionError:
            content = "地域が見つかりません。気象庁 area.json の6桁コードまたは正式な地域名を指定してください。"
        except JpInformationError:
            content = "気象庁の天気情報を安全に取得できませんでした。時間をおいて再実行してください。"
        except Exception:
            content = "天気情報の処理に失敗しました。詳細は表示されません。"
        await self._followup_if_allowed(self.capability_ids.weather, interaction, content)

    async def warning(self, interaction: discord.Interaction, region: str) -> None:
        if not await self._allowed(self.capability_ids.warning, interaction):
            await self._send_denied(interaction)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            report = await self.service.get_warning(region)
            content = render_warning(report)
        except UnknownRegionError:
            content = "地域が見つかりません。気象庁 area.json の6桁コードまたは正式な地域名を指定してください。"
        except JpInformationError:
            content = "気象庁の警報・注意報を安全に取得できませんでした。時間をおいて再実行してください。"
        except Exception:
            content = "警報・注意報の処理に失敗しました。詳細は表示されません。"
        await self._followup_if_allowed(self.capability_ids.warning, interaction, content)

    async def holiday_next(self, interaction: discord.Interaction, *, on_or_after: date | None = None) -> None:
        if not await self._allowed(self.capability_ids.holiday_next, interaction):
            await self._send_denied(interaction)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            calendar = await self.service.get_holiday_calendar()
            day = on_or_after or self.service._now().astimezone(JST).date()
            item = calendar.next_on_or_after(day)
            content = (
                render_next_holiday(item, source_url=calendar.source_url, retrieved_at=calendar.retrieved_at)
                if item is not None
                else "内閣府CSVの公式掲載範囲内に、これ以降の祝日はありません。将来日を独自計算しません。"
            )
        except PublishedRangeError:
            content = "指定日は内閣府CSVの公式掲載範囲外です。未掲載の将来日を独自計算しません。"
        except JpInformationError:
            content = "内閣府の祝日情報を安全に取得できませんでした。時間をおいて再実行してください。"
        except Exception:
            content = "祝日情報の処理に失敗しました。詳細は表示されません。"
        await self._followup_if_allowed(self.capability_ids.holiday_next, interaction, content)

    async def holiday_year(self, interaction: discord.Interaction, year: int) -> None:
        if not await self._allowed(self.capability_ids.holiday_year, interaction):
            await self._send_denied(interaction)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            if isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999:
                raise ValueError("invalid year")
            content = render_holiday_year(await self.service.holidays_for_year(year))
        except PublishedRangeError:
            content = "指定年は内閣府CSVの公式掲載範囲外です。未掲載の将来日を独自計算しません。"
        except ValueError:
            content = "年は1～9999の西暦で指定してください。"
        except JpInformationError:
            content = "内閣府の祝日情報を安全に取得できませんでした。時間をおいて再実行してください。"
        except Exception:
            content = "祝日情報の処理に失敗しました。詳細は表示されません。"
        await self._followup_if_allowed(self.capability_ids.holiday_year, interaction, content)

    def begin_close(self) -> None:
        self._closing = True

    async def _allowed(self, capability_id: str, interaction: discord.Interaction) -> bool:
        if self._closing:
            return False
        if self.capability_check is None:
            return True
        try:
            result = self.capability_check(capability_id, interaction)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            return False

    async def _followup_if_allowed(
        self,
        capability_id: str,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        # provider待機中にもmodule/権限/BOT終了状態は変わり得るため、送信直前に再検証する。
        if await self._allowed(capability_id, interaction):
            await self._followup(interaction, content)

    @staticmethod
    async def _send_denied(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "この機能は現在のRegistryポリシーでは利用できません。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @staticmethod
    async def _followup(interaction: discord.Interaction, content: str) -> None:
        await interaction.followup.send(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


# 既存 module の Adapter 命名に合わせた公開 alias。
JpInformationAdapter = DiscordJpInformationAdapter


def _format_datetime(value: datetime) -> str:
    return value.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")


def _bounded_message(lines: list[str], footer: list[str], *, maximum: int = 1_900) -> str:
    footer_text = "\n".join(footer)
    body_budget = max(0, maximum - len(footer_text) - 1)
    body: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + (1 if body else 0)
        if length + addition > body_budget:
            body.append("…（表示上限のため省略）")
            break
        body.append(line)
        length += addition
    body_text = "\n".join(body)
    return f"{body_text}\n{footer_text}"[:maximum]
