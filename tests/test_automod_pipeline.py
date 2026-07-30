from __future__ import annotations

from datetime import datetime, timezone

import pytest

from yonerai_discord.modules.automod.detectors import KeywordDetector
from yonerai_discord.modules.automod.domain import (
    ActionPlan,
    ActionType,
    DetectionContext,
    Decision,
    EventKind,
    MessageEvent,
    PlannedAction,
    Policy,
    Severity,
)
from yonerai_discord.modules.automod.pipeline import (
    ActionExecutor,
    ActionPlanner,
    AutomodPipeline,
    DetectionEngine,
    PolicyDecider,
)


class FakePort:
    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.calls: list[tuple[str, int]] = []
        self.audit = []

    async def claim_action(self, action_key: str) -> bool:
        if action_key in self.claimed:
            return False
        self.claimed.add(action_key)
        return True

    async def write_audit(self, record) -> None:
        self.audit.append(record)


def make_event(**overrides: object) -> MessageEvent:
    values: dict[str, object] = {
        "kind": EventKind.MESSAGE_CREATE,
        "guild_id": 1,
        "channel_id": 2,
        "message_id": 3,
        "author_id": 4,
        "content": "禁止語",
        "occurred_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return MessageEvent(**values)  # type: ignore[arg-type]


def pipeline(port: FakePort, severity: Severity = Severity.MEDIUM) -> AutomodPipeline:
    return AutomodPipeline(
        DetectionEngine((KeywordDetector(frozenset({"禁止語"}), severity),)),
        PolicyDecider(),
        ActionPlanner(),
        ActionExecutor(port),
    )


@pytest.mark.asyncio
async def test_pipeline_is_idempotent_for_redelivered_event() -> None:
    port = FakePort()
    service = pipeline(port)
    target = make_event()
    await service.process(target, DetectionContext(), Policy())
    await service.process(target, DetectionContext(), Policy())
    assert port.calls == []
    assert len(port.claimed) == 1
    assert all(record.succeeded for record in port.audit)


@pytest.mark.asyncio
async def test_edit_with_changed_content_gets_distinct_action_key() -> None:
    port = FakePort()
    service = pipeline(port)
    await service.process(make_event(), DetectionContext(), Policy())
    await service.process(
        make_event(kind=EventKind.MESSAGE_EDIT, content="禁止語 改変"),
        DetectionContext(),
        Policy(),
    )
    assert len(port.claimed) == 2


@pytest.mark.asyncio
async def test_allowlisted_role_prevents_actions() -> None:
    port = FakePort()
    target = make_event(author_role_ids=frozenset({99}))
    records = await pipeline(port).process(
        target,
        DetectionContext(),
        Policy(ignored_role_ids=frozenset({99})),
    )
    assert records == ()
    assert port.calls == []


@pytest.mark.asyncio
async def test_strikes_change_report_severity_but_never_enforce() -> None:
    port = FakePort()
    service = pipeline(port)
    await service.process(make_event(message_id=10), DetectionContext(), Policy(timeout_seconds=45), prior_strikes=2)
    await service.process(make_event(message_id=11), DetectionContext(), Policy(), prior_strikes=5)
    assert port.calls == []
    assert {record.action_type for record in port.audit} == {ActionType.AUDIT}
    assert {record.severity for record in port.audit} == {Severity.HIGH, Severity.CRITICAL}


@pytest.mark.asyncio
async def test_executor_rejects_forged_enforcement_plan() -> None:
    port = FakePort()
    target = make_event()
    action = PlannedAction("ban_member", "forged", "unsafe")  # type: ignore[arg-type]
    plan = ActionPlan(
        target,
        Decision(True, Severity.CRITICAL, "unsafe", (), 99),
        (action,),
    )

    with pytest.raises(RuntimeError, match="enforcement is disabled"):
        await ActionExecutor(port).execute(plan)

    assert port.calls == []
    assert port.claimed == set()
