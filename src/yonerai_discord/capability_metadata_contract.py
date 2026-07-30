"""Canonical transport contract for code-owned capability metadata.

This module is intentionally independent from Discord, providers, and the AI
package so both retrieval and ContextBuilder validate the exact same schema.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from yonerai_discord.secret_detection import contains_secret_like


CAPABILITY_METADATA_KEYS = frozenset(
    {
        "bindings",
        "capability_id",
        "content_revision",
        "intent_tags",
        "minimum_rbac",
        "module_id",
        "name",
        "primary_intent",
        "risk",
        "source_provenance",
        "surface_bindings",
    }
)
MAX_CAPABILITY_METADATA_ENTRY_BYTES = 8 * 1024
MAX_CAPABILITY_METADATA_ITEMS = 8
MAX_CAPABILITY_METADATA_TRANSPORT_BYTES = 32 * 1024

_IDENTIFIER_ASCII = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._:-/")
CAPABILITY_METADATA_INTENTS = frozenset(
    {
        "conversation",
        "knowledge",
        "web_research",
        "code",
        "site",
        "media",
        "music",
        "memory",
        "moderation",
        "self_evolution",
        "unknown",
    }
)
CAPABILITY_METADATA_RISKS = frozenset({"low", "medium", "high", "critical"})
CAPABILITY_METADATA_RBAC_LEVELS = frozenset(
    {
        "everyone",
        "trusted",
        "moderator",
        "guild_admin",
        "guild_owner",
        "bot_owner",
    }
)
CAPABILITY_METADATA_PROVENANCE = frozenset({"canonical_registry", "runtime_manifest"})
_INTENTS = CAPABILITY_METADATA_INTENTS
_RISKS = CAPABILITY_METADATA_RISKS
_RBAC_LEVELS = CAPABILITY_METADATA_RBAC_LEVELS
_PROVENANCE = CAPABILITY_METADATA_PROVENANCE
_WEB_SEARCH_TOOL_ID = "web_search"
_WEB_SEARCH_CAPABILITY_ID = "cap-run-web-search-openai-paid"


def capability_metadata_content_revision(value: Mapping[str, Any]) -> str:
    """Hash canonical metadata fields excluding ``content_revision`` itself."""

    if not isinstance(value, Mapping):
        raise TypeError("capability metadata content must be a mapping")
    copied = dict(value)
    if "content_revision" in copied:
        raise ValueError("content revision input must exclude content_revision")
    return hashlib.sha256(_canonical_json(copied)).hexdigest()


def canonical_capability_metadata_mapping(value: object) -> dict[str, Any]:
    """Validate and canonicalize one exact capability metadata record."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("capability metadata must be valid JSON") from exc
    else:
        decoded = value
    if not isinstance(decoded, Mapping) or set(decoded) != CAPABILITY_METADATA_KEYS:
        raise ValueError("capability metadata contains unsupported fields")

    capability_id = _identifier(decoded["capability_id"], label="capability_id")
    module_id = _identifier(decoded["module_id"], label="module_id")
    name = decoded["name"]
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name.strip().encode("utf-8")) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ValueError("capability metadata name is invalid")
    name = name.strip()
    if contains_secret_like(name):
        raise ValueError("capability metadata name contains a secret-like marker")
    primary_intent = _choice(decoded["primary_intent"], _INTENTS, label="primary_intent")
    intent_tags = _string_set(decoded["intent_tags"], _INTENTS, label="intent_tags", maximum=8)
    if not intent_tags or primary_intent not in intent_tags:
        raise ValueError("primary_intent must be included in intent_tags")
    risk = _choice(decoded["risk"], _RISKS, label="risk")
    minimum_rbac = _choice(decoded["minimum_rbac"], _RBAC_LEVELS, label="minimum_rbac")
    source_provenance = _choice(
        decoded["source_provenance"],
        _PROVENANCE,
        label="source_provenance",
    )
    bindings = _identifier_set(decoded["bindings"], label="bindings", maximum=4)
    surface_bindings = _identifier_set(
        decoded["surface_bindings"],
        label="surface_bindings",
        maximum=16,
    )
    if bindings not in {(), (_WEB_SEARCH_TOOL_ID,)}:
        raise ValueError("Stage 1 metadata bindings may contain only web_search")
    if bindings and capability_id != _WEB_SEARCH_CAPABILITY_ID:
        raise ValueError("web_search metadata must use the canonical capability")
    if not bindings and not surface_bindings:
        raise ValueError("capability metadata requires a code-owned binding")

    canonical_without_revision = {
        "bindings": list(bindings),
        "capability_id": capability_id,
        "intent_tags": list(intent_tags),
        "minimum_rbac": minimum_rbac,
        "module_id": module_id,
        "name": name,
        "primary_intent": primary_intent,
        "risk": risk,
        "source_provenance": source_provenance,
        "surface_bindings": list(surface_bindings),
    }
    content_revision = _sha256(decoded["content_revision"], label="content_revision")
    if content_revision != capability_metadata_content_revision(canonical_without_revision):
        raise ValueError("capability metadata content_revision does not match its content")
    canonical = {
        **canonical_without_revision,
        "content_revision": content_revision,
    }
    encoded = _canonical_json(canonical)
    if len(encoded) > MAX_CAPABILITY_METADATA_ENTRY_BYTES:
        raise ValueError("capability metadata entry exceeds the UTF-8 byte limit")
    return canonical


