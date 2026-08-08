"""Typed, external-only contract for future dynamic Forge sandbox work."""

from __future__ import annotations

import ast
import hashlib
import io
import ipaddress
import json
import re
import tokenize
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol


SANDBOX_POLICY_REVISION = "1"
MAX_SOURCE_BYTES = 32_768
MAX_JSON_BYTES = 16_384
MAX_OUTPUT_BYTES = 16_384
MAX_ARTIFACTS = 4
MAX_WALL_TIME_MS = 60_000
MAX_CPU_TIME_MS = 30_000
MAX_MEMORY_MIB = 512
MAX_PROCESSES = 0
MAX_FILES = 8

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_NONCE = re.compile(r"[a-f0-9]{32}\Z")
_SECRET = re.compile(
    r"(?i)(?:\b(?:basic|bearer)\s+[a-z0-9._~+/=-]{4,}"
    r"|\b(?:api[_-]?key|authorization|cookie|token|secret|password)\s*[:=]\s*\S+"
    r"|\bsk-(?:proj-)?[a-z0-9_-]{8,}"
    r"|\bgithub_pat_[a-z0-9_]{8,}"
    r"|\bgh[pousr]_[a-z0-9]{8,}"
    r"|\bxox[baprs]-[a-z0-9-]{8,}"
    r"|\bAIza[a-z0-9_-]{16,}"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{12,})"
)
_HOST_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/]|\\\\|\\device\\|file:|\.\.[\\/]"
    r"|(?:^|[\s\"'=:(])/[a-z0-9._~-]+(?:/|$)"
    r"|\b(?:glob|import|open|exec|eval|compile|__import__|os|pathlib|shutil|socket|requests|urllib|subprocess|powershell|cmd\.exe)\b)"
)
_PRIVATE_KEY_MARKER = re.compile(r"(?i)-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----")
_NETWORK_LOCATOR = re.compile(
    r"(?i)(?:\b(?:https?|ftp|ws|wss)://"
    r"|\b(?:localhost|ip6-localhost|ip6-loopback)\b"
    r"|(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d))"
)
_IP_LITERAL = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])"
    r"|(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"
)
_HOST_CONTROL_KEY_FRAGMENTS = frozenset(
    {
        "args",
        "argv",
        "cmd",
        "command",
        "cwd",
        "env",
        "environment",
        "exec",
        "file",
        "mount",
        "path",
        "process",
        "shell",
        "socket",
        "uri",
        "url",
    }
)
_SECRET_KEY_FRAGMENTS = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "password",
        "privatekey",
        "secret",
        "token",
    }
)


class SandboxContractError(ValueError):
    """A value is outside the sealed external-sandbox contract."""


class SandboxEntrypoint(StrEnum):
    PYTHON_PURE = "python_pure"


