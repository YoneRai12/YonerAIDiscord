from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    stage: str
    summary: str

    def __post_init__(self) -> None:
        if not self.stage.strip() or not self.summary.strip():
            raise ValueError("progress fields are required")
        if len(self.stage) > 40 or len(self.summary) > 300:
            raise ValueError("progress update is too long")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.stage}:{self.summary}".encode("utf-8")).hexdigest()


class ProgressThrottle:
    """同じDiscord messageを編集する前提で、頻度と総回数を制限する。"""

    def __init__(self, *, minimum_interval: timedelta = timedelta(milliseconds=700), max_updates: int = 8) -> None:
        if minimum_interval < timedelta(0) or max_updates <= 0:
            raise ValueError("invalid progress limits")
        self.minimum_interval = minimum_interval
        self.max_updates = max_updates
        self._last_at: datetime | None = None
        self._last_fingerprint: str | None = None
        self._count = 0

    def allow(self, update: ProgressUpdate, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if self._count >= self.max_updates or update.fingerprint == self._last_fingerprint:
            return False
        if self._last_at is not None and now - self._last_at < self.minimum_interval:
            return False
        self._last_at = now
        self._last_fingerprint = update.fingerprint
        self._count += 1
        return True
