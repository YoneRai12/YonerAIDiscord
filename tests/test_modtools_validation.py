from __future__ import annotations

import pytest

from yonerai_discord.modules.modtools import (
    ModAction,
    PermissionSnapshot,
    decide_permission,
    validate_purge,
    validate_reason,
    validate_timeout_minutes,
)


def snapshot(**overrides: object) -> PermissionSnapshot:
    values: dict[str, object] = {
        "guild_owner_id": 1,
        "bot_user_id": 2,
        "actor_id": 10,
        "actor_permissions": frozenset({"moderate_members", "kick_members", "ban_members", "manage_messages"}),
        "bot_permissions": frozenset({"moderate_members", "kick_members", "ban_members", "manage_messages"}),
        "actor_top_role": 50,
        "bot_top_role": 100,
        "target_id": 20,
        "target_top_role": 25,
    }
    values.update(overrides)
    return PermissionSnapshot(**values)  # type: ignore[arg-type]


def test_permission_requires_actor_and_bot_permission() -> None:
    actor_denied = decide_permission(
        ModAction.KICK,
        snapshot(actor_permissions=frozenset({"moderate_members"})),
    )
    assert not actor_denied.allowed
    assert "kick_members" in actor_denied.reason
    bot_denied = decide_permission(
        ModAction.BAN,
        snapshot(bot_permissions=frozenset({"moderate_members"})),
    )
    assert not bot_denied.allowed
    assert "Bot" in bot_denied.reason


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"target_id": 10}, "自分自身"),
        ({"target_id": 2}, "Bot自身"),
        ({"target_id": 1}, "所有者"),
        ({"target_top_role": 50}, "あなたのロール"),
        ({"target_top_role": 100}, "Botのロール"),
    ],
)
def test_target_protection_and_hierarchy(changes: dict[str, object], message: str) -> None:
    decision = decide_permission(ModAction.TIMEOUT, snapshot(**changes))
    assert not decision.allowed
    assert message in decision.reason


def test_owner_bypasses_actor_permission_and_role_but_not_bot_hierarchy() -> None:
    allowed = decide_permission(
        ModAction.BAN,
        snapshot(
            actor_id=1,
            actor_permissions=frozenset(),
            actor_top_role=1,
            target_top_role=50,
        ),
    )
    assert allowed.allowed
    denied = decide_permission(
        ModAction.BAN,
        snapshot(actor_id=1, actor_permissions=frozenset(), target_top_role=100),
    )
    assert not denied.allowed


def test_read_only_warning_lookup_does_not_apply_target_hierarchy() -> None:
    decision = decide_permission(
        ModAction.WARNINGS,
        snapshot(target_id=1, target_top_role=1000),
    )
    assert decision.allowed


@pytest.mark.parametrize("reason", ["", "   ", "\n"])
def test_reason_is_required(reason: str) -> None:
    with pytest.raises(ValueError, match="必須"):
        validate_reason(reason)


def test_purge_has_hard_limit_reason_and_explicit_confirmation() -> None:
    with pytest.raises(ValueError, match="1〜100"):
        validate_purge(101, "cleanup", dry_run=True, confirm="")
    with pytest.raises(ValueError, match="PURGE"):
        validate_purge(10, "cleanup", dry_run=False, confirm="yes")
    assert validate_purge(100, " cleanup ", dry_run=False, confirm="PURGE") == "cleanup"
    assert validate_purge(10, "preview", dry_run=True, confirm="") == "preview"


@pytest.mark.parametrize("minutes", [0, 40321])
def test_timeout_is_limited_to_discord_max(minutes: int) -> None:
    with pytest.raises(ValueError, match="28日"):
        validate_timeout_minutes(minutes)
