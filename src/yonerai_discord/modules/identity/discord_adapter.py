from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from .models import ChallengeBinding, ChallengeIntent, ChallengePurpose, ClaimedChallenge
from .service import FeatureDisabledError, IdentityService
from .sqlite_repository import SQLiteChallengeRepository


NO_MENTIONS = discord.AllowedMentions.none()
_PRIVILEGED_ROLE_PERMISSIONS = frozenset(
    {
        "administrator",
        "moderate_members",
        "kick_members",
        "ban_members",
        "mention_everyone",
        "view_audit_log",
        "view_guild_insights",
        "mute_members",
        "deafen_members",
        "move_members",
        "priority_speaker",
    }
)


class UnsafeVerificationRole(ValueError):
    pass


def _enabled_permission_names(permissions: Any) -> frozenset[str]:
    names: set[str] = set()
    if permissions is None:
        return frozenset()
    try:
        for item in permissions:
            if isinstance(item, tuple) and len(item) == 2 and item[1] is True:
                names.add(str(item[0]))
    except TypeError:
        pass
    try:
        values = vars(permissions)
    except TypeError:
        values = {}
    names.update(str(name) for name, value in values.items() if value is True)
    names.update(name for name in _PRIVILEGED_ROLE_PERMISSIONS if bool(getattr(permissions, name, False)))
    return frozenset(names)


def _contains_privileged_permission(permissions: Any) -> bool:
    return any(
        name.startswith("manage_") or name in _PRIVILEGED_ROLE_PERMISSIONS
        for name in _enabled_permission_names(permissions)
    )


def _role_has_privileged_channel_overwrite(guild: Any, role: Any) -> bool:
    for channel in tuple(getattr(guild, "channels", ())):
        overwrite_for = getattr(channel, "overwrites_for", None)
        if not callable(overwrite_for):
            continue
        try:
            overwrite = overwrite_for(role)
        except Exception:
            # Cached channel state must be inspectable before accepting a security role.
            return True
        if _contains_privileged_permission(overwrite):
            return True
    return False


def validate_verification_role(
    guild: Any,
    role: Any,
    *,
    target: Any | None = None,
    actor: Any | None = None,
) -> None:
    role_guild = getattr(role, "guild", None)
    if role_guild is not None and getattr(role_guild, "id", None) != getattr(guild, "id", None):
        raise UnsafeVerificationRole("role belongs to another guild")
    bot_member = getattr(guild, "me", None)
    if bot_member is None:
        raise UnsafeVerificationRole("Bot member is unavailable")
    bot_permissions = getattr(bot_member, "guild_permissions", None)
    if not (
        bool(getattr(bot_permissions, "administrator", False)) or bool(getattr(bot_permissions, "manage_roles", False))
    ):
        raise UnsafeVerificationRole("Bot needs Manage Roles")
    is_default = getattr(role, "is_default", None)
    if (callable(is_default) and is_default()) or getattr(role, "id", None) == getattr(guild, "id", None):
        raise UnsafeVerificationRole("default role cannot be assigned")
    if bool(getattr(role, "managed", False)):
        raise UnsafeVerificationRole("managed role cannot be assigned")
    if _contains_privileged_permission(getattr(role, "permissions", None)):
        raise UnsafeVerificationRole("privileged role cannot be used for verification")
    if _role_has_privileged_channel_overwrite(guild, role):
        raise UnsafeVerificationRole("privileged channel overwrite cannot be used for verification")
    bot_top = getattr(getattr(bot_member, "top_role", None), "position", -1)
    role_position = getattr(role, "position", 0)
    if not isinstance(bot_top, int) or not isinstance(role_position, int) or bot_top <= role_position:
        raise UnsafeVerificationRole("verification role is outside the Bot hierarchy")
    if actor is not None:
        actor_id = getattr(actor, "id", None)
        guild_owner = actor_id == getattr(guild, "owner_id", None)
        actor_permissions = getattr(actor, "guild_permissions", None)
        administrator = bool(getattr(actor_permissions, "administrator", False))
        if not guild_owner and not (
            administrator
            or (
                bool(getattr(actor_permissions, "manage_guild", False))
                and bool(getattr(actor_permissions, "manage_roles", False))
            )
        ):
            raise UnsafeVerificationRole("actor cannot configure verification roles")
        actor_top = getattr(getattr(actor, "top_role", None), "position", -1)
        if not guild_owner and (not isinstance(actor_top, int) or actor_top <= role_position):
            raise UnsafeVerificationRole("verification role is outside the actor hierarchy")
    if target is not None:
        roles = tuple(getattr(target, "roles", ()))
        if not any(getattr(item, "id", None) == getattr(role, "id", None) for item in roles):
            target_top = getattr(getattr(target, "top_role", None), "position", -1)
            if not isinstance(target_top, int) or bot_top <= target_top:
                raise UnsafeVerificationRole("target member is outside the Bot hierarchy")


