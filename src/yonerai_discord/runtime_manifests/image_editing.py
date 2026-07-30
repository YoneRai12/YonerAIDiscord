from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


IMAGE_EDITING_CAPABILITY_ID = "cap-run-image-edit"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        IMAGE_EDITING_CAPABILITY_ID,
        "media.image-editing",
        "検証済みPNGを別IDのcanonical PNGへ編集",
        plugin="image_editing",
        level=RbacLevel.TRUSTED,
        risk=RiskLevel.HIGH,
        default_enabled=False,
    ),
)


__all__ = ["CAPABILITIES", "IMAGE_EDITING_CAPABILITY_ID"]
