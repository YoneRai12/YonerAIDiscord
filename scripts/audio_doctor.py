import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yonerai_discord.modules.audio_core.doctor import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
