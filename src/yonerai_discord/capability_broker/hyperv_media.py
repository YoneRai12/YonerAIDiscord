"""Managed sandbox adapter for the fixed Hyper-V media inspection provider."""

from __future__ import annotations

import asyncio
import re
from typing import Protocol

from yonerai_discord.modules.media_inspection.domain import MediaInspectionResult
from yonerai_discord.modules.media_inspection.hyperv_contract import (
    HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
    HYPERV_MEDIA_IDENTITY_DIGEST,
    HyperVMediaExecutionResult,
    HyperVMediaProbeResult,
)

from .contract import (
    ArtifactKind,
    BackendCleanupReceipt,
    BackendExecution,
    BackendStatus,
    CapabilityCleanupError,
    CapabilityContractError,
    CapabilityKind,
    CapabilityRequest,
    CapabilityUnavailableError,
    CleanupReason,
)


HYPERV_MEDIA_BACKEND_ID = "managed-hyperv-media"

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_SUBTITLE_INSTRUCTION = "字幕を抽出し、取得できた字幕テキストだけを返してください"
_OCR_INSTRUCTION = "サムネイル画像をOCRし、読み取れたテキストだけを返してください"


class HyperVMediaProvider(Protocol):
    @property
    def attestation(self) -> HyperVMediaProbeResult | HyperVMediaExecutionResult | None: ...

    async def probe(self) -> HyperVMediaProbeResult: ...

    async def inspect(self, url: str, instruction: str) -> MediaInspectionResult: ...

    async def close(self) -> None: ...


class HyperVMediaManagedBackend:
    """Seals the media provider behind three typed intents and no command field."""

    def __init__(
        self,
        provider: HyperVMediaProvider | None,
        *,
        identity_digest: str = HYPERV_MEDIA_IDENTITY_DIGEST,
    ) -> None:
        if not isinstance(identity_digest, str) or _SHA256.fullmatch(identity_digest) is None:
            raise CapabilityContractError("Hyper-V identity digest is invalid")
        self._provider = provider
        self._identity_digest = identity_digest
        self._closed = False
        self._cleanup_confirmed_for: str | None = None

    async def status(self, policy_digest: str) -> BackendStatus:
        if not isinstance(policy_digest, str) or _SHA256.fullmatch(policy_digest) is None:
            raise CapabilityContractError("broker policy digest is invalid")
        if self._provider is None or self._closed:
            return BackendStatus(
                configured=False,
                ready=False,
                backend_id=HYPERV_MEDIA_BACKEND_ID,
                identity_digest=self._identity_digest,
                policy_digest=policy_digest,
            )
        try:
            receipt = await self._provider.probe()
        except asyncio.CancelledError:
            raise
        except Exception:
            receipt = None
        ready = _attestation_matches(receipt, identity_digest=self._identity_digest)
        return BackendStatus(
            configured=True,
            ready=ready is True,
            backend_id=HYPERV_MEDIA_BACKEND_ID,
            identity_digest=self._identity_digest,
            policy_digest=policy_digest,
        )

    async def execute(self, request: CapabilityRequest) -> BackendExecution:
        if not isinstance(request, CapabilityRequest):
            raise CapabilityContractError("request must be a CapabilityRequest")
        if self._provider is None or self._closed:
            raise CapabilityUnavailableError("Hyper-V managed backend is unavailable")
        if not _attestation_matches(self._provider.attestation, identity_digest=self._identity_digest):
            raise CapabilityUnavailableError("Hyper-V media policy attestation is unavailable")
        instruction = _instruction_for(request)
        try:
            result = await self._provider.inspect(request.payload.source_url, instruction)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CapabilityUnavailableError("Hyper-V media provider failed safely") from None
        if not isinstance(result, MediaInspectionResult):
            raise CapabilityUnavailableError("Hyper-V media provider returned an invalid result")
        if not _attestation_matches(
            self._provider.attestation,
            identity_digest=self._identity_digest,
            execution=True,
        ):
            raise CapabilityUnavailableError("Hyper-V media policy changed during execution")
        # A successful provider return is only possible after the existing v2
        # transport validated cleanup_confirmed=true.
        self._cleanup_confirmed_for = request.request_digest
        artifact_kind = _artifact_kind_for(request.capability)
        if not _evidence_matches(request.capability, result.text):
            raise CapabilityUnavailableError("Hyper-V media evidence did not match the requested intent")
        return BackendExecution(
            request_digest=request.request_digest,
            binding=request.binding,
            backend_id=HYPERV_MEDIA_BACKEND_ID,
            identity_digest=self._identity_digest,
            policy_digest=request.policy_digest,
            artifact_kind=artifact_kind,
            text=result.text,
            # HyperVMediaInspectionProvider only returns after the v2 transport
            # verifies cleanup_confirmed=true and the fixed worker identity.
            cleanup_confirmed=True,
        )

    async def cleanup(self, request: CapabilityRequest, reason: CleanupReason) -> BackendCleanupReceipt:
        if not isinstance(request, CapabilityRequest) or not isinstance(reason, CleanupReason):
            raise CapabilityContractError("cleanup request is invalid")
        if self._provider is None:
            raise CapabilityUnavailableError("Hyper-V managed backend is unavailable")
        try:
            await self._provider.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise CapabilityUnavailableError("Hyper-V provider cleanup failed safely") from None
        self._closed = True
        if self._cleanup_confirmed_for != request.request_digest:
            raise CapabilityCleanupError("Hyper-V guest workspace cleanup is unconfirmed")
        return BackendCleanupReceipt(
            request_digest=request.request_digest,
            backend_id=HYPERV_MEDIA_BACKEND_ID,
            identity_digest=self._identity_digest,
            worker_terminated=True,
            workspace_destroyed=True,
        )


