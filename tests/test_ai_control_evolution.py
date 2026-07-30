from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yonerai_discord.ai_control import (
    EvolutionDisabledError,
    EvolutionOperation,
    EvolutionProposalSpec,
    EvolutionReviewMachine,
    ForbiddenEvolutionOperationError,
    InvalidProposalTransitionError,
    OwnerAuthorizationError,
    ProposalStatus,
)


class AdvancingClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 21, tzinfo=UTC)

    def __call__(self) -> datetime:
        result = self.current
        self.current += timedelta(seconds=1)
        return result


def proposal_spec(**overrides: object) -> EvolutionProposalSpec:
    values: dict[str, object] = {
        "title": "Improve deterministic routing",
        "rationale": "The proposal makes model selection easier to audit.",
        "patch": "--- a/router.py\n+++ b/router.py\n@@\n-old\n+new",
        "target_paths": ("src/router.py", "tests/test_router.py"),
        "operations": (EvolutionOperation.CODE_PATCH, EvolutionOperation.TEST_PATCH),
        "target_branch": "proposal/router-improvement",
    }
    values.update(overrides)
    return EvolutionProposalSpec(**values)  # type: ignore[arg-type]


def test_self_evolution_is_disabled_by_default() -> None:
    machine = EvolutionReviewMachine(bot_owner_ids=frozenset({10}))
    with pytest.raises(EvolutionDisabledError):
        machine.propose(actor_id=10, spec=proposal_spec())


def test_only_bot_owners_can_propose_or_review() -> None:
    machine = EvolutionReviewMachine(bot_owner_ids=frozenset({10}), enabled=True)
    with pytest.raises(OwnerAuthorizationError):
        machine.propose(actor_id=11, spec=proposal_spec())
    proposal = machine.propose(actor_id=10, spec=proposal_spec())
    with pytest.raises(OwnerAuthorizationError):
        machine.begin_review(proposal.proposal_id, actor_id=11)
    with pytest.raises(OwnerAuthorizationError):
        machine.get(proposal.proposal_id, actor_id=11)
    with pytest.raises(OwnerAuthorizationError):
        machine.audit_events(actor_id=11)


@pytest.mark.parametrize(
    "operation",
    [
        EvolutionOperation.DIRECT_MAIN_EDIT,
        EvolutionOperation.MERGE,
        EvolutionOperation.PUSH,
        EvolutionOperation.RESTART,
        EvolutionOperation.SECRET_CHANGE,
        EvolutionOperation.RBAC_CHANGE,
    ],
)
def test_forbidden_direct_operations_are_rejected(operation: EvolutionOperation) -> None:
    machine = EvolutionReviewMachine(bot_owner_ids=frozenset({10}), enabled=True)
    with pytest.raises(ForbiddenEvolutionOperationError):
        machine.propose(actor_id=10, spec=proposal_spec(operations=(operation,)))


@pytest.mark.parametrize(
    "override",
    [
        {"target_branch": "main"},
        {"target_paths": (".env",)},
        {"target_paths": ("config/secrets.toml",)},
        {"target_paths": ("src/rbac/policy.py",)},
        {"target_paths": ("config/permissions.json",)},
    ],
)
def test_protected_branches_secrets_and_rbac_paths_are_rejected(override: dict[str, object]) -> None:
    machine = EvolutionReviewMachine(bot_owner_ids=frozenset({10}), enabled=True)
    with pytest.raises(ForbiddenEvolutionOperationError):
        machine.propose(actor_id=10, spec=proposal_spec(**override))


def test_review_state_machine_records_hash_approver_and_chained_audit() -> None:
    machine = EvolutionReviewMachine(
        bot_owner_ids=frozenset({10, 20}),
        enabled=True,
        clock=AdvancingClock(),
    )
    proposed = machine.propose(actor_id=10, spec=proposal_spec())
    assert proposed.status is ProposalStatus.PROPOSED
    assert proposed.proposal_id == f"evo-{proposed.proposal_hash[:20]}"
    assert len(proposed.proposal_hash) == 64

    reviewing = machine.begin_review(proposed.proposal_id, actor_id=20, note="Review started")
    approved = machine.approve(reviewing.proposal_id, actor_id=20, note="Tests and scope verified")

    assert approved.status is ProposalStatus.APPROVED
    assert approved.approver_id == 20
    assert approved.review_note == "Tests and scope verified"
    events = machine.audit_events(actor_id=20, proposal_id=approved.proposal_id)
    assert [event.sequence for event in events] == [1, 2, 3]
    assert events[0].previous_hash is None
    assert events[1].previous_hash == events[0].event_hash
    assert events[2].previous_hash == events[1].event_hash
    assert all(len(event.event_hash) == 64 for event in events)
    assert machine.verify_integrity(approved.proposal_id, actor_id=20) is True


def test_approval_cannot_skip_review_or_continue_after_terminal_state() -> None:
    machine = EvolutionReviewMachine(bot_owner_ids=frozenset({10}), enabled=True)
    proposal = machine.propose(actor_id=10, spec=proposal_spec())
    with pytest.raises(InvalidProposalTransitionError):
        machine.approve(proposal.proposal_id, actor_id=10, note="too early")
    machine.begin_review(proposal.proposal_id, actor_id=10)
    machine.reject(proposal.proposal_id, actor_id=10, note="needs changes")
    with pytest.raises(InvalidProposalTransitionError):
        machine.approve(proposal.proposal_id, actor_id=10, note="too late")