class DiscordVerificationRoleGranter:
    def __init__(self, bot: Any, repository: SQLiteChallengeRepository) -> None:
        self._bot = bot
        self._repository = repository

    async def grant(self, claimed: ClaimedChallenge) -> bool:
        if (
            claimed.purpose is not ChallengePurpose.VERIFICATION
            or claimed.binding.intent is not ChallengeIntent.VERIFY_MEMBER
        ):
            return False
        guild = self._bot.get_guild(claimed.binding.guild_id)
        if guild is None:
            return False
        config = self._repository.guild_config(claimed.binding.guild_id)
        if config is None or not config.enabled:
            return False
        role = guild.get_role(config.verified_role_id)
        if role is None:
            return False
        try:
            # REST再取得により、cache上の別memberを対象判断へ使わない。
            member = await guild.fetch_member(claimed.binding.user_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return False
        # Discord副作用の直前に、永続設定・guild/user binding・権限・hierarchyを再検査する。
        fresh = self._repository.guild_config(claimed.binding.guild_id)
        if fresh is None or not fresh.enabled or fresh.verified_role_id != config.verified_role_id:
            return False
        registry = getattr(self._bot, "capability_registry", None)
        try:
            centrally_enabled = bool(
                registry
                and registry.is_capability_enabled(
                    "cap-run-verify-start",
                    claimed.binding.guild_id,
                )
            )
        except Exception:
            centrally_enabled = False
        if not centrally_enabled:
            return False
        try:
            validate_verification_role(guild, role, target=member)
        except UnsafeVerificationRole:
            return False
        if any(getattr(item, "id", None) == role.id for item in getattr(member, "roles", ())):
            return True
        try:
            await member.add_roles(role, reason="YonerAI member verification completed")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return False
        return True


class VerifyGroup(app_commands.Group):
    def __init__(
        self,
        bot: Any,
        repository: SQLiteChallengeRepository,
        identity: IdentityService | None,
        *,
        global_enabled: bool,
    ) -> None:
        super().__init__(name="verify", description="安全な本人確認")
        self.bot = bot
        self.repository = repository
        self.identity = identity
        self.global_enabled = global_enabled

    @app_commands.command(name="status", description="本人確認の有効状態だけを表示します")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        config = self.repository.guild_config(interaction.guild_id) if interaction.guild_id else None
        lines = (
            f"所有者側switch: {'ON' if self.global_enabled else 'OFF'}",
            f"callback/Turnstile: {'準備済み' if self.identity is not None else '未設定'}",
            f"サーバー側switch: {'ON' if config and config.enabled else 'OFF'}",
            f"認証role: {'設定済み' if config else '未設定'}",
        )
        await _reply(interaction, "\n".join(lines))

    @app_commands.command(name="start", description="本人専用のワンタイム認証URLを発行します")
    @app_commands.guild_only()
    async def start(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or interaction.guild_id is None or not isinstance(member, discord.Member):
            await _reply(interaction, "サーバー内のメンバーだけが利用できます。")
            return
        config = self.repository.guild_config(interaction.guild_id)
        if not self.global_enabled or self.identity is None or config is None or not config.enabled:
            await _reply(interaction, "本人確認はまだ利用可能になっていません。")
            return
        role = guild.get_role(config.verified_role_id)
        if role is None:
            await _reply(interaction, "認証roleの設定が無効です。管理者へ連絡してください。")
            return
        try:
            validate_verification_role(guild, role, target=member)
            issued = self.identity.issue(
                ChallengePurpose.VERIFICATION,
                ChallengeBinding(interaction.guild_id, member.id, ChallengeIntent.VERIFY_MEMBER),
            )
            url = self.identity.verification_url(issued)
        except (UnsafeVerificationRole, FeatureDisabledError, ValueError):
            await _reply(interaction, "安全設定を満たさないため認証URLを発行できません。")
            return
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="本人確認を開始", style=discord.ButtonStyle.link, url=url))
        await _reply(
            interaction,
            "このボタンは本人専用・一度限り・短時間有効です。転送しないでください。",
            view=view,
        )

    @app_commands.command(name="configure", description="認証roleとサーバー側switchを設定します")
    @app_commands.guild_only()
    async def configure(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        enabled: bool,
    ) -> None:
        guild = interaction.guild
        if guild is None or interaction.guild_id is None:
            await _reply(interaction, "サーバー内でのみ設定できます。")
            return
        if not self._can_configure(interaction):
            await _reply(interaction, "Manage ServerとManage Rolesを持つ管理者だけが設定できます。")
            return
        try:
            validate_verification_role(guild, role, actor=interaction.user)
        except UnsafeVerificationRole:
            await _reply(interaction, "管理権限付き・managed・Bot以上のroleは認証roleにできません。")
            return
        self.repository.configure_guild(
            interaction.guild_id,
            role.id,
            enabled,
            interaction.user.id,
        )
        state = "ON" if enabled else "OFF"
        await _reply(interaction, f"サーバー側の本人確認switchを{state}にしました。")

    @staticmethod
    def _can_configure(interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild is not None and interaction.user.id == getattr(guild, "owner_id", None):
            return True
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(
            getattr(permissions, "administrator", False)
            or (getattr(permissions, "manage_guild", False) and getattr(permissions, "manage_roles", False))
        )


async def _reply(interaction: discord.Interaction, message: str, **extra: Any) -> None:
    kwargs = {"ephemeral": True, "allowed_mentions": NO_MENTIONS, **extra}
    if interaction.response.is_done():
        await interaction.followup.send(message[:1_900], **kwargs)
    else:
        await interaction.response.send_message(message[:1_900], **kwargs)
