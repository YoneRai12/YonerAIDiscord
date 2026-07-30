from __future__ import annotations

import asyncio
import io
import re
import stat
import unicodedata
import zipfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import discord

from .models import Attachment, AttachmentKind, ImageDetail


_IMAGE_MAGIC: tuple[tuple[str, bytes], ...] = (
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/gif", b"GIF87a"),
    ("image/gif", b"GIF89a"),
)
_IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_OFFICE_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".jsonl",
        ".csv",
        ".tsv",
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".java",
        ".kt",
        ".kts",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".sh",
        ".ps1",
        ".sql",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".xml",
        ".html",
        ".htm",
        ".css",
    }
)
_TEXT_MIME = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/sql",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/javascript",
    }
)
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OFFICE_REQUIRED_ENTRY = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml",
}
_OFFICE_CONTENT_TYPE = {
    ".docx": b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    ".xlsx": b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    ".pptx": b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
}
_MAX_OFFICE_ENTRIES = 2_048
_MAX_OFFICE_ENTRY_BYTES = 32 * 1024 * 1024
_MAX_OFFICE_EXPANDED_BYTES = 64 * 1024 * 1024
_MAX_OFFICE_COMPRESSION_RATIO = 100
_MAX_CONTENT_TYPES_BYTES = 256 * 1024
_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
        r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bmfa\.[A-Za-z0-9_-]{20,}\b",
        r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}\b",
        r"\b(?:Bearer|Bot)\s+[A-Za-z0-9._~+/=-]{16,}\b",
        r"(?:api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|"
        r"password|passwd|private[_ -]?key|discord[_ -]?token)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"']{8,}",
    )
)


