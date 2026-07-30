from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import discord
from discord import app_commands

from yonerai_discord.capabilities import EVENT_CAPABILITIES

from .detectors import LinkDetector, MentionDetector
from .domain import (
    DetectionContext,
    EventKind,
    GuildAutomodConfig,
    MessageEvent,
    Policy,
    Severity,
)
from .pipeline import DetectionEngine, PolicyDecider
from .repository import SqliteAutomodRepository

EVENT_CAPABILITY_KEYS = {
    EventKind.MESSAGE_CREATE: "automod_message_create",
    EventKind.MESSAGE_EDIT: "automod_message_edit",
}
REPORT_ONLY_CONFIRMATION = "REPORT_ONLY"


async def _reply(interaction: discord.Interaction, message: str) -> None:
    await interaction.response.send_message(
        message[:2_000],
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class AutomodGroup(app_commands.Group):
    def __init__(self, repository: SqliteAutomodRepository, bot: Any) -> None:
        super().__init__(name="automod", description="report-only AutoModの設定")
        self.repository = repository
        self.bot = bot

    @app_commands.command(name="status", description="AutoModの安全設定と実行条件を表示します")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await _reply(interaction, "サーバー内で実行してください。")
            return
        config = self.repository.get(interaction.guild_id)
        listener = DiscordAutomodListeners(self.repository, self.bot)
        create_enabled = listener.capability_enabled(EventKind.MESSAGE_CREATE, interaction.guild_id)
        edit_enabled = listener.capability_enabled(EventKind.MESSAGE_EDIT, interaction.guild_id)
        await _reply(interaction, _render_status(config, self.bot, create_enabled, edit_enabled))

    @app_commands.command(name="channel", description="redacted判定の送信先を設定します")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or interaction.guild_id is None:
            await _reply(interaction, "サーバー内で実行してください。")
            return
        if channel is None:
            config = self.repository.set_report_channel(interaction.guild_id, None)
            await _reply(interaction, "送信先を解除し、AutoModを停止しました。")
            return
        if channel.guild.id != guild.id:
            await _reply(interaction, "同じサーバーのチャンネルを指定してください。")
            return
        me = guild.me
        permissions = channel.permissions_for(me) if me is not None else None
        if not (permissions is not None and permissions.view_channel and permissions.send_messages):
            await _reply(interaction, "Botにこのチャンネルの表示・送信権限が必要です。")
            return
        config = self.repository.set_report_channel(interaction.guild_id, channel.id)
        await _reply(
            interaction,
            f"送信先を <#{config.report_channel_id}> に設定しました。本文は保存・送信しません。",
        )

    @app_commands.command(name="policy", description="report-only検出を有効または停止します")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def policy(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        confirmation: str = "",
    ) -> None:
        if interaction.guild_id is None:
            await _reply(interaction, "サーバー内で実行してください。")
            return
        if enabled and confirmation.strip() != REPORT_ONLY_CONFIRMATION:
            await _reply(
                interaction,
                f"有効化には confirmation へ {REPORT_ONLY_CONFIRMATION} を正確に入力してください。",
            )
            return
        try:
            config = self.repository.set_enabled(interaction.guild_id, enabled)
        except ValueError:
            await _reply(interaction, "先に /automod channel で送信先を設定してください。")
            return
        state = "有効" if config.enabled else "停止"
        await _reply(
            interaction,
            f"AutoModを{state}にしました。モードはreport-only固定で、自動削除・timeout・banは行いません。",
        )


class DiscordAutomodListeners:
    """本文を永続化せず、検出結果だけを監査先へ送るadapter。"""

    def __init__(self, repository: SqliteAutomodRepository, bot: Any) -> None:
        self.repository = repository
        self.bot = bot
        self.detector = DetectionEngine((MentionDetector(), LinkDetector()))
        self.decider = PolicyDecider()
        # allowlistを管理しない最小構成で通常URLを大量通知しないよう、
        # Discord招待・大量mentionなどHIGH以上だけを報告する。
        self.policy = Policy(minimum_severity=Severity.HIGH)

    async def on_message(self, message: discord.Message) -> None:
        await self._process(message, EventKind.MESSAGE_CREATE)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content == after.content:
            return
        await self._process(after, EventKind.MESSAGE_EDIT)

    async def _process(self, message: discord.Message, kind: EventKind) -> None:
        if self._ignore(message) or not self._global_ready():
            return
        guild = message.guild
        if guild is None:
            return
        config = self.repository.get(guild.id)
        if not config.enabled or config.report_channel_id is None:
            return
        event = _message_event(message, kind)
        findings = self.detector.detect(event, DetectionContext(), self.policy)
        decision = self.decider.decide(event, findings, self.policy)
        if not decision.should_act:
            return
        report_channel = guild.get_channel(config.report_channel_id)
        send = getattr(report_channel, "send", None)
        if report_channel is None or not callable(send):
            return
        me = guild.me
        permissions_for = getattr(report_channel, "permissions_for", None)
        permissions = permissions_for(me) if me is not None and callable(permissions_for) else None
        if not (
            permissions is not None
            and bool(getattr(permissions, "view_channel", False))
            and bool(getattr(permissions, "send_messages", False))
        ):
            return
        report = _render_report(event, decision.reason, decision.severity.name)
        event_revision = _event_revision(event, report_channel.id)
        # Discordへの唯一の副作用の直前に、中央Registryを再評価する。
        if not self.event_allowed(kind, message):
            return
        try:
            await send(report, allowed_mentions=discord.AllowedMentions.none())
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
                    surface=f"automod.{kind.value}",
                    guild_id=guild.id,
                    event_id=message.id,
                    actor_id=message.author.id,
                    revision=event_revision,
                )
            except asyncio.CancelledError:
                raise
            except Exception as terminal_error:
                raise send_error from terminal_error

    def capability_enabled(self, kind: EventKind, guild_id: int) -> bool:
        event_name = EVENT_CAPABILITY_KEYS[kind]
        capability_id = EVENT_CAPABILITIES.get(event_name)
        registry = getattr(self.bot, "capability_registry", None)
        if capability_id is None or registry is None:
            return False
        try:
            return bool(registry.capability_status(capability_id, guild_id).executable)
        except (KeyError, TypeError, ValueError):
            return False

    def event_allowed(self, kind: EventKind, message: discord.Message) -> bool:
        event_name = EVENT_CAPABILITY_KEYS[kind]
        capability_id = EVENT_CAPABILITIES.get(event_name)
        guard = getattr(self.bot, "capability_guard", None)
        checker = getattr(guard, "event_allowed", None)
        if capability_id is None or not callable(checker) or message.guild is None:
            return False
        return bool(
            checker(
                capability_id,
                surface=event_name,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                event_id=message.id,
                user_id=message.author.id,
                author_is_bot=bool(getattr(message.author, "bot", False)),
            )
        )

    def _global_ready(self) -> bool:
        settings = getattr(self.bot, "settings", None)
        intents = getattr(self.bot, "intents", None)
        return bool(
            settings is not None
            and getattr(settings, "automod_enabled", False) is True
            and intents is not None
            and getattr(intents, "guild_messages", False)
            and getattr(intents, "message_content", False)
        )

    @staticmethod
    def _ignore(message: discord.Message) -> bool:
        return bool(
            message.guild is None
            or getattr(message, "webhook_id", None) is not None
            or getattr(message.author, "bot", False)
            or not isinstance(getattr(message, "content", None), str)
            or not message.content
            or getattr(message, "is_system", lambda: False)()
        )


