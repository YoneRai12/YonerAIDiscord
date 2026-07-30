from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .domain import AllowedMentions, ReminderDelivery
from .ports import ReminderRepository, ReminderSender


logger = logging.getLogger(__name__)


class RetryableDeliveryError(RuntimeError):
    """外部送信が始まっていないとadapterが保証できる失敗。"""


DeliveryPolicy = Callable[[ReminderDelivery], bool]


class DeliveryAbortedError(RetryableDeliveryError):
    """Discord送信開始前に停止またはpolicy変更を確認できた。"""


class ReminderDispatcher:
    def __init__(
        self,
        repository: ReminderRepository,
        sender: ReminderSender,
        *,
        lease: timedelta = timedelta(minutes=2),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        delivery_policy: DeliveryPolicy | None = None,
        policy_defer: timedelta = timedelta(minutes=5),
        retry_delay: timedelta = timedelta(seconds=30),
        max_attempts: int = 5,
    ) -> None:
        if lease <= timedelta(0):
            raise ValueError("lease must be positive")
        if policy_defer <= timedelta(0) or retry_delay <= timedelta(0):
            raise ValueError("defer delays must be positive")
        if not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        self.repository = repository
        self.sender = sender
        self.lease = lease
        self.clock = clock
        # policy未配線はfail-closed。pluginが必ずRegistry連携を渡す。
        self.delivery_policy = delivery_policy or (lambda _delivery: False)
        self.policy_defer = policy_defer
        self.retry_delay = retry_delay
        self.max_attempts = max_attempts

    async def dispatch_due(
        self,
        limit: int = 50,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> int:
        now = self.clock()
        claims = self.repository.claim_due(now, self.lease, limit)
        sent = 0
        for claim in claims:
            reminder = claim.reminder
            if should_stop is not None and should_stop():
                self.repository.defer(
                    reminder.id,
                    claim.claim_token,
                    self.clock() + self.retry_delay,
                    "WorkerStopping",
                )
                continue
            meeting = self.repository.get_meeting(reminder.meeting_id)
            if meeting is None:
                self.repository.mark_failed(reminder.id, claim.claim_token, "MeetingNotFound")
                continue
            mentions = AllowedMentions(
                user_ids=(reminder.target_user_id,) if reminder.target_user_id is not None else (),
                role_ids=(),
                everyone=False,
                replied_user=False,
            )
            delivery = ReminderDelivery(reminder, meeting, mentions)
            allowed = self._delivery_allowed(delivery, should_stop)
            if allowed is not True:
                self.repository.defer(
                    reminder.id,
                    claim.claim_token,
                    self.clock() + self.policy_defer,
                    "CapabilityUnavailable",
                )
                continue
            if reminder.attempts > self.max_attempts:
                self.repository.mark_failed(reminder.id, claim.claim_token, "AttemptLimitExceeded")
                continue
            if not self.repository.prepare_delivery(reminder.id, claim.claim_token, self.clock()):
                logger.error("scheduling_delivery_prepare_rejected")
                continue

            def still_allowed() -> bool:
                return self._delivery_allowed(delivery, should_stop)

            if not still_allowed():
                self._release_prepared(reminder.id, claim.claim_token, "DeliveryAbortedError")
                continue
            try:
                await self.sender.send(delivery, still_allowed=still_allowed)
            except RetryableDeliveryError as exc:
                self._release_prepared(reminder.id, claim.claim_token, type(exc).__name__)
                logger.warning(
                    "scheduling_delivery_deferred",
                    extra={"reminder_id": reminder.id, "error_type": type(exc).__name__},
                )
                continue
            except Exception as exc:
                self._mark_uncertain(reminder.id, claim.claim_token, type(exc).__name__)
                logger.warning(
                    "scheduling_delivery_uncertain",
                    extra={"reminder_id": reminder.id, "error_type": type(exc).__name__},
                )
                continue
            if not still_allowed():
                # 任意senderが停止契約を無視した可能性があるため、成功とは確定しない。
                self._mark_uncertain(reminder.id, claim.claim_token, "PolicyChangedAfterSend")
                continue
            try:
                completed = self.repository.complete_delivery(
                    reminder.id,
                    claim.claim_token,
                    self.clock(),
                )
            except Exception as exc:
                self._mark_uncertain(reminder.id, claim.claim_token, type(exc).__name__)
                logger.error(
                    "scheduling_delivery_finalize_failed",
                    extra={"reminder_id": reminder.id, "error_type": type(exc).__name__},
                )
                continue
            if not completed:
                self._mark_uncertain(reminder.id, claim.claim_token, "FinalizeRejected")
                logger.error("scheduling_delivery_finalize_rejected")
                continue
            sent += 1
        return sent

    def _delivery_allowed(
        self,
        delivery: ReminderDelivery,
        should_stop: Callable[[], bool] | None,
    ) -> bool:
        if should_stop is not None and should_stop():
            return False
        try:
            return self.delivery_policy(delivery) is True
        except Exception as exc:
            logger.error(
                "scheduling_policy_failed_closed",
                extra={"error_type": type(exc).__name__},
            )
            return False

    def _release_prepared(self, reminder_id: str, claim_token: str, error_type: str) -> None:
        released = self.repository.release_prepared(
            reminder_id,
            claim_token,
            self.clock() + self.retry_delay,
            error_type,
        )
        if not released:
            self._mark_uncertain(reminder_id, claim_token, "RetryReleaseRejected")

    def _mark_uncertain(self, reminder_id: str, claim_token: str, error_type: str) -> None:
        try:
            self.repository.mark_delivery_uncertain(
                reminder_id,
                claim_token,
                self.clock(),
                error_type,
            )
        except Exception as exc:
            # PREPARED intentはそれ自体が再送を防ぐ。例外本文は保存しない。
            logger.critical(
                "scheduling_uncertain_persist_failed",
                extra={"error_type": type(exc).__name__},
            )


class ReminderWorker:
    def __init__(self, dispatcher: ReminderDispatcher, poll_seconds: float = 30.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.dispatcher = dispatcher
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.dispatcher.dispatch_due(should_stop=self._stop.is_set)
            except Exception as exc:
                logger.error("scheduling_tick_failed", extra={"error_type": type(exc).__name__})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    def request_stop(self) -> None:
        self._stop.set()
