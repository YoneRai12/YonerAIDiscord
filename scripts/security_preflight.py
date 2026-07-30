from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


_TEXT_SUFFIXES = {
    ".cfg",
    ".ini",
    ".json",
    ".jsonc",
    ".md",
    ".patch",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_EXCLUDED_PARTS = frozenset({".git", ".venv", ".pytest-tmp", ".ruff_cache", "__pycache__"})
_FORBIDDEN_TRACKED_NAMES = frozenset({".env", "id_rsa", "id_ed25519"})
_FORBIDDEN_TRACKED_SUFFIXES = frozenset({".db", ".pem", ".pfx", ".sqlite", ".sqlite3", ".zip"})
_PATTERNS = {
    "openai_api_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "discord_bot_token": re.compile(r"\b[MNO][A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    kind: str


def _repository_files(root: Path) -> tuple[Path, ...]:
    """追跡済みと未追跡・非ignoreの両方を検査対象にする。"""

    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(root / item.decode("utf-8") for item in completed.stdout.split(b"\0") if item)


def scan(root: Path) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for path in _repository_files(root):
        relative = path.relative_to(root)
        lowered_parts = {part.lower() for part in relative.parts}
        if lowered_parts & _EXCLUDED_PARTS:
            continue
        if path.name.lower() in _FORBIDDEN_TRACKED_NAMES or path.suffix.lower() in _FORBIDDEN_TRACKED_SUFFIXES:
            findings.append(Finding(relative.as_posix(), 0, "forbidden_tracked_artifact"))
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES or not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
        except (OSError, UnicodeError):
            findings.append(Finding(relative.as_posix(), 0, "unreadable_utf8_text"))
            continue
        for line_number, line in enumerate(lines, 1):
            for kind, pattern in _PATTERNS.items():
                if pattern.search(line):
                    findings.append(Finding(relative.as_posix(), line_number, kind))
    return tuple(findings)


def main() -> int:
    parser = argparse.ArgumentParser(description="tracked secret/artifact preflight")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    root = parser.parse_args().workspace.resolve()
    findings = scan(root)
    if findings:
        print("security preflight: FAILED")
        for finding in findings:
            location = finding.path if finding.line == 0 else f"{finding.path}:{finding.line}"
            print(f"- {location}: {finding.kind}")
        return 1
    print("security preflight: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
