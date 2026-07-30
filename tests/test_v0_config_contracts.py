from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_contract(filename: str) -> dict[str, Any]:
    """YAML 1.2 accepts JSON, so these contracts need no YAML dependency."""
    return json.loads((ROOT / "config" / filename).read_text(encoding="utf-8"))


def test_character_kernel_is_a_personality_only_contract() -> None:
    assert load_contract("character_kernel.yaml") == {
        "schema_version": 1,
        "persona": {"name": "YonerAI", "archetype": "calm collaborative companion"},
        "traits": ["curious", "thoughtful", "honest", "warm"],
        "communication_style": [
            "use clear everyday language",
            "make uncertainty explicit",
            "prefer useful context over performative certainty",
        ],
        "relational_stance": [
            "treat the person as a collaborator",
            "preserve the person's agency",
            "adapt tone without imitating identity",
        ],
    }


def test_runtime_policy_is_limited_to_boundary_policy() -> None:
    assert load_contract("runtime_policy.yaml") == {
        "schema_version": 1,
        "privacy": {
            "data_minimization": "authorized-inputs-only",
            "secret_handling": "never-expose-or-transmit",
            "local_default": True,
        },
        "consent": {
            "remote_processing": "explicit-per-request",
            "durable_memory_write": "explicit-user-action",
            "revocation": "immediate",
        },
        "scope": {"visibility": "explicit", "residency": "explicit", "retention": "explicit"},
        "cloud_escape": {
            "default": "deny",
            "requirements": ["explicit-consent", "authorized-scope", "approved-destination"],
            "failure_mode": "fail-closed",
        },
        "tool_policy": {
            "default": "deny",
            "allowlist_only": True,
            "authorization": "fresh-at-invocation",
            "commit_time_authorization": True,
        },
    }


def test_contracts_contain_no_runtime_identifiers_or_secret_values() -> None:
    prohibited = re.compile(
        r"https?://|(?:api[_-]?key|model[_-]?id|provider[_-]?id|token|secret_ref|endpoint|url)", re.I
    )

    def strings(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [item for key, nested in value.items() for item in [key, *strings(nested)]]
        if isinstance(value, list):
            return [item for nested in value for item in strings(nested)]
        return [value] if isinstance(value, str) else []

    for filename in ("character_kernel.yaml", "runtime_policy.yaml"):
        assert not [item for item in strings(load_contract(filename)) if prohibited.search(item)]
