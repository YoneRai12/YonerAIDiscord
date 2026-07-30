from __future__ import annotations

import discord

from yonerai_discord.capabilities import EVENT_CAPABILITIES
from yonerai_discord.modules.operations import SafeInteractionView

from .policy import selfrole_permissions_are_safe
from .repository import CommunityRepository


NO_MENTIONS = discord.AllowedMentions.none()


class PollVoteButton(discord.ui.Button["PollView"]):
    def __init__(self, poll_id: str, option_index: int, label: str) -> None:
        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.secondary,
            custom_id=f"community:poll:{poll_id}:{option_index}",
        )
        self.poll_id = poll_id
        self.option_index = option_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内でのみ利用できます。", ephemeral=True)
            return
        if not await _component_currently_allowed(interaction, "component.poll-vote"):
            await _deny_changed(interaction)
            return
        # interaction_checkからcallbackまでのawait境界に加え、DB副作用直前にも再評価する。
        if not await _component_currently_allowed(interaction, "component.poll-vote"):
            await _deny_changed(interaction)
            return
        if self.view.repository.vote(interaction.guild_id, self.poll_id, interaction.user.id, self.option_index):
            await interaction.response.send_message(
                "投票を受け付けました。", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
        else:
            await interaction.response.send_message(
                "投票済み、終了済み、または無効な投票です。", ephemeral=True, allowed_mentions=NO_MENTIONS
            )


class PollView(SafeInteractionView):
    def __init__(self, repository: CommunityRepository, poll_id: str, options: tuple[str, ...]) -> None:
        super().__init__(timeout=None)
        self.repository = repository
        for index, option in enumerate(options):
            self.add_item(PollVoteButton(poll_id, index, option))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _component_allowed(interaction, "component.poll-vote")


class SelfRoleButton(discord.ui.Button["SelfRoleView"]):
    def __init__(self, guild_id: int, role_id: int) -> None:
        super().__init__(
            label=f"Role {role_id}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"community:selfrole:{guild_id}:{role_id}",
        )
        self.expected_guild_id = guild_id
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild is None
            or interaction.guild.id != self.expected_guild_id
            or not isinstance(interaction.user, discord.Member)
        ):
            await interaction.response.send_message("このサーバーでは利用できません。", ephemeral=True)
            return
        if not await _component_currently_allowed(interaction, "component.selfrole-toggle"):
            await _deny_changed(interaction)
            return
        if self.role_id not in self.view.repository.selfroles(interaction.guild.id):
            await interaction.response.send_message("このロールは現在選択できません。", ephemeral=True)
            return
        cached_bot_member = interaction.guild.me
        if cached_bot_member is None:
            await interaction.response.send_message("Botがこのロールを操作できません。", ephemeral=True)
            return
        # Discord REST取得前に現在のactor levelを確定し、REST取得後は同じlevelで
        # shutdown・module・capability・RBACだけを同期再評価する。
        actor = await _component_actor_if_currently_allowed(interaction, "component.selfrole-toggle")
        if actor is None:
            await _deny_changed(interaction)
            return
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
            bot_member = await interaction.guild.fetch_member(cached_bot_member.id)
            fresh_roles = await interaction.guild.fetch_roles()
        except discord.HTTPException:
            await interaction.response.send_message(
                "現在のロール状態を確認できないため、操作しませんでした。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return

        # fetch_roles()後にawaitを挟まず、REST-fresh Roleだけを副作用へ渡す。
        if not _component_level_still_allowed(
            interaction,
            "component.selfrole-toggle",
            actor_level=getattr(actor, "level", None),
        ):
            await _deny_changed(interaction)
            return
        if self.role_id not in self.view.repository.selfroles(interaction.guild.id):
            await interaction.response.send_message("このロールは現在選択できません。", ephemeral=True)
            return
        if member.id != interaction.user.id or bot_member.id != cached_bot_member.id:
            await interaction.response.send_message(
                "現在のロール状態を確認できないため、操作しませんでした。",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return

        roles_by_id = {role.id: role for role in fresh_roles}
        role = roles_by_id.get(self.role_id)
        bot_role_ids = {item.id for item in getattr(bot_member, "roles", ())}
        if not bot_role_ids or not bot_role_ids.issubset(roles_by_id):
            await interaction.response.send_message("Botがこのロールを操作できません。", ephemeral=True)
            return
        bot_roles = [roles_by_id[role_id] for role_id in bot_role_ids]
        bot_top_role = max(bot_roles, key=lambda item: (item.position, item.id))
        bot_can_manage_roles = any(
            item.permissions.administrator or item.permissions.manage_roles for item in bot_roles
        )
        if (
            role is None
            or role.is_default()
            or role.managed
            or not bot_can_manage_roles
            or role.position >= bot_top_role.position
        ):
            await interaction.response.send_message("Botがこのロールを操作できません。", ephemeral=True)
            return
        permissions = role.permissions
        if not selfrole_permissions_are_safe(
            administrator=permissions.administrator,
            manage_guild=permissions.manage_guild,
            manage_roles=permissions.manage_roles,
            manage_channels=permissions.manage_channels,
            ban_members=permissions.ban_members,
            kick_members=permissions.kick_members,
            moderate_members=permissions.moderate_members,
        ):
            await interaction.response.send_message("このロールは権限が強すぎるため付与できません。", ephemeral=True)
            return
        try:
            if self.role_id in {item.id for item in getattr(member, "roles", ())}:
                await member.remove_roles(role, reason="self-role remove")
                message = f"{role.name} を外しました。"
            else:
                await member.add_roles(role, reason="self-role add")
                message = f"{role.name} を付与しました。"
        except discord.HTTPException:
            await interaction.response.send_message("ロール操作に失敗しました。", ephemeral=True)
            return
        await interaction.response.send_message(message, ephemeral=True, allowed_mentions=NO_MENTIONS)


class SelfRoleView(SafeInteractionView):
    def __init__(self, repository: CommunityRepository, guild_id: int, role_ids: tuple[int, ...]) -> None:
        super().__init__(timeout=None)
        self.repository = repository
        for role_id in role_ids[:25]:
            self.add_item(SelfRoleButton(guild_id, role_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _component_allowed(interaction, "component.selfrole-toggle")


async def _component_allowed(interaction: discord.Interaction, event_name: str) -> bool:
    capability_id = EVENT_CAPABILITIES.get(event_name)
    guard = getattr(getattr(interaction, "client", None), "capability_guard", None)
    if capability_id is not None and guard is not None:
        return bool(
            await guard.check_capability(
                interaction,
                capability_id,
                surface=event_name,
            )
        )
    await interaction.response.send_message(
        "Botの認可基盤が準備できていません。",
        ephemeral=True,
        allowed_mentions=NO_MENTIONS,
    )
    return False


async def _component_currently_allowed(interaction: discord.Interaction, event_name: str) -> bool:
    return await _component_actor_if_currently_allowed(interaction, event_name) is not None


async def _component_actor_if_currently_allowed(interaction: discord.Interaction, event_name: str) -> object | None:
    capability_id = EVENT_CAPABILITIES.get(event_name)
    guard = getattr(getattr(interaction, "client", None), "capability_guard", None)
    actor_for = getattr(guard, "actor", None)
    guild_id = getattr(interaction, "guild_id", None)
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if (
        capability_id is None
        or not callable(actor_for)
        or not isinstance(guild_id, int)
        or not isinstance(user_id, int)
    ):
        return None
    try:
        actor = await actor_for(interaction)
        if _component_level_still_allowed(interaction, event_name, actor_level=actor.level):
            return actor
    except Exception:
        pass
    return None


def _component_level_still_allowed(
    interaction: discord.Interaction,
    event_name: str,
    *,
    actor_level: object,
) -> bool:
    capability_id = EVENT_CAPABILITIES.get(event_name)
    guard = getattr(getattr(interaction, "client", None), "capability_guard", None)
    currently_allowed = getattr(guard, "currently_allowed", None)
    guild_id = getattr(interaction, "guild_id", None)
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if (
        capability_id is None
        or not callable(currently_allowed)
        or not isinstance(guild_id, int)
        or not isinstance(user_id, int)
        or actor_level is None
    ):
        return False
    try:
        return bool(
            currently_allowed(
                capability_id,
                guild_id=guild_id,
                user_id=user_id,
                actor_level=actor_level,
            )
        )
    except Exception:
        return False


async def _deny_changed(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "操作待機中に機能または権限が変更されたため、何も変更していません。",
        ephemeral=True,
        allowed_mentions=NO_MENTIONS,
    )
