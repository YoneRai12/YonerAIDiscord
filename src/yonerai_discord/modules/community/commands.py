from __future__ import annotations

import asyncio
import re
import sqlite3
import uuid
import weakref

import discord
from discord import app_commands

from yonerai_discord.capabilities import COMMAND_CAPABILITIES

from .domain import ActorPolicy, Poll, Suggestion, SuggestionStatus, Ticket, TicketStatus
from .policy import (
    can_configure_selfroles,
    can_manage_poll,
    can_manage_ticket,
    can_update_suggestion,
    selfrole_permissions_are_safe,
)
from .repository import CommunityRepository
from .views import NO_MENTIONS, PollView, SelfRoleView


def actor_policy(interaction: discord.Interaction) -> ActorPolicy | None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return None
    return member_policy(interaction.guild, interaction.user)


def member_policy(guild: discord.Guild, member: discord.Member) -> ActorPolicy:
    permissions = member.guild_permissions
    return ActorPolicy(
        actor_id=member.id,
        guild_owner_id=guild.owner_id,
        administrator=permissions.administrator,
        manage_guild=permissions.manage_guild,
        manage_channels=permissions.manage_channels,
        manage_roles=permissions.manage_roles,
        manage_messages=permissions.manage_messages,
    )


async def guild_only(interaction: discord.Interaction) -> bool:
    if actor_policy(interaction) is not None:
        return True
    await interaction.response.send_message("サーバー内でのみ利用できます。", ephemeral=True)
    return False


