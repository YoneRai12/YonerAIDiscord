"""Untrusted AI成果物をローカルへ安全に保存するための小さな境界層。"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024
_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\\\|?*\x00-\x1f]')


class ArtifactStoreError(RuntimeError):
    """成果物を安全に保存できなかった。"""


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    artifact_id: str
    slug: str
    filename: str
    path: Path
    content_type: str
    byte_size: int
    sha256: str


class ArtifactStore:
    """公開せず、data配下にHTML等の生成物を原子的に保存する。

    ``scope`` は呼び出し側が guild/channel/user のような識別子を組み立てる。
    ここでは単一の安全な path segment 以外を受け付けないため、AI出力から
    パスを作っても親ディレクトリへ抜けない。
    """

    def __init__(self, root: Path | str, *, max_bytes: int = DEFAULT_ARTIFACT_MAX_BYTES) -> None:
        self.root = Path(root)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.max_bytes = max_bytes

    def save_html(self, prompt: str, html: str, *, scope: str) -> StoredArtifact:
        if not isinstance(html, str) or not html.strip():
            raise ValueError("html must be a non-empty string")
        data = html.encode("utf-8")
        if len(data) > self.max_bytes:
            raise ArtifactStoreError("artifact exceeds configured size limit")

        safe_scope = _safe_scope(scope)
        self._ensure_root()
        scope_dir = self.root / safe_scope
        _mkdir_not_symlink(scope_dir)

        artifact_id = f"art-{secrets.token_hex(10)}"
        artifact_dir = scope_dir / artifact_id
        # ``token_hex`` collision is extraordinarily unlikely, but O_EXCL makes it
        # a correctness property instead of an assumption.
        try:
            artifact_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ArtifactStoreError("artifact identifier collision") from exc
        if artifact_dir.is_symlink():
            raise ArtifactStoreError("artifact directory must not be a symlink")

        slug = safe_display_slug(prompt)
        filename = f"{slug}-{artifact_id[-6:]}.html"
        target = artifact_dir / filename
        temporary = artifact_dir / f".{filename}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            # The directory only contains a partial artifact that no caller can
            # discover by id yet; remove it best-effort to avoid junk accumulation.
            try:
                artifact_dir.rmdir()
            except OSError:
                pass
            raise

        return StoredArtifact(
            artifact_id=artifact_id,
            slug=slug,
            filename=filename,
            path=target,
            content_type="text/html; charset=utf-8",
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def _ensure_root(self) -> None:
        _assert_no_symlink_ancestor(self.root)
        _mkdir_not_symlink(self.root)


def safe_display_slug(prompt: str, *, fallback: str = "yonerai-web") -> str:
    """Discord添付表示用の、日本語を保った安全な短いslugを作る。"""

    if not isinstance(prompt, str):
        prompt = ""
    normalized = unicodedata.normalize("NFKC", prompt).strip()
    normalized = _INVALID_FILENAME_CHARS.sub("-", normalized)
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip(" .-")
    # Windows予約名と隠しファイルを避ける。Unicodeはそのまま許可する。
    if not normalized or normalized.casefold() in {
        "con",
        "prn",
        "aux",
        "nul",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
    }:
        normalized = fallback
    return normalized[:72].rstrip(" .-") or fallback


def _safe_scope(value: str) -> str:
    if not isinstance(value, str) or not _SCOPE_PATTERN.fullmatch(value):
        raise ValueError("scope must be one safe path segment")
    return value


def _mkdir_not_symlink(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ArtifactStoreError("artifact path must not be a symlink")


def _assert_no_symlink_ancestor(path: Path) -> None:
    """設定されたrootへ至る既存ディレクトリを、symlink経由にしない。"""

    absolute = path.absolute()
    chain = (absolute, *absolute.parents)
    for candidate in chain:
        if candidate.exists() and candidate.is_symlink():
            raise ArtifactStoreError("artifact path must not traverse a symlink")
