from __future__ import annotations

import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def isolated_workspace_directory() -> Iterator[Path]:
    """pytest tmp_pathを使わず、workspace内だけで隔離用directoryを管理する。"""

    root = (Path.cwd() / "tmp" / "earthquake-tests").resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / uuid.uuid4().hex).resolve()
    if root not in target.parents:
        raise RuntimeError("test directory escaped the workspace root")
    target.mkdir()
    try:
        yield target
    finally:
        if target.exists() and root in target.parents:
            shutil.rmtree(target)