def canonical_capability_metadata_json(value: object) -> str:
    return _canonical_json(canonical_capability_metadata_mapping(value)).decode("utf-8")


def canonical_capability_metadata_list(values: object) -> tuple[str, ...]:
    """Validate and canonicalize the bounded metadata transport as one list."""

    if not isinstance(values, (list, tuple)):
        raise TypeError("capability metadata transport must be a list or tuple")
    if len(values) > MAX_CAPABILITY_METADATA_ITEMS:
        raise ValueError("capability metadata transport contains too many entries")
    mappings = tuple(canonical_capability_metadata_mapping(value) for value in values)
    capability_ids = tuple(str(value["capability_id"]) for value in mappings)
    if len(capability_ids) != len(set(capability_ids)):
        raise ValueError("capability metadata transport contains duplicate capabilities")
    encoded = _canonical_json(list(mappings))
    if len(encoded) > MAX_CAPABILITY_METADATA_TRANSPORT_BYTES:
        raise ValueError("capability metadata transport exceeds the UTF-8 byte limit")
    return tuple(_canonical_json(value).decode("utf-8") for value in mappings)


def capability_metadata_list_digest(values: object) -> str:
    """Digest the exact canonical candidate order used by one Context build."""

    canonical = canonical_capability_metadata_list(values)
    mappings = [json.loads(value) for value in canonical]
    return hashlib.sha256(_canonical_json(mappings)).hexdigest()


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("capability metadata must be canonical JSON data") from exc


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if not normalized or len(normalized.encode("utf-8")) > 256:
        raise ValueError(f"{label} is invalid")
    if any(character not in _IDENTIFIER_ASCII for character in normalized):
        raise ValueError(f"{label} contains an invalid character")
    if contains_secret_like(normalized):
        raise ValueError(f"{label} contains a secret-like marker")
    return normalized


def _choice(value: object, allowed: frozenset[str], *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise ValueError(f"{label} is invalid")
    return normalized


def _string_set(
    value: object,
    allowed: frozenset[str],
    *,
    label: str,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be a JSON array")
    items = tuple(sorted({_choice(item, allowed, label=label) for item in value}))
    if len(items) != len(value) or len(items) > maximum:
        raise ValueError(f"{label} contains invalid or duplicate entries")
    return items


def _identifier_set(
    value: object,
    *,
    label: str,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be a JSON array")
    items = tuple(sorted({_identifier(item, label=label) for item in value}))
    if len(items) != len(value) or len(items) > maximum:
        raise ValueError(f"{label} contains invalid or duplicate entries")
    return items


def _sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{label} must be a SHA-256 hex revision")
    return normalized


__all__ = [
    "CAPABILITY_METADATA_KEYS",
    "CAPABILITY_METADATA_INTENTS",
    "CAPABILITY_METADATA_PROVENANCE",
    "CAPABILITY_METADATA_RBAC_LEVELS",
    "CAPABILITY_METADATA_RISKS",
    "MAX_CAPABILITY_METADATA_ENTRY_BYTES",
    "MAX_CAPABILITY_METADATA_ITEMS",
    "MAX_CAPABILITY_METADATA_TRANSPORT_BYTES",
    "canonical_capability_metadata_json",
    "canonical_capability_metadata_list",
    "canonical_capability_metadata_mapping",
    "capability_metadata_list_digest",
    "capability_metadata_content_revision",
]
