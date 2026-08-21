"""Fail-closed service boundary for an injected trusted external sandbox."""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .sandbox_contract import (
    ExternalSandboxPort,
    SandboxCandidate,
    SandboxHandshake,
    SandboxRequest,
    SandboxResult,
    SandboxScope,
    SandboxTerminationReason,
    SandboxTerminationReceipt,
)


class SandboxRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"


class SandboxOperationProfile(StrEnum):
    """Code-owned outer operation budgets; never a caller-supplied duration."""

    DIRECT = "direct"
    DISPOSABLE_VM = "disposable_vm"


class SandboxRunCancelledError(asyncio.CancelledError):
    """Content-free cancellation carrying only exact cleanup confirmation."""

    __slots__ = ("cleanup_confirmed",)

    def __init__(self, *, cleanup_confirmed: bool) -> None:
        if type(cleanup_confirmed) is not bool:
            raise TypeError("cleanup_confirmed must be bool")
        super().__init__()
        self.cleanup_confirmed = cleanup_confirmed


# 65s VM boot/socket accept + 60s guest wall + 60s owner cleanup + 10s
# bounded scheduling overhead.  The guest's own wall/CPU policy is unchanged.
DISPOSABLE_VM_OPERATION_TIMEOUT_SECONDS = 195.0


@dataclass(frozen=True, slots=True)
class SandboxRunOutcome:
    status: SandboxRunStatus
    result: SandboxResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, SandboxRunStatus):
            raise TypeError("status must be SandboxRunStatus")
        if self.status is SandboxRunStatus.SUCCEEDED:
            if type(self.result) is not SandboxResult:
                raise ValueError("successful outcome requires a typed result")
        elif self.result is not None:
            raise ValueError("non-success outcome cannot carry a result")


@dataclass(frozen=True, slots=True)
class _BackendLease:
    port: ExternalSandboxPort
    containment_current: Callable[[], bool] | None
    generation: int


