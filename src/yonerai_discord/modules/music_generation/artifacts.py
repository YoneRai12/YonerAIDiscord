"""Private, bounded WAV artifacts for music generation."""

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


WAV_MEDIA_TYPE = "audio/wav"
MAX_WAV_BYTES = 8 * 1024 * 1024
MIN_DURATION_SECONDS = 1
MAX_DURATION_SECONDS = 30
DEFAULT_MAX_ARTIFACTS = 64
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024

_ARTIFACT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}\Z")
_ARTIFACT_FILE = re.compile(r"(?P<artifact_id>[a-z0-9][a-z0-9_-]{0,95})\.wav\Z")
_TEMP_FILE = re.compile(r"\.audio-[a-f0-9]{16}\.tmp\Z")


class MusicArtifactError(RuntimeError):
    """Music artifact storage failed safely."""


class MusicArtifactValidationError(MusicArtifactError):
    """WAV bytes or their artifact reference violate the narrow contract."""


class MusicArtifactAuthorizationError(MusicArtifactError):
    """The request that owns an artifact is no longer authorized to read it."""


@dataclass(frozen=True, slots=True)
class ValidatedWav:
    data: bytes
    duration_seconds: float
    sha256: str


class MusicArtifactStore:
    """Store opaque request-bound PCM WAV files in a pre-created flat directory."""

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
            or not MAX_WAV_BYTES <= max_total_bytes <= 8 * 1024 * 1024 * 1024
        ):
            raise ValueError("max_total_bytes is outside the allowed range")
        self.root = Path(root)
        self._ensure_root()
        self.max_artifacts = max_artifacts
        self.max_total_bytes = max_total_bytes
        self._request_bindings: dict[str, tuple[bytes, int]] = {}
        self._total_bytes = 0
        self._lock = threading.RLock()
        self._cleanup_orphans()

    def put_wav(self, data: bytes, *, request_binding: str, artifact_id: str | None = None) -> ArtifactRef:
        validated = validate_wav(data)
        binding_digest = _request_binding_digest(request_binding)
        safe_id = _safe_artifact_id(artifact_id or f"aud-{secrets.token_hex(16)}")
        with self._lock:
            if safe_id in self._request_bindings:
                raise MusicArtifactError("artifact identifier collision")
            self._ensure_root()
            self._evict_for(len(validated.data))
            target = self.root / f"{safe_id}.wav"
            temporary = self.root / f".audio-{secrets.token_hex(8)}.tmp"
            try:
                reservation = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(reservation)
            except FileExistsError as exc:
                raise MusicArtifactError("artifact identifier collision") from exc
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
        return ArtifactRef(safe_id, ArtifactKind.AUDIO, WAV_MEDIA_TYPE, len(validated.data), validated.sha256)

    def read_wav(
        self, ref: ArtifactRef, *, request_binding: str, read_allowed: Callable[[], bool] | None = None
    ) -> bytes:
        safe_id = _validate_ref(ref)
        binding_digest = _request_binding_digest(request_binding)
        with self._lock:
            entry = self._request_bindings.get(safe_id)
            if entry is None or entry[0] != binding_digest:
                raise MusicArtifactAuthorizationError("music artifact request binding changed")
        self._ensure_root()
        target = self.root / f"{safe_id}.wav"
        _assert_not_symlink(target)
        _require_allowed(read_allowed)
        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        except (FileNotFoundError, OSError) as exc:
            raise MusicArtifactError("music artifact is unavailable") from exc
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise MusicArtifactError("music artifact must be a regular file")
            data = handle.read(MAX_WAV_BYTES + 1)
        _require_allowed(read_allowed)
        if len(data) > MAX_WAV_BYTES:
            raise MusicArtifactValidationError("stored WAV exceeds the size limit")
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise MusicArtifactValidationError("stored WAV integrity check failed")
        validate_wav(data)
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
            (self.root / f"{artifact_id}.wav").unlink()
            self._request_bindings.pop(artifact_id)
            self._total_bytes -= size_bytes

    def _ensure_root(self) -> None:
        _assert_no_symlink_ancestor(self.root)
        if not self.root.is_dir() or self.root.is_symlink():
            raise MusicArtifactError("music artifact root is unavailable")


