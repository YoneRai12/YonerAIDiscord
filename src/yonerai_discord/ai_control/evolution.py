from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Callable


class EvolutionDisabledError(PermissionError):
    pass


class OwnerAuthorizationError(PermissionError):
    pass


class ForbiddenEvolutionOperationError(PermissionError):
    pass


class ProposalNotFoundError(KeyError):
    pass


class InvalidProposalTransitionError(ValueError):
    pass


class DuplicateProposalError(ValueError):
    pass


class EvolutionOperation(StrEnum):
    CODE_PATCH = "code_patch"
    PROMPT_PATCH = "prompt_patch"
    TEST_PATCH = "test_patch"
    DOCUMENTATION_PATCH = "documentation_patch"

    DIRECT_MAIN_EDIT = "direct_main_edit"
    MERGE = "merge"
    PUSH = "push"
    RESTART = "restart"
    SECRET_CHANGE = "secret_change"
    RBAC_CHANGE = "rbac_change"


FORBIDDEN_OPERATIONS: frozenset[EvolutionOperation] = frozenset(
    {
        EvolutionOperation.DIRECT_MAIN_EDIT,
        EvolutionOperation.MERGE,
        EvolutionOperation.PUSH,
        EvolutionOperation.RESTART,
        EvolutionOperation.SECRET_CHANGE,
        EvolutionOperation.RBAC_CHANGE,
    }
)


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class AuditEventType(StrEnum):
    PROPOSAL_CREATED = "proposal_created"
    REVIEW_STARTED = "review_started"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"


@dataclass(frozen=True, slots=True)
class EvolutionProposalSpec:
    title: str
    rationale: str
    patch: str
    target_paths: tuple[str, ...]
    operations: tuple[EvolutionOperation, ...] = (EvolutionOperation.CODE_PATCH,)
    target_branch: str = "proposal"

    def __post_init__(self) -> None:
        if not self.title.strip() or len(self.title) > 200:
            raise ValueError("title must contain 1 to 200 characters")
        if not self.rationale.strip() or len(self.rationale) > 4_000:
            raise ValueError("rationale must contain 1 to 4000 characters")
        if not self.patch.strip() or len(self.patch) > 200_000:
            raise ValueError("patch must contain 1 to 200000 characters")
        if not self.target_paths or any(not path.strip() for path in self.target_paths):
            raise ValueError("at least one non-empty target path is required")
        if not self.operations or any(not isinstance(item, EvolutionOperation) for item in self.operations):
            raise ValueError("operations must contain EvolutionOperation values")
        if not self.target_branch.strip():
            raise ValueError("target_branch must not be blank")


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    proposal_id: str
    proposal_hash: str
    spec: EvolutionProposalSpec
    proposer_id: int
    status: ProposalStatus
    created_at: datetime
    updated_at: datetime
    approver_id: int | None = None
    review_note: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    sequence: int
    event_type: AuditEventType
    proposal_id: str
    proposal_hash: str
    actor_id: int
    from_status: ProposalStatus | None
    to_status: ProposalStatus
    occurred_at: datetime
    note: str | None
    previous_hash: str | None
    event_hash: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_spec(spec: EvolutionProposalSpec) -> dict[str, object]:
    return {
        "operations": [operation.value for operation in spec.operations],
        "patch": spec.patch,
        "rationale": spec.rationale,
        "target_branch": spec.target_branch,
        "target_paths": list(spec.target_paths),
        "title": spec.title,
    }


