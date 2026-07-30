from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


SPEECH_TRANSCRIPTION_CAPABILITY_ID = "cap-run-speech-transcribe"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        SPEECH_TRANSCRIPTION_CAPABILITY_ID,
        "media.speech-transcription",
        "検証済みaudio artifact 1件をbounded textへ文字起こし",
        plugin="speech_transcription",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.HIGH,
        default_enabled=False,
    ),
)


__all__ = ["CAPABILITIES", "SPEECH_TRANSCRIPTION_CAPABILITY_ID"]
