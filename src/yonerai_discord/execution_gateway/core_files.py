from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from .models import ArtifactReference


CORE_ARTIFACT_REF_EXTENSION_V01 = "yonerai_core_artifact_ref_v0_1"
MAX_CORE_FILE_BYTES_V01 = 25 * 1024 * 1024

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MEDIA_TYPE_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z")
_KINDS = frozenset({"file", "image"})
_RETENTION_POLICIES = frozenset({"ephemeral", "session", "conversation"})
_LOCAL_BACKENDS = frozenset({"local", "local-artifact-store", "filesystem"})


class CoreFilesContractError(ValueError):
    """Files登録またはCore artifact refの境界違反。"""


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise CoreFilesContractError(f"{label} is invalid")
    return value


def _media_type(value: object) -> str:
    if not isinstance(value, str):
        raise CoreFilesContractError("media_type is invalid")
    normalized = value.strip().lower()
    if value != normalized or _MEDIA_TYPE_RE.fullmatch(normalized) is None:
        raise CoreFilesContractError("media_type is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class CoreArtifactOwnerScopeV01:
    provider: str
    subject_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        if self.provider != "discord":
            raise CoreFilesContractError("artifact owner provider is invalid")
        object.__setattr__(self, "subject_id", _identifier(self.subject_id, label="artifact owner subject_id"))
        object.__setattr__(
            self,
            "conversation_id",
            _identifier(self.conversation_id, label="artifact owner conversation_id"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "subject_id": self.subject_id,
            "conversation_id": self.conversation_id,
        }


@dataclass(frozen=True, slots=True)
class CoreArtifactRefV01:
    """Coreが読めることをFiles登録で確認したartifact ref。"""

    artifact_id: str
    attachment_id: str
    kind: str
    media_type: str
    size_bytes: int
    sha256: str
    owner_scope: CoreArtifactOwnerScopeV01
    backend: str
    retention: str
    provenance: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, label="artifact_id"))
        object.__setattr__(self, "attachment_id", _identifier(self.attachment_id, label="attachment_id"))
        if self.kind not in _KINDS:
            raise CoreFilesContractError("artifact kind is invalid")
        object.__setattr__(self, "media_type", _media_type(self.media_type))
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
            or self.size_bytes > MAX_CORE_FILE_BYTES_V01
        ):
            raise CoreFilesContractError("artifact size_bytes is invalid")
        if not isinstance(self.sha256, str) or _SHA256_RE.fullmatch(self.sha256) is None:
            raise CoreFilesContractError("artifact sha256 is invalid")
        if not isinstance(self.owner_scope, CoreArtifactOwnerScopeV01):
            raise CoreFilesContractError("artifact owner_scope is invalid")
        backend = _identifier(self.backend, label="artifact backend")
        if backend.casefold() in _LOCAL_BACKENDS:
            raise CoreFilesContractError("Core artifact backend must not be local")
        object.__setattr__(self, "backend", backend)
        if self.retention not in _RETENTION_POLICIES:
            raise CoreFilesContractError("artifact retention is invalid")
        object.__setattr__(self, "provenance", _identifier(self.provenance, label="artifact provenance"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "attachment_id": self.attachment_id,
            "kind": self.kind,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "owner_scope": self.owner_scope.to_mapping(),
            "backend": self.backend,
            "retention": self.retention,
            "provenance": self.provenance,
        }


@dataclass(frozen=True, slots=True)
class CoreFileRegistrationV01:
    """検証対象bytesと、そのbytesへ束縛した登録metadata。"""

    local_artifact_id: str
    kind: str
    media_type: str
    owner_scope: CoreArtifactOwnerScopeV01
    retention: str
    provenance: str
    content: bytes = field(repr=False)
    size_bytes: int = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "local_artifact_id",
            _identifier(self.local_artifact_id, label="local_artifact_id"),
        )
        if self.kind not in _KINDS:
            raise CoreFilesContractError("registration kind is invalid")
        media_type = _media_type(self.media_type)
        object.__setattr__(self, "media_type", media_type)
        if not isinstance(self.owner_scope, CoreArtifactOwnerScopeV01):
            raise CoreFilesContractError("registration owner_scope is invalid")
        if self.retention not in _RETENTION_POLICIES:
            raise CoreFilesContractError("registration retention is invalid")
        object.__setattr__(
            self,
            "provenance",
            _identifier(self.provenance, label="registration provenance"),
        )
        if not isinstance(self.content, bytes):
            raise CoreFilesContractError("registration content must be bytes")
        if not self.content or len(self.content) > MAX_CORE_FILE_BYTES_V01:
            raise CoreFilesContractError("registration content size is invalid")
        _validate_content_type(self.content, media_type=media_type, kind=self.kind)
        object.__setattr__(self, "size_bytes", len(self.content))
        object.__setattr__(self, "sha256", hashlib.sha256(self.content).hexdigest())

    def metadata_mapping(self) -> dict[str, object]:
        return {
            "local_artifact_id": self.local_artifact_id,
            "kind": self.kind,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "owner_scope": self.owner_scope.to_mapping(),
            "retention": self.retention,
            "provenance": self.provenance,
        }


