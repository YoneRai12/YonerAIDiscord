from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from yonerai_discord.provider_registry import (
    ArtifactRef,
    ProviderRequest,
    QualityTier,
    SpeechSynthesisInput,
)


SPEECH_SYNTHESIS_MODULE_ID = "media.speech-synthesis"
SPEECH_SYNTHESIS_PLUGIN_NAME = "speech_synthesis"
SPEECH_SYNTHESIS_CAPABILITY_ID = "cap-run-speech-synthesize"
GENERATED_SPEECH_FILENAME = "generated-speech.wav"
MAX_SPEECH_TEXT_CHARS = 500
STANDARD_VOICE_ALIASES = frozenset({"standard"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SpeechSynthesisError(RuntimeError):
    pass


class SpeechSynthesisUnavailableError(SpeechSynthesisError):
    pass


class SpeechSynthesisAuthorizationError(SpeechSynthesisError):
    pass


class SpeechSynthesisContractError(SpeechSynthesisError):
    pass


class SpeechSynthesisIdempotencyError(SpeechSynthesisError):
    pass


@dataclass(frozen=True, slots=True)
class SpeechSynthesisRequest:
    request_id: str
    guild_id: int
    channel_id: int
    actor_id: int
    text: str = field(repr=False)
    voice_alias: str = "standard"
    language_code: str = "ja-JP"
    tier: QualityTier = QualityTier.BALANCED

    def __post_init__(self) -> None:
        request_id = _identifier(self.request_id, "request_id")
        for name in ("guild_id", "channel_id", "actor_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        text = self.text.strip()
        if (
            not text
            or len(text) > MAX_SPEECH_TEXT_CHARS
            or any(
                unicodedata.category(character).startswith("C") and character not in {"\n", "\t"} for character in text
            )
        ):
            raise ValueError("speech text is outside the Stage 1 range")
        typed_input = SpeechSynthesisInput(
            text,
            voice_alias=self.voice_alias,
            language_code=self.language_code,
        )
        if typed_input.voice_alias not in STANDARD_VOICE_ALIASES:
            raise ValueError("voice_alias is not a standard Stage 1 voice")
        if typed_input.language_code is None:
            raise ValueError("language_code is required")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "voice_alias", typed_input.voice_alias)
        object.__setattr__(self, "language_code", typed_input.language_code)
        object.__setattr__(self, "tier", QualityTier(self.tier))

    @property
    def actor_ref(self) -> str:
        return f"discord-user-{self.actor_id}"

    @property
    def trace_id(self) -> str:
        return f"trace-{self.request_id}"

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            "\0".join(
                (
                    self.request_id,
                    str(self.guild_id),
                    str(self.channel_id),
                    str(self.actor_id),
                    self.text_hash,
                    self.voice_alias,
                    self.language_code,
                    self.tier.value,
                )
            ).encode()
        ).hexdigest()

    @property
    def provider_request_id(self) -> str:
        return f"tts-request-{self.fingerprint[:48]}"


@dataclass(frozen=True, slots=True)
class SynthesizedAudio:
    artifact: ArtifactRef
    wav: bytes = field(repr=False)
    request_binding: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ArtifactRef):
            raise TypeError("artifact must be an ArtifactRef")
        if not isinstance(self.wav, bytes) or not self.wav:
            raise ValueError("wav must contain bytes")
        if not isinstance(self.request_binding, str) or not _SHA256.fullmatch(self.request_binding):
            raise ValueError("request_binding must be a SHA-256 digest")


def speech_artifact_request_binding(
    request: ProviderRequest,
    *,
    provider_id: str,
    provider_model: str | None,
    model_alias: str | None,
    quality_tier: QualityTier,
) -> str:
    """Scopeと本文hashを含むopaque provider requestを実routeへ束縛する。"""

    if not isinstance(request, ProviderRequest):
        raise TypeError("request must be a ProviderRequest")
    return hashlib.sha256(
        "\0".join(
            (
                request.request_id,
                request.trace_id,
                request.actor_ref,
                request.capability.value,
                provider_id,
                provider_model or "",
                model_alias or "",
                QualityTier(quality_tier).value,
            )
        ).encode()
    ).hexdigest()


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if (
        not normalized
        or len(normalized) > 120
        or not normalized[0].isalnum()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in normalized)
    ):
        raise ValueError(f"{label} must be a lowercase identifier")
    return normalized


__all__ = [
    "GENERATED_SPEECH_FILENAME",
    "MAX_SPEECH_TEXT_CHARS",
    "SPEECH_SYNTHESIS_CAPABILITY_ID",
    "SPEECH_SYNTHESIS_MODULE_ID",
    "SPEECH_SYNTHESIS_PLUGIN_NAME",
    "STANDARD_VOICE_ALIASES",
    "SpeechSynthesisAuthorizationError",
    "SpeechSynthesisContractError",
    "SpeechSynthesisError",
    "SpeechSynthesisIdempotencyError",
    "SpeechSynthesisRequest",
    "SpeechSynthesisUnavailableError",
    "SynthesizedAudio",
    "speech_artifact_request_binding",
]
