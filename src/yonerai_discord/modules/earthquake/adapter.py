from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import discord
from discord import app_commands

from .domain import EarthquakeEvent, EventKind
from .repository import GuildSubscription, SqliteEarthquakeRepository
from .service import EarthquakeService


DeliveryPolicy = Callable[[GuildSubscription, EarthquakeEvent], bool | Awaitable[bool]]
SubscriptionChangeCallback = Callable[[], Awaitable[None]]
SCALE_INPUTS = {
    "1": 10,
    "2": 20,
    "3": 30,
    "4": 40,
    "5弱": 45,
    "5強": 50,
    "6弱": 55,
    "6強": 60,
    "7": 70,
}
BOT_CHANNEL_PERMISSIONS = (
    ("view_channel", "チャンネルを見る"),
    ("send_messages", "メッセージを送信"),
    ("embed_links", "埋め込みリンク"),
)


class DiscordEarthquakeNotifier:
    def __init__(self, bot: Any, *, delivery_policy: DeliveryPolicy) -> None:
        if not callable(delivery_policy):
            raise TypeError("delivery_policy must be callable")
        self.bot = bot
        self.delivery_policy = delivery_policy

    async def send(self, subscription: GuildSubscription, event: EarthquakeEvent) -> bool:
        guild = self.bot.get_guild(subscription.guild_id)
        if guild is None:
            return False
        channel = guild.get_channel(subscription.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(subscription.channel_id)
            except Exception:
                return False
        send = getattr(channel, "send", None)
        if not callable(send):
            return False
        content = render_event(event)
        event_id = _notification_event_id(event)
        revision = _notification_revision(subscription, event)
        # 中央policyはキャッシュせず、外部副作用の直前に必ず再評価する。
        allowed = self.delivery_policy(subscription, event)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if allowed is not True:
            return False
        try:
            await send(content, allowed_mentions=discord.AllowedMentions.none())
        except asyncio.CancelledError:
            raise
        except Exception as send_error:
            terminal = getattr(self.bot, "interaction_failure_terminal", None)
            record_failure = getattr(terminal, "fail_background_once", None)
            if not callable(record_failure):
                raise
            try:
                await record_failure(
                    self.bot,
                    send_error,
                    surface="earthquake.notification",
                    guild_id=subscription.guild_id,
                    event_id=event_id,
                    actor_id=None,
                    revision=revision,
                )
            except asyncio.CancelledError:
                raise
            except Exception as terminal_error:
                raise send_error from terminal_error
            raise
        return True


def render_event(event: EarthquakeEvent) -> str:
    title = "緊急地震速報" if event.kind is EventKind.EEW else "地震情報"
    if event.cancelled:
        title += "（取消）"
    issue = _discord_time(event.issue_time)
    received = _discord_time(event.received_at)
    lines = [
        f"**{title}**  最大震度: **{event.scale_label}**",
        f"震源: {event.hypocenter_name or '不明'} / M{_number(event.magnitude)} / 深さ {_depth(event.depth_km)}",
        f"出典: {event.source}（P2P地震情報経由）",
        f"発表時刻: {issue}",
        f"受信時刻: {received}",
    ]
    if event.correction:
        lines.append(f"訂正区分: {event.correction}")
    if event.domestic_tsunami:
        lines.append(f"国内津波情報: {event.domestic_tsunami}")
    lines.append("この通知は気象庁の公式防災情報そのものではありません。")
    return "\n".join(lines)


def _notification_event_id(event: EarthquakeEvent) -> int:
    digest = hashlib.sha256(f"{event.id}\0{event.payload_hash}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1) or 1


def _notification_revision(subscription: GuildSubscription, event: EarthquakeEvent) -> str:
    values = (
        subscription.guild_id,
        subscription.channel_id,
        subscription.min_scale,
        subscription.notify_551,
        subscription.notify_556,
        subscription.enabled,
        event.id,
        event.code,
        event.kind.value,
        event.payload_hash,
    )
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EarthquakeGroup(app_commands.Group):
    def __init__(
        self,
        repository: SqliteEarthquakeRepository,
        service: EarthquakeService,
        *,
        on_subscriptions_changed: SubscriptionChangeCallback | None = None,
    ) -> None:
        super().__init__(name="earthquake", description="地震・緊急地震速報の通知設定")
        self.repository = repository
        self.service = service
        self.on_subscriptions_changed = on_subscriptions_changed

    @app_commands.command(name="latest", description="P2P地震情報から最新の地震・EEWを確認します")
    async def latest(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            event = await self.service.fetch_latest()
            content = "現在取得できる地震・EEW情報はありません。" if event is None else render_event(event)
        except Exception as exc:
            self.service.record_error(exc)
            await interaction.followup.send(
                "最新情報を安全に取得できませんでした。しばらくしてから再試行してください。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.followup.send(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="status", description="接続状態とこのサーバーの購読設定を表示します")
    async def status(self, interaction: discord.Interaction) -> None:
        try:
            snapshot = self.service.snapshot()
            subscription = None
            if interaction.guild_id is not None:
                subscription = await asyncio.to_thread(self.repository.get, interaction.guild_id)
            lines = [
                f"接続: `{'connected' if snapshot.connected else 'disconnected'}`",
                f"再接続: `{snapshot.reconnects}` / 受信: `{snapshot.received_payloads}` / 通知: `{snapshot.delivered_notifications}`",
                f"重複: `{snapshot.duplicate_payloads}` / 不正: `{snapshot.invalid_payloads}` / 古いEEW: `{snapshot.stale_eew_payloads}`",
                f"最終エラー: `{snapshot.last_error_type or 'none'}`",
            ]
            if subscription is not None:
                lines.append(
                    "購読: "
                    f"`{'ON' if subscription.enabled else 'OFF'}` / channel: "
                    f"`{subscription.channel_id or 'none'}` / 最小震度: `{subscription.min_scale}` / "
                    f"551=`{subscription.notify_551}` / 556=`{subscription.notify_556}`"
                )
        except Exception as exc:
            self.service.record_error(exc)
            await interaction.response.send_message(
                "地震モジュールの状態を安全に取得できませんでした。しばらくしてから再試行してください。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="subscribe", description="このサーバーの地震通知を有効にします")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def subscribe(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        min_scale: Literal["1", "2", "3", "4", "5弱", "5強", "6弱", "6強", "7"] = "4",
        notify_551: bool = True,
        notify_556: bool = True,
    ) -> None:
        if interaction.guild_id is None or not _can_manage_guild(interaction):
            await _deny(interaction)
            return
        if channel.guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "同じサーバー内のチャンネルを指定してください。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        missing_permissions = _missing_bot_channel_permissions(interaction, channel)
        if missing_permissions:
            await interaction.response.send_message(
                "Botのチャンネル権限が不足しています。必要な権限: " + "、".join(missing_permissions),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not (notify_551 or notify_556):
            await interaction.response.send_message(
                "地震情報（551）またはEEW（556）のどちらかを有効にしてください。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        subscription = await asyncio.to_thread(
            self.repository.subscribe,
            interaction.guild_id,
            channel.id,
            min_scale=SCALE_INPUTS[min_scale],
            notify_551=notify_551,
            notify_556=notify_556,
        )
        if not await self._reconcile_after_change(interaction):
            return
        await interaction.response.send_message(
            f"地震通知を <#{subscription.channel_id}> で有効にしました。最小震度: {min_scale}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="unsubscribe", description="このサーバーの地震通知を停止します")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def unsubscribe(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or not _can_manage_guild(interaction):
            await _deny(interaction)
            return
        await asyncio.to_thread(self.repository.unsubscribe, interaction.guild_id)
        if not await self._reconcile_after_change(interaction):
            return
        await interaction.response.send_message(
            "地震通知を停止しました。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _reconcile_after_change(self, interaction: discord.Interaction) -> bool:
        callback = self.on_subscriptions_changed
        if callback is None:
            return True
        try:
            await callback()
        except Exception as exc:
            self.service.record_error(exc)
            await interaction.response.send_message(
                "設定は保存されましたが、地震フィードの接続状態を安全に更新できませんでした。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True


def _can_manage_guild(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(
        permissions is not None
        and (getattr(permissions, "administrator", False) or getattr(permissions, "manage_guild", False))
    )


def _missing_bot_channel_permissions(interaction: discord.Interaction, channel: Any) -> tuple[str, ...]:
    guild = getattr(interaction, "guild", None)
    if guild is None:
        return tuple(label for _, label in BOT_CHANNEL_PERMISSIONS)
    bot_member = getattr(guild, "me", None)
    if bot_member is None:
        user = getattr(getattr(interaction, "client", None), "user", None)
        get_member = getattr(guild, "get_member", None)
        if user is not None and callable(get_member):
            bot_member = get_member(getattr(user, "id", None))
    if bot_member is None:
        return tuple(label for _, label in BOT_CHANNEL_PERMISSIONS)
    try:
        permissions = channel.permissions_for(bot_member)
    except Exception:
        return tuple(label for _, label in BOT_CHANNEL_PERMISSIONS)
    return tuple(label for name, label in BOT_CHANNEL_PERMISSIONS if not bool(getattr(permissions, name, False)))


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "この操作にはサーバー管理権限が必要です。",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def _discord_time(value: Any) -> str:
    if value is None:
        return "不明"
    return f"<t:{int(value.timestamp())}:F>"


def _number(value: float | None) -> str:
    return "不明" if value is None else f"{value:g}"


def _depth(value: int | None) -> str:
    return "不明" if value is None else f"{value}km"
