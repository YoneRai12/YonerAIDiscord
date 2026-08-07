"""開発checkoutのsrcを優先してBotを起動する薄いbootstrap。"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yonerai_discord.main import main as run_bot  # noqa: E402
from yonerai_discord.main import parse_cli_args  # noqa: E402


def _load_explicit_env() -> None:
    raw = os.environ.get("YONERAI_ENV_FILE", "").strip()
    if not raw:
        return
    env_file = Path(raw)
    if not env_file.is_absolute() or env_file.is_symlink() or not env_file.is_file():
        raise SystemExit("YONERAI_ENV_FILE must be an absolute regular file")
    load_dotenv(dotenv_path=env_file, override=False)


def main(argv: Sequence[str] | None = None) -> None:
    parse_cli_args(argv)
    _load_explicit_env()
    run_bot(())


if __name__ == "__main__":
    main()
