from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID = "cap-run-browser-remote-screenshot"
REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID = "cap-run-browser-remote-interactive"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID,
        "web.browser-rendering",
        "明示opt-inしたCloudflare Browser Rendering remote screenshot経路",
        plugin="browser_rendering",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.HIGH,
        default_enabled=False,
        owner_only=True,
    ),
    _cap(
        REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID,
        "web.browser-rendering",
        "明示opt-inしたCloudflare Browser Runの固定YouTube操作経路",
        plugin="browser_rendering",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.HIGH,
        default_enabled=False,
        owner_only=True,
    ),
)


__all__ = [
    "CAPABILITIES",
    "REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID",
    "REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID",
]
