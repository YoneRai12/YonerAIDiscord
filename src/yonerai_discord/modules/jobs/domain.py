from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Mapping


_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class JobStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    SKIPPED = "skipped"


class OutcomeKind(str, Enum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    NONRETRYABLE_FAILURE = "nonretryable_failure"
    UNCERTAIN = "uncertain"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True, order=True)
class Revision:
    value: int

    def __post_init__(self) -> None:
        if self.value < 1:
            raise ValueError("revision must be at least 1")


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    action_key: str
    revision: Revision
    kind: str
    payload: Mapping[str, Any]
    available_at: datetime
    guild_id: int | None = None
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 128 or any(ord(char) < 33 for char in self.id):
            raise ValueError("id must contain 1 to 128 non-whitespace printable characters")
        if not self.action_key or len(self.action_key) > 200 or any(ord(char) < 32 for char in self.action_key):
            raise ValueError("action_key must contain 1 to 200 printable characters")
        if not _KIND_RE.fullmatch(self.kind):
            raise ValueError("kind must match [a-z][a-z0-9_.-]{0,63}")
        # legacy rowも読める上限。新規submitはservice側で25へさらに制限する。
        if not 1 <= self.max_attempts <= 1000:
            raise ValueError("max_attempts must be between 1 and 1000")
        if self.attempts < 0:
            raise ValueError("attempts cannot be negative")
        if self.attempts > self.max_attempts:
            raise ValueError("attempts cannot exceed max_attempts")
        if self.available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        if self.guild_id is not None and (isinstance(self.guild_id, bool) or self.guild_id <= 0):
            raise ValueError("guild_id must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class Lease:
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Claim:
    job: Job
    lease: Lease


@dataclass(frozen=True, slots=True)
class Attempt:
    job_id: str
    number: int
    started_at: datetime


class ExecutionContext:
    """Executorへ停止通知と副作用境界の再認可契約を渡す。"""

    __slots__ = (
        "_cancel_event",
        "_cancelled",
        "_completed_without_side_effect",
        "_contract_observed",
        "_denied_before_side_effect",
        "_side_effect_started",
        "_still_allowed",
    )

    def __init__(self, still_allowed: Callable[[], bool]) -> None:
        self._still_allowed = still_allowed
        self._cancel_event = asyncio.Event()
        self._cancelled = False
        self._contract_observed = False
        self._side_effect_started = False
        self._completed_without_side_effect = False
        self._denied_before_side_effect = False

    @property
    def cancellation_event(self) -> asyncio.Event:
        return self._cancel_event

    @property
    def cancellation_requested(self) -> bool:
        return self._cancelled or not self._evaluate_policy()

    def cancel(self) -> None:
        self._cancelled = True
        self._cancel_event.set()

    def still_allowed(self) -> bool:
        """長いawaitの後と外部副作用の直前にexecutorが呼ぶ。"""

        self._contract_observed = True
        return not self._cancelled and self._evaluate_policy()

    def begin_side_effect(self) -> bool:
        """外部副作用へ入る直前に一度だけ許可を確定する。"""

        self._contract_observed = True
        if self._completed_without_side_effect:
            return False
        if not self.still_allowed():
            self._denied_before_side_effect = True
            return False
        self._side_effect_started = True
        return True

    def complete_without_side_effect(self) -> bool:
        """internal/no-op等が外部副作用を行わなかったことを明示する。"""

        self._contract_observed = True
        if self._side_effect_started:
            return False
        if not self.still_allowed():
            self._denied_before_side_effect = True
            return False
        self._completed_without_side_effect = True
        return True

    @property
    def contract_complete(self) -> bool:
        return self._contract_observed and (
            self._side_effect_started or self._completed_without_side_effect or self._denied_before_side_effect
        )

    @property
    def side_effect_started(self) -> bool:
        return self._side_effect_started

    @property
    def no_side_effect_proven(self) -> bool:
        return self._completed_without_side_effect or self._denied_before_side_effect

    def service_allows_execution(self) -> bool:
        """service側の事後確認。executorの契約履行とは数えない。"""

        return not self._cancelled and self._evaluate_policy()

    def _evaluate_policy(self) -> bool:
        try:
            return self._still_allowed() is True
        except Exception:
            return False


@dataclass(frozen=True, slots=True)
class Receipt:
    external_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: OutcomeKind
    receipt: Receipt | None = None
    error_type: str | None = None
    detail: str = ""

    @classmethod
    def succeeded(cls, receipt: Receipt | None = None) -> Outcome:
        return cls(OutcomeKind.SUCCEEDED, receipt=receipt or Receipt())

    @classmethod
    def retryable_failure(cls, error_type: str, detail: str = "") -> Outcome:
        return cls(OutcomeKind.RETRYABLE_FAILURE, error_type=error_type[:100], detail=detail[:500])

    @classmethod
    def nonretryable_failure(cls, error_type: str, detail: str = "") -> Outcome:
        return cls(OutcomeKind.NONRETRYABLE_FAILURE, error_type=error_type[:100], detail=detail[:500])

    @classmethod
    def uncertain(cls, error_type: str, detail: str = "") -> Outcome:
        """外部で成否が確定できない場合。自動retryはしない。"""

        return cls(OutcomeKind.UNCERTAIN, error_type=error_type[:100], detail=detail[:500])

    @classmethod
    def skipped(cls, detail: str = "") -> Outcome:
        return cls(OutcomeKind.SKIPPED, detail=detail[:500])


class RetryableJobError(RuntimeError):
    pass


class NonRetryableJobError(RuntimeError):
    pass
