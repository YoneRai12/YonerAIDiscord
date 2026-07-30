from __future__ import annotations

from .domain import ActorPermissions, HierarchyContext, PolicyViolation, ServerAction


def require_action_permission(action: ServerAction, permissions: ActorPermissions) -> None:
    if permissions.administrator:
        return
    allowed = {
        ServerAction.SLOWMODE: permissions.manage_channels,
        ServerAction.LOCK: permissions.manage_channels,
        ServerAction.UNLOCK: permissions.manage_channels,
        ServerAction.NICK: permissions.manage_nicknames,
        ServerAction.ROLE_ADD: permissions.manage_roles,
        ServerAction.ROLE_REMOVE: permissions.manage_roles,
        ServerAction.ANNOUNCE: permissions.manage_guild,
        ServerAction.CONFIGURE: permissions.manage_guild,
    }[action]
    if not allowed:
        raise PolicyViolation("missing_permission", f"permission denied for {action.value}")


def require_member_hierarchy(context: HierarchyContext) -> None:
    if context.target_is_owner:
        raise PolicyViolation("guild_owner", "guild owner cannot be modified")
    if not context.actor_is_owner and context.actor_top_role <= context.target_member_top_role:
        raise PolicyViolation("actor_hierarchy", "target member is not below the actor")
    if context.bot_top_role <= context.target_member_top_role:
        raise PolicyViolation("bot_hierarchy", "target member is not below the bot")


def require_role_hierarchy(context: HierarchyContext) -> None:
    require_member_hierarchy(context)
    if context.target_role_position is None:
        raise ValueError("target_role_position is required")
    if not context.actor_is_owner and context.actor_top_role <= context.target_role_position:
        raise PolicyViolation("actor_role_hierarchy", "role is not below the actor")
    if context.bot_top_role <= context.target_role_position:
        raise PolicyViolation("bot_role_hierarchy", "role is not below the bot")


def validate_announcement(
    content: str,
    *,
    allow_everyone: bool,
    reason: str,
    administrator: bool,
) -> bool:
    if not content.strip() or len(content) > 2_000:
        raise ValueError("announcement must be 1..2000 characters")
    requests_everyone = "@everyone" in content or "@here" in content
    if not requests_everyone:
        return False
    if not allow_everyone:
        raise PolicyViolation("everyone_not_allowed", "explicit allow_everyone is required")
    if not administrator:
        raise PolicyViolation("administrator_required", "administrator is required")
    if not reason.strip() or len(reason.strip()) > 300:
        raise PolicyViolation("reason_required", "a reason of 1..300 characters is required")
    return True
