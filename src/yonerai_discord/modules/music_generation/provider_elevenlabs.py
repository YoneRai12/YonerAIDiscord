from __future__ import annotations

import asyncio
import inspect
import json
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import aiohttp

from yonerai_discord.provider_registry import (
    HealthStatus,
    LogicalCapability,
    MediaGenerationInput,
    ProviderHealth,
    ProviderInvocation,
    ProviderRequest,
    ProviderResult,
    require_execution_allowed,
)
from yonerai_discord.provider_registry.domain import utc_now
from yonerai_discord.provider_registry.ports import ExecutionAuthorizationCheck

from .artifacts import MAX_WAV_BYTES, MusicArtifactStore, validate_wav
from .domain import music_artifact_request_binding


ELEVEN_MUSIC_PROVIDER_ID = "elevenlabs-api"
ELEVEN_MUSIC_ADAPTER_ID = "elevenlabs-music-v2"
ELEVEN_MUSIC_ORIGIN = "https://api.elevenlabs.io"
ELEVEN_MUSIC_MODEL = "music_v2"
ELEVEN_MUSIC_OUTPUT_FORMAT = "wav_44100"
ELEVEN_MUSIC_MODEL_ALIASES = ("music.fast", "music.balanced", "music.quality")
_MAX_PROMPT_LENGTH = 4_000
_MAX_MODELS_RESPONSE_BYTES = 512 * 1024

MusicRequestPolicyCheck = Callable[
    [ProviderRequest, ProviderInvocation],
    bool | Awaitable[bool],
]


class ElevenLabsMusicProviderError(RuntimeError):
    """Prompt、credential、provider本文を含めない音楽provider境界エラー。"""


class ElevenLabsMusicTimeoutError(TimeoutError):
    """応答が確定せず、自動再試行してはいけない音楽provider timeout。"""


class ElevenLabsMusicRemoteOutcomeUncertainCancelledError(asyncio.CancelledError):
    """POST開始後のlocal取消でもremote生成が継続し得ることを保持する。"""


class ElevenLabsMusicTransport(Protocol):
    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool: ...

    async def compose_instrumental(
        self,
        *,
        prompt: str,
        duration_seconds: int,
        model: str,
        timeout_seconds: float,
    ) -> bytes: ...

    async def close(self) -> None: ...


