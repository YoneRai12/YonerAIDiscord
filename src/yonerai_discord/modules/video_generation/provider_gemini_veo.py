from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlparse

import aiohttp

from yonerai_discord.provider_registry import (
    HealthStatus,
    LogicalCapability,
    MediaGenerationInput,
    ProviderHealth,
    ProviderInvocation,
    ProviderRequest,
    ProviderResult,
    QualityTier,
    require_execution_allowed,
)
from yonerai_discord.provider_registry.domain import utc_now
from yonerai_discord.provider_registry.ports import ExecutionAuthorizationCheck

from .artifacts import MAX_MP4_BYTES, VideoArtifactStore, validate_mp4
from .domain import video_artifact_request_binding


GEMINI_VEO_PROVIDER_ID = "google-gemini-api"
GEMINI_VEO_ADAPTER_ID = "gemini-veo-v1beta"
GEMINI_VEO_ORIGIN = "https://generativelanguage.googleapis.com"
GEMINI_VEO_MODELS = frozenset(
    {
        "veo-3.1-lite-generate-preview",
        "veo-3.1-fast-generate-preview",
        "veo-3.1-generate-preview",
    }
)
_MAX_PROMPT_LENGTH = 4_000
_MAX_START_BYTES = 64 * 1024
_MAX_STATUS_BYTES = 256 * 1024
_MAX_MODEL_BYTES = 64 * 1024
_OPERATION = re.compile(r"^(?:models/[a-z0-9.-]{1,100}/)?operations/[A-Za-z0-9._-]{1,180}\Z")
_MODEL_PROFILE = {
    "veo-3.1-lite-generate-preview": (4, "720p"),
    "veo-3.1-fast-generate-preview": (6, "720p"),
    "veo-3.1-generate-preview": (8, "4k"),
}
_TIER_PROFILE = {
    QualityTier.FAST: ("veo-3.1-lite-generate-preview", 4, "720p"),
    QualityTier.BALANCED: ("veo-3.1-fast-generate-preview", 6, "720p"),
    QualityTier.QUALITY: ("veo-3.1-generate-preview", 8, "4k"),
}
GEMINI_VEO_MODEL_ALIASES = ("video.fast", "video.balanced", "video.quality")


class GeminiVeoProviderError(RuntimeError):
    """Prompt、credential、provider本文を含めないVeo provider境界エラー。"""


class GeminiVeoRemoteOutcomeUncertainError(TimeoutError):
    """remote operationが継続している可能性を保持する非再試行timeout。"""


class GeminiVeoRemoteOutcomeUncertainCancelledError(asyncio.CancelledError):
    """local task取消後もremote operationが継続し得ることを保持する。"""


class GeminiVeoTransport(Protocol):
    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool: ...

    async def generate_video(
        self,
        *,
        prompt: str,
        model: str,
        duration_seconds: int,
        resolution: str,
        timeout_seconds: float,
    ) -> bytes: ...

    async def close(self) -> None: ...


