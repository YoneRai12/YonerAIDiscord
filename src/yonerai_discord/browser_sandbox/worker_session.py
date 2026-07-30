from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
from collections import deque
from enum import StrEnum
from typing import Callable, Protocol

from .models import BrowserSessionRequest, BrowserSessionResult
from .policy import BrowserSandboxPolicy
from .worker_contract import (
    BROWSER_WORKER_PROTOCOL_REVISION,
    BrowserWorkerCleanupError,
    BrowserWorkerContractError,
    BrowserWorkerExecuteRequest,
    BrowserWorkerExecutionResult,
    BrowserWorkerHandshakeRequest,
    BrowserWorkerHandshakeResponse,
    BrowserWorkerTerminalReason,
    BrowserWorkerTerminateRequest,
    BrowserWorkerTerminationReceipt,
    browser_isolation_contract_digest,
    browser_policy_snapshot_digest,
    require_stage2_worker_request,
    validate_worker_handshake,
    validate_worker_result,
    validate_worker_termination,
)


class BrowserWorkerTransport(Protocol):
    """Test seam for a future external worker transport; no production implementation is wired."""

    async def handshake(self, request: BrowserWorkerHandshakeRequest) -> BrowserWorkerHandshakeResponse: ...

    async def execute(self, request: BrowserWorkerExecuteRequest) -> BrowserWorkerExecutionResult: ...

    async def close(self, request: BrowserWorkerTerminateRequest) -> BrowserWorkerTerminationReceipt: ...

    async def terminate(self, request: BrowserWorkerTerminateRequest) -> BrowserWorkerTerminationReceipt: ...


class BrowserWorkerSessionState(StrEnum):
    NEW = "new"
    HANDSHAKING = "handshaking"
    EXECUTING = "executing"
    CLOSING = "closing"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


_NONCE_LOCK = threading.Lock()
_RECENT_NONCES: deque[str] = deque()
_RECENT_NONCE_SET: set[str] = set()
_MAX_RECENT_NONCES = 4096


