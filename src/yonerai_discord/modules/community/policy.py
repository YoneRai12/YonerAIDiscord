from __future__ import annotations

from .domain import ActorPolicy


def can_manage_ticket(actor: ActorPolicy, ticket_owner_id: int) -> bool:
    return actor.actor_id == ticket_owner_id or actor.is_owner_or_admin or actor.manage_channels


def can_manage_poll(actor: ActorPolicy, poll_creator_id: int) -> bool:
    return actor.actor_id == poll_creator_id or actor.is_owner_or_admin or actor.manage_messages


def can_update_suggestion(actor: ActorPolicy) -> bool:
    return actor.is_owner_or_admin or actor.manage_guild


def can_configure_selfroles(actor: ActorPolicy) -> bool:
    return actor.is_owner_or_admin or actor.manage_roles


def selfrole_permissions_are_safe(
    *,
    administrator: bool,
    manage_guild: bool,
    manage_roles: bool,
    manage_channels: bool,
    ban_members: bool,
    kick_members: bool,
    moderate_members: bool,
) -> bool:
    """セルフ付与で権限昇格につながる管理系ロールを拒否する。"""
    return not any(
        (administrator, manage_guild, manage_roles, manage_channels, ban_members, kick_members, moderate_members)
    )
