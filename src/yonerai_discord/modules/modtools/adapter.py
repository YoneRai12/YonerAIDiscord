from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands

from yonerai_discord.capabilities import COMMAND_CAPABILITIES

from ..automod.normalization import extract_domains
from .domain import ModAction, PermissionSnapshot
from .repository import ModtoolsRepository
from .validation import (
    decide_permission,
    validate_purge,
    validate_reason,
    validate_timeout_minutes,
)


_LEDGER_UNCONFIRMED = "（台帳未確定・要監査）"


class ModGroup(app_commands.Group):
    def __init__(self, repository: ModtoolsRepository, bot: Any) -> None:
        super().__init__(name="mod", description="安全なモデレーション操作")
        self.repository = repository
        self.bot = bot

    async def _send(self, interaction: discord.Interaction, message: str) -> None:
        kwargs = {
            "ephemeral": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if interaction.response.is_done():
            await interaction.followup.send(message[:1900], **kwargs)
        else:
            await interaction.response.send_message(message[:1900], **kwargs)

    def _snapshot(
        self,
        interaction: discord.Interaction,
        target: discord.Member | None = None,
        *,
        actor_override: discord.Member | None = None,
        target_override: discord.Member | None = None,
    ) -> PermissionSnapshot | None:
        guild = interaction.guild
        actor = actor_override or interaction.user
        if guild is None or not isinstance(actor, discord.Member) or guild.me is None:
            return None
        live_target = (
            target_override
            if target_override is not None
            else (guild.get_member(target.id) if target is not None else None)
        )
        actor_permissions = frozenset(name for name, enabled in actor.guild_permissions if enabled)
        bot_permissions = frozenset(name for name, enabled in guild.me.guild_permissions if enabled)
        return PermissionSnapshot(
            guild_owner_id=guild.owner_id,
            bot_user_id=guild.me.id,
            actor_id=actor.id,
            actor_permissions=actor_permissions,
            bot_permissions=bot_permissions,
            actor_top_role=actor.top_role.position,
            bot_top_role=guild.me.top_role.position,
            target_id=live_target.id if live_target is not None else (target.id if target is not None else None),
            target_top_role=live_target.top_role.position if live_target is not None else None,
        )

    async def _authorize(
        self,
        interaction: discord.Interaction,
        action: ModAction,
        target: discord.Member | None = None,
        *,
        actor_override: discord.Member | None = None,
        target_override: discord.Member | None = None,
    ) -> bool:
        snapshot = self._snapshot(
            interaction,
            target,
            actor_override=actor_override,
            target_override=target_override,
        )
        if snapshot is None:
            await self._send(interaction, "このコマンドはサーバー内でのみ使用できます")
            return False
        decision = decide_permission(action, snapshot)
        if not decision.allowed:
            await self._send(interaction, decision.reason)
            return False
        return True

    async def _side_effect_still_allowed(
        self,
        interaction: discord.Interaction,
        action: ModAction,
        target: discord.Member | None = None,
    ) -> bool:
        return await self._fresh_authorized_members(interaction, action, target) is not None

    async def _purge_side_effect_still_allowed(
        self,
        interaction: discord.Interaction,
        action: ModAction,
        target: discord.Member | None = None,
    ) -> bool:
        """REST-freshなchannel overwriteを含む実効権限でpurgeを再認可する。"""

        fresh = await self._fresh_authorized_members(interaction, action, target)
        if fresh is None:
            return False
        fresh_actor, _ = fresh
        guild = interaction.guild
        channel_id = getattr(interaction, "channel_id", None)
        cached_bot_member = getattr(guild, "me", None)
        bot_id = getattr(cached_bot_member, "id", None)
        fetch_channel = getattr(guild, "fetch_channel", None)
        fetch_member = getattr(guild, "fetch_member", None)
        if (
            guild is None
            or not isinstance(channel_id, int)
            or not isinstance(bot_id, int)
            or not callable(fetch_channel)
            or not callable(fetch_member)
        ):
            await self._send(interaction, "対象チャンネルまたはBotの現在権限を確認できないため、削除を停止しました")
            return False
        try:
            fresh_bot_member = await fetch_member(bot_id)
            fresh_channel = await fetch_channel(channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            await self._send(interaction, "対象チャンネルまたはBotの現在権限を確認できないため、削除を停止しました")
            return False
        if getattr(fresh_bot_member, "id", None) != bot_id or getattr(fresh_channel, "id", None) != channel_id:
            await self._send(interaction, "対象チャンネルまたはBotの現在権限を確認できないため、削除を停止しました")
            return False

        path = f"mod {action.value.replace('_', '-')}"
        capability_id = COMMAND_CAPABILITIES.get(path)
        registry = getattr(self.bot, "capability_registry", None)
        if not self._central_action_enabled(registry, capability_id, interaction.guild_id):
            await self._send(interaction, "機能設定が変更されたため、残りの削除を停止しました")
            return False
        if not await self._central_policy_allows(capability_id, guild, fresh_actor):
            await self._send(interaction, "必要権限または機能設定が変更されたため、残りの削除を停止しました")
            return False
        # policy await中のemergency OFFを取りこぼさず、ここからdeleteまではawaitを挟まない。
        if not self._central_action_enabled(registry, capability_id, interaction.guild_id):
            await self._send(interaction, "機能設定が変更されたため、残りの削除を停止しました")
            return False
        permissions_for = getattr(fresh_channel, "permissions_for", None)
        if not callable(permissions_for):
            await self._send(interaction, "対象チャンネルの現在権限を確認できないため、削除を停止しました")
            return False
        try:
            actor_permissions = permissions_for(fresh_actor)
            bot_permissions = permissions_for(fresh_bot_member)
        except Exception:
            await self._send(interaction, "対象チャンネルの現在権限を確認できないため、削除を停止しました")
            return False
        if not bool(getattr(actor_permissions, "manage_messages", False)):
            await self._send(interaction, "このチャンネルでメッセージを削除する現在権限がないため、削除を停止しました")
            return False
        required_bot_permissions = ("view_channel", "read_message_history", "manage_messages")
        if not all(bool(getattr(bot_permissions, name, False)) for name in required_bot_permissions):
            await self._send(interaction, "Botのチャンネル権限が不足しているため、削除を停止しました")
            return False
        return True

    async def _fresh_authorized_members(
        self,
        interaction: discord.Interaction,
        action: ModAction,
        target: discord.Member | None = None,
    ) -> tuple[discord.Member, discord.Member | None] | None:
        path = f"mod {action.value.replace('_', '-')}"
        capability_id = COMMAND_CAPABILITIES.get(path)
        registry = getattr(self.bot, "capability_registry", None)
        centrally_enabled = self._central_action_enabled(
            registry,
            capability_id,
            interaction.guild_id,
        )
        if not centrally_enabled:
            await self._send(interaction, "機能設定が変更されたため、残りの操作を停止しました")
            return None
        guild = interaction.guild
        actor_id = getattr(interaction.user, "id", None)
        if guild is None or not isinstance(actor_id, int):
            await self._send(interaction, "実行者を再確認できないため、残りの操作を停止しました")
            return None
        fresh_target: discord.Member | None = None
        if target is not None:
            try:
                fresh_target = await guild.fetch_member(target.id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                await self._send(
                    interaction,
                    "対象者の現在roleを確認できないため、残りの操作を停止しました",
                )
                return None
        # target取得後にactorを取得し、actor権限のsnapshotを最も副作用に近付ける。
        try:
            fresh_actor = await guild.fetch_member(actor_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            await self._send(interaction, "実行者の現在権限を確認できないため、残りの操作を停止しました")
            return None
        # REST await中のemergency OFFを取りこぼさないよう最後に再確認する。
        if not self._central_action_enabled(
            registry,
            capability_id,
            interaction.guild_id,
        ):
            await self._send(interaction, "機能設定が変更されたため、残りの操作を停止しました")
            return None
        if not await self._central_policy_allows(capability_id, guild, fresh_actor):
            await self._send(interaction, "必要権限または機能設定が変更されたため、残りの操作を停止しました")
            return None
        if not await self._authorize(
            interaction,
            action,
            target,
            actor_override=fresh_actor,
            target_override=fresh_target,
        ):
            return None
        return fresh_actor, fresh_target

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
        """dynamic required-levelをfresh actorで副作用直前に再評価する。"""

        guard = getattr(self.bot, "capability_guard", None)
        evaluate = getattr(guard, "evaluate_fresh_member", None)
        if capability_id is None or evaluate is None:
            return False
        try:
            decision = await evaluate(capability_id, guild=guild, member=actor)
        except Exception:
            return False
        return bool(getattr(decision, "allowed", False))

    def _record(
        self,
        interaction: discord.Interaction,
        action: ModAction,
        reason: str,
        *,
        target_id: int | None = None,
        status: str = "completed",
        metadata: dict[str, object] | None = None,
    ) -> int:
        case = self.repository.record_case(
            guild_id=interaction.guild_id,
            action=action,
            target_id=target_id,
            moderator_id=interaction.user.id,
            reason=reason,
            status=status,
            metadata=metadata,
        )
        return case.id

    @app_commands.command(name="warn", description="メンバーへ警告を記録します")
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        try:
            reason = validate_reason(reason)
        except ValueError as exc:
            await self._send(interaction, str(exc))
            return
        if not await self._authorize(interaction, ModAction.WARN, member):
            return
        fresh = await self._fresh_authorized_members(interaction, ModAction.WARN, member)
        if fresh is None:
            return
        _, fresh_member = fresh
        if fresh_member is None:
            await self._send(interaction, "対象メンバーを再確認できなかったため、警告を記録しませんでした")
            return
        warning = self.repository.add_warning(
            guild_id=interaction.guild_id,
            target_id=fresh_member.id,
            moderator_id=interaction.user.id,
            reason=reason,
        )
        await self._send(interaction, f"警告を記録しました。Case #{warning.case_id}")

    @app_commands.command(name="warnings", description="メンバーの有効な警告を表示します")
    @app_commands.guild_only()
    async def warnings(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self._authorize(interaction, ModAction.WARNINGS, member):
            return
        warnings = self.repository.warnings_for(interaction.guild_id, member.id)
        if not warnings:
            await self._send(interaction, "有効な警告はありません")
            return
        lines = [f"Case #{item.case_id}: {item.reason}" for item in warnings[:20]]
        await self._send(interaction, "\n".join(lines))

    @app_commands.command(name="timeout", description="メンバーをタイムアウトします")
    @app_commands.guild_only()
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: int,
        reason: str,
    ) -> None:
        try:
            reason = validate_reason(reason)
            validate_timeout_minutes(minutes)
        except ValueError as exc:
            await self._send(interaction, str(exc))
            return

        async def execute(target: discord.Member, *, reason: str) -> None:
            await target.timeout(timedelta(minutes=minutes), reason=reason)

        await self._member_action(
            interaction,
            member,
            reason,
            ModAction.TIMEOUT,
            execute,
            metadata={"minutes": minutes},
        )

    @app_commands.command(name="untimeout", description="タイムアウトを解除します")
    @app_commands.guild_only()
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        try:
            reason = validate_reason(reason)
        except ValueError as exc:
            await self._send(interaction, str(exc))
            return

        async def execute(target: discord.Member, *, reason: str) -> None:
            await target.timeout(None, reason=reason)

        await self._member_action(interaction, member, reason, ModAction.UNTIMEOUT, execute)

    @app_commands.command(name="kick", description="メンバーをキックします")
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        async def execute(target: discord.Member, *, reason: str) -> None:
            await target.kick(reason=reason)

        await self._member_action(interaction, member, reason, ModAction.KICK, execute)

    @app_commands.command(name="ban", description="メンバーをBANします")
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        async def execute(target: discord.Member, *, reason: str) -> None:
            await interaction.guild.ban(target, reason=reason)

        await self._member_action(interaction, member, reason, ModAction.BAN, execute)

    async def _member_action(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str,
        action: ModAction,
        execute: Callable[..., Awaitable[None]],
        *,
        metadata: dict[str, object] | None = None,
    ) -> None:
        try:
            reason = validate_reason(reason)
        except ValueError as exc:
            await self._send(interaction, str(exc))
            return
        if not await self._authorize(interaction, action, member):
            return
        fresh_for_ledger = await self._fresh_authorized_members(interaction, action, member)
        if fresh_for_ledger is None:
            return
        _, ledger_target = fresh_for_ledger
        if ledger_target is None:
            await self._send(interaction, "対象メンバーを再確認できなかったため、操作を中止しました")
            return
        case_id = self._record(
            interaction,
            action,
            reason,
            target_id=ledger_target.id,
            status="pending",
            metadata={**(metadata or {}), "discord_result": "pending"},
        )
        fresh_for_discord = await self._fresh_authorized_members(interaction, action, member)
        if fresh_for_discord is None:
            finalized = self._finalize_case(
                interaction,
                case_id,
                status="aborted",
                metadata={**(metadata or {}), "discord_result": "not_started", "stop_reason": "authorization_changed"},
            )
            suffix = "" if finalized else _LEDGER_UNCONFIRMED
            await self._send(interaction, f"操作は未実行です。Case #{case_id} は中止しました{suffix}")
            return
        _, fresh_target = fresh_for_discord
        if fresh_target is None:
            finalized = self._finalize_case(
                interaction,
                case_id,
                status="aborted",
                metadata={**(metadata or {}), "discord_result": "not_started", "stop_reason": "target_missing"},
            )
            suffix = "" if finalized else _LEDGER_UNCONFIRMED
            await self._send(interaction, f"対象メンバーを再確認できず、操作を中止しました。Case #{case_id}{suffix}")
            return
        try:
            await execute(fresh_target, reason=reason)
        except (discord.Forbidden, discord.NotFound) as exc:
            finalized = self._finalize_case(
                interaction,
                case_id,
                status="failed",
                metadata={**(metadata or {}), "discord_result": "rejected", "error_type": type(exc).__name__},
            )
            suffix = "" if finalized else _LEDGER_UNCONFIRMED
            await self._send(interaction, f"Discordに拒否されたため操作は未実行です。Case #{case_id}{suffix}")
            return
        except Exception as exc:
            finalized = self._finalize_case(
                interaction,
                case_id,
                status="uncertain",
                metadata={**(metadata or {}), "discord_result": "raised", "error_type": type(exc).__name__},
            )
            suffix = "" if finalized else _LEDGER_UNCONFIRMED
            await self._send(
                interaction,
                f"Discord操作の結果を確定できませんでした。Case #{case_id} を監査してください{suffix}",
            )
            return
        finalized = self._finalize_case(
            interaction,
            case_id,
            status="completed",
            metadata={**(metadata or {}), "discord_result": "completed"},
        )
        if finalized:
            await self._send(interaction, f"{action.value} を実行しました。Case #{case_id}")
        else:
            await self._send(
                interaction,
                f"{action.value} はDiscord上で実行済みです。Case #{case_id}{_LEDGER_UNCONFIRMED}",
            )

    def _finalize_case(
        self,
        interaction: discord.Interaction,
        case_id: int,
        *,
        status: str,
        metadata: dict[str, object],
    ) -> bool:
        try:
            return bool(
                self.repository.update_case(
                    interaction.guild_id,
                    case_id,
                    status=status,
                    metadata=metadata,
                )
            )
        except Exception:
            return False

    @app_commands.command(name="unban", description="ユーザーIDを指定してBANを解除します")
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str) -> None:
        try:
            target_id = int(user_id)
            if target_id <= 0:
                raise ValueError
            reason = validate_reason(reason)
        except ValueError:
            await self._send(interaction, "正しいユーザーIDと理由を指定してください")
            return
        if not await self._authorize(interaction, ModAction.UNBAN):
            return
        if await self._fresh_authorized_members(interaction, ModAction.UNBAN) is None:
            return
        case_id = self._record(
            interaction,
            ModAction.UNBAN,
            reason,
            target_id=target_id,
            status="pending",
            metadata={"discord_result": "pending"},
        )
        if await self._fresh_authorized_members(interaction, ModAction.UNBAN) is None:
            finalized = self._finalize_case(
                interaction,
                case_id,
                status="aborted",
                metadata={"discord_result": "not_started", "stop_reason": "authorization_changed"},
            )
            suffix = "" if finalized else _LEDGER_UNCONFIRMED
            await self._send(interaction, f"BAN解除は未実行です。Case #{case_id} は中止しました{suffix}")
            return
        try:
            await interaction.guild.unban(discord.Object(id=target_id), reason=reason)
        except (discord.Forbidden, discord.NotFound) as exc:
            finalized = self._finalize_case(
                interaction,
                case_id,
                status="failed",
                metadata={"discord_result": "rejected", "error_type": type(exc).__name__},
            )
            suffix = "" if finalized else _LEDGER_UNCONFIRMED
            await self._send(interaction, f"Discordに拒否されたためBAN解除は未実行です。Case #{case_id}{suffix}")
            return
        except Exception as exc:
            finalized = self._finalize_case(
                interaction,
                case_id,
                status="uncertain",
                metadata={"discord_result": "raised", "error_type": type(exc).__name__},
            )
            suffix = "" if finalized else _LEDGER_UNCONFIRMED
            await self._send(
                interaction,
                f"BAN解除の結果を確定できませんでした。Case #{case_id} を監査してください{suffix}",
            )
            return
        finalized = self._finalize_case(
            interaction,
            case_id,
            status="completed",
            metadata={"discord_result": "completed"},
        )
        if finalized:
            await self._send(interaction, f"BANを解除しました。Case #{case_id}")
        else:
            await self._send(interaction, f"BAN解除は実行済みです。Case #{case_id}{_LEDGER_UNCONFIRMED}")

    @app_commands.command(name="purge", description="直近メッセージを安全に一括削除します")
    @app_commands.guild_only()
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: int,
        reason: str,
        dry_run: bool = True,
        confirm: str = "",
    ) -> None:
        await self._purge(interaction, amount, reason, dry_run, confirm, ModAction.PURGE, None, lambda _: True)

    @app_commands.command(name="purge-user", description="指定メンバーのメッセージを一括削除します")
    @app_commands.guild_only()
    async def purge_user(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
        reason: str,
        dry_run: bool = True,
        confirm: str = "",
    ) -> None:
        await self._purge(
            interaction,
            amount,
            reason,
            dry_run,
            confirm,
            ModAction.PURGE_USER,
            member,
            lambda message: message.author.id == member.id,
        )

    @app_commands.command(name="purge-links", description="URLを含むメッセージを一括削除します")
    @app_commands.guild_only()
    async def purge_links(
        self,
        interaction: discord.Interaction,
        amount: int,
        reason: str,
        dry_run: bool = True,
        confirm: str = "",
    ) -> None:
        await self._purge(
            interaction,
            amount,
            reason,
            dry_run,
            confirm,
            ModAction.PURGE_LINKS,
            None,
            lambda message: bool(extract_domains(message.content)),
        )

    async def _purge(
        self,
        interaction: discord.Interaction,
        amount: int,
        reason: str,
        dry_run: bool,
        confirm: str,
        action: ModAction,
        target: discord.Member | None,
        predicate: Callable[[discord.Message], bool],
    ) -> None:
        try:
            reason = validate_purge(amount, reason, dry_run=dry_run, confirm=confirm)
        except ValueError as exc:
            await self._send(interaction, str(exc))
            return
        if not await self._authorize(interaction, action, target):
            return
        channel = interaction.channel
        if channel is None or not hasattr(channel, "history"):
            await self._send(interaction, "このチャンネルでは削除できません")
            return
        matches: list[discord.Message] = []
        async for message in channel.history(limit=min(500, max(100, amount * 5))):
            if predicate(message):
                matches.append(message)
                if len(matches) >= amount:
                    break
        # dry-runも含め、cached guild permissionだけで対象チャンネルを代理操作させない。
        if not await self._purge_side_effect_still_allowed(interaction, action, target):
            return
        if dry_run:
            await self._send(
                interaction,
                f"Dry run: {len(matches)}件が対象です。実行時は dry_run:false confirm:PURGE を指定してください",
            )
            return
        pending_metadata: dict[str, object] = {
            "requested": amount,
            "deleted": 0,
            "stopped": False,
            "stop_reason": None,
        }
        try:
            case_id = self._record(
                interaction,
                action,
                reason,
                target_id=target.id if target is not None else None,
                status="pending",
                metadata=pending_metadata,
            )
        except Exception:
            await self._send(
                interaction,
                "監査台帳を開始できないため、メッセージは削除していません",
            )
            return
        deleted = 0
        stopped = False
        stop_reason: str | None = None
        for message in matches:
            if not await self._purge_side_effect_still_allowed(interaction, action, target):
                stopped = True
                stop_reason = "authorization_changed"
                break
            # Discordのmessage delete endpointはaudit reasonを受け取らないため、
            # 理由は必ずcase ledgerへ保存する。
            try:
                await message.delete()
            except Exception:
                stopped = True
                stop_reason = "discord_delete_failed"
                break
            deleted += 1
        final_metadata = {
            "requested": amount,
            "deleted": deleted,
            "stopped": stopped,
            "stop_reason": stop_reason,
        }
        final_status = "partial" if stopped and deleted > 0 else "failed" if stopped else "completed"
        finalized = self._finalize_case(
            interaction,
            case_id,
            status=final_status,
            metadata=final_metadata,
        )
        if not finalized:
            outcome = "Discord削除済み" if deleted > 0 else "Discord削除は0件"
            await self._send(interaction, f"{outcome}・台帳未確定・要監査。Case #{case_id}")
            return
        if stop_reason == "authorization_changed":
            suffix = "（途中で権限または機能設定が変わったため停止）"
        elif stop_reason == "discord_delete_failed":
            suffix = "（Discord削除の失敗で途中停止）"
        else:
            suffix = ""
        await self._send(interaction, f"{deleted}件を削除しました{suffix}。Case #{case_id}")

    @app_commands.command(name="case", description="モデレーションCaseを表示します")
    @app_commands.guild_only()
    async def case(self, interaction: discord.Interaction, case_id: int) -> None:
        if not await self._authorize(interaction, ModAction.CASE):
            return
        item = self.repository.get_case(interaction.guild_id, case_id)
        if item is None:
            await self._send(interaction, "Caseが見つかりません")
            return
        await self._send(
            interaction,
            f"Case #{item.id}\n操作: {item.action.value}\n対象: {item.target_id}\n"
            f"実行者: {item.moderator_id}\n理由: {item.reason}\n状態: {item.status}",
        )
