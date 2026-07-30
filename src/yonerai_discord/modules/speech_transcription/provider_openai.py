from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Protocol
from urllib.parse import quote

import aiohttp

from yonerai_discord.provider_registry import (
    ArtifactKind,
    HealthStatus,
    LogicalCapability,
    ProviderHealth,
    ProviderInvocation,
    ProviderRequest,
    ProviderResult,
    SpeechTranscriptionInput,
    require_execution_allowed,
)
from yonerai_discord.provider_registry.domain import ArtifactRef, utc_now
from yonerai_discord.provider_registry.ports import ExecutionAuthorizationCheck

from .domain import (
    ALLOWED_AUDIO_MEDIA_TYPES,
    MAX_AUDIO_BYTES,
    normalize_transcript_text,
)


OPENAI_STT_MODEL = "gpt-4o-mini-transcribe"
OPENAI_TRANSCRIPT_RESPONSE_BYTES = 32 * 1024
OPENAI_MODEL_RESPONSE_BYTES = 8 * 1024
OPENAI_STT_MODEL_ALIASES = ("stt.fast", "stt.balanced", "stt.quality")

_AUDIO_FILENAME_BY_MEDIA_TYPE = {
    "audio/flac": "input-audio.flac",
    "audio/mp4": "input-audio.m4a",
    "audio/mpeg": "input-audio.mp3",
    "audio/ogg": "input-audio.ogg",
    "audio/wav": "input-audio.wav",
    "audio/webm": "input-audio.webm",
}
_LANGUAGE_CODE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?\Z")


class SpeechAudioBytesResolver(Protocol):
    """Scope-bound ArtifactRefからだけ検証対象bytesを解決する注入port。"""

    def read_audio(self, request: ProviderRequest, ref: ArtifactRef) -> bytes: ...


class OpenAITranscriptionTransport(Protocol):
    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool: ...

    async def transcribe(
        self,
        *,
        audio: bytes,
        filename: str,
        media_type: str,
        model: str,
        language_code: str | None,
        prompt: str,
        timeout_seconds: float,
    ) -> str: ...

    async def close(self) -> None: ...


