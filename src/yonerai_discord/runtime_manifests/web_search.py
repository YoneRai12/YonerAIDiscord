from __future__ import annotations

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID = "cap-run-web-search-openai-paid"


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID,
        "integration.api-web",
        "Owner明示opt-in専用のOpenAI有料Web検索tool",
        plugin="ai",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.MEDIUM,
        default_enabled=False,
        owner_only=True,
    ),
)


__all__ = ["CAPABILITIES", "OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID"]