class CoreFilesRegistrationPortV01(Protocol):
    async def register(self, request: CoreFileRegistrationV01) -> CoreArtifactRefV01: ...


@dataclass(frozen=True, slots=True)
class CoreFileReadRequestV01:
    """Core Filesから配送対象をscope-boundで読むためのtyped request。"""

    delivery_id: str
    ref: CoreArtifactRefV01 = field(repr=False)
    owner_scope: CoreArtifactOwnerScopeV01 = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "delivery_id",
            _identifier(self.delivery_id, label="delivery_id"),
        )
        if not isinstance(self.ref, CoreArtifactRefV01):
            raise CoreFilesContractError("read ref is invalid")
        if not isinstance(self.owner_scope, CoreArtifactOwnerScopeV01):
            raise CoreFilesContractError("read owner_scope is invalid")
        if self.ref.owner_scope != self.owner_scope:
            raise CoreFilesContractError("read ref owner_scope does not match request")


@dataclass(frozen=True, slots=True)
class CoreFileReadReceiptV01:
    """Core Filesがrequest bindingをechoして返す、非公開bytes receipt。"""

    delivery_id: str
    ref: CoreArtifactRefV01 = field(repr=False)
    owner_scope: CoreArtifactOwnerScopeV01 = field(repr=False)
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "delivery_id",
            _identifier(self.delivery_id, label="delivery_id"),
        )
        if not isinstance(self.ref, CoreArtifactRefV01):
            raise CoreFilesContractError("read receipt ref is invalid")
        if not isinstance(self.owner_scope, CoreArtifactOwnerScopeV01):
            raise CoreFilesContractError("read receipt owner_scope is invalid")
        if not isinstance(self.content, bytes):
            raise CoreFilesContractError("read receipt content must be bytes")
        if not self.content or len(self.content) > MAX_CORE_FILE_BYTES_V01:
            raise CoreFilesContractError("read receipt content size is invalid")


class CoreFilesReadPortV01(Protocol):
    async def read_for_delivery(self, request: CoreFileReadRequestV01) -> CoreFileReadReceiptV01: ...


async def read_core_file_v01(
    request: CoreFileReadRequestV01,
    port: CoreFilesReadPortV01,
) -> bytes:
    """Core Files receiptをrequest/ref/contentへ再束縛してbytesだけを返す。"""

    if not isinstance(request, CoreFileReadRequestV01):
        raise TypeError("request must be a CoreFileReadRequestV01")
    read_for_delivery = getattr(port, "read_for_delivery", None)
    if not callable(read_for_delivery):
        raise TypeError("port must expose a callable read_for_delivery method")
    receipt = await read_for_delivery(request)
    if not isinstance(receipt, CoreFileReadReceiptV01):
        raise CoreFilesContractError("Files read returned an invalid receipt")
    if (
        receipt.delivery_id != request.delivery_id
        or receipt.ref != request.ref
        or receipt.owner_scope != request.owner_scope
        or receipt.ref.owner_scope != request.owner_scope
    ):
        raise CoreFilesContractError("Files read receipt does not match the delivery request")
    content = receipt.content
    if len(content) != request.ref.size_bytes:
        raise CoreFilesContractError("Files read content does not match the registered Core ref")
    if hashlib.sha256(content).hexdigest() != request.ref.sha256:
        raise CoreFilesContractError("Files read content does not match the registered Core ref")
    _validate_content_type(content, media_type=request.ref.media_type, kind=request.ref.kind)
    return content


async def register_core_file_v01(
    request: CoreFileRegistrationV01,
    port: CoreFilesRegistrationPortV01,
) -> ArtifactReference:
    """注入されたFiles portのreceiptを検証し、Core ref付き中立DTOへ変換する。"""

    if not isinstance(request, CoreFileRegistrationV01):
        raise TypeError("request must be a CoreFileRegistrationV01")
    register = getattr(port, "register", None)
    if not callable(register):
        raise TypeError("port must expose a callable register method")
    ref = await register(request)
    if not isinstance(ref, CoreArtifactRefV01):
        raise CoreFilesContractError("Files registration returned an invalid Core artifact ref")
    if (
        ref.artifact_id == request.local_artifact_id
        or ref.attachment_id == request.local_artifact_id
        or ref.kind != request.kind
        or ref.media_type != request.media_type
        or ref.size_bytes != request.size_bytes
        or ref.sha256 != request.sha256
        or ref.owner_scope != request.owner_scope
        or ref.retention != request.retention
        or ref.provenance != request.provenance
        or ref.backend.casefold() in _LOCAL_BACKENDS
    ):
        raise CoreFilesContractError("Files registration receipt does not match the validated local artifact")
    return artifact_reference_from_core_v01(ref)


