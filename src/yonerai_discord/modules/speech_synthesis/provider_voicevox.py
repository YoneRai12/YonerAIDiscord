from __future__ import annotations

import asyncio
import hashlib
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from yonerai_discord.modules.music_generation.artifacts import (
    MAX_WAV_BYTES,
    MusicArtifactStore,
    validate_wav,
)
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    HealthStatus,
    LogicalCapability,
    ProviderHealth,
    ProviderInvocation,
    ProviderRequest,
    ProviderResult,
    SpeechSynthesisInput,
    require_execution_allowed,
)
from yonerai_discord.provider_registry.domain import utc_now
from yonerai_discord.provider_registry.ports import ExecutionAuthorizationCheck
from yonerai_discord.voice_contract import (
    MIN_VOICEVOX_WAV_BYTES,
    VOICEVOX_ALLOWED_SPEAKER_IDS,
    VOICEVOX_SPEAKER_ID,
)

from .domain import speech_artifact_request_binding


VOICEVOX_PROVIDER_MODEL = "voicevox-engine"
VOICEVOX_MODEL_ALIASES = ("tts.fast", "tts.balanced", "tts.quality")
MAX_VOICEVOX_PLAYBACK_WAV_BYTES = 50 * 1024 * 1024
_UINT32_MAX = 2**32 - 1


@dataclass(frozen=True, slots=True)
class VoicevoxSynthesisRequest:
    """Provider requestのscopeと本文をVOICEVOX呼出しへ型付きで束縛する。"""

    request_id: str
    trace_id: str
    actor_ref: str
    request_binding: str = field(repr=False)
    text: str = field(repr=False)
    voice_alias: str = "standard"
    language_code: str = "ja-jp"
    speaker_id: int = VOICEVOX_SPEAKER_ID
    speed_scale: float = 1.0
    volume_scale: float = 1.0

    def __post_init__(self) -> None:
        if (
            not self.request_id
            or not self.trace_id
            or not self.actor_ref
            or len(self.request_binding) != 64
            or any(character not in "0123456789abcdef" for character in self.request_binding)
            or self.voice_alias != "standard"
            or self.language_code not in {"ja", "ja-jp"}
            or type(self.speaker_id) is not int
            or self.speaker_id not in VOICEVOX_ALLOWED_SPEAKER_IDS
            or self.speed_scale != 1.0
            or type(self.volume_scale) is not float
            or self.volume_scale != 1.0
        ):
            raise ValueError("VOICEVOX synthesis request is outside the fixed contract")


class VoicevoxSynthesisPort(Protocol):
    async def probe_version(self, *, timeout_seconds: float) -> bool: ...

    async def synthesize(self, request: VoicevoxSynthesisRequest) -> object: ...

    async def close(self) -> None: ...


