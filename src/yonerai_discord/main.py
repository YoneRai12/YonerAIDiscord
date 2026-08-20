from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from typing import Any

from dotenv import load_dotenv

from .sandbox_doctor import run_sandbox_doctor
from .sandbox_operator_cli import (
    SandboxCliDependencies,
    dispatch_sandbox_command,
    parse_sandbox_args,
    render_sandbox_result,
    sandbox_exit_code,
)


logger = logging.getLogger(__name__)


def __getattr__(name: str) -> Any:
    """Resolve legacy bot-runtime patch points without cold-importing them."""
    if name not in {
        "ConfigurationError",
        "Settings",
        "configure_logging",
        "RuntimeInstanceLock",
        "RuntimeLockError",
    }:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from .config import ConfigurationError, Settings
    from .logging import configure_logging
    from .runtime_lock import RuntimeInstanceLock, RuntimeLockError

    runtime_attributes = {
        "ConfigurationError": ConfigurationError,
        "Settings": Settings,
        "configure_logging": configure_logging,
        "RuntimeInstanceLock": RuntimeInstanceLock,
        "RuntimeLockError": RuntimeLockError,
    }
    globals().update(runtime_attributes)
    return runtime_attributes[name]


def _load_bot_runtime() -> tuple[type[BaseException], Any, Any, Any, type[BaseException]]:
    """Import Discord-only runtime dependencies after sandbox CLI dispatch."""
    module = sys.modules[__name__]
    return (
        getattr(module, "ConfigurationError"),
        getattr(module, "Settings"),
        getattr(module, "configure_logging"),
        getattr(module, "RuntimeInstanceLock"),
        getattr(module, "RuntimeLockError"),
    )


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="YonerAI Discord BOT を起動します。")


def parse_cli_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    actual = tuple(sys.argv[1:] if argv is None else argv)
    if actual and actual[0] == "sandbox":
        parsed = parse_sandbox_args(actual[1:])
        parsed.root_command = "sandbox"
        return parsed
    parsed = _parser().parse_args(actual)
    parsed.root_command = "bot"
    return parsed


def _run_sandbox_cli(args: argparse.Namespace) -> int:
    result = asyncio.run(
        dispatch_sandbox_command(
            args,
            SandboxCliDependencies(doctor=run_sandbox_doctor),
        )
    )
    print(render_sandbox_result(result, json_mode=bool(getattr(args, "json_mode", False))))
    return sandbox_exit_code(result)


async def run(settings: Any) -> None:
    from .bot import YonerAIBot

    bot = YonerAIBot(settings)
    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        if not bot.is_closing:
            loop.create_task(bot.close(), name="signal-shutdown")

    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(handled_signal, request_shutdown)
        except (NotImplementedError, RuntimeError):
            # WindowsのProactorEventLoopなど、signal handler非対応環境では
            # KeyboardInterrupt後にmain()のfinallyでcloseする。
            break

    try:
        await bot.start(settings.discord_token, reconnect=True)
    finally:
        await bot.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    _parsed_args: argparse.Namespace | None = None,
) -> int | None:
    parsed = _parsed_args or parse_cli_args(argv)
    if parsed.root_command == "sandbox":
        return _run_sandbox_cli(parsed)
    configuration_error, settings_type, logging_configurator, lock_type, lock_error = _load_bot_runtime()
    load_dotenv()
    try:
        settings = settings_type.from_env()
    except configuration_error as exc:
        raise SystemExit(f"設定エラー: {exc}") from exc
    logging_configurator(
        settings.log_level,
        log_path=settings.log_path,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    runtime_lock = lock_type(settings.database_path)
    try:
        try:
            runtime_lock.acquire()
        except lock_error as exc:
            logger.error("runtime_lock_unavailable", extra={"error_type": type(exc).__name__})
            raise SystemExit("同じデータ領域のBotが起動中か、起動排他を確立できません。") from exc
        try:
            asyncio.run(run(settings))
        except KeyboardInterrupt:
            logger.info("keyboard_interrupt")
    finally:
        runtime_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
