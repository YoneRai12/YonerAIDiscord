"""Fail-closed adapter for a disposable YonerAI Hyper-V execution VM.

Discord is only one possible caller. Surface identifiers stay inside the
adapter and are projected to the fixed broker as a domain-separated digest.
The BOT never receives a VM name, address, host path, or privileged command.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from .sandbox_contract import (
    ExternalSandboxPort,
    SandboxCandidate,
    SandboxHandshake,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
    SandboxScope,
    SandboxTerminationReason,
    SandboxTerminationReceipt,
)
from .sandbox_service import ExternalSandboxService


HYPERV_DISPOSABLE_BACKEND_ID = "hyperv-disposable"
HYPERV_DISPOSABLE_PIPE_NAME = r"\\.\pipe\YonerAI-ForgeSandbox-Broker-v1"
HYPERV_BROKER_REQUEST_SCHEMA = "yonerai.exec-sandbox.broker-run.v1"
HYPERV_BROKER_RECEIPT_SCHEMA = "yonerai.exec-sandbox.broker-receipt.v1"
HYPERV_JOB_SCHEMA = "yonerai.exec-sandbox.job.v1"
HYPERV_BROKER_PROTOCOL_REVISION = "2026-08-08.1"
_TRUSTED_DATA_CHANNEL_IMPLEMENTED = False

_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_LOCATOR = re.compile(
    r"(?i)(?:\b(?:https?|ftp|file)://|\\\\|\\device\\|[a-z]:[\\/]|(?:^|[\s\"'=:(])/[a-z0-9._~-]+(?:/|$))"
)
_IPV4 = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
_IPV6 = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{0,4}:){2,}[0-9a-f]{0,4}(?![0-9a-f])")
_SECRET = re.compile(
    r"(?i)(?:\b(?:basic|bearer)\s+\S+|\b(?:api[_-]?key|authorization|cookie|password|secret|token)\s*[:=])"
)


class HyperVDisposableBackendError(RuntimeError):
    """A content-free failure at the fixed broker boundary."""

    def __init__(self) -> None:
        super().__init__("Hyper-V disposable backend failed safely")


class HyperVJobState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class HyperVFailureCode(StrEnum):
    NONE = "none"
    UNAVAILABLE = "unavailable"
    POLICY_DENIED = "policy_denied"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    EXECUTION_FAILED = "execution_failed"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"


@dataclass(frozen=True, slots=True)
class HyperVDisposableBinding:
    """Internal binding; raw surface IDs are never serialized to the broker."""

    scope: SandboxScope = field(repr=False)
    backend_generation: int
    policy_digest: str
    request_digest: str
    session_nonce: str = field(repr=False)
    resource_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SandboxScope):
            raise TypeError("binding scope must be typed")
        if type(self.backend_generation) is not int or self.backend_generation <= 0:
            raise ValueError("backend generation is invalid")
        for value in (self.policy_digest, self.request_digest, self.resource_token):
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise ValueError("binding digest is invalid")
        if not isinstance(self.session_nonce, str) or re.fullmatch(r"[a-f0-9]{32}", self.session_nonce) is None:
            raise ValueError("binding nonce is invalid")

    @property
    def scope_digest(self) -> str:
        canonical = json.dumps(
            dict(self.scope.to_mapping()),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(
            b"yonerai.execution-sandbox.scope.v1\0" + self.session_nonce.encode("ascii") + b"\0" + canonical
        ).hexdigest()

    def to_wire_mapping(self) -> Mapping[str, object]:
        """Return only opaque binding facts accepted by the fixed broker."""
        return MappingProxyType(
            {
                "backend_generation": self.backend_generation,
                "policy_digest": self.policy_digest,
                "request_digest": self.request_digest,
                "resource_token": self.resource_token,
                "scope_digest": self.scope_digest,
                "session_nonce": self.session_nonce,
            }
        )


@dataclass(frozen=True, slots=True)
class HyperVNetworkEvidence:
    network_prestart: int
    network_running: int
    network_pre_result: int
    network_post_stop: int

    def __post_init__(self) -> None:
        for value in (
            self.network_prestart,
            self.network_running,
            self.network_pre_result,
            self.network_post_stop,
        ):
            if type(value) is not int or value != 0:
                raise ValueError("network evidence is invalid")


@dataclass(frozen=True, slots=True)
class HyperVCleanupReceipt:
    binding: HyperVDisposableBinding
    network: HyperVNetworkEvidence
    vm_stopped: bool
    vm_removed: bool
    workspace_destroyed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.binding, HyperVDisposableBinding) or not isinstance(self.network, HyperVNetworkEvidence):
            raise TypeError("cleanup receipt is invalid")


@dataclass(frozen=True, slots=True)
class HyperVBrokerJobRequest:
    binding: HyperVDisposableBinding
    candidate: SandboxCandidate = field(repr=False)
    policy: SandboxPolicy

    def __post_init__(self) -> None:
        if (
            not isinstance(self.binding, HyperVDisposableBinding)
            or type(self.candidate) is not SandboxCandidate
            or type(self.policy) is not SandboxPolicy
        ):
            raise TypeError("broker request must be typed")
        _require_offline_policy(self.policy)
        if self.binding.policy_digest != self.policy.digest:
            raise ValueError("broker policy binding is invalid")

    @property
    def canonical_frame(self) -> bytes:
        nonce = hashlib.sha256(self.binding.session_nonce.encode("ascii")).hexdigest()
        value = {
            "job": {
                "binding": {
                    "policy_digest": f"sha256:{self.binding.policy_digest}",
                    "request_digest": f"sha256:{self.binding.request_digest}",
                    "scope_digest": f"sha256:{self.binding.scope_digest}",
                },
                "entrypoint": "python3",
                "generation": self.binding.backend_generation,
                "input": json.loads(self.candidate.canonical_input_bytes),
                "job_id": f"job_{self.binding.resource_token[:32]}",
                "language": "python",
                "limits": {
                    "artifact_bytes": 0,
                    "artifact_count": 0,
                    "cpu_count": 1,
                    "memory_mib": 512,
                    "wall_seconds": 60,
                },
                "nonce": nonce,
                "schema": HYPERV_JOB_SCHEMA,
                "source": self.candidate.source,
            },
            "protocol_revision": HYPERV_BROKER_PROTOCOL_REVISION,
            "schema": HYPERV_BROKER_REQUEST_SCHEMA,
        }
        body = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if not body or len(body) > 131_072:
            raise HyperVDisposableBackendError()
        return len(body).to_bytes(4, "big") + body


@dataclass(frozen=True, slots=True)
class HyperVBrokerJobReceipt:
    binding: HyperVDisposableBinding
    state: HyperVJobState
    failure_code: HyperVFailureCode
    output: object = field(repr=False)
    output_sha256: str
    output_count: int
    artifacts: tuple[()] = ()
    cleanup: HyperVCleanupReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.binding, HyperVDisposableBinding):
            raise TypeError("broker receipt binding is invalid")
        if not isinstance(self.state, HyperVJobState) or not isinstance(self.failure_code, HyperVFailureCode):
            raise TypeError("broker receipt state is invalid")
        if not isinstance(self.output_sha256, str) or _DIGEST.fullmatch(self.output_sha256) is None:
            raise ValueError("broker output digest is invalid")
        if type(self.output_count) is not int or not 0 <= self.output_count <= 64:
            raise ValueError("broker output count is invalid")
        if self.artifacts != ():
            raise ValueError("Stage 1 artifacts must be empty")
        if not isinstance(self.cleanup, HyperVCleanupReceipt):
            raise TypeError("broker cleanup receipt is invalid")


class HyperVDisposableBrokerPort(Protocol):
    """Fixed low-privilege broker client; no command/path/address arguments."""

    @property
    def ready(self) -> bool: ...

    async def run(self, request: HyperVBrokerJobRequest) -> HyperVBrokerJobReceipt: ...

    async def cancel(
        self,
        binding: HyperVDisposableBinding,
        reason: SandboxTerminationReason,
    ) -> HyperVCleanupReceipt: ...


class FixedNamedPipeExchangePort(Protocol):
    """Bounded exchange with the single code-owned pipe endpoint."""

    @property
    def ready(self) -> bool: ...

    @property
    def pipe_name(self) -> str: ...

    async def exchange(self, frame: bytes) -> bytes: ...


class HyperVDisposableNamedPipeBrokerPort:
    """Strict broker wire adapter.

    The actual pipe exchange stays unconfigured until a trusted, cancellable
    data channel is injected. This class never starts PowerShell or accepts a
    pipe name from callers.
    """

    def __init__(
        self,
        *,
        exchange: FixedNamedPipeExchangePort | None,
        exchange_identity_current: Callable[[], object | None],
    ) -> None:
        self._exchange = exchange
        self._exchange_identity_current = exchange_identity_current

    @property
    def ready(self) -> bool:
        # VERSION.lock deliberately keeps the trusted output/cancel channel
        # unconfigured in Stage 1. An injected object must not override truth.
        try:
            return (
                _TRUSTED_DATA_CHANNEL_IMPLEMENTED
                and self._exchange is not None
                and self._exchange_identity_current() is self._exchange
                and self._exchange.pipe_name == HYPERV_DISPOSABLE_PIPE_NAME
                and self._exchange.ready is True
            )
        except Exception:
            return False

    async def run(self, request: HyperVBrokerJobRequest) -> HyperVBrokerJobReceipt:
        if not self.ready or self._exchange is None:
            raise HyperVDisposableBackendError()
        try:
            raw = await self._exchange.exchange(request.canonical_frame)
            return _decode_broker_receipt(raw, request.binding)
        except asyncio.CancelledError:
            raise
        except HyperVDisposableBackendError:
            raise
        except Exception:
            raise HyperVDisposableBackendError() from None

    async def cancel(
        self,
        binding: HyperVDisposableBinding,
        reason: SandboxTerminationReason,
    ) -> HyperVCleanupReceipt:
        # The Stage 1 pipe contract has no independently authenticated cancel
        # channel. Cancellation therefore remains unconfigured and fail-closed.
        del binding, reason
        raise HyperVDisposableBackendError()


class HyperVDisposableSandboxPort(ExternalSandboxPort):
    """Adapt one typed candidate to one fully destroyed VM job."""

    def __init__(
        self,
        *,
        broker: HyperVDisposableBrokerPort,
        broker_identity_current: Callable[[], object | None],
    ) -> None:
        self._broker = broker
        self._broker_identity_current = broker_identity_current
        self._active_binding: HyperVDisposableBinding | None = None
        self._cleanup: HyperVCleanupReceipt | None = None
        self._closing = False

    @property
    def containment_current(self) -> bool:
        if self._closing:
            return False
        try:
            return self._broker_identity_current() is self._broker and self._broker.ready is True
        except Exception:
            return False

    def begin_close(self) -> None:
        self._closing = True

    async def handshake(self, request: SandboxRequest) -> SandboxHandshake:
        binding = _binding_from_request(request)
        if not self.containment_current or self._active_binding is not None:
            raise HyperVDisposableBackendError()
        self._active_binding = binding
        self._cleanup = None
        return SandboxHandshake(
            scope=request.scope,
            backend_identity=request.backend_identity,
            backend_generation=request.backend_generation,
            policy_digest=request.policy_digest,
            request_digest=request.request_digest,
            session_nonce=request.session_nonce,
            network_connections=0,
            host_mount=False,
            secret_access=False,
            environment_access=False,
            privileged=False,
            docker_socket=False,
            clipboard=False,
            persistent_profile=False,
            child_processes=0,
        )

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        binding = _binding_from_request(request)
        if not self.containment_current or self._active_binding != binding:
            raise HyperVDisposableBackendError()
        try:
            receipt = await self._broker.run(
                HyperVBrokerJobRequest(binding=binding, candidate=request.candidate, policy=request.policy)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise HyperVDisposableBackendError() from None
        if receipt.binding == binding and isinstance(receipt.cleanup, HyperVCleanupReceipt):
            self._cleanup = receipt.cleanup
        if not _valid_job_receipt(binding, request, receipt):
            raise HyperVDisposableBackendError()
        return SandboxResult(
            scope=request.scope,
            backend_identity=request.backend_identity,
            backend_generation=request.backend_generation,
            policy_digest=request.policy_digest,
            request_digest=request.request_digest,
            session_nonce=request.session_nonce,
            output=receipt.output,
            artifacts=(),
        )

    async def terminate(
        self,
        request: SandboxRequest,
        reason: SandboxTerminationReason,
    ) -> SandboxTerminationReceipt:
        binding = _binding_from_request(request)
        cleanup = self._cleanup if self._cleanup is not None and self._cleanup.binding == binding else None
        if cleanup is None:
            try:
                cleanup = await self._broker.cancel(binding, reason)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise HyperVDisposableBackendError() from None
        if not _valid_cleanup(binding, cleanup):
            raise HyperVDisposableBackendError()
        self._active_binding = None
        self._cleanup = None
        return SandboxTerminationReceipt(
            scope=request.scope,
            backend_identity=request.backend_identity,
            backend_generation=request.backend_generation,
            policy_digest=request.policy_digest,
            request_digest=request.request_digest,
            session_nonce=request.session_nonce,
            reason=reason,
            worker_terminated=True,
            workspace_destroyed=True,
        )


def build_hyperv_disposable_sandbox_service(
    *,
    broker: HyperVDisposableBrokerPort,
    broker_identity_current: Callable[[], object | None],
    policy: SandboxPolicy | None = None,
    backend_generation: int = 1,
) -> tuple[ExternalSandboxService, HyperVDisposableSandboxPort]:
    """Compose the existing Stage 3a lifecycle with a 60-second cleanup bound."""
    port = HyperVDisposableSandboxPort(
        broker=broker,
        broker_identity_current=broker_identity_current,
    )
    service = ExternalSandboxService(
        port=port,
        containment_current=lambda: port.containment_current,
        policy=policy,
        cleanup_timeout_seconds=60,
        backend_generation=backend_generation,
    )
    return service, port


def _binding_from_request(request: SandboxRequest) -> HyperVDisposableBinding:
    if type(request) is not SandboxRequest or type(request.candidate) is not SandboxCandidate:
        raise HyperVDisposableBackendError()
    if request.backend_identity != HYPERV_DISPOSABLE_BACKEND_ID:
        raise HyperVDisposableBackendError()
    _require_offline_policy(request.policy)
    resource_token = hashlib.sha256(
        b"yonerai.execution-sandbox.resource.v1\0"
        + request.request_digest.encode("ascii")
        + request.session_nonce.encode("ascii")
    ).hexdigest()
    try:
        return HyperVDisposableBinding(
            scope=request.scope,
            backend_generation=request.backend_generation,
            policy_digest=request.policy_digest,
            request_digest=request.request_digest,
            session_nonce=request.session_nonce,
            resource_token=resource_token,
        )
    except Exception:
        raise HyperVDisposableBackendError() from None


def _require_offline_policy(policy: SandboxPolicy) -> None:
    expected = SandboxPolicy()
    if (
        type(policy) is not SandboxPolicy
        or dict(policy.to_mapping()) != dict(expected.to_mapping())
        or type(policy.network_connections) is not int
        or policy.network_connections != 0
        or type(policy.max_processes) is not int
        or policy.max_processes != 0
        or any(
            value is not False
            for value in (
                policy.host_mount,
                policy.secret_access,
                policy.environment_access,
                policy.privileged,
                policy.docker_socket,
                policy.clipboard,
                policy.persistent_profile,
            )
        )
    ):
        raise HyperVDisposableBackendError()


def _valid_job_receipt(
    binding: HyperVDisposableBinding,
    request: SandboxRequest,
    receipt: object,
) -> bool:
    if (
        type(receipt) is not HyperVBrokerJobReceipt
        or receipt.binding != binding
        or receipt.state is not HyperVJobState.COMPLETED
        or receipt.failure_code is not HyperVFailureCode.NONE
        or receipt.artifacts != ()
        or not _valid_cleanup(binding, receipt.cleanup)
    ):
        return False
    try:
        encoded = json.dumps(
            receipt.output,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError):
        return False
    if hashlib.sha256(encoded).hexdigest() != receipt.output_sha256:
        return False
    if receipt.output_count != _output_count(receipt.output):
        return False
    if _contains_source_echo(receipt.output, request.candidate.source):
        return False
    return not _contains_forbidden_public_text(receipt.output)


def _valid_cleanup(binding: HyperVDisposableBinding, receipt: object) -> bool:
    return (
        type(receipt) is HyperVCleanupReceipt
        and receipt.binding == binding
        and isinstance(receipt.network, HyperVNetworkEvidence)
        and receipt.network.network_prestart == 0
        and receipt.network.network_running == 0
        and receipt.network.network_pre_result == 0
        and receipt.network.network_post_stop == 0
        and receipt.vm_stopped is True
        and receipt.vm_removed is True
        and receipt.workspace_destroyed is True
    )


def _contains_forbidden_public_text(value: object) -> bool:
    if isinstance(value, str):
        return bool(
            _LOCATOR.search(value)
            or _contains_ipv4_literal(value)
            or _contains_ipv6_literal(value)
            or _SECRET.search(value)
        )
    if isinstance(value, Mapping):
        return any(
            _contains_forbidden_public_text(key) or _contains_forbidden_public_text(item) for key, item in value.items()
        )
    if isinstance(value, (tuple, list)):
        return any(_contains_forbidden_public_text(item) for item in value)
    return False


def _contains_ipv4_literal(value: str) -> bool:
    for candidate in _IPV4.finditer(value):
        try:
            address = ipaddress.ip_address(candidate.group(0))
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address):
            return True
    return False


def _contains_ipv6_literal(value: str) -> bool:
    for candidate in _IPV6.finditer(value):
        try:
            address = ipaddress.ip_address(candidate.group(0))
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv6Address):
            return True
    return False


def _contains_source_echo(value: object, source: str) -> bool:
    if isinstance(value, str):
        return source in value
    if isinstance(value, Mapping):
        return any(
            _contains_source_echo(key, source) or _contains_source_echo(item, source) for key, item in value.items()
        )
    if isinstance(value, (tuple, list)):
        return any(_contains_source_echo(item, source) for item in value)
    return False


def _output_count(value: object) -> int:
    if isinstance(value, Mapping):
        return len(value)
    if isinstance(value, (tuple, list)):
        return len(value)
    return 0 if value is None else 1


def _decode_broker_receipt(raw: bytes, binding: HyperVDisposableBinding) -> HyperVBrokerJobReceipt:
    value = _decode_frame(raw)
    if set(value) != {
        "artifacts",
        "cleanup",
        "failure_code",
        "generation",
        "job_id",
        "nonce_digest",
        "output_count",
        "output_sha256",
        "policy_digest",
        "protocol_revision",
        "request_digest",
        "schema",
        "scope_digest",
        "state",
    }:
        raise HyperVDisposableBackendError()
    expected_job_id = f"job_{binding.resource_token[:32]}"
    expected_nonce_digest = (
        "sha256:"
        + hashlib.sha256(hashlib.sha256(binding.session_nonce.encode("ascii")).hexdigest().encode("ascii")).hexdigest()
    )
    expected_output_digest = "sha256:" + hashlib.sha256(b"null").hexdigest()
    if (
        value["schema"] != HYPERV_BROKER_RECEIPT_SCHEMA
        or value["protocol_revision"] != HYPERV_BROKER_PROTOCOL_REVISION
        or value["policy_digest"] != f"sha256:{binding.policy_digest}"
        or value["scope_digest"] != f"sha256:{binding.scope_digest}"
        or value["request_digest"] != f"sha256:{binding.request_digest}"
        or value["generation"] != binding.backend_generation
        or value["nonce_digest"] != expected_nonce_digest
        or value["job_id"] != expected_job_id
        or value["artifacts"] != []
        or value["output_count"] != 0
        or value["output_sha256"] != expected_output_digest
    ):
        raise HyperVDisposableBackendError()
    cleanup = value["cleanup"]
    if not isinstance(cleanup, dict) or set(cleanup) != {
        "network_post_stop",
        "network_pre_result",
        "network_prestart",
        "network_running",
        "vm_removed",
        "vm_stopped",
        "workspace_destroyed",
    }:
        raise HyperVDisposableBackendError()
    try:
        return HyperVBrokerJobReceipt(
            binding=binding,
            state=HyperVJobState(value["state"]),
            failure_code=HyperVFailureCode(value["failure_code"]),
            output=None,
            output_sha256=value["output_sha256"].removeprefix("sha256:"),
            output_count=value["output_count"],
            cleanup=HyperVCleanupReceipt(
                binding=binding,
                network=HyperVNetworkEvidence(
                    network_prestart=cleanup["network_prestart"],
                    network_running=cleanup["network_running"],
                    network_pre_result=cleanup["network_pre_result"],
                    network_post_stop=cleanup["network_post_stop"],
                ),
                vm_stopped=cleanup["vm_stopped"],
                vm_removed=cleanup["vm_removed"],
                workspace_destroyed=cleanup["workspace_destroyed"],
            ),
        )
    except Exception:
        raise HyperVDisposableBackendError() from None


def _decode_frame(raw: bytes) -> Mapping[str, object]:
    if not isinstance(raw, bytes) or len(raw) < 5 or len(raw) > 196_612:
        raise HyperVDisposableBackendError()
    length = int.from_bytes(raw[:4], "big")
    body = raw[4:]
    if length != len(body) or not body:
        raise HyperVDisposableBackendError()
    try:
        decoded = json.loads(
            body.decode("utf-8", "strict"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise HyperVDisposableBackendError() from None
    if not isinstance(decoded, dict):
        raise HyperVDisposableBackendError()
    return decoded


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


__all__ = [
    "HYPERV_BROKER_PROTOCOL_REVISION",
    "HYPERV_BROKER_RECEIPT_SCHEMA",
    "HYPERV_BROKER_REQUEST_SCHEMA",
    "HYPERV_DISPOSABLE_BACKEND_ID",
    "HYPERV_DISPOSABLE_PIPE_NAME",
    "HYPERV_JOB_SCHEMA",
    "FixedNamedPipeExchangePort",
    "HyperVBrokerJobReceipt",
    "HyperVBrokerJobRequest",
    "HyperVCleanupReceipt",
    "HyperVDisposableBackendError",
    "HyperVDisposableBinding",
    "HyperVDisposableBrokerPort",
    "HyperVDisposableNamedPipeBrokerPort",
    "HyperVDisposableSandboxPort",
    "HyperVFailureCode",
    "HyperVJobState",
    "HyperVNetworkEvidence",
    "build_hyperv_disposable_sandbox_service",
]
