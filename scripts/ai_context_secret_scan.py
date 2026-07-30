from __future__ import annotations

import argparse
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


_TEXT_SUFFIXES = {
    ".cfg",
    ".csv",
    ".example",
    ".ini",
    ".json",
    ".jsonc",
    ".lock",
    ".md",
    ".patch",
    ".ps1",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
_KNOWN_PATTERNS = {
    "openai_api_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "discord_bot_token": re.compile(r"\b[MNO][A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<key>(?:api[_-]?key|access[_-]?token|auth[_-]?token|bot[_-]?token|client[_-]?secret|hmac[_-]?secret|password|passwd|private[_-]?key|secret|token))\b"
    r"[\"']?\s*[=:]\s*[\"'](?P<value>[^\s\"',#}\]]{8,})[\"']"
)
_HIGH_ENTROPY_TOKEN = re.compile(r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/-]{36,}={0,2}(?![A-Za-z0-9_+/=-])")
_SENSITIVE_CONTEXT = re.compile(
    r"(?i)(?:api[_-]?key|authorization|cookie|credential|password|private[_-]?key|session|secret|token)"
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    kind: str


def _entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    markers = (
        "example",
        "placeholder",
        "replace_with",
        "changeme",
        "dummy",
        "fake",
        "test_",
        "sample",
        "<redacted",
        "<secret",
        "${",
        "{{",
        "env:",
    )
    if any(marker in lowered for marker in markers):
        return True
    if value in {"None", "null", "true", "false"}:
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9_]{7,}", value):
        return True
    return len(set(value)) <= 3


def _generic_entropy_candidate(value: str) -> bool:
    if _looks_like_placeholder(value) or len(value) > 256:
        return False
    if re.fullmatch(r"[0-9a-fA-F]+", value):
        return False
    if not re.search(r"[A-Za-z]", value) or not re.search(r"[0-9]", value):
        return False
    return _entropy(value.rstrip("=")) >= 4.6


def scan(root: Path) -> tuple[Finding, ...]:
    base = root.resolve()
    findings: list[Finding] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(base).parts:
            continue
        relative = path.relative_to(base).as_posix()
        if path.name not in {".env.example", ".gitignore"} and path.suffix.lower() not in _TEXT_SUFFIXES:
            findings.append(Finding(relative, 0, "non_text_entry"))
            continue
        if path.stat().st_size > 2_000_000:
            findings.append(Finding(relative, 0, "oversized_text_entry"))
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
        except (OSError, UnicodeError):
            findings.append(Finding(relative, 0, "unreadable_utf8_text"))
            continue

        for line_number, line in enumerate(lines, 1):
            line_kinds: set[str] = set()
            for kind, pattern in _KNOWN_PATTERNS.items():
                if pattern.search(line):
                    line_kinds.add(kind)
            for match in _SENSITIVE_ASSIGNMENT.finditer(line):
                value = match.group("value")
                if not _looks_like_placeholder(value) and (_entropy(value) >= 3.5 or len(value) >= 20):
                    line_kinds.add("sensitive_assignment")
            if _SENSITIVE_CONTEXT.search(line):
                for match in _HIGH_ENTROPY_TOKEN.finditer(line):
                    if _generic_entropy_candidate(match.group(0)):
                        line_kinds.add("high_entropy_token")
            findings.extend(Finding(relative, line_number, kind) for kind in sorted(line_kinds))
    return tuple(findings)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI context snapshot secret/high-entropy scan")
    parser.add_argument("--workspace", type=Path, required=True)
    root = parser.parse_args().workspace.resolve()
    findings = scan(root)
    if findings:
        print("AI context secret scan: FAILED")
        for finding in findings:
            location = finding.path if finding.line == 0 else f"{finding.path}:{finding.line}"
            print(f"- {location}: {finding.kind}")
        return 1
    print("AI context secret scan: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
