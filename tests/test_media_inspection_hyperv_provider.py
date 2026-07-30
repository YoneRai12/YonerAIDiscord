from __future__ import annotations

from pathlib import Path

import pytest

from yonerai_discord.modules.media_inspection.domain import MediaInspectionUnavailableError
from yonerai_discord.modules.media_inspection.hyperv_contract import (
    HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
    HYPERV_MEDIA_IDENTITY_DIGEST,
    HyperVMediaExecutionResult,
    HyperVMediaProbeResult,
)
from yonerai_discord.modules.media_inspection.hyperv_provider import (
    HyperVMediaInspectionProvider,
)


class _Transport:
    def __init__(
        self,
        *,
        ready: bool = True,
        fail_inspect: bool = False,
        execution_policy_digest: str = HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    ) -> None:
        self.probe_ready = ready
        self.fail_inspect = fail_inspect
        self.execution_policy_digest = execution_policy_digest
        self.inspections: list[dict[str, str]] = []
        self.closed = False

    async def probe(self) -> HyperVMediaProbeResult:
        return HyperVMediaProbeResult(
            self.probe_ready,
            True,
            HYPERV_MEDIA_IDENTITY_DIGEST,
            HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
            HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
        )

    async def inspect(self, *, url: str, instruction: str) -> HyperVMediaExecutionResult:
        self.inspections.append({"url": url, "instruction": instruction})
        if self.fail_inspect:
            raise RuntimeError("worker failed")
        return HyperVMediaExecutionResult(
            "ローカル解析",
            True,
            HYPERV_MEDIA_IDENTITY_DIGEST,
            HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
            self.execution_policy_digest,
        )

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_provider_is_local_and_sends_only_canonical_input(tmp_path: Path) -> None:
    transport = _Transport()
    provider = HyperVMediaInspectionProvider(
        project_root=tmp_path,
        timeout_seconds=30,
        transport=transport,  # type: ignore[arg-type]
    )

    assert provider.requires_external_ai_consent is False
    assert (await provider.probe()).ready is True
    result = await provider.inspect(
        "https://youtu.be/ABCDEFGHIJK?feature=share",
        "内容を説明して",
    )

    assert result.text == "ローカル解析"
    assert transport.inspections == [
        {
            "url": "https://www.youtube.com/watch?v=ABCDEFGHIJK",
            "instruction": "内容を説明して",
        }
    ]
    await provider.close()
    assert transport.closed is True


@pytest.mark.asyncio
async def test_provider_fails_closed_after_worker_failure(tmp_path: Path) -> None:
    transport = _Transport(fail_inspect=True)
    provider = HyperVMediaInspectionProvider(
        project_root=tmp_path,
        timeout_seconds=30,
        transport=transport,  # type: ignore[arg-type]
    )
    assert (await provider.probe()).ready is True

    with pytest.raises(MediaInspectionUnavailableError):
        await provider.inspect("https://youtu.be/ABCDEFGHIJK", "内容を説明して")

    assert provider.ready is False
    with pytest.raises(MediaInspectionUnavailableError):
        await provider.inspect("https://youtu.be/ABCDEFGHIJK", "再試行")


@pytest.mark.asyncio
async def test_provider_rejects_policy_drift_after_probe_without_returning_artifact(
    tmp_path: Path,
) -> None:
    transport = _Transport(execution_policy_digest="0" * 64)
    provider = HyperVMediaInspectionProvider(
        project_root=tmp_path,
        timeout_seconds=30,
        transport=transport,  # type: ignore[arg-type]
    )
    assert (await provider.probe()).ready is True

    with pytest.raises(MediaInspectionUnavailableError, match="policy changed"):
        await provider.inspect("https://youtu.be/ABCDEFGHIJK", "再試行")

    assert len(transport.inspections) == 1
    assert provider.ready is False
    assert provider.attestation is None
