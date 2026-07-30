from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from .service import DurableJobService


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    running: bool
    draining: bool
    cycles: int
    processed: int
    last_cycle_at: datetime | None
    last_error_type: str | None


class DurableJobWorker:
    """bounded batchでpollし、stop後は新しいclaimを取らずgraceful drainする。"""

    def __init__(
        self,
        service: DurableJobService,
        *,
        poll_seconds: float = 5.0,
        batch_size: int = 10,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100")
        self.service = service
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self._stop = asyncio.Event()
        self._running = False
        self._cycles = 0
        self._processed = 0
        self._last_cycle_at: datetime | None = None
        self._last_error_type: str | None = None

    def request_stop(self) -> None:
        self.service.request_drain()
        self._stop.set()

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("durable job worker is already running")
        self._running = True
        try:
            while not self._stop.is_set():
                self._last_cycle_at = datetime.now(UTC)
                self._cycles += 1
                try:
                    results = await self.service.run_once(limit=self.batch_size)
                    self._processed += len(results)
                    self._last_error_type = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error_type = type(exc).__name__
                    logger.error(
                        "durable_job_worker_cycle_failed",
                        extra={"error_type": type(exc).__name__},
                    )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    continue
        finally:
            self._running = False

    def snapshot(self) -> WorkerSnapshot:
        return WorkerSnapshot(
            running=self._running,
            draining=self._running and self._stop.is_set(),
            cycles=self._cycles,
            processed=self._processed,
            last_cycle_at=self._last_cycle_at,
            last_error_type=self._last_error_type,
        )
