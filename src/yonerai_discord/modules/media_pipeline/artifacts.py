"""scope-boundなcanonical PNGだけをcontent-addressedで保存する。"""

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import sqlite3
import stat
import struct
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .domain import (
    ArtifactKind,
    ArtifactRef,
    ArtifactScope,
    MAX_MARKDOWN_BYTES,
    MAX_PNG_BYTES,
    MediaAuthorizationError,
    MediaIntegrityError,
    MediaPipelineError,
    MediaValidationError,
    validate_image_dimensions,
)
from .index import (
    DEFAULT_MEDIA_ARTIFACT_LIMITS,
    MEDIA_ARTIFACT_TTL_SECONDS,
    MediaArtifactIndex,
    MediaArtifactLimits,
    MediaArtifactRecord,
)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_MEDIA_TYPE = "image/png"
MARKDOWN_MEDIA_TYPE = "text/markdown; charset=utf-8"
_ARTIFACT_DOMAIN = b"yonerai.media.artifact.v1\0"
_DELIVERY_LEASE_DOMAIN = b"yonerai.media.delivery-lease.v1\0"
_HEX_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_OPAQUE_ARTIFACT_ID = re.compile(r"mp-[a-f0-9]{64}\Z")
_DELIVERY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_FINAL_FILE = re.compile(r"(mp-[a-f0-9]{64})\.(png|md)\Z")
_TEMP_FILE = re.compile(r"\.mp-[a-f0-9]{64}\.[a-f0-9]{16}\.tmp\Z")
_WINDOWS_DRIVE_REMOTE = 4
MAX_DELIVERY_RETENTION_SECONDS = 7 * 24 * 60 * 60
CommitCheck = Callable[[], bool]
_FileIdentity = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class CanonicalPng:
    data: bytes
    width: int
    height: int
    content_digest: str


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalMarkdown:
    data: bytes
    content_digest: str


@dataclass(frozen=True, slots=True, repr=False)
class MediaArtifactRetentionLease:
    lease_id: str
    retain_until: int


