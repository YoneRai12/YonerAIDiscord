from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "security_preflight.py"
    spec = importlib.util.spec_from_file_location("security_preflight", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scanner_reports_kind_and_location_without_echoing_secret(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    secret = "sk-proj-" + "A" * 28
    (tmp_path / "bad.py").write_text(f"value = {secret!r}\n", encoding="utf-8")
    subprocess.run(["git", "add", "bad.py"], cwd=tmp_path, check=True)

    findings = _module().scan(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [("bad.py", 1, "openai_api_key")]
    assert secret not in repr(findings)


def test_scanner_rejects_tracked_secret_artifacts(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".env").write_text("DISCORD_TOKEN=value\n", encoding="utf-8")
    (tmp_path / "backup.sqlite3").write_bytes(b"not-a-real-database")
    subprocess.run(["git", "add", "-f", ".env", "backup.sqlite3"], cwd=tmp_path, check=True)

    findings = _module().scan(tmp_path)

    assert {(item.path, item.kind) for item in findings} == {
        (".env", "forbidden_tracked_artifact"),
        ("backup.sqlite3", "forbidden_tracked_artifact"),
    }


def test_scanner_also_checks_untracked_nonignored_source(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    secret = "sk-proj-" + "B" * 28
    (tmp_path / "new_module.py").write_text(f"token = {secret!r}\n", encoding="utf-8")

    findings = _module().scan(tmp_path)

    assert [(item.path, item.line, item.kind) for item in findings] == [("new_module.py", 1, "openai_api_key")]


def test_filesystem_tree_mode_scans_gitignored_files_without_running_git(tmp_path: Path) -> None:
    secret = "sk-proj-" + "C" * 28
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text(f"value = {secret!r}\n", encoding="utf-8")

    findings = _module().scan(tmp_path, filesystem_tree=True)

    assert [(item.path, item.line, item.kind) for item in findings] == [("ignored.py", 1, "openai_api_key")]
    script = Path(__file__).parents[1] / "scripts" / "security_preflight.py"
    completed = subprocess.run(
        [sys.executable, "-I", str(script), "--filesystem-tree", "--workspace", str(tmp_path)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 1
    assert secret not in completed.stdout
    assert "ignored.py:1: openai_api_key" in completed.stdout


def test_filesystem_tree_mode_rejects_symlink_or_reparse(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "safe.txt").write_text("safe\n", encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        junction = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip(f"directory symlink/junction unavailable: {exc}; rc={junction.returncode}")

    findings = _module().scan(tmp_path, filesystem_tree=True)

    assert ("linked", 0, "non_regular_or_reparse") in {(item.path, item.line, item.kind) for item in findings}


def test_filesystem_tree_mode_skips_excluded_entry_before_boundary_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    module = _module()

    class FakeEntry:
        def __init__(self, path: Path, *, symlink: bool = False, inspection_forbidden: bool = False) -> None:
            self.name = path.name
            self.path = str(path)
            self._symlink = symlink
            self._inspection_forbidden = inspection_forbidden

        def stat(self, *, follow_symlinks: bool = True):  # noqa: ANN201
            assert not self._inspection_forbidden, f"excluded entry was inspected: {self.path}"
            assert follow_symlinks is False
            return nested.stat()

        def is_symlink(self) -> bool:
            assert not self._inspection_forbidden, f"excluded entry was inspected: {self.path}"
            return self._symlink

    root_entry = FakeEntry(nested)
    excluded_entry = FakeEntry(nested / ".venv", inspection_forbidden=True)
    foreign_reparse = FakeEntry(nested / ".venv-copy", symlink=True)

    def fake_scandir(path: Path):  # noqa: ANN202
        if Path(path) == root:
            return [root_entry]
        if Path(path) == nested:
            return [excluded_entry, foreign_reparse]
        raise AssertionError(f"unexpected recursion: {path}")

    monkeypatch.setattr(module.os, "scandir", fake_scandir)

    files, findings = module._filesystem_tree_files(root)

    assert files == ()
    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("nested/.venv-copy", 0, "non_regular_or_reparse")
    ]


def test_filesystem_tree_exclusion_keeps_root_and_exact_component_controls(tmp_path: Path) -> None:
    root = tmp_path / ".venv"
    root.mkdir()
    root_secret = "sk-proj-" + "D" * 28
    partial_secret = "sk-proj-" + "E" * 28
    excluded_secret = "sk-proj-" + "F" * 28
    (root / "ordinary.py").write_text(f"value = {root_secret!r}\n", encoding="utf-8")
    partial = root / "project.venv" / "nested.py"
    partial.parent.mkdir()
    partial.write_text(f"value = {partial_secret!r}\n", encoding="utf-8")
    excluded = root / "nested" / ".venv" / "ignored.py"
    excluded.parent.mkdir(parents=True)
    excluded.write_text(f"value = {excluded_secret!r}\n", encoding="utf-8")

    findings = _module().scan(root, filesystem_tree=True)

    assert [(item.path, item.line, item.kind) for item in findings] == [
        ("ordinary.py", 1, "openai_api_key"),
        ("project.venv/nested.py", 1, "openai_api_key"),
    ]


@pytest.mark.parametrize("relative", [".env/settings.txt", "config.env.production/value.txt"])
def test_filesystem_tree_mode_rejects_nested_env_components(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("safe-looking value\n", encoding="utf-8")

    findings = _module().scan(tmp_path, filesystem_tree=True)

    assert (relative, 0, "forbidden_tracked_artifact") in {(item.path, item.line, item.kind) for item in findings}
