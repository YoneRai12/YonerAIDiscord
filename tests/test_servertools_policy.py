from __future__ import annotations

import pytest

from yonerai_discord.modules.servertools import (
    ActorPermissions,
    HierarchyContext,
    PolicyViolation,
    ServerAction,
    require_action_permission,
    require_member_hierarchy,
    require_role_hierarchy,
    validate_announcement,
)


@pytest.mark.parametrize(
    ("action", "permissions"),
    [
        (ServerAction.SLOWMODE, ActorPermissions(manage_channels=True)),
        (ServerAction.LOCK, ActorPermissions(manage_channels=True)),
        (ServerAction.UNLOCK, ActorPermissions(manage_channels=True)),
        (ServerAction.NICK, ActorPermissions(manage_nicknames=True)),
        (ServerAction.ROLE_ADD, ActorPermissions(manage_roles=True)),
        (ServerAction.ROLE_REMOVE, ActorPermissions(manage_roles=True)),
        (ServerAction.ANNOUNCE, ActorPermissions(manage_guild=True)),
        (ServerAction.CONFIGURE, ActorPermissions(manage_guild=True)),
    ],
)
def test_action_permissions(action: ServerAction, permissions: ActorPermissions) -> None:
    require_action_permission(action, permissions)
    with pytest.raises(PolicyViolation, match="permission denied"):
        require_action_permission(action, ActorPermissions())


def test_administrator_can_run_every_action() -> None:
    for action in ServerAction:
        require_action_permission(action, ActorPermissions(administrator=True))


def test_member_hierarchy_checks_actor_bot_and_owner() -> None:
    require_member_hierarchy(HierarchyContext(10, 9, 5))
    with pytest.raises(PolicyViolation) as actor_error:
        require_member_hierarchy(HierarchyContext(5, 9, 5))
    assert actor_error.value.code == "actor_hierarchy"
    with pytest.raises(PolicyViolation) as bot_error:
        require_member_hierarchy(HierarchyContext(10, 5, 5))
    assert bot_error.value.code == "bot_hierarchy"
    with pytest.raises(PolicyViolation) as owner_error:
        require_member_hierarchy(HierarchyContext(10, 9, 1, target_is_owner=True))
    assert owner_error.value.code == "guild_owner"


def test_role_hierarchy_checks_target_role() -> None:
    require_role_hierarchy(HierarchyContext(10, 9, 3, target_role_position=4))
    with pytest.raises(PolicyViolation) as error:
        require_role_hierarchy(HierarchyContext(10, 4, 3, target_role_position=4))
    assert error.value.code == "bot_role_hierarchy"


def test_everyone_announcement_requires_all_three_safety_conditions() -> None:
    with pytest.raises(PolicyViolation) as missing_flag:
        validate_announcement("@everyone maintenance", allow_everyone=False, reason="planned", administrator=True)
    assert missing_flag.value.code == "everyone_not_allowed"
    with pytest.raises(PolicyViolation) as non_admin:
        validate_announcement("@here maintenance", allow_everyone=True, reason="planned", administrator=False)
    assert non_admin.value.code == "administrator_required"
    with pytest.raises(PolicyViolation) as missing_reason:
        validate_announcement("@everyone maintenance", allow_everyone=True, reason=" ", administrator=True)
    assert missing_reason.value.code == "reason_required"
    assert (
        validate_announcement(
            "@everyone maintenance", allow_everyone=True, reason="planned maintenance", administrator=True
        )
        is True
    )


def test_normal_announcement_never_enables_everyone_mentions() -> None:
    assert validate_announcement("maintenance notice", allow_everyone=False, reason="", administrator=False) is False