class DiscordInputError(ValueError):
    """外部送信前に安全に利用者へ返せる入力拒否。本文やファイル名は保持しない。"""

    def __init__(self, reason: str, user_message: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_message = user_message


@dataclass(frozen=True, slots=True)
class AttachmentLimits:
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    read_timeout_seconds: float

    def __post_init__(self) -> None:
        if self.max_files <= 0 or self.max_file_bytes <= 0 or self.max_total_bytes <= 0:
            raise ValueError("attachment limits must be positive")
        if self.max_total_bytes < self.max_file_bytes:
            raise ValueError("max_total_bytes must be at least max_file_bytes")
        if self.read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class DiscordInputBundle:
    attachments: tuple[Attachment, ...]
    attachment_count: int
    total_bytes: int


async def collect_discord_attachments(
    messages: Iterable[Any],
    *,
    enabled: bool,
    limits: AttachmentLimits,
    read_allowed: Callable[[], bool | Awaitable[bool]] | None = None,
) -> DiscordInputBundle:
    """Discord Attachmentだけを読み込み、型と上限を検証してAI入力へ変換する。"""

    candidates = _unique_attachments(messages)
    if not candidates:
        return DiscordInputBundle((), 0, 0)
    if not enabled:
        raise DiscordInputError(
            "attachments_disabled",
            "このBOTではAI添付解析が無効です。管理者に AI_ATTACHMENTS_ENABLED の確認を依頼してください。",
        )
    if len(candidates) > limits.max_files:
        raise DiscordInputError(
            "attachment_count_exceeded",
            f"添付は一度に{limits.max_files}件までです。件数を減らしてもう一度送ってください。",
        )

    declared_total = 0
    for candidate in candidates:
        declared_size = getattr(candidate, "size", None)
        if isinstance(declared_size, bool) or not isinstance(declared_size, int) or declared_size < 0:
            raise DiscordInputError("attachment_size_unknown", "添付サイズを確認できないため解析できません。")
        if declared_size > limits.max_file_bytes:
            raise DiscordInputError("attachment_too_large", "添付ファイルが1件あたりの上限を超えています。")
        declared_total += declared_size
        if declared_total > limits.max_total_bytes:
            raise DiscordInputError("attachment_total_too_large", "添付ファイルの合計サイズが上限を超えています。")

    converted: list[Attachment] = []
    actual_total = 0
    for candidate in candidates:
        if read_allowed is not None and not await _authorization_current(read_allowed):
            raise DiscordInputError(
                "attachment_authorization_changed",
                "待機中に権限または機能設定が変更されたため、添付の読み取りを中止しました。",
            )
        reader = getattr(candidate, "read", None)
        if not callable(reader):
            raise DiscordInputError("attachment_unreadable", "添付ファイルを読み取れませんでした。")
        try:
            payload = await asyncio.wait_for(reader(use_cached=True), timeout=limits.read_timeout_seconds)
        except TimeoutError as exc:
            raise DiscordInputError(
                "attachment_read_timeout", "添付ファイルの読み取りがタイムアウトしました。"
            ) from exc
        except discord.HTTPException as exc:
            raise DiscordInputError("attachment_read_failed", "添付ファイルを読み取れませんでした。") from exc
        except Exception as exc:
            raise DiscordInputError("attachment_read_failed", "添付ファイルを読み取れませんでした。") from exc
        if read_allowed is not None and not await _authorization_current(read_allowed):
            raise DiscordInputError(
                "attachment_authorization_changed",
                "待機中に権限または機能設定が変更されたため、添付の読み取りを中止しました。",
            )
        if not isinstance(payload, bytes):
            raise DiscordInputError("attachment_invalid_payload", "添付ファイルの形式を確認できませんでした。")
        if len(payload) > limits.max_file_bytes:
            raise DiscordInputError("attachment_too_large_after_read", "添付ファイルが1件あたりの上限を超えています。")
        actual_total += len(payload)
        if actual_total > limits.max_total_bytes:
            raise DiscordInputError(
                "attachment_total_too_large_after_read",
                "添付ファイルの合計サイズが上限を超えています。",
            )
        converted.append(_convert_attachment(candidate, payload))

    return DiscordInputBundle(tuple(converted), len(converted), actual_total)


async def _authorization_current(check: Callable[[], bool | Awaitable[bool]]) -> bool:
    try:
        current = check()
        if hasattr(current, "__await__"):
            current = await current
        return current is True
    except Exception:
        return False


def sanitize_filename(value: object) -> str:
    raw = value if isinstance(value, str) else ""
    basename = PurePosixPath(raw.replace("\\", "/")).name
    cleaned = "".join("_" if unicodedata.category(char).startswith("C") else char for char in basename)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return "attachment.bin"
    if len(cleaned.encode("utf-8")) <= 240:
        return cleaned
    suffix = PurePosixPath(cleaned).suffix[:16]
    suffix_bytes = len(suffix.encode("utf-8"))
    byte_budget = max(1, 240 - suffix_bytes)
    stem = cleaned[: -len(suffix)] if suffix else cleaned
    shortened = _truncate_utf8(stem, byte_budget).rstrip(" .")
    return (shortened or "attachment") + suffix


def contains_secret_like_text(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in _SECRET_PATTERNS)


def _unique_attachments(messages: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[tuple[str, int]] = set()
    for message in messages:
        for candidate in getattr(message, "attachments", ()) or ():
            attachment_id = getattr(candidate, "id", None)
            key = ("id", attachment_id) if isinstance(attachment_id, int) else ("object", id(candidate))
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
    return result


def _convert_attachment(candidate: Any, payload: bytes) -> Attachment:
    filename = sanitize_filename(getattr(candidate, "filename", ""))
    if contains_secret_like_text(filename):
        raise DiscordInputError(
            "attachment_filename_secret_detected",
            "秘密情報らしい文字列を含むファイル名はAIへ送信できません。名前を変更してから再送してください。",
        )
    extension = PurePosixPath(filename).suffix.casefold()
    declared_mime = _normalized_mime(getattr(candidate, "content_type", None))

    image_mime = _image_mime(payload)
    if image_mime is not None:
        if declared_mime and declared_mime != image_mime:
            raise DiscordInputError("attachment_mime_mismatch", "添付の拡張子またはMIMEと実データが一致しません。")
        expected_image_mime = _IMAGE_EXTENSIONS.get(extension)
        if expected_image_mime is not None and expected_image_mime != image_mime:
            raise DiscordInputError("attachment_extension_mismatch", "添付の拡張子またはMIMEと実データが一致しません。")
        if image_mime == "image/gif" and not _is_single_frame_gif(payload):
            raise DiscordInputError(
                "attachment_gif_animated_or_invalid",
                "GIFは破損していない静止画（1フレーム）だけ解析できます。",
            )
        return Attachment(
            kind=AttachmentKind.IMAGE,
            data=payload,
            mime_type=image_mime,
            filename=filename,
            detail=ImageDetail.HIGH,
        )

    if payload.startswith(b"%PDF-"):
        if declared_mime and declared_mime != "application/pdf":
            raise DiscordInputError("attachment_mime_mismatch", "添付の拡張子またはMIMEと実データが一致しません。")
        if extension and extension != ".pdf":
            raise DiscordInputError("attachment_extension_mismatch", "添付の拡張子またはMIMEと実データが一致しません。")
        return Attachment(
            kind=AttachmentKind.FILE,
            data=payload,
            mime_type="application/pdf",
            filename=filename,
        )

    if payload.startswith(_ZIP_MAGIC):
        office_mime = _OFFICE_MIME.get(extension)
        if office_mime is None:
            raise DiscordInputError("attachment_zip_not_allowed", "ZIPはDOCX・XLSX・PPTX形式だけ解析できます。")
        if declared_mime and declared_mime not in {office_mime, "application/zip", "application/octet-stream"}:
            raise DiscordInputError("attachment_mime_mismatch", "添付の拡張子またはMIMEと実データが一致しません。")
        if not _is_valid_office_package(payload, extension):
            raise DiscordInputError(
                "attachment_office_invalid",
                "Office添付の構造、展開サイズ、または圧縮率を安全に確認できませんでした。",
            )
        return Attachment(
            kind=AttachmentKind.FILE,
            data=payload,
            mime_type=office_mime,
            filename=filename,
        )

    if _claims_non_text_type(declared_mime, extension):
        raise DiscordInputError("attachment_magic_mismatch", "添付の拡張子またはMIMEと実データが一致しません。")
    if not _is_allowed_text_type(declared_mime, extension):
        raise DiscordInputError("attachment_type_not_allowed", "この添付形式はAI解析の許可対象ではありません。")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DiscordInputError("attachment_text_not_utf8", "テキスト添付はUTF-8で保存してください。") from exc
    if not _looks_like_text(text):
        raise DiscordInputError("attachment_text_invalid", "添付を安全なテキストとして確認できませんでした。")
    if contains_secret_like_text(text):
        raise DiscordInputError(
            "attachment_secret_detected",
            "秘密情報らしい文字列を含む添付はAIへ送信できません。内容を除去してから再送してください。",
        )
    mime_type = _canonical_text_mime(declared_mime, extension)
    return Attachment(
        kind=AttachmentKind.FILE,
        data=payload,
        mime_type=mime_type or "text/plain",
        filename=filename,
    )


def _image_mime(payload: bytes) -> str | None:
    for mime_type, magic in _IMAGE_MAGIC:
        if payload.startswith(magic):
            return mime_type
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_single_frame_gif(payload: bytes) -> bool:
    """GIF blockを展開せず走査し、完全な静止画1frameだけを許可する。"""

    if len(payload) < 14 or payload[:6] not in {b"GIF87a", b"GIF89a"}:
        return False
    position = 13
    packed = payload[10]
    if packed & 0x80:
        position += 3 * (2 ** ((packed & 0x07) + 1))
    frames = 0
    while position < len(payload):
        marker = payload[position]
        position += 1
        if marker == 0x3B:
            return frames == 1 and position == len(payload)
        if marker == 0x2C:
            frames += 1
            if frames > 1 or position + 9 > len(payload):
                return False
            local_packed = payload[position + 8]
            position += 9
            if local_packed & 0x80:
                position += 3 * (2 ** ((local_packed & 0x07) + 1))
            if position >= len(payload):
                return False
            position += 1  # LZW minimum code size
            position = _skip_gif_sub_blocks(payload, position)
        elif marker == 0x21:
            if position >= len(payload):
                return False
            position += 1  # extension label
            position = _skip_gif_sub_blocks(payload, position)
        else:
            return False
        if position < 0:
            return False
    return False


def _skip_gif_sub_blocks(payload: bytes, position: int) -> int:
    while position < len(payload):
        size = payload[position]
        position += 1
        if size == 0:
            return position
        position += size
        if position > len(payload):
            return -1
    return -1


def _is_valid_office_package(payload: bytes, extension: str) -> bool:
    """OOXMLのcentral directoryだけをbounded検査し、展開やpath書込みはしない。"""

    required = _OFFICE_REQUIRED_ENTRY.get(extension)
    expected_content_type = _OFFICE_CONTENT_TYPE.get(extension)
    if required is None or expected_content_type is None:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(payload), mode="r") as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= _MAX_OFFICE_ENTRIES:
                return False
            names: set[str] = set()
            expanded_total = 0
            for entry in entries:
                name = entry.filename.replace("\\", "/")
                parts = name.split("/")
                if (
                    not name
                    or name.startswith("/")
                    or "\\" in entry.filename
                    or any(part in {"", ".", ".."} for part in parts if not (part == "" and name.endswith("/")))
                    or name in names
                    or entry.flag_bits & 0x1
                    or stat.S_ISLNK((entry.external_attr >> 16) & 0xFFFF)
                    or entry.file_size < 0
                    or entry.file_size > _MAX_OFFICE_ENTRY_BYTES
                    or entry.compress_size < 0
                ):
                    return False
                names.add(name)
                expanded_total += entry.file_size
                if expanded_total > _MAX_OFFICE_EXPANDED_BYTES:
                    return False
                if entry.file_size and (
                    entry.compress_size == 0 or entry.file_size > entry.compress_size * _MAX_OFFICE_COMPRESSION_RATIO
                ):
                    return False
            required_names = {"[Content_Types].xml", "_rels/.rels", required}
            if not required_names.issubset(names):
                return False
            content_types_info = archive.getinfo("[Content_Types].xml")
            if content_types_info.file_size > _MAX_CONTENT_TYPES_BYTES:
                return False
            content_types = archive.read(content_types_info)
            return b"<Types" in content_types and expected_content_type in content_types
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False


def _normalized_mime(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.partition(";")[0].strip().casefold()


def _is_allowed_text_type(mime_type: str, extension: str) -> bool:
    return mime_type.startswith("text/") or mime_type in _TEXT_MIME or extension in _TEXT_EXTENSIONS


def _claims_non_text_type(mime_type: str, extension: str) -> bool:
    return (
        mime_type.startswith("image/")
        or mime_type == "application/pdf"
        or extension in _IMAGE_EXTENSIONS
        or extension == ".pdf"
        or extension in _OFFICE_MIME
    )


def _looks_like_text(value: str) -> bool:
    if "\x00" in value:
        return False
    forbidden_controls = sum(
        1 for char in value if unicodedata.category(char) == "Cc" and char not in {"\t", "\n", "\r", "\f"}
    )
    return forbidden_controls == 0


def _canonical_text_mime(declared_mime: str, extension: str) -> str:
    if declared_mime in {"application/json", "application/xml", "text/csv", "text/markdown", "text/plain", "text/xml"}:
        return declared_mime
    if extension in {".json", ".jsonl"}:
        return "application/json"
    if extension == ".csv":
        return "text/csv"
    if extension in {".md", ".markdown"}:
        return "text/markdown"
    if extension == ".xml":
        return "application/xml"
    return "text/plain"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


__all__ = [
    "AttachmentLimits",
    "DiscordInputBundle",
    "DiscordInputError",
    "collect_discord_attachments",
    "contains_secret_like_text",
    "sanitize_filename",
]
