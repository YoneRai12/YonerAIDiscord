from __future__ import annotations

from hashlib import sha256

from .detectors import Detector
from .domain import (
    ActionPlan,
    ActionType,
    AuditRecord,
    Decision,
    Detection,
    DetectionContext,
    MessageEvent,
    PlannedAction,
    Policy,
    Severity,
)
from .ports import ModerationPort


class DetectionEngine:
    def __init__(self, detectors: tuple[Detector, ...]) -> None:
        self.detectors = detectors

    def detect(self, event: MessageEvent, context: DetectionContext, policy: Policy) -> tuple[Detection, ...]:
        return tuple(finding for detector in self.detectors for finding in detector.detect(event, context, policy))


class PolicyDecider:
    def decide(
        self,
        event: MessageEvent,
        detections: tuple[Detection, ...],
        policy: Policy,
        *,
        prior_strikes: int = 0,
    ) -> Decision:
        if (
            event.author_id in policy.ignored_user_ids
            or event.channel_id in policy.ignored_channel_ids
            or not event.author_role_ids.isdisjoint(policy.ignored_role_ids)
        ):
            return Decision(False, Severity.INFO, "allowlist", detections, prior_strikes)
        eligible = tuple(item for item in detections if item.severity >= policy.minimum_severity)
        if not eligible:
            return Decision(False, Severity.INFO, "no_actionable_detection", detections, prior_strikes)
        severity = max(item.severity for item in eligible)
        if prior_strikes >= 5:
            severity = Severity.CRITICAL
        elif prior_strikes >= 2 and severity < Severity.CRITICAL:
            severity = Severity(min(int(Severity.CRITICAL), int(severity) + 10))
        reason = ", ".join(sorted({item.rule for item in eligible}))
        return Decision(True, severity, reason, eligible, prior_strikes)


class ActionPlanner:
    def plan(self, event: MessageEvent, decision: Decision, policy: Policy) -> ActionPlan:
        if not decision.should_act:
            return ActionPlan(event, decision, ())
        # AutoModは安全なreport-onlyに限定する。検出のseverityやstrikeは
        # 自動削除・timeout・banの許可に変換しない。
        types: tuple[ActionType, ...] = (ActionType.AUDIT,)
        actions = tuple(
            PlannedAction(
                action_type=action_type,
                action_key=self._action_key(event, action_type, decision.reason),
                reason=decision.reason,
                duration_seconds=None,
            )
            for action_type in types
        )
        return ActionPlan(event, decision, actions)

    @staticmethod
    def _action_key(event: MessageEvent, action_type: ActionType, reason: str) -> str:
        # createの再配信は同じキー、編集で本文が変われば別キーになる。
        material = ":".join(
            (
                str(event.guild_id),
                str(event.channel_id),
                str(event.message_id),
                event.kind.value,
                event.content_fingerprint,
                action_type.value,
                reason,
            )
        )
        return f"automod:{sha256(material.encode('utf-8')).hexdigest()}"


class ActionExecutor:
    def __init__(self, port: ModerationPort) -> None:
        self.port = port

    async def execute(self, plan: ActionPlan) -> tuple[AuditRecord, ...]:
        records: list[AuditRecord] = []
        for action in plan.actions:
            if action.action_type is not ActionType.AUDIT:
                # 上流で改ざんしたActionPlanが渡されても破壊的副作用は実行しない。
                raise RuntimeError("automod enforcement is disabled; report-only actions are allowed")
            if not await self.port.claim_action(action.action_key):
                continue
            succeeded = True
            detail = ""
            try:
                # AUDITはDiscordへの強制処理を行わない。adapter側の
                # capability guard通過後にredacted reportだけを送信する。
                pass
            except Exception as exc:
                succeeded = False
                detail = f"{type(exc).__name__}: {exc}"[:500]
            record = AuditRecord(
                action_key=action.action_key,
                guild_id=plan.event.guild_id,
                channel_id=plan.event.channel_id,
                message_id=plan.event.message_id,
                actor_id=plan.event.author_id,
                action_type=action.action_type,
                severity=plan.decision.severity,
                reason=action.reason,
                succeeded=succeeded,
                detail=detail,
            )
            await self.port.write_audit(record)
            records.append(record)
        return tuple(records)


class AutomodPipeline:
    def __init__(
        self,
        detector: DetectionEngine,
        decider: PolicyDecider,
        planner: ActionPlanner,
        executor: ActionExecutor,
    ) -> None:
        self.detector = detector
        self.decider = decider
        self.planner = planner
        self.executor = executor

    async def process(
        self,
        event: MessageEvent,
        context: DetectionContext,
        policy: Policy,
        *,
        prior_strikes: int = 0,
    ) -> tuple[AuditRecord, ...]:
        findings = self.detector.detect(event, context, policy)
        decision = self.decider.decide(event, findings, policy, prior_strikes=prior_strikes)
        plan = self.planner.plan(event, decision, policy)
        return await self.executor.execute(plan)