def artifact_reference_from_core_v01(ref: CoreArtifactRefV01) -> ArtifactReference:
    if not isinstance(ref, CoreArtifactRefV01):
        raise TypeError("ref must be a CoreArtifactRefV01")
    return ArtifactReference(
        artifact_id=ref.artifact_id,
        kind=ref.kind,
        media_type=ref.media_type,
        size_bytes=ref.size_bytes,
        metadata=MappingProxyType(
            {
                "sha256": ref.sha256,
                "owner_scope": ref.owner_scope.to_mapping(),
                "backend": ref.backend,
                "retention": ref.retention,
                "provenance": ref.provenance,
            }
        ),
        extensions=MappingProxyType({CORE_ARTIFACT_REF_EXTENSION_V01: ref}),
    )


def core_artifact_ref_from_mapping_v01(value: object) -> CoreArtifactRefV01:
    if not isinstance(value, Mapping):
        raise CoreFilesContractError("Core artifact ref payload is invalid")
    copied = dict(value)
    if set(copied) != {
        "artifact_id",
        "attachment_id",
        "kind",
        "media_type",
        "size_bytes",
        "sha256",
        "owner_scope",
        "backend",
        "retention",
        "provenance",
    }:
        raise CoreFilesContractError("Core artifact ref payload is invalid")
    owner = copied["owner_scope"]
    if not isinstance(owner, Mapping) or set(owner) != {"provider", "subject_id", "conversation_id"}:
        raise CoreFilesContractError("Core artifact owner payload is invalid")
    return CoreArtifactRefV01(
        artifact_id=copied["artifact_id"],  # type: ignore[arg-type]
        attachment_id=copied["attachment_id"],  # type: ignore[arg-type]
        kind=copied["kind"],  # type: ignore[arg-type]
        media_type=copied["media_type"],  # type: ignore[arg-type]
        size_bytes=copied["size_bytes"],  # type: ignore[arg-type]
        sha256=copied["sha256"],  # type: ignore[arg-type]
        owner_scope=CoreArtifactOwnerScopeV01(
            provider=owner["provider"],  # type: ignore[arg-type]
            subject_id=owner["subject_id"],  # type: ignore[arg-type]
            conversation_id=owner["conversation_id"],  # type: ignore[arg-type]
        ),
        backend=copied["backend"],  # type: ignore[arg-type]
        retention=copied["retention"],  # type: ignore[arg-type]
        provenance=copied["provenance"],  # type: ignore[arg-type]
    )


def core_ref_from_artifact_v01(
    artifact: ArtifactReference,
    *,
    owner_scope: CoreArtifactOwnerScopeV01,
) -> CoreArtifactRefV01:
    if not isinstance(artifact, ArtifactReference):
        raise TypeError("artifact must be an ArtifactReference")
    extensions = dict(artifact.extensions)
    if set(extensions) != {CORE_ARTIFACT_REF_EXTENSION_V01}:
        raise CoreFilesContractError("artifact is not a registered Core ref")
    ref = extensions[CORE_ARTIFACT_REF_EXTENSION_V01]
    if (
        not isinstance(ref, CoreArtifactRefV01)
        or ref.owner_scope != owner_scope
        or artifact.artifact_id != ref.artifact_id
        or artifact.kind != ref.kind
        or artifact.uri is not None
        or artifact.media_type != ref.media_type
        or artifact.size_bytes != ref.size_bytes
    ):
        raise CoreFilesContractError("artifact does not match its registered Core ref")
    metadata = dict(artifact.metadata)
    if metadata != {
        "sha256": ref.sha256,
        "owner_scope": ref.owner_scope.to_mapping(),
        "backend": ref.backend,
        "retention": ref.retention,
        "provenance": ref.provenance,
    }:
        raise CoreFilesContractError("artifact metadata does not match its registered Core ref")
    return ref


def _validate_content_type(content: bytes, *, media_type: str, kind: str) -> None:
    if kind == "image" and media_type not in {"image/png", "image/jpeg"}:
        raise CoreFilesContractError("image registration requires a supported image media_type")
    if media_type == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CoreFilesContractError("PNG content does not match media_type")
    if media_type == "image/jpeg" and not content.startswith(b"\xff\xd8\xff"):
        raise CoreFilesContractError("JPEG content does not match media_type")
    if media_type == "application/pdf" and not content.startswith(b"%PDF-"):
        raise CoreFilesContractError("PDF content does not match media_type")
    if media_type == "text/plain":
        try:
            decoded = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CoreFilesContractError("text content is not valid UTF-8") from None
        if "\x00" in decoded:
            raise CoreFilesContractError("text content contains NUL")


__all__ = [
    "CORE_ARTIFACT_REF_EXTENSION_V01",
    "MAX_CORE_FILE_BYTES_V01",
    "CoreArtifactOwnerScopeV01",
    "CoreArtifactRefV01",
    "CoreFileReadReceiptV01",
    "CoreFileReadRequestV01",
    "CoreFileRegistrationV01",
    "CoreFilesContractError",
    "CoreFilesReadPortV01",
    "CoreFilesRegistrationPortV01",
    "artifact_reference_from_core_v01",
    "core_artifact_ref_from_mapping_v01",
    "core_ref_from_artifact_v01",
    "read_core_file_v01",
    "register_core_file_v01",
]
