from __future__ import annotations

import asyncio

import pytest

from yonerai_discord.modules.yonerai import (
    BoundaryState,
    ReadinessOutcome,
    RemoteReadiness,
    YonerAIRuntimeConfig,
    YonerAIStatusService,
    YonerAIResponseLimitError,
    read_bounded_response,
    render_status,
)


class Gateway:
    def __init__(self, outcome: ReadinessOutcome = ReadinessOutcome.HEALTHY) -> None:
        self.outcome = outcome
        self.calls = []

    async def probe(self, budget):
        self.calls.append(budget)
        return RemoteReadiness(self.outcome)


async def chunks(*values: bytes):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_disabled_boundary_never_invokes_gateway() -> None:
    gateway = Gateway()
    service = YonerAIStatusService(YonerAIRuntimeConfig(), gateway)

    result = await service.health()

    assert result.state is BoundaryState.DISABLED
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_remote", "remote_opt_in"),
    [(False, False), (True, False), (False, True)],
)
async def test_incomplete_remote_opt_in_never_invokes_gateway(
    allow_remote: bool,
    remote_opt_in: bool,
) -> None:
    gateway = Gateway()
    service = YonerAIStatusService(
        YonerAIRuntimeConfig(
            enabled=True,
            allow_remote=allow_remote,
            remote_status_opt_in=remote_opt_in,
        ),
        gateway,
    )

    result = await service.health()

    expected = (
        BoundaryState.LOCAL_ONLY if not allow_remote and not remote_opt_in else BoundaryState.REMOTE_OPT_IN_INCOMPLETE
    )
    assert result.state is expected
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_official_contract_pending_has_no_external_call() -> None:
    service = YonerAIStatusService(YonerAIRuntimeConfig(enabled=True, allow_remote=True, remote_status_opt_in=True))

    assert (await service.health()).state is BoundaryState.CONTRACT_PENDING


@pytest.mark.asyncio
async def test_injected_read_only_gateway_receives_strict_budget() -> None:
    gateway = Gateway(ReadinessOutcome.DEGRADED)
    service = YonerAIStatusService(
        YonerAIRuntimeConfig(
            enabled=True,
            allow_remote=True,
            remote_status_opt_in=True,
            timeout_seconds=1.25,
            max_response_bytes=4_096,
        ),
        gateway,
    )

    result = await service.health()

    assert result.state is BoundaryState.DEGRADED
    assert gateway.calls[0].timeout_seconds == 1.25
    assert gateway.calls[0].max_response_bytes == 4_096


@pytest.mark.asyncio
async def test_gateway_timeout_is_collapsed_without_exception_or_body() -> None:
    class SlowGateway:
        async def probe(self, budget):
            await asyncio.sleep(1)
            return RemoteReadiness(ReadinessOutcome.HEALTHY)

    service = YonerAIStatusService(
        YonerAIRuntimeConfig(
            enabled=True,
            allow_remote=True,
            remote_status_opt_in=True,
            timeout_seconds=0.25,
        ),
        SlowGateway(),
    )
    result = await service.health()
    assert result.state is BoundaryState.UNAVAILABLE


@pytest.mark.asyncio
async def test_bounded_reader_rejects_declared_and_streamed_oversize_without_body() -> None:
    secret_body = b"private-upstream-body"
    with pytest.raises(YonerAIResponseLimitError) as declared:
        await read_bounded_response(
            chunks(secret_body),
            max_response_bytes=4,
            declared_length=len(secret_body),
        )
    assert secret_body.decode() not in str(declared.value)

    with pytest.raises(YonerAIResponseLimitError) as streamed:
        await read_bounded_response(chunks(b"123", secret_body), max_response_bytes=4)
    assert secret_body.decode() not in str(streamed.value)


@pytest.mark.asyncio
async def test_bounded_reader_accepts_payload_at_limit() -> None:
    assert await read_bounded_response(chunks(b"12", b"34"), max_response_bytes=4) == b"1234"


def test_rendered_status_cannot_include_token_url_query_or_upstream_body() -> None:
    token = "test_fixture_secret_token_value"
    config = YonerAIRuntimeConfig(
        enabled=True,
        allow_remote=True,
        remote_status_opt_in=True,
        auth_token=token,
    )
    text = render_status(YonerAIStatusService(config).status())

    assert token not in text
    assert "http" not in text.lower()
    assert "?" not in text
    assert "設定済み" in text
