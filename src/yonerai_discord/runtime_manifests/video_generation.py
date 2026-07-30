from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


VIDEO_GENERATION_CAPABILITY_ID = "cap-run-video-generate"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        VIDEO_GENERATION_CAPABILITY_ID,
        "media.video-generation",
        "明示promptから検証済みMP4動画を生成",
        command="video generate",
        plugin="video_generation",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.HIGH,
        default_enabled=False,
    ),
)


__all__ = ["CAPABILITIES", "VIDEO_GENERATION_CAPABILITY_ID"]
