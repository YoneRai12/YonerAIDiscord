from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web

from yonerai_discord.search_fabric.composition import (
    AiohttpLoopbackSearchHealthProbe,
    LocalSearchFabricRuntime,
    SearchCompositionError,
    build_local_search_fabric_runtime,
)
from yonerai_discord.search_fabric.gateway import LoopbackAddress
from yonerai_discord.search_fabric.orchestrator import (
    SearchAuthorizationError,
    classify_search_intent,
    search_query_is_high_stakes,
)
from yonerai_discord.search_fabric.contracts import SearchIntent


def _settings(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "web_search_enabled": True,
        "web_search_backend": "yonerai_search_gateway",
        "yonerai_search_gateway_mode": "loopback",
        "yonerai_search_gateway_url": "http://127.0.0.1:8787",
        "search_allow_paid_fallback": False,
        "search_timeout_seconds": 12.0,
        "search_max_response_bytes": 512 * 1024,
        "search_fetch_max_bytes": 2 * 1024 * 1024,
        "search_max_results": 10,
        "search_max_fetches": 5,
        "search_cache_ttl_seconds": 1_800,
        "search_require_corroboration_for_high_stakes": True,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_code_owned_composition_accepts_only_fixed_loopback_profile() -> None:
    runtime = build_local_search_fabric_runtime(_settings())
    assert isinstance(runtime, LocalSearchFabricRuntime)
    assert tuple(
        adapter.provider_id
        for adapter in runtime._orchestrator._scholarly_metadata_adapters  # noqa: SLF001 - composition proof
    ) == ("crossref.public", "pubmed.public")
    assert runtime.ready is False
    assert build_local_search_fabric_runtime(_settings(web_search_enabled=False)) is None

    for changes in (
        {"yonerai_search_gateway_url": "http://localhost:8787"},
        {"yonerai_search_gateway_url": "http://127.0.0.1:8787/path"},
        {"yonerai_search_gateway_url": "https://127.0.0.1:8787"},
        {"search_allow_paid_fallback": True},
        {"search_cache_ttl_seconds": 0},
    ):
        with pytest.raises(SearchCompositionError):
            build_local_search_fabric_runtime(_settings(**changes))


@pytest.mark.asyncio
async def test_health_probe_requires_exact_bounded_health_contract() -> None:
    state = {"document": {"schema": "yonerai.search-health.v1", "backend_id": "searxng.local", "ready": True}}

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(state["document"])

    application = web.Application()
    application.router.add_get("/healthz", health)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None  # noqa: SLF001 - test-only ephemeral listener
    port = int(site._server.sockets[0].getsockname()[1])  # noqa: SLF001
    probe = AiohttpLoopbackSearchHealthProbe(
        host=LoopbackAddress.IPV4,
        port=port,
        timeout_seconds=2.0,
    )
    try:
        assert await probe.probe() is True
        state["document"] = {
            "schema": "yonerai.search-health.v1",
            "backend_id": "searxng.local",
            "ready": False,
        }
        assert await probe.probe() is False
        state["document"] = {
            "schema": "yonerai.search-health.v1",
            "backend_id": "searxng.local",
            "ready": True,
            "unexpected": True,
        }
        assert await probe.probe() is False
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("SearXNGの査読論文", SearchIntent.SCHOLARLY),
        ("公式仕様を確認", SearchIntent.OFFICIAL),
        ("GitHub releaseを確認", SearchIntent.CODE),
        ("今日の最新ニュース", SearchIntent.NEWS),
        ("富士山について", SearchIntent.GENERAL),
    ),
)
def test_search_intent_routing_is_code_owned_and_bounded(query: str, expected: SearchIntent) -> None:
    assert classify_search_intent(query) is expected


def test_high_stakes_detection_is_conservative_and_model_independent() -> None:
    assert search_query_is_high_stakes("医療に関する最新情報") is True
    assert search_query_is_high_stakes("一般的な製品紹介") is False
    with pytest.raises(ValueError):
        classify_search_intent("x" * 4_097)


@pytest.mark.asyncio
async def test_request_authorization_revoke_does_not_poison_global_search_readiness() -> None:
    runtime = build_local_search_fabric_runtime(_settings())
    assert isinstance(runtime, LocalSearchFabricRuntime)
    runtime._ready = True  # noqa: SLF001 - last successful code-owned health probe

    async def revoked() -> bool:
        return False

    with pytest.raises(SearchAuthorizationError):
        await runtime.search(
            "権限取消",
            request_id="search-auth-revoked",
            intent=SearchIntent.GENERAL,
            language="ja-JP",
            high_stakes=False,
            authorization_current=revoked,
        )

    assert runtime.ready is True
