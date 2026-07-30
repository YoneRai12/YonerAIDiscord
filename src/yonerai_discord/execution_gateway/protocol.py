from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .models import CapabilityResult, RunEvent, RunInput, RunReference


class ExecutionGateway(Protocol):
    """Discord Surfaceが依存する唯一のrun実行境界。"""

    async def start(self, request: RunInput) -> RunReference: ...

    def events(self, run_id: str) -> AsyncIterator[RunEvent]: ...

    async def submit_result(self, run_id: str, result: CapabilityResult) -> None: ...

    async def cancel(self, run_id: str) -> None: ...


__all__ = ["ExecutionGateway"]
