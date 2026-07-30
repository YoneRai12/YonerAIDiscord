from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from yonerai_discord.ai_control.routing import (
    ModelName,
    RiskLevel,
    TaskComplexity,
    TaskKind,
    route_model,
    TaskProfile,
)
from yonerai_discord.modules.ai.action_router import ActionEffect
from yonerai_discord.modules.ai.models import AIReply, AIRequest, DataBoundary
from yonerai_discord.modules.ai.orchestration_planner import (
    PlannerActionCandidate,
    PlannerDispatchContext,
    PlannerModelRequest,
    PlannerRepetitionGroup,
    PlannerUnavailableError,
)
from yonerai_discord.modules.ai.orchestration_planner_adapter import AIServicePlannerPort
from yonerai_discord.modules.ai.ports import ProviderAuthorizationError, _verify_service_sink_async
from yonerai_discord.modules.ai.service import AIService, AIUnavailableError


class RecordingAIService(AIService):
    def __init__(self, response: str = '{"steps":[]}') -> None:
        super().__init__(None, provider_catalog_revision="1" * 64)
        self.response = response
        self.calls: list[tuple[AIRequest, dict[str, object]]] = []
        self.provider_calls = 0

    async def ask(self, request: AIRequest, **kwargs: object) -> AIReply:
        self.calls.append((request, kwargs))
        fresh = kwargs.get("fresh_provider_call_allowed")
        if not callable(fresh):
            raise AssertionError("fresh provider authorization is required")
        allowed = fresh()
        if hasattr(allowed, "__await__"):
            allowed = await allowed
        if allowed is not True:
            raise AIUnavailableError("authorization expired")
        self.provider_calls += 1
        return AIReply(self.response, request.required_model_alias or "missing", "test")


def _candidate() -> PlannerActionCandidate:
    return PlannerActionCandidate(
        "tools.dice",
        "bounded dice",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"expression": {"type": "string", "minLength": 1, "maxLength": 32}},
            "required": ["expression"],
        },
        ("dice",),
        ("roll",),
        RiskLevel.LOW,
        ActionEffect.READ_ONLY,
    )


def _request(*, complex_task: bool = False) -> PlannerModelRequest:
    decision = route_model(
        TaskProfile(
            kind=TaskKind.GENERAL,
            complexity=TaskComplexity.COMPLEX if complex_task else TaskComplexity.STANDARD,
        )
    )
    return PlannerModelRequest(
        "dice twice",
        (_candidate(),),
        decision,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["steps"],
            "properties": {"steps": {"type": "array", "maxItems": 20}},
        },
    )


def _dispatch(
    check: Callable[[], bool] = lambda: True,
    candidate_action_authorizer: Callable[[tuple[str, ...]], object] = lambda values: values,
) -> PlannerDispatchContext:
    return PlannerDispatchContext(
        1,
        2,
        3,
        DataBoundary.LOCAL_ONLY,
        check,
        candidate_action_authorizer,  # type: ignore[arg-type]
    )


def _port(service: AIService) -> AIServicePlannerPort:
    return AIServicePlannerPort(
        service,
        capability_catalog_revision=lambda: "2" * 64,
        clock=lambda: 100.0,
    )


@pytest.mark.asyncio
async def test_adapter_calls_ai_service_once_with_minimal_empty_tool_request() -> None:
    service = RecordingAIService('{"steps":[]}')
    port = _port(service)

    assert await port.generate(_request(), _dispatch()) == '{"steps":[]}'

    assert len(service.calls) == 1
    request, kwargs = service.calls[0]
    assert request.required_model_alias == "ai.balanced"
    assert request.history == () and request.attachments == () and dict(request.metadata) == {}
    assert request.uses_tools is request.web_search is request.has_side_effects is False
    assert request.allowed_model_tools == () and request.max_tool_calls == 0
    assert request.bounded_toolset is not None
    assert request.bounded_toolset.effective_tools == ()
    assert request.bounded_toolset.candidates == ()
    assert request.context_authorization_current() is True
    assert set(kwargs) == {"provider_call_allowed", "fresh_provider_call_allowed"}


@pytest.mark.asyncio
async def test_payload_contains_only_instruction_candidates_schema_and_fixed_instruction() -> None:
    service = RecordingAIService()
    port = _port(service)

    await port.generate(_request(), _dispatch())

    payload = json.loads(service.calls[0][0].system_prompt)
    assert set(payload) == {"instruction", "candidates", "response_schema", "system_instruction"}
    assert payload["instruction"] == "dice twice"
    assert len(payload["candidates"]) == 1
    serialized = service.calls[0][0].system_prompt
    for forbidden in ("history", "memory", "attachment", "settings", "executor", "parser", "secret", "consent"):
        assert forbidden not in serialized.casefold()