def _instruction_for(request: CapabilityRequest) -> str:
    if request.capability is CapabilityKind.MEDIA_INSPECTION:
        assert request.payload.instruction is not None
        return request.payload.instruction
    if request.capability is CapabilityKind.SUBTITLE_EXTRACTION:
        return _SUBTITLE_INSTRUCTION
    if request.capability is CapabilityKind.THUMBNAIL_OCR:
        return _OCR_INSTRUCTION
    raise CapabilityContractError("managed media capability is invalid")


def _artifact_kind_for(capability: CapabilityKind) -> ArtifactKind:
    return {
        CapabilityKind.MEDIA_INSPECTION: ArtifactKind.INSPECTION_TEXT,
        CapabilityKind.SUBTITLE_EXTRACTION: ArtifactKind.SUBTITLE_TEXT,
        CapabilityKind.THUMBNAIL_OCR: ArtifactKind.OCR_TEXT,
    }[capability]


def _evidence_matches(capability: CapabilityKind, text: str) -> bool:
    if capability is CapabilityKind.MEDIA_INSPECTION:
        return True
    expected = "subtitles" if capability is CapabilityKind.SUBTITLE_EXTRACTION else "thumbnail_ocr"
    lines = text.splitlines()
    return len(lines) >= 5 and lines[4].strip().endswith(f": {expected}")


def _attestation_matches(
    value: object,
    *,
    identity_digest: str,
    execution: bool = False,
) -> bool:
    expected_type = HyperVMediaExecutionResult if execution else HyperVMediaProbeResult
    return (
        isinstance(value, expected_type)
        and value.cleanup_confirmed is True
        and value.identity_digest == identity_digest
        and value.effective_policy_revision == HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION
        and value.effective_policy_digest == HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST
        and (not isinstance(value, HyperVMediaProbeResult) or value.ready is True)
    )


__all__ = [
    "HYPERV_MEDIA_BACKEND_ID",
    "HyperVMediaManagedBackend",
    "HyperVMediaProvider",
]
