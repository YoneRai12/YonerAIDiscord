"""既定OFF・read-onlyのYonerAI将来連携境界。"""

from __future__ import annotations

from typing import Any

from .adapter import YonerAIGroup, render_status
from .config import YonerAIConfigurationError, YonerAIRuntimeConfig
from .contract import (
    ProbeBudget,
    ReadinessOutcome,
    RemoteReadiness,
    YonerAIReadinessGateway,
)
from .service import BoundaryState, BoundaryStatus, YonerAIStatusService
from .transport import YonerAIResponseLimitError, read_bounded_response


class YonerAIPlugin:
    def __init__(self) -> None:
        self._bot: Any | None = None
        self.service: YonerAIStatusService | None = None

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            return
        config = YonerAIRuntimeConfig.load(bot.settings)
        # 公式API仕様が確定するまでgatewayは注入しない。remote opt-inが揃っても外へ接続しない。
        service = YonerAIStatusService(config)
        bot.tree.add_command(YonerAIGroup(service))
        setattr(bot, "yonerai_status_service", service)
        self.service = service
        self._bot = bot

    async def stop(self) -> None:
        if self._bot is not None:
            self._bot.tree.remove_command("yonerai")
            if getattr(self._bot, "yonerai_status_service", None) is self.service:
                delattr(self._bot, "yonerai_status_service")
        self.service = None
        self._bot = None


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("yonerai", YonerAIPlugin)


__all__ = [
    "BoundaryState",
    "BoundaryStatus",
    "ProbeBudget",
    "ReadinessOutcome",
    "RemoteReadiness",
    "YonerAIConfigurationError",
    "YonerAIPlugin",
    "YonerAIReadinessGateway",
    "YonerAIResponseLimitError",
    "YonerAIRuntimeConfig",
    "YonerAIStatusService",
    "render_status",
    "read_bounded_response",
    "setup",
]
