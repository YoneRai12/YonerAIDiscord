from __future__ import annotations

import pytest

from yonerai_discord.modules.community import (
    ActorPolicy,
    can_configure_selfroles,
    can_manage_poll,
    can_manage_ticket,
    can_update_suggestion,
    selfrole_permissions_are_safe,
)
from yonerai_discord.modules.community.domain import Poll, Suggestion, Ticket


def actor(actor_id: int = 10, **permissions: bool) -> ActorPolicy:
    return ActorPolicy(actor_id=actor_id, guild_owner_id=1, **permissions)


def test_ticket_owner_or_privileged_moderator_can_manage() -> None:
    assert can_manage_ticket(actor(10), ticket_owner_id=10)
    assert can_manage_ticket(actor(1), ticket_owner_id=10)
    assert can_manage_ticket(actor(20, administrator=True), ticket_owner_id=10)
    assert can_manage_ticket(actor(20, manage_channels=True), ticket_owner_id=10)
    assert not can_manage_ticket(actor(20), ticket_owner_id=10)


def test_poll_creator_or_message_moderator_can_manage() -> None:
    assert can_manage_poll(actor(10), poll_creator_id=10)
    assert can_manage_poll(actor(20, manage_messages=True), poll_creator_id=10)
    assert can_manage_poll(actor(1), poll_creator_id=10)
    assert not can_manage_poll(actor(20), poll_creator_id=10)


def test_admin_policies_are_scoped_to_required_capability() -> None:
    assert can_update_suggestion(actor(20, manage_guild=True))
    assert not can_update_suggestion(actor(20, manage_messages=True))
    assert can_configure_selfroles(actor(20, manage_roles=True))
    assert not can_configure_selfroles(actor(20, manage_channels=True))


def test_selfrole_cannot_grant_moderation_or_administration() -> None:
    safe = dict(
        administrator=False,
        manage_guild=False,
        manage_roles=False,
        manage_channels=False,
        ban_members=False,
        kick_members=False,
        moderate_members=False,
    )
    assert selfrole_permissions_are_safe(**safe)
    assert not selfrole_permissions_are_safe(**(safe | {"administrator": True}))
    assert not selfrole_permissions_are_safe(**(safe | {"moderate_members": True}))


@pytest.mark.parametrize(
    ("factory", "args"),
    [
        (Ticket, ("ticket", 0, 1, "件名")),
        (Poll, ("poll", 1, 0, "質問", ("A", "B"))),
        (Suggestion, ("suggestion", 1, 0, "本文")),
    ],
)
def test_persisted_community_records_reject_invalid_discord_ids(factory, args) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        factory(*args)


def test_community_identifiers_are_normalized_and_control_characters_rejected() -> None:
    ticket = Ticket(" ticket ", 1, 2, " 件名 ")
    assert ticket.id == "ticket"
    assert ticket.subject == "件名"
    with pytest.raises(ValueError):
        Ticket("ticket\nother", 1, 2, "件名")
