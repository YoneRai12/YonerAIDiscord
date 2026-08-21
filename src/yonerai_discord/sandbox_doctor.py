from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from yonerai_discord.capability_broker import (
    AuditOutcome,
    BackendCleanupReceipt,
    BackendExecution,
    BackendStatus,
    CapabilityAuthorizationError,
    CapabilityBinding,
    CapabilityBroker,
    CapabilityCleanupError,
    CapabilityKind,
    CapabilityRequest,
    CapabilityTimeoutError,
    CleanupReason,
    HYPERV_MEDIA_BACKEND_ID,
    HYPERV_MEDIA_IDENTITY_DIGEST,
    HyperVMediaManagedBackend,
    InMemoryCapabilityAuditSink,
    MediaCapabilityInput,
)
from yonerai_discord.capability_broker.contract import (
    ArtifactKind,
)
from yonerai_discord.capability_forge.execution_sandbox_signing import (
    JOB_SCHEMA_VERSION,
    RECEIPT_SCHEMA_VERSION,
)
from yonerai_discord.capability_forge.hyperv_socket_transport import (
    AF_HYPERV,
    GUEST_PORT,
    HV_PROTOCOL_RAW,
    SERVICE_ID,
)
from yonerai_discord.modules.media_inspection.domain import (
    MediaInspectionResult,
)
from yonerai_discord.modules.media_inspection.hyperv_contract import (
    HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
    HyperVMediaExecutionResult,
    HyperVMediaProbeResult,
)


SCHEMA = "yonerai.sandbox-doctor.v1"
_EVIDENCE = "injected sandbox contract evidence"


class SandboxDoctorState(StrEnum):
    CONTRACT_READY = "contract_ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SandboxDoctorReport:
    state: SandboxDoctorState
    typed_status: bool
    exact_binding: bool
    receipt_integrity: bool
    artifact_ownership: bool
    audit: bool
    timeout_cleanup: bool
    cancel_cleanup: bool
    transport_contract: bool
    signed_job_contract: bool
    signed_receipt_contract: bool
    durable_replay_contract: bool
    error_code: str | None = None

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "state": self.state.value,
            "scope": {
                "backend": "injected_hyperv_contract",
                "actual_vm_contacted": False,
                "live_ready": False,
            },
            "components": {
                "transport": "implemented_offline" if self.transport_contract else "unavailable",
                "signed_job": "implemented_offline" if self.signed_job_contract else "unavailable",
                "signed_receipt": "implemented_offline" if self.signed_receipt_contract else "unavailable",
                "durable_replay": "implemented_offline" if self.durable_replay_contract else "unavailable",
                "trusted_broker": "unconfigured",
                "guest_worker": "unconfigured",
                "vm_lifecycle": "unconfigured",
            },
            "blockers": [
                "trusted_broker_unconfigured",
                "actual_vm_absent",
                "canary_not_run",
            ],
            "checks": {
                "typed_status": self.typed_status,
                "exact_binding": self.exact_binding,
                "receipt_integrity": self.receipt_integrity,
                "artifact_ownership": self.artifact_ownership,
                "audit": self.audit,
                "timeout_cleanup": self.timeout_cleanup,
                "cancel_cleanup": self.cancel_cleanup,
            },
            "error_code": self.error_code,
        }


class _InjectedHyperVProvider:
    def __init__(self) -> None:
        self.attestation: HyperVMediaProbeResult | HyperVMediaExecutionResult | None = None

    async def probe(self) -> HyperVMediaProbeResult:
        receipt = HyperVMediaProbeResult(
            ready=True,
            cleanup_confirmed=True,
            identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
            effective_policy_revision=HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
            effective_policy_digest=HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
        )
        self.attestation = receipt
        return receipt

    async def inspect(self, url: str, instruction: str) -> MediaInspectionResult:
        del url, instruction
        self.attestation = HyperVMediaExecutionResult(
            text=_EVIDENCE,
            cleanup_confirmed=True,
            identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
            effective_policy_revision=HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
            effective_policy_digest=HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
        )
        return MediaInspectionResult(_EVIDENCE)

    async def close(self) -> None:
        return None


