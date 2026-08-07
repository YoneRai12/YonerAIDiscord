from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest

from scripts import run_bot_from_src


bot_main = importlib.import_module("yonerai_discord.main")


def test_explicit_env_file_is_loaded_without_overriding_process_values(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BOOTSTRAP_NEW=value\nBOOTSTRAP_KEEP=file\n", encoding="utf-8")
    monkeypatch.setenv("YONERAI_ENV_FILE", str(env_file))
    monkeypatch.setenv("BOOTSTRAP_KEEP", "process")
    monkeypatch.delenv("BOOTSTRAP_NEW", raising=False)

    run_bot_from_src._load_explicit_env()

    assert os.environ["BOOTSTRAP_NEW"] == "value"
    assert os.environ["BOOTSTRAP_KEEP"] == "process"


@pytest.mark.parametrize(("argv", "exit_code"), [(["--help"], 0), (["--unknown"], 2)])
def test_source_bootstrap_cli_exits_before_explicit_env_or_bot_start(
    argv: list[str],
    exit_code: int,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def forbidden(name: str):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"unexpected side effect: {name}")

        return fail

    monkeypatch.setattr(run_bot_from_src, "_load_explicit_env", forbidden("explicit_env"))
    monkeypatch.setattr(run_bot_from_src, "load_dotenv", forbidden("dotenv"))
    monkeypatch.setattr(run_bot_from_src, "run_bot", forbidden("bot"))

    with pytest.raises(SystemExit) as raised:
        run_bot_from_src.main(argv)

    assert raised.value.code == exit_code
    assert calls == []


def test_source_bootstrap_without_arguments_preserves_env_then_bot_order(monkeypatch) -> None:
    calls: list[object] = []

    monkeypatch.setattr(run_bot_from_src, "parse_cli_args", lambda argv: calls.append(("parse", argv)))
    monkeypatch.setattr(run_bot_from_src, "_load_explicit_env", lambda: calls.append("explicit_env"))
    monkeypatch.setattr(run_bot_from_src, "run_bot", lambda argv: calls.append(("bot", argv)))

    run_bot_from_src.main([])

    assert calls == [("parse", []), "explicit_env", ("bot", ())]


@pytest.mark.parametrize(("argv", "exit_code"), [(["--help"], 0), (["--unknown"], 2)])
def test_bot_cli_exits_before_runtime_side_effects(
    argv: list[str],
    exit_code: int,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def forbidden(name: str):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"unexpected side effect: {name}")

        return fail

    monkeypatch.setattr(bot_main, "load_dotenv", forbidden("dotenv"))
    monkeypatch.setattr(bot_main.Settings, "from_env", forbidden("settings"))
    monkeypatch.setattr(bot_main, "configure_logging", forbidden("logging"))
    monkeypatch.setattr(bot_main, "RuntimeInstanceLock", forbidden("lock"))
    monkeypatch.setattr(bot_main.asyncio, "run", forbidden("asyncio"))
    monkeypatch.setattr(bot_main, "YonerAIBot", forbidden("bot"))

    with pytest.raises(SystemExit) as raised:
        bot_main.main(argv)

    assert raised.value.code == exit_code
    assert calls == []


def test_bot_cli_without_arguments_preserves_runtime_startup(monkeypatch) -> None:
    calls: list[object] = []
    settings = SimpleNamespace(
        log_level="INFO",
        log_path=None,
        log_max_bytes=1,
        log_backup_count=1,
        database_path="runtime.sqlite3",
    )
    run_token = object()

    class FakeSettings:
        @classmethod
        def from_env(cls):
            calls.append("settings")
            return settings

    class FakeLock:
        def __init__(self, database_path):
            calls.append(("lock", database_path))

        def acquire(self):
            calls.append("acquire")

        def release(self):
            calls.append("release")

    monkeypatch.setattr(bot_main, "load_dotenv", lambda: calls.append("dotenv"))
    monkeypatch.setattr(bot_main, "Settings", FakeSettings)
    monkeypatch.setattr(bot_main, "configure_logging", lambda *args, **kwargs: calls.append("logging"))
    monkeypatch.setattr(bot_main, "RuntimeInstanceLock", FakeLock)
    monkeypatch.setattr(bot_main, "run", lambda actual: run_token if actual is settings else None)
    monkeypatch.setattr(bot_main.asyncio, "run", lambda actual: calls.append(("asyncio", actual)))

    bot_main.main([])

    assert calls == [
        "dotenv",
        "settings",
        "logging",
        ("lock", "runtime.sqlite3"),
        "acquire",
        ("asyncio", run_token),
        "release",
    ]
