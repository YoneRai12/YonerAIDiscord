from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "ai_context_secret_scan.py"
    spec = importlib.util.spec_from_file_location("ai_context_secret_scan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scan_reports_secret_kinds_without_echoing_values(tmp_path: Path) -> None:
    github_token = "github_pat_" + "Ab9_" * 8
    assigned_secret = "s3cr3t-" + "Qp7Zx2" * 6
    (tmp_path / "settings.json").write_text(
        f'{{"token": "{assigned_secret}", "credential": "{github_token}"}}\n',
        encoding="utf-8",
    )

    findings = _module().scan(tmp_path)

    assert {item.kind for item in findings} >= {"github_token", "sensitive_assignment"}
    assert github_token not in repr(findings)
    assert assigned_secret not in repr(findings)


def test_scan_allows_placeholders_env_refs_and_hashes(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(
        "TOKEN=\nHMAC_SECRET=REPLACE_WITH_SECRET\nAPI_KEY=${API_KEY}\n",
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        '{"secret_ref":"env:PUBLISH_HMAC_SECRET","sha256":"' + "a1" * 32 + '"}\n',
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text(
        '{"integrity":"sha512-Ab9_Ab9_Ab9_Ab9_Ab9_Ab9_Ab9_Ab9_Ab9_"}\n',
        encoding="utf-8",
    )

    assert _module().scan(tmp_path) == ()


def test_scan_rejects_non_text_and_unreadable_entries(tmp_path: Path) -> None:
    (tmp_path / "archive.zip").write_bytes(b"PK\x03\x04")
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe\x00")

    findings = _module().scan(tmp_path)

    assert {(item.path, item.kind) for item in findings} == {
        ("archive.zip", "non_text_entry"),
        ("bad.md", "unreadable_utf8_text"),
    }
