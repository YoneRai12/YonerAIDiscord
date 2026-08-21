from __future__ import annotations

import argparse
import os
import re
import stat
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
_ENV_COMPONENT = re.compile(r"(?:^|\.)env(?:\.|$)", re.IGNORECASE)
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


def _filesystem_tree_files(root: Path) -> tuple[tuple[Path, ...], tuple[Finding, ...]]:
    files: list[Path] = []
    findings: list[Finding] = []
    pending = [root]
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            relative = current.relative_to(root).as_posix() if current != root else "."
            findings.append(Finding(relative, 0, "unreadable_filesystem_entry"))
            continue
        for entry in entries:
            if entry.name.lower() in _EXCLUDED_PARTS:
                continue
            path = Path(entry.path)
            relative = path.relative_to(root)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                findings.append(Finding(relative.as_posix(), 0, "unreadable_filesystem_entry"))
                continue
            attributes = getattr(metadata, "st_file_attributes", 0)
            if entry.is_symlink() or attributes & reparse_flag:
                findings.append(Finding(relative.as_posix(), 0, "non_regular_or_reparse"))
            elif stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                files.append(path)
            else:
                findings.append(Finding(relative.as_posix(), 0, "non_regular_or_reparse"))
    files.sort(key=lambda path: path.relative_to(root).as_posix())
    findings.sort(key=lambda finding: (finding.path, finding.line, finding.kind))
    return tuple(files), tuple(findings)


def scan(root: Path, *, filesystem_tree: bool = False) -> tuple[Finding, ...]:
    if filesystem_tree:
        files, boundary_findings = _filesystem_tree_files(root)
        findings = list(boundary_findings)
    else:
        files = _repository_files(root)
        findings = []
    for path in files:
        relative = path.relative_to(root)
        lowered_parts = {part.lower() for part in relative.parts}
        if lowered_parts & _EXCLUDED_PARTS:
            continue
        forbidden_env_component = any(
            _ENV_COMPONENT.search(part) and part.lower() != ".env.example" for part in relative.parts
        )
        if (
            forbidden_env_component
            or path.name.lower() in _FORBIDDEN_TRACKED_NAMES
            or path.suffix.lower() in _FORBIDDEN_TRACKED_SUFFIXES
        ):
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
    parser.add_argument("--filesystem-tree", action="store_true")
    arguments = parser.parse_args()
    root = arguments.workspace.resolve()
    findings = scan(root, filesystem_tree=arguments.filesystem_tree)
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