class TicketGroup(app_commands.Group):
    def __init__(self, repository: CommunityRepository, bot: object) -> None:
        super().__init__(name="ticket", description="問い合わせチケットを管理します")
        self.repository = repository
        self.bot = bot
        # close/add/removeを同じticket単位で直列化する。processを跨ぐ事務ではなく、
        # DiscordとSQLiteの同時commitは不可能なため失敗時は補償と未確定表示を用いる。
        self._close_locks: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = weakref.WeakValueDictionary()

    @property
    def _closing_now(self) -> bool:
        return bool(getattr(self.bot, "is_closing", False))

    def _discard_unbound_ticket(self, ticket: Ticket) -> bool:
        try:
            self.repository.delete_unbound_ticket(ticket.guild_id, ticket.id)
        except sqlite3.Error:
            return False
        return True

    async def _central_policy_allows(
        self,
        command_path: str,
        guild: discord.Guild,
        actor: discord.Member,
    ) -> bool:
        capability_id = COMMAND_CAPABILITIES.get(command_path)
        guard = getattr(self.bot, "capability_guard", None)
        evaluate = getattr(guard, "evaluate_fresh_member", None)
        if capability_id is None or evaluate is None:
            return False
        try:
            decision = await evaluate(capability_id, guild=guild, member=actor)
        except Exception:
            return False
        return bool(getattr(decision, "allowed", False))

    @app_commands.command(name="open", description="非公開チケットを作成します")
    async def open(self, interaction: discord.Interaction, subject: str) -> None:
        if not await guild_only(interaction):
            return
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        guild = interaction.guild
        cached_actor = interaction.user
        cached_bot_member = guild.me
        if cached_bot_member is None or not cached_bot_member.guild_permissions.manage_channels:
            await interaction.response.send_message("Botにチャンネル管理権限がありません。", ephemeral=True)
            return
        try:
            ticket = Ticket(uuid.uuid4().hex, guild.id, cached_actor.id, subject)
        except ValueError:
            await interaction.response.send_message("件名は1〜200文字で入力してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if self._closing_now:
            await interaction.followup.send(
                "Botは停止処理中のため、チケットを作成していません。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        try:
            fresh_actor = await guild.fetch_member(cached_actor.id)
            fresh_bot_member = await guild.fetch_member(cached_bot_member.id)
        except discord.HTTPException:
            await interaction.followup.send(
                "実行者またはBotの現在状態を確認できないため、チケットを作成していません。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        if fresh_actor.id != ticket.owner_id or fresh_bot_member.id != cached_bot_member.id:
            await interaction.followup.send(
                "実行者またはBotの現在状態を確認できないため、チケットを作成していません。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        if not await self._central_policy_allows("ticket open", guild, fresh_actor):
            await interaction.followup.send(
                "操作待機中に権限または機能設定が変更されたため、チケットを作成していません。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        if self._closing_now:
            await interaction.followup.send(
                "Botは停止処理中のため、チケットを作成していません。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        if not fresh_bot_member.guild_permissions.manage_channels:
            await interaction.followup.send(
                "操作待機中にBotの権限が変更されたため、チケットを作成していません。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return

        try:
            created = self.repository.create_ticket(ticket)
        except sqlite3.Error:
            await interaction.followup.send(
                "チケットの保存先に接続できません。時間をおいて再試行してください。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        if not created:
            await interaction.followup.send(
                "すでに未完了のチケットがあります。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        # DB insertからDiscord副作用まではawaitを挟まない。別threadからshutdownが
        # 開始された場合も、直前の同期確認で未bind行を補償して停止する。
        if self._closing_now:
            cleaned = self._discard_unbound_ticket(ticket)
            suffix = "" if cleaned else " 未完了記録の補償に失敗したため、管理者の確認が必要です。"
            await interaction.followup.send(
                f"Botは停止処理中のため、チケットを作成していません。{suffix}",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            fresh_actor: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            fresh_bot_member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
            ),
        }
        safe_name = re.sub(r"[^a-z0-9-]", "-", subject.lower())[:30].strip("-") or "support"
        try:
            channel = await guild.create_text_channel(
                f"ticket-{safe_name}-{ticket.id[:6]}",
                overwrites=overwrites,
                topic=f"Ticket {ticket.id} / owner {ticket.owner_id}",
                reason=f"ticket open {ticket.id}",
            )
            if not self.repository.bind_ticket_channel(guild.id, ticket.id, channel.id):
                await channel.delete(reason=f"ticket database bind failed {ticket.id}")
                self._discard_unbound_ticket(ticket)
                await interaction.followup.send(
                    "チケットの保存に失敗しました。時間をおいて再試行してください。", ephemeral=True
                )
                return
            await channel.send(
                f"チケットを作成しました。件名: {ticket.subject}\nID: `{ticket.id}`",
                allowed_mentions=NO_MENTIONS,
            )
        except (discord.HTTPException, sqlite3.Error):
            cleaned = self._discard_unbound_ticket(ticket)
            suffix = "" if cleaned else " 未完了記録の補償に失敗したため、管理者の確認が必要です。"
            await interaction.followup.send(
                f"チャンネル作成に失敗しました。Botの権限を確認してください。{suffix}", ephemeral=True
            )
            return
        await interaction.followup.send(f"チケットを作成しました: {channel.mention}", ephemeral=True)

    def _current(self, interaction: discord.Interaction) -> Ticket | None:
        if interaction.guild_id is None or interaction.channel_id is None:
            return None
        return self.repository.ticket_by_channel(interaction.guild_id, interaction.channel_id)

    @app_commands.command(name="close", description="現在のチケットを閉じます")
    async def close(self, interaction: discord.Interaction) -> None:
        policy = actor_policy(interaction)
        try:
            ticket = self._current(interaction)
        except sqlite3.Error:
            await interaction.response.send_message(
                "チケットの現在状態を確認できないため、何も変更していません。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        channel = interaction.channel
        guild = interaction.guild
        if (
            policy is None
            or ticket is None
            or guild is None
            or not isinstance(channel, discord.TextChannel)
            or ticket.channel_id != channel.id
        ):
            await interaction.response.send_message("ここはチケットチャンネルではありません。", ephemeral=True)
            return
        if not can_manage_ticket(policy, ticket.owner_id):
            await interaction.response.send_message("このチケットを閉じる権限がありません。", ephemeral=True)
            return
        bot_member = guild.me
        if bot_member is None or not channel.permissions_for(bot_member).manage_channels:
            await interaction.response.send_message(
                "Botにこのチャンネルを管理する権限がないため、チケットは閉じていません。", ephemeral=True
            )
            return
        if ticket.status is not TicketStatus.OPEN:
            await interaction.response.send_message("このチケットはすでに閉じています。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        lock = self._close_locks.setdefault((ticket.guild_id, ticket.id), asyncio.Lock())
        async with lock:
            try:
                fresh = self.repository.ticket_by_channel(ticket.guild_id, channel.id)
            except sqlite3.Error:
                await interaction.followup.send(
                    "チケットの現在状態を確認できないため、何も変更していません。時間をおいて再試行してください。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            if fresh is None or fresh.id != ticket.id or fresh.status is not TicketStatus.OPEN:
                await interaction.followup.send(
                    "このチケットはすでに閉じられたか、状態が変更されています。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return

            try:
                participant_ids = self.repository.ticket_participant_ids(ticket.guild_id, ticket.id)
                fetched_channel = await guild.fetch_channel(channel.id)
                if not isinstance(fetched_channel, discord.TextChannel) or fetched_channel.id != channel.id:
                    raise TypeError("ticket channel changed")
                original_name = fetched_channel.name
                # REST再取得後のoverwriteを基準にし、操作待機中の外部変更を
                # cached snapshotで消しにくくする。edit後の他processとの競合は検出不能。
                original_overwrites = {
                    target: discord.PermissionOverwrite.from_pair(*overwrite.pair())
                    for target, overwrite in fetched_channel.overwrites.items()
                }
                cached_bot_member = guild.me
                if cached_bot_member is None:
                    raise TypeError("bot member unavailable")
                bot_member = await guild.fetch_member(cached_bot_member.id)
                closed_overwrites = await self._closed_overwrites(
                    guild,
                    bot_member,
                    original_overwrites,
                    {ticket.owner_id, *participant_ids},
                )
            except (discord.HTTPException, sqlite3.Error, TypeError):
                await interaction.followup.send(
                    "参加者、チャンネルまたは保存状態を確認できないため、チケットは閉じていません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return

            # 上のDB/REST awaitが全て終わった後にactorをREST再取得し、
            # native permissionとdynamic RBACの両方をDiscord editの直前に再評価する。
            try:
                fresh_actor = await guild.fetch_member(policy.actor_id)
            except discord.HTTPException:
                await interaction.followup.send(
                    "実行者の現在権限を確認できないため、チケットは閉じていません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            if not await self._central_policy_allows("ticket close", guild, fresh_actor):
                await interaction.followup.send(
                    "必要権限または機能設定が変更されたため、チケットは閉じていません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            fresh_policy = member_policy(guild, fresh_actor)
            if not can_manage_ticket(fresh_policy, fresh.owner_id):
                await interaction.followup.send(
                    "操作待機中に権限が変更されたため、チケットは閉じていません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            if not fetched_channel.permissions_for(bot_member).manage_channels:
                await interaction.followup.send(
                    "操作待機中にBotの権限が変更されたため、チケットは閉じていません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            try:
                await fetched_channel.edit(
                    name=self._closed_channel_name(original_name),
                    overwrites=closed_overwrites,
                    reason=f"ticket close {ticket.id}",
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    "Discord側の更新完了を確認できなかったため、DBは閉じていません。チャンネル状態とBot権限を確認してください。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return

            try:
                closed = self.repository.close_ticket(ticket.guild_id, ticket.id)
            except sqlite3.Error:
                closed = False
            try:
                current = None if closed else self.repository.ticket_by_channel(ticket.guild_id, fetched_channel.id)
            except sqlite3.Error:
                await interaction.followup.send(
                    "チャンネルは読み取り専用にしましたが、DBの確定状態を確認できません。安全のため変更を維持しています。管理者へ連絡してください。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return

            # Another process may have committed the same close between the REST update and this CAS.
            if closed or (current is not None and current.id == ticket.id and current.status is TicketStatus.CLOSED):
                await interaction.followup.send("チケットを閉じました。", ephemeral=True, allowed_mentions=NO_MENTIONS)
                return

            compensated = await self._restore_ticket_channel(
                fetched_channel,
                ticket,
                original_name=original_name,
                original_overwrites=original_overwrites,
            )
            if compensated:
                state = "チケットは開いたままです。" if current is not None else "DBのcloseは確認されていません。"
                await interaction.followup.send(
                    f"保存の確定に失敗したため、チャンネル変更を元に戻しました。{state}",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
            else:
                await interaction.followup.send(
                    "保存の確定とチャンネル変更の復元に失敗しました。DBは未確定で、チャンネル状態も確認が必要です。管理者へ連絡してください。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )

    @staticmethod
    def _closed_channel_name(name: str) -> str:
        return name[:100] if name.startswith("closed-") else f"closed-{name}"[:100]

    async def _closed_overwrites(
        self,
        guild: discord.Guild,
        bot_member: discord.Member,
        original: dict[discord.Role | discord.Member, discord.PermissionOverwrite],
        participant_ids: set[int],
    ) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
        overwrites = {
            target: discord.PermissionOverwrite.from_pair(*overwrite.pair()) for target, overwrite in original.items()
        }
        for target, overwrite in overwrites.items():
            if not (isinstance(target, discord.Member) and target.id == bot_member.id):
                self._make_read_only(overwrite)

        default_overwrite = overwrites.setdefault(guild.default_role, discord.PermissionOverwrite())
        self._make_read_only(default_overwrite)

        bot_overwrite = overwrites.get(bot_member, discord.PermissionOverwrite())
        bot_overwrite.view_channel = True
        bot_overwrite.send_messages = True
        bot_overwrite.send_messages_in_threads = True
        bot_overwrite.manage_channels = True
        overwrites[bot_member] = bot_overwrite

        known_members = {target.id: target for target in overwrites if isinstance(target, discord.Member)}
        for member_id in sorted(participant_ids):
            if member_id == bot_member.id:
                continue
            member = known_members.get(member_id) or guild.get_member(member_id)
            if member is None:
                try:
                    member = await guild.fetch_member(member_id)
                except discord.NotFound:
                    continue
            overwrite = overwrites.get(member, discord.PermissionOverwrite())
            self._make_read_only(overwrite)
            overwrites[member] = overwrite
        return overwrites

    @staticmethod
    def _make_read_only(overwrite: discord.PermissionOverwrite) -> None:
        overwrite.add_reactions = False
        overwrite.send_messages = False
        overwrite.send_messages_in_threads = False
        overwrite.send_tts_messages = False
        overwrite.send_voice_messages = False
        overwrite.send_polls = False
        overwrite.create_public_threads = False
        overwrite.create_private_threads = False
        overwrite.manage_threads = False
        overwrite.use_application_commands = False
        overwrite.use_external_apps = False

    @staticmethod
    async def _restore_ticket_channel(
        channel: discord.TextChannel,
        ticket: Ticket,
        *,
        original_name: str,
        original_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite],
    ) -> bool:
        try:
            await channel.edit(
                name=original_name,
                overwrites=original_overwrites,
                reason=f"ticket close rollback {ticket.id}",
            )
        except discord.HTTPException:
            return False
        return True

    @app_commands.command(name="add", description="現在のチケットへメンバーを追加します")
    async def add(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._participant(interaction, member, True)

    @app_commands.command(name="remove", description="現在のチケットからメンバーを外します")
    async def remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._participant(interaction, member, False)

    async def _participant(self, interaction: discord.Interaction, member: discord.Member, add: bool) -> None:
        policy = actor_policy(interaction)
        try:
            ticket = self._current(interaction)
        except sqlite3.Error:
            await interaction.response.send_message(
                "チケットの現在状態を確認できないため、何も変更していません。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        guild = interaction.guild
        channel = interaction.channel
        if (
            policy is None
            or ticket is None
            or guild is None
            or not isinstance(channel, discord.TextChannel)
            or ticket.channel_id != channel.id
        ):
            await interaction.response.send_message("ここはチケットチャンネルではありません。", ephemeral=True)
            return
        if not can_manage_ticket(policy, ticket.owner_id):
            await interaction.response.send_message("参加者を変更する権限がありません。", ephemeral=True)
            return
        if not add and member.id == ticket.owner_id:
            await interaction.response.send_message("チケット作成者は参加者から外せません。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        lock = self._close_locks.setdefault((ticket.guild_id, ticket.id), asyncio.Lock())
        async with lock:
            try:
                fresh_ticket = self.repository.ticket_by_channel(ticket.guild_id, channel.id)
            except sqlite3.Error:
                await interaction.followup.send(
                    "チケットの現在状態を確認できないため、何も変更していません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            if fresh_ticket is None or fresh_ticket.id != ticket.id or fresh_ticket.status is not TicketStatus.OPEN:
                await interaction.followup.send(
                    "チケットが閉じられたか状態が変更されたため、参加者は変更していません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            try:
                fetched_channel = await guild.fetch_channel(channel.id)
                if not isinstance(fetched_channel, discord.TextChannel) or fetched_channel.id != channel.id:
                    raise TypeError("ticket channel changed")
                fresh_target = await guild.fetch_member(member.id)
                cached_bot_member = guild.me
                if cached_bot_member is None:
                    raise TypeError("bot member unavailable")
                bot_member = await guild.fetch_member(cached_bot_member.id)
                fresh_actor = await guild.fetch_member(policy.actor_id)
            except (discord.HTTPException, TypeError):
                await interaction.followup.send(
                    "実行者、対象者またはチャンネルを再確認できないため、何も変更していません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            command_path = "ticket add" if add else "ticket remove"
            if not await self._central_policy_allows(command_path, guild, fresh_actor):
                await interaction.followup.send(
                    "必要権限または機能設定が変更されたため、参加者は変更していません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            fresh_policy = member_policy(guild, fresh_actor)
            if not can_manage_ticket(fresh_policy, fresh_ticket.owner_id):
                await interaction.followup.send(
                    "操作待機中に権限が変更されたため、参加者は変更していません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            if not fetched_channel.permissions_for(bot_member).manage_channels:
                await interaction.followup.send(
                    "Botの現在権限を確認できないため、参加者は変更していません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            if not add and fresh_target.id == fresh_ticket.owner_id:
                await interaction.followup.send(
                    "チケット作成者は参加者から外せません。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            try:
                changed = (
                    self.repository.add_ticket_participant(ticket.guild_id, ticket.id, fresh_target.id)
                    if add
                    else self.repository.remove_ticket_participant(ticket.guild_id, ticket.id, fresh_target.id)
                )
            except sqlite3.Error:
                await interaction.followup.send(
                    "チケットの保存先に接続できません。時間をおいて再試行してください。",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            if not changed:
                await interaction.followup.send(
                    "状態に変更はありません。", ephemeral=True, allowed_mentions=NO_MENTIONS
                )
                return
            try:
                overwrite = (
                    discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
                    if add
                    else None
                )
                await fetched_channel.set_permissions(
                    fresh_target,
                    overwrite=overwrite,
                    reason=f"ticket participant {ticket.id}",
                )
            except discord.HTTPException:
                # DiscordとSQLiteは分散transactionでない。同一processのcloseはlockで排他し、
                # Discord反映失敗時はDBを可能な限り補償する。
                compensated = False
                try:
                    compensated = (
                        self.repository.remove_ticket_participant(ticket.guild_id, ticket.id, fresh_target.id)
                        if add
                        else self.repository.add_ticket_participant(ticket.guild_id, ticket.id, fresh_target.id)
                    )
                except sqlite3.Error:
                    pass
                suffix = "" if compensated else "（DB未確定・要監査）"
                await interaction.followup.send(
                    f"チャンネル権限の反映に失敗しました。変更の取り消しを試行しました{suffix}",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            await interaction.followup.send("参加者を更新しました。", ephemeral=True, allowed_mentions=NO_MENTIONS)

    @app_commands.command(name="transcript-info", description="会話履歴の扱いを確認します")
    async def transcript_info(self, interaction: discord.Interaction) -> None:
        policy = actor_policy(interaction)
        ticket = self._current(interaction)
        if policy is None or ticket is None or not can_manage_ticket(policy, ticket.owner_id):
            await interaction.response.send_message("この情報を確認する権限がありません。", ephemeral=True)
            return
        await interaction.response.send_message(
            "このBotはチケット本文を外部送信・自動保存しません。履歴はDiscord上のこのチャンネルが正本です。",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )


class PollGroup(app_commands.Group):
    def __init__(self, repository: CommunityRepository) -> None:
        super().__init__(name="poll", description="投票を作成・集計します")
        self.repository = repository

    @app_commands.command(name="create", description="選択式投票を作成します。選択肢は | で区切ります")
    async def create(self, interaction: discord.Interaction, question: str, options: str) -> None:
        if not await guild_only(interaction):
            return
        assert interaction.guild_id is not None
        try:
            poll = Poll(
                uuid.uuid4().hex, interaction.guild_id, interaction.user.id, question, tuple(options.split("|"))
            )
        except ValueError:
            await interaction.response.send_message(
                "質問と、重複しない2〜5個の選択肢を `|` 区切りで指定してください。", ephemeral=True
            )
            return
        self.repository.create_poll(poll)
        view = PollView(self.repository, poll.id, poll.options)
        await interaction.response.send_message(
            f"**投票**: {poll.question}\n投票ID: `{poll.id}`", view=view, allowed_mentions=NO_MENTIONS
        )
        message = await interaction.original_response()
        self.repository.bind_poll_message(poll.guild_id, poll.id, message.channel.id, message.id)

    @app_commands.command(name="close", description="投票を終了します")
    async def close(self, interaction: discord.Interaction, poll_id: str) -> None:
        policy = actor_policy(interaction)
        poll = self.repository.get_poll(interaction.guild_id or 0, poll_id)
        if policy is None or poll is None:
            await interaction.response.send_message("投票が見つかりません。", ephemeral=True)
            return
        if not can_manage_poll(policy, poll.creator_id):
            await interaction.response.send_message("この投票を終了する権限がありません。", ephemeral=True)
            return
        changed = self.repository.close_poll(poll.guild_id, poll.id)
        await interaction.response.send_message(
            "投票を終了しました。" if changed else "すでに終了しています。", ephemeral=True
        )

    @app_commands.command(name="results", description="投票結果を表示します")
    async def results(self, interaction: discord.Interaction, poll_id: str) -> None:
        results = self.repository.poll_results(interaction.guild_id or 0, poll_id)
        if not results:
            await interaction.response.send_message("投票が見つかりません。", ephemeral=True)
            return
        lines = [f"{item.option}: {item.votes}票" for item in results]
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)


class SuggestGroup(app_commands.Group):
    def __init__(self, repository: CommunityRepository) -> None:
        super().__init__(name="suggest", description="提案を登録・確認します")
        self.repository = repository

    @app_commands.command(name="create", description="提案を登録します")
    async def create(self, interaction: discord.Interaction, content: str) -> None:
        if not await guild_only(interaction):
            return
        try:
            suggestion = Suggestion(uuid.uuid4().hex, interaction.guild_id or 0, interaction.user.id, content)
        except ValueError:
            await interaction.response.send_message("提案は1〜1500文字で入力してください。", ephemeral=True)
            return
        self.repository.create_suggestion(suggestion)
        await interaction.response.send_message(f"提案を登録しました。ID: `{suggestion.id}`", ephemeral=True)

    @app_commands.command(name="status", description="提案状態の確認、または管理者による更新を行います")
    async def status(self, interaction: discord.Interaction, suggestion_id: str, new_status: str | None = None) -> None:
        policy = actor_policy(interaction)
        suggestion = self.repository.get_suggestion(interaction.guild_id or 0, suggestion_id)
        if policy is None or suggestion is None:
            await interaction.response.send_message("提案が見つかりません。", ephemeral=True)
            return
        if new_status is not None:
            if not can_update_suggestion(policy):
                await interaction.response.send_message("提案状態を更新する権限がありません。", ephemeral=True)
                return
            try:
                status = SuggestionStatus(new_status.strip().lower())
            except ValueError:
                await interaction.response.send_message(
                    "状態は pending/accepted/rejected/implemented から選んでください。", ephemeral=True
                )
                return
            self.repository.update_suggestion(suggestion.guild_id, suggestion.id, status)
            suggestion = self.repository.get_suggestion(suggestion.guild_id, suggestion.id) or suggestion
        await interaction.response.send_message(
            f"提案 `{suggestion.id}` の状態: {suggestion.status.value}", ephemeral=True
        )


class SelfRoleGroup(app_commands.Group):
    def __init__(self, repository: CommunityRepository) -> None:
        super().__init__(name="selfrole", description="自分で付け外しできるロールを管理します")
        self.repository = repository

    @app_commands.command(name="panel", description="セルフロール選択パネルを設置します")
    async def panel(self, interaction: discord.Interaction) -> None:
        policy = actor_policy(interaction)
        if policy is None:
            await interaction.response.send_message("サーバー内でのみ利用できます。", ephemeral=True)
            return
        if not can_configure_selfroles(policy):
            await interaction.response.send_message("セルフロールのパネルを設置する権限がありません。", ephemeral=True)
            return
        role_ids = self.repository.selfroles(interaction.guild_id or 0)
        if not role_ids:
            await interaction.response.send_message("セルフロールが未設定です。", ephemeral=True)
            return
        await interaction.response.send_message(
            "ボタンでロールを付け外しできます。",
            view=SelfRoleView(self.repository, interaction.guild_id or 0, role_ids),
            allowed_mentions=NO_MENTIONS,
        )

    @app_commands.command(name="add", description="セルフロール候補を追加します")
    async def add(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self._configure(interaction, role, True)

    @app_commands.command(name="remove", description="セルフロール候補を削除します")
    async def remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self._configure(interaction, role, False)

    async def _configure(self, interaction: discord.Interaction, role: discord.Role, add: bool) -> None:
        policy = actor_policy(interaction)
        if policy is None or not can_configure_selfroles(policy):
            await interaction.response.send_message("セルフロールを設定する権限がありません。", ephemeral=True)
            return
        guild = interaction.guild
        assert guild is not None
        if role.guild.id != guild.id:
            await interaction.response.send_message("このサーバーのロールを指定してください。", ephemeral=True)
            return
        bot_member = guild.me
        role_permissions = role.permissions
        unsafe_permissions = not selfrole_permissions_are_safe(
            administrator=role_permissions.administrator,
            manage_guild=role_permissions.manage_guild,
            manage_roles=role_permissions.manage_roles,
            manage_channels=role_permissions.manage_channels,
            ban_members=role_permissions.ban_members,
            kick_members=role_permissions.kick_members,
            moderate_members=role_permissions.moderate_members,
        )
        above_actor = (
            isinstance(interaction.user, discord.Member)
            and not policy.is_owner_or_admin
            and role >= interaction.user.top_role
        )
        if add and (
            role.is_default()
            or role.managed
            or bot_member is None
            or role >= bot_member.top_role
            or unsafe_permissions
            or above_actor
        ):
            await interaction.response.send_message("Botが安全に操作できないロールです。", ephemeral=True)
            return
        changed = (
            self.repository.add_selfrole(guild.id, role.id, interaction.user.id)
            if add
            else self.repository.remove_selfrole(guild.id, role.id)
        )
        await interaction.response.send_message(
            "設定を更新しました。" if changed else "状態に変更はありません。", ephemeral=True
        )
