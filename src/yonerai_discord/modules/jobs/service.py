from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from .domain import (
    Attempt,
    Claim,
    ExecutionContext,
    Job,
    JobStatus,
    NonRetryableJobError,
    Outcome,
    OutcomeKind,
    Receipt,
    RetryableJobError,
    Revision,
)
from .ports import Executor, ExecutorRegistry
from .repository import SqliteJobRepository


def classify_exception(exc: Exception) -> Outcome:
    if isinstance(exc, RetryableJobError):
        return Outcome.retryable_failure(type(exc).__name__)
    if isinstance(exc, NonRetryableJobError):
        return Outcome.nonretryable_failure(type(exc).__name__)
    if isinstance(exc, TimeoutError | ConnectionError):
        # transport timeout/resetは相手が副作用を受理済みか判定できない。
        return Outcome.uncertain(type(exc).__name__)
    return Outcome.nonretryable_failure(type(exc).__name__)


_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class DurableJobService:
    def __init__(
        self,
        repository: SqliteJobRepository,
        executors: ExecutorRegistry | Mapping[str, Executor],
        *,
        lease_seconds: int = 60,
        backoff_base_seconds: int = 5,
        backoff_max_seconds: int = 3600,
        execution_timeout_seconds: float = 45.0,
        disabled_defer_seconds: int = 300,
        execution_policy: Callable[[Job], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            lease_seconds <= 0
            or backoff_base_seconds <= 0
            or backoff_max_seconds <= 0
            or execution_timeout_seconds <= 0
            or disabled_defer_seconds <= 0
        ):
            raise ValueError("lease, timeout, defer and backoff values must be positive")
        if lease_seconds <= execution_timeout_seconds:
            raise ValueError("lease_seconds must exceed execution_timeout_seconds")
        self.repository = repository
        self.executors = executors
        self.lease = timedelta(seconds=lease_seconds)
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.execution_timeout_seconds = execution_timeout_seconds
        self.disabled_defer_seconds = disabled_defer_seconds
        self.execution_policy = execution_policy or (lambda _job: True)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._draining = False
        self._active_contexts: set[ExecutionContext] = set()

    def request_drain(self) -> None:
        """新しい副作用を止め、実行中executorへcancelを通知する。"""

        self._draining = True
        for context in tuple(self._active_contexts):
            context.cancel()

    def submit(
        self,
        *,
        action_key: str,
        revision: int,
        kind: str,
        payload: Mapping[str, Any],
        guild_id: int | None = None,
        available_at: datetime | None = None,
        max_attempts: int = 5,
        job_id: str | None = None,
    ) -> Job:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 25:
            raise ValueError("max_attempts must be between 1 and 25")
        normalized_action = self._validate_action_key(action_key)
        normalized_kind = self._validate_kind(kind)
        safe_payload = self._validate_payload(payload)
        job = Job(
            id=job_id or uuid.uuid4().hex,
            action_key=normalized_action,
            revision=Revision(revision),
            kind=normalized_kind,
            payload=safe_payload,
            available_at=available_at or self.clock(),
            guild_id=guild_id,
            max_attempts=max_attempts,
        )
        if self.repository.enqueue(job, self.clock()):
            return job
        existing = self.repository.get_by_action(normalized_action, job.revision)
        if existing is None:
            raise ValueError(f"job id already exists with another action: {job.id}")
        return existing

    async def run_once(self, limit: int = 50) -> tuple[tuple[str, JobStatus], ...]:
        now = self.clock()
        claims = await asyncio.to_thread(self.repository.claim_due, now, self.lease, limit)
        results: list[tuple[str, JobStatus]] = []
        current_index = -1
        try:
            for current_index, claim in enumerate(claims):
                execution_allowed = self._execution_allowed(claim.job)
                if not execution_allowed:
                    deferred = await asyncio.to_thread(
                        self.repository.defer_claim,
                        claim,
                        self.clock() + timedelta(seconds=self.disabled_defer_seconds),
                        "execution_policy_disabled",
                        self.clock(),
                    )
                    if deferred:
                        results.append((claim.job.id, JobStatus.PENDING))
                    continue

                executor = self.executors.get(claim.job.kind)
                if executor is None:
                    status = await asyncio.to_thread(
                        self._record_outcome,
                        claim,
                        Outcome.nonretryable_failure(
                            "UnknownExecutor",
                            f"executor is not explicitly registered: {claim.job.kind}",
                        ),
                    )
                    results.append((claim.job.id, status))
                    continue

                started = self.clock()
                if not await asyncio.to_thread(self.repository.mark_execution_started, claim, started):
                    continue
                still_allowed = self._execution_allowed(claim.job)
                if not still_allowed:
                    deferred = await asyncio.to_thread(
                        self.repository.defer_before_executor,
                        claim,
                        self.clock() + timedelta(seconds=self.disabled_defer_seconds),
                        "execution_policy_changed_before_executor",
                        self.clock(),
                    )
                    if deferred:
                        results.append((claim.job.id, JobStatus.PENDING))
                    continue
                attempt = Attempt(claim.job.id, claim.job.attempts, started)
                context = ExecutionContext(lambda: self._execution_allowed(claim.job))
                self._active_contexts.add(context)
                try:
                    try:
                        async with asyncio.timeout(self.execution_timeout_seconds):
                            outcome = await executor.execute(claim.job, attempt, context)
                    except asyncio.CancelledError:
                        await asyncio.shield(
                            asyncio.to_thread(
                                self._best_effort_uncertain,
                                claim,
                                "worker_cancelled_during_execution",
                                "executor completion is unknown",
                                self.clock(),
                            )
                        )
                        raise
                    except Exception as exc:
                        outcome = classify_exception(exc)
                    if not isinstance(outcome, Outcome):
                        outcome = Outcome.nonretryable_failure(
                            "InvalidExecutorOutcome",
                            f"executor returned {type(outcome).__name__}",
                        )
                finally:
                    self._active_contexts.discard(context)

                if not context.contract_complete:
                    outcome = Outcome.uncertain(
                        "ExecutorContractViolation",
                        "executor did not acknowledge the side-effect contract",
                    )
                elif not context.service_allows_execution():
                    if context.side_effect_started:
                        outcome = Outcome.uncertain(
                            "ExecutionCancelledAfterSideEffect",
                            "execution was disabled after the side-effect boundary",
                        )
                    elif context.no_side_effect_proven:
                        deferred = await asyncio.to_thread(
                            self.repository.defer_before_executor,
                            claim,
                            self.clock() + timedelta(seconds=self.disabled_defer_seconds),
                            "execution_disabled_before_side_effect",
                            self.clock(),
                        )
                        if deferred:
                            results.append((claim.job.id, JobStatus.PENDING))
                        else:
                            await asyncio.to_thread(
                                self._best_effort_uncertain,
                                claim,
                                "defer_conflict",
                                "execution disable could not be finalized",
                                self.clock(),
                            )
                            results.append((claim.job.id, JobStatus.UNCERTAIN))
                        continue
                elif context.side_effect_started and outcome.kind in {
                    OutcomeKind.RETRYABLE_FAILURE,
                    OutcomeKind.NONRETRYABLE_FAILURE,
                    OutcomeKind.SKIPPED,
                }:
                    outcome = Outcome.uncertain(
                        "SideEffectOutcomeUncertain",
                        "executor crossed the side-effect boundary without a definitive success",
                    )
                status = await asyncio.to_thread(self._record_outcome, claim, outcome)
                results.append((claim.job.id, status))
        except asyncio.CancelledError:
            # batch claim済みでも未着手の後続jobは副作用0件と証明できるため、
            # shutdown cancellationでattemptを消費させず即時deferする。
            remaining = claims[current_index + 1 :]
            await asyncio.shield(self._defer_unstarted_claims(remaining))
            raise
        return tuple(results)

    async def _defer_unstarted_claims(self, claims: tuple[Claim, ...]) -> None:
        for claim in claims:
            try:
                await asyncio.to_thread(
                    self.repository.defer_claim,
                    claim,
                    self.clock() + timedelta(seconds=self.disabled_defer_seconds),
                    "worker_cancelled_before_execution",
                    self.clock(),
                )
            except Exception:
                # lease回復が残るため、1件のDB競合で他の未着手claimの解放を止めない。
                continue

    def _record_outcome(self, claim, outcome: Outcome) -> JobStatus:
        finished = self.clock()
        if outcome.kind is OutcomeKind.SUCCEEDED:
            try:
                finalized = self.repository.finalize_success(claim, outcome.receipt or Receipt(), finished)
            except Exception as exc:
                self._best_effort_uncertain(
                    claim,
                    f"finalize_{type(exc).__name__}",
                    "success finalization failed",
                    finished,
                )
                return JobStatus.UNCERTAIN
            if not finalized:
                self._best_effort_uncertain(claim, "finalize_conflict", "success could not be finalized", finished)
                return JobStatus.UNCERTAIN
            return JobStatus.SUCCEEDED
        if outcome.kind is OutcomeKind.SKIPPED:
            if not self.repository.finalize_skipped(claim, outcome.detail, finished):
                self._best_effort_uncertain(claim, "finalize_conflict", "skip could not be finalized", finished)
                return JobStatus.UNCERTAIN
            return JobStatus.SKIPPED
        if outcome.kind is OutcomeKind.NONRETRYABLE_FAILURE:
            if not self.repository.finalize_failed(
                claim,
                outcome.error_type or "nonretryable_failure",
                outcome.detail,
                finished,
            ):
                self._best_effort_uncertain(claim, "finalize_conflict", "failure could not be finalized", finished)
                return JobStatus.UNCERTAIN
            return JobStatus.FAILED

        if outcome.kind is OutcomeKind.UNCERTAIN:
            if not self.repository.mark_uncertain(
                claim,
                outcome.error_type or "uncertain_execution",
                outcome.detail,
                finished,
            ):
                self._best_effort_uncertain(claim, "finalize_conflict", "uncertain state not finalized", finished)
            return JobStatus.UNCERTAIN

        if claim.job.attempts >= claim.job.max_attempts:
            if not self.repository.finalize_failed(
                claim,
                outcome.error_type or "max_attempts_exhausted",
                outcome.detail,
                finished,
            ):
                self._best_effort_uncertain(claim, "finalize_conflict", "max-attempt failure not finalized", finished)
                return JobStatus.UNCERTAIN
            return JobStatus.FAILED
        available_at = finished + timedelta(seconds=self.backoff_seconds(claim.job.attempts))
        if not self.repository.schedule_retry(
            claim,
            available_at,
            outcome.error_type or "retryable_failure",
            outcome.detail,
            finished,
        ):
            self._best_effort_uncertain(claim, "finalize_conflict", "retry could not be scheduled", finished)
            return JobStatus.UNCERTAIN
        return JobStatus.PENDING

    def backoff_seconds(self, attempt_number: int) -> int:
        exponent = min(30, max(0, attempt_number - 1))
        return min(self.backoff_max_seconds, self.backoff_base_seconds * (2**exponent))

    def _execution_allowed(self, job: Job) -> bool:
        if self._draining:
            return False
        try:
            return self.execution_policy(job) is True
        except Exception:
            return False

    def _best_effort_uncertain(
        self,
        claim,
        error_type: str,
        detail: str,
        finished_at: datetime,
    ) -> None:
        try:
            self.repository.mark_uncertain(claim, error_type, detail, finished_at)
        except Exception:
            # execution_started_atは永続化済み。lease recoveryがuncertainへ収束させる。
            return

    @staticmethod
    def _validate_action_key(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("action_key must be a string")
        normalized = value.strip()
        if not normalized or len(normalized) > 200 or any(ord(char) < 32 for char in normalized):
            raise ValueError("action_key must contain 1 to 200 printable characters")
        return normalized

    @staticmethod
    def _validate_kind(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("kind must be a string")
        normalized = value.strip().lower()
        if not _KIND_RE.fullmatch(normalized):
            raise ValueError("kind must match [a-z][a-z0-9_.-]{0,63}")
        return normalized

    @staticmethod
    def _validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        value = dict(payload)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be finite JSON data") from exc
        if len(encoded.encode("utf-8")) > 16_384:
            raise ValueError("payload must encode to at most 16384 bytes")
        return value