class VoicevoxSpeechSynthesisProviderAdapter:
    """既存VOICEVOX clientをrequest-boundなProviderAdapter契約へ閉じ込める。"""

    provider_id = "voicevox-local"
    adapter_id = "voicevox-speech-synthesis-v1"

    def __init__(
        self,
        client: VoicevoxSynthesisPort,
        artifact_store: MusicArtifactStore,
        *,
        speaker_id: int = VOICEVOX_SPEAKER_ID,
        readiness_current: Callable[[], bool] | None = None,
        probed_model_aliases: tuple[str, ...] = VOICEVOX_MODEL_ALIASES,
    ) -> None:
        if not callable(getattr(client, "synthesize", None)):
            raise TypeError("client must provide synthesize")
        if not callable(getattr(artifact_store, "put_wav", None)):
            raise TypeError("artifact_store must provide put_wav")
        if type(speaker_id) is not int or speaker_id not in VOICEVOX_ALLOWED_SPEAKER_IDS:
            raise ValueError("speaker_not_allowed")
        self._client = client
        self._store = artifact_store
        self._speaker_id = speaker_id
        self._readiness_current = readiness_current
        self._probed_model_aliases = _model_aliases(probed_model_aliases)
        self._closing = False
        self._operation_lock = asyncio.Lock()

    async def health(self) -> ProviderHealth:
        async with self._operation_lock:
            if not self._ready():
                ready = False
            else:
                probe = getattr(self._client, "probe_version", None)
                try:
                    ready = callable(probe) and await asyncio.wait_for(
                        probe(timeout_seconds=5.0),
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
                detail_code="voicevox_ready" if ready else "voicevox_not_ready",
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
            or request.capability is not LogicalCapability.SPEECH_TTS
            or not isinstance(request.payload, SpeechSynthesisInput)
            or request.input_artifacts
            or invocation.provider_id != self.provider_id
            or invocation.provider_model != VOICEVOX_PROVIDER_MODEL
            or request.payload.voice_alias != "standard"
            or request.payload.language_code not in {"ja", "ja-jp"}
        ):
            raise RuntimeError("VOICEVOX provider contract mismatch")
        binding = speech_artifact_request_binding(
            request,
            provider_id=self.provider_id,
            provider_model=invocation.provider_model,
            model_alias=invocation.model_alias,
            quality_tier=invocation.quality_tier,
        )
        typed_request = VoicevoxSynthesisRequest(
            request_id=request.request_id,
            trace_id=request.trace_id,
            actor_ref=request.actor_ref,
            request_binding=binding,
            text=request.payload.text,
            voice_alias=request.payload.voice_alias,
            language_code=request.payload.language_code,
            speaker_id=self._speaker_id,
        )

        await require_execution_allowed(execution_allowed)
        try:
            synthesized = await self._client.synthesize(typed_request)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RuntimeError("VOICEVOX synthesis failed safely") from None

        try:
            wav = canonicalize_voicevox_wav(getattr(synthesized, "wav", None))
            validated = validate_wav(wav)
        except Exception:
            raise RuntimeError("VOICEVOX returned an invalid WAV") from None
        if not self._ready():
            raise RuntimeError("VOICEVOX provider readiness changed")

        await require_execution_allowed(execution_allowed)
        if not self._ready():
            raise RuntimeError("VOICEVOX provider readiness changed")
        try:
            ref = self._store.put_wav(validated.data, request_binding=binding)
        except Exception:
            raise RuntimeError("VOICEVOX artifact commit failed safely") from None
        if (
            not isinstance(ref, ArtifactRef)
            or ref.kind is not ArtifactKind.AUDIO
            or ref.media_type != "audio/wav"
            or ref.size_bytes != len(validated.data)
            or ref.sha256 != hashlib.sha256(validated.data).hexdigest()
        ):
            raise RuntimeError("VOICEVOX artifact store returned an invalid reference")
        return ProviderResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            provider_model=invocation.provider_model,
            artifacts=(ref,),
        )

    async def close(self) -> None:
        async with self._operation_lock:
            self._closing = True
            close = getattr(self._client, "close", None)
            if callable(close):
                await close()

    def _ready(self) -> bool:
        if self._closing or self._readiness_current is None:
            return False
        try:
            return self._readiness_current() is True
        except Exception:
            return False


def _model_aliases(values: tuple[str, ...]) -> tuple[str, ...]:
    aliases = tuple(values)
    if (
        len(aliases) != 3
        or len(set(aliases)) != len(aliases)
        or any(not isinstance(alias, str) or not alias for alias in aliases)
    ):
        raise ValueError("probed_model_aliases must contain three unique aliases")
    return aliases


def canonicalize_voicevox_wav(data: object) -> bytes:
    """VOICEVOXのexact PCM16 fmt/dataを1–30秒のartifact WAVへ変換する。"""

    return _canonicalize_voicevox_wav(
        data,
        enforce_artifact_duration=True,
        max_wav_bytes=MAX_WAV_BYTES,
    )


def canonicalize_voicevox_playback_wav(
    data: object,
    *,
    max_wav_bytes: int = MAX_WAV_BYTES,
) -> bytes:
    """VOICEVOXのexact PCM16 fmt/dataをbounded direct-playback WAVへ変換する。"""

    return _canonicalize_voicevox_wav(
        data,
        enforce_artifact_duration=False,
        max_wav_bytes=max_wav_bytes,
    )


