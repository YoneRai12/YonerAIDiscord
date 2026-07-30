from __future__ import annotations

import re
from collections.abc import Mapping

from .domain import Attempt, ExecutionContext, Job, Outcome, Receipt
from .ports import Executor


_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class InternalNoopExecutor:
    """副作用を持たない内蔵handler。runtimeのlease/queue診断にのみ使う。"""

    async def execute(self, job: Job, attempt: Attempt, context: ExecutionContext) -> Outcome:
        if not context.complete_without_side_effect():
            return Outcome.skipped("execution no longer allowed")
        if job.payload:
            return Outcome.nonretryable_failure(
                "UnexpectedPayload",
                "internal.noop does not accept payload fields",
            )
        return Outcome.succeeded(Receipt(details={"handler": "internal.noop"}))


class ExplicitExecutorRegistry:
    """
    信頼済みPython codeから明示的に渡されたexecutorだけを登録する。

    import path、payload内のクラス名、eval/exec等からの動的解決は行わない。
    """

    def __init__(self, executors: Mapping[str, Executor] | None = None) -> None:
        self._executors: dict[str, Executor] = {}
        self.register("internal.noop", InternalNoopExecutor())
        for kind, executor in (executors or {}).items():
            self.register(kind, executor)

    def register(self, kind: str, executor: Executor) -> None:
        normalized = self._normalize_kind(kind)
        if normalized in self._executors:
            raise ValueError(f"executor is already registered: {normalized}")
        if not callable(getattr(executor, "execute", None)):
            raise TypeError("executor must provide execute()")
        self._executors[normalized] = executor

    def get(self, kind: str) -> Executor | None:
        try:
            normalized = self._normalize_kind(kind)
        except (TypeError, ValueError):
            return None
        return self._executors.get(normalized)

    def unregister_if_current(self, kind: str, expected: Executor) -> bool:
        try:
            normalized = self._normalize_kind(kind)
        except (TypeError, ValueError):
            return False
        if normalized == "internal.noop":
            return False
        if self._executors.get(normalized) is not expected:
            return False
        del self._executors[normalized]
        return True

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))

    @staticmethod
    def _normalize_kind(kind: str) -> str:
        if not isinstance(kind, str):
            raise TypeError("executor kind must be a string")
        normalized = kind.strip().lower()
        if not _KIND_RE.fullmatch(normalized):
            raise ValueError("executor kind must match [a-z][a-z0-9_.-]{0,63}")
        return normalized