class ExternalSandboxService:
    """Accepts results only after a separately trusted containment callback."""

    def __init__(
        self,
        *,
        port: ExternalSandboxPort | None,
        containment_current: Callable[[], bool] | None,
        policy=None,
        operation_profile: SandboxOperationProfile = SandboxOperationProfile.DIRECT,
        handshake_timeout_seconds: float = 1,
        cleanup_timeout_seconds: float = 1,
        backend_generation: int = 1,
    ) -> None:
        from .sandbox_contract import SandboxPolicy

        if policy is not None and type(policy) is not SandboxPolicy:
            raise TypeError("policy must be a code-owned SandboxPolicy")
        if type(operation_profile) is not SandboxOperationProfile:
            raise TypeError("operation profile must be code-owned")
        if (
            isinstance(handshake_timeout_seconds, bool)
            or not isinstance(handshake_timeout_seconds, (int, float))
            or not 0 < handshake_timeout_seconds <= 1
        ):
            raise ValueError("handshake timeout must be within one second")
        if type(backend_generation) is not int or backend_generation <= 0:
            raise ValueError("backend generation must be positive")
        if (
            isinstance(cleanup_timeout_seconds, bool)
            or not isinstance(cleanup_timeout_seconds, (int, float))
            or not 0 < cleanup_timeout_seconds <= 60
        ):
            raise ValueError("cleanup timeout must be within sixty seconds")
        self._lease = (
            _BackendLease(port=port, containment_current=containment_current, generation=backend_generation)
            if port is not None
            else None
        )
        self._highest_generation = backend_generation if port is not None else 0
        self._quarantined_generation: int | None = None
        self._state_lock = threading.Lock()
        self._active_runs = 0
        self._confirmed_successes: dict[int, tuple[SandboxRunOutcome, SandboxCandidate]] = {}
        self._policy = policy or SandboxPolicy()
        self._operation_profile = operation_profile
        self._handshake_timeout_seconds = float(handshake_timeout_seconds)
        self._cleanup_timeout_seconds = float(cleanup_timeout_seconds)

    @property
    def containment_current(self) -> bool:
        """Report whether the installed generation remains trusted and unquarantined.

        This intentionally remains true while one run is active so a separately
        authorized cancellation can still reach that run.  Capacity and trust
        are different facts; ``run()`` continues to enforce the single-run
        reservation itself.
        """
        with self._state_lock:
            lease = self._lease
        return lease is not None and self._is_current(lease)

    def register_backend_generation(
        self,
        *,
        port: ExternalSandboxPort,
        containment_current: Callable[[], bool] | None,
        backend_generation: int,
    ) -> None:
        """Explicitly replace a backend generation and clear only the old quarantine."""
        if port is None:
            raise TypeError("port is required")
        with self._state_lock:
            if self._active_runs:
                raise RuntimeError("backend generation cannot change while runs are active")
            if (
                type(backend_generation) is not int
                or backend_generation <= 0
                or backend_generation <= self._highest_generation
            ):
                raise ValueError("backend generation must increase")
            if self._lease is not None and port is self._lease.port:
                raise ValueError("backend generation requires a new port identity")
            self._lease = _BackendLease(
                port=port,
                containment_current=containment_current,
                generation=backend_generation,
            )
            self._highest_generation = backend_generation
            self._quarantined_generation = None
            self._confirmed_successes.clear()

    def consume_cleanup_confirmed_success(
        self,
        *,
        outcome: SandboxRunOutcome,
        candidate: SandboxCandidate,
    ) -> SandboxResult | None:
        """Consume one result issued only after this service confirmed cleanup."""
        if type(outcome) is not SandboxRunOutcome or type(candidate) is not SandboxCandidate:
            return None
        with self._state_lock:
            issued = self._confirmed_successes.get(id(outcome))
            if issued is None or issued[0] is not outcome or issued[1] is not candidate:
                return None
            del self._confirmed_successes[id(outcome)]
        return outcome.result if type(outcome.result) is SandboxResult else None

    async def run(
        self,
        *,
        candidate: SandboxCandidate,
        scope: SandboxScope,
        backend_identity: str,
    ) -> SandboxRunOutcome:
        lease = self._reserve_lease()
        if lease is None:
            return SandboxRunOutcome(SandboxRunStatus.UNAVAILABLE)
        try:
            return await self._run_reserved(
                lease=lease,
                candidate=candidate,
                scope=scope,
                backend_identity=backend_identity,
            )
        finally:
            self._release_run()

    async def _run_reserved(
        self,
        *,
        lease: _BackendLease,
        candidate: SandboxCandidate,
        scope: SandboxScope,
        backend_identity: str,
    ) -> SandboxRunOutcome:
        if not self._is_current(lease):
            return SandboxRunOutcome(SandboxRunStatus.UNAVAILABLE)
        request = SandboxRequest(
            candidate=candidate,
            scope=scope,
            policy=self._policy,
            backend_identity=backend_identity,
            backend_generation=lease.generation,
            session_nonce=secrets.token_hex(16),
        )

        started = False
        reason = SandboxTerminationReason.FAILED
        outcome = SandboxRunOutcome(SandboxRunStatus.FAILED)
        cancelled = False
        cleanup_confirmed = True
        try:
            # A timed-out handshake may have allocated a remote session. Treat
            # the attempt as started so cleanup is still required.
            started = True
            handshake = await asyncio.wait_for(lease.port.handshake(request), timeout=self._handshake_timeout_seconds)
            if not self._is_current(lease) or not _valid_handshake(request, handshake):
                reason = SandboxTerminationReason.REJECTED
                outcome = SandboxRunOutcome(SandboxRunStatus.REJECTED)
            else:
                try:
                    result = await asyncio.wait_for(
                        lease.port.execute(request), timeout=self._operation_timeout_seconds(request)
                    )
                except TimeoutError:
                    reason = SandboxTerminationReason.TIMEOUT
                    outcome = SandboxRunOutcome(SandboxRunStatus.TIMED_OUT)
                else:
                    if not self._is_current(lease) or not _valid_result(request, result):
                        reason = SandboxTerminationReason.REJECTED
                        outcome = SandboxRunOutcome(SandboxRunStatus.REJECTED)
                    else:
                        reason = SandboxTerminationReason.COMPLETED
                        outcome = SandboxRunOutcome(
                            SandboxRunStatus.SUCCEEDED,
                            result=result,
                        )
        except asyncio.CancelledError:
            reason = SandboxTerminationReason.CANCELLED
            cancelled = True
        except Exception:
            outcome = SandboxRunOutcome(SandboxRunStatus.FAILED)
        finally:
            if started:
                receipt, cleanup_cancelled = await _terminate_shielded(
                    lease.port,
                    request,
                    reason,
                    timeout_seconds=self._cleanup_timeout_seconds,
                )
                cancelled = cancelled or cleanup_cancelled
                cleanup_confirmed = _valid_termination(request, receipt, reason) and self._is_current(lease)
                if not cleanup_confirmed:
                    self._quarantine(lease)
        if cancelled:
            raise SandboxRunCancelledError(cleanup_confirmed=cleanup_confirmed) from None
        if not cleanup_confirmed:
            return SandboxRunOutcome(SandboxRunStatus.CLEANUP_UNCONFIRMED)
        if outcome.status is SandboxRunStatus.SUCCEEDED and not self._is_current(lease):
            return SandboxRunOutcome(SandboxRunStatus.REJECTED)
        if outcome.status is SandboxRunStatus.SUCCEEDED and not self._register_confirmed_success(
            lease=lease,
            outcome=outcome,
            candidate=candidate,
        ):
            return SandboxRunOutcome(SandboxRunStatus.REJECTED)
        return outcome

    def _operation_timeout_seconds(self, request: SandboxRequest) -> float:
        if self._operation_profile is SandboxOperationProfile.DISPOSABLE_VM:
            return DISPOSABLE_VM_OPERATION_TIMEOUT_SECONDS
        return request.policy.max_wall_time_ms / 1000

    def _register_confirmed_success(
        self,
        *,
        lease: _BackendLease,
        outcome: SandboxRunOutcome,
        candidate: SandboxCandidate,
    ) -> bool:
        with self._state_lock:
            if self._lease is not lease or self._quarantined_generation == lease.generation:
                return False
            if len(self._confirmed_successes) >= 64:
                del self._confirmed_successes[next(iter(self._confirmed_successes))]
            self._confirmed_successes[id(outcome)] = (outcome, candidate)
            return True

    def _is_current(self, lease: _BackendLease) -> bool:
        with self._state_lock:
            state_current = self._lease is lease and self._quarantined_generation != lease.generation
        if not state_current:
            return False
        try:
            return callable(lease.containment_current) and lease.containment_current() is True
        except Exception:
            return False

    def _quarantine(self, lease: _BackendLease) -> None:
        with self._state_lock:
            if self._lease is lease:
                self._quarantined_generation = lease.generation
                self._confirmed_successes.clear()

    def _reserve_lease(self) -> _BackendLease | None:
        with self._state_lock:
            lease = self._lease
            if lease is None or self._active_runs != 0 or self._quarantined_generation == lease.generation:
                return None
            self._active_runs += 1
            return lease

    def _release_run(self) -> None:
        with self._state_lock:
            if self._active_runs <= 0:
                raise RuntimeError("sandbox run reservation is unbalanced")
            self._active_runs -= 1


