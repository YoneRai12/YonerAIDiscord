from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Sequence

from dotenv import load_dotenv

from .bot import YonerAIBot
from .config import ConfigurationError, Settings
from .logging import configure_logging
from .runtime_lock import RuntimeInstanceLock, RuntimeLockError


logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="YonerAI Discord BOT を起動します。")


def parse_cli_args(argv: Sequence[str] | None = None) -> None:
    _parser().parse_args(argv)


async def run(settings: Settings) -> None:
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


def main(argv: Sequence[str] | None = None) -> None:
    parse_cli_args(argv)
    load_dotenv()
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        raise SystemExit(f"設定エラー: {exc}") from exc
    configure_logging(
        settings.log_level,
        log_path=settings.log_path,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    runtime_lock = RuntimeInstanceLock(settings.database_path)
    try:
        try:
            runtime_lock.acquire()
        except RuntimeLockError as exc:
            logger.error("runtime_lock_unavailable", extra={"error_type": type(exc).__name__})
            raise SystemExit("同じデータ領域のBotが起動中か、起動排他を確立できません。") from exc
        try:
            asyncio.run(run(settings))
        except KeyboardInterrupt:
            logger.info("keyboard_interrupt")
    finally:
        runtime_lock.release()


if __name__ == "__main__":
    main()
