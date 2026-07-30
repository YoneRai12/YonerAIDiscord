"""既存AIServiceへbounded planner JSONを1回だけ委譲するadapter。"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from yonerai_discord.ai_control.routing import ModelName, RiskLevel, TaskComplexity, TaskKind
from yonerai_discord.v0_contracts import (
    FORMAL_PROVIDER_INPUT_DIRECTIVE,
    ContextAuthorizationToken,
    MemoryVisibility,
    Scope,
)

from .bounded_tools import (
    BoundedIntent,
    BoundedToolSet,
    ToolScopeBinding,
    capability_metadata_digest,
)
from .models import AIRequest, provider_facing_envelope_digest
from .orchestration_planner import (
    PlannerDispatchContext,
    PlannerModelRequest,
    PlannerUnavailableError,
)
from .service import AIService


_MAX_PLANNER_PAYLOAD_BYTES = 32 * 1024
_PLANNER_PROVIDER_INPUT = "Return the bounded orchestration plan as strict JSON."
_PLANNER_SYSTEM_INSTRUCTION = (
    "Use only the supplied candidate action IDs and schemas. "
    "Return exactly one JSON object matching response_schema. "
    "Preserve every requested operation and its order through explicit steps and depends_on. "
    "When the instruction says N times, expand that operation into N separate steps; "
    "never shorten, summarize, or invent an implicit loop. "
    "When repetition_groups are supplied, emit exactly each group's count as contiguous steps "
    "in listed order, keep parameters identical inside each group, and set every later-group "
    "step's depends_on to exactly all step IDs from the immediately preceding group. "
    "For an artifact_ref input use only the exact placeholder "
    '{"artifact_from_step":"step-id"} referencing a direct completed dependency. '
    "Never return artifact IDs, digests, paths, bytes, receipts, public text, expressions, "
    "or any other output binding. Do not invent tools, authority, paths, or commands."
)


class AIServicePlannerPort:
    """PlannerModelPortを既存AIServiceのformal requestへ変換する。"""

    def __init__(
        self,
        service: AIService,
        *,
        capability_catalog_revision: Callable[[], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(service, AIService):
            raise TypeError("service must be an AIService")
        if not callable(capability_catalog_revision):
            raise TypeError("capability_catalog_revision must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._service = service
        self._capability_catalog_revision = capability_catalog_revision
        self._clock = clock

    async def generate(
        self,
        request: PlannerModelRequest,
        dispatch: PlannerDispatchContext,
    ) -> str:
        if not isinstance(request, PlannerModelRequest):
            raise TypeError("request must be a PlannerModelRequest")
        if not isinstance(dispatch, PlannerDispatchContext):
            raise TypeError("dispatch must be a PlannerDispatchContext")
        alias = _logical_alias(request)
        complexity = TaskComplexity.COMPLEX if alias == "ai.quality" else TaskComplexity.STANDARD
        issued_at = float(self._clock())
        toolset = BoundedToolSet(
            scope=ToolScopeBinding(dispatch.guild_id, dispatch.channel_id, dispatch.user_id),
            intent=BoundedIntent.CONVERSATION,
            complexity=complexity.value,
            candidates=(),
            effective_tools=(),
            max_tool_calls=0,
            tool_capability_bindings=(),
            capability_catalog_revision=self._capability_catalog_revision(),
            provider_catalog_revision=self._service.provider_catalog_revision,
            issued_at=issued_at,
            expires_at=issued_at + 30.0,
        )
        system_prompt = _canonical_planner_payload(request)
        risk = _request_risk(request)
        envelope_digest = provider_facing_envelope_digest(
            prompt=_PLANNER_PROVIDER_INPUT,
            provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
            history=(),
            attachments=(),
            metadata={},
            task_kind=TaskKind.GENERAL,
            complexity=complexity,
            risk=risk,
            uses_tools=False,
            web_search=False,
            has_side_effects=False,
            boundary=dispatch.boundary,
            required_model_alias=alias,
        )
        authorization = ContextAuthorizationToken.issue(
            Scope(
                dispatch.guild_id,
                dispatch.user_id,
                channel_id=dispatch.channel_id,
                visibility=MemoryVisibility.GUILD_PUBLIC,
            ),
            request_channel_id=dispatch.channel_id,
            prompt=system_prompt,
            bounded_toolset_digest=toolset.digest,
            capability_catalog_revision=toolset.capability_catalog_revision,
            provider_catalog_revision=toolset.provider_catalog_revision,
            intent=toolset.intent.value,
            complexity=toolset.complexity.value,
            effective_tools=(),
            capability_metadata_sha256=capability_metadata_digest(toolset),
            provider_envelope_sha256=envelope_digest,
        )
        ai_request = AIRequest(
            prompt=_PLANNER_PROVIDER_INPUT,
            guild_id=dispatch.guild_id,
            channel_id=dispatch.channel_id,
            user_id=dispatch.user_id,
            provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
            context_authorization=authorization,
            required_model_alias=alias,
            boundary=dispatch.boundary,
            system_prompt=system_prompt,
            task_kind=TaskKind.GENERAL,
            complexity=complexity,
            risk=risk,
            uses_tools=False,
            web_search=False,
            has_side_effects=False,
            intent=BoundedIntent.CONVERSATION,
            bounded_toolset=toolset,
            allowed_model_tools=(),
            max_tool_calls=0,
            history=(),
            attachments=(),
        )
        candidate_action_ids = tuple(candidate.action_id for candidate in request.candidates)
        authorized_ids = await dispatch.authorize_candidate_action_ids(candidate_action_ids)
        if authorized_ids != frozenset(candidate_action_ids):
            raise PlannerUnavailableError("planner candidate authorization expired")

        async def planner_provider_sink_allowed() -> bool:
            try:
                root_allowed = dispatch.provider_call_allowed()
                if inspect.isawaitable(root_allowed):
                    root_allowed = await root_allowed
                if root_allowed is not True:
                    return False
                fresh_ids = await dispatch.authorize_candidate_action_ids(candidate_action_ids)
                if fresh_ids != frozenset(candidate_action_ids):
                    return False
                root_allowed = dispatch.provider_call_allowed()
                if inspect.isawaitable(root_allowed):
                    root_allowed = await root_allowed
                return root_allowed is True
            except asyncio.CancelledError:
                raise
            except Exception:
                return False

        reply = await self._service.ask(
            ai_request,
            provider_call_allowed=lambda: True,
            fresh_provider_call_allowed=planner_provider_sink_allowed,
        )
        return reply.text


def _logical_alias(request: PlannerModelRequest) -> str:
    if request.model_decision.model is ModelName.TERRA:
        return "ai.balanced"
    if request.model_decision.model is ModelName.SOL:
        return "ai.quality"
    raise ValueError("planner model must be Terra or Sol")


def _request_risk(request: PlannerModelRequest) -> RiskLevel:
    return (
        RiskLevel.HIGH
        if any(candidate.risk is RiskLevel.HIGH for candidate in request.candidates)
        else RiskLevel.NORMAL
    )


def _canonical_planner_payload(request: PlannerModelRequest) -> str:
    payload = {
        "candidates": [_plain_json(candidate.to_mapping()) for candidate in request.candidates],
        "instruction": request.instruction,
        "response_schema": _plain_json(request.response_schema),
        "system_instruction": _PLANNER_SYSTEM_INSTRUCTION,
    }
    if request.repetition_groups:
        payload["repetition_groups"] = [_plain_json(group.to_mapping()) for group in request.repetition_groups]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_PLANNER_PAYLOAD_BYTES:
        raise ValueError("planner provider payload is too large")
    return encoded.decode("utf-8")


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json(item) for item in value]
    return value


__all__ = ["AIServicePlannerPort"]
