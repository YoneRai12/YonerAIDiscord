from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .domain import (
    MAX_INSPECTION_INSTRUCTION_CHARS,
    MediaInspectionInputError,
    MediaInspectionResult,
    MediaInspectionUnavailableError,
)
from .hyperv_transport import HyperVMediaInspectionTransport
from .hyperv_contract import (
    HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
    HYPERV_MEDIA_IDENTITY_DIGEST,
    HyperVMediaExecutionResult,
    HyperVMediaProbeResult,
)
from .urls import canonicalize_youtube_url


_MAX_HYPERV_INSTRUCTION_CHARS = min(MAX_INSPECTION_INSTRUCTION_CHARS, 2_000)


class HyperVMediaInspectionProvider:
    """Local provider backed by the fixed Hyper-V forced-command worker."""

    requires_external_ai_consent = False

    def __init__(
        self,
        *,
        project_root: str | Path,
        timeout_seconds: float,
        transport: HyperVMediaInspectionTransport | None = None,
    ) -> None:
        self._transport = transport or HyperVMediaInspectionTransport(
            project_root=project_root,
            timeout_seconds=timeout_seconds,
        )
        self._ready = False
        self._attestation: HyperVMediaProbeResult | HyperVMediaExecutionResult | None = None
        self._closing = False
        self._semaphore = asyncio.Semaphore(1)

    @property
    def ready(self) -> bool:
        return self._ready and not self._closing

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def attestation(self) -> HyperVMediaProbeResult | HyperVMediaExecutionResult | None:
        return self._attestation

    async def probe(self) -> HyperVMediaProbeResult:
        if self._closing:
            raise MediaInspectionUnavailableError("Hyper-V media inspection is unavailable")
        async with self._semaphore:
            try:
                result = await self._transport.probe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ready = False
                self._attestation = None
                raise MediaInspectionUnavailableError("Hyper-V media inspection probe failed safely") from exc
            if not _attestation_matches(result):
                self._ready = False
                self._attestation = None
                raise MediaInspectionUnavailableError("Hyper-V media inspection policy attestation is invalid")
            self._attestation = result
            self._ready = result.ready is True and result.cleanup_confirmed is True
            return result

    async def inspect(self, url: str, instruction: str) -> MediaInspectionResult:
        if not self.ready:
            raise MediaInspectionUnavailableError("Hyper-V media inspection is unavailable")
        canonical_url = _worker_canonical_url(url)
        safe_instruction = _validate_instruction(instruction)
        async with self._semaphore:
            if not self.ready or not isinstance(self._attestation, HyperVMediaProbeResult):
                raise MediaInspectionUnavailableError("Hyper-V media inspection is unavailable")
            try:
                result = await self._transport.inspect(
                    url=canonical_url,
                    instruction=safe_instruction,
                )
            except asyncio.CancelledError:
                self._ready = False
                raise
            except Exception as exc:
                self._ready = False
                self._attestation = None
                raise MediaInspectionUnavailableError("Hyper-V media inspection failed safely") from exc
            if not _attestation_matches(result):
                self._ready = False
                self._attestation = None
                raise MediaInspectionUnavailableError("Hyper-V media inspection policy changed")
            self._attestation = result
        return MediaInspectionResult(result.text)

    def begin_close(self) -> None:
        self._closing = True
        self._ready = False
        self._attestation = None

    async def close(self) -> None:
        self.begin_close()
        await self._transport.close()


def _validate_instruction(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_HYPERV_INSTRUCTION_CHARS
        or "\x00" in value
    ):
        raise MediaInspectionInputError("inspection instruction is invalid")
    return value


def _worker_canonical_url(value: object) -> str:
    canonical = canonicalize_youtube_url(value)
    parsed = urlsplit(canonical)
    if parsed.hostname == "youtu.be":
        video_id = parsed.path.removeprefix("/")
    elif parsed.path.startswith("/shorts/"):
        video_id = parsed.path.removeprefix("/shorts/")
    else:
        values = parse_qs(parsed.query).get("v", ())
        video_id = values[0] if len(values) == 1 else ""
    if len(video_id) != 11:
        raise MediaInspectionInputError("video URL is invalid")
    return f"https://www.youtube.com/watch?v={video_id}"


def _attestation_matches(value: object) -> bool:
    return (
        isinstance(value, (HyperVMediaProbeResult, HyperVMediaExecutionResult))
        and value.cleanup_confirmed is True
        and value.identity_digest == HYPERV_MEDIA_IDENTITY_DIGEST
        and value.effective_policy_revision == HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION
        and value.effective_policy_digest == HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST
    )


__all__ = ["HyperVMediaInspectionProvider"]
