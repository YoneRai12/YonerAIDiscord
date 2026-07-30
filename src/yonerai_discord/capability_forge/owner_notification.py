"""Recipe Forge Stage 2bのprovider-neutral owner通知境界。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from .lifecycle import (
    CandidateKind,
    LifecycleCandidate,
    NotificationClaim,
    SqliteForgeLifecycleRepository,
    TemplateIdentity,
)


_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_CUSTOM_ID = re.compile(r"cf1:([krp]):([a-f0-9]{64}):([1-9][0-9]{0,15})\Z")


class OwnerNotificationValidationError(ValueError): ...


class OwnerDecisionAction(StrEnum):
    KEEP = "keep"
    REJECT = "reject"
    PROMOTE_REQUESTED = "promote_requested"


_ACTION_CODE = {
    OwnerDecisionAction.KEEP: "k",
    OwnerDecisionAction.REJECT: "r",
    OwnerDecisionAction.PROMOTE_REQUESTED: "p",
}
_CODE_ACTION = {code: action for action, code in _ACTION_CODE.items()}


class NotificationDeliveryStatus(StrEnum):
    IDLE = "idle"
    SENT = "sent"
    RETRY_SCHEDULED = "retry_scheduled"
    LOST_CLAIM = "lost_claim"


class OwnerDecisionStatus(StrEnum):
    APPLIED = "applied"
    DENIED = "denied"
    OWNER_UNAVAILABLE = "owner_unavailable"
    STALE = "stale"


class OwnerNotificationFailureCode(StrEnum):
    OWNER_RESOLUTION_RETRYABLE = "owner_resolution.retryable"
    OWNER_DM_RETRYABLE = "owner_dm.retryable"
    DELIVERY_TIMEOUT_RETRYABLE = "owner_delivery.timeout_retryable"


@dataclass(frozen=True, slots=True)
class OwnerIdentity:
    user_id: int

    def __post_init__(self) -> None:
        _user_id(self.user_id)


@dataclass(frozen=True, slots=True)
class OwnerDecisionButton:
    action: OwnerDecisionAction
    custom_id: str


@dataclass(frozen=True, slots=True)
class OwnerNotificationCard:
    recipe_digest: str
    expected_revision: int
    code_owned_description: str
    templates: tuple[TemplateIdentity, ...]
    actions: tuple[OwnerDecisionButton, ...]
    candidate_kind: CandidateKind = CandidateKind.SEALED_RECIPE


@dataclass(frozen=True, slots=True)
class NotificationDeliveryReceipt:
    status: NotificationDeliveryStatus
    recipe_digest: str | None
    candidate_revision: int | None
    failure_code: OwnerNotificationFailureCode | None = None


@dataclass(frozen=True, slots=True)
class OwnerDecisionRequest:
    recipe_digest: str
    expected_revision: int
    action: OwnerDecisionAction
    actor_user_id: int

    def __post_init__(self) -> None:
        _digest(self.recipe_digest)
        _revision(self.expected_revision)
        _user_id(self.actor_user_id)
        if not isinstance(self.action, OwnerDecisionAction):
            raise OwnerNotificationValidationError("action must be an OwnerDecisionAction")

    @classmethod
    def from_custom_id(cls, *, custom_id: str, actor_user_id: int) -> OwnerDecisionRequest:
        if not isinstance(custom_id, str):
            raise OwnerNotificationValidationError("custom_id is invalid")
        matched = _CUSTOM_ID.fullmatch(custom_id)
        if matched is None:
            raise OwnerNotificationValidationError("custom_id is invalid")
        action_code, digest, revision = matched.groups()
        return cls(
            recipe_digest=digest,
            expected_revision=int(revision),
            action=_CODE_ACTION[action_code],
            actor_user_id=actor_user_id,
        )


@dataclass(frozen=True, slots=True)
class OwnerDecisionReceipt:
    status: OwnerDecisionStatus
    recipe_digest: str
    action: OwnerDecisionAction
    candidate_revision: int | None
    official: bool = field(init=False, default=False)
    runtime_ready: bool = field(init=False, default=False)


class OwnerResolver(Protocol):
    async def resolve_current_bot_owner(self) -> OwnerIdentity | None: ...


class OwnerDmPort(Protocol):
    async def send_private_owner_card(
        self,
        *,
        owner_user_id: int,
        card: OwnerNotificationCard,
        idempotency_key: str,
        allowed_mentions: tuple[()],
    ) -> None: ...


class OwnerNotificationService:
    """一件のpending候補をowner DMへ送り、typed decisionをCAS適用する。"""

    def __init__(
        self,
        *,
        repository: SqliteForgeLifecycleRepository,
        owner_resolver: OwnerResolver,
        dm_port: OwnerDmPort,
        clock: Callable[[], datetime] | None = None,
        claim_lease: timedelta = timedelta(seconds=60),
        delivery_timeout: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        if not isinstance(repository, SqliteForgeLifecycleRepository):
            raise TypeError("repository must be SqliteForgeLifecycleRepository")
        if not isinstance(claim_lease, timedelta) or claim_lease <= timedelta(0):
            raise OwnerNotificationValidationError("claim_lease must be positive")
        if (
            not isinstance(delivery_timeout, timedelta)
            or delivery_timeout <= timedelta(0)
            or delivery_timeout >= claim_lease
        ):
            raise OwnerNotificationValidationError("delivery_timeout must be positive and shorter than claim_lease")
        if not isinstance(retry_delay, timedelta) or retry_delay <= timedelta(0):
            raise OwnerNotificationValidationError("retry_delay must be positive")
        self._repository = repository
        self._owner_resolver = owner_resolver
        self._dm_port = dm_port
        self._clock = clock or (lambda: datetime.now(UTC))
        self._claim_lease = claim_lease
        self._delivery_timeout = delivery_timeout
        self._retry_delay = retry_delay

    async def deliver_next(self) -> NotificationDeliveryReceipt:
        claim = self._repository.claim_pending_notification(
            now=self._now(),
            lease=self._claim_lease,
        )
        if claim is None:
            return NotificationDeliveryReceipt(
                status=NotificationDeliveryStatus.IDLE,
                recipe_digest=None,
                candidate_revision=None,
            )
        try:
            async with asyncio.timeout(self._delivery_timeout.total_seconds()):
                try:
                    owner = await self._owner_resolver.resolve_current_bot_owner()
                except Exception:
                    return self._retry(claim, OwnerNotificationFailureCode.OWNER_RESOLUTION_RETRYABLE)
                if not isinstance(owner, OwnerIdentity):
                    return self._retry(claim, OwnerNotificationFailureCode.OWNER_RESOLUTION_RETRYABLE)
                try:
                    await self._dm_port.send_private_owner_card(
                        owner_user_id=owner.user_id,
                        card=_card(claim.candidate),
                        idempotency_key=(f"forge-owner-notification:v1:{claim.candidate.recipe_digest}"),
                        allowed_mentions=(),
                    )
                except Exception:
                    return self._retry(claim, OwnerNotificationFailureCode.OWNER_DM_RETRYABLE)
        except TimeoutError:
            return self._retry(claim, OwnerNotificationFailureCode.DELIVERY_TIMEOUT_RETRYABLE)

        sent = self._repository.mark_notification_sent(
            recipe_digest=claim.candidate.recipe_digest,
            claim_token=claim.claim_token,
            expected_revision=claim.candidate.revision,
            sent_at=self._now(),
        )
        if sent is None:
            return _delivery_receipt(NotificationDeliveryStatus.LOST_CLAIM, claim)
        return NotificationDeliveryReceipt(
            status=NotificationDeliveryStatus.SENT,
            recipe_digest=sent.recipe_digest,
            candidate_revision=sent.revision,
        )

    async def apply_owner_decision(self, request: OwnerDecisionRequest) -> OwnerDecisionReceipt:
        if not isinstance(request, OwnerDecisionRequest):
            raise TypeError("request must be OwnerDecisionRequest")
        try:
            owner = await asyncio.wait_for(
                self._owner_resolver.resolve_current_bot_owner(),
                timeout=self._delivery_timeout.total_seconds(),
            )
        except Exception:
            return _decision_receipt(OwnerDecisionStatus.OWNER_UNAVAILABLE, request, None)
        if not isinstance(owner, OwnerIdentity):
            return _decision_receipt(OwnerDecisionStatus.OWNER_UNAVAILABLE, request, None)
        if owner.user_id != request.actor_user_id:
            return _decision_receipt(OwnerDecisionStatus.DENIED, request, None)

        methods = {
            OwnerDecisionAction.KEEP: self._repository.keep,
            OwnerDecisionAction.REJECT: self._repository.reject,
            OwnerDecisionAction.PROMOTE_REQUESTED: self._repository.request_promotion,
        }
        changed = methods[request.action](
            request.recipe_digest,
            expected_revision=request.expected_revision,
            changed_at=self._now(),
        )
        if changed is None:
            return _decision_receipt(OwnerDecisionStatus.STALE, request, None)
        return _decision_receipt(OwnerDecisionStatus.APPLIED, request, changed.revision)

    def _retry(
        self,
        claim: NotificationClaim,
        failure_code: OwnerNotificationFailureCode,
    ) -> NotificationDeliveryReceipt:
        failed_at = self._now()
        pending = self._repository.mark_notification_retryable_failure(
            recipe_digest=claim.candidate.recipe_digest,
            claim_token=claim.claim_token,
            expected_revision=claim.candidate.revision,
            failed_at=failed_at,
            retry_not_before=failed_at + self._retry_delay,
            failure_code=failure_code.value,
        )
        if pending is None:
            return _delivery_receipt(NotificationDeliveryStatus.LOST_CLAIM, claim)
        return NotificationDeliveryReceipt(
            status=NotificationDeliveryStatus.RETRY_SCHEDULED,
            recipe_digest=pending.recipe_digest,
            candidate_revision=pending.revision,
            failure_code=failure_code,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise OwnerNotificationValidationError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _card(candidate: LifecycleCandidate) -> OwnerNotificationCard:
    expected_revision = candidate.revision + 1
    return OwnerNotificationCard(
        recipe_digest=candidate.recipe_digest,
        expected_revision=expected_revision,
        code_owned_description=candidate.code_owned_description,
        templates=candidate.templates,
        actions=tuple(
            OwnerDecisionButton(
                action=action,
                custom_id=_custom_id(action, candidate.recipe_digest, expected_revision),
            )
            for action in OwnerDecisionAction
        ),
        candidate_kind=candidate.candidate_kind,
    )


def _custom_id(action: OwnerDecisionAction, recipe_digest: str, expected_revision: int) -> str:
    digest = _digest(recipe_digest)
    revision = _revision(expected_revision)
    if not isinstance(action, OwnerDecisionAction):
        raise OwnerNotificationValidationError("action must be an OwnerDecisionAction")
    return f"cf1:{_ACTION_CODE[action]}:{digest}:{revision}"


def _delivery_receipt(
    status: NotificationDeliveryStatus,
    claim: NotificationClaim,
) -> NotificationDeliveryReceipt:
    return NotificationDeliveryReceipt(
        status=status,
        recipe_digest=claim.candidate.recipe_digest,
        candidate_revision=claim.candidate.revision,
    )


def _decision_receipt(
    status: OwnerDecisionStatus,
    request: OwnerDecisionRequest,
    revision: int | None,
) -> OwnerDecisionReceipt:
    return OwnerDecisionReceipt(
        status=status,
        recipe_digest=request.recipe_digest,
        action=request.action,
        candidate_revision=revision,
    )


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise OwnerNotificationValidationError("recipe_digest must be a lowercase SHA-256 digest")
    return value


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OwnerNotificationValidationError("revision must be a positive integer")
    return value


def _user_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OwnerNotificationValidationError("user_id must be a positive integer")
    return value
