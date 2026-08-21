from __future__ import annotations

from types import SimpleNamespace

import pytest

from yonerai_discord.modules.yonerai import (
    ProbeBudget,
    ReadinessOutcome,
    YonerAIRuntimeConfig,
)
from yonerai_discord.modules.yonerai.readiness import (
    AiohttpYonerAIReadinessGateway,
    build_yonerai_readiness_gateway,
)


class _Content:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.chunk_sizes: list[int] = []

    def iter_chunked(self, size: int):
        async def iterate():
            self.chunk_sizes.append(size)
            for chunk in self._chunks:
                yield chunk

        return iterate()


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.content = _Content((body,))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.gets: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> _Response:
        self.gets.append((url, kwargs))
        return self.response


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    monkeypatch.setattr(
        "yonerai_discord.modules.yonerai.readiness.aiohttp.ClientSession",
        lambda **_kwargs: session,
    )


@pytest.mark.asyncio
async def test_loopback_health_contract_is_bounded_and_does_not_require_remote_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b'{"ok":true,"distribution_node":{"profile":"private","verified_release":"v1"}}')
    session = _Session(response)
    _patch_session(monkeypatch, session)
    gateway = AiohttpYonerAIReadinessGateway("http://127.0.0.1:8001", "")

    result = await gateway.probe(ProbeBudget(timeout_seconds=1.0, max_response_bytes=1024))

    assert result.outcome is ReadinessOutcome.HEALTHY
    url, kwargs = session.gets[0]
    assert url == "http://127.0.0.1:8001/health"
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"] == {"Accept": "application/json"}
    assert response.content.chunk_sizes == [1024]


@pytest.mark.asyncio
async def test_remote_health_uses_bearer_and_invalid_shapes_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(_Response(b'{"ok":false}'))
    _patch_session(monkeypatch, session)
    gateway = AiohttpYonerAIReadinessGateway("https://core.example", "private-token")

    result = await gateway.probe(ProbeBudget(timeout_seconds=1.0, max_response_bytes=1024))

    assert result.outcome is ReadinessOutcome.UNAVAILABLE
    assert session.gets[0][1]["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer private-token",
    }
    assert "private-token" not in repr(gateway)


@pytest.mark.parametrize(
    "body",
    (
        b'{"ok":true,"extra":true}',
        b'{"ok":true,"ok":false}',
        b'{"ok":true,"distribution_node":{"profile":1,"verified_release":"v1"}}',
    ),
)
@pytest.mark.asyncio
async def test_health_rejects_ambiguous_or_widened_contracts(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    session = _Session(_Response(body))
    _patch_session(monkeypatch, session)
    gateway = AiohttpYonerAIReadinessGateway("http://127.0.0.1:8001", "")

    with pytest.raises(OSError, match="readiness probe failed"):
        await gateway.probe(ProbeBudget(timeout_seconds=1.0, max_response_bytes=1024))


def test_readiness_composition_allows_local_and_requires_all_remote_gates() -> None:
    local_settings = SimpleNamespace(
        yonerai_core_origin="http://127.0.0.1:8001",
        yonerai_auth_token="",
    )
    local_config = YonerAIRuntimeConfig(enabled=True)
    assert isinstance(
        build_yonerai_readiness_gateway(local_settings, local_config),
        AiohttpYonerAIReadinessGateway,
    )

    remote_settings = SimpleNamespace(
        yonerai_core_origin="https://core.example",
        yonerai_auth_token="private-token",
    )
    assert build_yonerai_readiness_gateway(remote_settings, local_config) is None
    assert isinstance(
        build_yonerai_readiness_gateway(
            remote_settings,
            YonerAIRuntimeConfig(
                enabled=True,
                allow_remote=True,
                remote_status_opt_in=True,
                auth_token="private-token",
            ),
        ),
        AiohttpYonerAIReadinessGateway,
    )


def test_localhost_readiness_uses_the_same_tokenless_local_contract() -> None:
    gateway = build_yonerai_readiness_gateway(
        SimpleNamespace(
            yonerai_core_origin="http://localhost:8001",
            yonerai_auth_token="",
        ),
        YonerAIRuntimeConfig(enabled=True),
    )

    assert isinstance(gateway, AiohttpYonerAIReadinessGateway)
    assert gateway.local_only is True
    assert gateway._origin == "http://localhost:8001"