class AiohttpElevenLabsMusicTransport:
    """固定Eleven Music endpointへWAV・instrumental条件だけを送るtransport。"""

    def __init__(self, *, api_key: str, origin: str = ELEVEN_MUSIC_ORIGIN) -> None:
        self._api_key = _credential(api_key)
        if origin != ELEVEN_MUSIC_ORIGIN:
            raise ValueError("Eleven Music origin is fixed")
        self._origin = origin

    def __repr__(self) -> str:
        return "AiohttpElevenLabsMusicTransport()"

    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
        if model != ELEVEN_MUSIC_MODEL:
            return False
        try:
            timeout = _timeout(timeout_seconds)
            async with asyncio.timeout(timeout):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    async with session.get(
                        f"{self._origin}/v1/models",
                        headers={"xi-api-key": self._api_key},
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200 or _content_type(response) != "application/json":
                            return False
                        raw = await _read_bounded(response.content, _MAX_MODELS_RESPONSE_BYTES)
            models = _json_array(raw)
            if not 1 <= len(models) <= 1_000:
                return False
            model_ids: list[str] = []
            for item in models:
                if not isinstance(item, dict):
                    return False
                model_id = item.get("model_id")
                if not isinstance(model_id, str) or not model_id:
                    return False
                model_ids.append(model_id)
            return model_ids.count(model) == 1
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def compose_instrumental(
        self,
        *,
        prompt: str,
        duration_seconds: int,
        model: str,
        timeout_seconds: float,
    ) -> bytes:
        prompt = _prompt(prompt)
        if model != ELEVEN_MUSIC_MODEL:
            raise ElevenLabsMusicProviderError("music provider model is unavailable")
        duration = _duration(duration_seconds)
        timeout = _timeout(timeout_seconds)
        submission_attempted = False
        try:
            async with asyncio.timeout(timeout):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    submission_attempted = True
                    async with session.post(
                        f"{self._origin}/v1/music",
                        params={"output_format": ELEVEN_MUSIC_OUTPUT_FORMAT},
                        json={
                            "prompt": prompt,
                            "music_length_ms": duration * 1_000,
                            "model_id": ELEVEN_MUSIC_MODEL,
                            "force_instrumental": True,
                            "store_for_inpainting": False,
                        },
                        headers={
                            "xi-api-key": self._api_key,
                            "Content-Type": "application/json",
                        },
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200:
                            raise ElevenLabsMusicProviderError("music provider request failed")
                        content_type = _content_type(response)
                        if content_type not in {"audio/wav", "audio/x-wav"}:
                            raise ElevenLabsMusicProviderError("music provider response type is invalid")
                        return await _read_bounded(response.content, MAX_WAV_BYTES)
        except ElevenLabsMusicRemoteOutcomeUncertainCancelledError:
            raise
        except asyncio.CancelledError:
            if submission_attempted:
                raise ElevenLabsMusicRemoteOutcomeUncertainCancelledError(
                    "music provider outcome is uncertain after local cancellation"
                ) from None
            raise
        except ElevenLabsMusicProviderError:
            raise
        except TimeoutError:
            raise ElevenLabsMusicTimeoutError("music provider request timed out") from None
        except Exception:
            raise ElevenLabsMusicProviderError("music provider request failed") from None

    async def close(self) -> None:
        return None


class ElevenLabsMusicProviderAdapter:
    """Eleven Music v2のinstrumental WAVだけをStage 1 storeへcommitする。"""

    provider_id = ELEVEN_MUSIC_PROVIDER_ID
    adapter_id = ELEVEN_MUSIC_ADAPTER_ID

    def __init__(
        self,
        transport: ElevenLabsMusicTransport,
        artifact_store: MusicArtifactStore,
        *,
        readiness_current: Callable[[], bool] | None = None,
        request_policy_current: MusicRequestPolicyCheck | None = None,
        probed_model_aliases: tuple[str, ...] = ELEVEN_MUSIC_MODEL_ALIASES,
    ) -> None:
        if not callable(getattr(transport, "compose_instrumental", None)):
            raise TypeError("transport must provide compose_instrumental")
        if not callable(getattr(transport, "close", None)):
            raise TypeError("transport must provide close")
        if not callable(getattr(artifact_store, "put_wav", None)):
            raise TypeError("artifact_store must provide put_wav")
        self._transport = transport
        self._store = artifact_store
        self._readiness_current = readiness_current
        self._request_policy_current = request_policy_current
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
                    ready = callable(probe) and await asyncio.wait_for(
                        probe(ELEVEN_MUSIC_MODEL, timeout_seconds=5.0),
                        timeout=5.0,
                    )
                    ready = ready is True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    ready = False
            return ProviderHealth(
                self.provider_id,
                HealthStatus.READY if ready else HealthStatus.UNAVAILABLE,
                utc_now(),
                detail_code="eleven_music_ready" if ready else "eleven_music_not_ready",
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
        payload = request.payload
        duration = getattr(payload, "duration_seconds", None)
        if (
            not self._ready()
            or request.capability is not LogicalCapability.MUSIC_GENERATION
            or not isinstance(payload, MediaGenerationInput)
            or request.input_artifacts
            or invocation.provider_id != self.provider_id
            or invocation.provider_model != ELEVEN_MUSIC_MODEL
            or not request.request_id.startswith("music-request-")
            or duration is None
            or isinstance(duration, bool)
            or not float(duration).is_integer()
            or not 3 <= int(duration) <= 30
        ):
            raise ElevenLabsMusicProviderError("music provider contract mismatch")
        prompt = _prompt(payload.prompt)
        timeout = _timeout(invocation.timeout_seconds)
        await require_execution_allowed(execution_allowed)
        await _require_request_policy_current(self._request_policy_current, request, invocation)
        await require_execution_allowed(execution_allowed)
        timeout_scope: asyncio.Timeout | None = None
        try:
            async with asyncio.timeout(timeout) as timeout_scope:
                wav = await self._transport.compose_instrumental(
                    prompt=prompt,
                    duration_seconds=int(duration),
                    model=ELEVEN_MUSIC_MODEL,
                    timeout_seconds=timeout,
                )
        except ElevenLabsMusicRemoteOutcomeUncertainCancelledError:
            if timeout_scope is not None and timeout_scope.expired():
                raise ElevenLabsMusicTimeoutError("music generation timed out") from None
            raise
        except asyncio.CancelledError:
            raise
        except ElevenLabsMusicProviderError:
            raise
        except TimeoutError:
            raise ElevenLabsMusicTimeoutError("music generation timed out") from None
        except Exception:
            raise ElevenLabsMusicProviderError("music generation failed") from None
        try:
            validated = validate_wav(wav)
        except Exception:
            raise ElevenLabsMusicProviderError("music provider WAV is invalid") from None
        if validated.duration_seconds != int(duration):
            raise ElevenLabsMusicProviderError("music provider duration is invalid")
        await require_execution_allowed(execution_allowed)
        await _require_request_policy_current(self._request_policy_current, request, invocation)
        await require_execution_allowed(execution_allowed)
        if not self._ready():
            raise ElevenLabsMusicProviderError("music provider readiness changed")
        binding = music_artifact_request_binding(
            request,
            provider_id=self.provider_id,
            provider_model=ELEVEN_MUSIC_MODEL,
            model_alias=invocation.model_alias,
            quality_tier=invocation.quality_tier,
        )
        try:
            ref = self._store.put_wav(wav, request_binding=binding)
        except Exception:
            raise ElevenLabsMusicProviderError("music artifact commit failed") from None
        return ProviderResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            provider_model=ELEVEN_MUSIC_MODEL,
            artifacts=(ref,),
        )

    async def close(self) -> None:
        self._closing = True
        async with self._operation_lock:
            await self._transport.close()

    def _ready(self) -> bool:
        if self._closing or self._readiness_current is None or self._request_policy_current is None:
            return False
        try:
            return self._readiness_current() is True
        except Exception:
            return False


async def _require_request_policy_current(
    check: MusicRequestPolicyCheck | None,
    request: ProviderRequest,
    invocation: ProviderInvocation,
) -> None:
    """service発行fingerprint・rights/profile policyの現行性をcompositionで再検証する。"""
    if check is None:
        raise ElevenLabsMusicProviderError("music request policy is unavailable")
    try:
        allowed = check(request, invocation)
        if inspect.isawaitable(allowed):
            allowed = await allowed
    except asyncio.CancelledError:
        raise
    except Exception:
        allowed = False
    if allowed is not True:
        raise ElevenLabsMusicProviderError("music request policy is unavailable")


async def _read_bounded(stream: Any, maximum: int) -> bytes:
    iterator = getattr(stream, "iter_chunked", None)
    if not callable(iterator):
        raise ElevenLabsMusicProviderError("music provider response stream is unavailable")
    chunks: list[bytes] = []
    total = 0
    async for chunk in iterator(64 * 1024):
        if not isinstance(chunk, bytes):
            raise ElevenLabsMusicProviderError("music provider response is invalid")
        total += len(chunk)
        if total > maximum:
            raise ElevenLabsMusicProviderError("music provider response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _content_type(response: object) -> str:
    headers = getattr(response, "headers", None)
    if not hasattr(headers, "get"):
        return ""
    value = headers.get("Content-Type", "")
    if not isinstance(value, str):
        return ""
    return value.split(";", 1)[0].strip().lower()


def _json_array(raw: bytes) -> list[object]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ElevenLabsMusicProviderError("music model response is invalid") from None
    if not isinstance(value, list):
        raise ElevenLabsMusicProviderError("music model response is invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _model_aliases(values: tuple[str, ...]) -> tuple[str, ...]:
    aliases = tuple(values)
    if (
        len(aliases) != 3
        or len(set(aliases)) != len(aliases)
        or any(not isinstance(alias, str) or not alias for alias in aliases)
    ):
        raise ValueError("probed_model_aliases must contain three unique aliases")
    return aliases


def _credential(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise ValueError("api_key is unavailable")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("api_key is invalid")
    return value


def _prompt(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_PROMPT_LENGTH:
        raise ElevenLabsMusicProviderError("music prompt is invalid")
    if value != value.strip() or any(
        unicodedata.category(character).startswith("C") and character not in {"\n", "\t"} for character in value
    ):
        raise ElevenLabsMusicProviderError("music prompt is invalid")
    return value


def _duration(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 3 <= value <= 30:
        raise ElevenLabsMusicProviderError("music duration is invalid")
    return value


def _timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 5.0 <= float(value) <= 900.0:
        raise ElevenLabsMusicProviderError("music timeout is invalid")
    return float(value)


__all__ = [
    "AiohttpElevenLabsMusicTransport",
    "ELEVEN_MUSIC_ADAPTER_ID",
    "ELEVEN_MUSIC_MODEL",
    "ELEVEN_MUSIC_MODEL_ALIASES",
    "ELEVEN_MUSIC_ORIGIN",
    "ELEVEN_MUSIC_OUTPUT_FORMAT",
    "ELEVEN_MUSIC_PROVIDER_ID",
    "ElevenLabsMusicProviderAdapter",
    "ElevenLabsMusicProviderError",
    "ElevenLabsMusicRemoteOutcomeUncertainCancelledError",
    "ElevenLabsMusicTimeoutError",
    "ElevenLabsMusicTransport",
    "MusicRequestPolicyCheck",
]
