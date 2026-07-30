from __future__ import annotations

from typing import Protocol

from .domain import Attempt, ExecutionContext, Job, Outcome


class Executor(Protocol):
    """Discord・AI・Voice・Minecraft等の外部副作用adapter共通port。"""

    async def execute(self, job: Job, attempt: Attempt, context: ExecutionContext) -> Outcome: ...


class ExecutorRegistry(Protocol):
    def get(self, kind: str) -> Executor | None: ...
