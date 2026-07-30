from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


ADMIN_UI_READ_CAPABILITY_ID = "cap-run-admin-ui-read"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        ADMIN_UI_READ_CAPABILITY_ID,
        "security.admin-ui",
        "選択guildのredactedな管理状態をGETで表示",
        plugin="admin_ui",
        level=RbacLevel.GUILD_ADMIN,
        risk=RiskLevel.HIGH,
        default_enabled=False,
    ),
)


__all__ = ["ADMIN_UI_READ_CAPABILITY_ID", "CAPABILITIES"]
