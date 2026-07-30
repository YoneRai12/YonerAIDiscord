"""Real loopback-only HTTP transport for the Search Fabric gateway."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from typing import Any

import aiohttp

from .contracts import MAX_SEARCH_JSON_BYTES
from .gateway import LoopbackSearchPostRequest, LoopbackSearchPostResponse


class LoopbackSearchTransportError(RuntimeError):
    """A content-free loopback transport failure."""


class _RejectDnsResolver(aiohttp.abc.AbstractResolver):
    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[aiohttp.abc.ResolveResult]:
        del host, port, family
        raise LoopbackSearchTransportError("DNS resolution is forbidden for loopback search")

    async def close(self) -> None:
        return None


class AiohttpLoopbackSearchPostTransport:
    """POST to one literal loopback endpoint without proxy, redirect, or DNS."""

    def __init__(
        self,
        *,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        self._session_factory = session_factory

    async def post(self, request: LoopbackSearchPostRequest) -> LoopbackSearchPostResponse:
        if not isinstance(request, LoopbackSearchPostRequest):
            raise TypeError("request must be a LoopbackSearchPostRequest")
        host = request.host.value
        authority = f"[{host}]" if ":" in host else host
        url = f"http://{authority}:{request.port}{request.path}"
        timeout = aiohttp.ClientTimeout(total=float(request.timeout_seconds))
        connector = aiohttp.TCPConnector(
            resolver=_RejectDnsResolver(),
            family=socket.AF_UNSPEC,
            limit=1,
            use_dns_cache=False,
        )
        try:
            async with self._session_factory(
                timeout=timeout,
                connector=connector,
                trust_env=False,
            ) as session:
                async with session.post(
                    url,
                    data=request.body,
                    headers={
                        "accept": "application/json",
                        "content-type": "application/json",
                    },
                    allow_redirects=False,
                    proxy=None,
                ) as response:
                    body = await _read_bounded(response, request.max_response_bytes)
                    return LoopbackSearchPostResponse(
                        status=response.status,
                        body=body,
                        media_type=response.headers.get("content-type", ""),
                    )
        except asyncio.CancelledError:
            raise
        except LoopbackSearchTransportError:
            raise
        except (TimeoutError, aiohttp.ClientError, OSError):
            raise LoopbackSearchTransportError("loopback Search Fabric request failed") from None
        finally:
            if not connector.closed:
                await connector.close()


async def _read_bounded(response: Any, maximum: int) -> bytes:
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1_024 <= maximum <= MAX_SEARCH_JSON_BYTES:
        raise ValueError("maximum is outside the Search Fabric response limit")
    chunks: list[bytes] = []
    consumed = 0
    async for chunk in response.content.iter_chunked(16 * 1024):
        consumed += len(chunk)
        if consumed > maximum:
            raise LoopbackSearchTransportError("loopback Search Fabric response exceeded the byte limit")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


__all__ = [
    "AiohttpLoopbackSearchPostTransport",
    "LoopbackSearchTransportError",
]
