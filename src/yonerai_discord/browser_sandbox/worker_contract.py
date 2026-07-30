from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum

from .models import (
    ALLOWED_BROWSER_ACTION_TYPES,
    BROWSER_ISOLATION_CONTRACT,
    BrowserAction,
    BrowserAdapterContractError,
    BrowserIsolationContract,
    BrowserOutput,
    BrowserOutputKind,
    BrowserPolicyError,
    BrowserSessionRequest,
    Click,
    ExtractText,
    Navigate,
    Scroll,
    SelectOption,
    Screenshot,
    ScreenshotFormat,
    TypeText,
    Wait,
)
from .policy import BrowserSandboxPolicy


BROWSER_WORKER_PROTOCOL_REVISION = "yonerai.browser-worker.v2"
BROWSER_WORKER_SUPPORTED_ACTIONS = (
    "click",
    "extract_text",
    "navigate",
    "screenshot",
    "scroll",
    "select_option",
    "type_text",
    "wait",
)
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
_WORKER_ACTION_TYPES = (Navigate, Click, TypeText, SelectOption, Scroll, Wait, Screenshot, ExtractText)


class BrowserWorkerContractError(BrowserAdapterContractError):
    """Worker protocol data did not match the in-process session contract."""


class BrowserWorkerCleanupError(BrowserWorkerContractError):
    """The worker could not prove termination and profile destruction."""


class BrowserWorkerTerminalReason(StrEnum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CRASH = "crash"
    CONTRACT_ERROR = "contract_error"


@dataclass(frozen=True, slots=True)
class BrowserWorkerHandshakeRequest:
    protocol_revision: str
    request_id: str
    session_id: str
    nonce: str = field(repr=False)
    isolation_contract_digest: str
    policy_snapshot_digest: str
    deadline_milliseconds: int
    max_total_bytes: int
    max_actions: int
    max_network_requests: int
    max_redirects: int

    def __post_init__(self) -> None:
        _require_protocol(self.protocol_revision)
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.session_id, "session_id")
        _require_nonce(self.nonce)
        _require_digest(self.isolation_contract_digest, "isolation_contract_digest")
        _require_digest(self.policy_snapshot_digest, "policy_snapshot_digest")
        _bounded_int("deadline_milliseconds", self.deadline_milliseconds, 1, 300_000)
        _bounded_int("max_total_bytes", self.max_total_bytes, 64 * 1024, 100 * 1024 * 1024)
        _bounded_int("max_actions", self.max_actions, 1, 200)
        _bounded_int("max_network_requests", self.max_network_requests, 1, 2_000)
        _bounded_int("max_redirects", self.max_redirects, 0, 20)


@dataclass(frozen=True, slots=True)
class BrowserWorkerHandshakeResponse:
    protocol_revision: str
    request_id: str
    session_id: str
    nonce: str = field(repr=False)
    isolation_contract_digest: str
    policy_snapshot_digest: str
    supported_actions: tuple[str, ...]
    external_worker_identity: str
    ephemeral_profile: bool
    context_reuse_enabled: bool
    downloads_enabled: bool
    uploads_enabled: bool
    script_evaluation_enabled: bool
    developer_protocol_enabled: bool
    host_mount_enabled: bool
    clipboard_enabled: bool
    credential_import_enabled: bool

    def __post_init__(self) -> None:
        _require_protocol(self.protocol_revision)
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.session_id, "session_id")
        _require_nonce(self.nonce)
        _require_digest(self.isolation_contract_digest, "isolation_contract_digest")
        _require_digest(self.policy_snapshot_digest, "policy_snapshot_digest")
        if not isinstance(self.supported_actions, tuple) or any(
            not isinstance(item, str) for item in self.supported_actions
        ):
            raise TypeError("supported_actions must be a tuple of strings")
        _require_identifier(self.external_worker_identity, "external_worker_identity")
        for name in (
            "ephemeral_profile",
            "context_reuse_enabled",
            "downloads_enabled",
            "uploads_enabled",
            "script_evaluation_enabled",
            "developer_protocol_enabled",
            "host_mount_enabled",
            "clipboard_enabled",
            "credential_import_enabled",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class BrowserWorkerExecuteRequest:
    handshake: BrowserWorkerHandshakeRequest
    external_worker_identity: str
    actions: tuple[BrowserAction, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.handshake, BrowserWorkerHandshakeRequest):
            raise TypeError("handshake must be a BrowserWorkerHandshakeRequest")
        _require_identifier(self.external_worker_identity, "external_worker_identity")
        actions = tuple(self.actions)
        if not actions or len(actions) > self.handshake.max_actions:
            raise BrowserWorkerContractError("worker action count is outside the negotiated limit")
        if any(type(action) not in _WORKER_ACTION_TYPES for action in actions):
            raise BrowserWorkerContractError("worker action is not allowed")
        object.__setattr__(self, "actions", actions)


@dataclass(frozen=True, slots=True)
class BrowserWorkerExecutionResult:
    request_id: str
    session_id: str
    nonce: str = field(repr=False)
    isolation_contract_digest: str
    policy_snapshot_digest: str
    external_worker_identity: str
    outputs: tuple[BrowserOutput, ...] = field(repr=False)
    completed_actions: int
    network_request_count: int
    redirect_count: int
    consumed_bytes: int

    def __post_init__(self) -> None:
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.session_id, "session_id")
        _require_nonce(self.nonce)
        _require_digest(self.isolation_contract_digest, "isolation_contract_digest")
        _require_digest(self.policy_snapshot_digest, "policy_snapshot_digest")
        _require_identifier(self.external_worker_identity, "external_worker_identity")
        outputs = tuple(self.outputs)
        if any(type(output) is not BrowserOutput for output in outputs):
            raise TypeError("outputs must contain exact BrowserOutput values")
        object.__setattr__(self, "outputs", outputs)
        for name in ("completed_actions", "network_request_count", "redirect_count", "consumed_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class BrowserWorkerTerminateRequest:
    protocol_revision: str
    request_id: str
    session_id: str
    nonce: str = field(repr=False)
    isolation_contract_digest: str
    policy_snapshot_digest: str
    external_worker_identity: str
    reason: BrowserWorkerTerminalReason

    def __post_init__(self) -> None:
        _require_protocol(self.protocol_revision)
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.session_id, "session_id")
        _require_nonce(self.nonce)
        _require_digest(self.isolation_contract_digest, "isolation_contract_digest")
        _require_digest(self.policy_snapshot_digest, "policy_snapshot_digest")
        _require_identifier(self.external_worker_identity, "external_worker_identity")
        object.__setattr__(self, "reason", BrowserWorkerTerminalReason(self.reason))


