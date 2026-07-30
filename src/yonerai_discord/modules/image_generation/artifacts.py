"""画像生成結果をPNGとして検証し、opaque IDで保存する境界。"""

from __future__ import annotations

import binascii
import hashlib
import os
import re
import secrets
import stat
import struct
import threading
import time
import zlib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from yonerai_discord.provider_registry import ArtifactKind, ArtifactRef


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_MEDIA_TYPE = "image/png"
MAX_PNG_BYTES = 8 * 1024 * 1024
MIN_IMAGE_DIMENSION = 64
MAX_IMAGE_DIMENSION = 2048
MAX_IMAGE_PIXELS = 4_194_304
MAX_PNG_CHUNKS = 128
DEFAULT_MAX_ARTIFACTS = 64
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_ARTIFACT_TTL_SECONDS = 15 * 60

_ARTIFACT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}\Z")
_ARTIFACT_FILE = re.compile(r"(?P<artifact_id>[a-z0-9][a-z0-9_-]{0,95})\.png\Z")
_TEMP_FILE = re.compile(r"\.image-[a-f0-9]{16}\.tmp\Z")
_CHUNK_TYPE = re.compile(rb"[A-Za-z]{4}\Z")
_PRESERVED_CHUNKS = {b"PLTE", b"tRNS"}
_VALID_BIT_DEPTHS = {
    0: {1, 2, 4, 8, 16},
    2: {8, 16},
    3: {1, 2, 4, 8},
    4: {8, 16},
    6: {8, 16},
}
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


class ImageArtifactError(RuntimeError):
    """画像artifactを安全に扱えなかった。"""


class ImageArtifactValidationError(ImageArtifactError):
    """PNGまたはArtifactRefが契約を満たしていない。"""


class ImageArtifactAuthorizationError(ImageArtifactError):
    """読出し権限が失効した。"""


@dataclass(frozen=True, slots=True)
class CanonicalPng:
    data: bytes
    width: int
    height: int
    sha256: str


