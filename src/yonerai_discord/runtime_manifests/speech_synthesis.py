from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


SPEECH_SYNTHESIS_CAPABILITY_ID = "cap-run-speech-synthesize"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        SPEECH_SYNTHESIS_CAPABILITY_ID,
        "media.speech-synthesis",
        "bounded textとstandard voice aliasを検証済みPCM WAV 1件へ音声合成",
        plugin="speech_synthesis",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.MEDIUM,
        default_enabled=False,
    ),
)


__all__ = ["CAPABILITIES", "SPEECH_SYNTHESIS_CAPABILITY_ID"]
