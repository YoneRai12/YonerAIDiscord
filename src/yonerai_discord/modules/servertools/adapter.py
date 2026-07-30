from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import discord
from discord import app_commands

from yonerai_discord.capabilities import COMMAND_CAPABILITIES, EVENT_CAPABILITIES

from .domain import (
    ActorPermissions,
    GuildServerConfig,
    HierarchyContext,
    PolicyViolation,
    ServerAction,
)
from .policy import (
    require_action_permission,
    require_member_hierarchy,
    require_role_hierarchy,
    validate_announcement,
)
from .rendering import (
    MemberTemplateValues,
    render_member_message,
    render_message_delete,
    render_message_edit,
)
from .repository import SqliteServerToolsRepository


logger = logging.getLogger(__name__)

_FRESH_REAUTH_DENIED = "現在の機能設定・権限・対象を再確認できないため、何も変更していません。"


@dataclass(frozen=True, slots=True)
class ServerAnnouncementReceipt:
    guild_id: int
    user_id: int
    source_channel_id: int
    source_message_id: int
    prompt_message_id: int | None
    target_channel_id: int
    content: str = field(repr=False)
    content_sha256: str = ""
    allow_everyone: bool = False
    reason: str = field(default="", repr=False)
    digest: str = ""


def announcement_receipt_digest(receipt: ServerAnnouncementReceipt) -> str:
    values = (
        receipt.guild_id,
        receipt.user_id,
        receipt.source_channel_id,
        receipt.source_message_id,
        receipt.prompt_message_id,
        receipt.target_channel_id,
        receipt.content,
        receipt.allow_everyone,
        receipt.reason,
    )
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _valid_announcement_receipt(receipt: ServerAnnouncementReceipt, guild: discord.Guild) -> bool:
    return (
        receipt.prompt_message_id is not None
        and receipt.guild_id == guild.id
        and receipt.digest == announcement_receipt_digest(replace(receipt, digest=""))
        and receipt.content_sha256 == hashlib.sha256(receipt.content.encode("utf-8")).hexdigest()
    )


@dataclass(frozen=True, slots=True)
class FreshServerContext:
    guild: discord.Guild
    actor: discord.Member
    bot_member: discord.Member
    channel: discord.TextChannel | None = None
    target: discord.Member | None = None
    role: discord.Role | None = None
    log_channel: discord.TextChannel | None = None


def _member_permissions(
    member: discord.Member,
    channel: discord.abc.GuildChannel | None = None,
) -> ActorPermissions:
    permissions = channel.permissions_for(member) if channel is not None else member.guild_permissions
    return ActorPermissions(
        administrator=bool(getattr(permissions, "administrator", False)),
        manage_guild=bool(getattr(permissions, "manage_guild", False)),
        manage_channels=bool(getattr(permissions, "manage_channels", False)),
        manage_nicknames=bool(getattr(permissions, "manage_nicknames", False)),
        manage_roles=bool(getattr(permissions, "manage_roles", False)),
    )


def _actor_permissions(
    interaction: discord.Interaction, channel: discord.abc.GuildChannel | None = None
) -> ActorPermissions:
    if isinstance(interaction.user, discord.Member):
        return _member_permissions(interaction.user, channel)
    return ActorPermissions()


def _hierarchy_for(
    guild: discord.Guild,
    actor: discord.Member,
    bot_member: discord.Member,
    member: discord.Member,
    role: discord.Role | None = None,
) -> HierarchyContext:
    return HierarchyContext(
        actor_top_role=actor.top_role.position,
        bot_top_role=bot_member.top_role.position,
        target_member_top_role=member.top_role.position,
        target_role_position=role.position if role is not None else None,
        target_is_owner=member.id == guild.owner_id,
        actor_is_owner=actor.id == guild.owner_id,
    )


def _hierarchy(
    interaction: discord.Interaction, member: discord.Member, role: discord.Role | None = None
) -> HierarchyContext:
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member) or guild.me is None:
        raise PolicyViolation("guild_only", "guild context is required")
    return _hierarchy_for(guild, interaction.user, guild.me, member, role)


