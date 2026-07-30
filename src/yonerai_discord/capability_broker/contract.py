"""Typed, fail-closed contract for managed capability sandboxes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit


CAPABILITY_BROKER_CONTRACT = "yonerai.capability-broker.v1"
MAX_MEDIA_INSTRUCTION_CHARS = 2_000
MAX_MEDIA_INPUT_BYTES = 64 * 1024
MAX_MEDIA_OUTPUT_BYTES = 32 * 1024
MAX_WALL_TIME_SECONDS = 120.0

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_YOUTUBE_INPUT_HOSTS = frozenset({"www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"})


class CapabilityBrokerError(RuntimeError):
    """Base error whose message never contains provider or input material."""


class CapabilityContractError(CapabilityBrokerError, ValueError):
    """A typed request or response is outside the broker contract."""


class CapabilityUnavailableError(CapabilityBrokerError):
    """The managed backend is not safely available."""


class CapabilityAuthorizationError(CapabilityBrokerError):
    """Current permission or invocation approval is not valid."""


class CapabilityIdempotencyError(CapabilityBrokerError):
    """An idempotency key was reused for a different request."""


class CapabilityTimeoutError(CapabilityBrokerError):
    """The managed request exceeded its wall-clock limit."""


class CapabilityCleanupError(CapabilityBrokerError):
    """Backend cleanup could not be confirmed."""


class CapabilityAuditError(CapabilityBrokerError):
    """Required audit persistence is unavailable."""


class CapabilityKind(StrEnum):
    MEDIA_INSPECTION = "media.inspection"
    SUBTITLE_EXTRACTION = "media.subtitle.extract"
    THUMBNAIL_OCR = "media.thumbnail.ocr"


class ArtifactKind(StrEnum):
    INSPECTION_TEXT = "inspection_text"
    SUBTITLE_TEXT = "subtitle_text"
    OCR_TEXT = "ocr_text"


class PermissionClass(StrEnum):
    READ_PUBLIC_MEDIA = "read_public_media"


class ApprovalClass(StrEnum):
    FRESH_INVOCATION = "fresh_invocation"


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"


class AuditOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    REPLAYED = "replayed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"


class CleanupReason(StrEnum):
    COMPLETED = "completed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    actor_id: int
    guild_id: int
    conversation_id: int

    def __post_init__(self) -> None:
        for value in (self.actor_id, self.guild_id, self.conversation_id):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise CapabilityContractError("capability binding is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class MediaCapabilityInput:
    source_url: str = field(repr=False)
    instruction: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source_url, str):
            raise CapabilityContractError("media source is invalid")
        try:
            self.source_url.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise CapabilityContractError("media source is invalid") from None
        parsed = urlsplit(self.source_url)
        try:
            port = parsed.port
        except ValueError:
            raise CapabilityContractError("media source is outside the managed allowlist") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _YOUTUBE_INPUT_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.fragment
        ):
            raise CapabilityContractError("media source is outside the managed allowlist")
        if self.instruction is not None:
            _bounded_text(self.instruction, maximum=MAX_MEDIA_INSTRUCTION_CHARS, label="media instruction")


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    wall_time_seconds: float = 60.0
    max_input_bytes: int = MAX_MEDIA_INPUT_BYTES
    max_output_bytes: int = MAX_MEDIA_OUTPUT_BYTES
    max_concurrency: int = 1
    host_mount: bool = False
    secret_access: bool = False
    environment_access: bool = False
    privileged: bool = False
    persistent_workspace: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.wall_time_seconds, bool)
            or not isinstance(self.wall_time_seconds, (int, float))
            or not 1.0 <= float(self.wall_time_seconds) <= MAX_WALL_TIME_SECONDS
        ):
            raise CapabilityContractError("wall time limit is invalid")
        object.__setattr__(self, "wall_time_seconds", float(self.wall_time_seconds))
        for value, maximum in (
            (self.max_input_bytes, MAX_MEDIA_INPUT_BYTES),
            (self.max_output_bytes, MAX_MEDIA_OUTPUT_BYTES),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise CapabilityContractError("resource limit is invalid")
        if type(self.max_concurrency) is not int or self.max_concurrency != 1:
            raise CapabilityContractError("managed media concurrency must be one")
        if any(
            value is not False
            for value in (
                self.host_mount,
                self.secret_access,
                self.environment_access,
                self.privileged,
                self.persistent_workspace,
            )
        ):
            raise CapabilityContractError("managed sandbox containment flags are invalid")

    @property
    def digest(self) -> str:
        return _digest_document(
            {
                "environment_access": self.environment_access,
                "host_mount": self.host_mount,
                "max_concurrency": self.max_concurrency,
                "max_input_bytes": self.max_input_bytes,
                "max_output_bytes": self.max_output_bytes,
                "persistent_workspace": self.persistent_workspace,
                "privileged": self.privileged,
                "secret_access": self.secret_access,
                "wall_time_seconds": self.wall_time_seconds,
            },
            domain=b"yonerai.capability-broker.resources.v1\0",
        )


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    allowed_hosts: tuple[str, ...] = (
        ".youtube.com",
        ".youtu.be",
        ".googlevideo.com",
        ".ytimg.com",
        ".ggpht.com",
        "youtubei.googleapis.com",
    )

    def __post_init__(self) -> None:
        if self.allowed_hosts != (
            ".youtube.com",
            ".youtu.be",
            ".googlevideo.com",
            ".ytimg.com",
            ".ggpht.com",
            "youtubei.googleapis.com",
        ):
            raise CapabilityContractError("managed media network allowlist is not code-owned")

    @property
    def digest(self) -> str:
        return _digest_document(
            {"allowed_hosts": self.allowed_hosts},
            domain=b"yonerai.capability-broker.network.v1\0",
        )


@dataclass(frozen=True, slots=True, repr=False)
class CapabilityRequest:
    request_id: str
    idempotency_key: str
    binding: CapabilityBinding
    capability: CapabilityKind
    payload: MediaCapabilityInput = field(repr=False)
    permission: PermissionClass = PermissionClass.READ_PUBLIC_MEDIA
    approval: ApprovalClass = ApprovalClass.FRESH_INVOCATION
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    network: NetworkPolicy = field(default_factory=NetworkPolicy)

    def __post_init__(self) -> None:
        _identifier(self.request_id, label="request id")
        _identifier(self.idempotency_key, label="idempotency key")
        if not isinstance(self.binding, CapabilityBinding) or not isinstance(self.payload, MediaCapabilityInput):
            raise CapabilityContractError("capability request must use typed binding and payload")
        try:
            capability = CapabilityKind(self.capability)
            permission = PermissionClass(self.permission)
            approval = ApprovalClass(self.approval)
        except (TypeError, ValueError) as exc:
            raise CapabilityContractError("capability policy class is invalid") from exc
        if permission is not PermissionClass.READ_PUBLIC_MEDIA or approval is not ApprovalClass.FRESH_INVOCATION:
            raise CapabilityContractError("capability policy class is not allowed")
        if not isinstance(self.resources, ResourceLimits) or not isinstance(self.network, NetworkPolicy):
            raise CapabilityContractError("capability policy must be typed")
        input_bytes = len(self.payload.source_url.encode("utf-8", "strict"))
        if self.payload.instruction is not None:
            input_bytes += len(self.payload.instruction.encode("utf-8", "strict"))
        if input_bytes > self.resources.max_input_bytes:
            raise CapabilityContractError("capability input exceeds its resource limit")
        if capability is CapabilityKind.MEDIA_INSPECTION:
            if self.payload.instruction is None:
                raise CapabilityContractError("media inspection requires an instruction")
        elif self.payload.instruction is not None:
            raise CapabilityContractError("subtitle and OCR intents do not accept arbitrary instructions")
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "permission", permission)
        object.__setattr__(self, "approval", approval)

    @property
    def policy_digest(self) -> str:
        return capability_policy_digest(
            permission=self.permission,
            approval=self.approval,
            resources=self.resources,
            network=self.network,
        )

    @property
    def request_digest(self) -> str:
        return _digest_document(
            {
                "binding": {
                    "actor_id": self.binding.actor_id,
                    "conversation_id": self.binding.conversation_id,
                    "guild_id": self.binding.guild_id,
                },
                "capability": self.capability.value,
                "contract": CAPABILITY_BROKER_CONTRACT,
                "idempotency_key": self.idempotency_key,
                "instruction": self.payload.instruction,
                "policy_digest": self.policy_digest,
                "request_id": self.request_id,
                "source_url": self.payload.source_url,
            },
            domain=b"yonerai.capability-broker.request.v1\0",
        )


@dataclass(frozen=True, slots=True)
class BackendStatus:
    configured: bool
    ready: bool
    backend_id: str
    identity_digest: str
    policy_digest: str

    def __post_init__(self) -> None:
        _identifier(self.backend_id, label="backend id")
        _digest(self.identity_digest, label="backend identity")
        _digest(self.policy_digest, label="backend policy")
        if type(self.configured) is not bool or type(self.ready) is not bool:
            raise CapabilityContractError("backend status flags are invalid")
        if not self.configured and self.ready:
            raise CapabilityContractError("unconfigured backend cannot be ready")


@dataclass(frozen=True, slots=True, repr=False)
class BackendExecution:
    request_digest: str
    binding: CapabilityBinding
    backend_id: str
    identity_digest: str
    policy_digest: str
    artifact_kind: ArtifactKind
    text: str = field(repr=False)
    cleanup_confirmed: bool

    def __post_init__(self) -> None:
        _digest(self.request_digest, label="request digest")
        _identifier(self.backend_id, label="backend id")
        _digest(self.identity_digest, label="backend identity")
        _digest(self.policy_digest, label="backend policy")
        if not isinstance(self.binding, CapabilityBinding):
            raise CapabilityContractError("backend execution binding is invalid")
        try:
            kind = ArtifactKind(self.artifact_kind)
        except (TypeError, ValueError) as exc:
            raise CapabilityContractError("backend artifact kind is invalid") from exc
        _bounded_text(self.text, maximum=MAX_MEDIA_OUTPUT_BYTES, label="backend output", bytes_limit=True)
        if self.cleanup_confirmed is not True:
            raise CapabilityCleanupError("managed sandbox cleanup was not confirmed")
        object.__setattr__(self, "artifact_kind", kind)


@dataclass(frozen=True, slots=True)
class BackendCleanupReceipt:
    request_digest: str
    backend_id: str
    identity_digest: str
    worker_terminated: bool
    workspace_destroyed: bool

    def __post_init__(self) -> None:
        _digest(self.request_digest, label="request digest")
        _identifier(self.backend_id, label="backend id")
        _digest(self.identity_digest, label="backend identity")
        if self.worker_terminated is not True or self.workspace_destroyed is not True:
            raise CapabilityCleanupError("managed sandbox cleanup was not confirmed")


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    artifact_id: str
    kind: ArtifactKind
    media_type: str
    size_bytes: int
    sha256: str
    owner: CapabilityBinding

    def __post_init__(self) -> None:
        _identifier(self.artifact_id, label="artifact id")
        try:
            kind = ArtifactKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise CapabilityContractError("artifact kind is invalid") from exc
        if self.media_type != "text/plain; charset=utf-8":
            raise CapabilityContractError("artifact media type is invalid")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 1 <= self.size_bytes <= MAX_MEDIA_OUTPUT_BYTES
        ):
            raise CapabilityContractError("artifact size is invalid")
        _digest(self.sha256, label="artifact hash")
        if not isinstance(self.owner, CapabilityBinding):
            raise CapabilityContractError("artifact owner is invalid")
        object.__setattr__(self, "kind", kind)


@dataclass(frozen=True, slots=True, repr=False)
class CapabilityArtifact:
    descriptor: ArtifactDescriptor
    _text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ArtifactDescriptor):
            raise CapabilityContractError("artifact descriptor is invalid")
        encoded = self._text.encode("utf-8", "strict")
        if len(encoded) != self.descriptor.size_bytes or hashlib.sha256(encoded).hexdigest() != self.descriptor.sha256:
            raise CapabilityContractError("artifact integrity is invalid")

    def read_text(self, *, binding: CapabilityBinding) -> str:
        if binding != self.descriptor.owner:
            raise CapabilityAuthorizationError("artifact ownership binding does not match")
        return self._text


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    request_id: str
    request_digest: str
    status: ExecutionStatus
    backend_id: str
    identity_digest: str
    policy_digest: str
    artifact: ArtifactDescriptor
    cleanup_confirmed: bool

    def __post_init__(self) -> None:
        _identifier(self.request_id, label="request id")
        _digest(self.request_digest, label="request digest")
        _identifier(self.backend_id, label="backend id")
        _digest(self.identity_digest, label="backend identity")
        _digest(self.policy_digest, label="backend policy")
        if self.status is not ExecutionStatus.COMPLETED:
            raise CapabilityContractError("execution receipt status is invalid")
        if not isinstance(self.artifact, ArtifactDescriptor) or self.cleanup_confirmed is not True:
            raise CapabilityCleanupError("execution receipt is not cleanup-confirmed")


@dataclass(frozen=True, slots=True, repr=False)
class BrokerResult:
    receipt: ExecutionReceipt
    artifact: CapabilityArtifact = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, ExecutionReceipt) or not isinstance(self.artifact, CapabilityArtifact):
            raise CapabilityContractError("broker result is invalid")
        if self.receipt.artifact != self.artifact.descriptor:
            raise CapabilityContractError("broker result artifact binding is invalid")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    request_id: str
    request_digest: str
    binding: CapabilityBinding
    capability: CapabilityKind
    outcome: AuditOutcome
    occurred_at: datetime
    backend_id: str | None = None
    identity_digest: str | None = None
    artifact_id: str | None = None
    output_sha256: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.request_id, label="request id")
        _digest(self.request_digest, label="request digest")
        if not isinstance(self.binding, CapabilityBinding):
            raise CapabilityContractError("audit binding is invalid")
        try:
            object.__setattr__(self, "capability", CapabilityKind(self.capability))
            object.__setattr__(self, "outcome", AuditOutcome(self.outcome))
        except (TypeError, ValueError) as exc:
            raise CapabilityContractError("audit classification is invalid") from exc
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise CapabilityContractError("audit timestamp must be timezone-aware")
        if self.backend_id is not None:
            _identifier(self.backend_id, label="backend id")
        if self.identity_digest is not None:
            _digest(self.identity_digest, label="backend identity")
        if self.artifact_id is not None:
            _identifier(self.artifact_id, label="artifact id")
        if self.output_sha256 is not None:
            _digest(self.output_sha256, label="output hash")
        if self.failure_code is not None:
            _identifier(self.failure_code, label="failure code")


@dataclass(frozen=True, slots=True)
class CapabilityBrokerStatus:
    configured: bool
    ready: bool
    reason_code: str
    backend_id: str | None = None
    identity_digest: str | None = None

    def __post_init__(self) -> None:
        if type(self.configured) is not bool or type(self.ready) is not bool:
            raise CapabilityContractError("broker status flags are invalid")
        _identifier(self.reason_code, label="status reason")
        if self.backend_id is not None:
            _identifier(self.backend_id, label="backend id")
        if self.identity_digest is not None:
            _digest(self.identity_digest, label="backend identity")


AuthorizationCurrent = Callable[[], bool | Awaitable[bool]]
PermissionCurrent = Callable[[CapabilityBinding, PermissionClass], bool | Awaitable[bool]]


class ManagedSandboxBackend(Protocol):
    async def status(self, policy_digest: str) -> BackendStatus: ...

    async def execute(self, request: CapabilityRequest) -> BackendExecution: ...

    async def cleanup(self, request: CapabilityRequest, reason: CleanupReason) -> BackendCleanupReceipt: ...


class CapabilityAuditSink(Protocol):
    async def append(self, record: AuditRecord) -> None: ...


def capability_policy_digest(
    *,
    permission: PermissionClass = PermissionClass.READ_PUBLIC_MEDIA,
    approval: ApprovalClass = ApprovalClass.FRESH_INVOCATION,
    resources: ResourceLimits | None = None,
    network: NetworkPolicy | None = None,
) -> str:
    if permission is not PermissionClass.READ_PUBLIC_MEDIA or approval is not ApprovalClass.FRESH_INVOCATION:
        raise CapabilityContractError("capability policy class is not allowed")
    bounded_resources = resources or ResourceLimits()
    bounded_network = network or NetworkPolicy()
    if not isinstance(bounded_resources, ResourceLimits) or not isinstance(bounded_network, NetworkPolicy):
        raise CapabilityContractError("capability policy must be typed")
    return _digest_document(
        {
            "approval": approval.value,
            "network": bounded_network.digest,
            "permission": permission.value,
            "resources": bounded_resources.digest,
        },
        domain=b"yonerai.capability-broker.policy.v1\0",
    )


def _identifier(value: object, *, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise CapabilityContractError(f"{label} is invalid")


def _digest(value: object, *, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CapabilityContractError(f"{label} is invalid")


def _bounded_text(
    value: object,
    *,
    maximum: int,
    label: str,
    bytes_limit: bool = False,
) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
    ):
        raise CapabilityContractError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise CapabilityContractError(f"{label} is invalid") from exc
    if (len(encoded) if bytes_limit else len(value)) > maximum:
        raise CapabilityContractError(f"{label} exceeds its bound")


def _digest_document(value: object, *, domain: bytes) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(domain + encoded).hexdigest()


__all__ = [
    "ApprovalClass",
    "ArtifactDescriptor",
    "ArtifactKind",
    "AuditOutcome",
    "AuditRecord",
    "AuthorizationCurrent",
    "BackendCleanupReceipt",
    "BackendExecution",
    "BackendStatus",
    "BrokerResult",
    "CAPABILITY_BROKER_CONTRACT",
    "CapabilityArtifact",
    "CapabilityAuditError",
    "CapabilityAuditSink",
    "CapabilityAuthorizationError",
    "CapabilityBinding",
    "CapabilityBrokerError",
    "CapabilityBrokerStatus",
    "CapabilityCleanupError",
    "CapabilityContractError",
    "CapabilityIdempotencyError",
    "CapabilityKind",
    "CapabilityRequest",
    "CapabilityTimeoutError",
    "CapabilityUnavailableError",
    "CleanupReason",
    "ExecutionReceipt",
    "ExecutionStatus",
    "ManagedSandboxBackend",
    "MediaCapabilityInput",
    "NetworkPolicy",
    "PermissionClass",
    "PermissionCurrent",
    "ResourceLimits",
    "capability_policy_digest",
]