@dataclass(frozen=True, slots=True)
class BrowserWorkerTerminationReceipt:
    protocol_revision: str
    request_id: str
    session_id: str
    nonce: str = field(repr=False)
    isolation_contract_digest: str
    policy_snapshot_digest: str
    external_worker_identity: str
    reason: BrowserWorkerTerminalReason
    worker_terminated: bool
    profile_destroyed: bool
    terminal_sequence: int = 1

    def __post_init__(self) -> None:
        _require_protocol(self.protocol_revision)
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.session_id, "session_id")
        _require_nonce(self.nonce)
        _require_digest(self.isolation_contract_digest, "isolation_contract_digest")
        _require_digest(self.policy_snapshot_digest, "policy_snapshot_digest")
        _require_identifier(self.external_worker_identity, "external_worker_identity")
        object.__setattr__(self, "reason", BrowserWorkerTerminalReason(self.reason))
        if type(self.worker_terminated) is not bool or type(self.profile_destroyed) is not bool:
            raise TypeError("termination flags must be booleans")
        if isinstance(self.terminal_sequence, bool) or not isinstance(self.terminal_sequence, int):
            raise TypeError("terminal_sequence must be an integer")


def browser_isolation_contract_digest(
    contract: BrowserIsolationContract = BROWSER_ISOLATION_CONTRACT,
) -> str:
    if not isinstance(contract, BrowserIsolationContract):
        raise TypeError("contract must be a BrowserIsolationContract")
    return _canonical_digest(
        {
            "context_reuse_enabled": contract.context_reuse_enabled,
            "developer_protocol_enabled": contract.developer_protocol_enabled,
            "downloads_enabled": contract.downloads_enabled,
            "profile_mode": contract.profile_mode,
            "script_evaluation_enabled": contract.script_evaluation_enabled,
            "uploads_enabled": contract.uploads_enabled,
        }
    )


def browser_policy_snapshot_digest(policy: BrowserSandboxPolicy) -> str:
    if not isinstance(policy, BrowserSandboxPolicy):
        raise TypeError("policy must be a BrowserSandboxPolicy")
    limits = policy.limits
    return _canonical_digest(
        {
            "allowed_domains": sorted(policy.allowed_domains),
            "allowed_ports": {scheme: sorted(policy.allowed_ports[scheme]) for scheme in sorted(policy.allowed_ports)},
            "denied_domains": sorted(policy.denied_domains),
            "limits": {
                "max_duration_seconds": float(limits.max_duration_seconds),
                "max_network_requests": limits.max_network_requests,
                "max_redirects": limits.max_redirects,
                "max_steps": limits.max_steps,
                "max_total_bytes": limits.max_total_bytes,
                "max_total_wait_milliseconds": limits.max_total_wait_milliseconds,
            },
        }
    )


def require_stage2_worker_request(request: BrowserSessionRequest) -> None:
    if not isinstance(request, BrowserSessionRequest):
        raise TypeError("request must be a BrowserSessionRequest")
    if any(type(action) not in ALLOWED_BROWSER_ACTION_TYPES for action in request.actions):
        raise BrowserPolicyError("worker request contains an unsupported action")