class MediaArtifactStore:
    """固定root直下でopaque IDだけをpathへ変換するPNG store。"""

    def __init__(
        self,
        root: Path | str,
        *,
        database_path: Path | str | None = None,
        limits: MediaArtifactLimits = DEFAULT_MEDIA_ARTIFACT_LIMITS,
        ttl_seconds: int = MEDIA_ARTIFACT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        candidate = Path(root)
        if not candidate.is_absolute():
            raise MediaValidationError("artifact root must be an absolute local path")
        if any(part == ".." for part in candidate.parts) or any(character in str(candidate) for character in "*?[]"):
            raise MediaValidationError("artifact root must not contain traversal or glob syntax")
        if str(candidate).startswith(("\\\\", "//")):
            raise MediaValidationError("UNC artifact roots are not allowed")
        if _windows_drive_type(candidate) == _WINDOWS_DRIVE_REMOTE:
            raise MediaValidationError("mapped network artifact roots are not allowed")
        self._root = candidate
        self._lock = threading.RLock()
        self._assert_safe_root()
        index_path = (
            Path(database_path) if database_path is not None else candidate.parent / f".{candidate.name}-index.sqlite3"
        )
        self._store_identity = hashlib.sha256(
            _DELIVERY_LEASE_DOMAIN
            + os.path.normcase(str(candidate.absolute())).encode("utf-8")
            + b"\0"
            + os.path.normcase(str(index_path.absolute())).encode("utf-8")
        ).hexdigest()
        self._index = MediaArtifactIndex(
            index_path,
            limits=limits,
            ttl_seconds=ttl_seconds,
            clock=clock,
        )
        try:
            with self._lock:
                self._maintenance_and_reconcile()
        except BaseException:
            self._index.close()
            raise

    @property
    def binding_digest(self) -> str:
        """Return the content-free identity used to bind durable consumers."""

        return self._store_identity

    def commit_image(
        self,
        image: Image.Image,
        *,
        scope: ArtifactScope,
        recipe_digest: str,
        kind: ArtifactKind,
        commit_check: CommitCheck,
    ) -> ArtifactRef:
        with self._lock:
            return self._commit_image(
                image,
                scope=scope,
                recipe_digest=recipe_digest,
                kind=kind,
                commit_check=commit_check,
            )

    def _commit_image(
        self,
        image: Image.Image,
        *,
        scope: ArtifactScope,
        recipe_digest: str,
        kind: ArtifactKind,
        commit_check: CommitCheck,
    ) -> ArtifactRef:
        if kind is ArtifactKind.DOCUMENT:
            raise MediaValidationError("document artifacts must use commit_markdown")
        canonical = canonicalize_image(image)
        ref = _build_ref(canonical, scope=scope, recipe_digest=recipe_digest, kind=kind)
        return self._commit_artifact(ref, canonical.data, scope=scope, commit_check=commit_check)

    def commit_markdown(
        self,
        text: str,
        *,
        scope: ArtifactScope,
        recipe_digest: str,
        commit_check: CommitCheck,
    ) -> ArtifactRef:
        with self._lock:
            canonical = canonicalize_markdown(text)
            ref = _build_ref(
                canonical,
                scope=scope,
                recipe_digest=recipe_digest,
                kind=ArtifactKind.DOCUMENT,
            )
            return self._commit_artifact(ref, canonical.data, scope=scope, commit_check=commit_check)

    def _commit_artifact(
        self,
        ref: ArtifactRef,
        data: bytes,
        *,
        scope: ArtifactScope,
        commit_check: CommitCheck,
    ) -> ArtifactRef:
        self._cleanup_expired()
        published_identity: _FileIdentity | None = None
        target = self._target(ref)
        temporary_identity: _FileIdentity | None = None
        try:
            with self._index.immediate() as connection:
                now = self._index.now()
                self._reconcile(connection, now=now)
                existing = self._index.get(connection, ref.artifact_id)
                if existing is not None:
                    _require_record_binding(existing, ref=ref, scope=scope)
                    _read_record_data(self._target(existing.ref), existing)
                    _require_commit_allowed(commit_check)
                    return ref

                self._index.enforce_quota(
                    connection,
                    scope=scope,
                    incoming_bytes=len(data),
                )
                temporary = self._root / f".{ref.artifact_id}.{secrets.token_hex(8)}.tmp"
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
                try:
                    descriptor = os.open(temporary, flags, 0o600)
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    temporary_identity = _owned_temp_identity(temporary)
                    self._assert_safe_root()
                    _require_same_owned_temp(temporary, temporary_identity)
                    _require_commit_allowed(commit_check)
                    try:
                        os.link(temporary, target)
                    except FileExistsError as exc:
                        raise MediaIntegrityError("artifact publish target unexpectedly exists") from exc
                    except OSError as exc:
                        raise MediaPipelineError("artifact atomic publish failed") from exc
                    target_identity = _regular_file_identity(target)
                    if target_identity != temporary_identity:
                        raise MediaIntegrityError("published artifact identity changed")
                    published_identity = target_identity
                    _read_ref_data(target, ref)
                    self._index.insert(connection, ref, scope, created_at=now)
                finally:
                    if temporary_identity is not None:
                        cleanup_during_exception = sys.exc_info()[0] is not None
                        try:
                            _cleanup_owned_temp(temporary, temporary_identity)
                        except MediaIntegrityError:
                            if not cleanup_during_exception:
                                raise
        except BaseException:
            if published_identity is not None:
                _unlink_if_same_file(target, published_identity)
            raise
        return ref

    def close(self) -> None:
        with self._lock:
            self._index.close()

    def retain(
        self,
        ref: ArtifactRef,
        *,
        scope: ArtifactScope,
        delivery_key: str,
        retain_until: int,
    ) -> MediaArtifactRetentionLease:
        with self._lock:
            _validate_ref_scope(ref, scope)
            delivery_key_digest = _delivery_key_digest(delivery_key)
            self._cleanup_expired()
            with self._index.immediate() as connection:
                now = self._index.now()
                if (
                    isinstance(retain_until, bool)
                    or not isinstance(retain_until, int)
                    or retain_until <= now
                    or retain_until > now + MAX_DELIVERY_RETENTION_SECONDS
                ):
                    raise MediaValidationError("retain_until is outside the delivery retention window")
                self._reconcile(
                    connection,
                    now=now,
                    exact_read_artifact_id=ref.artifact_id,
                )
                record = self._index.get(connection, ref.artifact_id)
                if record is None:
                    raise MediaIntegrityError("artifact is unavailable")
                _require_record_binding(record, ref=ref, scope=scope)
                _read_record_data(self._target(ref), record)
                retained = self._index.retain_for_delivery(
                    connection,
                    delivery_key_digest=delivery_key_digest,
                    artifact_id=ref.artifact_id,
                    scope_digest=scope.digest,
                    store_identity=self._store_identity,
                    retain_until=retain_until,
                    created_at=now,
                )
                if retained is None:
                    raise MediaIntegrityError("media artifact delivery lease is already released")
            return MediaArtifactRetentionLease(
                lease_id=_delivery_lease_id(
                    store_identity=self._store_identity,
                    delivery_key_digest=delivery_key_digest,
                    artifact_id=ref.artifact_id,
                ),
                retain_until=retained,
            )

    def release(
        self,
        ref: ArtifactRef,
        *,
        scope: ArtifactScope,
        delivery_key: str,
    ) -> bool:
        with self._lock:
            _validate_ref_scope(ref, scope)
            delivery_key_digest = _delivery_key_digest(delivery_key)
            with self._index.immediate() as connection:
                now = self._index.now()
                self._index.expire_delivery_leases(connection, now)
                released = self._index.release_delivery(
                    connection,
                    delivery_key_digest=delivery_key_digest,
                    artifact_id=ref.artifact_id,
                    scope_digest=scope.digest,
                    store_identity=self._store_identity,
                )
                if released is None:
                    record = self._index.get(connection, ref.artifact_id)
                    if record is None:
                        raise MediaIntegrityError("artifact is unavailable")
                    _require_record_binding(record, ref=ref, scope=scope)
            self._cleanup_expired()
            return bool(released)

    def _maintenance_and_reconcile(self) -> None:
        self._cleanup_expired()
        with self._index.immediate() as connection:
            self._reconcile(connection, now=self._index.now(), recover_crash_temps=True)

    def _cleanup_expired(self) -> None:
        self._assert_safe_root()
        pending_unlinks: list[tuple[Path, _FileIdentity]] = []
        with self._index.immediate() as connection:
            now = self._index.now()
            self._index.expire_delivery_leases(connection, now)
            for record in self._index.expired(connection, now):
                target = self._target(record.ref)
                if _path_exists_or_reparse(target):
                    identity = _regular_file_identity(target)
                    _read_record_data(target, record)
                    if _regular_file_identity(target) != identity:
                        raise MediaIntegrityError("expired artifact identity changed")
                    pending_unlinks.append((target, identity))
                self._index.delete(connection, record.artifact_id)
        for target, identity in pending_unlinks:
            _unlink_if_same_file_or_missing(target, identity)

    def _reconcile(
        self,
        connection: sqlite3.Connection,
        *,
        now: int,
        exact_read_artifact_id: str | None = None,
        recover_crash_temps: bool = False,
    ) -> None:
        self._assert_safe_root()
        records = self._index.all(connection)
        indexed: dict[str, ArtifactRef] = {}
        for record in records:
            if record.expires_at <= now and not self._index.is_retained(connection, record.artifact_id, now):
                raise MediaIntegrityError("expired media artifact was not reconciled")
            ref = record.ref
            target = self._target(ref)
            if ref.artifact_id != exact_read_artifact_id and _regular_file_identity(target)[2] != ref.byte_size:
                raise MediaIntegrityError("artifact indexed size changed")
            indexed[ref.artifact_id] = ref

        for entry in os.scandir(self._root):
            path = Path(entry.path)
            if _TEMP_FILE.fullmatch(entry.name):
                if not recover_crash_temps:
                    raise MediaIntegrityError("artifact root contains an unexpected temp entry")
                identity = _crash_temp_identity(path)
                if _crash_temp_identity(path) != identity:
                    raise MediaIntegrityError("artifact crash temp identity changed")
                _unlink_if_same_file(path, identity)
                continue
            match = _FINAL_FILE.fullmatch(entry.name)
            if match is None:
                raise MediaIntegrityError("artifact root contains an unknown entry")
            artifact_id = match.group(1)
            if artifact_id in indexed:
                if path != self._target(indexed[artifact_id]):
                    raise MediaIntegrityError("artifact file type changed")
                continue
            identity = _regular_file_identity(path)
            _read_unindexed_canonical(path, suffix=match.group(2))
            if _regular_file_identity(path) != identity:
                raise MediaIntegrityError("unindexed artifact identity changed")
            _unlink_if_same_file(path, identity)

    def read_png(self, ref: ArtifactRef, *, scope: ArtifactScope) -> bytes:
        with self._lock:
            return self._read_png(ref, scope=scope)

    def _read_png(self, ref: ArtifactRef, *, scope: ArtifactScope) -> bytes:
        if not isinstance(ref, ArtifactRef) or ref.kind is ArtifactKind.DOCUMENT:
            raise MediaValidationError("PNG artifact reference is unavailable")
        return self._read_artifact(ref, scope=scope)

    def read_markdown(self, ref: ArtifactRef, *, scope: ArtifactScope) -> bytes:
        with self._lock:
            if not isinstance(ref, ArtifactRef) or ref.kind is not ArtifactKind.DOCUMENT:
                raise MediaValidationError("document artifact reference is unavailable")
            return self._read_artifact(ref, scope=scope)

    def resolve_reference(
        self,
        artifact_id: str,
        *,
        scope: ArtifactScope,
        recipe_digest: str,
        content_digest: str,
        kind: ArtifactKind,
        byte_size: int,
    ) -> ArtifactRef:
        """Resolve one opaque external reference back to its exact scoped ref."""

        if not isinstance(artifact_id, str) or _OPAQUE_ARTIFACT_ID.fullmatch(artifact_id) is None:
            raise MediaValidationError("artifact identifier is unavailable")
        if not isinstance(scope, ArtifactScope):
            raise MediaValidationError("scope must be an ArtifactScope")
        for label, digest in (("recipe_digest", recipe_digest), ("content_digest", content_digest)):
            if not isinstance(digest, str) or _HEX_DIGEST.fullmatch(digest) is None:
                raise MediaValidationError(f"{label} must be a lowercase SHA-256 digest")
        if not isinstance(kind, ArtifactKind):
            raise MediaValidationError("kind must be an ArtifactKind")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 1:
            raise MediaValidationError("byte_size is invalid")
        with self._lock:
            self._cleanup_expired()
            with self._index.immediate() as connection:
                now = self._index.now()
                self._reconcile(connection, now=now, exact_read_artifact_id=artifact_id)
                record = self._index.get(connection, artifact_id)
                if record is None:
                    raise MediaIntegrityError("artifact is unavailable")
                ref = record.ref
                if (
                    ref.recipe_digest != recipe_digest
                    or ref.content_digest != content_digest
                    or ref.kind is not kind
                    or ref.byte_size != byte_size
                ):
                    raise MediaIntegrityError("artifact reference binding is invalid")
                _require_record_binding(record, ref=ref, scope=scope)
                _validate_ref_scope(ref, scope)
                _read_record_data(self._target(ref), record)
                return ref

    def _read_artifact(self, ref: ArtifactRef, *, scope: ArtifactScope) -> bytes:
        _validate_ref_scope(ref, scope)

        self._cleanup_expired()
        with self._index.immediate() as connection:
            self._reconcile(
                connection,
                now=self._index.now(),
                exact_read_artifact_id=ref.artifact_id,
            )
            record = self._index.get(connection, ref.artifact_id)
            if record is None:
                raise MediaIntegrityError("artifact is unavailable")
            _require_record_binding(record, ref=ref, scope=scope)
            return _read_record_data(self._target(ref), record)

    def _target(self, ref: ArtifactRef) -> Path:
        suffix = "md" if ref.kind is ArtifactKind.DOCUMENT else "png"
        return self._root / f"{ref.artifact_id}.{suffix}"

    def _assert_safe_root(self) -> None:
        _assert_no_reparse_ancestor(self._root)
        if not self._root.is_dir() or _is_link_or_reparse(self._root):
            raise MediaIntegrityError("artifact root is unavailable or redirected")


def _require_record_binding(
    record: MediaArtifactRecord,
    *,
    ref: ArtifactRef,
    scope: ArtifactScope,
) -> None:
    if (
        record.ref != ref
        or record.request_id != scope.request_id
        or record.guild_id != scope.guild_id
        or record.channel_id != scope.channel_id
        or record.user_id != scope.user_id
        or record.scope_digest != scope.digest
    ):
        raise MediaIntegrityError("artifact index binding is invalid")


def _validate_ref_scope(ref: ArtifactRef, scope: ArtifactScope) -> None:
    if not isinstance(ref, ArtifactRef):
        raise MediaValidationError("ref must be an ArtifactRef")
    if not isinstance(scope, ArtifactScope):
        raise MediaValidationError("scope must be an ArtifactScope")
    if scope.digest != ref.scope_digest:
        raise MediaAuthorizationError("artifact scope binding changed")
    if (
        _artifact_id(
            scope_digest=ref.scope_digest,
            recipe_digest=ref.recipe_digest,
            content_digest=ref.content_digest,
            kind=ref.kind,
            width=ref.width,
            height=ref.height,
            byte_size=ref.byte_size,
        )
        != ref.artifact_id
    ):
        raise MediaIntegrityError("artifact reference binding is invalid")


def _delivery_key_digest(delivery_key: str) -> str:
    if not isinstance(delivery_key, str) or _DELIVERY_KEY.fullmatch(delivery_key) is None:
        raise MediaValidationError("delivery_key must be a bounded opaque identifier")
    return hashlib.sha256(_DELIVERY_LEASE_DOMAIN + delivery_key.encode("ascii")).hexdigest()


def _delivery_lease_id(*, store_identity: str, delivery_key_digest: str, artifact_id: str) -> str:
    return (
        "ml-"
        + hashlib.sha256(
            _DELIVERY_LEASE_DOMAIN
            + store_identity.encode("ascii")
            + b"\0"
            + delivery_key_digest.encode("ascii")
            + b"\0"
            + artifact_id.encode("ascii")
        ).hexdigest()
    )


def _read_record_data(path: Path, record: MediaArtifactRecord) -> bytes:
    return _read_ref_data(path, record.ref)


def _read_ref_data(path: Path, ref: ArtifactRef) -> bytes:
    if ref.kind is ArtifactKind.DOCUMENT:
        return _read_ref_markdown(path, ref)
    return _read_ref_png(path, ref)


def _read_ref_png(path: Path, ref: ArtifactRef) -> bytes:
    data = _read_bounded_regular_file(path, maximum=MAX_PNG_BYTES)
    if len(data) != ref.byte_size or hashlib.sha256(data).hexdigest() != ref.content_digest:
        raise MediaIntegrityError("artifact content digest changed")
    canonical = validate_canonical_png(data)
    if canonical.width != ref.width or canonical.height != ref.height:
        raise MediaIntegrityError("artifact dimensions changed")
    return data


def _read_ref_markdown(path: Path, ref: ArtifactRef) -> bytes:
    data = _read_bounded_regular_file(path, maximum=MAX_MARKDOWN_BYTES)
    if len(data) != ref.byte_size or hashlib.sha256(data).hexdigest() != ref.content_digest:
        raise MediaIntegrityError("artifact content digest changed")
    validate_canonical_markdown(data)
    if ref.width != 0 or ref.height != 0:
        raise MediaIntegrityError("document dimensions changed")
    return data


def _read_unindexed_canonical(path: Path, *, suffix: str) -> None:
    if suffix == "png":
        validate_canonical_png(_read_bounded_regular_file(path, maximum=MAX_PNG_BYTES))
    elif suffix == "md":
        validate_canonical_markdown(_read_bounded_regular_file(path, maximum=MAX_MARKDOWN_BYTES))
    else:
        raise MediaIntegrityError("artifact file type is unavailable")


def _read_bounded_regular_file(path: Path, *, maximum: int) -> bytes:
    _assert_regular_path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError) as exc:
        raise MediaIntegrityError("artifact is unavailable") from exc
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise MediaIntegrityError("artifact must be a regular file")
        return handle.read(maximum + 1)