class SandboxTerminationReason(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SandboxScope:
    request_id: str
    guild_id: int | None
    channel_id: int | None
    user_id: int

    def __post_init__(self) -> None:
        _identifier(self.request_id)
        _optional_id(self.guild_id)
        _optional_id(self.channel_id)
        _positive_id(self.user_id)

    def to_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "channel_id": self.channel_id,
                "guild_id": self.guild_id,
                "request_id": self.request_id,
                "user_id": self.user_id,
            }
        )


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    revision: str = SANDBOX_POLICY_REVISION
    language: SandboxEntrypoint = SandboxEntrypoint.PYTHON_PURE
    max_source_bytes: int = MAX_SOURCE_BYTES
    max_input_bytes: int = MAX_JSON_BYTES
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_cpu_time_ms: int = MAX_CPU_TIME_MS
    max_wall_time_ms: int = MAX_WALL_TIME_MS
    max_memory_mib: int = MAX_MEMORY_MIB
    max_processes: int = MAX_PROCESSES
    max_files: int = MAX_FILES
    network_connections: int = 0
    host_mount: bool = False
    secret_access: bool = False
    environment_access: bool = False
    privileged: bool = False
    docker_socket: bool = False
    clipboard: bool = False
    persistent_profile: bool = False

    def __post_init__(self) -> None:
        if self.revision != SANDBOX_POLICY_REVISION or self.language is not SandboxEntrypoint.PYTHON_PURE:
            raise SandboxContractError("policy is not code-owned")
        for value, maximum in (
            (self.max_source_bytes, MAX_SOURCE_BYTES),
            (self.max_input_bytes, MAX_JSON_BYTES),
            (self.max_output_bytes, MAX_OUTPUT_BYTES),
            (self.max_cpu_time_ms, MAX_CPU_TIME_MS),
            (self.max_wall_time_ms, MAX_WALL_TIME_MS),
            (self.max_memory_mib, MAX_MEMORY_MIB),
            (self.max_files, MAX_FILES),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise SandboxContractError("policy limit is invalid")
        if type(self.max_processes) is not int or self.max_processes != 0:
            raise SandboxContractError("child process limit must be zero")
        if (
            type(self.network_connections) is not int
            or self.network_connections != 0
            or any(
                value is not False
                for value in (
                    self.host_mount,
                    self.secret_access,
                    self.environment_access,
                    self.privileged,
                    self.docker_socket,
                    self.clipboard,
                    self.persistent_profile,
                )
            )
        ):
            raise SandboxContractError("policy containment flags are not exact")

    @property
    def digest(self) -> str:
        encoded = json.dumps(dict(self.to_mapping()), separators=(",", ":"), sort_keys=True).encode("ascii")
        return hashlib.sha256(b"yonerai.capability_forge.sandbox.policy.v1\0" + encoded).hexdigest()

    def to_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "clipboard": self.clipboard,
                "docker_socket": self.docker_socket,
                "environment_access": self.environment_access,
                "host_mount": self.host_mount,
                "language": self.language.value,
                "max_cpu_time_ms": self.max_cpu_time_ms,
                "max_files": self.max_files,
                "max_input_bytes": self.max_input_bytes,
                "max_memory_mib": self.max_memory_mib,
                "max_output_bytes": self.max_output_bytes,
                "max_processes": self.max_processes,
                "max_source_bytes": self.max_source_bytes,
                "max_wall_time_ms": self.max_wall_time_ms,
                "network_connections": self.network_connections,
                "persistent_profile": self.persistent_profile,
                "privileged": self.privileged,
                "revision": self.revision,
                "secret_access": self.secret_access,
            }
        )