class AiohttpOpenAITranscriptionTransport:
    """OpenAI公式originの固定`/v1/audio/transcriptions`境界。"""

    def __init__(self, *, api_key: str, endpoint: str = "https://api.openai.com") -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        if endpoint.rstrip("/") != "https://api.openai.com":
            raise ValueError("only the official OpenAI API origin is supported")
        self._api_key = api_key.strip()
        self._session: aiohttp.ClientSession | None = None
        self._operation_lock = asyncio.Lock()
        self._closed = False

    def __repr__(self) -> str:
        return "AiohttpOpenAITranscriptionTransport()"

    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
        if model != OPENAI_STT_MODEL or not _valid_timeout(timeout_seconds, maximum=30.0):
            return False
        async with self._operation_lock:
            if self._closed:
                return False
            try:
                async with asyncio.timeout(float(timeout_seconds)):
                    session = self._session
                    if session is None or session.closed:
                        session = self._session = aiohttp.ClientSession()
                    async with session.get(
                        f"https://api.openai.com/v1/models/{quote(model, safe='')}",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=float(timeout_seconds)),
                    ) as response:
                        if response.status != 200 or _response_content_type(response) != "application/json":
                            return False
                        raw = await _read_limited(response, OPENAI_MODEL_RESPONSE_BYTES)
                payload = _strict_json_object(raw)
                if set(payload) != {"id", "object", "created", "owned_by"}:
                    return False
                return (
                    payload.get("id") == model
                    and payload.get("object") == "model"
                    and isinstance(payload.get("created"), int)
                    and not isinstance(payload.get("created"), bool)
                    and payload["created"] >= 0
                    and isinstance(payload.get("owned_by"), str)
                    and bool(payload["owned_by"])
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return False

    async def transcribe(
        self,
        *,
        audio: bytes,
        filename: str,
        media_type: str,
        model: str,
        language_code: str | None,
        prompt: str,
        timeout_seconds: float,
    ) -> str:
        async with self._operation_lock:
            if self._closed:
                raise RuntimeError("transcription transport is closed")
            return await self._transcribe_locked(
                audio=audio,
                filename=filename,
                media_type=media_type,
                model=model,
                language_code=language_code,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
            )

    async def _transcribe_locked(
        self,
        *,
        audio: bytes,
        filename: str,
        media_type: str,
        model: str,
        language_code: str | None,
        prompt: str,
        timeout_seconds: float,
    ) -> str:
        expected_filename = _AUDIO_FILENAME_BY_MEDIA_TYPE.get(media_type)
        if (
            not isinstance(audio, bytes)
            or not 1 <= len(audio) <= MAX_AUDIO_BYTES
            or media_type not in ALLOWED_AUDIO_MEDIA_TYPES
            or filename != expected_filename
            or model != OPENAI_STT_MODEL
            or not isinstance(prompt, str)
            or len(prompt) > 2_000
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1.0 <= float(timeout_seconds) <= 900.0
        ):
            raise RuntimeError("transcription request is outside the fixed contract")
        if language_code is not None and (
            not isinstance(language_code, str) or not _LANGUAGE_CODE.fullmatch(language_code.strip().lower())
        ):
            raise RuntimeError("transcription language is outside the fixed contract")

        session = self._session
        if session is None or session.closed:
            session = self._session = aiohttp.ClientSession()
        form = aiohttp.FormData()
        form.add_field("file", audio, filename=filename, content_type=media_type)
        form.add_field("model", model)
        form.add_field("response_format", "json")
        if language_code:
            form.add_field("language", language_code.split("-", 1)[0].lower())
        if prompt:
            form.add_field("prompt", prompt)
        async with session.post(
            "https://api.openai.com/v1/audio/transcriptions",
            data=form,
            headers={"Authorization": f"Bearer {self._api_key}"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=float(timeout_seconds)),
        ) as response:
            if response.status != 200:
                raise RuntimeError("transcription provider rejected the request")
            raw = await _read_limited(response, OPENAI_TRANSCRIPT_RESPONSE_BYTES)
        payload = _strict_json_object(raw)
        if set(payload) - {"text", "usage", "logprobs"}:
            raise RuntimeError("transcription provider returned an invalid result")
        try:
            return normalize_transcript_text(payload.get("text"))
        except (TypeError, ValueError):
            raise RuntimeError("transcription provider returned an invalid result") from None

    async def close(self) -> None:
        async with self._operation_lock:
            self._closed = True
            if self._session is not None and not self._session.closed:
                await self._session.close()


class OpenAITranscriptionProviderAdapter:
    provider_id = "openai-api"
    adapter_id = "openai-speech-transcription-v1"

    def __init__(
        self,
        transport: OpenAITranscriptionTransport,
        *,
        audio_resolver: SpeechAudioBytesResolver | Callable[[ProviderRequest, ArtifactRef], bytes] | None = None,
        read_audio: Callable[[ProviderRequest, ArtifactRef], bytes] | None = None,
        readiness_current: Callable[[], bool] | None = None,
        probed_model_aliases: tuple[str, ...] = OPENAI_STT_MODEL_ALIASES,
    ) -> None:
        resolver = audio_resolver if audio_resolver is not None else read_audio
        reader = getattr(resolver, "read_audio", None)
        if not callable(reader) and callable(resolver):
            reader = resolver
        if not callable(getattr(transport, "transcribe", None)) or not callable(reader):
            raise TypeError("transport and audio_resolver are required")
        self._transport = transport
        self._read_audio = reader
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
                    ready = callable(probe) and await asyncio.wait_for(
                        probe(OPENAI_STT_MODEL, timeout_seconds=5.0),
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
                detail_code="openai_stt_ready" if ready else "openai_stt_not_ready",
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
            or request.capability is not LogicalCapability.SPEECH_STT
            or not isinstance(request.payload, SpeechTranscriptionInput)
            or len(request.input_artifacts) != 1
            or invocation.provider_id != self.provider_id
            or invocation.provider_model != OPENAI_STT_MODEL
        ):
            raise RuntimeError("OpenAI transcription provider contract mismatch")
        ref = request.input_artifacts[0]
        if (
            ref.kind is not ArtifactKind.AUDIO
            or ref.media_type not in ALLOWED_AUDIO_MEDIA_TYPES
            or ref.size_bytes is None
            or not 1 <= ref.size_bytes <= MAX_AUDIO_BYTES
            or ref.sha256 is None
        ):
            raise RuntimeError("audio artifact is outside the fixed contract")

        await require_execution_allowed(execution_allowed)
        try:
            audio = self._read_audio(request, ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RuntimeError("audio artifact could not be resolved") from None
        if (
            not isinstance(audio, bytes)
            or len(audio) != ref.size_bytes
            or hashlib.sha256(audio).hexdigest() != ref.sha256
        ):
            raise RuntimeError("audio artifact could not be verified")

        await require_execution_allowed(execution_allowed)
        try:
            text = await self._transport.transcribe(
                audio=audio,
                filename=_AUDIO_FILENAME_BY_MEDIA_TYPE[ref.media_type],
                media_type=ref.media_type,
                model=invocation.provider_model,
                language_code=request.payload.language_code,
                prompt=request.payload.prompt,
                timeout_seconds=invocation.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RuntimeError("transcription provider failed safely") from None

        await require_execution_allowed(execution_allowed)
        if not self._ready():
            raise RuntimeError("OpenAI transcription provider readiness changed")
        try:
            normalized = normalize_transcript_text(text)
        except (TypeError, ValueError):
            raise RuntimeError("transcription provider returned an invalid result") from None
        return ProviderResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            provider_model=invocation.provider_model,
            text=normalized,
        )

    async def close(self) -> None:
        async with self._operation_lock:
            self._closing = True
            await self._transport.close()

    def _ready(self) -> bool:
        if self._closing or self._readiness_current is None:
            return False
        try:
            return self._readiness_current() is True
        except Exception:
            return False


def _strict_json_object(raw: bytes) -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON value")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise RuntimeError("transcription provider returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise RuntimeError("transcription provider returned invalid JSON")
    return payload


def _response_content_type(response: object) -> str:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return ""
    value = headers.get("Content-Type", "")
    if not isinstance(value, str):
        return ""
    return value.split(";", 1)[0].strip().lower()


def _valid_timeout(value: object, *, maximum: float) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and 0.05 <= float(value) <= maximum


def _model_aliases(values: tuple[str, ...]) -> tuple[str, ...]:
    aliases = tuple(values)
    if (
        len(aliases) != 3
        or len(set(aliases)) != len(aliases)
        or any(not isinstance(alias, str) or not alias for alias in aliases)
    ):
        raise ValueError("probed_model_aliases must contain three unique aliases")
    return aliases


async def _read_limited(response: object, limit: int) -> bytes:
    declared = getattr(response, "content_length", None)
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > limit:
        raise RuntimeError("provider response exceeds the configured limit")
    content = getattr(response, "content", None)
    iterator = getattr(content, "iter_chunked", None)
    if not callable(iterator):
        raise RuntimeError("provider response stream is unavailable")
    chunks: list[bytes] = []
    size = 0
    async for chunk in iterator(8 * 1024):
        if not isinstance(chunk, bytes):
            raise RuntimeError("provider response stream is invalid")
        size += len(chunk)
        if size > limit:
            raise RuntimeError("provider response exceeds the configured limit")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = [
    "AiohttpOpenAITranscriptionTransport",
    "OPENAI_STT_MODEL",
    "OPENAI_STT_MODEL_ALIASES",
    "OpenAITranscriptionProviderAdapter",
    "OpenAITranscriptionTransport",
    "SpeechAudioBytesResolver",
]