def canonicalize_image(image: Image.Image) -> CanonicalPng:
    if not isinstance(image, Image.Image):
        raise MediaValidationError("image must be a Pillow Image")
    validate_image_dimensions(*image.size)
    normalized = image.convert("RGB")
    try:
        normalized.info.clear()
        buffer = io.BytesIO()
        normalized.save(buffer, format="PNG", optimize=False, compress_level=9)
        data = buffer.getvalue()
    finally:
        normalized.close()
    if len(data) > MAX_PNG_BYTES:
        raise MediaValidationError("canonical PNG exceeds the byte limit")
    return CanonicalPng(
        data=data,
        width=image.width,
        height=image.height,
        content_digest=hashlib.sha256(data).hexdigest(),
    )


def validate_canonical_png(data: bytes) -> CanonicalPng:
    if not isinstance(data, bytes) or not data.startswith(PNG_SIGNATURE):
        raise MediaIntegrityError("artifact has an invalid PNG signature")
    if not 24 <= len(data) <= MAX_PNG_BYTES:
        raise MediaIntegrityError("artifact PNG byte size is invalid")
    width, height = struct.unpack(">II", data[16:24])
    validate_image_dimensions(width, height)
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.mode != "RGB" or image.size != (width, height):
                raise MediaIntegrityError("artifact PNG format is not canonical")
            image.load()
            rebuilt = canonicalize_image(image)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaIntegrityError("artifact PNG decode failed") from exc
    if rebuilt.data != data:
        raise MediaIntegrityError("artifact PNG contains non-canonical data or metadata")
    return rebuilt