@dataclass(frozen=True, slots=True)
class SandboxCandidate:
    source: str = field(repr=False)
    entrypoint: SandboxEntrypoint = SandboxEntrypoint.PYTHON_PURE
    input_data: object = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.entrypoint is not SandboxEntrypoint.PYTHON_PURE:
            raise SandboxContractError("entrypoint is not allowed")
        _safe_text(self.source, MAX_SOURCE_BYTES, python_source=True)
        frozen = _freeze_json(self.input_data, MAX_JSON_BYTES)
        object.__setattr__(self, "input_data", frozen)

    @property
    def canonical_input_bytes(self) -> bytes:
        return json.dumps(
            _thaw_value(self.input_data), ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("ascii")


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    candidate: SandboxCandidate
    scope: SandboxScope
    policy: SandboxPolicy
    backend_identity: str
    backend_generation: int
    session_nonce: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, SandboxCandidate) or not isinstance(self.scope, SandboxScope):
            raise SandboxContractError("request must be typed")
        if not isinstance(self.policy, SandboxPolicy):
            raise SandboxContractError("request policy must be typed")
        _identifier(self.backend_identity)
        _positive_generation(self.backend_generation)
        if not isinstance(self.session_nonce, str) or _NONCE.fullmatch(self.session_nonce) is None:
            raise SandboxContractError("session nonce is invalid")

    @property
    def policy_digest(self) -> str:
        return self.policy.digest

    @property
    def request_digest(self) -> str:
        encoded = json.dumps(
            {
                "backend_generation": self.backend_generation,
                "backend_identity": self.backend_identity,
                "entrypoint": self.candidate.entrypoint.value,
                "input": _thaw_value(self.candidate.input_data),
                "policy_digest": self.policy_digest,
                "scope": dict(self.scope.to_mapping()),
                "session_nonce": self.session_nonce,
                "source": self.candidate.source,
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(b"yonerai.capability_forge.sandbox.request.v1\0" + encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SandboxHandshake:
    scope: SandboxScope
    backend_identity: str
    backend_generation: int
    policy_digest: str
    request_digest: str
    session_nonce: str = field(repr=False)
    network_connections: int
    host_mount: bool
    secret_access: bool
    environment_access: bool
    privileged: bool
    docker_socket: bool
    clipboard: bool
    persistent_profile: bool
    child_processes: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SandboxScope):
            raise SandboxContractError("handshake scope is invalid")
        _identifier(self.backend_identity)
        _positive_generation(self.backend_generation)
        _digest(self.policy_digest)
        _digest(self.request_digest)
        _nonce(self.session_nonce)
        if type(self.network_connections) is not int or self.network_connections != 0:
            raise SandboxContractError("handshake network contract is invalid")
        if type(self.child_processes) is not int or self.child_processes != 0:
            raise SandboxContractError("handshake process contract is invalid")
        if any(
            value is not False
            for value in (
                self.host_mount,
                self.secret_access,
                self.environment_access,
                self.privileged,
                self.docker_socket,
                self.clipboard,
                self.persistent_profile,
            )
        ):
            raise SandboxContractError("handshake containment contract is invalid")


@dataclass(frozen=True, slots=True)
class SandboxArtifactDescriptor:
    opaque_id: str = field(repr=False)
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        _identifier(self.opaque_id)
        if self.media_type not in {"application/json", "text/plain"}:
            raise SandboxContractError("artifact type is invalid")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= MAX_OUTPUT_BYTES
        ):
            raise SandboxContractError("artifact size is invalid")


@dataclass(frozen=True, slots=True)
class SandboxResult:
    scope: SandboxScope
    backend_identity: str
    backend_generation: int
    policy_digest: str
    request_digest: str
    session_nonce: str = field(repr=False)
    output: object = field(repr=False)
    artifacts: tuple[SandboxArtifactDescriptor, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.backend_identity)
        _positive_generation(self.backend_generation)
        if not isinstance(self.scope, SandboxScope):
            raise SandboxContractError("result binding is invalid")
        _digest(self.policy_digest)
        _digest(self.request_digest)
        _nonce(self.session_nonce)
        object.__setattr__(self, "output", _freeze_json(self.output, MAX_OUTPUT_BYTES))
        if not isinstance(self.artifacts, tuple) or len(self.artifacts) > MAX_ARTIFACTS:
            raise SandboxContractError("artifact list is invalid")
        if any(type(item) is not SandboxArtifactDescriptor for item in self.artifacts):
            raise SandboxContractError("artifact list is invalid")


@dataclass(frozen=True, slots=True)
class SandboxTerminationReceipt:
    scope: SandboxScope
    backend_identity: str
    backend_generation: int
    policy_digest: str
    request_digest: str
    session_nonce: str = field(repr=False)
    reason: SandboxTerminationReason
    worker_terminated: bool
    workspace_destroyed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SandboxScope) or not isinstance(self.reason, SandboxTerminationReason):
            raise SandboxContractError("termination receipt is invalid")
        _identifier(self.backend_identity)
        _positive_generation(self.backend_generation)
        _digest(self.policy_digest)
        _digest(self.request_digest)
        _nonce(self.session_nonce)
        if self.worker_terminated is not True or self.workspace_destroyed is not True:
            raise SandboxContractError("termination receipt is unconfirmed")


class ExternalSandboxPort(Protocol):
    async def handshake(self, request: SandboxRequest) -> SandboxHandshake: ...

    async def execute(self, request: SandboxRequest) -> SandboxResult: ...

    async def terminate(
        self, request: SandboxRequest, reason: SandboxTerminationReason
    ) -> SandboxTerminationReceipt: ...


def _identifier(value: object) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise SandboxContractError("identifier is invalid")


def _digest(value: object) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise SandboxContractError("digest is invalid")