@pytest.mark.asyncio
async def test_payload_includes_only_bounded_ordered_repetition_groups_when_present() -> None:
    service = RecordingAIService()
    port = _port(service)
    instruction = "dice 2 times then dice 3 times"
    first = "dice 2 times"
    second = "dice 3 times"
    request = PlannerModelRequest(
        instruction,
        (_candidate(),),
        _request().model_decision,
        _request().response_schema,
        (
            PlannerRepetitionGroup("tools.dice", 2, 0, len(first), first),
            PlannerRepetitionGroup(
                "tools.dice",
                3,
                instruction.index(second),
                len(instruction),
                second,
            ),
        ),
    )

    await port.generate(request, _dispatch())

    payload = json.loads(service.calls[0][0].system_prompt)
    assert payload["repetition_groups"] == [
        {"action_id": "tools.dice", "count": 2, "source_span": first},
        {"action_id": "tools.dice", "count": 3, "source_span": second},
    ]
    assert "immediately preceding group" in payload["system_instruction"]


@pytest.mark.asyncio
async def test_payload_exposes_only_typed_artifact_schema_and_exact_placeholder_instruction() -> None:
    service = RecordingAIService()
    port = _port(service)
    candidate = PlannerActionCandidate(
        "test.media",
        "bounded media",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"source": {"type": "artifact_ref", "kind": "qr_code"}},
            "required": ["source"],
        },
        ("media",),
        ("compose",),
        RiskLevel.LOW,
        ActionEffect.READ_ONLY,
        {"type": "artifact_ref", "kind": "image"},
    )
    request = PlannerModelRequest(
        "compose media",
        (candidate,),
        _request().model_decision,
        _request().response_schema,
    )

    await port.generate(request, _dispatch())

    ai_request = service.calls[0][0]
    payload = json.loads(ai_request.system_prompt)
    assert set(payload) == {"instruction", "candidates", "response_schema", "system_instruction"}
    assert payload["candidates"][0]["input_schema"]["properties"]["source"] == {
        "type": "artifact_ref",
        "kind": "qr_code",
    }
    assert payload["candidates"][0]["output_artifact_schema"] == {
        "type": "artifact_ref",
        "kind": "image",
    }
    assert '{"artifact_from_step":"step-id"}' in payload["system_instruction"]
    serialized = ai_request.system_prompt.casefold()
    for forbidden in ("artifact_id", "scope_digest", "content_digest", "recipe_digest", "history", "memory"):
        assert forbidden not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("complex_task", "model", "alias"),
    (
        (False, ModelName.TERRA, "ai.balanced"),
        (True, ModelName.SOL, "ai.quality"),
    ),
)
async def test_adapter_uses_only_balanced_or_quality_alias(
    complex_task: bool,
    model: ModelName,
    alias: str,
) -> None:
    service = RecordingAIService()
    request = _request(complex_task=complex_task)

    await _port(service).generate(request, _dispatch())

    assert request.model_decision.model is model
    assert service.calls[0][0].required_model_alias == alias
    assert service.calls[0][0].required_model_alias != "ai.fast"


@pytest.mark.asyncio
async def test_fresh_authorization_revoke_rejects_service_result() -> None:
    service = RecordingAIService()

    with pytest.raises(AIUnavailableError):
        await _port(service).generate(
            _request(),
            _dispatch(lambda: False),
        )

    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_candidate_revoke_at_provider_sink_skips_ai_service() -> None:
    service = RecordingAIService()

    with pytest.raises(PlannerUnavailableError):
        await _port(service).generate(
            _request(),
            _dispatch(candidate_action_authorizer=lambda _values: ()),
        )

    assert service.calls == []


@pytest.mark.asyncio
async def test_candidate_revoke_while_service_is_queued_stops_provider_sink() -> None:
    service = RecordingAIService()
    authorization_calls = 0

    def authorize(values: tuple[str, ...]) -> tuple[str, ...]:
        nonlocal authorization_calls
        authorization_calls += 1
        return values if authorization_calls == 1 else ()

    with pytest.raises(AIUnavailableError):
        await _port(service).generate(
            _request(),
            _dispatch(candidate_action_authorizer=authorize),
        )

    assert authorization_calls == 2
    assert len(service.calls) == 1
    assert service.provider_calls == 0


@pytest.mark.asyncio
async def test_real_ai_service_accepts_live_revision_with_empty_candidates() -> None:
    class Provider:
        is_local = True
        runtime_provider_id = "provider.test"
        runtime_model_bindings = {
            "ai.fast": "test-fast",
            "ai.balanced": "test-balanced",
            "ai.quality": "test-quality",
        }

        def __init__(self) -> None:
            self.calls = 0

        def resolved_model_alias(self, request: AIRequest) -> str:
            return request.required_model_alias or "ai.balanced"

        async def complete_authorized(
            self,
            request: AIRequest,
            verifier: object,
        ) -> AIReply:
            if not await _verify_service_sink_async(verifier, request=request, provider=self):
                raise ProviderAuthorizationError("authorization rejected")
            self.calls += 1
            return AIReply('{"steps":[]}', request.required_model_alias or "missing", "test")

    provider = Provider()
    service = AIService(
        provider,  # type: ignore[arg-type]
        require_prepared_context=True,
        require_authorization=True,
        provider_catalog_revision="1" * 64,
        capability_catalog_revision=lambda: "2" * 64,
    )

    result = await AIServicePlannerPort(
        service,
        capability_catalog_revision=lambda: "2" * 64,
    ).generate(_request(), _dispatch())

    assert result == '{"steps":[]}'
    assert provider.calls == 1
