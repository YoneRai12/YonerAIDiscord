from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp

from yonerai_discord.modules.speech_synthesis.provider_voicevox import (
    MAX_VOICEVOX_PLAYBACK_WAV_BYTES,
    canonicalize_voicevox_playback_wav,
)
from yonerai_discord.voice_contract import MIN_VOICEVOX_WAV_BYTES

from .models import SpeechRequest, SynthesizedSpeech


class VoicevoxConfigurationError(ValueError):
    pass


_VOICEVOX_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\Z"
)


def _loopback(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise VoicevoxConfigurationError("VOICEVOX_URL must be an absolute HTTP(S) URL")
    if parsed.hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


class VoicevoxClient:
    def __init__(
        self,
        *,
        endpoint: str,
        allow_remote: bool = False,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        if not _loopback(self._endpoint) and not allow_remote:
            raise VoicevoxConfigurationError("remote VOICEVOX requires VOICE_ALLOW_REMOTE=true")
        if (
            type(max_response_bytes) is not int
            or not MIN_VOICEVOX_WAV_BYTES <= max_response_bytes <= MAX_VOICEVOX_PLAYBACK_WAV_BYTES
        ):
            raise VoicevoxConfigurationError("VOICE_MAX_RESPONSE_BYTES is outside the allowed range")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._session: aiohttp.ClientSession | None = None

    async def probe_version(self, *, timeout_seconds: float) -> bool:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.05 <= float(timeout_seconds) <= 30.0
        ):
            return False
        try:
            async with asyncio.timeout(float(timeout_seconds)):
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(timeout=self._timeout)
                async with self._session.get(
                    f"{self._endpoint}/version",
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=float(timeout_seconds)),
                ) as response:
                    if response.status != 200:
                        return False
                    content_type = response.headers.get("Content-Type", "")
                    if not isinstance(content_type, str) or content_type.split(";", 1)[0].strip().lower() != (
                        "application/json"
                    ):
                        return False
                    raw = await _read_limited(response, 256)
            version = json.loads(raw.decode("utf-8", errors="strict"))
            return isinstance(version, str) and _VOICEVOX_VERSION.fullmatch(version) is not None
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        params = {"text": request.text, "speaker": str(request.speaker_id)}
        async with self._session.post(
            f"{self._endpoint}/audio_query",
            params=params,
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise RuntimeError("VOICEVOX audio query failed")
            raw_query = await _read_limited(response, min(self._max_response_bytes, 1024 * 1024))
        try:
            query: Any = json.loads(raw_query.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("VOICEVOX returned an invalid audio query") from exc
        if not isinstance(query, dict):
            raise RuntimeError("VOICEVOX returned an invalid audio query")
        query["speedScale"] = request.speed_scale
        query["volumeScale"] = request.volume_scale
        async with self._session.post(
            f"{self._endpoint}/synthesis",
            params={"speaker": str(request.speaker_id)},
            json=query,
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise RuntimeError("VOICEVOX synthesis failed")
            wav = await _read_limited(response, self._max_response_bytes)
        try:
            canonical = canonicalize_voicevox_playback_wav(
                wav,
                max_input_bytes=self._max_response_bytes,
            )
        except Exception:
            raise RuntimeError("VOICEVOX returned an invalid WAV") from None
        sample_rate = int.from_bytes(canonical[24:28], "little")
        return SynthesizedSpeech(wav=canonical, sample_rate=sample_rate)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


async def _read_limited(response: Any, limit: int) -> bytes:
    if type(limit) is not int or not 1 <= limit <= MAX_VOICEVOX_PLAYBACK_WAV_BYTES:
        raise RuntimeError("VOICEVOX response limit is invalid")
    declared = getattr(response, "content_length", None)
    if isinstance(declared, int) and declared > limit:
        raise RuntimeError("VOICEVOX response exceeds the configured limit")
    content = getattr(response, "content", None)
    iterator = getattr(content, "iter_chunked", None)
    if not callable(iterator):
        payload = await response.read()
        if len(payload) > limit:
            raise RuntimeError("VOICEVOX response exceeds the configured limit")
        return payload
    chunks: list[bytes] = []
    length = 0
    async for chunk in iterator(64 * 1024):
        length += len(chunk)
        if length > limit:
            raise RuntimeError("VOICEVOX response exceeds the configured limit")
        chunks.append(chunk)
    return b"".join(chunks)
