from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


MUSIC_GENERATION_CAPABILITY_ID = "cap-run-music-generate"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        MUSIC_GENERATION_CAPABILITY_ID,
        "media.music-generation",
        "権利確認済みpromptから検証済みPCM16 WAVを生成",
        command="musicgen generate",
        plugin="music_generation",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.HIGH,
        default_enabled=False,
    ),
)


__all__ = ["CAPABILITIES", "MUSIC_GENERATION_CAPABILITY_ID"]