class _LifecycleBackend:
    def __init__(self, outcome: str, *, cleanup_valid: bool = True) -> None:
        self.outcome = outcome
        self.cleanup_valid = cleanup_valid
        self.cleanup_reasons: list[CleanupReason] = []

    async def status(self, policy_digest: str) -> BackendStatus:
        return BackendStatus(
            configured=True,
            ready=True,
            backend_id=HYPERV_MEDIA_BACKEND_ID,
            identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
            policy_digest=policy_digest,
        )

    async def execute(self, request: CapabilityRequest) -> BackendExecution:
        del request
        if self.outcome == "timeout":
            raise TimeoutError
        raise asyncio.CancelledError

    async def cleanup(
        self,
        request: CapabilityRequest,
        reason: CleanupReason,
    ) -> BackendCleanupReceipt:
        self.cleanup_reasons.append(reason)
        if not self.cleanup_valid:
            return object()  # type: ignore[return-value]
        return BackendCleanupReceipt(
            request_digest=request.request_digest,
            backend_id=HYPERV_MEDIA_BACKEND_ID,
            identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
            worker_terminated=True,
            workspace_destroyed=True,
        )


def _request(*, suffix: str) -> CapabilityRequest:
    return CapabilityRequest(
        request_id=f"doctor:{suffix}",
        idempotency_key=f"doctor:{suffix}",
        binding=CapabilityBinding(actor_id=101, guild_id=202, conversation_id=303),
        capability=CapabilityKind.MEDIA_INSPECTION,
        payload=MediaCapabilityInput(
            "https://youtube.com/watch?v=doctor",
            "公開メディアを検査してください",
        ),
    )


def _broker(backend: object, audit: InMemoryCapabilityAuditSink) -> CapabilityBroker:
    return CapabilityBroker(
        backend=backend,  # type: ignore[arg-type]
        audit_sink=audit,
        expected_backend_id=HYPERV_MEDIA_BACKEND_ID,
        expected_identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
    )


async def _verify_success() -> tuple[bool, bool, bool, bool, bool]:
    audit = InMemoryCapabilityAuditSink()
    broker = _broker(HyperVMediaManagedBackend(_InjectedHyperVProvider()), audit)
    request = _request(suffix="success")
    status = await broker.status()
    result = await broker.execute(
        request,
        permission_current=lambda binding, permission: binding == request.binding and permission == request.permission,
        authorization_current=lambda: True,
    )
    descriptor = result.receipt.artifact
    typed_status = (
        status.configured is True
        and status.ready is True
        and status.reason_code == "ready"
        and status.backend_id == HYPERV_MEDIA_BACKEND_ID
    )
    exact_binding = descriptor.owner == request.binding and result.receipt.request_digest == request.request_digest
    receipt_integrity = (
        result.receipt.cleanup_confirmed is True
        and descriptor.kind is ArtifactKind.INSPECTION_TEXT
        and descriptor.sha256 == hashlib.sha256(_EVIDENCE.encode("utf-8")).hexdigest()
        and descriptor.size_bytes == len(_EVIDENCE.encode("utf-8"))
    )
    try:
        result.artifact.read_text(binding=CapabilityBinding(actor_id=999, guild_id=202, conversation_id=303))
    except CapabilityAuthorizationError:
        ownership_denied = True
    else:
        ownership_denied = False
    artifact_ownership = result.artifact.read_text(binding=request.binding) == _EVIDENCE and ownership_denied
    audit_ok = [record.outcome for record in audit.records] == [
        AuditOutcome.STARTED,
        AuditOutcome.SUCCEEDED,
    ] and all(
        record.binding == request.binding
        and record.request_digest == request.request_digest
        and record.capability is request.capability
        for record in audit.records
    )
    return typed_status, exact_binding, receipt_integrity, artifact_ownership, audit_ok


