from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from .config import YonerAIRuntimeConfig
from .contract import ProbeBudget, ReadinessOutcome, YonerAIReadinessGateway


class BoundaryState(StrEnum):
    DISABLED = "disabled"
    LOCAL_ONLY = "local_only"
    REMOTE_OPT_IN_INCOMPLETE = "remote_opt_in_incomplete"
    CONTRACT_PENDING = "contract_pending"
    READY_TO_PROBE = "ready_to_probe"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class BoundaryStatus:
    state: BoundaryState
    enabled: bool
    remote_permitted: bool
    token_configured: bool


class YonerAIStatusService:
    """副作用のない状態確認だけを公開する境界service。"""

    def __init__(
        self,
        config: YonerAIRuntimeConfig,
        gateway: YonerAIReadinessGateway | None = None,
    ) -> None:
        self.config = config
        self._gateway = gateway

    def status(self) -> BoundaryStatus:
        local_probe = getattr(self._gateway, "local_only", False) is True
        if not self.config.enabled:
            state = BoundaryState.DISABLED
        elif self._gateway is not None and (self.config.remote_permitted or local_probe):
            state = BoundaryState.READY_TO_PROBE
        elif not self.config.allow_remote and not self.config.remote_status_opt_in:
            state = BoundaryState.LOCAL_ONLY
        elif not self.config.remote_permitted:
            state = BoundaryState.REMOTE_OPT_IN_INCOMPLETE
        else:
            state = BoundaryState.CONTRACT_PENDING
        return BoundaryStatus(
            state=state,
            enabled=self.config.enabled,
            remote_permitted=self.config.remote_permitted,
            token_configured=self.config.token_configured,
        )

    async def health(self) -> BoundaryStatus:
        current = self.status()
        local_probe = getattr(self._gateway, "local_only", False) is True
        if not self.config.enabled or self._gateway is None or (not self.config.remote_permitted and not local_probe):
            return current
        budget = ProbeBudget(
            timeout_seconds=float(self.config.timeout_seconds),
            max_response_bytes=self.config.max_response_bytes,
        )
        try:
            async with asyncio.timeout(float(self.config.timeout_seconds)):
                result = await self._gateway.probe(budget)
        except (TimeoutError, OSError, ValueError, TypeError):
            state = BoundaryState.UNAVAILABLE
        else:
            state = {
                ReadinessOutcome.HEALTHY: BoundaryState.HEALTHY,
                ReadinessOutcome.DEGRADED: BoundaryState.DEGRADED,
                ReadinessOutcome.UNAVAILABLE: BoundaryState.UNAVAILABLE,
            }.get(result.outcome, BoundaryState.UNAVAILABLE)
        return BoundaryStatus(
            state=state,
            enabled=True,
            remote_permitted=self.config.remote_permitted,
            token_configured=self.config.token_configured,
        )
