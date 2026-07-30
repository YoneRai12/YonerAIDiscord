"""Private Search Fabric gateway service backed by a dedicated SearXNG service."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from typing import Any, Protocol

from aiohttp import web

from .contracts import (
    MAX_SEARCH_JSON_BYTES,
    SearchFabricContractError,
    SearchFabricJsonCodec,
    SearchIntent,
    SearchResultV1,
)
from .searxng import (
    AiohttpSearxngTransport,
    SEARXNG_BACKEND_ID,
    SEARXNG_INTERNAL_ORIGIN,
    SearxngDiscoveryError,
    SearxngSearchAdapter,
    SearxngSearchQuery,
)


_ALLOWED_BINDS = frozenset({"127.0.0.1", "::1", "0.0.0.0"})


class SearchGatewayBackend(Protocol):
    async def health(self) -> bool: ...

    async def search(self, query: SearxngSearchQuery) -> SearchResultV1: ...


class SearchGatewayServer:
    def __init__(self, backend: SearchGatewayBackend) -> None:
        if not callable(getattr(backend, "health", None)) or not callable(getattr(backend, "search", None)):
            raise TypeError("backend must implement the Search Gateway backend contract")
        self._backend = backend

    def create_application(self) -> web.Application:
        application = web.Application(
            client_max_size=MAX_SEARCH_JSON_BYTES,
            middlewares=[_fixed_error_middleware],
        )
        application.router.add_get("/healthz", self._health)
        application.router.add_post("/v1/search", self._search)
        application.router.add_post("/v1/search/compat", self._search_compat)
        application.on_response_prepare.append(_add_security_headers)
        return application

    async def _health(self, _request: web.Request) -> web.Response:
        try:
            ready = await self._backend.health() is True
        except asyncio.CancelledError:
            raise
        except Exception:
            ready = False
        body = _canonical_json(
            {
                "schema": "yonerai.search-health.v1",
                "backend_id": SEARXNG_BACKEND_ID,
                "ready": ready,
            }
        )
        return web.Response(
            status=200 if ready else 503,
            body=body,
            content_type="application/json",
        )

    async def _search(self, request: web.Request) -> web.Response:
        result = await self._execute_search(request)
        return web.Response(
            body=SearchFabricJsonCodec.encode_result(result),
            content_type="application/json",
        )

    async def _search_compat(self, request: web.Request) -> web.Response:
        """Expose only the existing exact provider-neutral sources projection."""

        result = await self._execute_search(request)
        return web.Response(
            body=_canonical_json(result.to_compat_mapping()),
            content_type="application/json",
        )

    async def _execute_search(self, request: web.Request) -> SearchResultV1:
        if request.content_type.casefold() != "application/json":
            raise web.HTTPUnsupportedMediaType(text="unsupported media type")
        if request.content_length is not None and request.content_length > MAX_SEARCH_JSON_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_SEARCH_JSON_BYTES,
                actual_size=request.content_length,
            )
        body = await request.read()
        query = _decode_request(body)
        try:
            result = await self._backend.search(query)
        except asyncio.CancelledError:
            raise
        except (SearxngDiscoveryError, TimeoutError):
            raise web.HTTPServiceUnavailable(text="search backend unavailable") from None
        except Exception:
            raise web.HTTPServiceUnavailable(text="search backend unavailable") from None
        return result


def _decode_request(body: bytes) -> SearxngSearchQuery:
    if type(body) is not bytes or not 1 <= len(body) <= MAX_SEARCH_JSON_BYTES:
        raise web.HTTPBadRequest(text="invalid search request")
    try:
        document = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, SearchFabricContractError):
        raise web.HTTPBadRequest(text="invalid search request") from None
    expected = {
        "schema",
        "request_id",
        "query",
        "query_digest",
        "intent",
        "language",
        "limit",
    }
    if not isinstance(document, dict) or set(document) != expected:
        raise web.HTTPBadRequest(text="invalid search request")
    if document["schema"] != "yonerai.search-request.v1":
        raise web.HTTPBadRequest(text="invalid search request")
    try:
        encoded = SearchFabricJsonCodec.encode_request(
            request_id=document["request_id"],
            query=document["query"],
            intent=SearchIntent(document["intent"]),
            language=document["language"],
            limit=document["limit"],
        )
        canonical = json.loads(encoded.decode("utf-8"))
        if canonical["query_digest"] != document["query_digest"]:
            raise ValueError("query digest mismatch")
        return SearxngSearchQuery(
            request_id=document["request_id"],
            query=document["query"],
            query_digest=document["query_digest"],
            intent=SearchIntent(document["intent"]),
            language=document["language"],
            limit=document["limit"],
        )
    except (TypeError, ValueError, SearchFabricContractError):
        raise web.HTTPBadRequest(text="invalid search request") from None


@web.middleware
async def _fixed_error_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException as exc:
        status = exc.status
        if status == 413:
            message = "request too large"
        elif status == 415:
            message = "unsupported media type"
        elif status == 503:
            message = "search backend unavailable"
        elif status in {404, 405}:
            message = "not found"
        else:
            message = "invalid search request"
        return web.json_response({"error": message}, status=status)
    except asyncio.CancelledError:
        raise
    except Exception:
        return web.json_response({"error": "search gateway failure"}, status=503)


async def _add_security_headers(
    _request: web.Request,
    response: web.StreamResponse,
) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SearchFabricContractError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise SearchFabricContractError("non-finite JSON numbers are forbidden")


def build_application(*, searxng_origin: str = SEARXNG_INTERNAL_ORIGIN) -> web.Application:
    if searxng_origin != SEARXNG_INTERNAL_ORIGIN:
        raise ValueError("SearXNG origin is code-owned")
    backend = SearxngSearchAdapter(AiohttpSearxngTransport())
    return SearchGatewayServer(backend).create_application()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the private YonerAI Search Fabric gateway")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--searxng-origin", default=SEARXNG_INTERNAL_ORIGIN)
    arguments = parser.parse_args(argv)
    if arguments.bind not in _ALLOWED_BINDS:
        parser.error("bind must be a code-owned local/container address")
    if not 1 <= arguments.port <= 65_535:
        parser.error("port is outside the TCP range")
    try:
        application = build_application(searxng_origin=arguments.searxng_origin)
    except ValueError as exc:
        parser.error(str(exc))
    web.run_app(
        application,
        host=arguments.bind,
        port=arguments.port,
        access_log=None,
        print=None,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "SearchGatewayBackend",
    "SearchGatewayServer",
    "build_application",
    "main",
]