def _bot_can(channel: discord.abc.GuildChannel, interaction: discord.Interaction, permission: str) -> bool:
    guild = interaction.guild
    return bool(guild and guild.me and getattr(channel.permissions_for(guild.me), permission, False))


async def _reply(interaction: discord.Interaction, message: str) -> None:
    await interaction.response.send_message(
        message[:2_000], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


class ServerGroup(app_commands.Group):
    def __init__(self, repository: SqliteServerToolsRepository, bot: Any) -> None:
        super().__init__(name="server", description="Discordサーバーの安全な運用設定")
        self.repository = repository
        self.bot = bot

    async def _deny_fresh(self, interaction: discord.Interaction) -> None:
        await _reply(interaction, _FRESH_REAUTH_DENIED)

    @staticmethod
    def _central_action_enabled(
        registry: Any,
        capability_id: str | None,
        guild_id: int | None,
    ) -> bool:
        try:
            return bool(capability_id and registry and registry.is_capability_enabled(capability_id, guild_id))
        except Exception:
            return False

    async def _central_policy_allows(
        self,
        capability_id: str | None,
        guild: discord.Guild,
        actor: discord.Member,
    ) -> bool:
        """REST再取得済みactorでdynamic required-levelを再評価する。"""

        guard = getattr(self.bot, "capability_guard", None)
        evaluate = getattr(guard, "evaluate_fresh_member", None)
        if capability_id is None or evaluate is None:
            return False
        try:
            decision = await evaluate(capability_id, guild=guild, member=actor)
        except Exception:
            return False
        return bool(getattr(decision, "allowed", False))

    def _bot_member_id(self, guild: discord.Guild) -> int | None:
        bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
        if isinstance(bot_user_id, int) and bot_user_id > 0:
            return bot_user_id
        cached_bot_id = getattr(getattr(guild, "me", None), "id", None)
        return cached_bot_id if isinstance(cached_bot_id, int) and cached_bot_id > 0 else None

    async def _fresh_context(
        self,
        interaction: discord.Interaction,
        command_path: str,
        *,
        channel_id: int | None = None,
        target_id: int | None = None,
        role_id: int | None = None,
        log_channel_id: int | None = None,
        deny: bool = True,
    ) -> FreshServerContext | None:
        """全Discord snapshotと中央policyを副作用直前にfail-closedで更新する。"""

        guild = interaction.guild
        actor_id = getattr(interaction.user, "id", None)
        bot_member_id = self._bot_member_id(guild) if guild is not None else None
        capability_id = COMMAND_CAPABILITIES.get(command_path)
        registry = getattr(self.bot, "capability_registry", None)
        if (
            guild is None
            or not isinstance(actor_id, int)
            or actor_id <= 0
            or bot_member_id is None
            or not self._central_action_enabled(registry, capability_id, guild.id)
        ):
            if deny:
                await self._deny_fresh(interaction)
            return None

        fresh_channel: discord.TextChannel | None = None
        fresh_target: discord.Member | None = None
        fresh_role: discord.Role | None = None
        fresh_log_channel: discord.TextChannel | None = None
        try:
            if channel_id is not None:
                fetched_channel = await guild.fetch_channel(channel_id)
                if not isinstance(fetched_channel, discord.TextChannel):
                    raise TypeError("target channel is not a text channel")
                fresh_channel = fetched_channel
            if target_id is not None:
                fetched_target = await guild.fetch_member(target_id)
                if not isinstance(fetched_target, discord.Member):
                    raise TypeError("target is not a guild member")
                fresh_target = fetched_target
            if role_id is not None:
                roles = await guild.fetch_roles()
                fresh_role = next((item for item in roles if item.id == role_id), None)
                if not isinstance(fresh_role, discord.Role):
                    raise LookupError("target role is unavailable")
            if log_channel_id is not None:
                if fresh_channel is not None and fresh_channel.id == log_channel_id:
                    fresh_log_channel = fresh_channel
                else:
                    fetched_log_channel = await guild.fetch_channel(log_channel_id)
                    if not isinstance(fetched_log_channel, discord.TextChannel):
                        raise TypeError("log channel is not a text channel")
                    fresh_log_channel = fetched_log_channel
            fetched_bot = await guild.fetch_member(bot_member_id)
            if not isinstance(fetched_bot, discord.Member):
                raise TypeError("bot member is unavailable")
            # actorは全対象の後に取得し、実行権限snapshotを副作用へ最も近付ける。
            fetched_actor = await guild.fetch_member(actor_id)
            if not isinstance(fetched_actor, discord.Member):
                raise TypeError("actor is unavailable")
        except Exception as exc:
            logger.warning(
                "servertools_fresh_context_failed",
                extra={"error_type": type(exc).__name__},
            )
            if deny:
                await self._deny_fresh(interaction)
            return None

        # REST待機中・owner照会中のemergency OFFも直後の同期確認で取りこぼさない。
        if not self._central_action_enabled(registry, capability_id, guild.id):
            if deny:
                await self._deny_fresh(interaction)
            return None
        if not await self._central_policy_allows(capability_id, guild, fetched_actor):
            if deny:
                await self._deny_fresh(interaction)
            return None
        if not self._central_action_enabled(registry, capability_id, guild.id):
            await self._deny_fresh(interaction)
            return None
        return FreshServerContext(
            guild=guild,
            actor=fetched_actor,
            bot_member=fetched_bot,
            channel=fresh_channel,
            target=fresh_target,
            role=fresh_role,
            log_channel=fresh_log_channel,
        )

    async def send_mention_announcement(
        self,
        guild: discord.Guild,
        receipt: ServerAnnouncementReceipt,
        *,
        authorization_current: Callable[[], Awaitable[bool]] | None = None,
    ) -> bool:
        """確認済みmentionを既存announceと同じfresh context/policyで送る。"""

        if not _valid_announcement_receipt(receipt, guild):
            return False
        actor_id = receipt.user_id
        channel_id = receipt.target_channel_id
        content = receipt.content
        allow_everyone = receipt.allow_everyone
        reason = receipt.reason
        interaction = type("MentionAnnouncement", (), {"guild": guild, "user": type("User", (), {"id": actor_id})()})()
        requests_everyone = "@everyone" in content or "@here" in content
        config = self.repository.get(guild.id) if requests_everyone else None
        context = await self._fresh_context(
            interaction,
            "server announce",
            channel_id=channel_id,
            log_channel_id=config.log_channel_id if config is not None else None,
            deny=False,
        )
        if context is None or context.channel is None:
            return False
        try:
            ping_everyone = validate_announcement(
                content,
                allow_everyone=allow_everyone,
                reason=reason,
                administrator=_member_permissions(context.actor).administrator,
            )
        except (PolicyViolation, ValueError):
            return False
        if not self._native_action_allowed(
            context,
            ServerAction.ANNOUNCE,
            actor_channel_scoped=False,
            bot_permission="send_messages",
            bot_channel_scoped=True,
            bot_additional_permissions=("mention_everyone",) if ping_everyone else (),
        ):
            return False
        if (
            ping_everyone
            and config is not None
            and config.log_channel_id is not None
            and (
                context.log_channel is None or not context.log_channel.permissions_for(context.bot_member).send_messages
            )
        ):
            return False
        if authorization_current is not None and not await authorization_current():
            return False
        mentions = discord.AllowedMentions(everyone=ping_everyone, users=False, roles=False, replied_user=False)
        await context.channel.send(content, allowed_mentions=mentions)
        if ping_everyone and context.log_channel is not None:
            await context.log_channel.send(
                (
                    f"📣 全体メンション告知｜実行者: {context.actor.id}｜送信先: {context.channel.id}｜理由: {reason.strip()}"
                )[:2_000],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return True

    @staticmethod
    def _native_action_allowed(
        context: FreshServerContext,
        action: ServerAction,
        *,
        actor_channel_scoped: bool,
        bot_permission: str,
        bot_channel_scoped: bool,
        hierarchy: str | None = None,
        actor_additional_permissions: tuple[str, ...] = (),
        bot_additional_permissions: tuple[str, ...] = (),
    ) -> bool:
        """fresh Discord objectsだけからnative permissionと階層を判定する。"""

        try:
            actor_channel = context.channel if actor_channel_scoped else None
            require_action_permission(action, _member_permissions(context.actor, actor_channel))
            if bot_channel_scoped:
                if context.channel is None:
                    return False
                bot_permissions = context.channel.permissions_for(context.bot_member)
            else:
                bot_permissions = context.bot_member.guild_permissions
            actor_permissions = (
                context.channel.permissions_for(context.actor)
                if actor_channel_scoped and context.channel is not None
                else context.actor.guild_permissions
            )
            if any(
                not bool(getattr(actor_permissions, permission, False)) for permission in actor_additional_permissions
            ):
                return False
            if not bool(getattr(bot_permissions, bot_permission, False)) or any(
                not bool(getattr(bot_permissions, permission, False)) for permission in bot_additional_permissions
            ):
                return False
            if hierarchy is not None:
                if context.target is None:
                    return False
                hierarchy_context = _hierarchy_for(
                    context.guild,
                    context.actor,
                    context.bot_member,
                    context.target,
                    context.role,
                )
                if hierarchy == "role":
                    require_role_hierarchy(hierarchy_context)
                elif hierarchy == "member":
                    require_member_hierarchy(hierarchy_context)
                else:
                    return False
        except (AttributeError, PolicyViolation, ValueError):
            return False
        return True

    @app_commands.command(name="slowmode", description="チャンネルの低速モードを変更します")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21_600],
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await _reply(interaction, "テキストチャンネルを指定してください。")
            return
        context = await self._fresh_context(
            interaction,
            "server slowmode",
            channel_id=target.id,
        )
        if context is None:
            return
        if context.channel is None or not self._native_action_allowed(
            context,
            ServerAction.SLOWMODE,
            actor_channel_scoped=True,
            bot_permission="manage_channels",
            bot_channel_scoped=True,
        ):
            await self._deny_fresh(interaction)
            return
        await context.channel.edit(
            slowmode_delay=seconds,
            reason=f"/server slowmode by {context.actor.id}",
        )
        await _reply(interaction, f"{context.channel.mention} の低速モードを {seconds} 秒に設定しました。")

    @app_commands.command(name="lock", description="@everyoneの送信を拒否します")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        await self._set_lock(interaction, channel, locked=True)

    @app_commands.command(name="unlock", description="@everyoneの送信拒否を解除します")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        await self._set_lock(interaction, channel, locked=False)

    async def _set_lock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None,
        *,
        locked: bool,
    ) -> None:
        target = channel or interaction.channel
        guild = interaction.guild
        if not isinstance(target, discord.TextChannel) or guild is None:
            await _reply(interaction, "サーバー内のテキストチャンネルを指定してください。")
            return
        action = ServerAction.LOCK if locked else ServerAction.UNLOCK
        command_path = "server lock" if locked else "server unlock"
        context = await self._fresh_context(
            interaction,
            command_path,
            channel_id=target.id,
            role_id=guild.default_role.id,
        )
        if context is None:
            return
        if (
            context.channel is None
            or context.role is None
            or not self._native_action_allowed(
                context,
                action,
                actor_channel_scoped=True,
                bot_permission="manage_channels",
                bot_channel_scoped=True,
                actor_additional_permissions=("manage_roles",),
                bot_additional_permissions=("manage_roles",),
            )
        ):
            await self._deny_fresh(interaction)
            return
        overwrite = context.channel.overwrites_for(context.role)
        overwrite.send_messages = False if locked else None
        await context.channel.set_permissions(
            context.role,
            overwrite=overwrite,
            reason=f"/server {action.value} by {context.actor.id}",
        )
        await _reply(
            interaction,
            f"{context.channel.mention} を{'ロック' if locked else 'アンロック'}しました。",
        )

    @app_commands.command(name="nick", description="メンバーのニックネームを変更します")
    @app_commands.checks.has_permissions(manage_nicknames=True)
    async def nick(self, interaction: discord.Interaction, member: discord.Member, nickname: str = "") -> None:
        if len(nickname) > 32:
            raise ValueError("nickname must be at most 32 characters")
        context = await self._fresh_context(
            interaction,
            "server nick",
            target_id=member.id,
        )
        if context is None:
            return
        if context.target is None or not self._native_action_allowed(
            context,
            ServerAction.NICK,
            actor_channel_scoped=False,
            bot_permission="manage_nicknames",
            bot_channel_scoped=False,
            hierarchy="member",
        ):
            await self._deny_fresh(interaction)
            return
        await context.target.edit(
            nick=nickname.strip() or None,
            reason=f"/server nick by {context.actor.id}",
        )
        await _reply(interaction, "ニックネームを更新しました。")

    @app_commands.command(name="role-add", description="メンバーへロールを追加します")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def role_add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        await self._change_role(interaction, member, role, add=True)

    @app_commands.command(name="role-remove", description="メンバーからロールを外します")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def role_remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
        await self._change_role(interaction, member, role, add=False)

    async def _change_role(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        *,
        add: bool,
    ) -> None:
        action = ServerAction.ROLE_ADD if add else ServerAction.ROLE_REMOVE
        command_path = "server role-add" if add else "server role-remove"
        context = await self._fresh_context(
            interaction,
            command_path,
            target_id=member.id,
            role_id=role.id,
        )
        if context is None:
            return
        if (
            context.target is None
            or context.role is None
            or context.role.is_default()
            or context.role.managed
            or not self._native_action_allowed(
                context,
                action,
                actor_channel_scoped=False,
                bot_permission="manage_roles",
                bot_channel_scoped=False,
                hierarchy="role",
            )
        ):
            await self._deny_fresh(interaction)
            return
        method = context.target.add_roles if add else context.target.remove_roles
        await method(
            context.role,
            reason=f"/server {action.value} by {context.actor.id}",
        )
        await _reply(interaction, f"{context.role.name} を{'追加' if add else '解除'}しました。")

    @app_commands.command(name="announce", description="安全なメンション設定で告知します")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def announce(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str,
        allow_everyone: bool = False,
        reason: str = "",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._deny_fresh(interaction)
            return
        requests_everyone = "@everyone" in message or "@here" in message
        config = self.repository.get(guild.id) if requests_everyone else None
        context = await self._fresh_context(
            interaction,
            "server announce",
            channel_id=channel.id,
            log_channel_id=config.log_channel_id if config is not None else None,
        )
        if context is None:
            return
        permissions = _member_permissions(context.actor)
        try:
            ping_everyone = validate_announcement(
                message,
                allow_everyone=allow_everyone,
                reason=reason,
                administrator=permissions.administrator,
            )
        except (PolicyViolation, ValueError):
            await self._deny_fresh(interaction)
            return
        if context.channel is None or not self._native_action_allowed(
            context,
            ServerAction.ANNOUNCE,
            actor_channel_scoped=False,
            bot_permission="send_messages",
            bot_channel_scoped=True,
            bot_additional_permissions=("mention_everyone",) if ping_everyone else (),
        ):
            await self._deny_fresh(interaction)
            return
        if (
            ping_everyone
            and config is not None
            and config.log_channel_id is not None
            and (
                context.log_channel is None or not context.log_channel.permissions_for(context.bot_member).send_messages
            )
        ):
            await self._deny_fresh(interaction)
            return
        mentions = discord.AllowedMentions(everyone=ping_everyone, users=False, roles=False, replied_user=False)
        await context.channel.send(message, allowed_mentions=mentions)
        if ping_everyone and context.log_channel is not None:
            await context.log_channel.send(
                (
                    "📣 全体メンション告知"
                    f"｜実行者: {context.actor} ({context.actor.id})"
                    f"｜送信先: #{context.channel.name} ({context.channel.id})"
                    f"｜理由: {reason.strip()}"
                )[:2_000],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await _reply(interaction, "告知を送信しました。")

    @app_commands.command(name="welcome-set", description="参加メッセージと送信先を設定します")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_set(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str) -> None:
        await self._set_member_message(interaction, channel, message, welcome=True)

    @app_commands.command(name="goodbye-set", description="退出メッセージと送信先を設定します")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def goodbye_set(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str) -> None:
        await self._set_member_message(interaction, channel, message, welcome=False)

    async def _set_member_message(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str,
        *,
        welcome: bool,
    ) -> None:
        if interaction.guild_id is None:
            raise PolicyViolation("guild_only", "guild context is required")
        require_action_permission(ServerAction.CONFIGURE, _actor_permissions(interaction))
        if not _bot_can(channel, interaction, "send_messages"):
            await _reply(interaction, "Botに送信権限がありません。")
            return
        if welcome:
            self.repository.set_welcome(interaction.guild_id, channel.id, message)
        else:
            self.repository.set_goodbye(interaction.guild_id, channel.id, message)
        await _reply(interaction, "参加・退出メッセージ設定を保存しました。")

    @app_commands.command(name="log-channel", description="監査ログの送信先を設定します")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild_id is None:
            raise PolicyViolation("guild_only", "guild context is required")
        require_action_permission(ServerAction.CONFIGURE, _actor_permissions(interaction))
        if not _bot_can(channel, interaction, "send_messages"):
            await _reply(interaction, "Botに送信権限がありません。")
            return
        self.repository.set_log_channel(interaction.guild_id, channel.id)
        await _reply(interaction, "監査ログチャンネルを保存しました。本文保存は既定で無効です。")

    @app_commands.command(name="config-show", description="servertools設定を表示します")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def config_show(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            raise PolicyViolation("guild_only", "guild context is required")
        require_action_permission(ServerAction.CONFIGURE, _actor_permissions(interaction))
        config = self.repository.get(interaction.guild_id)
        await _reply(interaction, _render_config(config))


def _render_config(config: GuildServerConfig) -> str:
    return "\n".join(
        (
            "servertools 設定",
            f"Welcome: {_channel_label(config.welcome_channel_id)}",
            f"Goodbye: {_channel_label(config.goodbye_channel_id)}",
            f"監査ログ: {_channel_label(config.log_channel_id)}",
            f"監査本文: {'保存する' if config.audit_include_content else '保存しない'}",
            f"本文上限: {config.audit_content_limit}文字",
        )
    )


def _channel_label(channel_id: int | None) -> str:
    return f"<#{channel_id}>" if channel_id else "未設定"


class DiscordServerToolsListeners:
    def __init__(self, repository: SqliteServerToolsRepository) -> None:
        self.repository = repository
        self._bot: object | None = None

    def bind_bot(self, bot: object) -> None:
        self._bot = bot

    def _enabled(self, event_name: str, guild_id: int) -> bool:
        capability_id = EVENT_CAPABILITIES.get(event_name)
        registry = getattr(self._bot, "capability_registry", None)
        if capability_id is None or registry is None:
            return False
        try:
            return bool(registry.capability_status(capability_id, guild_id).executable)
        except (KeyError, TypeError, ValueError):
            return False

    async def _send_event_message(
        self,
        channel: discord.TextChannel,
        content: str,
        *,
        event_name: str,
        guild_id: int,
        event_id: int,
        actor_id: int | None,
        revision: Callable[[], str],
    ) -> None:
        try:
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except asyncio.CancelledError:
            raise
        except Exception as send_error:
            terminal = getattr(self._bot, "interaction_failure_terminal", None)
            record_failure = getattr(terminal, "fail_background_once", None)
            if not callable(record_failure):
                raise
            try:
                await record_failure(
                    self._bot,
                    send_error,
                    surface=f"servertools.{event_name}",
                    guild_id=guild_id,
                    event_id=event_id,
                    actor_id=actor_id,
                    revision=revision(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as terminal_error:
                raise send_error from terminal_error

    async def on_member_join(self, member: discord.Member) -> None:
        if not self._enabled("member_join", member.guild.id):
            return
        config = self.repository.get(member.guild.id)
        if config.welcome_channel_id is None or config.welcome_message is None:
            return
        channel = member.guild.get_channel(config.welcome_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        text = render_member_message(config.welcome_message, _member_values(member))
        if not self._enabled("member_join", member.guild.id):
            return
        await self._send_event_message(
            channel,
            text,
            event_name="member_join",
            guild_id=member.guild.id,
            event_id=member.id,
            actor_id=member.id,
            revision=lambda: _member_event_revision(member, channel.id),
        )

    async def on_member_remove(self, member: discord.Member) -> None:
        if not self._enabled("member_remove", member.guild.id):
            return
        config = self.repository.get(member.guild.id)
        if config.goodbye_channel_id is None or config.goodbye_message is None:
            return
        channel = member.guild.get_channel(config.goodbye_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        text = render_member_message(config.goodbye_message, _member_values(member))
        if not self._enabled("member_remove", member.guild.id):
            return
        await self._send_event_message(
            channel,
            text,
            event_name="member_remove",
            guild_id=member.guild.id,
            event_id=member.id,
            actor_id=member.id,
            revision=lambda: _member_event_revision(member, channel.id),
        )

    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if not self._enabled("message_delete", message.guild.id):
            return
        config = self.repository.get(message.guild.id)
        channel = _log_channel(message.guild, config)
        if channel is None or message.channel.id == channel.id:
            return
        text = render_message_delete(
            author=f"{message.author} ({message.author.id})",
            channel=f"#{getattr(message.channel, 'name', message.channel.id)} ({message.channel.id})",
            content=message.content,
            include_content=config.audit_include_content,
            content_limit=config.audit_content_limit,
        )
        if not self._enabled("message_delete", message.guild.id):
            return
        await self._send_event_message(
            channel,
            text,
            event_name="message_delete",
            guild_id=message.guild.id,
            event_id=message.id,
            actor_id=getattr(message.author, "id", None),
            revision=lambda: _event_revision("message_delete", message.id, channel.id),
        )

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or before.content == after.content:
            return
        if not self._enabled("message_edit", before.guild.id):
            return
        config = self.repository.get(before.guild.id)
        channel = _log_channel(before.guild, config)
        if channel is None or before.channel.id == channel.id:
            return
        text = render_message_edit(
            author=f"{before.author} ({before.author.id})",
            channel=f"#{getattr(before.channel, 'name', before.channel.id)} ({before.channel.id})",
            before=before.content,
            after=after.content,
            include_content=config.audit_include_content,
            content_limit=config.audit_content_limit,
        )
        if not self._enabled("message_edit", before.guild.id):
            return
        await self._send_event_message(
            channel,
            text,
            event_name="message_edit",
            guild_id=before.guild.id,
            event_id=before.id,
            actor_id=getattr(before.author, "id", None),
            revision=lambda: _event_revision(
                "message_edit",
                before.id,
                channel.id,
                hashlib.sha256(before.content.encode("utf-8")).hexdigest(),
                hashlib.sha256(after.content.encode("utf-8")).hexdigest(),
            ),
        )


def _member_values(member: discord.Member) -> MemberTemplateValues:
    return MemberTemplateValues(
        mention=member.mention,
        display_name=member.display_name,
        guild_name=member.guild.name,
        member_count=member.guild.member_count,
    )


def _member_event_revision(member: discord.Member, target_channel_id: int) -> str:
    joined_at = getattr(member, "joined_at", None)
    isoformat = getattr(joined_at, "isoformat", None)
    membership_revision: object = isoformat() if callable(isoformat) else ("process_object", id(member))
    return _event_revision("member", member.guild.id, member.id, target_channel_id, membership_revision)


def _event_revision(*values: object) -> str:
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _log_channel(guild: discord.Guild, config: GuildServerConfig) -> discord.TextChannel | None:
    if config.log_channel_id is None:
        return None
    channel = guild.get_channel(config.log_channel_id)
    return channel if isinstance(channel, discord.TextChannel) else None