def canonicalize_markdown(text: str) -> CanonicalMarkdown:
    if not isinstance(text, str):
        raise MediaValidationError("Markdown artifact must be text")
    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    if normalized.startswith("\ufeff"):
        normalized = normalized.removeprefix("\ufeff")
    if not normalized.strip():
        raise MediaValidationError("Markdown artifact must not be empty")
    if not normalized.endswith("\n"):
        normalized += "\n"
    if any(character == "\x00" or (ord(character) < 32 and character not in "\n\t") for character in normalized):
        raise MediaValidationError("Markdown artifact contains unsupported control text")
    try:
        data = normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MediaValidationError("Markdown artifact must be valid UTF-8 text") from exc
    if not 1 <= len(data) <= MAX_MARKDOWN_BYTES:
        raise MediaValidationError("Markdown artifact exceeds the byte limit")
    return CanonicalMarkdown(data=data, content_digest=hashlib.sha256(data).hexdigest())


def validate_canonical_markdown(data: bytes) -> CanonicalMarkdown:
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_MARKDOWN_BYTES:
        raise MediaIntegrityError("artifact Markdown byte size is invalid")
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data or not data.endswith(b"\n"):
        raise MediaIntegrityError("artifact Markdown encoding is not canonical")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MediaIntegrityError("artifact Markdown UTF-8 decode failed") from exc
    try:
        rebuilt = canonicalize_markdown(text)
    except MediaValidationError as exc:
        raise MediaIntegrityError("artifact Markdown is not canonical") from exc
    if rebuilt.data != data:
        raise MediaIntegrityError("artifact Markdown is not canonical")
    return rebuilt


