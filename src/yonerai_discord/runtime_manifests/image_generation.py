from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


IMAGE_GENERATION_CAPABILITY_ID = "cap-run-image-generate"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        IMAGE_GENERATION_CAPABILITY_ID,
        "media.image-generation",
        "明示promptから検証済みPNG画像を生成",
        command="image generate",
        plugin="image_generation",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.HIGH,
        default_enabled=False,
    ),
)


__all__ = ["CAPABILITIES", "IMAGE_GENERATION_CAPABILITY_ID"]
