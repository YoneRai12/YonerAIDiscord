from __future__ import annotations

from types import SimpleNamespace

from yonerai_discord.config import Settings
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.discord_policy import command_path, determine_rbac_level


def settings(**changes: str) -> Settings:
    environment = {"DISCORD_TOKEN": "unused-test-token"} | changes
    return Settings.from_env(environment)


def test_command_path_handles_nested_subcommand_and_ignores_arguments() -> None:
    assert (
        command_path(
            {
                "name": "System",
                "options": [
                    {
                        "type": 2,
                        "name": "Config",
                        "options": [
                            {
                                "type": 1,
                                "name": "Set",
                                "options": [{"type": 3, "name": "value", "value": "secret"}],
                            }
                        ],
                    }
                ],
            }
        )
        == "system config set"
    )
    assert command_path({"name": "AI", "options": [{"type": 1, "name": "Ask"}]}) == "ai ask"
    assert command_path(None) is None


def test_rbac_precedence_and_role_mapping() -> None:
    config = settings(BOT_OWNER_IDS="10", TRUSTED_ROLE_IDS="20", MODERATOR_ROLE_IDS="30")
    none = SimpleNamespace(
        administrator=False,
        manage_guild=False,
        moderate_members=False,
        manage_messages=False,
        kick_members=False,
        ban_members=False,
    )
    assert (
        determine_rbac_level(
            user_id=10,
            guild_owner_id=10,
            permissions=none,
            role_ids=frozenset(),
            settings=config,
        )
        is RbacLevel.BOT_OWNER
    )
    assert (
        determine_rbac_level(
            user_id=11,
            guild_owner_id=11,
            permissions=none,
            role_ids=frozenset(),
            settings=config,
        )
        is RbacLevel.GUILD_OWNER
    )
    assert (
        determine_rbac_level(
            user_id=12,
            guild_owner_id=None,
            permissions=SimpleNamespace(administrator=False, manage_guild=True),
            role_ids=frozenset({20}),
            settings=config,
        )
        is RbacLevel.GUILD_ADMIN
    )
    assert (
        determine_rbac_level(
            user_id=13,
            guild_owner_id=None,
            permissions=none,
            role_ids=frozenset({30}),
            settings=config,
        )
        is RbacLevel.MODERATOR
    )
    assert (
        determine_rbac_level(
            user_id=14,
            guild_owner_id=None,
            permissions=none,
            role_ids=frozenset({20}),
            settings=config,
        )
        is RbacLevel.TRUSTED
    )
    assert (
        determine_rbac_level(
            user_id=15,
            guild_owner_id=None,
            permissions=none,
            role_ids=frozenset(),
            settings=config,
        )
        is RbacLevel.EVERYONE
    )
