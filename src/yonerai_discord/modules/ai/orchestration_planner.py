"""ActionRegistryの公開契約だけをtyped planへ変換するbounded planner境界。"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from yonerai_discord.ai_control.routing import (
    ModelDecision,
    ModelName,
    RiskLevel,
    TaskComplexity,
    TaskKind,
    TaskProfile,
    route_model,
)

from .action_router import (
    ActionEffect,
    ActionMode,
    ActionRegistry,
    ActionSpec,
    PlannerActionContract,
    artifact_kinds_from_schema,
    artifact_list_bounds_from_schema,
)
from .models import DataBoundary
from .orchestration import (
    ArtifactsFromSteps,
    ArtifactFromStep,
    OrchestrationPlan,
    OrchestrationStep,
    artifact_source_step_ids,
)


_MAX_CANDIDATES = 8
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 8
_MAX_JSON_STRING = 2_000
_MAX_STEPS = 20
_MAX_REPETITION_GROUPS = 8
_TOKEN_RE = re.compile(r"[0-9a-zA-Zぁ-んァ-ヶ一-龠ー]{2,}")
_EXPLICIT_REPETITION_RE = re.compile(r"(?<![0-9])(?P<count>[1-9][0-9]{0,2})\s*(?:回|かい|times?(?![a-z0-9_]))")
_AMBIGUOUS_REPETITION_RE = re.compile(r"(?:複数回|繰り返)")
_REPETITION_GROUP_SEPARATOR_RE = re.compile(
    r"(?:そのあと|それから|次に|続けて|"
    r"(?<!\w)after\s+that(?!\w)|(?<!\w)then(?!\w)|(?<!\w)next(?!\w))"
)
_SPAN_EDGE_CHARS = " \t\r\n,、。.!?！？;；:"
_ACTION_REQUEST_END_RE = re.compile(
    r"(?:して|やって|作って|つくって|選んで|振って|振る|流して|ながして|再生して|かけて|見せて|"
    r"撮って|並べて|つないで|繋げて|変えて|実行して)(?:ください|くれる|ほしい)?(?:[。.!！])?\Z"
)
_NOMINAL_ACTION_REQUEST_RE = re.compile(
    r"(?:[^。.!！?？\r\n]{1,160}を[^。.!！?？\r\n]{1,80}に|"
    r"[^。.!！?？\r\n]{0,160}(?:作成|公開|構築|デプロイ|追加|削除|変更|修正|更新|置換|お願い))"
    r"(?:[。.!！])?\Z"
)
_ENGLISH_ACTION_REQUEST_RE = re.compile(
    r"(?:please\s+)?(?:roll|play|create|make|generate|run|execute|show|search|list|check|hash|encode|compose|"
    r"add|edit|change|update|replace|remove|delete|build|publish|deploy)\b"
)
_EXPLANATION_CUE_RE = re.compile(
    r"(?:"
    r"(?:説明して|教えて)(?:ください)?"
    r"|とは|の意味|の仕組み"
    r"|なぜ|どうして|どうなる|どうなりますか"
    r"|what happens|what would happen"
    r")(?:[。.!！])?\Z"
)
_ENGLISH_COMPARISON_CUE_RE = re.compile(r"\A(?:please\s+)?[a-z][a-z0-9_-]*\s+(?:versus|vs\.?)(?:\s|\Z)")
_TRANSLATION_CUE_RE = re.compile(r"(?:翻訳|英語に|日本語に|訳して|translate\b|to\s+(?:japanese|english)\b)")
_QUOTED_OR_INLINE_CODE_RE = re.compile(r"(?:`[^`\r\n]*`|「[^」\r\n]*」|『[^』\r\n]*』|\"[^\"\r\n]*\"|'[^'\r\n]*')")


def _instruction_is_question_or_explanation(instruction: str) -> bool:
    normalized = _normalized_instruction(instruction)
    return (
        "?" in normalized
        or "？" in normalized
        or _EXPLANATION_CUE_RE.search(normalized) is not None
        or _ENGLISH_COMPARISON_CUE_RE.search(normalized) is not None
    )


def _instruction_requests_actions(instruction: str) -> bool:
    normalized = _normalized_instruction(instruction)
    quoted_or_code = _QUOTED_OR_INLINE_CODE_RE.search(normalized) is not None
    actionable = _actionable_instruction(normalized)
    return (
        not _instruction_is_question_or_explanation(normalized)
        and bool(actionable)
        and not (quoted_or_code and _TRANSLATION_CUE_RE.search(actionable) is not None)
        and not (
            _AMBIGUOUS_REPETITION_RE.search(actionable) is not None
            and _EXPLICIT_REPETITION_RE.search(actionable) is None
        )
        and (
            _ACTION_REQUEST_END_RE.search(actionable) is not None
            or _NOMINAL_ACTION_REQUEST_RE.fullmatch(actionable) is not None
            or _ENGLISH_ACTION_REQUEST_RE.match(actionable) is not None
        )
    )


def _actionable_instruction(instruction: str) -> str:
    normalized = _normalized_instruction(instruction)
    return _QUOTED_OR_INLINE_CODE_RE.sub(" ", normalized).strip()


class PlannerError(RuntimeError):
    """planner境界の固定エラー。入力本文を例外へ含めない。"""


class PlannerUnavailableError(PlannerError):
    """planner portまたは安全な候補が利用できない。"""


class PlannerValidationError(PlannerError):
    """model JSONをplanへ変換できない。"""


class PlannerOutputBindingUnavailableError(PlannerValidationError):
    """このbatchで未実装のstep出力bindingを要求した。"""


@dataclass(frozen=True, slots=True)
class PlannerFacts:
    request_id: str
    guild_id: int
    channel_id: int
    user_id: int
    idempotency_key: str
    complexity: TaskComplexity = TaskComplexity.STANDARD
    compound: bool = False
    minimum_candidate_count: int = 1
    required_candidate_action_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("request_id", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
                raise ValueError(f"{name} is invalid")
        for name in ("guild_id", "channel_id", "user_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.complexity, TaskComplexity):
            raise TypeError("complexity must be a TaskComplexity")
        if type(self.compound) is not bool:
            raise TypeError("compound must be a boolean")
        if (
            isinstance(self.minimum_candidate_count, bool)
            or not isinstance(self.minimum_candidate_count, int)
            or not 1 <= self.minimum_candidate_count <= _MAX_CANDIDATES
        ):
            raise ValueError("minimum_candidate_count is outside the bounded contract")
        required_ids = self.required_candidate_action_ids
        if (
            not isinstance(required_ids, tuple)
            or len(required_ids) > _MAX_CANDIDATES
            or any(type(action_id) is not str or not action_id for action_id in required_ids)
            or len(required_ids) != len(set(required_ids))
            or (required_ids and len(required_ids) != self.minimum_candidate_count)
        ):
            raise ValueError("required_candidate_action_ids are outside the bounded contract")


@dataclass(frozen=True, slots=True)
class PlannerActionCandidate:
    action_id: str
    description: str
    input_schema: Mapping[str, Any] = field(repr=False)
    tags: tuple[str, ...]
    intent_hints: tuple[str, ...]
    risk: RiskLevel
    effect: ActionEffect
    output_artifact_schema: Mapping[str, Any] | None = field(default=None, repr=False)
    grounded_parameters: tuple[str, ...] = ()

    def to_mapping(self) -> Mapping[str, Any]:
        result = {
            "action_id": self.action_id,
            "description": self.description,
            "input_schema": _plain_json(self.input_schema),
            "tags": list(self.tags),
            "intent_hints": list(self.intent_hints),
            "risk": self.risk.value,
            "effect": self.effect.value,
        }
        if self.output_artifact_schema is not None:
            result["output_artifact_schema"] = _plain_json(self.output_artifact_schema)
        return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class PlannerRepetitionGroup:
    action_id: str
    count: int
    source_start: int
    source_end: int
    source_span: str = field(repr=False)
    expected_parameters: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id:
            raise ValueError("repetition group action_id is invalid")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or not 1 <= self.count <= _MAX_STEPS:
            raise ValueError("repetition group count is outside the bounded contract")
        if (
            isinstance(self.source_start, bool)
            or isinstance(self.source_end, bool)
            or not isinstance(self.source_start, int)
            or not isinstance(self.source_end, int)
            or not 0 <= self.source_start < self.source_end
        ):
            raise ValueError("repetition group source range is invalid")
        if (
            not isinstance(self.source_span, str)
            or not self.source_span
            or len(self.source_span) > _MAX_JSON_STRING
            or _normalized_instruction(self.source_span) != self.source_span
        ):
            raise ValueError("repetition group source span is invalid")
        expected_parameters = self.expected_parameters
        if (
            not isinstance(expected_parameters, tuple)
            or len(expected_parameters) > 8
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or type(item[0]) is not str
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", item[0]) is None
                or type(item[1]) is not str
                or not item[1]
                or len(item[1]) > _MAX_JSON_STRING
                or _normalized_instruction(item[1]) != item[1]
                for item in expected_parameters
            )
            or tuple(sorted(expected_parameters)) != expected_parameters
            or len({name for name, _value in expected_parameters}) != len(expected_parameters)
        ):
            raise ValueError("repetition group expected parameters are invalid")

    def to_mapping(self) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "action_id": self.action_id,
            "count": self.count,
            "source_span": self.source_span,
        }
        if self.expected_parameters:
            result["expected_parameters"] = dict(self.expected_parameters)
        return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class PlannerModelRequest:
    instruction: str = field(repr=False)
    candidates: tuple[PlannerActionCandidate, ...]
    model_decision: ModelDecision
    response_schema: Mapping[str, Any] = field(repr=False)
    repetition_groups: tuple[PlannerRepetitionGroup, ...] = field(default=(), repr=False)
    allowed_tools: tuple[str, ...] = ()
    max_tool_calls: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise ValueError("planner instruction must not be empty")
        if not self.candidates or len(self.candidates) > _MAX_CANDIDATES:
            raise ValueError("planner candidate count is outside the bounded contract")
        if not isinstance(self.model_decision, ModelDecision):
            raise TypeError("model_decision must be a ModelDecision")
        if self.model_decision.model is ModelName.LUNA:
            raise ValueError("Luna cannot be used for plan generation")
        groups = self.repetition_groups
        if (
            not isinstance(groups, tuple)
            or (groups and not 1 <= len(groups) <= _MAX_REPETITION_GROUPS)
            or any(not isinstance(group, PlannerRepetitionGroup) for group in groups)
            or sum(group.count for group in groups) > _MAX_STEPS
        ):
            raise ValueError("planner repetition groups are outside the bounded contract")
        candidate_ids = frozenset(candidate.action_id for candidate in self.candidates)
        candidates_by_id = {candidate.action_id: candidate for candidate in self.candidates}
        previous_end = -1
        for group in groups:
            candidate = candidates_by_id.get(group.action_id)
            if (
                group.action_id not in candidate_ids
                or group.source_start < previous_end
                or group.source_end > len(self.instruction)
                or self.instruction[group.source_start : group.source_end] != group.source_span
                or candidate is None
                or not set(dict(group.expected_parameters)).issubset(candidate.grounded_parameters)
            ):
                raise ValueError("planner repetition group is not bound to the instruction")
            previous_end = group.source_end
        if self.allowed_tools or self.max_tool_calls != 0:
            raise ValueError("planner model tools must remain empty")


@dataclass(frozen=True, slots=True)
class PlannerDispatchContext:
    """Providerへserializeしない、呼出し時だけのscopeとfresh認可。"""

    guild_id: int = field(repr=False)
    channel_id: int = field(repr=False)
    user_id: int = field(repr=False)
    boundary: DataBoundary
    provider_call_allowed: Callable[[], bool | Awaitable[bool]] = field(repr=False)
    candidate_action_authorizer: Callable[
        [tuple[str, ...]],
        Collection[str] | Awaitable[Collection[str]],
    ] = field(repr=False)

    def __post_init__(self) -> None:
        for name in ("guild_id", "channel_id", "user_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.boundary, DataBoundary):
            raise TypeError("boundary must be a DataBoundary")
        if not callable(self.provider_call_allowed):
            raise TypeError("provider_call_allowed must be callable")
        if not callable(self.candidate_action_authorizer):
            raise TypeError("candidate_action_authorizer must be callable")

    async def authorize_candidate_action_ids(
        self,
        requested_action_ids: tuple[str, ...],
    ) -> frozenset[str]:
        if (
            not isinstance(requested_action_ids, tuple)
            or not requested_action_ids
            or any(type(action_id) is not str or not action_id for action_id in requested_action_ids)
            or len(requested_action_ids) != len(set(requested_action_ids))
        ):
            raise TypeError("requested planner candidate IDs are invalid")
        try:
            result = self.candidate_action_authorizer(requested_action_ids)
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PlannerUnavailableError("planner candidate authorization failed") from exc
        if isinstance(result, (str, bytes, bytearray, Mapping)) or not isinstance(result, Collection):
            raise PlannerUnavailableError("planner candidate authorization returned an invalid subset")
        values = tuple(result)
        requested = frozenset(requested_action_ids)
        if (
            any(type(action_id) is not str or not action_id for action_id in values)
            or len(values) != len(set(values))
            or not frozenset(values).issubset(requested)
        ):
            raise PlannerUnavailableError("planner candidate authorization returned an invalid subset")
        return frozenset(values)


class PlannerModelPort(Protocol):
    async def generate(
        self,
        request: PlannerModelRequest,
        dispatch: PlannerDispatchContext,
    ) -> str | bytes: ...


@dataclass(frozen=True, slots=True)
class PreparedPlanPort:
    plan: OrchestrationPlan
    model_decision: ModelDecision

    async def load_plan(self) -> OrchestrationPlan:
        return self.plan


@dataclass(frozen=True, slots=True)
class _RepetitionSpan:
    count: int
    start: int
    end: int
    text: str = field(repr=False)


class OrchestrationPlanner:
    """決定論的候補抽出とstrict JSON decodeだけを所有する。"""

    def __init__(
        self,
        registry: ActionRegistry,
        model_port: PlannerModelPort | None,
        *,
        max_candidates: int = _MAX_CANDIDATES,
    ) -> None:
        if not isinstance(registry, ActionRegistry):
            raise TypeError("registry must be an ActionRegistry")
        if model_port is not None and not callable(getattr(model_port, "generate", None)):
            raise TypeError("model_port must implement generate")
        if isinstance(max_candidates, bool) or not 1 <= max_candidates <= _MAX_CANDIDATES:
            raise ValueError("max_candidates is outside the bounded contract")
        self.registry = registry
        self.model_port = model_port
        self.max_candidates = max_candidates

    def candidates_for(self, instruction: str) -> tuple[PlannerActionCandidate, ...]:
        return self._ranked_candidates_for(instruction)[: self.max_candidates]

    def _ranked_candidates_for(self, instruction: str) -> tuple[PlannerActionCandidate, ...]:
        normalized = _normalized_instruction(instruction)
        tokens = frozenset(_TOKEN_RE.findall(normalized))
        ranked: list[tuple[int, str, ActionSpec, PlannerActionContract]] = []
        for spec in self.registry.specs:
            contract = spec.planner_contract
            if contract is None or spec.mode is not ActionMode.EXECUTE:
                continue
            score = _candidate_score(normalized, tokens, spec, contract)
            if score > 0:
                ranked.append((score, spec.action_id, spec, contract))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            PlannerActionCandidate(
                spec.action_id,
                contract.description,
                contract.input_schema,
                contract.tags,
                contract.intent_hints,
                contract.risk,
                spec.effect,
                contract.output_artifact_schema,
                contract.grounded_parameters,
            )
            for _, _, spec, contract in ranked
        )

    def candidate_requirement(
        self,
        instruction: str,
        *,
        caller_compound: bool = False,
        repetition_requested: bool = False,
        auto_compound: bool = False,
    ) -> int:
        """依頼をplannerへ渡すために必要なfresh認可済み候補数を返す。"""

        if any(type(value) is not bool for value in (caller_compound, repetition_requested, auto_compound)):
            raise TypeError("planner compound flags must be booleans")
        count = len(self.candidates_for(instruction))
        if repetition_requested:
            return 1 if count >= 1 else 0
        if caller_compound:
            return 1 if count >= 1 else 0
        explicit_ids = self.explicit_action_ids_for(instruction)
        if auto_compound and len(explicit_ids) >= 2:
            return len(explicit_ids)
        return 0

    def explicit_action_ids_for(self, instruction: str) -> tuple[str, ...]:
        """共有generic hintではなく、Action固有hintが本文にある候補だけを返す。"""

        normalized = _normalized_instruction(instruction)
        owners: dict[str, set[str]] = {}
        ranked = self._ranked_candidates_for(instruction)
        ranked_ids = {candidate.action_id for candidate in ranked}
        for spec in self.registry.specs:
            contract = spec.planner_contract
            if contract is None or spec.mode is not ActionMode.EXECUTE:
                continue
            for hint in (*contract.tags, *contract.intent_hints):
                if hint in normalized:
                    owners.setdefault(hint, set()).add(spec.action_id)
        explicit_ids = {
            action_id
            for hint, action_ids in owners.items()
            if len(action_ids) == 1
            for action_id in action_ids
            if action_id in ranked_ids
        }
        return tuple(candidate.action_id for candidate in ranked if candidate.action_id in explicit_ids)

    @staticmethod
    def instruction_requests_actions(instruction: str) -> bool:
        """機能固有語ではなく、依頼形と説明形の差だけを決定論的に判定する。"""

        return _instruction_requests_actions(instruction)

    @staticmethod
    def actionable_instruction(instruction: str) -> str:
        """引用・inline code内を命令候補から除いたNFKC済み本文を返す。"""

        return _actionable_instruction(instruction)

    @staticmethod
    def instruction_is_question_or_explanation(instruction: str) -> bool:
        """機能候補や差し替えplannerに依存せず、質問・説明形をfail-closed判定する。"""

        return _instruction_is_question_or_explanation(instruction)

    def can_attempt(
        self,
        instruction: str,
        *,
        compound: bool = False,
        repetition_requested: bool = False,
        auto_compound: bool = False,
    ) -> bool:
        """caller分類または複数の実行候補からplanner使用可否を決める。"""

        return (
            self.candidate_requirement(
                instruction,
                caller_compound=compound,
                repetition_requested=repetition_requested,
                auto_compound=auto_compound,
            )
            > 0
        )

    async def prepare(
        self,
        instruction: str,
        facts: PlannerFacts,
        dispatch: PlannerDispatchContext,
    ) -> PreparedPlanPort:
        if self.model_port is None:
            raise PlannerUnavailableError("planner model port is unavailable")
        if not isinstance(dispatch, PlannerDispatchContext):
            raise TypeError("dispatch must be a PlannerDispatchContext")
        if (
            dispatch.guild_id != facts.guild_id
            or dispatch.channel_id != facts.channel_id
            or dispatch.user_id != facts.user_id
        ):
            raise PlannerValidationError("planner dispatch scope does not match caller facts")
        normalized_instruction = _normalized_instruction(instruction)
        explicit_repetition_total = _explicit_repetition_total(normalized_instruction)
        repetition_spans = _explicit_repetition_spans(normalized_instruction)
        if explicit_repetition_total == 0:
            repetition_spans = _ordered_action_spans(normalized_instruction)
        if explicit_repetition_total == 0 and _AMBIGUOUS_REPETITION_RE.search(normalized_instruction) is not None:
            raise PlannerValidationError("explicit repetition count is required")
        if explicit_repetition_total > _MAX_STEPS:
            raise PlannerValidationError("explicit repetition exceeds the step limit")
        ranked_candidates = self._ranked_candidates_for(normalized_instruction)
        if not facts.compound or not ranked_candidates:
            raise PlannerUnavailableError("bounded planner candidates are insufficient")
        repetition_groups = self._resolve_repetition_groups(repetition_spans, ranked_candidates)
        if repetition_groups:
            group_action_ids = frozenset(group.action_id for group in repetition_groups)
            ranked_candidates = tuple(
                candidate for candidate in ranked_candidates if candidate.action_id in group_action_ids
            )
        if facts.required_candidate_action_ids:
            required_ids = frozenset(facts.required_candidate_action_ids)
            ranked_candidates = tuple(
                candidate for candidate in ranked_candidates if candidate.action_id in required_ids
            )
            if {candidate.action_id for candidate in ranked_candidates} != required_ids:
                raise PlannerUnavailableError("required planner candidates are unavailable")
        authorized_ids = await dispatch.authorize_candidate_action_ids(
            tuple(candidate.action_id for candidate in ranked_candidates)
        )
        candidates = tuple(candidate for candidate in ranked_candidates if candidate.action_id in authorized_ids)[
            : self.max_candidates
        ]
        if len(candidates) < facts.minimum_candidate_count:
            raise PlannerUnavailableError("bounded planner candidates are insufficient")
        if repetition_groups and {candidate.action_id for candidate in candidates} != {
            group.action_id for group in repetition_groups
        }:
            raise PlannerUnavailableError("repetition group candidates are unavailable")
        if explicit_repetition_total and not repetition_groups and len(candidates) != 1:
            raise PlannerValidationError("explicit repetition with multiple actions is unsupported")
        decision = _planner_model_decision(facts.complexity, candidates)
        request = PlannerModelRequest(
            instruction=normalized_instruction,
            candidates=candidates,
            model_decision=decision,
            response_schema=_response_schema(candidates),
            repetition_groups=repetition_groups,
        )
        try:
            raw = await self.model_port.generate(request, dispatch)
        except asyncio.CancelledError:
            raise
        except PlannerError:
            raise
        except Exception as exc:
            raise PlannerUnavailableError("planner model port failed") from exc
        document = _decode_document(raw)
        plan = self._build_plan(
            document,
            candidates,
            facts,
            instruction=request.instruction,
            explicit_repetition_total=explicit_repetition_total,
            repetition_groups=repetition_groups,
        )
        return PreparedPlanPort(plan, decision)

    def _resolve_repetition_groups(
        self,
        spans: tuple[_RepetitionSpan, ...],
        ranked_candidates: tuple[PlannerActionCandidate, ...],
    ) -> tuple[PlannerRepetitionGroup, ...]:
        if not spans:
            return ()
        allowed_ids = frozenset(candidate.action_id for candidate in ranked_candidates)
        groups: list[PlannerRepetitionGroup] = []
        for span in spans:
            group_candidates = tuple(
                candidate for candidate in self._ranked_candidates_for(span.text) if candidate.action_id in allowed_ids
            )
            if len(group_candidates) != 1:
                if len(spans) == 1 and len(group_candidates) > 1:
                    raise PlannerValidationError("explicit repetition with multiple actions is unsupported")
                raise PlannerValidationError("repetition group action is ambiguous")
            candidate = group_candidates[0]
            contract = self.registry.get(candidate.action_id).planner_contract
            if contract is None:
                raise PlannerValidationError("repetition group action contract is unavailable")
            expected_parameters: tuple[tuple[str, str], ...] = ()
            extractor = contract.repetition_parameter_extractor
            if extractor is not None:
                try:
                    extracted = extractor(span.text)
                except Exception as exc:
                    raise PlannerValidationError("repetition group parameters could not be extracted") from exc
                if not isinstance(extracted, Mapping) or set(extracted) != set(contract.grounded_parameters):
                    raise PlannerValidationError("repetition group parameters could not be extracted")
                normalized_parameters: list[tuple[str, str]] = []
                for name, value in extracted.items():
                    if type(name) is not str or type(value) is not str:
                        raise PlannerValidationError("repetition group parameters could not be extracted")
                    normalized_value = _normalized_instruction(value)
                    if not normalized_value or len(normalized_value) > _MAX_JSON_STRING:
                        raise PlannerValidationError("repetition group parameters could not be extracted")
                    normalized_parameters.append((name, normalized_value))
                expected_parameters = tuple(sorted(normalized_parameters))
            groups.append(
                PlannerRepetitionGroup(
                    candidate.action_id,
                    span.count,
                    span.start,
                    span.end,
                    span.text,
                    expected_parameters,
                )
            )
        return tuple(groups)

    def _build_plan(
        self,
        document: Mapping[str, Any],
        candidates: tuple[PlannerActionCandidate, ...],
        facts: PlannerFacts,
        *,
        instruction: str,
        explicit_repetition_total: int = 0,
        repetition_groups: tuple[PlannerRepetitionGroup, ...] = (),
    ) -> OrchestrationPlan:
        if set(document) != {"steps"}:
            raise PlannerValidationError("planner response has unknown fields")
        raw_steps = document["steps"]
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= _MAX_STEPS:
            raise PlannerValidationError("planner step count is invalid")
        candidate_map = {candidate.action_id: candidate for candidate in candidates}
        steps: list[OrchestrationStep] = []
        for raw_step in raw_steps:
            if not isinstance(raw_step, Mapping) or set(raw_step) != {
                "step_id",
                "action_id",
                "parameters",
                "depends_on",
            }:
                raise PlannerValidationError("planner step has unknown fields")
            action_id = raw_step["action_id"]
            if not isinstance(action_id, str) or action_id not in candidate_map:
                raise PlannerValidationError("planner selected an unavailable action")
            candidate = candidate_map[action_id]
            raw_parameters = raw_step["parameters"]
            if _contains_forbidden_binding_syntax(raw_parameters):
                raise PlannerOutputBindingUnavailableError("step output binding is unavailable")
            parameters = _validate_parameters(raw_parameters, candidate.input_schema)
            for name in candidate.grounded_parameters:
                value = parameters.get(name)
                if (
                    not isinstance(value, str)
                    or not value
                    or unicodedata.normalize("NFKC", value).casefold() not in instruction
                ):
                    raise PlannerValidationError("planner parameter is not grounded in the instruction")
            dependencies = raw_step["depends_on"]
            if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
                raise PlannerValidationError("planner dependencies are invalid")
            try:
                steps.append(
                    OrchestrationStep(
                        step_id=raw_step["step_id"],
                        action_id=action_id,
                        parameters=parameters,
                        depends_on=tuple(dependencies),
                        effect=candidate.effect,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise PlannerValidationError("planner step failed bounded validation") from exc
        _validate_plan_graph_and_artifacts(steps, candidate_map)
        if repetition_groups:
            _validate_repetition_group_plan(steps, candidate_map, repetition_groups)
        elif explicit_repetition_total and (
            len(steps) != explicit_repetition_total or any(step.action_id != candidates[0].action_id for step in steps)
        ):
            raise PlannerValidationError("planner did not preserve the explicit repetition count")
        return OrchestrationPlan(
            request_id=facts.request_id,
            guild_id=facts.guild_id,
            channel_id=facts.channel_id,
            user_id=facts.user_id,
            idempotency_key=facts.idempotency_key,
            steps=tuple(steps),
        )


def _explicit_repetition_spans(instruction: str) -> tuple[_RepetitionSpan, ...]:
    normalized = _normalized_instruction(instruction)
    repetitions = tuple(_EXPLICIT_REPETITION_RE.finditer(normalized))
    separators = tuple(_REPETITION_GROUP_SEPARATOR_RE.finditer(normalized))
    if not repetitions:
        return ()
    if len(repetitions) == 1:
        if _has_incomplete_trailing_separator(normalized, 0, repetitions[-1], separators):
            raise PlannerValidationError("each explicit repetition group requires one count")
        start, end = _trim_source_range(normalized, 0, len(normalized))
        if start >= end:
            raise PlannerValidationError("explicit repetition group source is missing")
        return (_RepetitionSpan(int(repetitions[0].group("count")), start, end, normalized[start:end]),)
    if len(repetitions) > _MAX_REPETITION_GROUPS:
        raise PlannerValidationError("explicit repetition group count exceeds the limit")
    boundaries: list[re.Match[str]] = []
    group_start = 0
    for current, following in zip(repetitions, repetitions[1:]):
        boundary = next(
            (
                separator
                for separator in separators
                if current.end() <= separator.start()
                and separator.end() <= following.start()
                and _source_range_requests_action(normalized, group_start, separator.start())
            ),
            None,
        )
        if boundary is None:
            raise PlannerValidationError("multiple repetitions require explicit ordered groups")
        boundaries.append(boundary)
        group_start = boundary.end()
    if _has_incomplete_trailing_separator(normalized, group_start, repetitions[-1], separators):
        raise PlannerValidationError("each explicit repetition group requires one count")

    ranges: list[tuple[int, int]] = []
    start = 0
    for separator in boundaries:
        ranges.append((start, separator.start()))
        start = separator.end()
    ranges.append((start, len(normalized)))

    groups: list[_RepetitionSpan] = []
    for raw_start, raw_end in ranges:
        start, end = _trim_source_range(normalized, raw_start, raw_end)
        if start >= end:
            raise PlannerValidationError("explicit repetition group source is missing")
        source = normalized[start:end]
        matches = tuple(_EXPLICIT_REPETITION_RE.finditer(source))
        if len(matches) != 1:
            raise PlannerValidationError("each explicit repetition group requires one count")
        groups.append(_RepetitionSpan(int(matches[0].group("count")), start, end, source))
    if sum(group.count for group in groups) > _MAX_STEPS:
        raise PlannerValidationError("explicit repetition exceeds the step limit")
    return tuple(groups)


def _ordered_action_spans(instruction: str) -> tuple[_RepetitionSpan, ...]:
    normalized = _normalized_instruction(instruction)
    if _instruction_is_question_or_explanation(normalized):
        return ()
    separators = tuple(_REPETITION_GROUP_SEPARATOR_RE.finditer(normalized))
    boundaries: list[re.Match[str]] = []
    group_start = 0
    for separator in separators:
        if not _source_range_requests_action(normalized, group_start, separator.start()):
            continue
        boundaries.append(separator)
        group_start = separator.end()
        if len(boundaries) + 1 > _MAX_REPETITION_GROUPS:
            raise PlannerValidationError("ordered action group count exceeds the limit")
    if not boundaries:
        return ()
    if not _source_range_requests_action(normalized, group_start, len(normalized)):
        raise PlannerValidationError("ordered action group source is missing or incomplete")

    ranges: list[tuple[int, int]] = []
    start = 0
    for separator in boundaries:
        ranges.append((start, separator.start()))
        start = separator.end()
    ranges.append((start, len(normalized)))
    return tuple(
        _RepetitionSpan(1, start, end, normalized[start:end])
        for raw_start, raw_end in ranges
        for start, end in (_trim_source_range(normalized, raw_start, raw_end),)
    )


def _trim_source_range(value: str, start: int, end: int) -> tuple[int, int]:
    while start < end and value[start] in _SPAN_EDGE_CHARS:
        start += 1
    while end > start and value[end - 1] in _SPAN_EDGE_CHARS:
        end -= 1
    return start, end


def _source_range_requests_action(value: str, start: int, end: int) -> bool:
    start, end = _trim_source_range(value, start, end)
    return start < end and _instruction_requests_actions(value[start:end])


def _has_incomplete_trailing_separator(
    value: str,
    group_start: int,
    repetition: re.Match[str],
    separators: tuple[re.Match[str], ...],
) -> bool:
    return any(
        _source_range_requests_action(value, group_start, separator.start())
        or not _source_range_requests_action(value, separator.end(), len(value))
        for separator in separators
        if separator.start() >= repetition.end()
    )


def _explicit_repetition_total(instruction: str) -> int:
    normalized = _normalized_instruction(instruction)
    return sum(int(match.group("count")) for match in _EXPLICIT_REPETITION_RE.finditer(normalized))


def _planner_model_decision(
    complexity: TaskComplexity,
    candidates: tuple[PlannerActionCandidate, ...],
) -> ModelDecision:
    has_side_effects = any(candidate.effect is ActionEffect.SIDE_EFFECT for candidate in candidates)
    high_risk = any(candidate.risk is RiskLevel.HIGH for candidate in candidates)
    effective_complexity = (
        TaskComplexity.COMPLEX
        if complexity is TaskComplexity.COMPLEX or len(candidates) > 4
        else TaskComplexity.STANDARD
    )
    return route_model(
        TaskProfile(
            kind=TaskKind.GENERAL,
            complexity=effective_complexity,
            risk=RiskLevel.HIGH if high_risk else RiskLevel.NORMAL,
            uses_tools=False,
            has_side_effects=has_side_effects,
        )
    )


def _candidate_score(
    normalized: str,
    tokens: frozenset[str],
    spec: ActionSpec,
    contract: PlannerActionContract,
) -> int:
    score = 0
    matched_hint = False
    for hint in (*contract.tags, *contract.intent_hints):
        hint_tokens = tuple(_TOKEN_RE.findall(hint))
        if not hint_tokens:
            continue
        if hint in normalized:
            score += 8
            matched_hint = True
            continue
        matched_tokens = sum(token in tokens for token in set(hint_tokens))
        if matched_tokens >= min(2, len(set(hint_tokens))):
            score += 2 * matched_tokens
            matched_hint = True
    if matched_hint:
        score += sum(part in tokens for part in spec.action_id.replace(".", " ").split())
    return score


def _normalized_instruction(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("planner instruction must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not 1 <= len(normalized) <= 4_000:
        raise ValueError("planner instruction is outside the bounded contract")
    return normalized


def _decode_document(raw: str | bytes) -> Mapping[str, Any]:
    if isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise PlannerValidationError("planner response is not strict UTF-8") from exc
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise PlannerValidationError("planner response must be text or bytes")
    if len(encoded) > _MAX_RESPONSE_BYTES or encoded.startswith(b"\xef\xbb\xbf"):
        raise PlannerValidationError("planner response size or encoding is invalid")
    try:
        text = encoded.decode("utf-8", errors="strict")
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PlannerValidationError("planner response is invalid JSON") from exc
    _validate_json_bounds(document)
    if not isinstance(document, Mapping):
        raise PlannerValidationError("planner response root must be an object")
    return document


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_json_bounds(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise PlannerValidationError("planner response is too deeply nested")
    if isinstance(value, str):
        if len(value) > _MAX_JSON_STRING:
            raise PlannerValidationError("planner response string is too long")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, list):
        if len(value) > 64:
            raise PlannerValidationError("planner response array is too large")
        for item in value:
            _validate_json_bounds(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise PlannerValidationError("planner response object is too large")
        for key, item in value.items():
            _validate_json_bounds(key, depth=depth + 1)
            _validate_json_bounds(item, depth=depth + 1)
        return
    raise PlannerValidationError("planner response contains an unsupported value")


def _validate_parameters(
    value: Any,
    schema: Mapping[str, Any],
) -> Mapping[str, str | ArtifactFromStep | ArtifactsFromSteps]:
    if not isinstance(value, Mapping):
        raise PlannerValidationError("planner parameters must be an object")
    properties = schema.get("properties")
    required = schema.get("required")
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or not isinstance(properties, Mapping)
        or not isinstance(required, (list, tuple))
    ):
        raise PlannerValidationError("planner action schema is unsupported")
    if set(value) != set(required) or set(value) - set(properties):
        raise PlannerValidationError("planner parameters do not match the action schema")
    result: dict[str, str | ArtifactFromStep | ArtifactsFromSteps] = {}
    for key, raw in value.items():
        rule = properties[key]
        if not isinstance(key, str) or not isinstance(rule, Mapping):
            raise PlannerValidationError("planner parameter schema is unsupported")
        if rule.get("type") == "artifact_ref":
            artifact_kinds_from_schema(rule)
            if (
                not isinstance(raw, Mapping)
                or set(raw) != {"artifact_from_step"}
                or not isinstance(raw["artifact_from_step"], str)
            ):
                raise PlannerValidationError("artifact input requires the exact typed placeholder")
            try:
                result[key] = ArtifactFromStep(raw["artifact_from_step"])
            except (TypeError, ValueError) as exc:
                raise PlannerValidationError("artifact source step identifier is invalid") from exc
            continue
        if rule.get("type") == "artifact_ref_list":
            minimum, maximum = artifact_list_bounds_from_schema(rule)
            if (
                not isinstance(raw, Mapping)
                or set(raw) != {"artifacts_from_steps"}
                or not isinstance(raw["artifacts_from_steps"], list)
                or not minimum <= len(raw["artifacts_from_steps"]) <= maximum
                or any(not isinstance(item, str) for item in raw["artifacts_from_steps"])
            ):
                raise PlannerValidationError("artifact list input requires the exact typed placeholder")
            try:
                result[key] = ArtifactsFromSteps(tuple(raw["artifacts_from_steps"]))
            except (TypeError, ValueError) as exc:
                raise PlannerValidationError("artifact list source step identifiers are invalid") from exc
            continue
        if rule.get("type") != "string":
            raise PlannerValidationError("planner parameter schema is unsupported")
        if not isinstance(raw, str):
            raise PlannerValidationError("planner parameter type is invalid")
        minimum = rule.get("minLength", 0)
        maximum = rule.get("maxLength", _MAX_JSON_STRING)
        if not isinstance(minimum, int) or not isinstance(maximum, int) or not minimum <= len(raw) <= maximum:
            raise PlannerValidationError("planner parameter length is invalid")
        pattern = rule.get("pattern")
        if pattern is not None and (not isinstance(pattern, str) or re.fullmatch(pattern, raw) is None):
            raise PlannerValidationError("planner parameter format is invalid")
        result[key] = raw
    return MappingProxyType(result)


def _contains_forbidden_binding_syntax(value: Any) -> bool:
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).strip().casefold()
        return "${" in normalized or normalized.startswith("step:")
    if isinstance(value, Mapping):
        return any(_contains_forbidden_binding_syntax(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_binding_syntax(item) for item in value)
    return False


def _validate_repetition_group_plan(
    steps: list[OrchestrationStep],
    candidates: Mapping[str, PlannerActionCandidate],
    groups: tuple[PlannerRepetitionGroup, ...],
) -> None:
    if not 1 <= len(groups) <= _MAX_REPETITION_GROUPS or len(steps) != sum(group.count for group in groups):
        raise PlannerValidationError("planner did not preserve the explicit repetition groups")

    grouped_steps: list[tuple[OrchestrationStep, ...]] = []
    grouped_grounded_values: list[tuple[str, ...]] = []
    offset = 0
    for group in groups:
        current = tuple(steps[offset : offset + group.count])
        offset += group.count
        if len(current) != group.count or any(step.action_id != group.action_id for step in current):
            raise PlannerValidationError("planner changed repetition group order or count")
        if any(step.parameters != current[0].parameters for step in current[1:]):
            raise PlannerValidationError("planner changed parameters inside a repetition group")
        expected_dependencies = () if not grouped_steps else tuple(step.step_id for step in grouped_steps[-1])
        if any(step.depends_on != expected_dependencies for step in current):
            raise PlannerValidationError("planner repetition group dependency barrier is invalid")

        candidate = candidates[group.action_id]
        values = tuple(current[0].parameters.get(name) for name in candidate.grounded_parameters)
        if any(not isinstance(value, str) for value in values):
            raise PlannerValidationError("planner repetition parameter is not grounded")
        if any(not _value_is_grounded_in_span(value, group.source_span) for value in values):
            raise PlannerValidationError("planner repetition parameter is outside its source group")
        expected_parameters = dict(group.expected_parameters)
        if expected_parameters and any(
            _normalized_instruction(current[0].parameters.get(name, "")) != value
            for name, value in expected_parameters.items()
        ):
            raise PlannerValidationError("planner repetition parameter does not match its source group")
        grouped_steps.append(current)
        grouped_grounded_values.append(values)

    groups_by_action: dict[str, list[int]] = {}
    for index, group in enumerate(groups):
        groups_by_action.setdefault(group.action_id, []).append(index)
    for indexes in groups_by_action.values():
        if len(indexes) < 2:
            continue
        for index in indexes:
            values = grouped_grounded_values[index]
            if not values or groups[index].expected_parameters:
                continue
            own_span = groups[index].source_span
            other_spans = tuple(groups[other].source_span for other in indexes if other != index)
            if any(
                not _value_is_grounded_in_span(value, own_span)
                or any(_value_is_grounded_in_span(value, span) for span in other_spans)
                for value in values
            ):
                raise PlannerValidationError("repeated action group parameters are ambiguous or swapped")


def _value_is_grounded_in_span(value: str, span: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return bool(normalized) and normalized in span


def _validate_plan_graph_and_artifacts(
    steps: list[OrchestrationStep],
    candidates: Mapping[str, PlannerActionCandidate],
) -> None:
    ids = [step.step_id for step in steps]
    if len(ids) != len(set(ids)):
        raise PlannerValidationError("planner step IDs must be unique")
    known = set(ids)
    positions = {step_id: index for index, step_id in enumerate(ids)}
    by_id = {step.step_id: step for step in steps}
    depths: dict[str, int] = {}

    def visit(step_id: str, trail: frozenset[str]) -> int:
        if step_id in trail:
            raise PlannerValidationError("planner dependency graph contains a cycle")
        if step_id in depths:
            return depths[step_id]
        step = by_id[step_id]
        if any(dependency not in known for dependency in step.depends_on):
            raise PlannerValidationError("planner dependency references an unknown step")
        value = 1 + max((visit(item, trail | {step_id}) for item in step.depends_on), default=0)
        if value > 8:
            raise PlannerValidationError("planner dependency graph is too deep")
        depths[step_id] = value
        return value

    for step in steps:
        visit(step.step_id, frozenset())
        consumer = candidates[step.action_id]
        properties = consumer.input_schema.get("properties")
        for name, value in step.parameters.items():
            if not isinstance(value, (ArtifactFromStep, ArtifactsFromSteps)):
                continue
            rule = properties.get(name) if isinstance(properties, Mapping) else None
            expected_type = "artifact_ref" if isinstance(value, ArtifactFromStep) else "artifact_ref_list"
            if not isinstance(rule, Mapping) or rule.get("type") != expected_type:
                raise PlannerValidationError("artifact binding does not match the consumer slot")
            accepted = artifact_kinds_from_schema(rule)
            source_ids = artifact_source_step_ids({name: value})
            if isinstance(value, ArtifactsFromSteps):
                minimum, maximum = artifact_list_bounds_from_schema(rule)
                if not minimum <= len(source_ids) <= maximum:
                    raise PlannerValidationError("artifact list binding count is outside the consumer bounds")
            for source_id in source_ids:
                if source_id not in known:
                    raise PlannerValidationError("artifact binding references an unknown step")
                if source_id == step.step_id:
                    raise PlannerValidationError("artifact binding cannot reference itself")
                if positions[source_id] >= positions[step.step_id]:
                    raise PlannerValidationError("artifact binding cannot reference a forward step")
                if source_id not in step.depends_on:
                    raise PlannerValidationError("artifact binding must reference a direct dependency")
                source = by_id[source_id]
                producer = candidates[source.action_id]
                output_schema = producer.output_artifact_schema
                if output_schema is None:
                    raise PlannerValidationError("artifact producer has no output schema")
                if accepted.isdisjoint(artifact_kinds_from_schema(output_schema, output=True)):
                    raise PlannerValidationError("artifact producer and consumer kinds do not match")


def _response_schema(candidates: tuple[PlannerActionCandidate, ...]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["steps"],
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": _MAX_STEPS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["step_id", "action_id", "parameters", "depends_on"],
                        "properties": {
                            "step_id": {"type": "string", "maxLength": 128},
                            "action_id": {"enum": [candidate.action_id for candidate in candidates]},
                            "parameters": {"type": "object"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                }
            },
        }
    )


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


__all__ = [
    "OrchestrationPlanner",
    "PlannerActionCandidate",
    "PlannerDispatchContext",
    "PlannerError",
    "PlannerFacts",
    "PlannerModelPort",
    "PlannerModelRequest",
    "PlannerOutputBindingUnavailableError",
    "PlannerRepetitionGroup",
    "PlannerUnavailableError",
    "PlannerValidationError",
    "PreparedPlanPort",
]