class AiohttpGeminiVeoTransport:
    """固定Gemini originのlong-running RESTだけをboundedに実行する。"""

    def __init__(
        self,
        *,
        api_key: str,
        poll_seconds: float = 5.0,
        origin: str = GEMINI_VEO_ORIGIN,
    ) -> None:
        self._api_key = _credential(api_key)
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not 0.05 <= float(poll_seconds) <= 30.0
        ):
            raise ValueError("poll_seconds is outside the allowed range")
        if origin != GEMINI_VEO_ORIGIN:
            raise ValueError("Gemini Veo origin is fixed")
        self._poll_seconds = float(poll_seconds)
        self._origin = origin

    def __repr__(self) -> str:
        return "AiohttpGeminiVeoTransport()"

    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
        try:
            model = _model(model)
            timeout = _timeout(timeout_seconds)
            async with asyncio.timeout(timeout):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    async with session.get(
                        f"{self._origin}/v1beta/models/{model}",
                        headers={"x-goog-api-key": self._api_key},
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200 or _content_type(response) != "application/json":
                            return False
                        payload = _json_object(await _read_limited(response.content, _MAX_MODEL_BYTES))
            return payload.get("name") == f"models/{model}"
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def generate_video(
        self,
        *,
        prompt: str,
        model: str,
        duration_seconds: int,
        resolution: str,
        timeout_seconds: float,
    ) -> bytes:
        prompt = _prompt(prompt)
        model = _model(model)
        duration_seconds, resolution = _profile(
            model=model,
            duration_seconds=duration_seconds,
            resolution=resolution,
        )
        timeout = _timeout(timeout_seconds)
        submission_attempted = False
        try:
            async with asyncio.timeout(timeout):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    submission_attempted = True
                    operation = await self._start(
                        session,
                        prompt=prompt,
                        model=model,
                        duration_seconds=duration_seconds,
                        resolution=resolution,
                    )
                    status = await self._poll(session, operation=operation)
                    uri = _video_uri(status)
                    return await self._download(session, uri=uri)
        except GeminiVeoRemoteOutcomeUncertainCancelledError:
            raise
        except asyncio.CancelledError:
            if submission_attempted:
                raise GeminiVeoRemoteOutcomeUncertainCancelledError(
                    "Veo operation outcome is uncertain after local cancellation"
                ) from None
            raise
        except GeminiVeoProviderError:
            raise
        except TimeoutError:
            raise GeminiVeoRemoteOutcomeUncertainError("Veo operation outcome is uncertain after timeout") from None
        except Exception:
            raise GeminiVeoProviderError("Veo provider request failed") from None

    async def close(self) -> None:
        return None

    async def _start(
        self,
        session: aiohttp.ClientSession,
        *,
        prompt: str,
        model: str,
        duration_seconds: int,
        resolution: str,
    ) -> str:
        async with session.post(
            f"{self._origin}/v1beta/models/{model}:predictLongRunning",
            headers=self._json_headers(),
            json={
                "instances": [{"prompt": prompt}],
                "parameters": {
                    "durationSeconds": duration_seconds,
                    "resolution": resolution,
                },
            },
            allow_redirects=False,
        ) as response:
            if response.status != 200 or _content_type(response) != "application/json":
                raise GeminiVeoProviderError("Veo generation request failed")
            started = _json_object(await _read_limited(response.content, _MAX_START_BYTES))
        if not set(started).issubset({"name", "metadata", "done"}):
            raise GeminiVeoProviderError("Veo operation response is invalid")
        started_done = started.get("done")
        if started_done is not None and started_done is not False:
            raise GeminiVeoProviderError("Veo operation response is invalid")
        operation = started["name"]
        if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
            raise GeminiVeoProviderError("Veo operation response is invalid")
        return operation

    async def _poll(
        self,
        session: aiohttp.ClientSession,
        *,
        operation: str,
    ) -> dict[str, object]:
        while True:
            async with session.get(
                f"{self._origin}/v1beta/{operation}",
                headers={"x-goog-api-key": self._api_key},
                allow_redirects=False,
            ) as response:
                if response.status != 200 or _content_type(response) != "application/json":
                    raise GeminiVeoProviderError("Veo operation polling failed")
                status = _json_object(await _read_limited(response.content, _MAX_STATUS_BYTES))
            done = status.get("done")
            if done is True:
                return status
            if done is not False and done is not None:
                raise GeminiVeoProviderError("Veo operation status is invalid")
            await asyncio.sleep(self._poll_seconds)

    async def _download(self, session: aiohttp.ClientSession, *, uri: str) -> bytes:
        parsed = urlparse(uri)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "generativelanguage.googleapis.com"
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise GeminiVeoProviderError("Veo video URI is outside the trusted origin")
        async with session.get(
            uri,
            headers={"x-goog-api-key": self._api_key},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise GeminiVeoProviderError("Veo video download failed")
            content_type = _content_type(response)
            if content_type not in {"video/mp4", "application/octet-stream"}:
                raise GeminiVeoProviderError("Veo video response type is invalid")
            return await _read_limited(response.content, MAX_MP4_BYTES)

    def _json_headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }


class GeminiVeoProviderAdapter:
    provider_id = GEMINI_VEO_PROVIDER_ID
    adapter_id = GEMINI_VEO_ADAPTER_ID

    def __init__(
        self,
        transport: GeminiVeoTransport,
        artifact_store: VideoArtifactStore,
        *,
        readiness_current: Callable[[], bool] | None = None,
        probed_model_aliases: tuple[str, ...] = GEMINI_VEO_MODEL_ALIASES,
    ) -> None:
        if not callable(getattr(transport, "generate_video", None)):
            raise TypeError("transport must provide generate_video")
        if not callable(getattr(transport, "close", None)):
            raise TypeError("transport must provide close")
        if not callable(getattr(artifact_store, "put_mp4", None)):
            raise TypeError("artifact_store must provide put_mp4")
        self._transport = transport
        self._store = artifact_store
        self._readiness_current = readiness_current
        self._probed_model_aliases = _model_aliases(probed_model_aliases)
        self._closing = False
        self._operation_lock = asyncio.Lock()

    async def health(self) -> ProviderHealth:
        async with self._operation_lock:
            if not self._ready():
                ready = False
            else:
                probe = getattr(self._transport, "probe_model", None)
                try:
                    ready = callable(probe) and all(
                        result is True
                        for result in await asyncio.wait_for(
                            asyncio.gather(*(probe(model, timeout_seconds=5.0) for model in sorted(GEMINI_VEO_MODELS))),
                            timeout=5.0,
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    ready = False
            return ProviderHealth(
                self.provider_id,
                HealthStatus.READY if ready else HealthStatus.UNAVAILABLE,
                utc_now(),
                detail_code="gemini_veo_ready" if ready else "gemini_veo_not_ready",
                probed_model_aliases=self._probed_model_aliases if ready else (),
            )

    async def execute(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        execution_allowed: ExecutionAuthorizationCheck | None = None,
    ) -> ProviderResult:
        async with self._operation_lock:
            return await self._execute_locked(
                request,
                invocation,
                execution_allowed=execution_allowed,
            )

    async def _execute_locked(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        execution_allowed: ExecutionAuthorizationCheck | None = None,
    ) -> ProviderResult:
        if (
            not self._ready()
            or request.capability is not LogicalCapability.VIDEO_GENERATION
            or not isinstance(request.payload, MediaGenerationInput)
            or request.input_artifacts
            or invocation.provider_id != self.provider_id
            or not request.request_id.startswith("video-request-")
        ):
            raise GeminiVeoProviderError("Veo provider contract mismatch")
        model = _model(invocation.provider_model)
        expected_model, duration_seconds, resolution = _TIER_PROFILE[invocation.quality_tier]
        if model != expected_model:
            raise GeminiVeoProviderError("Veo tier model is unavailable")
        prompt = _prompt(request.payload.prompt)
        timeout = _timeout(invocation.timeout_seconds)
        await require_execution_allowed(execution_allowed)
        try:
            mp4 = await asyncio.wait_for(
                self._transport.generate_video(
                    prompt=prompt,
                    model=model,
                    duration_seconds=duration_seconds,
                    resolution=resolution,
                    timeout_seconds=timeout,
                ),
                timeout=timeout,
            )
        except GeminiVeoRemoteOutcomeUncertainCancelledError:
            raise
        except asyncio.CancelledError:
            raise GeminiVeoRemoteOutcomeUncertainCancelledError(
                "Veo operation outcome is uncertain after local cancellation"
            ) from None
        except GeminiVeoRemoteOutcomeUncertainError:
            raise
        except TimeoutError:
            raise GeminiVeoRemoteOutcomeUncertainError("Veo operation outcome is uncertain after timeout") from None
        except GeminiVeoProviderError:
            raise
        except Exception:
            raise GeminiVeoProviderError("Veo generation failed") from None
        try:
            validate_mp4(mp4)
        except Exception:
            raise GeminiVeoProviderError("Veo provider MP4 is invalid") from None
        await require_execution_allowed(execution_allowed)
        if not self._ready():
            raise GeminiVeoProviderError("Veo provider readiness changed")
        binding = video_artifact_request_binding(
            request,
            provider_id=self.provider_id,
            provider_model=model,
            model_alias=invocation.model_alias,
            quality_tier=invocation.quality_tier,
        )
        try:
            ref = self._store.put_mp4(mp4, request_binding=binding)
        except Exception:
            raise GeminiVeoProviderError("video artifact commit failed") from None
        return ProviderResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            provider_model=model,
            artifacts=(ref,),
        )

    async def close(self) -> None:
        self._closing = True
        async with self._operation_lock:
            await self._transport.close()

    def _ready(self) -> bool:
        if self._closing or self._readiness_current is None:
            return False
        try:
            return self._readiness_current() is True
        except Exception:
            return False


async def _read_limited(stream: Any, limit: int) -> bytes:
    iterator = getattr(stream, "iter_chunked", None)
    if not callable(iterator):
        raise GeminiVeoProviderError("Veo response stream is unavailable")
    parts: list[bytes] = []
    size = 0
    async for chunk in iterator(64 * 1024):
        if not isinstance(chunk, bytes):
            raise GeminiVeoProviderError("Veo response is invalid")
        size += len(chunk)
        if size > limit:
            raise GeminiVeoProviderError("Veo response is too large")
        parts.append(chunk)
    return b"".join(parts)


def _json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise GeminiVeoProviderError("Veo response JSON is invalid") from None
    if not isinstance(value, dict):
        raise GeminiVeoProviderError("Veo response JSON is invalid")
    return value


def _model_aliases(values: tuple[str, ...]) -> tuple[str, ...]:
    aliases = tuple(values)
    if (
        len(aliases) != 3
        or len(set(aliases)) != len(aliases)
        or any(not isinstance(alias, str) or not alias for alias in aliases)
    ):
        raise ValueError("probed_model_aliases must contain three unique aliases")
    return aliases


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _video_uri(status: dict[str, object]) -> str:
    if status.get("done") is not True:
        raise GeminiVeoProviderError("Veo operation did not reach a terminal state")
    if "error" in status:
        raise GeminiVeoProviderError("Veo generation failed")
    try:
        samples = status["response"]["generateVideoResponse"]["generatedSamples"]  # type: ignore[index]
        if not isinstance(samples, list) or len(samples) != 1:
            raise TypeError
        uri = samples[0]["video"]["uri"]
    except (KeyError, IndexError, TypeError):
        raise GeminiVeoProviderError("Veo response did not contain one video") from None
    if (
        not isinstance(uri, str)
        or not uri
        or len(uri) > 4_096
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in uri)
    ):
        raise GeminiVeoProviderError("Veo response did not contain one video")
    return uri


def _content_type(response: object) -> str:
    headers = getattr(response, "headers", None)
    if not hasattr(headers, "get"):
        return ""
    value = headers.get("Content-Type", "")
    if not isinstance(value, str):
        return ""
    return value.split(";", 1)[0].strip().lower()


def _credential(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise ValueError("api_key is unavailable")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("api_key is invalid")
    return value


def _model(value: object) -> str:
    if not isinstance(value, str) or value not in GEMINI_VEO_MODELS:
        raise GeminiVeoProviderError("Veo model is unavailable")
    return value


def _prompt(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_PROMPT_LENGTH:
        raise GeminiVeoProviderError("Veo prompt is invalid")
    if value != value.strip() or any(
        unicodedata.category(character).startswith("C") and character not in {"\n", "\t"} for character in value
    ):
        raise GeminiVeoProviderError("Veo prompt is invalid")
    return value


def _profile(*, model: str, duration_seconds: object, resolution: object) -> tuple[int, str]:
    expected_duration, expected_resolution = _MODEL_PROFILE[model]
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or duration_seconds != expected_duration
        or resolution != expected_resolution
    ):
        raise GeminiVeoProviderError("Veo output profile is invalid")
    return duration_seconds, resolution


def _timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.05 <= float(value) <= 1_800.0:
        raise GeminiVeoProviderError("Veo timeout is invalid")
    return float(value)


__all__ = [
    "AiohttpGeminiVeoTransport",
    "GEMINI_VEO_ADAPTER_ID",
    "GEMINI_VEO_MODELS",
    "GEMINI_VEO_MODEL_ALIASES",
    "GEMINI_VEO_ORIGIN",
    "GEMINI_VEO_PROVIDER_ID",
    "GeminiVeoProviderAdapter",
    "GeminiVeoProviderError",
    "GeminiVeoRemoteOutcomeUncertainCancelledError",
    "GeminiVeoRemoteOutcomeUncertainError",
    "GeminiVeoTransport",
]