def _build_ref(
    canonical: CanonicalPng | CanonicalMarkdown,
    *,
    scope: ArtifactScope,
    recipe_digest: str,
    kind: ArtifactKind,
) -> ArtifactRef:
    if not isinstance(scope, ArtifactScope):
        raise MediaValidationError("scope must be an ArtifactScope")
    if not isinstance(recipe_digest, str) or not _HEX_DIGEST.fullmatch(recipe_digest):
        raise MediaValidationError("recipe_digest must be a SHA-256 digest")
    if not isinstance(kind, ArtifactKind):
        raise MediaValidationError("kind must be an ArtifactKind")
    if isinstance(canonical, CanonicalMarkdown):
        if kind is not ArtifactKind.DOCUMENT:
            raise MediaValidationError("Markdown artifacts require document kind")
        width = height = 0
    elif isinstance(canonical, CanonicalPng):
        if kind is ArtifactKind.DOCUMENT:
            raise MediaValidationError("document artifacts require Markdown content")
        width, height = canonical.width, canonical.height
    else:
        raise MediaValidationError("canonical artifact is unavailable")
    artifact_id = _artifact_id(
        scope_digest=scope.digest,
        recipe_digest=recipe_digest,
        content_digest=canonical.content_digest,
        kind=kind,
        width=width,
        height=height,
        byte_size=len(canonical.data),
    )
    return ArtifactRef(
        artifact_id=artifact_id,
        scope_digest=scope.digest,
        recipe_digest=recipe_digest,
        content_digest=canonical.content_digest,
        kind=kind,
        width=width,
        height=height,
        byte_size=len(canonical.data),
    )


