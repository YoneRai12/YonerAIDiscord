"""Sealed Stage 1 recipe success to lifecycle bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from .domain import RecipeCandidate, RecipeReuseClass, RecipeRunResult, RecipeRunStatus
from .lifecycle import CodeOwnedDescription, SqliteForgeLifecycleRepository
from .recipe import ForgePrimitiveRegistry
from .static_templates import ProductionRecipeRunner


_DESCRIPTION = CodeOwnedDescription("固定純粋テンプレートの成功候補", code_owned=True)


class ProductionRecipeLifecycleBridge:
    """Records only current, sealed, successful reusable-template receipts."""

    def __init__(
        self,
        *,
        runner: ProductionRecipeRunner,
        repository: SqliteForgeLifecycleRepository,
        current: Callable[[], bool],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(runner) is not ProductionRecipeRunner:
            raise TypeError("runner must be the sealed ProductionRecipeRunner")
        if type(repository) is not SqliteForgeLifecycleRepository:
            raise TypeError("repository must be SqliteForgeLifecycleRepository")
        if not callable(current):
            raise TypeError("current must be callable")
        registry = getattr(runner, "_registry", None)
        if type(registry) is not ForgePrimitiveRegistry or not registry.is_production_sealed:
            raise TypeError("runner registry must remain production sealed")
        self._runner = runner
        self._registry = registry
        self._repository = repository
        self._current = current
        self._clock = clock or (lambda: datetime.now(UTC))
        self._record_lock = asyncio.Lock()
        self._closed = False

    async def run(self, *, owner_user_id: int, candidate: RecipeCandidate) -> RecipeRunResult:
        """Runs the sealed recipe and best-effort records an eligible success.

        A lifecycle write failure does not rewrite the successful Stage 1
        receipt into an official or promoted state.
        """
        if not _valid_owner(owner_user_id) or not isinstance(candidate, RecipeCandidate):
            raise TypeError("owner_user_id and candidate must be typed values")
        if self._closed or not self._is_current():
            return await self._runner.run(candidate)

        result = await self._runner.run(candidate)
        if not self._eligible(candidate, result):
            return result
        async with self._record_lock:
            if self._closed or not self._is_current():
                return result
            try:
                await asyncio.to_thread(
                    self._repository.record_success,
                    user_id=owner_user_id,
                    candidate=candidate,
                    receipt=result.receipt,
                    registry=self._registry,
                    description=_DESCRIPTION,
                    succeeded_at=self._clock(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed record is never evidence of lifecycle discovery,
                # notification, promotion, module enablement, or readiness.
                pass
        return result

    async def begin_close(self) -> None:
        """Linearizes plugin shutdown with a lifecycle write in progress."""
        async with self._record_lock:
            self._closed = True

    def _is_current(self) -> bool:
        try:
            return self._current() is True and getattr(self._runner, "_registry", None) is self._registry
        except Exception:
            return False

    def _eligible(self, candidate: RecipeCandidate, result: RecipeRunResult) -> bool:
        receipt = result.receipt
        return (
            receipt.status is RecipeRunStatus.SUCCEEDED
            and receipt.reuse_class is RecipeReuseClass.TEMPLATE_REUSABLE
            and receipt.recipe_digest == candidate.digest
            and receipt.completed_steps == receipt.total_steps == len(candidate.steps)
        )


def _valid_owner(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


__all__ = ["ProductionRecipeLifecycleBridge"]