class BrowserWorkerSession:
    """One-shot in-process protocol verifier for a future isolated worker."""

    def __init__(
        self,
        *,
        request_id: str,
        policy: BrowserSandboxPolicy,
        transport: BrowserWorkerTransport,
        nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
        execution_timeout_seconds: float | None = None,
        cleanup_timeout_seconds: float = 1.0,
    ) -> None:
        if not isinstance(policy, BrowserSandboxPolicy):
            raise TypeError("policy must be a BrowserSandboxPolicy")
        for method in ("handshake", "execute", "close", "terminate"):
            if not callable(getattr(transport, method, None)):
                raise TypeError("transport does not implement the browser worker protocol")
        if not callable(nonce_factory):
            raise TypeError("nonce_factory must be callable")
        timeout = (
            float(policy.limits.max_duration_seconds)
            if execution_timeout_seconds is None
            else _bounded_timeout(
                execution_timeout_seconds,
                label="execution_timeout_seconds",
                maximum=float(policy.limits.max_duration_seconds),
            )
        )
        cleanup_timeout = _bounded_timeout(
            cleanup_timeout_seconds,
            label="cleanup_timeout_seconds",
            maximum=5.0,
        )
        self._request_id = request_id
        self._policy = policy
        self._transport = transport
        self._nonce_factory = nonce_factory
        self._timeout = timeout
        self._cleanup_timeout = cleanup_timeout
        self._state = BrowserWorkerSessionState.NEW
        self._handshake: BrowserWorkerHandshakeRequest | None = None
        self._worker_identity: str | None = None
        self._terminate_called = False
        self._cleanup_confirmed = False

    @property
    def state(self) -> BrowserWorkerSessionState:
        return self._state

    @property
    def cleanup_confirmed(self) -> bool:
        return self._cleanup_confirmed

    async def execute(self, request: BrowserSessionRequest) -> BrowserSessionResult:
        if self._state is not BrowserWorkerSessionState.NEW:
            raise BrowserWorkerContractError("browser worker session is one-shot")
        require_stage2_worker_request(request)
        self._policy.validate_session(request)
        handshake = self._issue_handshake()
        self._handshake = handshake
        self._state = BrowserWorkerSessionState.HANDSHAKING
        try:
            return await asyncio.wait_for(
                self._execute_once(handshake, request),
                timeout=self._timeout,
            )
        except asyncio.CancelledError:
            await self._cleanup_after_failure(BrowserWorkerTerminalReason.CANCELLED, preserve_cancel=True)
            raise
        except TimeoutError:
            await self._cleanup_after_failure(BrowserWorkerTerminalReason.TIMEOUT)
            raise BrowserWorkerContractError("browser worker session timed out") from None
        except BrowserWorkerCleanupError:
            await self._cleanup_after_failure(BrowserWorkerTerminalReason.CONTRACT_ERROR)
            raise
        except BrowserWorkerContractError:
            await self._cleanup_after_failure(BrowserWorkerTerminalReason.CONTRACT_ERROR)
            raise
        except Exception:
            await self._cleanup_after_failure(BrowserWorkerTerminalReason.CRASH)
            raise BrowserWorkerContractError("browser worker session failed safely") from None

    async def _execute_once(
        self,
        handshake: BrowserWorkerHandshakeRequest,
        request: BrowserSessionRequest,
    ) -> BrowserSessionResult:
        response = await self._transport.handshake(handshake)
        if type(response) is BrowserWorkerHandshakeResponse:
            self._worker_identity = response.external_worker_identity
        validate_worker_handshake(handshake, response)
        execute_request = BrowserWorkerExecuteRequest(
            handshake=handshake,
            external_worker_identity=response.external_worker_identity,
            actions=request.actions,
        )
        self._state = BrowserWorkerSessionState.EXECUTING
        result = await self._transport.execute(execute_request)
        validate_worker_result(execute_request, result)
        self._state = BrowserWorkerSessionState.CLOSING
        close_request = self._termination_request(BrowserWorkerTerminalReason.COMPLETED)
        receipt = await self._transport.close(close_request)
        validate_worker_termination(close_request, receipt)
        self._cleanup_confirmed = True
        self._state = BrowserWorkerSessionState.TERMINATED
        return BrowserSessionResult(outputs=result.outputs)

    def _issue_handshake(self) -> BrowserWorkerHandshakeRequest:
        try:
            nonce = self._nonce_factory()
        except Exception:
            raise BrowserWorkerContractError("browser worker nonce generation failed") from None
        _reserve_nonce(nonce)
        session_digest = hashlib.sha256(f"{self._request_id}\0{nonce}".encode("utf-8")).hexdigest()[:32]
        limits = self._policy.limits
        return BrowserWorkerHandshakeRequest(
            protocol_revision=BROWSER_WORKER_PROTOCOL_REVISION,
            request_id=self._request_id,
            session_id=f"browser-session-{session_digest}",
            nonce=nonce,
            isolation_contract_digest=browser_isolation_contract_digest(),
            policy_snapshot_digest=browser_policy_snapshot_digest(self._policy),
            deadline_milliseconds=max(1, int(self._timeout * 1000)),
            max_total_bytes=limits.max_total_bytes,
            max_actions=limits.max_steps,
            max_network_requests=limits.max_network_requests,
            max_redirects=limits.max_redirects,
        )

    def _termination_request(self, reason: BrowserWorkerTerminalReason) -> BrowserWorkerTerminateRequest:
        handshake = self._handshake
        if handshake is None:
            raise BrowserWorkerCleanupError("browser worker session binding was not issued")
        return BrowserWorkerTerminateRequest(
            protocol_revision=handshake.protocol_revision,
            request_id=handshake.request_id,
            session_id=handshake.session_id,
            nonce=handshake.nonce,
            isolation_contract_digest=handshake.isolation_contract_digest,
            policy_snapshot_digest=handshake.policy_snapshot_digest,
            external_worker_identity=self._worker_identity or "unverified-worker",
            reason=reason,
        )

    async def _cleanup_after_failure(
        self,
        reason: BrowserWorkerTerminalReason,
        *,
        preserve_cancel: bool = False,
    ) -> None:
        if self._cleanup_confirmed:
            return
        if self._terminate_called:
            self._state = BrowserWorkerSessionState.UNKNOWN
            if preserve_cancel:
                return
            raise BrowserWorkerCleanupError("browser worker termination was already attempted")
        self._terminate_called = True
        request = self._termination_request(reason)
        try:
            receipt = await asyncio.wait_for(
                self._transport.terminate(request),
                timeout=self._cleanup_timeout,
            )
            validate_worker_termination(request, receipt)
            if self._worker_identity is None:
                raise BrowserWorkerCleanupError("browser worker identity was not confirmed before termination")
        except asyncio.CancelledError:
            self._state = BrowserWorkerSessionState.UNKNOWN
            if preserve_cancel:
                return
            raise
        except Exception:
            self._state = BrowserWorkerSessionState.UNKNOWN
            if preserve_cancel:
                return
            raise BrowserWorkerCleanupError("browser worker termination was not confirmed") from None
        self._cleanup_confirmed = True
        self._state = BrowserWorkerSessionState.TERMINATED


def _reserve_nonce(value: object) -> None:
    if not isinstance(value, str):
        raise BrowserWorkerContractError("browser worker nonce generation failed")
    with _NONCE_LOCK:
        if value in _RECENT_NONCE_SET:
            raise BrowserWorkerContractError("browser worker nonce replay was rejected")
        try:
            BrowserWorkerHandshakeRequest(
                protocol_revision=BROWSER_WORKER_PROTOCOL_REVISION,
                request_id="nonce-validation",
                session_id="nonce-validation",
                nonce=value,
                isolation_contract_digest="0" * 64,
                policy_snapshot_digest="0" * 64,
                deadline_milliseconds=1,
                max_total_bytes=64 * 1024,
                max_actions=1,
                max_network_requests=1,
                max_redirects=0,
            )
        except (TypeError, ValueError):
            raise BrowserWorkerContractError("browser worker nonce generation failed") from None
        _RECENT_NONCES.append(value)
        _RECENT_NONCE_SET.add(value)
        if len(_RECENT_NONCES) > _MAX_RECENT_NONCES:
            _RECENT_NONCE_SET.discard(_RECENT_NONCES.popleft())


def _bounded_timeout(value: object, *, label: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.001 <= float(value) <= maximum:
        raise ValueError(f"{label} is outside the allowed range")
    return float(value)


__all__ = [
    "BrowserWorkerSession",
    "BrowserWorkerSessionState",
    "BrowserWorkerTransport",
]
