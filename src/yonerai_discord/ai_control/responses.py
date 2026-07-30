from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .routing import ModelDecision, TaskProfile, route_model


@dataclass(frozen=True, slots=True)
class ResponsesRequestSpec:
    """Serializable Responses API request data; it deliberately has no send method."""

    decision: ModelDecision
    input: str
    instructions: str | None = None
    metadata: Mapping[str, str] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.decision.model.value,
            "input": self.input,
            "store": False,
            "reasoning": {"effort": self.decision.reasoning_effort.value},
        }
        if self.instructions is not None:
            payload["instructions"] = self.instructions
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


def build_responses_request(
    *,
    input: str,
    profile: TaskProfile,
    instructions: str | None = None,
    metadata: Mapping[str, str] | None = None,
    model_override: str | None = None,
) -> ResponsesRequestSpec:
    """Build a store-disabled Responses API request specification without sending it."""

    if not input.strip():
        raise ValueError("input must not be empty")
    if instructions is not None and not instructions.strip():
        raise ValueError("instructions must not be blank")
    if metadata is not None:
        if len(metadata) > 16:
            raise ValueError("metadata must contain at most 16 entries")
        if any(not str(key).strip() or not isinstance(value, str) for key, value in metadata.items()):
            raise ValueError("metadata keys must be non-empty and values must be strings")

    decision = route_model(profile, override=model_override)
    return ResponsesRequestSpec(
        decision=decision,
        input=input,
        instructions=instructions,
        metadata=dict(metadata) if metadata is not None else None,
    )
