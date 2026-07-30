"""Cleanup-confirmed external sandbox success to proposal lifecycle bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from .lifecycle import SqliteForgeLifecycleRepository
from .sandbox_contract import (
    SandboxCandidate,
    SandboxEntrypoint,
    SandboxResult,
    SandboxScope,
)
from .sandbox_service import ExternalSandboxService, SandboxRunOutcome, SandboxRunStatus


class SandboxProposalLifecycleBridge:
    """Record fixed proposal metadata only after confirmed sandbox cleanup."""

    def __init__(
        self,
        *,
        sandbox: ExternalSandboxService,
        repository: SqliteForgeLifecycleRepository,
        current: Callable[[], bool],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(sandbox) is not ExternalSandboxService:
            raise TypeError("sandbox must be ExternalSandboxService")
        if type(repository) is not SqliteForgeLifecycleRepository:
            raise TypeError("repository must be SqliteForgeLifecycleRepository")
        if not callable(current):
            raise TypeError("current must be callable")
        self._sandbox = sandbox
        self._repository = repository
        self._current = current
        self._clock = clock or (lambda: datetime.now(UTC))
        self._record_lock = asyncio.Lock()
        self._closed = False

    async def run(
        self,
        *,
        owner_user_id: int,
        candidate: SandboxCandidate,
        scope: SandboxScope,
        backend_identity: str,
    ) -> SandboxRunOutcome:
        if (
            isinstance(owner_user_id, bool)
            or not isinstance(owner_user_id, int)
            or owner_user_id <= 0
            or not isinstance(candidate, SandboxCandidate)
            or not isinstance(scope, SandboxScope)
        ):
            raise TypeError("owner_user_id, candidate, and scope must be typed values")
        if scope.user_id != owner_user_id:
            raise ValueError("owner_user_id must match sandbox scope")

        outcome = await self._sandbox.run(
            candidate=candidate,
            scope=scope,
            backend_identity=backend_identity,
        )
        result = outcome.result
        if (
            self._closed
            or not self._is_current()
            or outcome.status is not SandboxRunStatus.SUCCEEDED
            or type(result) is not SandboxResult
            or candidate.entrypoint is not SandboxEntrypoint.PYTHON_PURE
            or result.scope != scope
            or result.backend_identity != backend_identity
        ):
            return outcome

        async with self._record_lock:
            if self._closed or not self._is_current():
                return outcome
            try:
                self._repository.record_sandbox_success(
                    user_id=owner_user_id,
                    sandbox=self._sandbox,
                    outcome=outcome,
                    candidate=candidate,
                    succeeded_at=self._clock(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed metadata write is never promotion or runtime evidence.
                pass
        return outcome

    async def begin_close(self) -> None:
        async with self._record_lock:
            self._closed = True

    def _is_current(self) -> bool:
        try:
            return self._current() is True
        except Exception:
            return False


__all__ = ["SandboxProposalLifecycleBridge"]
