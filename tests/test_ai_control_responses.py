from __future__ import annotations

import pytest

from yonerai_discord.ai_control import (
    InvalidModelOverrideError,
    ModelName,
    TaskKind,
    TaskProfile,
    build_responses_request,
)


def test_responses_request_is_store_disabled_and_has_explicit_effort() -> None:
    spec = build_responses_request(
        input="Summarize this message",
        instructions="Be concise",
        profile=TaskProfile(),
        metadata={"request_id": "req-1"},
    )
    assert spec.to_payload() == {
        "model": "gpt-5.6-terra",
        "input": "Summarize this message",
        "store": False,
        "reasoning": {"effort": "medium"},
        "instructions": "Be concise",
        "metadata": {"request_id": "req-1"},
    }


def test_request_builder_routes_code_generation_to_sol() -> None:
    spec = build_responses_request(input="write code", profile=TaskProfile(kind=TaskKind.CODE_GENERATION))
    assert spec.decision.model is ModelName.SOL
    assert spec.to_payload()["reasoning"] == {"effort": "high"}


def test_request_builder_rejects_invalid_override_before_any_network_activity() -> None:
    with pytest.raises(InvalidModelOverrideError):
        build_responses_request(input="hello", profile=TaskProfile(), model_override="not-allowed")


def test_request_builder_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="input"):
        build_responses_request(input="  ", profile=TaskProfile())