def _message_event(message: discord.Message, kind: EventKind) -> MessageEvent:
    author_roles = frozenset(role.id for role in getattr(message.author, "roles", ()) if isinstance(role.id, int))
    return MessageEvent(
        kind=kind,
        guild_id=message.guild.id,  # type: ignore[union-attr]
        channel_id=message.channel.id,
        message_id=message.id,
        author_id=message.author.id,
        content=message.content,
        author_role_ids=author_roles,
        mention_user_ids=frozenset(item.id for item in message.mentions),
        mention_role_ids=frozenset(item.id for item in message.role_mentions),
        mentions_everyone=message.mention_everyone,
        occurred_at=message.edited_at or message.created_at,
    )


def _event_revision(event: MessageEvent, report_channel_id: int) -> str:
    values = (
        event.kind.value,
        event.guild_id,
        event.channel_id,
        event.message_id,
        event.author_id,
        report_channel_id,
        event.occurred_at.isoformat(),
        event.content_fingerprint,
    )
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _render_report(event: MessageEvent, reason: str, severity: str) -> str:
    rule_names = {
        "mass_mention": "大量メンション",
        "unapproved_invite": "未承認のDiscord招待",
        "unapproved_link": "未承認リンク",
    }
    redacted_reason = ", ".join(rule_names.get(item.strip(), "検出ルール") for item in reason.split(","))
    return "\n".join(
        (
            "🛡️ AutoMod report-only（本文非保存）",
            f"判定: {severity} / {redacted_reason[:120]}",
            f"種別: {event.kind.value} / チャンネル: <#{event.channel_id}>",
            f"Message ID: {event.message_id} / 投稿者 ID: {event.author_id}",
            "自動処置: なし（人間による確認待ち）",
        )
    )[:800]


def _render_status(
    config: GuildAutomodConfig,
    bot: Any,
    create_capability: bool,
    edit_capability: bool,
) -> str:
    settings = getattr(bot, "settings", None)
    intents = getattr(bot, "intents", None)
    global_enabled = bool(settings and getattr(settings, "automod_enabled", False))
    message_events = bool(intents and getattr(intents, "guild_messages", False))
    message_content = bool(intents and getattr(intents, "message_content", False))
    channel = f"<#{config.report_channel_id}>" if config.report_channel_id else "未設定"
    return "\n".join(
        (
            "AutoMod 状態",
            f"サーバー設定: {'有効' if config.enabled else '停止'}",
            f"モード: {config.mode.value}（強制処置なし）",
            "検出policy: HIGH以上（Discord招待・大量mention）",
            f"送信先: {channel}",
            f"AUTOMOD_ENABLED: {'ON' if global_enabled else 'OFF'}",
            f"Messages intent: {'ON' if message_events else 'OFF'}",
            f"Message Content intent: {'ON' if message_content else 'OFF（Developer Portal側も人間の有効化が必要）'}",
            f"中央capability create/edit: {'ON' if create_capability else 'OFF'} / {'ON' if edit_capability else 'OFF'}",
            "本文はDB・監査送信・logに保存しません。",
        )
    )