async def _terminate(
    port: ExternalSandboxPort,
    request: SandboxRequest,
    reason: SandboxTerminationReason,
    *,
    timeout_seconds: float,
) -> SandboxTerminationReceipt | None:
    try:
        return await asyncio.wait_for(port.terminate(request, reason), timeout=timeout_seconds)
    except Exception:
        return None


async def _terminate_shielded(
    port: ExternalSandboxPort,
    request: SandboxRequest,
    reason: SandboxTerminationReason,
    *,
    timeout_seconds: float,
) -> tuple[SandboxTerminationReceipt | None, bool]:
    cleanup = asyncio.create_task(_terminate(port, request, reason, timeout_seconds=timeout_seconds))
    try:
        return await asyncio.shield(cleanup), False
    except asyncio.CancelledError:
        # Preserve the reservation until the already bounded cleanup task has
        # actually stopped, even if cancellation is requested repeatedly.
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        if cleanup.cancelled():
            return None, True
        return cleanup.result(), True


def _same_binding(request: SandboxRequest, value: object) -> bool:
    return (
        getattr(value, "scope", None) == request.scope
        and getattr(value, "backend_identity", None) == request.backend_identity
        and getattr(value, "backend_generation", None) == request.backend_generation
        and getattr(value, "policy_digest", None) == request.policy_digest
        and getattr(value, "request_digest", None) == request.request_digest
        and getattr(value, "session_nonce", None) == request.session_nonce
    )


def _valid_handshake(request: SandboxRequest, handshake: object) -> bool:
    return (
        isinstance(handshake, SandboxHandshake)
        and _same_binding(request, handshake)
        and (
            type(handshake.network_connections) is int
            and handshake.network_connections == 0
            and handshake.host_mount is False
            and handshake.secret_access is False
            and handshake.environment_access is False
            and handshake.privileged is False
            and handshake.docker_socket is False
            and handshake.clipboard is False
            and handshake.persistent_profile is False
            and type(handshake.child_processes) is int
            and handshake.child_processes == 0
        )
    )


def _valid_result(request: SandboxRequest, result: object) -> bool:
    return isinstance(result, SandboxResult) and _same_binding(request, result)


def _valid_termination(
    request: SandboxRequest, receipt: SandboxTerminationReceipt | None, reason: SandboxTerminationReason
) -> bool:
    return (
        isinstance(receipt, SandboxTerminationReceipt)
        and _same_binding(request, receipt)
        and receipt.reason is reason
        and receipt.worker_terminated is True
        and receipt.workspace_destroyed is True
    )


__all__ = [
    "DISPOSABLE_VM_OPERATION_TIMEOUT_SECONDS",
    "ExternalSandboxService",
    "SandboxOperationProfile",
    "SandboxRunCancelledError",
    "SandboxRunOutcome",
    "SandboxRunStatus",
]
