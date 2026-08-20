from __future__ import annotations

import asyncio
import ipaddress
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp

from .config import YonerAIRuntimeConfig
from .contract import ProbeBudget, ReadinessOutcome, RemoteReadiness
from .transport import read_bounded_response


class _Settings(Protocol):
    yonerai_core_origin: str


@dataclass(frozen=True, slots=True)
class AiohttpYonerAIReadinessGateway:
    _origin: str = field(repr=False)
    _bearer_token: str = field(repr=False)

    def __init__(self, origin: str, bearer_token: str) -> None:
        object.__setattr__(self, "_origin", _validated_origin(origin))
        object.__setattr__(self, "_bearer_token", _validated_token(bearer_token, origin=self._origin))

    def __repr__(self) -> str:
        return "AiohttpYonerAIReadinessGateway()"

    @property
    def local_only(self) -> bool:
        return _origin_is_loopback(self._origin)

    async def probe(self, budget: ProbeBudget) -> RemoteReadiness:
        if not isinstance(budget, ProbeBudget):
            raise TypeError("budget must be a ProbeBudget")
        if (
            isinstance(budget.timeout_seconds, bool)
            or not isinstance(budget.timeout_seconds, (int, float))
            or not math.isfinite(budget.timeout_seconds)
            or not 0.25 <= float(budget.timeout_seconds) <= 15.0
            or isinstance(budget.max_response_bytes, bool)
            or not isinstance(budget.max_response_bytes, int)
            or not 1_024 <= budget.max_response_bytes <= 262_144
        ):
            raise ValueError("readiness budget is invalid")
        headers = {"Accept": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        timeout = aiohttp.ClientTimeout(total=float(budget.timeout_seconds))
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self._origin}/health",
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200 or not _json_content_type(response.headers.get("Content-Type", "")):
                        raise ValueError("invalid health response")
                    declared_length = _declared_length(response.headers.get("Content-Length"))
                    chunks = response.content.iter_chunked(min(8192, budget.max_response_bytes))
                    body = await read_bounded_response(
                        chunks,
                        max_response_bytes=budget.max_response_bytes,
                        declared_length=declared_length,
                    )
                    payload = _health_payload(body)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OSError("YonerAI readiness probe failed") from None
        return RemoteReadiness(ReadinessOutcome.HEALTHY if payload["ok"] is True else ReadinessOutcome.UNAVAILABLE)


def build_yonerai_readiness_gateway(
    settings: _Settings,
    config: YonerAIRuntimeConfig,
) -> AiohttpYonerAIReadinessGateway | None:
    if not isinstance(config, YonerAIRuntimeConfig) or not config.enabled:
        return None
    try:
        origin = settings.yonerai_core_origin
    except Exception:
        return None
    if not isinstance(origin, str) or not origin:
        return None
    try:
        normalized = _validated_origin(origin)
    except (TypeError, ValueError):
        return None
    local = _origin_is_loopback(normalized)
    if not local and (not config.remote_permitted or not config.token_configured):
        return None
    try:
        return AiohttpYonerAIReadinessGateway(normalized, config.auth_token)
    except (TypeError, ValueError):
        return None


def _validated_origin(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("origin is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin is invalid")
    normalized = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.scheme == "http" and not _origin_is_loopback(normalized):
        raise ValueError("plain HTTP origin must be loopback")
    return normalized


def _origin_is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(urlsplit(value).hostname or "").is_loopback
    except ValueError:
        return False


def _validated_token(value: object, *, origin: str) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) > 4_096:
        raise ValueError("token is invalid")
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError("token is invalid")
    if not value and not _origin_is_loopback(origin):
        raise ValueError("remote readiness requires authorization")
    return value


def _declared_length(value: object) -> int | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ValueError("invalid content length")
    return int(value)


def _health_payload(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _reject_non_finite(payload)
    except (UnicodeDecodeError, TypeError, ValueError):
        raise ValueError("invalid health response") from None
    if not isinstance(payload, dict) or set(payload) not in ({"ok"}, {"ok", "distribution_node"}):
        raise ValueError("invalid health response")
    if type(payload["ok"]) is not bool:
        raise ValueError("invalid health response")
    distribution = payload.get("distribution_node")
    if distribution is not None:
        if not isinstance(distribution, dict) or set(distribution) != {"profile", "verified_release"}:
            raise ValueError("invalid health response")
        for value in distribution.values():
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 128
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError("invalid health response")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def _reject_non_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite value")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)


def _json_content_type(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = [part.strip().casefold() for part in value.split(";")]
    return (
        bool(parts)
        and parts[0] == "application/json"
        and all(part in {"charset=utf-8", 'charset="utf-8"'} for part in parts[1:])
    )


__all__ = ["AiohttpYonerAIReadinessGateway", "build_yonerai_readiness_gateway"]
