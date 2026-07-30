from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .config import Settings
from .control_plane import ActorContext, RbacLevel


_SUBCOMMAND_TYPES = {1, 2}


def command_path(data: Mapping[str, Any] | None) -> str | None:
    """Discord interaction dataから ``group subcommand`` を決定論的に復元する。"""

    if not isinstance(data, Mapping):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    parts = [name.strip().lower()]
    options = data.get("options")
    while isinstance(options, list) and options:
        first = options[0]
        if not isinstance(first, Mapping) or first.get("type") not in _SUBCOMMAND_TYPES:
            break
        child_name = first.get("name")
        if not isinstance(child_name, str) or not child_name.strip():
            return None
        parts.append(child_name.strip().lower())
        options = first.get("options")
    return " ".join(parts)


def determine_rbac_level(
    *,
    user_id: int,
    guild_owner_id: int | None,
    permissions: Any | None,
    role_ids: frozenset[int],
    settings: Settings,
    application_owner: bool = False,
) -> RbacLevel:
    """Discord SDKの事実を中央RBACへ単調に写像する。"""

    if application_owner or user_id in settings.bot_owner_ids:
        return RbacLevel.BOT_OWNER
    if guild_owner_id is not None and user_id == guild_owner_id:
        return RbacLevel.GUILD_OWNER
    if permissions is not None and (
        bool(getattr(permissions, "administrator", False)) or bool(getattr(permissions, "manage_guild", False))
    ):
        return RbacLevel.GUILD_ADMIN
    if role_ids.intersection(settings.moderator_role_ids) or (
        permissions is not None
        and any(
            bool(getattr(permissions, permission, False))
            for permission in ("moderate_members", "manage_messages", "kick_members", "ban_members")
        )
    ):
        return RbacLevel.MODERATOR
    if role_ids.intersection(settings.trusted_role_ids):
        return RbacLevel.TRUSTED
    return RbacLevel.EVERYONE


async def actor_context_for_interaction(interaction: Any, bot: Any, settings: Settings) -> ActorContext:
    user = interaction.user
    guild = getattr(interaction, "guild", None)
    guild_id = getattr(interaction, "guild_id", None)
    return await actor_context_for_member(
        member=user,
        guild=guild,
        guild_id=int(guild_id) if guild_id is not None else None,
        bot=bot,
        settings=settings,
    )


async def actor_context_for_member(
    *,
    member: Any,
    guild: Any | None,
    guild_id: int | None,
    bot: Any,
    settings: Settings,
) -> ActorContext:
    """REST再取得済みmemberを中央RBACへ写像する。

    ``bot.is_owner`` の参照失敗は権限昇格として扱わない。呼び出し側は
    このcontextを使って、副作用直前にPolicyEngineを再評価する。
    """

    user_id = int(member.id)
    application_owner = False
    try:
        application_owner = bool(await bot.is_owner(member))
    except Exception:
        # Discord application情報の取得失敗で権限を昇格しない。
        application_owner = False

    guild_owner_id = int(guild.owner_id) if guild is not None and guild.owner_id is not None else None
    permissions = getattr(member, "guild_permissions", None)
    roles = getattr(member, "roles", ())
    role_ids = frozenset(int(role.id) for role in roles if getattr(role, "id", None) is not None)
    return ActorContext(
        actor_id=user_id,
        guild_id=guild_id,
        level=determine_rbac_level(
            user_id=user_id,
            guild_owner_id=guild_owner_id,
            permissions=permissions,
            role_ids=role_ids,
            settings=settings,
            application_owner=application_owner,
        ),
    )
