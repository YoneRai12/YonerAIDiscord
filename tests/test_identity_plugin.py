from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from yonerai_discord.modules.identity import IdentityPlugin


class Tree:
    def __init__(self) -> None:
        self.commands = {}

    def add_command(self, command) -> None:
        self.commands[command.name] = command

    def remove_command(self, name: str) -> None:
        self.commands.pop(name, None)


class Bot:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.tree = Tree()

    def get_guild(self, guild_id: int):
        return None


def _settings(path: Path, **identity) -> SimpleNamespace:
    values = {
        "database_path": path,
        "bot_owner_ids": frozenset(),
        "identity_enabled": False,
        "identity_public_base_url": "",
        "identity_turnstile_secret": "",
        "identity_allow_insecure_localhost": False,
    }
    values.update(identity)
    return SimpleNamespace(**values)


async def test_plugin_registers_status_but_keeps_callback_off_by_default(tmp_path: Path) -> None:
    bot = Bot(_settings(tmp_path / "identity.sqlite3"))
    plugin = IdentityPlugin()
    await plugin.start(bot)
    assert "verify" in bot.tree.commands
    assert bot.identity_callback_service is None
    assert bot.runtime_capability_readiness == {
        "cap-run-verify-status": True,
        "cap-run-verify-start": False,
        "cap-run-verify-configure": True,
    }
    await plugin.stop()
    assert "verify" not in bot.tree.commands
    assert not hasattr(bot, "identity_callback_service")


async def test_localhost_callback_needs_both_explicit_switches(tmp_path: Path) -> None:
    bot = Bot(
        _settings(
            tmp_path / "identity.sqlite3",
            identity_enabled=True,
            identity_public_base_url="http://127.0.0.1:8080",
            identity_allow_insecure_localhost=True,
        )
    )
    plugin = IdentityPlugin()
    await plugin.start(bot)
    assert bot.identity_callback_service is not None
    assert bot.runtime_capability_readiness["cap-run-verify-start"] is True
    await plugin.stop()


async def test_public_callback_without_turnstile_fails_closed(tmp_path: Path) -> None:
    bot = Bot(
        _settings(
            tmp_path / "identity.sqlite3",
            identity_enabled=True,
            identity_public_base_url="https://identity.example.test",
        )
    )
    plugin = IdentityPlugin()
    await plugin.start(bot)
    assert bot.identity_callback_service is None
    assert bot.runtime_capability_readiness["cap-run-verify-start"] is False
    await plugin.stop()
