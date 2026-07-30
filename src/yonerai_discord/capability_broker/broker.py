"""Fail-closed orchestration for managed capability sandbox backends."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from datetime import datetime, timezone

from .contract import (
    ArtifactDescriptor,
    ArtifactKind,
    AuditOutcome,
    AuditRecord,
    AuthorizationCurrent,
    BackendCleanupReceipt,
    BackendExecution,
    BackendStatus,
    BrokerResult,
    CapabilityArtifact,
    CapabilityAuditError,
    CapabilityAuditSink,
    CapabilityAuthorizationError,
    CapabilityBrokerStatus,
    CapabilityCleanupError,
    CapabilityContractError,
    CapabilityIdempotencyError,
    CapabilityKind,
    CapabilityRequest,
    CapabilityTimeoutError,
    CapabilityUnavailableError,
    CleanupReason,
    ExecutionReceipt,
    ExecutionStatus,
    ManagedSandboxBackend,
    PermissionCurrent,
    capability_policy_digest,
)


_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


class InMemoryCapabilityAuditSink:
    """Small offline/test sink; runtime composition may inject durable storage."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def append(self, record: AuditRecord) -> None:
        if not isinstance(record, AuditRecord):
            raise TypeError("record must be an AuditRecord")
        self.records.append(record)


