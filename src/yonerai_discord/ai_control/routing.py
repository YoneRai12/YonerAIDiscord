from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ModelName(StrEnum):
    TERRA = "gpt-5.6-terra"
    SOL = "gpt-5.6-sol"
    LUNA = "gpt-5.6-luna"


ALLOWED_MODELS: frozenset[str] = frozenset(model.value for model in ModelName)


class ReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskKind(StrEnum):
    GENERAL = "general"
    CLASSIFICATION = "classification"
    FORMATTING = "formatting"
    CODE_GENERATION = "code_generation"
    SELF_EVOLUTION = "self_evolution"


class TaskComplexity(StrEnum):
    TINY = "tiny"
    STANDARD = "standard"
    COMPLEX = "complex"


class RiskLevel(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class InvalidModelOverrideError(ValueError):
    """Raised when an override names a model outside the fixed allowlist."""


class UnsafeModelOverrideError(ValueError):
    """Raised when an override would weaken the required model policy."""


@dataclass(frozen=True, slots=True)
class TaskProfile:
    kind: TaskKind = TaskKind.GENERAL
    complexity: TaskComplexity = TaskComplexity.STANDARD
    risk: RiskLevel = RiskLevel.NORMAL
    uses_tools: bool = False
    has_side_effects: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TaskKind):
            raise TypeError("kind must be a TaskKind")
        if not isinstance(self.complexity, TaskComplexity):
            raise TypeError("complexity must be a TaskComplexity")
        if not isinstance(self.risk, RiskLevel):
            raise TypeError("risk must be a RiskLevel")


@dataclass(frozen=True, slots=True)
class ModelDecision:
    model: ModelName
    reasoning_effort: ReasoningEffort
    reason: str
    factors: tuple[str, ...]
    override_applied: bool = False


def _luna_eligible(profile: TaskProfile) -> bool:
    return (
        profile.kind in {TaskKind.CLASSIFICATION, TaskKind.FORMATTING}
        and profile.complexity is TaskComplexity.TINY
        and profile.risk is RiskLevel.LOW
        and not profile.uses_tools
        and not profile.has_side_effects
    )


def _baseline(profile: TaskProfile) -> ModelDecision:
    sol_factors: list[str] = []
    if profile.kind is TaskKind.SELF_EVOLUTION:
        sol_factors.append("self_evolution")
    if profile.kind is TaskKind.CODE_GENERATION:
        sol_factors.append("code_generation")
    if profile.complexity is TaskComplexity.COMPLEX:
        sol_factors.append("complex_task")
    if profile.risk is RiskLevel.HIGH:
        sol_factors.append("high_risk")
    if profile.uses_tools:
        sol_factors.append("tool_use")
    if profile.has_side_effects:
        sol_factors.append("side_effects")

    if sol_factors:
        factors = tuple(sol_factors)
        return ModelDecision(
            model=ModelName.SOL,
            reasoning_effort=ReasoningEffort.HIGH,
            reason="Sol is required because: " + ", ".join(factors),
            factors=factors,
        )

    if _luna_eligible(profile):
        factors = ("tiny_read_only_classification_or_formatting",)
        return ModelDecision(
            model=ModelName.LUNA,
            reasoning_effort=ReasoningEffort.LOW,
            reason="Luna is allowed for a tiny, low-risk, tool-free, side-effect-free classification or formatting task",
            factors=factors,
        )

    return ModelDecision(
        model=ModelName.TERRA,
        reasoning_effort=ReasoningEffort.MEDIUM,
        reason="Terra is the default for tasks that need neither Sol escalation nor the narrow Luna fast path",
        factors=("default",),
    )


def route_model(profile: TaskProfile, *, override: str | None = None) -> ModelDecision:
    """Return a deterministic model decision without performing any network operation."""

    baseline = _baseline(profile)
    if override is None:
        return baseline

    try:
        requested = ModelName(override)
    except ValueError as exc:
        allowed = ", ".join(sorted(ALLOWED_MODELS))
        raise InvalidModelOverrideError(f"model override must be one of: {allowed}") from exc

    if baseline.model is ModelName.SOL and requested is not ModelName.SOL:
        raise UnsafeModelOverrideError("this task requires gpt-5.6-sol; override cannot reduce the safety tier")
    if requested is ModelName.LUNA and not _luna_eligible(profile):
        raise UnsafeModelOverrideError(
            "gpt-5.6-luna is restricted to tiny, low-risk, tool-free, side-effect-free classification or formatting"
        )

    effort = {
        ModelName.LUNA: ReasoningEffort.LOW,
        ModelName.TERRA: ReasoningEffort.MEDIUM,
        ModelName.SOL: ReasoningEffort.HIGH,
    }[requested]
    return ModelDecision(
        model=requested,
        reasoning_effort=effort,
        reason=f"allowlisted override accepted: {requested.value}; baseline reason: {baseline.reason}",
        factors=baseline.factors,
        override_applied=True,
    )