def _nonce(value: object) -> None:
    if not isinstance(value, str) or _NONCE.fullmatch(value) is None:
        raise SandboxContractError("session nonce is invalid")


def _positive_id(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SandboxContractError("scope identifier is invalid")


def _positive_generation(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise SandboxContractError("backend generation is invalid")


def _optional_id(value: object) -> None:
    if value is not None:
        _positive_id(value)


def _safe_text(value: object, maximum_bytes: int, *, python_source: bool = False) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(char) < 32 and char not in "\n\t" for char in value)
    ):
        raise SandboxContractError("text is invalid")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise SandboxContractError("text is not UTF-8") from exc
    if len(encoded) > maximum_bytes:
        raise SandboxContractError("text is outside sandbox contract")
    ip_literal_found = _contains_python_data_ip_literal(value) if python_source else _contains_ip_literal(value)
    if (
        _SECRET.search(value)
        or _HOST_PATH.search(value)
        or _PRIVATE_KEY_MARKER.search(value)
        or _NETWORK_LOCATOR.search(value)
        or ip_literal_found
    ):
        raise SandboxContractError("text is outside sandbox contract")


def _contains_ip_literal(value: str) -> bool:
    for candidate in _IP_LITERAL.finditer(value):
        try:
            ipaddress.ip_address(candidate.group(0))
        except ValueError:
            continue
        return True
    return False


def _contains_python_data_ip_literal(value: str) -> bool:
    """Reject IP data while leaving valid Python slice operators alone."""

    try:
        tree = ast.parse(value, mode="exec")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, (bytes, str)):
                continue
            literal = node.value.decode("ascii", "ignore") if isinstance(node.value, bytes) else node.value
            if _contains_ip_literal(literal):
                return True
        tokens = tokenize.generate_tokens(io.StringIO(value).readline)
        return any(token.type == tokenize.COMMENT and _contains_ip_literal(token.string) for token in tokens)
    except (IndentationError, RecursionError, SyntaxError, tokenize.TokenError, ValueError):
        return True


def _freeze_json(value: object, maximum_bytes: int) -> object:
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SandboxContractError("JSON data is invalid") from exc
    if len(encoded) > maximum_bytes:
        raise SandboxContractError("JSON data exceeds its bound")
    _scan_json(decoded)
    return _freeze_value(decoded)


def _scan_json(value: object) -> None:
    if isinstance(value, str):
        _safe_json_text(value, MAX_JSON_BYTES)
    elif isinstance(value, list):
        if len(value) > 64:
            raise SandboxContractError("JSON collection is too large")
        for item in value:
            _scan_json(item)
    elif isinstance(value, dict):
        if len(value) > 64:
            raise SandboxContractError("JSON object is too large")
        for key, item in value.items():
            _safe_json_text(key, 128)
            normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
            if any(fragment in normalized_key for fragment in _HOST_CONTROL_KEY_FRAGMENTS):
                raise SandboxContractError("host-control JSON key is outside sandbox contract")
            if any(fragment in normalized_key for fragment in _SECRET_KEY_FRAGMENTS):
                raise SandboxContractError("secret-bearing JSON key is outside sandbox contract")
            _scan_json(item)
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise SandboxContractError("JSON value is invalid")


def _safe_json_text(value: object, maximum_bytes: int) -> None:
    _safe_text(value, maximum_bytes)
    if any(character in value for character in ("*", "?", "[")):
        raise SandboxContractError("glob-like JSON text is outside sandbox contract")


def _freeze_value(value: object) -> object:
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in sorted(value.items())})
    return value


def _thaw_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    return value


__all__ = [
    "ExternalSandboxPort",
    "SANDBOX_POLICY_REVISION",
    "SandboxArtifactDescriptor",
    "SandboxCandidate",
    "SandboxContractError",
    "SandboxEntrypoint",
    "SandboxHandshake",
    "SandboxPolicy",
    "SandboxRequest",
    "SandboxResult",
    "SandboxScope",
    "SandboxTerminationReceipt",
    "SandboxTerminationReason",
]