def proposal_hash(spec: EvolutionProposalSpec) -> str:
    encoded = json.dumps(_canonical_spec(spec), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _event_hash(
    *,
    sequence: int,
    event_type: AuditEventType,
    proposal_id: str,
    proposal_hash_value: str,
    actor_id: int,
    from_status: ProposalStatus | None,
    to_status: ProposalStatus,
    occurred_at: datetime,
    note: str | None,
    previous_hash: str | None,
) -> str:
    data = {
        "actor_id": actor_id,
        "event_type": event_type.value,
        "from_status": from_status.value if from_status is not None else None,
        "note": note,
        "occurred_at": occurred_at.isoformat(),
        "previous_hash": previous_hash,
        "proposal_hash": proposal_hash_value,
        "proposal_id": proposal_id,
        "sequence": sequence,
        "to_status": to_status.value,
    }
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_protected_branch(branch: str) -> bool:
    return branch.strip().lower() in {"main", "master", "refs/heads/main", "refs/heads/master"}


def _is_protected_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/").lower()
    segments = [segment for segment in normalized.split("/") if segment]
    protected_stems = {"secret", "secrets", "credential", "credentials", "rbac", "roles", "permissions"}
    return any(
        segment == ".env" or segment.startswith(".env.") or segment.split(".", maxsplit=1)[0] in protected_stems
        for segment in segments
    )


class EvolutionReviewMachine:
    """Owner-only, in-memory proposal review; it cannot apply or publish changes."""

    def __init__(
        self,
        *,
        bot_owner_ids: frozenset[int],
        enabled: bool = False,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not bot_owner_ids or any(owner_id <= 0 for owner_id in bot_owner_ids):
            raise ValueError("bot_owner_ids must contain positive IDs")
        self._bot_owner_ids = bot_owner_ids
        self._enabled = enabled
        self._clock = clock
        self._proposals: dict[str, EvolutionProposal] = {}
        self._audit: list[AuditEvent] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def propose(self, *, actor_id: int, spec: EvolutionProposalSpec) -> EvolutionProposal:
        self._authorize(actor_id)
        self._validate_scope(spec)
        digest = proposal_hash(spec)
        proposal_id = f"evo-{digest[:20]}"
        if proposal_id in self._proposals:
            raise DuplicateProposalError(f"proposal already exists: {proposal_id}")
        now = self._now()
        proposal = EvolutionProposal(
            proposal_id=proposal_id,
            proposal_hash=digest,
            spec=spec,
            proposer_id=actor_id,
            status=ProposalStatus.PROPOSED,
            created_at=now,
            updated_at=now,
        )
        self._proposals[proposal_id] = proposal
        self._append_audit(
            event_type=AuditEventType.PROPOSAL_CREATED,
            proposal=proposal,
            actor_id=actor_id,
            from_status=None,
            note=None,
        )
        return proposal

    def begin_review(self, proposal_id: str, *, actor_id: int, note: str | None = None) -> EvolutionProposal:
        return self._transition(
            proposal_id,
            actor_id=actor_id,
            expected=ProposalStatus.PROPOSED,
            target=ProposalStatus.IN_REVIEW,
            event_type=AuditEventType.REVIEW_STARTED,
            note=note,
        )

    def approve(self, proposal_id: str, *, actor_id: int, note: str) -> EvolutionProposal:
        if not note.strip():
            raise ValueError("approval note must not be blank")
        return self._transition(
            proposal_id,
            actor_id=actor_id,
            expected=ProposalStatus.IN_REVIEW,
            target=ProposalStatus.APPROVED,
            event_type=AuditEventType.PROPOSAL_APPROVED,
            note=note,
            approver_id=actor_id,
        )

    def reject(self, proposal_id: str, *, actor_id: int, note: str) -> EvolutionProposal:
        if not note.strip():
            raise ValueError("rejection note must not be blank")
        return self._transition(
            proposal_id,
            actor_id=actor_id,
            expected=ProposalStatus.IN_REVIEW,
            target=ProposalStatus.REJECTED,
            event_type=AuditEventType.PROPOSAL_REJECTED,
            note=note,
        )

    def get(self, proposal_id: str, *, actor_id: int) -> EvolutionProposal:
        self._authorize(actor_id)
        return self._get(proposal_id)

    def _get(self, proposal_id: str) -> EvolutionProposal:
        try:
            return self._proposals[proposal_id]
        except KeyError as exc:
            raise ProposalNotFoundError(proposal_id) from exc

    def audit_events(self, *, actor_id: int, proposal_id: str | None = None) -> tuple[AuditEvent, ...]:
        self._authorize(actor_id)
        if proposal_id is None:
            return tuple(self._audit)
        return tuple(event for event in self._audit if event.proposal_id == proposal_id)

    def verify_integrity(self, proposal_id: str, *, actor_id: int) -> bool:
        self._authorize(actor_id)
        proposal = self._get(proposal_id)
        if proposal_hash(proposal.spec) != proposal.proposal_hash:
            return False
        previous_hash: str | None = None
        for event in self._audit:
            expected = _event_hash(
                sequence=event.sequence,
                event_type=event.event_type,
                proposal_id=event.proposal_id,
                proposal_hash_value=event.proposal_hash,
                actor_id=event.actor_id,
                from_status=event.from_status,
                to_status=event.to_status,
                occurred_at=event.occurred_at,
                note=event.note,
                previous_hash=event.previous_hash,
            )
            if event.previous_hash != previous_hash or event.event_hash != expected:
                return False
            previous_hash = event.event_hash
        return True

    def _authorize(self, actor_id: int) -> None:
        if not self._enabled:
            raise EvolutionDisabledError("self-evolution proposals are disabled")
        if actor_id not in self._bot_owner_ids:
            raise OwnerAuthorizationError("only a configured bot owner may use self-evolution review")

    def _validate_scope(self, spec: EvolutionProposalSpec) -> None:
        forbidden = [operation.value for operation in spec.operations if operation in FORBIDDEN_OPERATIONS]
        if forbidden:
            raise ForbiddenEvolutionOperationError("forbidden self-evolution operations: " + ", ".join(forbidden))
        if _is_protected_branch(spec.target_branch):
            raise ForbiddenEvolutionOperationError("self-evolution cannot target main or master directly")
        protected_paths = [path for path in spec.target_paths if _is_protected_path(path)]
        if protected_paths:
            raise ForbiddenEvolutionOperationError(
                "self-evolution cannot change secrets or RBAC paths: " + ", ".join(protected_paths)
            )

    def _transition(
        self,
        proposal_id: str,
        *,
        actor_id: int,
        expected: ProposalStatus,
        target: ProposalStatus,
        event_type: AuditEventType,
        note: str | None,
        approver_id: int | None = None,
    ) -> EvolutionProposal:
        self._authorize(actor_id)
        current = self._get(proposal_id)
        if current.status is not expected:
            raise InvalidProposalTransitionError(
                f"cannot transition {proposal_id} from {current.status.value} to {target.value}"
            )
        updated = replace(
            current,
            status=target,
            updated_at=self._now(),
            approver_id=approver_id,
            review_note=note,
        )
        self._proposals[proposal_id] = updated
        self._append_audit(
            event_type=event_type,
            proposal=updated,
            actor_id=actor_id,
            from_status=current.status,
            note=note,
        )
        return updated

    def _append_audit(
        self,
        *,
        event_type: AuditEventType,
        proposal: EvolutionProposal,
        actor_id: int,
        from_status: ProposalStatus | None,
        note: str | None,
    ) -> None:
        previous_hash = self._audit[-1].event_hash if self._audit else None
        sequence = len(self._audit) + 1
        occurred_at = self._now()
        digest = _event_hash(
            sequence=sequence,
            event_type=event_type,
            proposal_id=proposal.proposal_id,
            proposal_hash_value=proposal.proposal_hash,
            actor_id=actor_id,
            from_status=from_status,
            to_status=proposal.status,
            occurred_at=occurred_at,
            note=note,
            previous_hash=previous_hash,
        )
        self._audit.append(
            AuditEvent(
                sequence=sequence,
                event_type=event_type,
                proposal_id=proposal.proposal_id,
                proposal_hash=proposal.proposal_hash,
                actor_id=actor_id,
                from_status=from_status,
                to_status=proposal.status,
                occurred_at=occurred_at,
                note=note,
                previous_hash=previous_hash,
                event_hash=digest,
            )
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)
