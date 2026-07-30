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
        if not self.config.enabled:
            state = BoundaryState.DISABLED
        elif not self.config.allow_remote and not self.config.remote_status_opt_in:
            state = BoundaryState.LOCAL_ONLY
        elif not self.config.remote_permitted:
            state = BoundaryState.REMOTE_OPT_IN_INCOMPLETE
        elif self._gateway is None:
            # 公式API contractが未確定の現時点では、ここで止めて外へ接続しない。
            state = BoundaryState.CONTRACT_PENDING
        else:
            state = BoundaryState.READY_TO_PROBE
        return BoundaryStatus(
            state=state,
            enabled=self.config.enabled,
            remote_permitted=self.config.remote_permitted,
            token_configured=self.config.token_configured,
        )

    async def health(self) -> BoundaryStatus:
        current = self.status()
        if not self.config.remote_permitted or self._gateway is None:
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
            remote_permitted=True,
            token_configured=self.config.token_configured,
        )