class ImageArtifactStore:
    """固定root配下へ検証済みPNGだけを保存する小さなstore。"""

    def __init__(
        self,
        root: Path | str,
        *,
        max_artifacts: int = DEFAULT_MAX_ARTIFACTS,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        ttl_seconds: int = DEFAULT_ARTIFACT_TTL_SECONDS,
    ) -> None:
        if isinstance(max_artifacts, bool) or not isinstance(max_artifacts, int) or not 1 <= max_artifacts <= 4_096:
            raise ValueError("max_artifacts is outside the allowed range")
        if (
            isinstance(max_total_bytes, bool)
            or not isinstance(max_total_bytes, int)
            or not MAX_PNG_BYTES <= max_total_bytes <= 8 * 1024 * 1024 * 1024
        ):
            raise ValueError("max_total_bytes is outside the allowed range")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 86_400:
            raise ValueError("ttl_seconds is outside the allowed range")
        self.root = Path(root)
        self._ensure_root()
        # Stage 1はprocess内の直接実行だけを対象とする。request bindingも
        # diskへ永続化せず、再起動後は既存artifactをfail-closedにする。
        self.max_artifacts = max_artifacts
        self.max_total_bytes = max_total_bytes
        self.ttl_seconds = ttl_seconds
        self._request_bindings: dict[str, tuple[bytes, int, float]] = {}
        self._protected_artifacts: dict[str, int] = {}
        self._total_bytes = 0
        self._lock = threading.RLock()
        self._cleanup_orphans()

    def put_png(
        self,
        data: bytes,
        *,
        request_binding: str,
        artifact_id: str | None = None,
    ) -> ArtifactRef:
        canonical = canonicalize_png(data)
        binding_digest = _request_binding_digest(request_binding)
        safe_id = _safe_artifact_id(artifact_id or f"img-{secrets.token_hex(16)}")
        with self._lock:
            if safe_id in self._request_bindings:
                raise ImageArtifactError("artifact identifier collision")
            self._ensure_root()
            self._evict_for(len(canonical.data))

            # artifactごとの親directoryを作らず、信頼済み固定root直下だけで
            # reserve/write/replaceする。これにより、作成後の親directoryを
            # symlinkへ差し替えるWindows上のTOCTOU経路を持たない。
            target = self.root / f"{safe_id}.png"
            temporary = self.root / f".image-{secrets.token_hex(8)}.tmp"
            try:
                reservation = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(reservation)
            except FileExistsError as exc:
                raise ImageArtifactError("artifact identifier collision") from exc
            try:
                descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(canonical.data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                os.chmod(target, 0o600)
            except Exception:
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                raise
            self._request_bindings[safe_id] = (
                binding_digest,
                len(canonical.data),
                time.monotonic() + self.ttl_seconds,
            )
            self._total_bytes += len(canonical.data)

        return ArtifactRef(
            artifact_id=safe_id,
            kind=ArtifactKind.IMAGE,
            media_type=PNG_MEDIA_TYPE,
            size_bytes=len(canonical.data),
            sha256=canonical.sha256,
        )

    @contextmanager
    def protect_png(
        self,
        ref: ArtifactRef,
        *,
        request_binding: str,
    ) -> Iterator[None]:
        """1操作中のsourceをquota eviction対象から外す。"""

        safe_id = _validate_ref(ref)
        binding_digest = _request_binding_digest(request_binding)
        with self._lock:
            self._require_current_entry(safe_id, binding_digest)
            self._protected_artifacts[safe_id] = self._protected_artifacts.get(safe_id, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._protected_artifacts.get(safe_id, 0) - 1
                if remaining > 0:
                    self._protected_artifacts[safe_id] = remaining
                else:
                    self._protected_artifacts.pop(safe_id, None)

    def read_png(
        self,
        ref: ArtifactRef,
        *,
        request_binding: str,
        read_allowed: Callable[[], bool] | None = None,
    ) -> bytes:
        safe_id = _validate_ref(ref)
        binding_digest = _request_binding_digest(request_binding)
        with self._lock:
            self._require_current_entry(safe_id, binding_digest)
        self._ensure_root()
        target = self.root / f"{safe_id}.png"
        _assert_not_symlink(target)
        _require_allowed(read_allowed)

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except (FileNotFoundError, OSError) as exc:
            raise ImageArtifactError("image artifact is unavailable") from exc
        try:
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ImageArtifactError("image artifact must be a regular file")
                data = handle.read(MAX_PNG_BYTES + 1)
        except Exception:
            raise

        _require_allowed(read_allowed)
        with self._lock:
            self._require_current_entry(safe_id, binding_digest)
        if len(data) > MAX_PNG_BYTES:
            raise ImageArtifactValidationError("stored PNG exceeds the size limit")
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ImageArtifactValidationError("stored PNG integrity check failed")
        canonical = canonicalize_png(data)
        if canonical.data != data:
            raise ImageArtifactValidationError("stored PNG is not canonical")
        return data

    def discard_png(self, ref: ArtifactRef, *, request_binding: str) -> bool:
        """未送信の private artifact を、同じ request binding のみで破棄する。"""
        safe_id = _validate_ref(ref)
        binding_digest = _request_binding_digest(request_binding)
        with self._lock:
            entry = self._request_bindings.get(safe_id)
            if entry is None or entry[0] != binding_digest or safe_id in self._protected_artifacts:
                return False
            self._ensure_root()
            target = self.root / f"{safe_id}.png"
            try:
                _assert_not_symlink(target)
                target.unlink(missing_ok=True)
            except OSError:
                return False
            self._request_bindings.pop(safe_id, None)
            self._total_bytes -= entry[1]
            return True

    def _cleanup_orphans(self) -> None:
        with self._lock:
            for candidate in self.root.iterdir():
                if candidate.is_dir():
                    continue
                if _ARTIFACT_FILE.fullmatch(candidate.name) or _TEMP_FILE.fullmatch(candidate.name):
                    candidate.unlink(missing_ok=True)

    def _evict_for(self, incoming_bytes: int) -> None:
        while self._request_bindings and (
            len(self._request_bindings) >= self.max_artifacts
            or self._total_bytes + incoming_bytes > self.max_total_bytes
        ):
            artifact_id = next(
                (candidate for candidate in self._request_bindings if candidate not in self._protected_artifacts),
                None,
            )
            if artifact_id is None:
                raise ImageArtifactError("image artifact quota cannot evict a protected artifact")
            _binding, size_bytes, _expires_at = self._request_bindings[artifact_id]
            (self.root / f"{artifact_id}.png").unlink(missing_ok=True)
            self._request_bindings.pop(artifact_id)
            self._total_bytes -= size_bytes

    def _expire_entry(self, artifact_id: str, entry: tuple[bytes, int, float]) -> None:
        """期限切れ対象だけをroot再検証後にunlinkし、成功後にstateを更新する。"""

        if artifact_id in self._protected_artifacts:
            return
        self._ensure_root()
        target = self.root / f"{artifact_id}.png"
        _assert_not_symlink(target)
        target.unlink(missing_ok=True)
        self._request_bindings.pop(artifact_id)
        self._total_bytes -= entry[1]

    def _require_current_entry(self, artifact_id: str, binding_digest: bytes) -> None:
        """caller holding _lock で、binding と TTL を同じ境界で検証する。"""

        entry = self._request_bindings.get(artifact_id)
        if entry is None or entry[0] != binding_digest:
            raise ImageArtifactAuthorizationError("image artifact request binding changed")
        if entry[2] <= time.monotonic():
            self._expire_entry(artifact_id, entry)
            raise ImageArtifactError("image artifact expired")

    def _ensure_root(self) -> None:
        _assert_no_symlink_ancestor(self.root)
        if not self.root.is_dir() or self.root.is_symlink():
            raise ImageArtifactError("image artifact root is unavailable")


def canonicalize_png(data: bytes) -> CanonicalPng:
    """PNGを検証し、実画素に不要なancillary metadataを除去する。"""

    if not isinstance(data, bytes):
        raise TypeError("PNG data must be bytes")
    if len(data) > MAX_PNG_BYTES:
        raise ImageArtifactValidationError("PNG exceeds the size limit")
    if not data.startswith(PNG_SIGNATURE):
        raise ImageArtifactValidationError("invalid PNG signature")

    chunks = _parse_chunks(data)
    ihdr = chunks[0][1]
    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", ihdr)
    _validate_ihdr(
        width=width,
        height=height,
        bit_depth=bit_depth,
        color_type=color_type,
        compression=compression,
        filter_method=filter_method,
        interlace=interlace,
    )
    _validate_chunk_contract(chunks, color_type=color_type)

    compressed = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    raw = _inflate_scanlines(
        compressed,
        width=width,
        height=height,
        bit_depth=bit_depth,
        color_type=color_type,
    )

    output = bytearray(PNG_SIGNATURE)
    output.extend(_encode_chunk(b"IHDR", ihdr))
    for kind, payload in chunks:
        if kind in _PRESERVED_CHUNKS:
            output.extend(_encode_chunk(kind, payload))
    output.extend(_encode_chunk(b"IDAT", zlib.compress(raw, level=9)))
    output.extend(_encode_chunk(b"IEND", b""))
    canonical = bytes(output)
    if len(canonical) > MAX_PNG_BYTES:
        raise ImageArtifactValidationError("canonical PNG exceeds the size limit")
    return CanonicalPng(
        data=canonical,
        width=width,
        height=height,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _parse_chunks(data: bytes) -> tuple[tuple[bytes, bytes], ...]:
    cursor = len(PNG_SIGNATURE)
    chunks: list[tuple[bytes, bytes]] = []
    while cursor < len(data):
        if len(chunks) >= MAX_PNG_CHUNKS:
            raise ImageArtifactValidationError("PNG contains too many chunks")
        if len(data) - cursor < 12:
            raise ImageArtifactValidationError("truncated PNG chunk")
        length = struct.unpack_from(">I", data, cursor)[0]
        cursor += 4
        chunk_type = data[cursor : cursor + 4]
        cursor += 4
        if not _CHUNK_TYPE.fullmatch(chunk_type) or chunk_type[2] & 0x20:
            raise ImageArtifactValidationError("invalid PNG chunk type")
        end = cursor + length
        if end + 4 > len(data):
            raise ImageArtifactValidationError("truncated PNG chunk payload")
        payload = data[cursor:end]
        expected_crc = struct.unpack_from(">I", data, end)[0]
        actual_crc = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ImageArtifactValidationError("invalid PNG chunk CRC")
        chunks.append((chunk_type, payload))
        cursor = end + 4
        if chunk_type == b"IEND":
            if cursor != len(data):
                raise ImageArtifactValidationError("PNG contains trailing data")
            break

    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        raise ImageArtifactValidationError("PNG must start with one IHDR chunk")
    if chunks[-1] != (b"IEND", b""):
        raise ImageArtifactValidationError("PNG must end with an empty IEND chunk")
    return tuple(chunks)


def _validate_ihdr(
    *,
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    compression: int,
    filter_method: int,
    interlace: int,
) -> None:
    if not MIN_IMAGE_DIMENSION <= width <= MAX_IMAGE_DIMENSION:
        raise ImageArtifactValidationError("PNG width is outside the allowed range")
    if not MIN_IMAGE_DIMENSION <= height <= MAX_IMAGE_DIMENSION:
        raise ImageArtifactValidationError("PNG height is outside the allowed range")
    if width * height > MAX_IMAGE_PIXELS:
        raise ImageArtifactValidationError("PNG pixel count exceeds the limit")
    if color_type not in _VALID_BIT_DEPTHS or bit_depth not in _VALID_BIT_DEPTHS[color_type]:
        raise ImageArtifactValidationError("unsupported PNG color format")
    if compression != 0 or filter_method != 0 or interlace != 0:
        raise ImageArtifactValidationError("PNG must use standard non-interlaced encoding")


def _validate_chunk_contract(chunks: tuple[tuple[bytes, bytes], ...], *, color_type: int) -> None:
    counts: dict[bytes, int] = {}
    saw_idat = False
    ended_idat = False
    palette_entries = 0
    for index, (kind, payload) in enumerate(chunks):
        counts[kind] = counts.get(kind, 0) + 1
        if kind == b"IHDR":
            if index != 0 or counts[kind] != 1:
                raise ImageArtifactValidationError("PNG contains duplicate IHDR")
            continue
        if kind == b"IDAT":
            if ended_idat:
                raise ImageArtifactValidationError("PNG IDAT chunks must be consecutive")
            saw_idat = True
            continue
        if saw_idat and kind != b"IEND":
            ended_idat = True
        if kind == b"PLTE":
            if saw_idat or counts[kind] != 1 or not 3 <= len(payload) <= 768 or len(payload) % 3:
                raise ImageArtifactValidationError("invalid PNG palette")
            if color_type in {0, 4}:
                raise ImageArtifactValidationError("PNG color type must not contain a palette")
            palette_entries = len(payload) // 3
        elif kind == b"tRNS":
            if saw_idat or counts[kind] != 1:
                raise ImageArtifactValidationError("invalid PNG transparency chunk")
            valid = (
                (color_type == 0 and len(payload) == 2)
                or (color_type == 2 and len(payload) == 6)
                or (color_type == 3 and palette_entries > 0 and 0 < len(payload) <= palette_entries)
            )
            if not valid:
                raise ImageArtifactValidationError("invalid PNG transparency data")
        elif kind == b"IEND":
            if counts[kind] != 1 or payload:
                raise ImageArtifactValidationError("invalid PNG IEND chunk")
        elif not kind[0] & 0x20:
            raise ImageArtifactValidationError("unknown critical PNG chunk")

    if not saw_idat:
        raise ImageArtifactValidationError("PNG must contain image data")
    if color_type == 3 and palette_entries == 0:
        raise ImageArtifactValidationError("indexed PNG requires a palette")


def _inflate_scanlines(
    compressed: bytes,
    *,
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
) -> bytes:
    row_bytes = (width * bit_depth * _CHANNELS[color_type] + 7) // 8
    expected = height * (row_bytes + 1)
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(compressed, expected + 1)
        if len(raw) > expected or inflater.unconsumed_tail:
            raise ImageArtifactValidationError("PNG decompressed data exceeds the expected size")
        raw += inflater.flush()
    except zlib.error as exc:
        raise ImageArtifactValidationError("invalid PNG compressed data") from exc
    if not inflater.eof or inflater.unused_data or len(raw) != expected:
        raise ImageArtifactValidationError("PNG decompressed data length is invalid")
    stride = row_bytes + 1
    if any(raw[offset] > 4 for offset in range(0, len(raw), stride)):
        raise ImageArtifactValidationError("PNG contains an invalid filter type")
    return raw


def _encode_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _safe_artifact_id(value: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT_ID.fullmatch(value):
        raise ImageArtifactValidationError("artifact_id must be one safe path segment")
    return value


def _request_binding_digest(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ImageArtifactValidationError("request_binding must be a non-empty bounded string")
    return hashlib.sha256(b"yonerai.image-artifact.v1\0" + value.encode("utf-8")).digest()


def _validate_ref(ref: ArtifactRef) -> str:
    if not isinstance(ref, ArtifactRef):
        raise TypeError("ref must be an ArtifactRef")
    if (
        ref.kind is not ArtifactKind.IMAGE
        or ref.media_type != PNG_MEDIA_TYPE
        or ref.size_bytes is None
        or ref.sha256 is None
        or ref.size_bytes > MAX_PNG_BYTES
    ):
        raise ImageArtifactValidationError("ArtifactRef is not a complete PNG image reference")
    return _safe_artifact_id(ref.artifact_id)


def _require_allowed(callback: Callable[[], bool] | None) -> None:
    if callback is None:
        return
    try:
        allowed = callback()
    except Exception as exc:
        raise ImageArtifactAuthorizationError("image artifact authorization changed") from exc
    if allowed is not True:
        raise ImageArtifactAuthorizationError("image artifact authorization changed")


def _assert_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ImageArtifactError("image artifact path must not be a symlink")


def _assert_no_symlink_ancestor(path: Path) -> None:
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and candidate.is_symlink():
            raise ImageArtifactError("image artifact root must not traverse a symlink")


__all__ = [
    "CanonicalPng",
    "ImageArtifactAuthorizationError",
    "ImageArtifactError",
    "ImageArtifactStore",
    "ImageArtifactValidationError",
    "DEFAULT_MAX_ARTIFACTS",
    "DEFAULT_MAX_TOTAL_BYTES",
    "MAX_IMAGE_DIMENSION",
    "MAX_IMAGE_PIXELS",
    "MAX_PNG_BYTES",
    "MAX_PNG_CHUNKS",
    "MIN_IMAGE_DIMENSION",
    "PNG_MEDIA_TYPE",
    "canonicalize_png",
]
