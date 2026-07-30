from __future__ import annotations

import pytest

from yonerai_discord.ai_control import (
    ALLOWED_MODELS,
    InvalidModelOverrideError,
    ModelName,
    ReasoningEffort,
    RiskLevel,
    TaskComplexity,
    TaskKind,
    TaskProfile,
    UnsafeModelOverrideError,
    route_model,
)


def test_allowlist_contains_only_the_three_supported_models() -> None:
    assert ALLOWED_MODELS == {"gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"}


def test_normal_task_deterministically_uses_terra() -> None:
    profile = TaskProfile()
    first = route_model(profile)
    second = route_model(profile)
    assert first == second
    assert first.model is ModelName.TERRA
    assert first.reasoning_effort is ReasoningEffort.MEDIUM
    assert first.factors == ("default",)
    assert first.reason


@pytest.mark.parametrize(
    ("profile", "factor"),
    [
        (TaskProfile(kind=TaskKind.CODE_GENERATION), "code_generation"),
        (TaskProfile(kind=TaskKind.SELF_EVOLUTION), "self_evolution"),
        (TaskProfile(complexity=TaskComplexity.COMPLEX), "complex_task"),
        (TaskProfile(risk=RiskLevel.HIGH), "high_risk"),
        (TaskProfile(uses_tools=True), "tool_use"),
        (TaskProfile(has_side_effects=True), "side_effects"),
    ],
)
def test_sol_is_required_for_escalation_conditions(profile: TaskProfile, factor: str) -> None:
    decision = route_model(profile)
    assert decision.model is ModelName.SOL
    assert decision.reasoning_effort is ReasoningEffort.HIGH
    assert factor in decision.factors


@pytest.mark.parametrize("kind", [TaskKind.CLASSIFICATION, TaskKind.FORMATTING])
def test_luna_is_limited_to_the_tiny_read_only_fast_path(kind: TaskKind) -> None:
    decision = route_model(TaskProfile(kind=kind, complexity=TaskComplexity.TINY, risk=RiskLevel.LOW))
    assert decision.model is ModelName.LUNA
    assert decision.reasoning_effort is ReasoningEffort.LOW


@pytest.mark.parametrize(
    "profile",
    [
        TaskProfile(kind=TaskKind.GENERAL, complexity=TaskComplexity.TINY, risk=RiskLevel.LOW),
        TaskProfile(kind=TaskKind.CLASSIFICATION, complexity=TaskComplexity.STANDARD, risk=RiskLevel.LOW),
        TaskProfile(kind=TaskKind.CLASSIFICATION, complexity=TaskComplexity.TINY, risk=RiskLevel.NORMAL),
        TaskProfile(kind=TaskKind.CLASSIFICATION, complexity=TaskComplexity.TINY, risk=RiskLevel.LOW, uses_tools=True),
        TaskProfile(
            kind=TaskKind.CLASSIFICATION,
            complexity=TaskComplexity.TINY,
            risk=RiskLevel.LOW,
            has_side_effects=True,
        ),
    ],
)
def test_luna_is_not_selected_when_any_fast_path_condition_is_missing(profile: TaskProfile) -> None:
    assert route_model(profile).model is not ModelName.LUNA


def test_unknown_override_is_rejected() -> None:
    with pytest.raises(InvalidModelOverrideError):
        route_model(TaskProfile(), override="gpt-4o")


def test_override_cannot_downgrade_a_sol_task() -> None:
    with pytest.raises(UnsafeModelOverrideError):
        route_model(TaskProfile(uses_tools=True), override=ModelName.TERRA.value)


def test_override_cannot_use_luna_outside_its_narrow_scope() -> None:
    with pytest.raises(UnsafeModelOverrideError):
        route_model(TaskProfile(), override=ModelName.LUNA.value)


def test_allowlisted_override_can_raise_the_model_tier() -> None:
    decision = route_model(TaskProfile(), override=ModelName.SOL.value)
    assert decision.model is ModelName.SOL
    assert decision.reasoning_effort is ReasoningEffort.HIGH
    assert decision.override_applied is True
