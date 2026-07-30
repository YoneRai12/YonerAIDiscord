from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import discord
from discord import app_commands

from .domain import (
    DeliveryIntentState,
    DeliveryResolution,
    Meeting,
    RSVP,
    RSVPStatus,
    Reminder,
    ReminderDelivery,
    parse_aware_datetime,
    reminder_action_key,
)
from .repository import SqliteReminderRepository
from .service import DeliveryAbortedError, RetryableDeliveryError


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduleCancelReceipt:
    guild_id: int
    user_id: int
    source_channel_id: int
    source_message_id: int
    prompt_message_id: int | None
    meeting_id: str
    digest: str = ""


def schedule_cancel_receipt_digest(receipt: ScheduleCancelReceipt) -> str:
    values = (
        receipt.guild_id,
        receipt.user_id,
        receipt.source_channel_id,
        receipt.source_message_id,
        receipt.prompt_message_id,
        receipt.meeting_id,
    )
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest()


class DiscordReminderSender:
    """送信直前に在籍・Bot判定をやり直し、メンションは既定拒否する。"""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot

    async def send(
        self,
        delivery: ReminderDelivery,
        *,
        still_allowed: Callable[[], bool],
    ) -> None:
        meeting = delivery.meeting
        content = f"予定 **{meeting.title}** は <t:{int(meeting.starts_at.timestamp())}:R> に始まります。"
        guild = self.bot.get_guild(meeting.guild_id)
        if guild is None:
            raise RetryableDeliveryError("guild is unavailable")
        self._require_allowed(still_allowed)
        target_id = delivery.reminder.target_user_id
        if target_id is not None:
            try:
                member = guild.get_member(target_id) or await guild.fetch_member(target_id)
            except Exception as exc:
                raise RetryableDeliveryError(type(exc).__name__) from None
            self._require_allowed(still_allowed)
            if member.bot:
                raise RetryableDeliveryError("bot users cannot receive reminders")
            self._require_allowed(still_allowed)
            await member.send(content, allowed_mentions=discord.AllowedMentions.none())
            return
        channel = guild.get_channel(meeting.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(meeting.channel_id)
            except Exception as exc:
                raise RetryableDeliveryError(type(exc).__name__) from None
        self._require_allowed(still_allowed)
        if not isinstance(channel, discord.abc.Messageable):
            raise RetryableDeliveryError("meeting channel is not messageable")
        self._require_allowed(still_allowed)
        await channel.send(content, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _require_allowed(still_allowed: Callable[[], bool]) -> None:
        try:
            allowed = still_allowed()
        except Exception:
            allowed = False
        if allowed is not True:
            raise DeliveryAbortedError("delivery is no longer allowed")


class ScheduleGroup(app_commands.Group):
    def __init__(self, repository: SqliteReminderRepository, bot: discord.Client) -> None:
        super().__init__(name="schedule", description="会議・予定・通知")
        self.repository = repository
        self.bot = bot

    @app_commands.command(name="create", description="日時を明示して予定を登録します")
    @app_commands.describe(
        title="予定名",
        starts_at="開始（例: 2026-07-21T20:00+09:00）",
        ends_at="終了（例: 2026-07-21T21:00+09:00）",
        timezone="表示用IANA timezone",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        title: str,
        starts_at: str,
        ends_at: str,
        timezone: str = "Asia/Tokyo",
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message("サーバー内でのみ利用できます。", ephemeral=True)
            return
        try:
            meeting = Meeting(
                id=f"MEET-{secrets.token_hex(4).upper()}",
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                creator_id=interaction.user.id,
                title=title[:120],
                starts_at=parse_aware_datetime(starts_at),
                ends_at=parse_aware_datetime(ends_at),
                timezone=timezone,
            )
            created = self.repository.save_meeting(meeting)
        except ValueError:
            await interaction.response.send_message(
                "日時またはtimezoneが不正です。日時には `+09:00` のような時差を含めてください。",
                ephemeral=True,
            )
            return
        if not created:
            await interaction.response.send_message(
                "予定IDが競合したため登録しませんでした。もう一度実行してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"登録しました: `{meeting.id}` **{meeting.title}**\n開始: <t:{int(meeting.starts_at.timestamp())}:F>",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="show", description="予定の詳細を表示します")
    async def show(self, interaction: discord.Interaction, meeting_id: str) -> None:
        meeting = self.repository.get_meeting(meeting_id.strip().upper())
        if meeting is None or meeting.guild_id != interaction.guild_id:
            await interaction.response.send_message("予定が見つかりません。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"`{meeting.id}` **{meeting.title}**\n<t:{int(meeting.starts_at.timestamp())}:F> ～ "
            f"<t:{int(meeting.ends_at.timestamp())}:t>",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="list", description="このサーバーの今後の予定を表示します")
    async def list_upcoming(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("サーバー内でのみ利用できます。", ephemeral=True)
            return
        meetings = self.repository.list_meetings(interaction.guild_id, datetime.now(UTC), limit)
        if not meetings:
            await interaction.response.send_message("今後の予定はありません。", ephemeral=True)
            return
        lines = [
            f"`{meeting.id}` **{meeting.title}** <t:{int(meeting.starts_at.timestamp())}:F>" for meeting in meetings
        ]
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def cancel_mention_meeting(
        self,
        guild: discord.Guild,
        receipt: ScheduleCancelReceipt,
        *,
        authorization_current: Callable[[], object],
    ) -> bool:
        """確認済みmention取消を、同じrepository/audit契約で確定する。"""

        if (
            receipt.prompt_message_id is None
            or receipt.guild_id != guild.id
            or receipt.digest != schedule_cancel_receipt_digest(replace(receipt, digest=""))
        ):
            return False
        try:
            authorization = authorization_current()
            if hasattr(authorization, "__await__"):
                authorization = await authorization
        except Exception:
            return False
        if (
            not isinstance(authorization, tuple)
            or len(authorization) != 3
            or authorization[0] is not True
            or not isinstance(authorization[1], bool)
            or authorization[2] is not self.repository
        ):
            return False
        can_manage = authorization[1]
        meeting = self.repository.get_meeting(receipt.meeting_id)
        if meeting is None or meeting.guild_id != receipt.guild_id:
            return False
        if meeting.creator_id != receipt.user_id and can_manage is not True:
            return False
        cancelled = self.repository.cancel_meeting(
            receipt.meeting_id,
            receipt.guild_id,
            receipt.user_id,
            can_manage=can_manage,
        )
        if not cancelled:
            return False
        interaction = type(
            "MentionScheduleCancel",
            (),
            {"guild_id": receipt.guild_id, "user": type("User", (), {"id": receipt.user_id})()},
        )()
        if not self._append_audit("scheduling.meeting.cancelled", interaction, {"meeting_id": receipt.meeting_id}):
            logger.warning("scheduling_mention_cancel_audit_failed")
        return True

    @app_commands.command(name="cancel", description="自分が作成した予定を取り消します")
    async def cancel(self, interaction: discord.Interaction, meeting_id: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("サーバー内でのみ利用できます。", ephemeral=True)
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        can_manage = bool(
            permissions is not None
            and (getattr(permissions, "manage_guild", False) or getattr(permissions, "administrator", False))
        )
        cancelled = self.repository.cancel_meeting(
            meeting_id.strip().upper(),
            interaction.guild_id,
            interaction.user.id,
            can_manage=can_manage,
        )
        if cancelled:
            self._append_audit(
                "scheduling.meeting.cancelled",
                interaction,
                {"meeting_id": meeting_id.strip().upper()},
            )
        message = (
            "予定と未送信の通知を取り消しました。"
            if cancelled
            else "予定がないか、権限がないか、通知処理中のため取り消せません。"
        )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="rsvp", description="予定へ出欠を回答します")
    async def rsvp(
        self,
        interaction: discord.Interaction,
        meeting_id: str,
        status: Literal["attending", "tentative", "declined"],
    ) -> None:
        meeting = self.repository.get_meeting(meeting_id.strip().upper())
        if meeting is None or meeting.guild_id != interaction.guild_id:
            await interaction.response.send_message("予定が見つかりません。", ephemeral=True)
            return
        self.repository.save_rsvp(
            RSVP(
                meeting_id=meeting.id,
                user_id=interaction.user.id,
                status=RSVPStatus(status),
                responded_at=datetime.now(UTC),
            )
        )
        labels = {"attending": "参加", "tentative": "未定", "declined": "欠席"}
        await interaction.response.send_message(f"{labels[status]}で回答しました。", ephemeral=True)

    @app_commands.command(name="remind", description="予定の事前通知を予約します")
    async def remind(
        self,
        interaction: discord.Interaction,
        meeting_id: str,
        minutes_before: app_commands.Range[int, 0, 10080],
        direct_message: bool = True,
    ) -> None:
        meeting = self.repository.get_meeting(meeting_id.strip().upper())
        if meeting is None or meeting.guild_id != interaction.guild_id:
            await interaction.response.send_message("予定が見つかりません。", ephemeral=True)
            return
        due_at = meeting.starts_at - timedelta(minutes=minutes_before)
        target = interaction.user.id if direct_message else None
        reminder = Reminder(
            id=f"REM-{secrets.token_hex(6).upper()}",
            meeting_id=meeting.id,
            action_key=reminder_action_key(meeting.id, due_at, target),
            due_at=due_at,
            target_user_id=target,
        )
        created = self.repository.schedule(reminder)
        message = "通知を予約しました。" if created else "同じ通知はすでに予約済みです。"
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="uncertain", description="重複防止で保留中の通知を表示します")
    async def uncertain(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        if interaction.guild_id is None or not await self._is_bot_owner(interaction):
            await interaction.response.send_message("この操作はBot所有者専用です。", ephemeral=True)
            return
        intents = self.repository.list_delivery_intents(guild_id=interaction.guild_id, limit=limit)
        if not intents:
            await interaction.response.send_message("保留中の通知はありません。", ephemeral=True)
            return
        lines = [f"`{intent.reminder_id}` {intent.state.value} / meeting `{intent.meeting_id}`" for intent in intents]
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="resolve", description="Discord側確認後に保留通知を解決します")
    @app_commands.describe(confirmation="Discord側を確認後、RESOLVEと入力")
    async def resolve(
        self,
        interaction: discord.Interaction,
        reminder_id: str,
        resolution: Literal["retry", "sent"],
        confirmation: str,
    ) -> None:
        if interaction.guild_id is None or not await self._is_bot_owner(interaction):
            await interaction.response.send_message("この操作はBot所有者専用です。", ephemeral=True)
            return
        if confirmation != "RESOLVE":
            await interaction.response.send_message(
                "Discord側の送信結果を確認後、confirmationに `RESOLVE` を入力してください。",
                ephemeral=True,
            )
            return
        normalized_id = reminder_id.strip().upper()
        intent = self.repository.get_delivery_intent(normalized_id)
        if (
            intent is None
            or intent.guild_id != interaction.guild_id
            or intent.state is not DeliveryIntentState.UNCERTAIN
        ):
            await interaction.response.send_message(
                "このサーバーに解決可能な保留通知がありません。",
                ephemeral=True,
            )
            return
        if not self._append_audit(
            "scheduling.delivery_resolution.requested",
            interaction,
            {"reminder_id": normalized_id, "resolution": resolution},
        ):
            await interaction.response.send_message(
                "監査ログへ記録できないため、解決操作を停止しました。",
                ephemeral=True,
            )
            return
        resolved = self.repository.resolve_delivery_intent(
            normalized_id,
            DeliveryResolution(resolution),
            datetime.now(UTC),
        )
        message = "解決状態を保存しました。" if resolved else "状態が変化したため解決できません。"
        await interaction.response.send_message(message, ephemeral=True)

    async def _is_bot_owner(self, interaction: discord.Interaction) -> bool:
        checker = getattr(self.bot, "is_owner", None)
        if not callable(checker):
            return False
        try:
            return bool(await checker(interaction.user))
        except Exception:
            return False

    def _append_audit(
        self,
        event: str,
        interaction: discord.Interaction,
        details: dict[str, str],
    ) -> bool:
        database = getattr(self.bot, "database", None)
        append = getattr(database, "append_audit", None)
        if not callable(append):
            return False
        try:
            append(
                event,
                actor_id=interaction.user.id,
                guild_id=interaction.guild_id,
                plugin="scheduling",
                details=details,
            )
        except Exception:
            return False
        return True
