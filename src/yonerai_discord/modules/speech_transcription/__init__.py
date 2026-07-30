"""既定OFF・provider/audio入口未接続の音声文字起こしStage 1。"""

from __future__ import annotations

from typing import Any

from .adapter import DiscordSpeechTranscriptionDelivery
from .artifacts import (
    ALLOWED_CHANNELS,
    ALLOWED_SAMPLE_RATES,
    MAX_AUDIO_SECONDS,
    MIN_AUDIO_SECONDS,
    BoundedSpeechAudioStore,
    SpeechAudioArtifactError,
    validate_pcm_wav,
)
from .domain import (
    ALLOWED_AUDIO_MEDIA_TYPES,
    MAX_AUDIO_BYTES,
    MAX_TRANSCRIPT_CHARS,
    SPEECH_TRANSCRIPTION_CAPABILITY_ID,
    SPEECH_TRANSCRIPTION_MODULE_ID,
    SPEECH_TRANSCRIPTION_PLUGIN_NAME,
    SpeechTranscript,
    SpeechTranscriptionAuthorizationError,
    SpeechTranscriptionContractError,
    SpeechTranscriptionError,
    SpeechTranscriptionIdempotencyError,
    SpeechTranscriptionRequest,
    SpeechTranscriptionUnavailableError,
)
from .plugin import SpeechTranscriptionPlugin
from .service import SpeechTranscriptionService


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(
        manager,
        "register",
        None,
    )
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register(
        SPEECH_TRANSCRIPTION_PLUGIN_NAME,
        SpeechTranscriptionPlugin,
    )


__all__ = [
    "ALLOWED_AUDIO_MEDIA_TYPES",
    "ALLOWED_CHANNELS",
    "ALLOWED_SAMPLE_RATES",
    "BoundedSpeechAudioStore",
    "MAX_AUDIO_BYTES",
    "MAX_AUDIO_SECONDS",
    "MAX_TRANSCRIPT_CHARS",
    "MIN_AUDIO_SECONDS",
    "SPEECH_TRANSCRIPTION_CAPABILITY_ID",
    "SPEECH_TRANSCRIPTION_MODULE_ID",
    "SPEECH_TRANSCRIPTION_PLUGIN_NAME",
    "DiscordSpeechTranscriptionDelivery",
    "SpeechTranscript",
    "SpeechAudioArtifactError",
    "SpeechTranscriptionAuthorizationError",
    "SpeechTranscriptionContractError",
    "SpeechTranscriptionError",
    "SpeechTranscriptionIdempotencyError",
    "SpeechTranscriptionPlugin",
    "SpeechTranscriptionRequest",
    "SpeechTranscriptionService",
    "SpeechTranscriptionUnavailableError",
    "setup",
    "validate_pcm_wav",
]
