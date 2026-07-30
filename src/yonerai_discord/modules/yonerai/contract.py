from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ReadinessOutcome(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ProbeBudget:
    """将来のread-only adapterが必ず守る資源上限。"""

    timeout_seconds: float
    max_response_bytes: int


@dataclass(frozen=True, slots=True)
class RemoteReadiness:
    """Discord側が依存してよい最小のYonerAI readiness contract。

    公式APIのJSON形状、業務データ、応答本文をこの型へ持ち込まない。
    公式contract確定後のadapterが検証済み結果だけを変換する。
    """

    outcome: ReadinessOutcome


class YonerAIReadinessGateway(Protocol):
    """外部実装用のread-only port。書き込み・ジョブ投入・コード実行は禁止。"""

    async def probe(self, budget: ProbeBudget) -> RemoteReadiness: ...
