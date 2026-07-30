from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


MEDIA_URL_INSPECTION_CAPABILITY_ID = "cap-run-media-url-inspection"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        MEDIA_URL_INSPECTION_CAPABILITY_ID,
        "media.url-inspection",
        "明示された公開YouTube URLを隔離Hyper-V VMまたは明示remote providerで解析",
        plugin="media_inspection",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.HIGH,
        default_enabled=False,
        owner_only=True,
    ),
)


__all__ = ["CAPABILITIES", "MEDIA_URL_INSPECTION_CAPABILITY_ID"]
