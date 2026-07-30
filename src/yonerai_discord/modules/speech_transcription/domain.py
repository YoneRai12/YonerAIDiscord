from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    QualityTier,
    SpeechTranscriptionInput,
)


SPEECH_TRANSCRIPTION_MODULE_ID = "media.speech-transcription"
SPEECH_TRANSCRIPTION_PLUGIN_NAME = "speech_transcription"
SPEECH_TRANSCRIPTION_CAPABILITY_ID = "cap-run-speech-transcribe"
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 1_900
ALLOWED_AUDIO_MEDIA_TYPES = frozenset(
    {
        "audio/flac",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SpeechTranscriptionError(RuntimeError):
    pass


class SpeechTranscriptionUnavailableError(SpeechTranscriptionError):
    pass


class SpeechTranscriptionAuthorizationError(SpeechTranscriptionError):
    pass


class SpeechTranscriptionContractError(SpeechTranscriptionError):
    pass


class SpeechTranscriptionIdempotencyError(SpeechTranscriptionError):
    pass


@dataclass(frozen=True, slots=True)
class SpeechTranscriptionRequest:
    request_id: str
    guild_id: int
    channel_id: int
    actor_id: int
    audio: ArtifactRef = field(repr=False)
    language_code: str | None = None
    prompt: str = field(default="", repr=False)
    tier: QualityTier = QualityTier.BALANCED

    def __post_init__(self) -> None:
        request_id = _identifier(self.request_id, "request_id")
        for name in ("guild_id", "channel_id", "actor_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.audio, ArtifactRef):
            raise TypeError("audio must be an ArtifactRef")
        if (
            self.audio.kind is not ArtifactKind.AUDIO
            or self.audio.media_type not in ALLOWED_AUDIO_MEDIA_TYPES
            or self.audio.size_bytes is None
            or not 1 <= self.audio.size_bytes <= MAX_AUDIO_BYTES
            or self.audio.sha256 is None
        ):
            raise ValueError("audio ArtifactRef is outside the Stage 1 contract")
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be a string")
        prompt = self.prompt.strip()
        typed_input = SpeechTranscriptionInput(language_code=self.language_code, prompt=prompt)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "language_code", typed_input.language_code)
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "tier", QualityTier(self.tier))

    @property
    def actor_ref(self) -> str:
        return f"discord-user-{self.actor_id}"

    @property
    def trace_id(self) -> str:
        return f"trace-{self.request_id}"

    @property
    def prompt_hash(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()

    @property
    def audio_binding(self) -> str:
        return hashlib.sha256(
            "\0".join(
                (
                    self.request_id,
                    str(self.guild_id),
                    str(self.channel_id),
                    str(self.actor_id),
                    self.audio.artifact_id,
                    self.audio.kind.value,
                    self.audio.media_type,
                    str(self.audio.size_bytes),
                    self.audio.sha256 or "",
                    self.language_code or "",
                    self.prompt_hash,
                    self.tier.value,
                )
            ).encode()
        ).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self.audio_binding

    @property
    def provider_request_id(self) -> str:
        # Production providerのArtifact resolverが、serviceで検証済みの
        # request bindingを別のlookup表なしでexact再検証できるよう全digestを保持する。
        return f"stt-request-{self.fingerprint}"


@dataclass(frozen=True, slots=True)
class SpeechTranscript:
    request_id: str
    text: str = field(repr=False)
    request_binding: str = field(repr=False)

    def __post_init__(self) -> None:
        request_id = _identifier(self.request_id, "request_id")
        text = normalize_transcript_text(self.text)
        if not isinstance(self.request_binding, str) or not _SHA256.fullmatch(self.request_binding):
            raise ValueError("request_binding must be a SHA-256 digest")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "text", text)


def normalize_transcript_text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("text must be a string")
    text = value.strip()
    if (
        not text
        or len(text) > MAX_TRANSCRIPT_CHARS
        or any(unicodedata.category(character).startswith("C") and character not in {"\n", "\t"} for character in text)
    ):
        raise ValueError("transcript text is outside the allowed range")
    return text


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