def _artifact_id(
    *,
    scope_digest: str,
    recipe_digest: str,
    content_digest: str,
    kind: ArtifactKind,
    width: int,
    height: int,
    byte_size: int,
) -> str:
    payload = "\0".join(
        (
            scope_digest,
            recipe_digest,
            content_digest,
            kind.value,
            str(width),
            str(height),
            str(byte_size),
        )
    ).encode("ascii")
    return f"mp-{hashlib.sha256(_ARTIFACT_DOMAIN + payload).hexdigest()}"


def _require_commit_allowed(commit_check: CommitCheck) -> None:
    if not callable(commit_check):
        raise MediaValidationError("commit_check must be callable")
    try:
        allowed = commit_check()
    except Exception as exc:
        raise MediaAuthorizationError("media commit authorization changed") from exc
    if allowed is not True:
        raise MediaAuthorizationError("media commit authorization changed")


def _path_exists_or_reparse(path: Path) -> bool:
    return path.exists() or _is_link_or_reparse(path)


def _owned_temp_identity(path: Path) -> _FileIdentity:
    if not _TEMP_FILE.fullmatch(path.name):
        raise MediaIntegrityError("artifact temp name is outside the owned contract")
    if _is_link_or_reparse(path):
        raise MediaIntegrityError("artifact temp must not be a symlink or reparse point")
    identity = _regular_file_identity(path)
    if identity[2] < 1 or identity[2] > MAX_PNG_BYTES:
        raise MediaIntegrityError("artifact temp size is outside the PNG limit")
    return identity