def validate_worker_handshake(
    request: BrowserWorkerHandshakeRequest,
    response: BrowserWorkerHandshakeResponse,
) -> None:
    if type(response) is not BrowserWorkerHandshakeResponse:
        raise BrowserWorkerContractError("worker returned an invalid handshake")
    if (
        response.protocol_revision != request.protocol_revision
        or response.request_id != request.request_id
        or response.session_id != request.session_id
        or response.nonce != request.nonce
        or response.isolation_contract_digest != request.isolation_contract_digest
        or response.policy_snapshot_digest != request.policy_snapshot_digest
        or response.supported_actions != BROWSER_WORKER_SUPPORTED_ACTIONS
        or response.ephemeral_profile is not True
        or response.context_reuse_enabled is not False
        or response.downloads_enabled is not False
        or response.uploads_enabled is not False
        or response.script_evaluation_enabled is not False
        or response.developer_protocol_enabled is not False
        or response.host_mount_enabled is not False
        or response.clipboard_enabled is not False
        or response.credential_import_enabled is not False
    ):
        raise BrowserWorkerContractError("worker handshake did not match the requested isolation contract")


def validate_worker_result(
    request: BrowserWorkerExecuteRequest,
    result: BrowserWorkerExecutionResult,
) -> None:
    if type(result) is not BrowserWorkerExecutionResult:
        raise BrowserWorkerContractError("worker returned an invalid execution result")
    handshake = request.handshake
    if (
        result.request_id != handshake.request_id
        or result.session_id != handshake.session_id
        or result.nonce != handshake.nonce
        or result.isolation_contract_digest != handshake.isolation_contract_digest
        or result.policy_snapshot_digest != handshake.policy_snapshot_digest
        or result.external_worker_identity != request.external_worker_identity
    ):
        raise BrowserWorkerContractError("worker result binding did not match the session")
    if (
        result.completed_actions != len(request.actions)
        or result.network_request_count > handshake.max_network_requests
        or result.redirect_count > handshake.max_redirects
        or result.redirect_count > result.network_request_count
        or result.consumed_bytes > handshake.max_total_bytes
        or sum(output.byte_length for output in result.outputs) > result.consumed_bytes
    ):
        raise BrowserWorkerContractError("worker result exceeded the negotiated counters")

    expected: dict[int, tuple[BrowserOutputKind, str]] = {}
    for index, action in enumerate(request.actions):
        if isinstance(action, Screenshot):
            media_type = "image/png" if action.image_format is ScreenshotFormat.PNG else "image/jpeg"
            expected[index] = (BrowserOutputKind.SCREENSHOT, media_type)
        elif isinstance(action, ExtractText):
            expected[index] = (BrowserOutputKind.TEXT, "text/plain; charset=utf-8")

    actual: dict[int, tuple[BrowserOutputKind, str]] = {}
    actual_order: list[int] = []
    for output in result.outputs:
        if output.step_index in actual:
            raise BrowserWorkerContractError("worker returned duplicate output for one action")
        actual_order.append(output.step_index)
        actual[output.step_index] = (output.kind, output.media_type)
    if actual != expected or actual_order != list(expected):
        raise BrowserWorkerContractError("worker outputs did not match the requested actions")


def validate_worker_termination(
    request: BrowserWorkerTerminateRequest,
    receipt: BrowserWorkerTerminationReceipt,
) -> None:
    if type(receipt) is not BrowserWorkerTerminationReceipt:
        raise BrowserWorkerCleanupError("worker termination receipt was missing")
    if (
        receipt.protocol_revision != request.protocol_revision
        or receipt.request_id != request.request_id
        or receipt.session_id != request.session_id
        or receipt.nonce != request.nonce
        or receipt.isolation_contract_digest != request.isolation_contract_digest
        or receipt.policy_snapshot_digest != request.policy_snapshot_digest
        or receipt.external_worker_identity != request.external_worker_identity
        or receipt.reason is not request.reason
        or receipt.worker_terminated is not True
        or receipt.profile_destroyed is not True
        or receipt.terminal_sequence != 1
    ):
        raise BrowserWorkerCleanupError("worker termination receipt did not match the session")


def _canonical_digest(value: object) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _require_protocol(value: object) -> None:
    if value != BROWSER_WORKER_PROTOCOL_REVISION:
        raise ValueError("unsupported browser worker protocol revision")


def _require_identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase ASCII identifier")


def _require_nonce(value: object) -> None:
    if not isinstance(value, str) or _NONCE.fullmatch(value) is None:
        raise ValueError("nonce must be a bounded base64url token")


def _require_digest(value: object, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be a SHA-256 digest")


def _bounded_int(label: str, value: object, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} is outside the allowed range")


__all__ = [
    "BROWSER_WORKER_PROTOCOL_REVISION",
    "BROWSER_WORKER_SUPPORTED_ACTIONS",
    "BrowserWorkerCleanupError",
    "BrowserWorkerContractError",
    "BrowserWorkerExecuteRequest",
    "BrowserWorkerExecutionResult",
    "BrowserWorkerHandshakeRequest",
    "BrowserWorkerHandshakeResponse",
    "BrowserWorkerTerminalReason",
    "BrowserWorkerTerminateRequest",
    "BrowserWorkerTerminationReceipt",
    "browser_isolation_contract_digest",
    "browser_policy_snapshot_digest",
    "require_stage2_worker_request",
    "validate_worker_handshake",
    "validate_worker_result",
    "validate_worker_termination",
]