def _upsample_24khz_pcm16(pcm: bytes, *, block_align: int) -> bytes:
    """1個の上限付き出力へ、frame順を保ってPCM列を複製する。"""

    expanded = bytearray(len(pcm) * 2)
    output_stride = block_align * 2
    for byte_offset in range(block_align):
        column = pcm[byte_offset::block_align]
        expanded[byte_offset::output_stride] = column
        expanded[byte_offset + block_align :: output_stride] = column
    return bytes(expanded)


def _canonicalize_voicevox_wav(
    data: object,
    *,
    enforce_artifact_duration: bool,
    max_wav_bytes: int,
) -> bytes:
    """構造・format・sizeを共有し、artifact固有のdurationだけを分離する。"""

    if type(max_wav_bytes) is not int or not MIN_VOICEVOX_WAV_BYTES <= max_wav_bytes <= MAX_VOICEVOX_PLAYBACK_WAV_BYTES:
        raise RuntimeError("VOICEVOX WAV size limit is invalid")
    if (
        not isinstance(data, bytes)
        or len(data) < 44
        or len(data) > max_wav_bytes
        or data[:4] != b"RIFF"
        or data[8:12] != b"WAVE"
        or struct.unpack_from("<I", data, 4)[0] != len(data) - 8
    ):
        raise RuntimeError("VOICEVOX returned an invalid WAV")

    offset = 12
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(data):
        if offset + 8 > len(data):
            raise RuntimeError("VOICEVOX returned a truncated WAV")
        chunk_type = data[offset : offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        start = offset + 8
        end = start + size
        padded_end = end + (size & 1)
        if end > len(data) or padded_end > len(data):
            raise RuntimeError("VOICEVOX returned a truncated WAV")
        if size & 1 and data[end:padded_end] != b"\0":
            raise RuntimeError("VOICEVOX returned an invalid WAV")
        chunks.append((chunk_type, data[start:end]))
        offset = padded_end
    if offset != len(data) or [kind for kind, _payload in chunks] != [b"fmt ", b"data"]:
        raise RuntimeError("VOICEVOX WAV chunks are outside the fixed contract")

    fmt, pcm = chunks[0][1], chunks[1][1]
    if len(fmt) != 16:
        raise RuntimeError("VOICEVOX WAV fmt chunk is invalid")
    audio_format, channels, sample_rate, byte_rate, block_align, bits = struct.unpack("<HHIIHH", fmt)
    if (
        audio_format != 1
        or bits != 16
        or channels not in {1, 2}
        or sample_rate not in {24_000, 44_100, 48_000}
        or block_align != channels * 2
        or byte_rate != sample_rate * block_align
        or not pcm
        or len(pcm) % block_align
    ):
        raise RuntimeError("VOICEVOX WAV format is unsupported")
    frames = len(pcm) // block_align
    duration = frames / sample_rate
    if enforce_artifact_duration and not 1.0 <= duration <= 30.0:
        raise RuntimeError("VOICEVOX WAV duration is outside the fixed contract")
    if sample_rate == 24_000:
        expanded_pcm_size = len(pcm) * 2
        if expanded_pcm_size > _UINT32_MAX or 44 + expanded_pcm_size > max_wav_bytes:
            raise RuntimeError("VOICEVOX WAV exceeds the fixed size limit")
        pcm = _upsample_24khz_pcm16(pcm, block_align=block_align)
        if len(pcm) != expanded_pcm_size:
            raise RuntimeError("VOICEVOX WAV conversion failed safely")
        sample_rate = 48_000
    byte_rate = sample_rate * block_align
    canonical_size = 44 + len(pcm)
    riff_size = canonical_size - 8
    if canonical_size > max_wav_bytes or len(pcm) > _UINT32_MAX or riff_size > _UINT32_MAX:
        raise RuntimeError("VOICEVOX WAV exceeds the fixed size limit")
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


__all__ = [
    "MAX_VOICEVOX_PLAYBACK_WAV_BYTES",
    "VOICEVOX_PROVIDER_MODEL",
    "canonicalize_voicevox_playback_wav",
    "canonicalize_voicevox_wav",
    "VoicevoxSpeechSynthesisProviderAdapter",
    "VoicevoxSynthesisPort",
    "VoicevoxSynthesisRequest",
]