def validate_wav(data: bytes) -> ValidatedWav:
    """Accept only exact RIFF/WAVE PCM16 mono/stereo WAV bytes with fmt then data."""
    if not isinstance(data, bytes):
        raise TypeError("WAV data must be bytes")
    if not data or len(data) > MAX_WAV_BYTES:
        raise MusicArtifactValidationError("WAV exceeds the size limit")
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MusicArtifactValidationError("WAV must be RIFF/WAVE")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if riff_size != len(data) - 8:
        raise MusicArtifactValidationError("WAV RIFF size must exactly match the file")
    if data[12:16] != b"fmt " or struct.unpack_from("<I", data, 16)[0] != 16:
        raise MusicArtifactValidationError("WAV must begin with one exact PCM fmt chunk")
    if len(data) < 36:
        raise MusicArtifactValidationError("truncated WAV fmt chunk")
    audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack_from(
        "<HHIIHH", data, 20
    )
    if audio_format != 1 or bits_per_sample != 16 or channels not in {1, 2} or sample_rate not in {44_100, 48_000}:
        raise MusicArtifactValidationError("WAV must be PCM16 mono/stereo at 44.1 or 48 kHz")
    expected_block_align = channels * (bits_per_sample // 8)
    if block_align != expected_block_align or byte_rate != sample_rate * block_align:
        raise MusicArtifactValidationError("WAV byte rate or block alignment is invalid")
    if data[36:40] != b"data" or len(data) < 44:
        raise MusicArtifactValidationError("WAV must contain data immediately after fmt")
    data_size = struct.unpack_from("<I", data, 40)[0]
    if data_size == 0 or data_size != len(data) - 44:
        raise MusicArtifactValidationError("WAV data size must exactly match the file")
    if data_size % block_align:
        raise MusicArtifactValidationError("WAV data must contain whole PCM frames")
    frames = data_size // block_align
    if not MIN_DURATION_SECONDS * sample_rate <= frames <= MAX_DURATION_SECONDS * sample_rate:
        raise MusicArtifactValidationError("WAV duration must be between 1 and 30 seconds")
    return ValidatedWav(data, frames / sample_rate, hashlib.sha256(data).hexdigest())


def _safe_artifact_id(value: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT_ID.fullmatch(value):
        raise MusicArtifactValidationError("artifact_id must be one safe path segment")
    return value


def _request_binding_digest(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise MusicArtifactValidationError("request_binding must be a non-empty bounded string")
    return hashlib.sha256(b"yonerai.music-artifact.v1\0" + value.encode("utf-8")).digest()


def _validate_ref(ref: ArtifactRef) -> str:
    if not isinstance(ref, ArtifactRef):
        raise TypeError("ref must be an ArtifactRef")
    if (
        ref.kind is not ArtifactKind.AUDIO
        or ref.media_type != WAV_MEDIA_TYPE
        or ref.size_bytes is None
        or ref.sha256 is None
        or ref.size_bytes > MAX_WAV_BYTES
    ):
        raise MusicArtifactValidationError("ArtifactRef is not a complete WAV audio reference")
    return _safe_artifact_id(ref.artifact_id)


def _require_allowed(callback: Callable[[], bool] | None) -> None:
    if callback is None:
        return
    try:
        allowed = callback()
    except Exception as exc:
        raise MusicArtifactAuthorizationError("music artifact authorization changed") from exc
    if allowed is not True:
        raise MusicArtifactAuthorizationError("music artifact authorization changed")


def _assert_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise MusicArtifactError("music artifact path must not be a symlink")


def _assert_no_symlink_ancestor(path: Path) -> None:
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and candidate.is_symlink():
            raise MusicArtifactError("music artifact root must not traverse a symlink")


__all__ = [
    "DEFAULT_MAX_ARTIFACTS",
    "DEFAULT_MAX_TOTAL_BYTES",
    "MAX_DURATION_SECONDS",
    "MAX_WAV_BYTES",
    "MIN_DURATION_SECONDS",
    "MusicArtifactAuthorizationError",
    "MusicArtifactError",
    "MusicArtifactStore",
    "MusicArtifactValidationError",
    "ValidatedWav",
    "WAV_MEDIA_TYPE",
    "validate_wav",
]
