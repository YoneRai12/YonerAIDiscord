from __future__ import annotations

from typing import Any

import pytest

from yonerai_discord.browser_sandbox import (
    BROWSER_ISOLATION_CONTRACT,
    BrowserAdapterContractError,
    BrowserIsolationContract,
    BrowserNetworkGuard,
    BrowserOutput,
    BrowserOutputKind,
    BrowserResourceLimitError,
    BrowserSandboxLimits,
    BrowserSandboxPolicy,
    BrowserSandboxService,
    BrowserSandboxUnavailableError,
    BrowserSessionRequest,
    BrowserSessionResult,
    ExtractText,
    Navigate,
    Screenshot,
    StaticDnsResolver,
)


def _policy(*, max_total_bytes: int = 25 * 1024 * 1024) -> BrowserSandboxPolicy:
    return BrowserSandboxPolicy(
        resolver=StaticDnsResolver({"example.com": ("93.184.216.34",)}),
        allowed_domains=("example.com",),
        limits=BrowserSandboxLimits(max_total_bytes=max_total_bytes),
    )


def test_fixed_isolation_contract_cannot_request_a_weaker_runtime() -> None:
    contract = BROWSER_ISOLATION_CONTRACT

    assert contract.profile_mode == "ephemeral"
    assert contract.downloads_enabled is False
    assert contract.uploads_enabled is False
    assert contract.script_evaluation_enabled is False
    assert contract.developer_protocol_enabled is False
    assert contract.context_reuse_enabled is False
    with pytest.raises(TypeError):
        BrowserIsolationContract(profile_mode="persistent")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_unconfigured_service_is_fail_closed() -> None:
    service = BrowserSandboxService(policy=_policy())

    assert service.configured is False
    with pytest.raises(BrowserSandboxUnavailableError, match="not configured"):
        await service.execute(BrowserSessionRequest(actions=(Navigate("https://example.com/"),)))


@pytest.mark.asyncio
async def test_adapter_receives_fixed_contract_and_guard_and_returns_typed_outputs() -> None:
    captured: dict[str, Any] = {}

    class FakeAdapter:
        async def execute(
            self,
            request: BrowserSessionRequest,
            *,
            network_guard: BrowserNetworkGuard,
            isolation_contract: BrowserIsolationContract,
        ) -> BrowserSessionResult:
            captured["contract"] = isolation_contract
            captured["authorized"] = network_guard.authorize_request(request.actions[0].url)  # type: ignore[union-attr]
            network_guard.consume_bytes(256)
            return BrowserSessionResult(
                outputs=(
                    BrowserOutput(
                        step_index=1,
                        kind=BrowserOutputKind.SCREENSHOT,
                        data=b"png",
                        media_type="image/png",
                    ),
                    BrowserOutput(
                        step_index=2,
                        kind=BrowserOutputKind.TEXT,
                        data="本文".encode(),
                        media_type="text/plain; charset=utf-8",
                    ),
                )
            )

    service = BrowserSandboxService(policy=_policy(), adapter=FakeAdapter())
    result = await service.execute(
        BrowserSessionRequest(
            actions=(
                Navigate("https://example.com/"),
                Screenshot(),
                ExtractText(),
            )
        )
    )

    assert service.configured is True
    assert captured["contract"] is BROWSER_ISOLATION_CONTRACT
    assert captured["authorized"].hostname == "example.com"
    assert [output.kind for output in result.outputs] == [BrowserOutputKind.SCREENSHOT, BrowserOutputKind.TEXT]


@pytest.mark.asyncio
async def test_result_must_be_typed_and_reference_matching_output_actions() -> None:
    class InvalidTypeAdapter:
        async def execute(self, *_: object, **__: object) -> object:
            return {"text": "unsafe"}

    service = BrowserSandboxService(policy=_policy(), adapter=InvalidTypeAdapter())  # type: ignore[arg-type]
    with pytest.raises(BrowserAdapterContractError, match="invalid result"):
        await service.execute(BrowserSessionRequest(actions=(Screenshot(),)))

    class WrongStepAdapter:
        async def execute(self, *_: object, **__: object) -> BrowserSessionResult:
            return BrowserSessionResult(
                outputs=(
                    BrowserOutput(
                        step_index=1,
                        kind=BrowserOutputKind.SCREENSHOT,
                        data=b"png",
                        media_type="image/png",
                    ),
                )
            )

    service = BrowserSandboxService(policy=_policy(), adapter=WrongStepAdapter())
    with pytest.raises(BrowserAdapterContractError, match="unknown step"):
        await service.execute(BrowserSessionRequest(actions=(Screenshot(),)))

    class WrongKindAdapter:
        async def execute(self, *_: object, **__: object) -> BrowserSessionResult:
            return BrowserSessionResult(
                outputs=(
                    BrowserOutput(
                        step_index=0,
                        kind=BrowserOutputKind.TEXT,
                        data=b"text",
                        media_type="text/plain; charset=utf-8",
                    ),
                )
            )

    service = BrowserSandboxService(policy=_policy(), adapter=WrongKindAdapter())
    with pytest.raises(BrowserAdapterContractError, match="does not match"):
        await service.execute(BrowserSessionRequest(actions=(Screenshot(),)))


@pytest.mark.asyncio
async def test_remote_and_output_bytes_share_one_hard_limit() -> None:
    class LargeAdapter:
        async def execute(
            self,
            request: BrowserSessionRequest,
            *,
            network_guard: BrowserNetworkGuard,
            isolation_contract: BrowserIsolationContract,
        ) -> BrowserSessionResult:
            del request, isolation_contract
            network_guard.consume_bytes(1)
            return BrowserSessionResult(
                outputs=(
                    BrowserOutput(
                        step_index=0,
                        kind=BrowserOutputKind.SCREENSHOT,
                        data=b"x" * (64 * 1024),
                        media_type="image/png",
                    ),
                )
            )

    service = BrowserSandboxService(policy=_policy(max_total_bytes=64 * 1024), adapter=LargeAdapter())
    with pytest.raises(BrowserResourceLimitError, match="byte budget exceeded"):
        await service.execute(BrowserSessionRequest(actions=(Screenshot(),)))
