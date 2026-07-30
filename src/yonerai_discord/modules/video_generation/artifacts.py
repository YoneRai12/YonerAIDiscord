"""動画生成で受け取る MP4 を private artifact として短期間だけ保持する境界。"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yonerai_discord.provider_registry import ArtifactKind, ArtifactRef


MP4_MEDIA_TYPE = "video/mp4"
MAX_MP4_BYTES = 8 * 1024 * 1024
MAX_MP4_BOXES = 512
MAX_MP4_NESTING_DEPTH = 16
DEFAULT_MAX_ARTIFACTS = 64
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024

_ARTIFACT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}\Z")
_ARTIFACT_FILE = re.compile(r"(?P<artifact_id>[a-z0-9][a-z0-9_-]{0,95})\.mp4\Z")
_TEMP_FILE = re.compile(r"\.video-[a-f0-9]{16}\.tmp\Z")
_COMPATIBLE_BRANDS = {b"isom", b"iso2", b"mp41", b"mp42", b"avc1"}
_CONTAINER_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"mvex", b"moof", b"traf", b"mfra"}
_TOP_LEVEL_BOXES = {b"ftyp", b"free", b"moov", b"mdat"}
_REJECTED_OPAQUE_BOXES = {
    b"meta",
    b"ilst",
    b"loci",
    b"\xa9xyz",
    b"\xa9too",
    b"\xa9nam",
    b"\xa9ART",
    b"\xa9alb",
    b"\xa9day",
    b"\xa9gen",
    b"\xa9wrt",
    b"\xa9com",
    b"cprt",
    b"desc",
    b"name",
    b"url ",
    b"urn ",
    b"skip",
    b"uuid",
    b"wide",
    b"Xtra",
}


class VideoArtifactError(RuntimeError):
    """動画 artifact の保存または読み出しに失敗した。"""


class VideoArtifactValidationError(VideoArtifactError):
    """MP4 または ArtifactRef が許容する最小契約を満たさない。"""


class VideoArtifactAuthorizationError(VideoArtifactError):
    """要求への束縛または読み出し許可が変化した。"""


@dataclass(frozen=True, slots=True)
class ValidatedMp4:
    data: bytes
    sha256: str


class VideoArtifactStore:
    """事前作成済み flat root に、検証済み MP4 だけを保存する。"""

    def __init__(
        self,
        root: Path | str,
        *,
        max_artifacts: int = DEFAULT_MAX_ARTIFACTS,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        if isinstance(max_artifacts, bool) or not isinstance(max_artifacts, int) or not 1 <= max_artifacts <= 4_096:
            raise ValueError("max_artifacts is outside the allowed range")
        if (
            isinstance(max_total_bytes, bool)
            or not isinstance(max_total_bytes, int)
            or not MAX_MP4_BYTES <= max_total_bytes <= 8 * 1024 * 1024 * 1024
        ):
            raise ValueError("max_total_bytes is outside the allowed range")
        self.root = Path(root)
        self._ensure_root()
        # Stage 1 の脅威モデルは process 内の request binding と disk 上の既知外残骸を
        # fail-closed にする。root へ書ける同一 OS account の別 process との競合は範囲外。
        self.max_artifacts = max_artifacts
        self.max_total_bytes = max_total_bytes
        self._request_bindings: dict[str, tuple[bytes, int]] = {}
        self._total_bytes = 0
        self._lock = threading.RLock()
        self._cleanup_orphans()

    def put_mp4(self, data: bytes, *, request_binding: str, artifact_id: str | None = None) -> ArtifactRef:
        validated = validate_mp4(data)
        binding_digest = _request_binding_digest(request_binding)
        safe_id = _safe_artifact_id(artifact_id or f"vid-{secrets.token_hex(16)}")
        with self._lock:
            if safe_id in self._request_bindings:
                raise VideoArtifactError("artifact identifier collision")
            self._ensure_root()
            self._evict_for(len(validated.data))
            target = self.root / f"{safe_id}.mp4"
            temporary = self.root / f".video-{secrets.token_hex(8)}.tmp"
            try:
                reservation = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(reservation)
            except FileExistsError as exc:
                raise VideoArtifactError("artifact identifier collision") from exc
            try:
                descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(validated.data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                os.chmod(target, 0o600)
            except Exception:
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                raise
            self._request_bindings[safe_id] = (binding_digest, len(validated.data))
            self._total_bytes += len(validated.data)
        return ArtifactRef(safe_id, ArtifactKind.VIDEO, MP4_MEDIA_TYPE, len(validated.data), validated.sha256)

    def read_mp4(
        self, ref: ArtifactRef, *, request_binding: str, read_allowed: Callable[[], bool] | None = None
    ) -> bytes:
        safe_id = _validate_ref(ref)
        binding_digest = _request_binding_digest(request_binding)
        with self._lock:
            entry = self._request_bindings.get(safe_id)
            if entry is None or entry[0] != binding_digest:
                raise VideoArtifactAuthorizationError("video artifact request binding changed")
        self._ensure_root()
        target = self.root / f"{safe_id}.mp4"
        _assert_not_symlink(target)
        _require_allowed(read_allowed)
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        except (FileNotFoundError, OSError) as exc:
            raise VideoArtifactError("video artifact is unavailable") from exc
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise VideoArtifactError("video artifact must be a regular file")
            data = handle.read(MAX_MP4_BYTES + 1)
        _require_allowed(read_allowed)
        if len(data) > MAX_MP4_BYTES:
            raise VideoArtifactValidationError("stored MP4 exceeds the size limit")
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise VideoArtifactValidationError("stored MP4 integrity check failed")
        validate_mp4(data)
        return data

    def _cleanup_orphans(self) -> None:
        with self._lock:
            for candidate in self.root.iterdir():
                if candidate.is_dir() and not candidate.is_symlink():
                    continue
                if _ARTIFACT_FILE.fullmatch(candidate.name) or _TEMP_FILE.fullmatch(candidate.name):
                    candidate.unlink(missing_ok=True)

    def _evict_for(self, incoming_bytes: int) -> None:
        while self._request_bindings and (
            len(self._request_bindings) >= self.max_artifacts
            or self._total_bytes + incoming_bytes > self.max_total_bytes
        ):
            artifact_id = next(iter(self._request_bindings))
            _binding, size_bytes = self._request_bindings[artifact_id]
            (self.root / f"{artifact_id}.mp4").unlink()
            # unlink が成功して初めて state を更新する。
            self._request_bindings.pop(artifact_id)
            self._total_bytes -= size_bytes

    def _ensure_root(self) -> None:
        _assert_no_symlink_ancestor(self.root)
        if not self.root.is_dir() or self.root.is_symlink():
            raise VideoArtifactError("video artifact root is unavailable")


def validate_mp4(data: bytes) -> ValidatedMp4:
    """外部 codec を起動せず、bounded ISO-BMFF 構造だけを検査する。"""
    if not isinstance(data, bytes):
        raise TypeError("MP4 data must be bytes")
    if not data or len(data) > MAX_MP4_BYTES:
        raise VideoArtifactValidationError("MP4 exceeds the size limit")
    boxes: list[tuple[bytes, int, int, int]] = []
    _parse_boxes(data, 0, len(data), 0, None, boxes)
    top_level = tuple(box for box in boxes if box[3] == 0)
    if not top_level or top_level[0][0] != b"ftyp":
        raise VideoArtifactValidationError("MP4 must begin with ftyp")
    top_kinds = tuple(box[0] for box in top_level)
    if any(kind not in _TOP_LEVEL_BOXES for kind in top_kinds):
        raise VideoArtifactValidationError("MP4 contains an unsupported top-level box")
    if top_kinds.count(b"ftyp") != 1:
        raise VideoArtifactValidationError("MP4 must contain exactly one top-level ftyp box")
    if top_kinds.count(b"moov") != 1:
        raise VideoArtifactValidationError("MP4 must contain exactly one top-level moov box")
    if top_kinds.count(b"mdat") != 1:
        raise VideoArtifactValidationError("MP4 must contain exactly one top-level mdat box")
    if top_kinds.count(b"free") > 1:
        raise VideoArtifactValidationError("MP4 may contain at most one top-level free box")
    _validate_ftyp(data[top_level[0][1] : top_level[0][2]])
    mdat = next(box for box in top_level if box[0] == b"mdat")
    if mdat[2] <= mdat[1]:
        raise VideoArtifactValidationError("MP4 mdat payload must not be empty")
    return ValidatedMp4(data=data, sha256=hashlib.sha256(data).hexdigest())


def _parse_boxes(
    data: bytes,
    start: int,
    end: int,
    depth: int,
    parent_kind: bytes | None,
    boxes: list[tuple[bytes, int, int, int]],
) -> None:
    if depth > MAX_MP4_NESTING_DEPTH:
        raise VideoArtifactValidationError("MP4 nesting depth exceeds the limit")
    cursor = start
    while cursor < end:
        if len(boxes) >= MAX_MP4_BOXES:
            raise VideoArtifactValidationError("MP4 contains too many boxes")
        if end - cursor < 8:
            raise VideoArtifactValidationError("truncated MP4 box header")
        size32, kind = struct.unpack_from(">I4s", data, cursor)
        header = 8
        if size32 == 0:
            raise VideoArtifactValidationError("MP4 box size zero is not allowed")
        if size32 == 1:
            if end - cursor < 16:
                raise VideoArtifactValidationError("truncated extended MP4 box header")
            size = struct.unpack_from(">Q", data, cursor + 8)[0]
            header = 16
        else:
            size = size32
        if size < header or size > end - cursor:
            raise VideoArtifactValidationError("MP4 box size overflows its containing box")
        payload_start, box_end = cursor + header, cursor + size
        if kind in _REJECTED_OPAQUE_BOXES:
            raise VideoArtifactValidationError("MP4 contains a rejected opaque or metadata box")
        if kind == b"free":
            if depth != 0:
                raise VideoArtifactValidationError("MP4 free box is only allowed at top level")
            _validate_free_box(data[payload_start:box_end])
        if kind == b"udta":
            if depth != 1 or parent_kind != b"moov":
                raise VideoArtifactValidationError("MP4 udta metadata wrapper is not canonical")
            _validate_ffmpeg_bitexact_udta(data[payload_start:box_end])
        if depth > 0 and kind in _TOP_LEVEL_BOXES:
            raise VideoArtifactValidationError("MP4 contains a top-level-only box inside a container")
        boxes.append((kind, payload_start, box_end, depth))
        if kind == b"dref":
            _validate_dref(data[payload_start:box_end])
        elif kind in _CONTAINER_BOXES:
            _parse_boxes(data, payload_start, box_end, depth + 1, kind, boxes)
        cursor = box_end
    if cursor != end:
        raise VideoArtifactValidationError("MP4 contains trailing data")


def _validate_ftyp(payload: bytes) -> None:
    if len(payload) < 8 or (len(payload) - 8) % 4:
        raise VideoArtifactValidationError("invalid ftyp box")
    brands = {payload[:4], *(payload[offset : offset + 4] for offset in range(8, len(payload), 4))}
    if not brands & _COMPATIBLE_BRANDS:
        raise VideoArtifactValidationError("ftyp has no supported compatible brand")


def _validate_free_box(payload: bytes) -> None:
    """FFmpegが置くpayloadなしのfree boxだけを許す。"""

    if payload:
        raise VideoArtifactValidationError("MP4 free box payload must be empty")


def _validate_ffmpeg_bitexact_udta(payload: bytes) -> None:
    """bitexact FFmpegが残す空metadata wrapperのexact形だけを許す。"""

    if len(payload) < 8:
        raise VideoArtifactValidationError("MP4 udta metadata wrapper is not canonical")
    meta_size, meta_kind = struct.unpack_from(">I4s", payload)
    if meta_kind != b"meta" or meta_size != len(payload) or meta_size < 12:
        raise VideoArtifactValidationError("MP4 udta metadata wrapper is not canonical")
    meta_payload = payload[8:]
    if meta_payload[:4] != b"\0\0\0\0":
        raise VideoArtifactValidationError("MP4 meta FullBox is not canonical")
    children = meta_payload[4:]

    if len(children) < 8:
        raise VideoArtifactValidationError("MP4 meta hdlr is not canonical")
    hdlr_size, hdlr_kind = struct.unpack_from(">I4s", children)
    expected_hdlr_payload = b"\0\0\0\0" + b"\0\0\0\0" + b"mdir" + b"appl" + (b"\0" * 8) + b"\0"
    if (
        hdlr_kind != b"hdlr"
        or hdlr_size != 8 + len(expected_hdlr_payload)
        or children[8:hdlr_size] != expected_hdlr_payload
    ):
        raise VideoArtifactValidationError("MP4 meta hdlr is not canonical")

    ilst = children[hdlr_size:]
    if ilst != struct.pack(">I4s", 8, b"ilst"):
        raise VideoArtifactValidationError("MP4 meta ilst must be empty")


def _validate_dref(payload: bytes) -> None:
    """自己完結url data reference 1件だけを受理する。"""

    if len(payload) < 20 or payload[:4] != b"\0\0\0\0":
        raise VideoArtifactValidationError("invalid dref FullBox")
    if struct.unpack(">I", payload[4:8])[0] != 1:
        raise VideoArtifactValidationError("dref must contain exactly one entry")
    child_size, child_kind = struct.unpack(">I4s", payload[8:16])
    if child_size != 12 or child_kind != b"url " or len(payload) != 8 + child_size:
        raise VideoArtifactValidationError("dref must contain one bounded self-contained url entry")
    if payload[16:20] != b"\0\0\0\1":
        raise VideoArtifactValidationError("dref url must be self-contained")


def _safe_artifact_id(value: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT_ID.fullmatch(value):
        raise VideoArtifactValidationError("artifact_id must be one safe path segment")
    return value


def _request_binding_digest(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise VideoArtifactValidationError("request_binding must be a non-empty bounded string")
    return hashlib.sha256(b"yonerai.video-artifact.v1\0" + value.encode("utf-8")).digest()


def _validate_ref(ref: ArtifactRef) -> str:
    if not isinstance(ref, ArtifactRef):
        raise TypeError("ref must be an ArtifactRef")
    if (
        ref.kind is not ArtifactKind.VIDEO
        or ref.media_type != MP4_MEDIA_TYPE
        or ref.size_bytes is None
        or ref.sha256 is None
        or ref.size_bytes > MAX_MP4_BYTES
    ):
        raise VideoArtifactValidationError("ArtifactRef is not a complete MP4 video reference")
    return _safe_artifact_id(ref.artifact_id)


def _require_allowed(callback: Callable[[], bool] | None) -> None:
    if callback is None:
        return
    try:
        allowed = callback()
    except Exception as exc:
        raise VideoArtifactAuthorizationError("video artifact authorization changed") from exc
    if allowed is not True:
        raise VideoArtifactAuthorizationError("video artifact authorization changed")


def _assert_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise VideoArtifactError("video artifact path must not be a symlink")


def _assert_no_symlink_ancestor(path: Path) -> None:
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and candidate.is_symlink():
            raise VideoArtifactError("video artifact root must not traverse a symlink")


__all__ = [
    "DEFAULT_MAX_ARTIFACTS",
    "DEFAULT_MAX_TOTAL_BYTES",
    "MAX_MP4_BOXES",
    "MAX_MP4_BYTES",
    "MAX_MP4_NESTING_DEPTH",
    "MP4_MEDIA_TYPE",
    "ValidatedMp4",
    "VideoArtifactAuthorizationError",
    "VideoArtifactError",
    "VideoArtifactStore",
    "VideoArtifactValidationError",
    "validate_mp4",
]
