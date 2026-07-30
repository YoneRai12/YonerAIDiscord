from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from yonerai_discord.db import Database


def _module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "verify_backup.py"
    spec = importlib.util.spec_from_file_location("verify_backup", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_online_backup_passes_read_only_restore_preflight(tmp_path: Path) -> None:
    database = Database(tmp_path / "live.sqlite3")
    database.open()
    database.migrate()
    destination = tmp_path / "backup.sqlite3"
    try:
        database.online_backup(destination)
    finally:
        database.close()

    report = _module().verify_backup(destination)

    assert report.valid
    assert report.quick_check == ("ok",)
    assert report.schema_versions[-1] >= 2


def test_invalid_backup_is_rejected_without_modification(tmp_path: Path) -> None:
    path = tmp_path / "invalid.sqlite3"
    original = b"not sqlite"
    path.write_bytes(original)

    with pytest.raises(ValueError, match="suite database"):
        _module().verify_backup(path)

    assert path.read_bytes() == original