async def _verify_timeout_cleanup(*, cleanup_valid: bool = True) -> bool:
    audit = InMemoryCapabilityAuditSink()
    backend = _LifecycleBackend("timeout", cleanup_valid=cleanup_valid)
    broker = _broker(backend, audit)
    request = _request(suffix="timeout")
    expected_error = CapabilityTimeoutError if cleanup_valid else CapabilityCleanupError
    try:
        await broker.execute(
            request,
            permission_current=lambda _binding, _permission: True,
            authorization_current=lambda: True,
        )
    except expected_error:
        pass
    else:
        return False
    return (
        cleanup_valid
        and backend.cleanup_reasons == [CleanupReason.TIMED_OUT]
        and [record.outcome for record in audit.records] == [AuditOutcome.STARTED, AuditOutcome.TIMED_OUT]
    )


async def _verify_cancel_cleanup(*, cleanup_valid: bool = True) -> bool:
    audit = InMemoryCapabilityAuditSink()
    backend = _LifecycleBackend("cancel", cleanup_valid=cleanup_valid)
    broker = _broker(backend, audit)
    request = _request(suffix="cancel")
    try:
        await broker.execute(
            request,
            permission_current=lambda _binding, _permission: True,
            authorization_current=lambda: True,
        )
    except CapabilityCleanupError:
        return False
    except asyncio.CancelledError:
        pass
    else:
        return False
    return (
        cleanup_valid
        and backend.cleanup_reasons == [CleanupReason.CANCELLED]
        and [record.outcome for record in audit.records] == [AuditOutcome.STARTED, AuditOutcome.CANCELLED]
    )


async def run_sandbox_doctor() -> SandboxDoctorReport:
    checks = (False, False, False, False, False)
    timeout_cleanup = False
    cancel_cleanup = False
    components = _verify_execution_sandbox_components()
    try:
        checks = await _verify_success()
        timeout_cleanup = await _verify_timeout_cleanup()
        cancel_cleanup = await _verify_cancel_cleanup()
    except asyncio.CancelledError:
        raise
    except Exception:
        return SandboxDoctorReport(
            SandboxDoctorState.FAILED,
            *checks,
            timeout_cleanup,
            cancel_cleanup,
            *components,
            error_code="contract_check_failed",
        )
    ready = all((*checks, timeout_cleanup, cancel_cleanup, *components))
    return SandboxDoctorReport(
        SandboxDoctorState.CONTRACT_READY if ready else SandboxDoctorState.FAILED,
        *checks,
        timeout_cleanup,
        cancel_cleanup,
        *components,
        error_code=None if ready else "contract_check_failed",
    )


def _verify_execution_sandbox_components() -> tuple[bool, bool, bool, bool]:
    """Verify code-owned C1 identities without contacting a broker or VM."""
    try:
        transport = (
            AF_HYPERV == 34
            and HV_PROTOCOL_RAW == 1
            and GUEST_PORT == 40_509
            and str(SERVICE_ID) == "00009e3d-facb-11e6-bd58-64006a7986d3"
        )
        signed_job = JOB_SCHEMA_VERSION == "yonerai.exec-sandbox.signed-job.v1"
        signed_receipt = RECEIPT_SCHEMA_VERSION == "yonerai.exec-sandbox.signed-receipt.v1"
        from yonerai_discord.capability_forge.execution_sandbox_replay import SqliteExecutionReplayLedger

        durable_replay = SqliteExecutionReplayLedger.__name__ == "SqliteExecutionReplayLedger"
    except Exception:
        return False, False, False, False
    return transport, signed_job, signed_receipt, durable_replay


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yonerai-discord-sandbox-doctor",
        description="Offline managed-sandbox contract doctor",
    )
    parser.parse_args(argv)
    report = asyncio.run(run_sandbox_doctor())
    print(
        json.dumps(
            report.to_mapping(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if report.state is SandboxDoctorState.CONTRACT_READY else 2


if __name__ == "__main__":
    raise SystemExit(main())
