"""登録済みActionSpecだけを依存順に実行するbounded orchestration kernel。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

import discord

from yonerai_discord.modules.media_pipeline.domain import ArtifactKind, ArtifactRef, ArtifactScope

from .action_router import (
    ActionEffect,
    ActionMode,
    ActionRegistry,
    ActionResult,
    ActionSpec,
    ActionStatus,
    NaturalActionRouter,
    artifact_kinds_from_schema,
    artifact_list_bounds_from_schema,
)
from .models import AIRequest
from .orchestration_repository import (
    DurableClaim,
    DurableClaimKind,
    DurableRunRecord,
    DurableStepDefinition,
    DurableStepRecord,
    OrchestrationRepositoryError,
    SqliteOrchestrationRepository,
)


_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_PARAMETERS = 32
_MAX_PARAMETER_KEY_CHARS = 64
_MAX_PARAMETER_VALUE_CHARS = 2_000
_MAX_PARAMETER_BYTES = 8_192
_MAX_CONFIRMED_PLAN_APPROVALS = 256
_WINDOWS_PATH_RE = re.compile(r"(?:[A-Za-z]:|[\\/]{2}|\\\\[?.]\\)")
# ``?`` はHTTPS queryを含むinert textでも使うため、実行されないplan文字列ではglob扱いしない。
_GLOB_CHARACTERS = frozenset("*[]")


class OrchestrationError(RuntimeError):
    """orchestration契約違反の基底例外。"""


class PlanValidationError(OrchestrationError):
    """plan全体を実行前に拒否した。"""


class PlanBindingError(OrchestrationError):
    """request/guild/channel/user bindingが一致しない。"""


class PlanIdempotencyConflictError(OrchestrationError):
    """同じidempotency keyが別planへ再利用された。"""


class PlanApprovalError(PlanValidationError):
    """planner由来のside-effect planに有効な本人確認receiptがない。"""


class PlanExecutionInProgressError(OrchestrationError):
    """Another durable owner still holds the bounded execution lease."""


class PlanStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class PlanEventType(StrEnum):
    PLAN_STARTED = "plan_started"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    PLAN_COMPLETED = "plan_completed"
    PLAN_FAILED = "plan_failed"


@dataclass(frozen=True, slots=True)
class OrchestrationPolicy:
    max_parallel_read_only: int = 3
    max_steps: int = 20
    total_timeout_seconds: float = 600.0
    max_dependency_depth: int = 8
    max_idempotency_entries: int = 256

    def __post_init__(self) -> None:
        for name, value, lower, upper in (
            ("max_parallel_read_only", self.max_parallel_read_only, 1, 3),
            ("max_steps", self.max_steps, 1, 20),
            ("max_dependency_depth", self.max_dependency_depth, 1, 8),
            ("max_idempotency_entries", self.max_idempotency_entries, 1, 1_024),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{name} is outside the allowed range")
        timeout = self.total_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0.1 <= float(timeout) <= 600.0
        ):
            raise ValueError("total_timeout_seconds is outside the allowed range")
        object.__setattr__(self, "total_timeout_seconds", float(timeout))


@dataclass(frozen=True, slots=True)
class ArtifactFromStep:
    """plan内だけで使える、opaque artifactの直接依存step参照。"""

    step_id: str

    def __post_init__(self) -> None:
        step_id = _identifier(self.step_id, "artifact source step_id")
        if step_id.casefold().startswith("step:") or "${" in step_id:
            raise ValueError("artifact source step_id uses forbidden binding syntax")
        object.__setattr__(self, "step_id", step_id)


@dataclass(frozen=True, slots=True)
class ArtifactsFromSteps:
    """plan内だけで使える、順序付きopaque artifact直接依存step参照。"""

    step_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.step_ids, tuple):
            raise TypeError("artifact source step_ids must be a tuple")
        step_ids = tuple(_identifier(step_id, "artifact source step_id") for step_id in self.step_ids)
        if not 1 <= len(step_ids) <= 8:
            raise ValueError("artifact source step_ids are outside the 1..8 contract")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("artifact source step_ids must be unique")
        if any(step_id.casefold().startswith("step:") or "${" in step_id for step_id in step_ids):
            raise ValueError("artifact source step_id uses forbidden binding syntax")
        object.__setattr__(self, "step_ids", step_ids)


@dataclass(frozen=True, slots=True)
class OrchestrationStep:
    step_id: str
    action_id: str
    parameters: Mapping[str, str | ArtifactFromStep | ArtifactsFromSteps] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )
    depends_on: tuple[str, ...] = ()
    effect: ActionEffect = ActionEffect.SIDE_EFFECT
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        step_id = _identifier(self.step_id, "step_id")
        action_id = _identifier(self.action_id, "action_id")
        parameters = _parameters(self.parameters)
        dependencies = tuple(_identifier(value, "dependency") for value in self.depends_on)
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("step dependencies must be unique")
        if step_id in dependencies:
            raise ValueError("step cannot depend on itself")
        if not isinstance(self.effect, ActionEffect):
            raise TypeError("effect must be an ActionEffect")
        timeout = self.timeout_seconds
        if timeout is not None:
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or not 0.05 <= float(timeout) <= 600.0
            ):
                raise ValueError("step timeout is outside the allowed range")
            timeout = float(timeout)
        object.__setattr__(self, "step_id", step_id)
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "parameters", MappingProxyType(parameters))
        object.__setattr__(self, "depends_on", dependencies)
        object.__setattr__(self, "timeout_seconds", timeout)


@dataclass(frozen=True, slots=True)
class OrchestrationPlan:
    request_id: str
    guild_id: int
    channel_id: int
    user_id: int
    idempotency_key: str
    steps: tuple[OrchestrationStep, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        object.__setattr__(self, "idempotency_key", _identifier(self.idempotency_key, "idempotency_key"))
        for name in ("guild_id", "channel_id", "user_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        steps = tuple(self.steps)
        if not steps or any(not isinstance(step, OrchestrationStep) for step in steps):
            raise ValueError("steps must contain OrchestrationStep values")
        object.__setattr__(self, "steps", steps)

    @property
    def digest(self) -> str:
        payload = {
            "request_id": self.request_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "idempotency_key": self.idempotency_key,
            "steps": [
                {
                    "step_id": step.step_id,
                    "action_id": step.action_id,
                    "parameters": {key: _parameter_json(value) for key, value in step.parameters.items()},
                    "depends_on": list(step.depends_on),
                    "effect": step.effect.value,
                    "timeout_seconds": step.timeout_seconds,
                }
                for step in self.steps
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class StepReceipt:
    step_id: str
    action_id: str
    status: StepStatus
    action_status: ActionStatus | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class PlanReceipt:
    request_id: str
    guild_id: int
    channel_id: int
    user_id: int
    idempotency_key: str
    plan_digest: str
    status: PlanStatus
    steps: tuple[StepReceipt, ...]


@dataclass(frozen=True, slots=True)
class PlanApprovalReceipt:
    """本文・parameters・秘密を保持しない、planner side-effect実行確認。"""

    plan_digest: str
    request_id: str
    guild_id: int
    channel_id: int
    user_id: int
    idempotency_key: str
    source_message_id: int
    prompt_message_id: int | None
    digest: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.plan_digest) is None:
            raise ValueError("plan approval plan digest is invalid")
        object.__setattr__(self, "request_id", _identifier(self.request_id, "approval request_id"))
        object.__setattr__(
            self,
            "idempotency_key",
            _identifier(self.idempotency_key, "approval idempotency_key"),
        )
        for name in ("guild_id", "channel_id", "user_id", "source_message_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"approval {name} must be a positive integer")
        prompt_message_id = self.prompt_message_id
        if prompt_message_id is not None and (
            isinstance(prompt_message_id, bool) or not isinstance(prompt_message_id, int) or prompt_message_id <= 0
        ):
            raise ValueError("approval prompt_message_id must be a positive integer")
        if self.digest and re.fullmatch(r"[0-9a-f]{64}", self.digest) is None:
            raise ValueError("plan approval canonical digest is invalid")


def plan_approval_receipt_digest(receipt: PlanApprovalReceipt) -> str:
    """receiptの安全なscope事実だけをcanonical digestへ束縛する。"""

    if not isinstance(receipt, PlanApprovalReceipt):
        raise TypeError("receipt must be a PlanApprovalReceipt")
    payload = {
        "schema": "yonerai.discord.plan-approval.v1",
        "plan_digest": receipt.plan_digest,
        "request_id": receipt.request_id,
        "guild_id": receipt.guild_id,
        "channel_id": receipt.channel_id,
        "user_id": receipt.user_id,
        "idempotency_key": receipt.idempotency_key,
        "source_message_id": receipt.source_message_id,
        "prompt_message_id": receipt.prompt_message_id,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def build_plan_approval_receipt(
    plan: OrchestrationPlan,
    *,
    source_message_id: int,
    prompt_message_id: int | None = None,
) -> PlanApprovalReceipt:
    """型付きplanから、実行内容を複製しない確認receiptを作る。"""

    if not isinstance(plan, OrchestrationPlan):
        raise TypeError("plan must be an OrchestrationPlan")
    unsealed = PlanApprovalReceipt(
        plan_digest=plan.digest,
        request_id=plan.request_id,
        guild_id=plan.guild_id,
        channel_id=plan.channel_id,
        user_id=plan.user_id,
        idempotency_key=plan.idempotency_key,
        source_message_id=source_message_id,
        prompt_message_id=prompt_message_id,
        digest="",
    )
    return replace(unsealed, digest=plan_approval_receipt_digest(unsealed))


@dataclass(frozen=True, slots=True)
class PlanPublicOutput:
    step_id: str
    action_id: str
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        text = self.text.strip()
        if not text or len(text) > 1_900:
            raise ValueError("plan public output is outside the bounded contract")
        object.__setattr__(self, "text", text)


@dataclass(frozen=True, slots=True)
class PlanArtifactOutput:
    """Discord公開文やreceiptへ混ぜない、内部artifact outcome。"""

    step_id: str
    action_id: str
    artifact: ArtifactRef = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ArtifactRef):
            raise TypeError("plan artifact output must be an ArtifactRef")


@dataclass(frozen=True, slots=True)
class PlanExecutionOutcome:
    receipt: PlanReceipt
    public_outputs: tuple[PlanPublicOutput, ...] = field(default=(), repr=False)
    artifact_outputs: tuple[PlanArtifactOutput, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _ResolvedArtifactBinding:
    source_step: OrchestrationStep
    source_spec: ActionSpec
    source_receipt: StepReceipt
    artifact: ArtifactRef = field(repr=False)
    accepted_kinds: frozenset[ArtifactKind]


@dataclass(slots=True)
class _CancellationState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancelled: bool = False
    claimed: bool = False


@dataclass(frozen=True, slots=True)
class PlanEvent:
    event_type: PlanEventType
    plan_digest: str
    idempotency_key: str
    step_id: str | None = None
    action_id: str | None = None


class PlanObserver(Protocol):
    def __call__(self, event: PlanEvent) -> Awaitable[None] | None: ...


class ValidatedPlanPort(Protocol):
    async def load_plan(self) -> OrchestrationPlan: ...


class OrchestrationEngine:
    """ActionRegistryを唯一の実行正本として使うin-memory bounded executor。"""

    def __init__(
        self,
        router: NaturalActionRouter,
        *,
        policy: OrchestrationPolicy | None = None,
        observer: PlanObserver | None = None,
        repository: SqliteOrchestrationRepository | None = None,
    ) -> None:
        if not isinstance(router, NaturalActionRouter):
            raise TypeError("router must be a NaturalActionRouter")
        if observer is not None and not callable(observer):
            raise TypeError("observer must be callable")
        self.router = router
        self.registry = router.registry
        self.policy = policy or OrchestrationPolicy()
        self.observer = observer
        if repository is not None and not isinstance(repository, SqliteOrchestrationRepository):
            raise TypeError("repository must be a SqliteOrchestrationRepository")
        if repository is not None and repository.lease_seconds <= self.policy.total_timeout_seconds:
            raise ValueError("repository lease must exceed the total orchestration timeout")
        self.repository = repository
        self._lock = asyncio.Lock()
        self._completed: OrderedDict[str, tuple[str, PlanExecutionOutcome]] = OrderedDict()
        self._confirmed_port_approvals: OrderedDict[
            int,
            tuple[PlanApprovalReceipt, NaturalActionRouter, ActionRegistry, bool],
        ] = OrderedDict()
        self._running: dict[str, tuple[str, asyncio.Task[PlanExecutionOutcome]]] = {}
        self._request_observer: ContextVar[PlanObserver | None] = ContextVar(
            "orchestration_request_observer",
            default=None,
        )
        self._request_outputs: ContextVar[dict[str, str] | None] = ContextVar(
            "orchestration_request_outputs",
            default=None,
        )
        self._request_artifacts: ContextVar[dict[str, ArtifactRef] | None] = ContextVar(
            "orchestration_request_artifacts",
            default=None,
        )
        self._port_approval_boundary: ContextVar[
            tuple[NaturalActionRouter, ActionRegistry, PlanApprovalReceipt] | None
        ] = ContextVar("orchestration_port_approval_boundary", default=None)
        self._durable_claim: ContextVar[DurableClaim | None] = ContextVar(
            "orchestration_durable_claim",
            default=None,
        )

    async def execute_from_port(
        self,
        port: ValidatedPlanPort,
        *,
        message: discord.Message,
        request: AIRequest,
        request_id: str,
        approval: PlanApprovalReceipt | None = None,
        observer: PlanObserver | None = None,
    ) -> PlanReceipt:
        plan = await port.load_plan()
        if not isinstance(plan, OrchestrationPlan):
            raise PlanValidationError("plan port returned an unvalidated value")
        self._validate_port_action_contracts(plan)
        boundary = self._validate_port_approval(plan, message, approval)
        token = self._port_approval_boundary.set(boundary)
        try:
            return await self.execute(
                plan,
                message=message,
                request=request,
                request_id=request_id,
                observer=observer,
            )
        finally:
            self._port_approval_boundary.reset(token)

    async def execute_outcome_from_port(
        self,
        port: ValidatedPlanPort,
        *,
        message: discord.Message,
        request: AIRequest,
        request_id: str,
        approval: PlanApprovalReceipt | None = None,
        observer: PlanObserver | None = None,
    ) -> PlanExecutionOutcome:
        plan = await port.load_plan()
        if not isinstance(plan, OrchestrationPlan):
            raise PlanValidationError("plan port returned an unvalidated value")
        self._validate_port_action_contracts(plan)
        boundary = self._validate_port_approval(plan, message, approval)
        token = self._port_approval_boundary.set(boundary)
        try:
            return await self.execute_outcome(
                plan,
                message=message,
                request=request,
                request_id=request_id,
                observer=observer,
            )
        finally:
            self._port_approval_boundary.reset(token)

    def _validate_port_action_contracts(self, plan: OrchestrationPlan) -> None:
        """外部plan portがplanner非公開actionを実行経路へ差し込むことを拒否する。"""

        for step in plan.steps:
            try:
                spec = self.registry.get(step.action_id)
            except KeyError as exc:
                raise PlanValidationError("plan references an unknown action") from exc
            if spec.planner_contract is None:
                raise PlanValidationError("plan port selected an action without a planner contract")

    def port_plan_requires_approval(self, plan: OrchestrationPlan) -> bool:
        """code-owned specがside effectを含む時だけ確認を要求する。"""

        if not isinstance(plan, OrchestrationPlan):
            raise TypeError("plan must be an OrchestrationPlan")
        self._validate_port_action_contracts(plan)
        specs = self._preflight(plan)
        return any(specs[step.step_id].effect is ActionEffect.SIDE_EFFECT for step in plan.steps)

    def approval_receipt_matches(
        self,
        plan: OrchestrationPlan,
        message: discord.Message,
        approval: PlanApprovalReceipt | None,
    ) -> bool:
        """外部callbackを呼ばずreceiptとDiscord source事実だけを照合する。"""

        if not isinstance(plan, OrchestrationPlan) or not isinstance(approval, PlanApprovalReceipt):
            return False
        source_message_id = getattr(message, "id", None)
        if isinstance(source_message_id, bool) or not isinstance(source_message_id, int) or source_message_id <= 0:
            return False
        return (
            approval.prompt_message_id is not None
            and approval.plan_digest == plan.digest
            and approval.request_id == plan.request_id
            and approval.guild_id == plan.guild_id
            and approval.channel_id == plan.channel_id
            and approval.user_id == plan.user_id
            and approval.idempotency_key == plan.idempotency_key
            and approval.source_message_id == source_message_id
            and approval.digest == plan_approval_receipt_digest(replace(approval, digest=""))
        )

    def register_port_plan_approval(
        self,
        plan: OrchestrationPlan,
        message: discord.Message,
        approval: PlanApprovalReceipt,
    ) -> None:
        """fresh Discord確認済みのexact receipt objectだけをboundedに保持する。"""

        if not self.port_plan_requires_approval(plan):
            raise PlanApprovalError("read-only planner plan does not require confirmation")
        if not self.approval_receipt_matches(plan, message, approval):
            raise PlanApprovalError("planner confirmation receipt does not match")
        key = id(approval)
        existing = self._confirmed_port_approvals.get(key)
        if existing is None:
            self._confirmed_port_approvals[key] = (approval, self.router, self.registry, False)
        elif existing[0] is not approval:
            raise PlanApprovalError("planner confirmation receipt identity collision")
        self._confirmed_port_approvals.move_to_end(key)
        while len(self._confirmed_port_approvals) > _MAX_CONFIRMED_PLAN_APPROVALS:
            self._confirmed_port_approvals.popitem(last=False)

    def _validate_port_approval(
        self,
        plan: OrchestrationPlan,
        message: discord.Message,
        approval: PlanApprovalReceipt | None,
    ) -> tuple[NaturalActionRouter, ActionRegistry, PlanApprovalReceipt] | None:
        requires_approval = self.port_plan_requires_approval(plan)
        if approval is None:
            if requires_approval:
                raise PlanApprovalError("planner side-effect plan requires confirmation")
            return None
        if not self.approval_receipt_matches(plan, message, approval):
            raise PlanApprovalError("planner confirmation receipt does not match")
        if requires_approval:
            registered = self._confirmed_port_approvals.get(id(approval))
            if (
                registered is None
                or registered[0] is not approval
                or registered[1] is not self.router
                or registered[2] is not self.registry
                or self.router.registry is not self.registry
            ):
                raise PlanApprovalError("planner confirmation receipt was not registered by this engine")
            self._confirmed_port_approvals.move_to_end(id(approval))
            return registered[1], registered[2], approval
        return None

    async def execute(
        self,
        plan: OrchestrationPlan,
        *,
        message: discord.Message,
        request: AIRequest,
        request_id: str,
        observer: PlanObserver | None = None,
    ) -> PlanReceipt:
        outcome = await self.execute_outcome(
            plan,
            message=message,
            request=request,
            request_id=request_id,
            observer=observer,
        )
        return outcome.receipt

    async def execute_outcome(
        self,
        plan: OrchestrationPlan,
        *,
        message: discord.Message,
        request: AIRequest,
        request_id: str,
        observer: PlanObserver | None = None,
    ) -> PlanExecutionOutcome:
        if not isinstance(plan, OrchestrationPlan):
            raise TypeError("plan must be an OrchestrationPlan")
        if observer is not None and not callable(observer):
            raise TypeError("observer must be callable")
        self._validate_binding(plan, message, request, request_id)
        digest = plan.digest
        async with self._lock:
            approval_boundary = self._port_approval_boundary.get()
            if approval_boundary is not None:
                approval = approval_boundary[2]
                registered = self._confirmed_port_approvals.get(id(approval))
                if (
                    registered is None
                    or registered[0] is not approval
                    or registered[1] is not approval_boundary[0]
                    or registered[2] is not approval_boundary[1]
                ):
                    raise PlanApprovalError("planner confirmation receipt is no longer current")
                if registered[3]:
                    completed_approval = self._completed.get(plan.idempotency_key)
                    running_approval = self._running.get(plan.idempotency_key)
                    if not (
                        (completed_approval is not None and completed_approval[0] == digest)
                        or (running_approval is not None and running_approval[0] == digest)
                    ):
                        raise PlanApprovalError("planner confirmation receipt was already consumed")
                else:
                    self._confirmed_port_approvals[id(approval)] = (
                        registered[0],
                        registered[1],
                        registered[2],
                        True,
                    )
            completed = self._completed.get(plan.idempotency_key)
            if completed is not None:
                if completed[0] != digest:
                    raise PlanIdempotencyConflictError("idempotency key belongs to another plan")
                self._completed.move_to_end(plan.idempotency_key)
                return completed[1]
            running = self._running.get(plan.idempotency_key)
            if running is not None:
                if running[0] != digest:
                    raise PlanIdempotencyConflictError("idempotency key belongs to another plan")
                task = running[1]
            else:
                specs = self._preflight(plan)
                durable_claim = await self._claim_durable(plan)
                if durable_claim is not None:
                    if durable_claim.kind is DurableClaimKind.REPLAY:
                        outcome = self._outcome_from_durable(plan, durable_claim.record)
                        self._completed[plan.idempotency_key] = (digest, outcome)
                        while len(self._completed) > self.policy.max_idempotency_entries:
                            self._completed.popitem(last=False)
                        return outcome
                    if durable_claim.kind is DurableClaimKind.BUSY:
                        raise PlanExecutionInProgressError("durable orchestration plan is already running")
                task = asyncio.create_task(
                    self._run_and_finalize(
                        plan,
                        specs,
                        message,
                        request,
                        digest,
                        observer,
                        durable_claim,
                    )
                )
                self._running[plan.idempotency_key] = (digest, task)
        return await asyncio.shield(task)

    async def _run_and_finalize(
        self,
        plan: OrchestrationPlan,
        specs: Mapping[str, ActionSpec],
        message: discord.Message,
        request: AIRequest,
        digest: str,
        observer: PlanObserver | None,
        durable_claim: DurableClaim | None,
    ) -> PlanExecutionOutcome:
        token = self._request_observer.set(observer)
        resume_steps = (
            durable_claim.record.steps
            if durable_claim is not None and durable_claim.kind is DurableClaimKind.RESUME
            else ()
        )
        initial_outputs = {
            step.step_id: step.public_text
            for step in resume_steps
            if step.state == "completed" and step.public_text is not None
        }
        initial_artifacts = {
            step.step_id: step.artifact
            for step in resume_steps
            if step.state == "completed" and step.artifact is not None
        }
        output_token = self._request_outputs.set(initial_outputs)
        artifact_token = self._request_artifacts.set(initial_artifacts)
        durable_token = self._durable_claim.set(durable_claim)
        try:
            receipt = await self._run(plan, specs, message, request)
            captured = self._request_outputs.get() or {}
            artifacts = self._request_artifacts.get() or {}
            public_outputs = tuple(
                PlanPublicOutput(step.step_id, step.action_id, captured[step.step_id])
                for step in plan.steps
                if step.step_id in captured
                and next(item for item in receipt.steps if item.step_id == step.step_id).status is StepStatus.COMPLETED
            )
            artifact_outputs = tuple(
                PlanArtifactOutput(step.step_id, step.action_id, artifacts[step.step_id])
                for step in plan.steps
                if step.step_id in artifacts
                and next(item for item in receipt.steps if item.step_id == step.step_id).status is StepStatus.COMPLETED
            )
            outcome = PlanExecutionOutcome(receipt, public_outputs, artifact_outputs)
            await self._complete_durable(outcome)
        except BaseException:
            await self._abandon_durable()
            async with self._lock:
                current = self._running.get(plan.idempotency_key)
                if current is not None and current[1] is asyncio.current_task():
                    self._running.pop(plan.idempotency_key, None)
            raise
        finally:
            self._durable_claim.reset(durable_token)
            self._request_artifacts.reset(artifact_token)
            self._request_outputs.reset(output_token)
            self._request_observer.reset(token)
        async with self._lock:
            existing = self._completed.get(plan.idempotency_key)
            if existing is None:
                self._completed[plan.idempotency_key] = (digest, outcome)
                while len(self._completed) > self.policy.max_idempotency_entries:
                    self._completed.popitem(last=False)
            else:
                outcome = existing[1]
            current = self._running.get(plan.idempotency_key)
            if current is not None and current[1] is asyncio.current_task():
                self._running.pop(plan.idempotency_key, None)
        return outcome

    async def _claim_durable(self, plan: OrchestrationPlan) -> DurableClaim | None:
        repository = self.repository
        if repository is None:
            return None
        definitions = tuple(
            DurableStepDefinition(step.step_id, step.action_id, step.effect.value) for step in plan.steps
        )
        try:
            return await asyncio.to_thread(
                repository.claim,
                idempotency_key=plan.idempotency_key,
                plan_digest=plan.digest,
                request_id=plan.request_id,
                guild_id=plan.guild_id,
                channel_id=plan.channel_id,
                user_id=plan.user_id,
                steps=definitions,
            )
        except OrchestrationRepositoryError as exc:
            raise PlanIdempotencyConflictError("durable orchestration claim was rejected") from exc

    def _outcome_from_durable(
        self,
        plan: OrchestrationPlan,
        record: DurableRunRecord,
    ) -> PlanExecutionOutcome:
        expected_scope = (
            plan.idempotency_key,
            plan.digest,
            plan.request_id,
            plan.guild_id,
            plan.channel_id,
            plan.user_id,
        )
        actual_scope = (
            record.idempotency_key,
            record.plan_digest,
            record.request_id,
            record.guild_id,
            record.channel_id,
            record.user_id,
        )
        if actual_scope != expected_scope or record.state not in {"completed", "failed"}:
            raise PlanValidationError("durable orchestration receipt does not match the plan")
        if len(record.steps) != len(plan.steps):
            raise PlanValidationError("durable orchestration receipt has invalid steps")
        receipts: list[StepReceipt] = []
        public_outputs: list[PlanPublicOutput] = []
        artifact_outputs: list[PlanArtifactOutput] = []
        scope = ArtifactScope(plan.request_id, plan.guild_id, plan.channel_id, plan.user_id)
        for expected, stored in zip(plan.steps, record.steps, strict=True):
            if (
                stored.step_id != expected.step_id
                or stored.action_id != expected.action_id
                or stored.effect != expected.effect.value
                or stored.state not in {"completed", "failed", "not_run"}
            ):
                raise PlanValidationError("durable orchestration step receipt is invalid")
            step_status = StepStatus(stored.state)
            try:
                action_status = ActionStatus(stored.action_status) if stored.action_status is not None else None
            except ValueError as exc:
                raise PlanValidationError("durable orchestration action status is invalid") from exc
            receipts.append(
                StepReceipt(
                    stored.step_id,
                    stored.action_id,
                    step_status,
                    action_status,
                    stored.failure_code,
                )
            )
            if step_status is StepStatus.COMPLETED and stored.public_text is not None:
                public_outputs.append(PlanPublicOutput(stored.step_id, stored.action_id, stored.public_text))
            if step_status is StepStatus.COMPLETED and stored.artifact is not None:
                if stored.artifact.scope_digest != scope.digest:
                    raise PlanValidationError("durable orchestration artifact scope is invalid")
                artifact_outputs.append(PlanArtifactOutput(stored.step_id, stored.action_id, stored.artifact))
        try:
            status = PlanStatus(record.plan_status or record.state)
        except ValueError as exc:
            raise PlanValidationError("durable orchestration plan status is invalid") from exc
        return PlanExecutionOutcome(
            PlanReceipt(
                plan.request_id,
                plan.guild_id,
                plan.channel_id,
                plan.user_id,
                plan.idempotency_key,
                plan.digest,
                status,
                tuple(receipts),
            ),
            tuple(public_outputs),
            tuple(artifact_outputs),
        )

    async def _complete_durable(self, outcome: PlanExecutionOutcome) -> None:
        repository = self.repository
        claim = self._durable_claim.get()
        if repository is None or claim is None or claim.owner_token is None:
            return
        receipt = outcome.receipt
        definitions = {step.step_id: step for step in claim.record.steps}
        public_outputs = {item.step_id: item.text for item in outcome.public_outputs}
        artifact_outputs = {item.step_id: item.artifact for item in outcome.artifact_outputs}
        records = tuple(
            DurableStepRecord(
                step_id=step.step_id,
                action_id=step.action_id,
                effect=definitions[step.step_id].effect,
                state=step.status.value,
                action_status=step.action_status.value if step.action_status is not None else None,
                failure_code=step.failure_code,
                public_text=public_outputs.get(step.step_id),
                artifact=artifact_outputs.get(step.step_id),
            )
            for step in receipt.steps
        )
        await asyncio.to_thread(
            repository.complete,
            idempotency_key=receipt.idempotency_key,
            plan_digest=receipt.plan_digest,
            owner_token=claim.owner_token,
            plan_status=receipt.status.value,
            steps=records,
        )

    async def _abandon_durable(self) -> None:
        repository = self.repository
        claim = self._durable_claim.get()
        if repository is None or claim is None or claim.owner_token is None:
            return
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    repository.abandon,
                    idempotency_key=claim.record.idempotency_key,
                    plan_digest=claim.record.plan_digest,
                    owner_token=claim.owner_token,
                )
            )
        except BaseException:
            return

    async def _mark_durable_step_started(
        self,
        plan: OrchestrationPlan,
        step: OrchestrationStep,
    ) -> bool:
        repository = self.repository
        claim = self._durable_claim.get()
        if repository is None or claim is None or claim.owner_token is None:
            return True
        return await asyncio.to_thread(
            repository.mark_step_started,
            idempotency_key=plan.idempotency_key,
            plan_digest=plan.digest,
            owner_token=claim.owner_token,
            step_id=step.step_id,
        )

    async def _checkpoint_durable_step(
        self,
        plan: OrchestrationPlan,
        step: OrchestrationStep,
        receipt: StepReceipt,
        *,
        public_text: str | None = None,
        artifact: ArtifactRef | None = None,
    ) -> None:
        repository = self.repository
        claim = self._durable_claim.get()
        if repository is None or claim is None or claim.owner_token is None:
            return
        await asyncio.to_thread(
            repository.checkpoint_step,
            idempotency_key=plan.idempotency_key,
            plan_digest=plan.digest,
            owner_token=claim.owner_token,
            step_id=step.step_id,
            state=receipt.status.value,
            action_status=receipt.action_status.value if receipt.action_status is not None else None,
            failure_code=receipt.failure_code,
            public_text=public_text,
            artifact=artifact,
        )

    async def _durable_cancellation_requested(self, plan: OrchestrationPlan) -> bool:
        repository = self.repository
        claim = self._durable_claim.get()
        if repository is None or claim is None or claim.owner_token is None:
            return False
        return await asyncio.to_thread(
            repository.cancellation_requested,
            idempotency_key=plan.idempotency_key,
            plan_digest=plan.digest,
            request_id=plan.request_id,
            guild_id=plan.guild_id,
            channel_id=plan.channel_id,
            user_id=plan.user_id,
        )

    async def _cancellation_receipt_if_requested(
        self,
        plan: OrchestrationPlan,
        step: OrchestrationStep,
        state: _CancellationState,
        *,
        deadline: float,
    ) -> StepReceipt | None:
        if state.cancelled:
            return StepReceipt(
                step.step_id,
                step.action_id,
                StepStatus.NOT_RUN,
                failure_code="not_started",
            )
        try:
            requested = await self._durable_cancellation_requested(plan)
        except OrchestrationRepositoryError:
            requested = True
            failure_code = "cancellation_check_failed"
        else:
            failure_code = "cancelled"
        if not requested:
            return None
        return await self._claim_cancellation_receipt(
            plan,
            step,
            state,
            failure_code=failure_code,
            deadline=deadline,
        )

    async def _cancellation_observed_after_execution(
        self,
        plan: OrchestrationPlan,
        state: _CancellationState,
    ) -> bool:
        if state.cancelled:
            return True
        try:
            requested = await self._durable_cancellation_requested(plan)
        except OrchestrationRepositoryError:
            requested = True
        if not requested:
            return False
        async with state.lock:
            state.cancelled = True
        return True

    async def _claim_cancellation_receipt(
        self,
        plan: OrchestrationPlan,
        step: OrchestrationStep,
        state: _CancellationState,
        *,
        failure_code: str,
        deadline: float,
    ) -> StepReceipt:
        async with state.lock:
            state.cancelled = True
            if state.claimed:
                return StepReceipt(
                    step.step_id,
                    step.action_id,
                    StepStatus.NOT_RUN,
                    failure_code="not_started",
                )
            state.claimed = True
        return await self._step_failed(plan, step, failure_code, deadline=deadline)

    def _validate_binding(
        self,
        plan: OrchestrationPlan,
        message: discord.Message,
        request: AIRequest,
        request_id: str,
    ) -> None:
        if request_id != plan.request_id:
            raise PlanBindingError("request binding does not match")
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        actual = (
            getattr(guild, "id", None),
            getattr(channel, "id", None),
            getattr(author, "id", None),
        )
        expected = (plan.guild_id, plan.channel_id, plan.user_id)
        request_scope = (request.guild_id, request.channel_id, request.user_id)
        if actual != expected or request_scope != expected:
            raise PlanBindingError("Discord scope does not match the plan")

    def _preflight(self, plan: OrchestrationPlan) -> Mapping[str, ActionSpec]:
        if len(plan.steps) > self.policy.max_steps:
            raise PlanValidationError("plan exceeds the step limit")
        ids = [step.step_id for step in plan.steps]
        if len(ids) != len(set(ids)):
            raise PlanValidationError("step IDs must be unique")
        known = set(ids)
        specs: dict[str, ActionSpec] = {}
        for step in plan.steps:
            if any(dependency not in known for dependency in step.depends_on):
                raise PlanValidationError("plan has an unknown dependency")
            try:
                spec = self.registry.get(step.action_id)
            except KeyError as exc:
                raise PlanValidationError("plan references an unknown action") from exc
            if spec.mode is ActionMode.DEFER_TO_SLASH:
                raise PlanValidationError("deferred actions cannot run in a plan")
            if spec.effect is not step.effect:
                raise PlanValidationError("step effect does not match the registered action")
            specs[step.step_id] = spec
        depths: dict[str, int] = {}

        def depth(step_id: str, trail: frozenset[str]) -> int:
            if step_id in trail:
                raise PlanValidationError("plan dependency graph contains a cycle")
            if step_id in depths:
                return depths[step_id]
            step = next(item for item in plan.steps if item.step_id == step_id)
            value = 1 + max((depth(item, trail | {step_id}) for item in step.depends_on), default=0)
            if value > self.policy.max_dependency_depth:
                raise PlanValidationError("plan exceeds the dependency depth limit")
            depths[step_id] = value
            return value

        for step_id in ids:
            depth(step_id, frozenset())
        positions = {step_id: index for index, step_id in enumerate(ids)}
        for step in plan.steps:
            spec = specs[step.step_id]
            contract = spec.planner_contract
            properties = contract.input_schema.get("properties") if contract is not None else None
            required = contract.input_schema.get("required") if contract is not None else ()
            if not isinstance(required, (list, tuple)):
                required = ()
            for name, value in step.parameters.items():
                rule = properties.get(name) if isinstance(properties, Mapping) else None
                if isinstance(value, (ArtifactFromStep, ArtifactsFromSteps)):
                    expected_type = "artifact_ref" if isinstance(value, ArtifactFromStep) else "artifact_ref_list"
                    if not isinstance(rule, Mapping) or rule.get("type") != expected_type:
                        raise PlanValidationError("artifact binding does not match the registered input slot")
                    accepted = artifact_kinds_from_schema(rule)
                    source_ids = _artifact_source_ids(value)
                    if isinstance(value, ArtifactsFromSteps):
                        minimum, maximum = artifact_list_bounds_from_schema(rule)
                        if not minimum <= len(source_ids) <= maximum:
                            raise PlanValidationError("artifact list binding count is outside the registered bounds")
                    for source_id in source_ids:
                        if source_id not in known:
                            raise PlanValidationError("artifact binding references an unknown step")
                        if source_id == step.step_id:
                            raise PlanValidationError("artifact binding cannot reference itself")
                        if positions[source_id] >= positions[step.step_id]:
                            raise PlanValidationError("artifact binding cannot reference a forward step")
                        if source_id not in step.depends_on:
                            raise PlanValidationError("artifact binding must reference a direct dependency")
                        producer_contract = specs[source_id].planner_contract
                        output_schema = (
                            producer_contract.output_artifact_schema if producer_contract is not None else None
                        )
                        if output_schema is None:
                            raise PlanValidationError("artifact producer has no registered output schema")
                        produced = artifact_kinds_from_schema(output_schema, output=True)
                        if accepted.isdisjoint(produced):
                            raise PlanValidationError("artifact producer and consumer kinds do not match")
                elif isinstance(rule, Mapping) and rule.get("type") in {"artifact_ref", "artifact_ref_list"}:
                    raise PlanValidationError("artifact input slot requires a typed step reference")
            if isinstance(properties, Mapping):
                for name, rule in properties.items():
                    if (
                        isinstance(rule, Mapping)
                        and rule.get("type") in {"artifact_ref", "artifact_ref_list"}
                        and (
                            (
                                name in step.parameters
                                and not isinstance(step.parameters[name], (ArtifactFromStep, ArtifactsFromSteps))
                            )
                            or (name in required and name not in step.parameters)
                        )
                    ):
                        raise PlanValidationError("artifact input slot requires a typed step reference")
        return MappingProxyType(specs)

    async def _run(
        self,
        plan: OrchestrationPlan,
        specs: Mapping[str, ActionSpec],
        message: discord.Message,
        request: AIRequest,
    ) -> PlanReceipt:
        deadline = asyncio.get_running_loop().time() + self.policy.total_timeout_seconds
        if not await self._emit(PlanEventType.PLAN_STARTED, plan, deadline=deadline):
            return await self._plan_timeout_receipt(plan, deadline)
        durable_claim = self._durable_claim.get()
        receipts: dict[str, StepReceipt] = {}
        if durable_claim is not None and durable_claim.kind is DurableClaimKind.RESUME:
            for stored in durable_claim.record.steps:
                if stored.state != "completed":
                    continue
                try:
                    action_status = ActionStatus(stored.action_status) if stored.action_status is not None else None
                except ValueError as exc:
                    raise PlanValidationError("durable checkpoint action status is invalid") from exc
                receipts[stored.step_id] = StepReceipt(
                    stored.step_id,
                    stored.action_id,
                    StepStatus.COMPLETED,
                    action_status,
                    stored.failure_code,
                )
        completed: set[str] = set(receipts)
        failed = False
        pending = {step.step_id: step for step in plan.steps if step.step_id not in completed}
        cancellation_state = _CancellationState()
        first_pending = next((step for step in plan.steps if step.step_id in pending), None)
        if first_pending is not None:
            cancelled = await self._cancellation_receipt_if_requested(
                plan,
                first_pending,
                cancellation_state,
                deadline=deadline,
            )
            if cancelled is not None:
                receipts[cancelled.step_id] = cancelled
                pending.pop(cancelled.step_id, None)
                failed = True
        while pending and not failed:
            ready = [
                step for step in plan.steps if step.step_id in pending and set(step.depends_on).issubset(completed)
            ]
            if not ready:
                failed = True
                break
            read_only = [step for step in ready if step.effect is ActionEffect.READ_ONLY]
            if read_only:
                for offset in range(0, len(read_only), self.policy.max_parallel_read_only):
                    batch = read_only[offset : offset + self.policy.max_parallel_read_only]
                    results = await asyncio.gather(
                        *(
                            self._run_step(
                                plan,
                                step,
                                specs[step.step_id],
                                specs,
                                receipts,
                                deadline,
                                message,
                                request,
                                cancellation_state,
                            )
                            for step in batch
                        )
                    )
                    for receipt in results:
                        receipts[receipt.step_id] = receipt
                        pending.pop(receipt.step_id, None)
                        if receipt.status is StepStatus.COMPLETED:
                            completed.add(receipt.step_id)
                        else:
                            failed = True
                    if cancellation_state.cancelled:
                        failed = True
                    if failed:
                        break
                continue
            step = ready[0]
            receipt = await self._run_step(
                plan,
                step,
                specs[step.step_id],
                specs,
                receipts,
                deadline,
                message,
                request,
                cancellation_state,
            )
            receipts[receipt.step_id] = receipt
            pending.pop(receipt.step_id, None)
            if receipt.status is StepStatus.COMPLETED:
                completed.add(receipt.step_id)
            else:
                failed = True
        for step in plan.steps:
            receipts.setdefault(
                step.step_id,
                StepReceipt(step.step_id, step.action_id, StepStatus.NOT_RUN, failure_code="not_started"),
            )
        status = PlanStatus.FAILED if failed else PlanStatus.COMPLETED
        await self._emit(
            PlanEventType.PLAN_FAILED if failed else PlanEventType.PLAN_COMPLETED,
            plan,
            deadline=deadline,
        )
        return PlanReceipt(
            plan.request_id,
            plan.guild_id,
            plan.channel_id,
            plan.user_id,
            plan.idempotency_key,
            plan.digest,
            status,
            tuple(receipts[step.step_id] for step in plan.steps),
        )

    async def _plan_timeout_receipt(self, plan: OrchestrationPlan, deadline: float) -> PlanReceipt:
        receipts = tuple(
            StepReceipt(
                step.step_id,
                step.action_id,
                StepStatus.FAILED if index == 0 else StepStatus.NOT_RUN,
                failure_code="plan_timeout" if index == 0 else "not_started",
            )
            for index, step in enumerate(plan.steps)
        )
        await self._emit(PlanEventType.PLAN_FAILED, plan, deadline=deadline)
        return PlanReceipt(
            plan.request_id,
            plan.guild_id,
            plan.channel_id,
            plan.user_id,
            plan.idempotency_key,
            plan.digest,
            PlanStatus.FAILED,
            receipts,
        )

    async def _run_step(
        self,
        plan: OrchestrationPlan,
        step: OrchestrationStep,
        spec: ActionSpec,
        specs: Mapping[str, ActionSpec],
        receipts: Mapping[str, StepReceipt],
        deadline: float,
        message: discord.Message,
        request: AIRequest,
        cancellation_state: _CancellationState,
    ) -> StepReceipt:
        cancelled = await self._cancellation_receipt_if_requested(
            plan,
            step,
            cancellation_state,
            deadline=deadline,
        )
        if cancelled is not None:
            return cancelled
        remaining = deadline - asyncio.get_running_loop().time()
        bounded_by_plan_deadline = step.timeout_seconds is None or step.timeout_seconds >= remaining
        if remaining <= 0:
            return await self._step_failed(plan, step, "plan_timeout", deadline=deadline)
        timeout = min(remaining, step.timeout_seconds or remaining)
        try:
            return await self._invoke_step(
                plan,
                step,
                spec,
                specs,
                receipts,
                deadline,
                asyncio.get_running_loop().time() + timeout,
                bounded_by_plan_deadline,
                message,
                request,
                cancellation_state,
            )
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return await self._step_failed(plan, step, "cancelled_uncertain", deadline=deadline)
        except Exception:
            return await self._step_failed(plan, step, "executor_error", deadline=deadline)

    async def _invoke_step(
        self,
        plan: OrchestrationPlan,
        step: OrchestrationStep,
        spec: ActionSpec,
        specs: Mapping[str, ActionSpec],
        receipts: Mapping[str, StepReceipt],
        deadline: float,
        execution_deadline: float,
        bounded_by_plan_deadline: bool,
        message: discord.Message,
        request: AIRequest,
        cancellation_state: _CancellationState,
    ) -> StepReceipt:
        context = await self._current_context(plan, step, spec, message, request)
        if context is None:
            return await self._step_failed(plan, step, "authorization_denied", deadline=deadline)
        if not await self._emit(PlanEventType.STEP_STARTED, plan, step, deadline=deadline):
            return await self._step_failed(plan, step, "plan_timeout", deadline=deadline)
        if asyncio.get_running_loop().time() >= execution_deadline:
            return await self._step_failed(
                plan,
                step,
                "plan_timeout" if bounded_by_plan_deadline else "timeout",
                deadline=deadline,
            )
        resolved_parameters, resolved_bindings, binding_failure = await self._resolve_parameters(
            plan,
            step,
            spec,
            specs,
            receipts,
            message,
            request,
        )
        if binding_failure is not None:
            return await self._step_failed(plan, step, binding_failure, deadline=deadline)
        retry_safe = (
            step.effect is ActionEffect.READ_ONLY
            and spec.planner_contract is not None
            and spec.planner_contract.retry_safe
        )
        max_attempts = 2 if retry_safe else 1
        result: Any = None
        for attempt in range(max_attempts):
            cancelled = await self._cancellation_receipt_if_requested(
                plan,
                step,
                cancellation_state,
                deadline=deadline,
            )
            if cancelled is not None:
                return cancelled
            if attempt == 0 and not await self._mark_durable_step_started(plan, step):
                return await self._claim_cancellation_receipt(
                    plan,
                    step,
                    cancellation_state,
                    failure_code="cancelled",
                    deadline=deadline,
                )
            context = await self._current_context(plan, step, spec, message, request)
            if context is None:
                return await self._step_failed(plan, step, "authorization_denied", deadline=deadline)
            binding_failure = self._bindings_still_current(plan, resolved_bindings, receipts, context, message)
            if binding_failure is not None:
                return await self._step_failed(plan, step, binding_failure, deadline=deadline)
            if resolved_bindings:
                base_context = context

                def bindings_are_current() -> bool:
                    return (
                        base_context.bindings_are_current()
                        and self._bindings_still_current(
                            plan,
                            resolved_bindings,
                            receipts,
                            base_context,
                            message,
                        )
                        is None
                    )

                context = replace(base_context, binding_guard=bindings_are_current)
            if asyncio.get_running_loop().time() >= execution_deadline:
                return await self._step_failed(
                    plan,
                    step,
                    "plan_timeout" if bounded_by_plan_deadline else "timeout",
                    deadline=deadline,
                )
            timeout_scope = asyncio.timeout_at(execution_deadline)
            try:
                async with timeout_scope:
                    result = await spec.executor(context, resolved_parameters)
            except TimeoutError:
                if timeout_scope.expired():
                    return await self._step_failed(
                        plan,
                        step,
                        "plan_timeout" if bounded_by_plan_deadline else "timeout",
                        deadline=deadline,
                    )
                return await self._step_failed(plan, step, "executor_error", deadline=deadline)
            except Exception:
                if attempt + 1 < max_attempts:
                    continue
                return await self._step_failed(plan, step, "executor_error", deadline=deadline)
            break
        if step.effect is ActionEffect.READ_ONLY:
            await self._cancellation_observed_after_execution(plan, cancellation_state)
        if not isinstance(result, ActionResult):
            return await self._step_failed(plan, step, "invalid_result", deadline=deadline)
        if result.status is not ActionStatus.COMPLETED:
            return await self._step_failed(
                plan,
                step,
                f"action_{result.status.value}",
                result.status,
                deadline=deadline,
            )
        output_schema = spec.planner_contract.output_artifact_schema if spec.planner_contract is not None else None
        if result.artifact is not None and output_schema is None:
            return await self._step_failed(
                plan,
                step,
                "artifact_undeclared",
                ActionStatus.COMPLETED,
                deadline=deadline,
            )
        if output_schema is not None:
            if result.artifact is None:
                return await self._step_failed(
                    plan,
                    step,
                    "artifact_missing",
                    ActionStatus.COMPLETED,
                    deadline=deadline,
                )
            expected_scope = ArtifactScope(
                plan.request_id,
                plan.guild_id,
                plan.channel_id,
                plan.user_id,
            )
            if result.artifact.scope_digest != expected_scope.digest:
                return await self._step_failed(
                    plan,
                    step,
                    "artifact_scope_mismatch",
                    ActionStatus.COMPLETED,
                    deadline=deadline,
                )
            if result.artifact.kind not in artifact_kinds_from_schema(output_schema, output=True):
                return await self._step_failed(
                    plan,
                    step,
                    "artifact_kind_mismatch",
                    ActionStatus.COMPLETED,
                    deadline=deadline,
                )
        public_text = result.text if spec.planner_contract is not None and not _contract_uses_artifact(spec) else None
        if not await self._emit(PlanEventType.STEP_COMPLETED, plan, step, deadline=deadline):
            receipt = StepReceipt(
                step.step_id,
                step.action_id,
                StepStatus.FAILED,
                ActionStatus.COMPLETED,
                "plan_timeout_after_completion",
            )
            await self._checkpoint_durable_step(
                plan,
                step,
                receipt,
                public_text=public_text,
                artifact=result.artifact,
            )
            return receipt
        receipt = StepReceipt(step.step_id, step.action_id, StepStatus.COMPLETED, result.status)
        await self._checkpoint_durable_step(
            plan,
            step,
            receipt,
            public_text=public_text,
            artifact=result.artifact,
        )
        if result.artifact is not None:
            artifacts = self._request_artifacts.get()
            if artifacts is not None:
                artifacts[step.step_id] = result.artifact
        if public_text is not None:
            outputs = self._request_outputs.get()
            if outputs is not None:
                outputs[step.step_id] = public_text
        return receipt

    async def _resolve_parameters(
        self,
        plan: OrchestrationPlan,
        step: OrchestrationStep,
        spec: ActionSpec,
        specs: Mapping[str, ActionSpec],
        receipts: Mapping[str, StepReceipt],
        message: discord.Message,
        request: AIRequest,
    ) -> tuple[Mapping[str, Any], tuple[_ResolvedArtifactBinding, ...], str | None]:
        resolved: dict[str, Any] = {}
        artifacts = self._request_artifacts.get() or {}
        contract = spec.planner_contract
        properties = contract.input_schema.get("properties") if contract is not None else None
        expected_scope = ArtifactScope(
            plan.request_id,
            plan.guild_id,
            plan.channel_id,
            plan.user_id,
        )
        steps_by_id = {item.step_id: item for item in plan.steps}
        bindings: list[_ResolvedArtifactBinding] = []
        for name, value in step.parameters.items():
            if not isinstance(value, (ArtifactFromStep, ArtifactsFromSteps)):
                resolved[name] = value
                continue
            rule = properties.get(name) if isinstance(properties, Mapping) else None
            expected_type = "artifact_ref" if isinstance(value, ArtifactFromStep) else "artifact_ref_list"
            if not isinstance(rule, Mapping) or rule.get("type") != expected_type:
                return MappingProxyType({}), (), "artifact_binding_invalid"
            accepted_kinds = artifact_kinds_from_schema(rule)
            source_ids = _artifact_source_ids(value)
            if isinstance(value, ArtifactsFromSteps):
                minimum, maximum = artifact_list_bounds_from_schema(rule)
                if not minimum <= len(source_ids) <= maximum:
                    return MappingProxyType({}), (), "artifact_binding_invalid"
            resolved_artifacts: list[ArtifactRef] = []
            for source_id in source_ids:
                source_step = steps_by_id.get(source_id)
                source_spec = specs.get(source_id)
                source_receipt = receipts.get(source_id)
                if (
                    source_step is None
                    or source_spec is None
                    or source_id not in step.depends_on
                    or source_receipt is None
                    or source_receipt.step_id != source_id
                    or source_receipt.action_id != source_step.action_id
                    or source_receipt.status is not StepStatus.COMPLETED
                    or source_receipt.action_status is not ActionStatus.COMPLETED
                ):
                    return MappingProxyType({}), (), "artifact_producer_incomplete"
                artifact = artifacts.get(source_id)
                if artifact is None:
                    return MappingProxyType({}), (), "artifact_missing"
                if artifact.scope_digest != expected_scope.digest:
                    return MappingProxyType({}), (), "artifact_scope_mismatch"
                if artifact.kind not in accepted_kinds:
                    return MappingProxyType({}), (), "artifact_kind_mismatch"
                resolved_artifacts.append(artifact)
                bindings.append(
                    _ResolvedArtifactBinding(
                        source_step,
                        source_spec,
                        source_receipt,
                        artifact,
                        accepted_kinds,
                    )
                )
            resolved[name] = resolved_artifacts[0] if isinstance(value, ArtifactFromStep) else tuple(resolved_artifacts)
        if bindings:
            contexts = await asyncio.gather(
                *(
                    self._current_context(plan, binding.source_step, binding.source_spec, message, request)
                    for binding in bindings
                )
            )
            if any(context is None for context in contexts):
                return MappingProxyType({}), (), "authorization_denied"
            for binding in bindings:
                try:
                    if self.registry.get(binding.source_step.action_id) is not binding.source_spec:
                        return MappingProxyType({}), (), "authorization_denied"
                except KeyError:
                    return MappingProxyType({}), (), "authorization_denied"
        return MappingProxyType(resolved), tuple(bindings), None

    def _bindings_still_current(
        self,
        plan: OrchestrationPlan,
        bindings: tuple[_ResolvedArtifactBinding, ...],
        receipts: Mapping[str, StepReceipt],
        consumer_context: Any,
        message: discord.Message,
    ) -> str | None:
        expected_scope = ArtifactScope(
            plan.request_id,
            plan.guild_id,
            plan.channel_id,
            plan.user_id,
        )
        actual_scope = (
            getattr(getattr(message, "guild", None), "id", None),
            getattr(getattr(message, "channel", None), "id", None),
            getattr(getattr(message, "author", None), "id", None),
        )
        artifacts = self._request_artifacts.get() or {}
        approval_boundary = self._port_approval_boundary.get()
        if (
            self.router.closing
            or bool(getattr(self.router.bot, "is_closing", False))
            or self.router.registry is not self.registry
            or (
                approval_boundary is not None
                and (self.router is not approval_boundary[0] or self.registry is not approval_boundary[1])
            )
            or actual_scope != (plan.guild_id, plan.channel_id, plan.user_id)
        ):
            return "authorization_denied"
        for binding in bindings:
            try:
                if self.registry.get(binding.source_step.action_id) is not binding.source_spec:
                    return "authorization_denied"
            except KeyError:
                return "authorization_denied"
            if (
                receipts.get(binding.source_step.step_id) is not binding.source_receipt
                or binding.source_receipt.status is not StepStatus.COMPLETED
                or binding.source_receipt.action_status is not ActionStatus.COMPLETED
                or binding.source_receipt.action_id != binding.source_step.action_id
            ):
                return "artifact_producer_incomplete"
            if not self.router._currently_allowed(
                binding.source_spec, consumer_context
            ) or not self.router._mention_currently_allowed(consumer_context):
                return "authorization_denied"
            if artifacts.get(binding.source_step.step_id) is not binding.artifact:
                return "artifact_missing"
            if binding.artifact.scope_digest != expected_scope.digest:
                return "artifact_scope_mismatch"
            if binding.artifact.kind not in binding.accepted_kinds:
                return "artifact_kind_mismatch"
        return None

    async def _current_context(
        self,
        plan: OrchestrationPlan,
        step: OrchestrationStep,
        spec: ActionSpec,
        message: discord.Message,
        request: AIRequest,
    ) -> Any | None:
        approval_boundary = self._port_approval_boundary.get()
        if (
            self.router.closing
            or bool(getattr(self.router.bot, "is_closing", False))
            or self.router.registry is not self.registry
            or (
                approval_boundary is not None
                and (self.router is not approval_boundary[0] or self.registry is not approval_boundary[1])
            )
        ):
            return None
        try:
            if self.registry.get(step.action_id) is not spec:
                return None
        except KeyError:
            return None
        if (
            plan.guild_id != getattr(getattr(message, "guild", None), "id", None)
            or plan.channel_id != getattr(getattr(message, "channel", None), "id", None)
            or plan.user_id != getattr(getattr(message, "author", None), "id", None)
        ):
            return None
        context = await self.router.fresh_context_for_spec(spec, message, request)
        if (
            context is None
            or self.router.closing
            or bool(getattr(self.router.bot, "is_closing", False))
            or self.router.registry is not self.registry
            or (
                approval_boundary is not None
                and (self.router is not approval_boundary[0] or self.registry is not approval_boundary[1])
            )
        ):
            return None
        try:
            if self.registry.get(step.action_id) is not spec:
                return None
        except KeyError:
            return None
        if not self.router._currently_allowed(spec, context) or not self.router._mention_currently_allowed(context):
            return None
        return replace(
            context,
            artifact_scope=ArtifactScope(
                plan.request_id,
                plan.guild_id,
                plan.channel_id,
                plan.user_id,
            ),
        )

    async def _step_failed(
        self,
        plan: OrchestrationPlan,
        step: OrchestrationStep,
        code: str,
        action_status: ActionStatus | None = None,
        *,
        deadline: float,
    ) -> StepReceipt:
        await self._emit(PlanEventType.STEP_FAILED, plan, step, deadline=deadline)
        receipt = StepReceipt(step.step_id, step.action_id, StepStatus.FAILED, action_status, code)
        await self._checkpoint_durable_step(plan, step, receipt)
        return receipt

    async def _emit(
        self,
        event_type: PlanEventType,
        plan: OrchestrationPlan,
        step: OrchestrationStep | None = None,
        *,
        deadline: float,
    ) -> bool:
        observer = self._request_observer.get() or self.observer
        if observer is None:
            return True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        event = PlanEvent(
            event_type,
            plan.digest,
            plan.idempotency_key,
            step.step_id if step is not None else None,
            step.action_id if step is not None else None,
        )
        task: asyncio.Future[Any] | None = None
        try:
            result = observer(event)
            if result is None:
                return True
            try:
                task = asyncio.ensure_future(result)
            except (TypeError, ValueError):
                return True
            timeout_scope = asyncio.timeout_at(deadline)
            try:
                async with timeout_scope:
                    await asyncio.shield(task)
            except TimeoutError:
                if timeout_scope.expired():
                    _cancel_and_consume(task)
                    return False
                return True
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                if task is not None:
                    _cancel_and_consume(task)
                raise
            return True
        except Exception:
            return True
        return True


def _cancel_and_consume(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()

    def consume(done: asyncio.Future[Any]) -> None:
        if done.cancelled():
            return
        try:
            done.exception()
        except (Exception, asyncio.CancelledError):
            return

    task.add_done_callback(consume)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _contract_uses_artifact(spec: ActionSpec) -> bool:
    contract = spec.planner_contract
    if contract is None:
        return False
    if contract.output_artifact_schema is not None:
        return True
    properties = contract.input_schema.get("properties")
    return isinstance(properties, Mapping) and any(
        isinstance(rule, Mapping) and rule.get("type") in {"artifact_ref", "artifact_ref_list"}
        for rule in properties.values()
    )


def _parameters(
    value: Mapping[str, str | ArtifactFromStep | ArtifactsFromSteps],
) -> dict[str, str | ArtifactFromStep | ArtifactsFromSteps]:
    if not isinstance(value, Mapping):
        raise TypeError("parameters must be a mapping")
    if len(value) > _MAX_PARAMETERS:
        raise ValueError("parameters contain too many values")
    result: dict[str, str | ArtifactFromStep | ArtifactsFromSteps] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, (str, ArtifactFromStep, ArtifactsFromSteps)):
            raise TypeError("parameter values must be strings or typed artifact step references")
        if (
            not key
            or key != key.strip()
            or len(key) > _MAX_PARAMETER_KEY_CHARS
            or (isinstance(item, str) and len(item) > _MAX_PARAMETER_VALUE_CHARS)
        ):
            raise ValueError("parameter key or value is outside the allowed range")
        if isinstance(item, (ArtifactFromStep, ArtifactsFromSteps)):
            result[key] = item
            continue
        inspected = unicodedata.normalize("NFKC", item).replace("\u00a5", "\\").replace("\u2216", "\\")
        path_parts = re.split(r"[\\/]", inspected)
        if (
            any(ord(character) < 32 and character not in "\t\n\r" for character in inspected)
            or inspected.startswith(("/", "\\"))
            or _WINDOWS_PATH_RE.match(inspected)
            or ".." in path_parts
            or any(character in inspected for character in _GLOB_CHARACTERS)
        ):
            raise ValueError("parameter contains a forbidden host path or glob")
        result[key] = item
    encoded = json.dumps(
        {key: _parameter_json(item) for key, item in result.items()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_PARAMETER_BYTES:
        raise ValueError("parameters exceed the UTF-8 byte limit")
    return result


def artifact_source_step_ids(parameters: Mapping[str, object]) -> tuple[str, ...]:
    """typed artifact inputだけから消費元step IDをmapping順に列挙する。"""

    if not isinstance(parameters, Mapping):
        raise TypeError("parameters must be a mapping")
    source_ids: list[str] = []
    for value in parameters.values():
        if isinstance(value, ArtifactFromStep):
            source_ids.append(value.step_id)
        elif isinstance(value, ArtifactsFromSteps):
            source_ids.extend(value.step_ids)
    return tuple(source_ids)


def _artifact_source_ids(value: ArtifactFromStep | ArtifactsFromSteps) -> tuple[str, ...]:
    source_ids = artifact_source_step_ids({"artifact": value})
    if not source_ids:
        raise TypeError("artifact binding must be a typed step reference")
    return source_ids


def _parameter_json(value: str | ArtifactFromStep | ArtifactsFromSteps) -> Any:
    if isinstance(value, ArtifactFromStep):
        return {"artifact_from_step": value.step_id}
    if isinstance(value, ArtifactsFromSteps):
        return {"artifacts_from_steps": list(value.step_ids)}
    return value
