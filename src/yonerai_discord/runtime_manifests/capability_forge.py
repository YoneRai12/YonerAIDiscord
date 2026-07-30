from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


FORGE_OWNER_NOTIFICATION_CAPABILITY_ID = "cap-run-capability-forge-owner-notification"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        FORGE_OWNER_NOTIFICATION_CAPABILITY_ID,
        "intelligence.capability-forge",
        "Recipe Forge の owner-only DM review",
        plugin="capability_forge",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.HIGH,
        default_enabled=False,
        owner_only=True,
    ),
)


__all__ = ["CAPABILITIES", "FORGE_OWNER_NOTIFICATION_CAPABILITY_ID"]