def _crash_temp_identity(path: Path) -> _FileIdentity:
    if not _TEMP_FILE.fullmatch(path.name):
        raise MediaIntegrityError("artifact crash temp name is outside the owned contract")
    if _is_link_or_reparse(path):
        raise MediaIntegrityError("artifact crash temp must not be a symlink or reparse point")
    identity = _regular_file_identity(path)
    if not 0 <= identity[2] <= MAX_PNG_BYTES:
        raise MediaIntegrityError("artifact crash temp size is outside the PNG limit")
    return identity


def _require_same_owned_temp(path: Path, expected: _FileIdentity) -> None:
    if _owned_temp_identity(path) != expected:
        raise MediaIntegrityError("artifact temp identity changed before publish")


def _cleanup_owned_temp(path: Path, expected: _FileIdentity) -> None:
    if not _path_exists_or_reparse(path):
        return
    if _is_link_or_reparse(path) or _regular_file_identity(path) != expected:
        raise MediaIntegrityError("artifact temp replacement suspected; cleanup refused")
    path.unlink()


def _unlink_if_same_file(path: Path, expected: _FileIdentity) -> None:
    if _is_link_or_reparse(path):
        raise MediaIntegrityError("artifact rollback target was redirected")
    if _regular_file_identity(path) != expected:
        raise MediaIntegrityError("artifact rollback target identity changed")
    path.unlink()


def _unlink_if_same_file_or_missing(path: Path, expected: _FileIdentity) -> None:
    if not _path_exists_or_reparse(path):
        return
    _unlink_if_same_file(path, expected)


def _regular_file_identity(path: Path) -> _FileIdentity:
    try:
        info = os.lstat(path)
    except (FileNotFoundError, OSError) as exc:
        raise MediaIntegrityError("artifact file identity is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise MediaIntegrityError("artifact file must be a regular file")
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if attributes & reparse_flag:
        raise MediaIntegrityError("artifact file must not be a reparse point")
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
    )


def _assert_regular_path(path: Path) -> None:
    if _is_link_or_reparse(path):
        raise MediaIntegrityError("artifact path must not be a symlink or reparse point")
    try:
        if not path.is_file():
            raise MediaIntegrityError("artifact is unavailable")
    except OSError as exc:
        raise MediaIntegrityError("artifact is unavailable") from exc


def _assert_no_reparse_ancestor(path: Path) -> None:
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if _is_link_or_reparse(candidate):
            raise MediaIntegrityError("artifact root must not traverse a symlink or reparse point")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _windows_drive_type(path: Path) -> int | None:
    if os.name != "nt":
        return None
    import ctypes

    root = path.anchor
    if not root:
        return None
    return int(ctypes.windll.kernel32.GetDriveTypeW(root))


__all__ = [
    "CanonicalMarkdown",
    "CanonicalPng",
    "CommitCheck",
    "MARKDOWN_MEDIA_TYPE",
    "MAX_DELIVERY_RETENTION_SECONDS",
    "MediaArtifactRetentionLease",
    "MediaArtifactStore",
    "PNG_MEDIA_TYPE",
    "PNG_SIGNATURE",
    "canonicalize_image",
    "canonicalize_markdown",
    "validate_canonical_markdown",
    "validate_canonical_png",
]
