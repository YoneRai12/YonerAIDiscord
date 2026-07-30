"""既定OFF・provider/store/Discord入口未接続の音声合成Stage 1。"""

from __future__ import annotations

from typing import Any

from .adapter import SpeechSynthesisDelivery
from .discord_sink import DiscordSpeechSynthesisSink
from .domain import (
    GENERATED_SPEECH_FILENAME,
    MAX_SPEECH_TEXT_CHARS,
    SPEECH_SYNTHESIS_CAPABILITY_ID,
    SPEECH_SYNTHESIS_MODULE_ID,
    SPEECH_SYNTHESIS_PLUGIN_NAME,
    STANDARD_VOICE_ALIASES,
    SpeechSynthesisAuthorizationError,
    SpeechSynthesisContractError,
    SpeechSynthesisError,
    SpeechSynthesisIdempotencyError,
    SpeechSynthesisRequest,
    SpeechSynthesisUnavailableError,
    SynthesizedAudio,
    speech_artifact_request_binding,
)
from .plugin import SpeechSynthesisPlugin
from .service import SpeechSynthesisService


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register(SPEECH_SYNTHESIS_PLUGIN_NAME, SpeechSynthesisPlugin)


__all__ = [
    "GENERATED_SPEECH_FILENAME",
    "MAX_SPEECH_TEXT_CHARS",
    "SPEECH_SYNTHESIS_CAPABILITY_ID",
    "SPEECH_SYNTHESIS_MODULE_ID",
    "SPEECH_SYNTHESIS_PLUGIN_NAME",
    "STANDARD_VOICE_ALIASES",
    "SpeechSynthesisAuthorizationError",
    "SpeechSynthesisContractError",
    "SpeechSynthesisDelivery",
    "DiscordSpeechSynthesisSink",
    "SpeechSynthesisError",
    "SpeechSynthesisIdempotencyError",
    "SpeechSynthesisPlugin",
    "SpeechSynthesisRequest",
    "SpeechSynthesisService",
    "SpeechSynthesisUnavailableError",
    "SynthesizedAudio",
    "setup",
    "speech_artifact_request_binding",
]