class CapabilityBroker:
    """Coordinates one sealed backend without exposing a command execution surface."""

    def __init__(
        self,
        *,
        backend: ManagedSandboxBackend | None,
        audit_sink: CapabilityAuditSink | None,
        expected_backend_id: str,
        expected_identity_digest: str,
    ) -> None:
        if not isinstance(expected_backend_id, str) or _IDENTIFIER.fullmatch(expected_backend_id) is None:
            raise CapabilityContractError("expected backend id is invalid")
        if not isinstance(expected_identity_digest, str) or _SHA256.fullmatch(expected_identity_digest) is None:
            raise CapabilityContractError("expected backend identity is invalid")
        self._backend = backend
        self._audit_sink = audit_sink
        self._expected_backend_id = expected_backend_id
        self._expected_identity_digest = expected_identity_digest
        self._completed: dict[str, tuple[str, BrokerResult]] = {}
        self._lock = asyncio.Lock()
        self._cleanup_uncertain = False

    async def status(self) -> CapabilityBrokerStatus:
        if self._backend is None:
            return CapabilityBrokerStatus(False, False, "backend_unconfigured")
        if self._audit_sink is None:
            return CapabilityBrokerStatus(False, False, "audit_unconfigured")
        if self._cleanup_uncertain:
            return CapabilityBrokerStatus(
                True,
                False,
                "cleanup_unconfirmed",
                self._expected_backend_id,
                self._expected_identity_digest,
            )
        try:
            async with asyncio.timeout(5.0):
                backend_status = await self._backend.status(capability_policy_digest())
        except asyncio.CancelledError:
            raise
        except Exception:
            return CapabilityBrokerStatus(
                True,
                False,
                "backend_status_failed",
                self._expected_backend_id,
                self._expected_identity_digest,
            )
        if not isinstance(backend_status, BackendStatus):
            return CapabilityBrokerStatus(
                True,
                False,
                "invalid_backend_status",
                self._expected_backend_id,
                self._expected_identity_digest,
            )
        reason = self._status_reason(backend_status, expected_policy=capability_policy_digest())
        return CapabilityBrokerStatus(
            configured=backend_status.configured,
            ready=reason == "ready",
            reason_code=reason,
            backend_id=backend_status.backend_id,
            identity_digest=backend_status.identity_digest,
        )

    async def execute(
        self,
        request: CapabilityRequest,
        *,
        permission_current: PermissionCurrent,
        authorization_current: AuthorizationCurrent,
    ) -> BrokerResult:
        if not isinstance(request, CapabilityRequest):
            raise CapabilityContractError("request must be a CapabilityRequest")
        if not callable(permission_current) or not callable(authorization_current):
            raise CapabilityAuthorizationError("fresh authorization is unavailable")
        if self._audit_sink is None:
            raise CapabilityAuditError("managed sandbox audit is not configured")
        if self._backend is None:
            await self._append_audit(
                request,
                AuditOutcome.FAILED,
                failure_code="backend_unconfigured",
            )
            raise CapabilityUnavailableError("managed sandbox backend is not configured")

        async with self._lock:
            if self._cleanup_uncertain:
                await self._append_audit(
                    request,
                    AuditOutcome.CLEANUP_UNCONFIRMED,
                    backend_id=self._expected_backend_id,
                    identity_digest=self._expected_identity_digest,
                    failure_code="cleanup_unconfirmed",
                )
                raise CapabilityCleanupError("managed sandbox cleanup was not confirmed")
            if not await self._authorized(request, permission_current, authorization_current):
                await self._append_audit(request, AuditOutcome.DENIED, failure_code="authorization_denied")
                raise CapabilityAuthorizationError("fresh authorization is not current")

            prior = self._completed.get(request.idempotency_key)
            if prior is not None:
                prior_digest, result = prior
                if prior_digest != request.request_digest:
                    await self._append_audit(
                        request,
                        AuditOutcome.DENIED,
                        failure_code="idempotency_conflict",
                    )
                    raise CapabilityIdempotencyError("idempotency key belongs to another request")
                await self._append_audit(
                    request,
                    AuditOutcome.REPLAYED,
                    backend_id=result.receipt.backend_id,
                    identity_digest=result.receipt.identity_digest,
                    artifact=result.receipt.artifact,
                )
                return result

            backend_status = await self._load_status(request)
            await self._append_audit(
                request,
                AuditOutcome.STARTED,
                backend_id=backend_status.backend_id,
                identity_digest=backend_status.identity_digest,
            )
            if not await self._authorized(request, permission_current, authorization_current):
                await self._append_audit(
                    request,
                    AuditOutcome.DENIED,
                    backend_id=backend_status.backend_id,
                    identity_digest=backend_status.identity_digest,
                    failure_code="authorization_changed_before_execution",
                )
                raise CapabilityAuthorizationError("fresh authorization changed before execution")
            execution = await self._run_backend(request)
            if not self._valid_execution(request, execution):
                await self._cleanup_or_raise(request, CleanupReason.FAILED)
                await self._append_audit(
                    request,
                    AuditOutcome.FAILED,
                    backend_id=self._expected_backend_id,
                    identity_digest=self._expected_identity_digest,
                    failure_code="invalid_execution_binding",
                )
                raise CapabilityUnavailableError("managed sandbox result binding is invalid")

            if not await self._authorized(request, permission_current, authorization_current):
                await self._append_audit(
                    request,
                    AuditOutcome.DENIED,
                    backend_id=execution.backend_id,
                    identity_digest=execution.identity_digest,
                    failure_code="authorization_changed",
                )
                raise CapabilityAuthorizationError("fresh authorization changed before artifact commit")

            result = self._build_result(request, execution)
            await self._append_audit(
                request,
                AuditOutcome.SUCCEEDED,
                backend_id=execution.backend_id,
                identity_digest=execution.identity_digest,
                artifact=result.receipt.artifact,
            )
            self._completed[request.idempotency_key] = (request.request_digest, result)
            return result

    async def _load_status(self, request: CapabilityRequest) -> BackendStatus:
        assert self._backend is not None
        try:
            async with asyncio.timeout(min(5.0, request.resources.wall_time_seconds)):
                status = await self._backend.status(request.policy_digest)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._append_audit(
                request,
                AuditOutcome.FAILED,
                backend_id=self._expected_backend_id,
                identity_digest=self._expected_identity_digest,
                failure_code="backend_status_failed",
            )
            raise CapabilityUnavailableError("managed sandbox status is unavailable") from None
        reason = self._status_reason(status, expected_policy=request.policy_digest)
        if reason != "ready":
            await self._append_audit(
                request,
                AuditOutcome.FAILED,
                backend_id=self._expected_backend_id,
                identity_digest=self._expected_identity_digest,
                failure_code=reason,
            )
            raise CapabilityUnavailableError(f"managed sandbox is unavailable: {reason}")
        return status

    def _status_reason(self, status: object, *, expected_policy: str) -> str:
        if not isinstance(status, BackendStatus):
            return "invalid_backend_status"
        if not status.configured:
            return "backend_unconfigured"
        if status.backend_id != self._expected_backend_id:
            return "backend_replaced"
        if status.identity_digest != self._expected_identity_digest:
            return "identity_replaced"
        if status.policy_digest != expected_policy:
            return "policy_replaced"
        if not status.ready:
            return "backend_not_ready"
        return "ready"

    async def _run_backend(self, request: CapabilityRequest) -> BackendExecution:
        assert self._backend is not None
        try:
            async with asyncio.timeout(request.resources.wall_time_seconds):
                return await self._backend.execute(request)
        except asyncio.CancelledError:
            await self._cleanup_or_raise(request, CleanupReason.CANCELLED)
            await self._append_audit(
                request,
                AuditOutcome.CANCELLED,
                backend_id=self._expected_backend_id,
                identity_digest=self._expected_identity_digest,
                failure_code="cancelled",
            )
            raise
        except TimeoutError:
            await self._cleanup_or_raise(request, CleanupReason.TIMED_OUT)
            await self._append_audit(
                request,
                AuditOutcome.TIMED_OUT,
                backend_id=self._expected_backend_id,
                identity_digest=self._expected_identity_digest,
                failure_code="timeout",
            )
            raise CapabilityTimeoutError("managed sandbox request timed out") from None
        except Exception:
            await self._cleanup_or_raise(request, CleanupReason.FAILED)
            await self._append_audit(
                request,
                AuditOutcome.FAILED,
                backend_id=self._expected_backend_id,
                identity_digest=self._expected_identity_digest,
                failure_code="backend_failed",
            )
            raise CapabilityUnavailableError("managed sandbox execution failed safely") from None

    def _valid_execution(self, request: CapabilityRequest, execution: object) -> bool:
        if not isinstance(execution, BackendExecution):
            return False
        return (
            execution.request_digest == request.request_digest
            and execution.binding == request.binding
            and execution.backend_id == self._expected_backend_id
            and execution.identity_digest == self._expected_identity_digest
            and execution.policy_digest == request.policy_digest
            and execution.artifact_kind is _artifact_kind_for(request)
            and len(execution.text.encode("utf-8", "strict")) <= request.resources.max_output_bytes
            and execution.cleanup_confirmed is True
        )

    async def _cleanup_or_raise(self, request: CapabilityRequest, reason: CleanupReason) -> None:
        assert self._backend is not None
        try:
            async with asyncio.timeout(min(5.0, request.resources.wall_time_seconds)):
                receipt = await asyncio.shield(self._backend.cleanup(request, reason))
            if not self._valid_cleanup(request, receipt):
                raise CapabilityCleanupError("managed sandbox cleanup was not confirmed")
        except asyncio.CancelledError:
            self._cleanup_uncertain = True
            raise
        except Exception:
            self._cleanup_uncertain = True
            await self._append_audit(
                request,
                AuditOutcome.CLEANUP_UNCONFIRMED,
                backend_id=self._expected_backend_id,
                identity_digest=self._expected_identity_digest,
                failure_code="cleanup_unconfirmed",
            )
            raise CapabilityCleanupError("managed sandbox cleanup was not confirmed") from None

    def _valid_cleanup(self, request: CapabilityRequest, receipt: object) -> bool:
        return (
            isinstance(receipt, BackendCleanupReceipt)
            and receipt.request_digest == request.request_digest
            and receipt.backend_id == self._expected_backend_id
            and receipt.identity_digest == self._expected_identity_digest
            and receipt.worker_terminated is True
            and receipt.workspace_destroyed is True
        )

    async def _authorized(
        self,
        request: CapabilityRequest,
        permission_current: PermissionCurrent,
        authorization_current: AuthorizationCurrent,
    ) -> bool:
        try:
            permission = permission_current(request.binding, request.permission)
            if inspect.isawaitable(permission):
                permission = await permission
            if permission is not True:
                return False
            authorization = authorization_current()
            if inspect.isawaitable(authorization):
                authorization = await authorization
            return authorization is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _append_audit(
        self,
        request: CapabilityRequest,
        outcome: AuditOutcome,
        *,
        backend_id: str | None = None,
        identity_digest: str | None = None,
        artifact: ArtifactDescriptor | None = None,
        failure_code: str | None = None,
    ) -> None:
        if self._audit_sink is None:
            raise CapabilityAuditError("managed sandbox audit is not configured")
        record = AuditRecord(
            request_id=request.request_id,
            request_digest=request.request_digest,
            binding=request.binding,
            capability=request.capability,
            outcome=outcome,
            occurred_at=datetime.now(timezone.utc),
            backend_id=backend_id,
            identity_digest=identity_digest,
            artifact_id=None if artifact is None else artifact.artifact_id,
            output_sha256=None if artifact is None else artifact.sha256,
            failure_code=failure_code,
        )
        try:
            await self._audit_sink.append(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CapabilityAuditError("managed sandbox audit write failed") from None

    @staticmethod
    def _build_result(request: CapabilityRequest, execution: BackendExecution) -> BrokerResult:
        encoded = execution.text.encode("utf-8", "strict")
        digest = hashlib.sha256(encoded).hexdigest()
        artifact_identity = hashlib.sha256(
            b"yonerai.capability-broker.artifact.v1\0"
            + request.request_digest.encode("ascii")
            + b"\0"
            + digest.encode("ascii")
        ).hexdigest()
        artifact = ArtifactDescriptor(
            artifact_id=f"artifact:{artifact_identity[:32]}",
            kind=execution.artifact_kind,
            media_type="text/plain; charset=utf-8",
            size_bytes=len(encoded),
            sha256=digest,
            owner=request.binding,
        )
        receipt = ExecutionReceipt(
            request_id=request.request_id,
            request_digest=request.request_digest,
            status=ExecutionStatus.COMPLETED,
            backend_id=execution.backend_id,
            identity_digest=execution.identity_digest,
            policy_digest=execution.policy_digest,
            artifact=artifact,
            cleanup_confirmed=True,
        )
        return BrokerResult(
            receipt=receipt,
            artifact=CapabilityArtifact(descriptor=artifact, _text=execution.text),
        )


def _artifact_kind_for(request: CapabilityRequest) -> ArtifactKind:
    return {
        CapabilityKind.MEDIA_INSPECTION: ArtifactKind.INSPECTION_TEXT,
        CapabilityKind.SUBTITLE_EXTRACTION: ArtifactKind.SUBTITLE_TEXT,
        CapabilityKind.THUMBNAIL_OCR: ArtifactKind.OCR_TEXT,
    }[request.capability]


__all__ = ["CapabilityBroker", "InMemoryCapabilityAuditSink"]
