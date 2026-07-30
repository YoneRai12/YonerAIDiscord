from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from yonerai_discord.modules.modtools import setup
from yonerai_discord.modules.modtools.plugin import ModtoolsPlugin


EXPECTED_COMMANDS = {
    "warn",
    "warnings",
    "timeout",
    "untimeout",
    "kick",
    "ban",
    "unban",
    "purge",
    "purge-user",
    "purge-links",
    "case",
}


def test_setup_registers_lazy_plugin_factory_without_importing_discord() -> None:
    calls = []
    setup(SimpleNamespace(register=lambda name, factory: calls.append((name, factory))))
    assert len(calls) == 1
    assert calls[0][0] == "modtools"
    assert callable(calls[0][1])


def test_adapter_defines_all_documented_commands() -> None:
    path = Path(__file__).parents[1] / "src" / "yonerai_discord" / "modules" / "modtools" / "adapter.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    commands: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            function = decorator.func
            if not isinstance(function, ast.Attribute) or function.attr != "command":
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    commands.add(str(keyword.value.value))
    assert commands == EXPECTED_COMMANDS


@pytest.mark.asyncio
async def test_plugin_publishes_only_its_current_repository_and_withdraws_it(tmp_path: Path) -> None:
    commands: list[object] = []
    bot = SimpleNamespace(
        settings=SimpleNamespace(database_path=tmp_path / "modtools.sqlite3"),
        tree=SimpleNamespace(
            add_command=lambda command: commands.append(command),
            remove_command=lambda *_args, **_kwargs: commands.clear(),
        ),
    )
    plugin = ModtoolsPlugin()

    await plugin.start(bot)

    assert bot.modtools_repository is plugin.repository
    assert bot.modtools_plugin is plugin
    assert plugin.closing is False

    await plugin.stop()

    assert not hasattr(bot, "modtools_repository")
    assert not hasattr(bot, "modtools_plugin")
