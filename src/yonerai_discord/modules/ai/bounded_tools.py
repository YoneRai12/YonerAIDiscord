"""Stage 1 bounded model-tool selection and execution authorization.

Capability metadata is useful for deterministic candidate retrieval, but it is
never execution authority.  Only an explicitly requested, sealed
``BoundedToolSet`` may expose the single fixed ``web_search`` provider tool.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from yonerai_discord.capability_metadata_contract import (
    MAX_CAPABILITY_METADATA_ITEMS,
    MAX_CAPABILITY_METADATA_TRANSPORT_BYTES,
    canonical_capability_metadata_json,
    canonical_capability_metadata_mapping,
    capability_metadata_content_revision,
    capability_metadata_list_digest,
)
from yonerai_discord.capabilities import (
    ACTION_CAPABILITIES,
    COMMAND_CAPABILITIES,
    EVENT_CAPABILITIES,
    MODEL_TOOL_CAPABILITY_BINDINGS,
    OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID,
)


MAX_CAPABILITY_CANDIDATES = MAX_CAPABILITY_METADATA_ITEMS
MAX_EFFECTIVE_MODEL_TOOLS = 1
MAX_TOOLSET_TTL_SECONDS = 30.0
MAX_TOOL_SCHEMA_BYTES = 8 * 1024
MAX_TOOL_METADATA_BYTES = MAX_CAPABILITY_METADATA_TRANSPORT_BYTES
MAX_TOOL_ARGUMENT_BYTES = 16 * 1024
MAX_JSON_DEPTH = 6
MAX_JSON_PROPERTIES = 64
MAX_STATIC_CATALOG_ENTRIES = 4_096
MAX_CAPABILITY_QUERY_CHARACTERS = 4_000
WEB_SEARCH_TOOL_ID = "web_search"

_EMPTY_REVISION = hashlib.sha256(b"[]").hexdigest()
_IDENTIFIER_ASCII = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._:-/")
_LEXICAL_TOKEN_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)


class BoundedIntent(StrEnum):
    CONVERSATION = "conversation"
    KNOWLEDGE = "knowledge"
    WEB_RESEARCH = "web_research"
    CODE = "code"
    SITE = "site"
    MEDIA = "media"
    MUSIC = "music"
    MEMORY = "memory"
    MODERATION = "moderation"
    SELF_EVOLUTION = "self_evolution"
    UNKNOWN = "unknown"


class BoundedComplexity(StrEnum):
    TINY = "tiny"
    STANDARD = "standard"
    COMPLEX = "complex"


class CapabilityRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class MinimumRBAC(StrEnum):
    EVERYONE = "everyone"
    TRUSTED = "trusted"
    MODERATOR = "moderator"
    GUILD_ADMIN = "guild_admin"
    GUILD_OWNER = "guild_owner"
    BOT_OWNER = "bot_owner"


class CapabilityProvenance(StrEnum):
    CANONICAL_REGISTRY = "canonical_registry"
    RUNTIME_MANIFEST = "runtime_manifest"


class ToolAuthorizationCode(StrEnum):
    ALLOWED = "allowed"
    AUTHORIZATION_MISSING = "authorization_missing"
    DIGEST_MISMATCH = "digest_mismatch"
    EXPIRED = "expired"
    CLOCK_INVALID = "clock_invalid"
    SCOPE_CHANGED = "scope_changed"
    INTENT_CHANGED = "intent_changed"
    COMPLEXITY_CHANGED = "complexity_changed"
    CAPABILITY_REVISION_CHANGED = "capability_revision_changed"
    PROVIDER_REVISION_CHANGED = "provider_revision_changed"
    PROVIDER_BINDING_CHANGED = "provider_binding_changed"
    TOOL_CAPABILITY_DENIED = "tool_capability_denied"
    DM_WEB_SEARCH_DENIED = "dm_web_search_denied"


@dataclass(frozen=True, slots=True)
class ToolScopeBinding:
    """Identity/surface binding. Raw Discord IDs are intentionally absent from repr."""

    guild_id: int | None = field(repr=False)
    channel_id: int | None = field(repr=False)
    user_id: int = field(repr=False)

    def __post_init__(self) -> None:
        if self.channel_id is not None:
            _positive_id(self.channel_id, label="channel_id")
        _positive_id(self.user_id, label="user_id")
        if self.guild_id is not None:
            _positive_id(self.guild_id, label="guild_id")

    @property
    def is_dm(self) -> bool:
        return self.guild_id is None

    def canonical_mapping(self) -> dict[str, int | None]:
        return {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
        }


@dataclass(frozen=True, slots=True)
class StaticCapabilityMetadata:
    """Code-owned, non-executable capability metadata used only for retrieval."""

    capability_id: str
    module_id: str
    name: str = field(repr=False)
    primary_intent: BoundedIntent | str
    intent_tags: tuple[BoundedIntent | str, ...]
    risk: CapabilityRisk | str
    minimum_rbac: MinimumRBAC | str
    source_provenance: CapabilityProvenance | str = field(repr=False)
    content_revision: str
    bindings: tuple[str, ...] = ()
    surface_bindings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("capability_id", "module_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), label=name))
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string")
        if len(self.name.encode("utf-8")) > 512:
            raise ValueError("name exceeds the metadata byte limit")
        object.__setattr__(self, "name", self.name.strip())
        primary_intent = BoundedIntent(self.primary_intent)
        tags = tuple(sorted({BoundedIntent(item) for item in self.intent_tags}, key=lambda item: item.value))
        bindings = tuple(sorted({_model_tool_identifier(item) for item in self.bindings}))
        surface_bindings = tuple(sorted({_identifier(item, label="surface binding") for item in self.surface_bindings}))
        if not tags or len(tags) > 8 or len(tags) != len(set(tags)):
            raise ValueError("intent_tags must contain one to eight unique entries")
        if primary_intent not in tags:
            raise ValueError("primary_intent must be included in intent_tags")
        if len(bindings) > 4 or len(bindings) != len(set(bindings)):
            raise ValueError("bindings must contain at most four unique entries")
        if not surface_bindings and not bindings:
            raise ValueError("static capability metadata must have a code-owned binding")
        if len(surface_bindings) > 16:
            raise ValueError("surface_bindings must contain at most sixteen unique entries")
        object.__setattr__(self, "primary_intent", primary_intent)
        object.__setattr__(self, "intent_tags", tags)
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "surface_bindings", surface_bindings)
        if bindings and (
            bindings != (WEB_SEARCH_TOOL_ID,) or self.capability_id != OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID
        ):
            raise ValueError("web_search metadata must use the canonical capability binding")
        object.__setattr__(self, "risk", CapabilityRisk(self.risk))
        object.__setattr__(self, "minimum_rbac", MinimumRBAC(self.minimum_rbac))
        object.__setattr__(self, "source_provenance", CapabilityProvenance(self.source_provenance))
        content = {
            "bindings": list(bindings),
            "capability_id": self.capability_id,
            "intent_tags": [item.value for item in tags],
            "minimum_rbac": self.minimum_rbac.value,
            "module_id": self.module_id,
            "name": self.name,
            "primary_intent": primary_intent.value,
            "risk": self.risk.value,
            "source_provenance": self.source_provenance.value,
            "surface_bindings": list(surface_bindings),
        }
        revision = _sha256_revision(self.content_revision, label="content_revision")
        if revision != capability_metadata_content_revision(content):
            raise ValueError("capability metadata content_revision does not match its content")
        object.__setattr__(self, "content_revision", revision)
        canonical_capability_metadata_mapping(
            {
                **content,
                "content_revision": revision,
            }
        )

    def canonical_mapping(self) -> dict[str, Any]:
        return {
            "bindings": list(self.bindings),
            "capability_id": self.capability_id,
            "content_revision": self.content_revision,
            "intent_tags": [item.value for item in self.intent_tags],
            "minimum_rbac": self.minimum_rbac.value,
            "module_id": self.module_id,
            "name": self.name,
            "primary_intent": self.primary_intent.value,
            "risk": self.risk.value,
            "source_provenance": self.source_provenance.value,
            "surface_bindings": list(self.surface_bindings),
        }


@dataclass(frozen=True, slots=True)
class StaticCapabilitySnapshot:
    """Deterministic snapshot of static registry/runtime-manifest metadata."""

    entries: tuple[StaticCapabilityMetadata, ...] = ()
    content_revision: str = ""

    def __post_init__(self) -> None:
        entries = tuple(sorted(self.entries, key=lambda item: item.capability_id))
        if len(entries) > MAX_STATIC_CATALOG_ENTRIES:
            raise ValueError("static capability catalog contains too many entries")
        if len(entries) != len({item.capability_id for item in entries}):
            raise ValueError("capability metadata IDs must be unique")
        revision = canonical_revision([item.canonical_mapping() for item in entries])
        if self.content_revision and self.content_revision != revision:
            raise ValueError("capability metadata revision does not match its content")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "content_revision", revision)

    def retrieve(self, intent: str, *, limit: int = MAX_CAPABILITY_CANDIDATES) -> tuple[StaticCapabilityMetadata, ...]:
        normalized = BoundedIntent(intent)
        _validate_candidate_limit(limit)
        if normalized is BoundedIntent.UNKNOWN:
            return ()
        result = tuple(
            sorted(
                (item for item in self.entries if normalized in item.intent_tags),
                key=lambda item: _candidate_rank(item, normalized),
            )[:limit]
        )
        _validate_retrieved_candidates(result)
        return result

    def retrieve_authorized(
        self,
        intent: str,
        *,
        query: str,
        eligible_capability_ids: Iterable[str],
        limit: int = MAX_CAPABILITY_CANDIDATES,
    ) -> tuple[StaticCapabilityMetadata, ...]:
        """Return the actor-eligible lexical top-K without retaining ``query``.

        Eligibility is an explicit input from the authoritative policy layer.
        Unknown, duplicate, or malformed IDs fail closed instead of silently
        widening the candidate set.  Intent and eligibility are both applied
        before the bounded lexical ranking.
        """

        normalized_intent = BoundedIntent(intent)
        _validate_candidate_limit(limit)
        normalized_query = _normalize_capability_query(query)
        eligible_ids = _eligible_capability_ids(
            eligible_capability_ids,
            known_ids=frozenset(item.capability_id for item in self.entries),
        )
        if normalized_intent is BoundedIntent.UNKNOWN or not eligible_ids:
            return ()
        result = tuple(
            sorted(
                (
                    item
                    for item in self.entries
                    if item.capability_id in eligible_ids and normalized_intent in item.intent_tags
                ),
                key=lambda item: _lexical_candidate_rank(
                    item,
                    normalized_intent,
                    normalized_query,
                ),
            )[:limit]
        )
        _validate_retrieved_candidates(result)
        return result


def _validate_retrieved_candidates(
    candidates: tuple[StaticCapabilityMetadata, ...],
) -> None:
    validate_json_limits(
        [item.canonical_mapping() for item in candidates],
        label="retrieved capability metadata",
        max_bytes=MAX_TOOL_METADATA_BYTES,
        max_properties=MAX_JSON_PROPERTIES * MAX_CAPABILITY_CANDIDATES,
    )


@dataclass(frozen=True, slots=True)
class BoundedToolSet:
    """Candidate metadata plus the explicit 0/1 provider-tool allowlist."""

    scope: ToolScopeBinding = field(repr=False)
    intent: BoundedIntent | str
    complexity: BoundedComplexity | str
    candidates: tuple[StaticCapabilityMetadata, ...]
    effective_tools: tuple[str, ...]
    max_tool_calls: int
    tool_capability_bindings: tuple[tuple[str, str], ...]
    capability_catalog_revision: str
    provider_catalog_revision: str
    issued_at: float
    expires_at: float
    digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ToolScopeBinding):
            raise TypeError("scope must be ToolScopeBinding")
        object.__setattr__(self, "intent", BoundedIntent(self.intent))
        object.__setattr__(self, "complexity", BoundedComplexity(self.complexity))
        candidates = tuple(
            sorted(
                self.candidates,
                key=lambda item: _candidate_rank(item, self.intent),
            )
        )
        if len(candidates) > MAX_CAPABILITY_CANDIDATES:
            raise ValueError("capability candidates must contain at most eight entries")
        if any(not isinstance(item, StaticCapabilityMetadata) for item in candidates):
            raise TypeError("candidates must contain StaticCapabilityMetadata")
        if len(candidates) != len({item.capability_id for item in candidates}):
            raise ValueError("candidate capability IDs must be unique")
        validate_json_limits(
            [item.canonical_mapping() for item in candidates],
            label="bounded capability metadata",
            max_bytes=MAX_TOOL_METADATA_BYTES,
            max_properties=MAX_JSON_PROPERTIES * MAX_CAPABILITY_CANDIDATES,
        )
        tools = tuple(self.effective_tools)
        if tools not in {(), (WEB_SEARCH_TOOL_ID,)}:
            raise ValueError("effective tools must be empty or exactly web_search")
        if tools and self.intent is not BoundedIntent.WEB_RESEARCH:
            raise ValueError("web_search is allowed only for the web_research intent")
        if isinstance(self.max_tool_calls, bool) or self.max_tool_calls != len(tools):
            raise ValueError("max_tool_calls must exactly match the 0/1 effective toolset")
        if tools and (self.scope.is_dm or self.scope.channel_id is None):
            raise ValueError("DM web search is not supported")
        bindings = tuple(
            sorted(
                (
                    _model_tool_identifier(tool_id),
                    _identifier(capability_id, label="tool capability ID"),
                )
                for tool_id, capability_id in self.tool_capability_bindings
            )
        )
        if tools:
            expected = tuple(
                (WEB_SEARCH_TOOL_ID, OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID)
                for item in candidates
                if (WEB_SEARCH_TOOL_ID in item.bindings and item.capability_id == OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID)
            )
            if len(expected) != 1 or bindings != expected:
                raise ValueError("web_search must bind exactly one static capability")
        elif bindings:
            raise ValueError("empty toolsets cannot contain execution capability bindings")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "effective_tools", tools)
        object.__setattr__(self, "tool_capability_bindings", bindings)
        object.__setattr__(
            self,
            "capability_catalog_revision",
            _sha256_revision(self.capability_catalog_revision, label="capability_catalog_revision"),
        )
        object.__setattr__(
            self,
            "provider_catalog_revision",
            _sha256_revision(self.provider_catalog_revision, label="provider_catalog_revision"),
        )
        issued_at = _finite_time(self.issued_at, label="issued_at")
        expires_at = _finite_time(self.expires_at, label="expires_at")
        assert issued_at is not None and expires_at is not None
        if expires_at <= issued_at or expires_at > issued_at + MAX_TOOLSET_TTL_SECONDS:
            raise ValueError("toolset TTL must be greater than zero and at most 30 seconds")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        canonical_digest = canonical_revision(self._canonical_mapping(include_digest=False))
        if self.digest and self.digest != canonical_digest:
            raise ValueError("toolset digest does not match its content")
        object.__setattr__(self, "digest", canonical_digest)

    @classmethod
    def issue(
        cls,
        *,
        scope: ToolScopeBinding,
        intent: str,
        complexity: str,
        snapshot: StaticCapabilitySnapshot,
        provider_catalog_revision: str,
        web_search: bool,
        issued_at: float,
        ttl_seconds: float = MAX_TOOLSET_TTL_SECONDS,
        query: str | None = None,
        eligible_capability_ids: Iterable[str] | None = None,
    ) -> BoundedToolSet:
        if type(web_search) is not bool:
            raise TypeError("web_search must be a boolean")
        ttl = _finite_time(ttl_seconds, label="ttl_seconds")
        if not 0 < ttl <= MAX_TOOLSET_TTL_SECONDS:
            raise ValueError("ttl_seconds must be greater than zero and at most 30")
        normalized_intent = BoundedIntent(intent)
        if (query is None) is not (eligible_capability_ids is None):
            raise ValueError("query and eligible_capability_ids must be provided together")
        candidates = (
            snapshot.retrieve(normalized_intent)
            if query is None
            else snapshot.retrieve_authorized(
                normalized_intent,
                query=query,
                eligible_capability_ids=eligible_capability_ids,
            )
        )
        tools = (WEB_SEARCH_TOOL_ID,) if web_search else ()
        if tools and not any(
            item.capability_id == OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID and WEB_SEARCH_TOOL_ID in item.bindings
            for item in candidates
        ):
            raise ValueError("web_search capability is not eligible for this request")
        bindings = (
            tuple(
                (WEB_SEARCH_TOOL_ID, item.capability_id) for item in candidates if WEB_SEARCH_TOOL_ID in item.bindings
            )
            if tools
            else ()
        )
        return cls(
            scope=scope,
            intent=normalized_intent,
            complexity=complexity,
            candidates=candidates,
            effective_tools=tools,
            max_tool_calls=len(tools),
            tool_capability_bindings=bindings,
            capability_catalog_revision=snapshot.content_revision,
            provider_catalog_revision=provider_catalog_revision,
            issued_at=issued_at,
            expires_at=float(issued_at) + ttl,
        )

    def _canonical_mapping(self, *, include_digest: bool) -> dict[str, Any]:
        value = {
            "candidates": [item.canonical_mapping() for item in self.candidates],
            "capability_catalog_revision": self.capability_catalog_revision,
            "complexity": self.complexity.value,
            "effective_tools": list(self.effective_tools),
            "expires_at": self.expires_at,
            "intent": self.intent.value,
            "issued_at": self.issued_at,
            "max_tool_calls": self.max_tool_calls,
            "provider_catalog_revision": self.provider_catalog_revision,
            "scope": self.scope.canonical_mapping(),
            "tool_capability_bindings": [list(item) for item in self.tool_capability_bindings],
        }
        if include_digest:
            value["digest"] = self.digest
        return value


@dataclass(frozen=True, slots=True)
class ToolExecutionAuthorization:
    """Execution seal. Candidate metadata alone can never construct this authority."""

    toolset_digest: str
    provider_catalog_revision: str
    provider_id: str
    model_alias: str
    issued_at: float
    expires_at: float
    digest: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "toolset_digest", _sha256_revision(self.toolset_digest, label="toolset_digest"))
        object.__setattr__(
            self,
            "provider_catalog_revision",
            _sha256_revision(self.provider_catalog_revision, label="provider_catalog_revision"),
        )
        object.__setattr__(self, "provider_id", _identifier(self.provider_id, label="provider_id"))
        object.__setattr__(self, "model_alias", _identifier(self.model_alias, label="model_alias"))
        issued_at = _finite_time(self.issued_at, label="issued_at")
        expires_at = _finite_time(self.expires_at, label="expires_at")
        assert issued_at is not None and expires_at is not None
        if expires_at <= issued_at or expires_at > issued_at + MAX_TOOLSET_TTL_SECONDS:
            raise ValueError("authorization TTL must be greater than zero and at most 30 seconds")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        canonical = canonical_revision(self._canonical_mapping())
        if self.digest and self.digest != canonical:
            raise ValueError("authorization digest does not match its content")
        object.__setattr__(self, "digest", canonical)

    @classmethod
    def seal(
        cls,
        toolset: BoundedToolSet,
        *,
        provider_id: str,
        model_alias: str,
        issued_at: float,
    ) -> ToolExecutionAuthorization:
        if not toolset.effective_tools:
            raise ValueError("an empty toolset does not need execution authorization")
        now = _finite_time(issued_at, label="issued_at")
        if now < toolset.issued_at or now >= toolset.expires_at:
            raise ValueError("cannot seal an inactive toolset")
        return cls(
            toolset_digest=toolset.digest,
            provider_catalog_revision=toolset.provider_catalog_revision,
            provider_id=provider_id,
            model_alias=model_alias,
            issued_at=now,
            expires_at=toolset.expires_at,
        )

    def _canonical_mapping(self) -> dict[str, Any]:
        return {
            "expires_at": self.expires_at,
            "issued_at": self.issued_at,
            "model_alias": self.model_alias,
            "provider_catalog_revision": self.provider_catalog_revision,
            "provider_id": self.provider_id,
            "toolset_digest": self.toolset_digest,
        }


@dataclass(frozen=True, slots=True)
class ToolAuthorizationDecision:
    allowed: bool
    code: ToolAuthorizationCode


def tool_authorization_current(
    toolset: BoundedToolSet,
    authorization: ToolExecutionAuthorization | None,
    *,
    scope: ToolScopeBinding,
    intent: str,
    complexity: str,
    capability_catalog_revision: str,
    provider_catalog_revision: str,
    provider_id: str,
    model_alias: str,
    now: float,
    capability_authorizations: Mapping[str, bool],
) -> ToolAuthorizationDecision:
    """Validate every non-content binding without logging raw IDs or schemas."""

    current_time = _finite_time(now, label="now", fail_closed=True)
    if current_time is None:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.CLOCK_INVALID)
    if toolset.scope != scope:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.SCOPE_CHANGED)
    try:
        current_intent = BoundedIntent(intent)
    except (TypeError, ValueError):
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.INTENT_CHANGED)
    if toolset.intent is not current_intent:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.INTENT_CHANGED)
    try:
        current_complexity = BoundedComplexity(complexity)
    except (TypeError, ValueError):
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.COMPLEXITY_CHANGED)
    if toolset.complexity is not current_complexity:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.COMPLEXITY_CHANGED)
    if toolset.capability_catalog_revision != capability_catalog_revision:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.CAPABILITY_REVISION_CHANGED)
    if toolset.provider_catalog_revision != provider_catalog_revision:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.PROVIDER_REVISION_CHANGED)
    if current_time < toolset.issued_at or current_time >= toolset.expires_at:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.EXPIRED)
    if not toolset.effective_tools:
        if authorization is not None:
            return ToolAuthorizationDecision(False, ToolAuthorizationCode.DIGEST_MISMATCH)
        return ToolAuthorizationDecision(True, ToolAuthorizationCode.ALLOWED)
    if toolset.scope.is_dm:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.DM_WEB_SEARCH_DENIED)
    if authorization is None:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.AUTHORIZATION_MISSING)
    if authorization.provider_catalog_revision != provider_catalog_revision:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.PROVIDER_REVISION_CHANGED)
    if authorization.provider_id != provider_id or authorization.model_alias != model_alias:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.PROVIDER_BINDING_CHANGED)
    if authorization.toolset_digest != toolset.digest:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.DIGEST_MISMATCH)
    if current_time < authorization.issued_at:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.CLOCK_INVALID)
    if current_time >= authorization.expires_at:
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.EXPIRED)
    if set(capability_authorizations) != {
        capability_id for _, capability_id in toolset.tool_capability_bindings
    } or any(
        capability_authorizations[capability_id] is not True for _, capability_id in toolset.tool_capability_bindings
    ):
        return ToolAuthorizationDecision(False, ToolAuthorizationCode.TOOL_CAPABILITY_DENIED)
    return ToolAuthorizationDecision(True, ToolAuthorizationCode.ALLOWED)


def validate_json_limits(
    value: Any,
    *,
    label: str,
    max_bytes: int,
    max_depth: int = MAX_JSON_DEPTH,
    max_properties: int = MAX_JSON_PROPERTIES,
) -> None:
    """Apply the Stage 1 UTF-8/depth/property limits without accepting dynamic schemas."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON data") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds the UTF-8 byte limit")
    properties = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal properties
        if depth > max_depth:
            raise ValueError(f"{label} exceeds the depth limit")
        if isinstance(item, Mapping):
            properties += len(item)
            if properties > max_properties:
                raise ValueError(f"{label} exceeds the property limit")
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} object keys must be strings")
                walk(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child, depth + 1)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError(f"{label} contains unsupported JSON data")

    walk(value, 1)


def canonical_revision(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_static_capability_snapshot(
    registry: object,
) -> StaticCapabilitySnapshot:
    """Project only implemented, code-connected surfaces into the Stage 1 index."""

    capability_getter = getattr(registry, "capability", None)
    if not callable(capability_getter):
        raise TypeError("registry must expose capability(capability_id)")
    surface_bindings_by_id: dict[str, set[str]] = {}
    for path, capability_id in COMMAND_CAPABILITIES.items():
        surface_bindings_by_id.setdefault(capability_id, set()).add(_surface_binding("command", path))
    for event_name, capability_id in EVENT_CAPABILITIES.items():
        surface_bindings_by_id.setdefault(capability_id, set()).add(_surface_binding("event", event_name))
    for action_path, capability_id in ACTION_CAPABILITIES.items():
        surface_bindings_by_id.setdefault(capability_id, set()).add(_surface_binding("action", action_path))
    model_tools_by_id: dict[str, set[str]] = {}
    for tool_id, capability_id in MODEL_TOOL_CAPABILITY_BINDINGS.items():
        model_tools_by_id.setdefault(capability_id, set()).add(_model_tool_identifier(tool_id))
    connected_ids = tuple(sorted(set(surface_bindings_by_id) | set(model_tools_by_id)))
    entries: list[StaticCapabilityMetadata] = []
    for capability_id in connected_ids:
        spec = capability_getter(capability_id)
        if getattr(spec, "implemented", None) is not True:
            continue
        surface_bindings = tuple(sorted(surface_bindings_by_id.get(capability_id, ())))
        model_tool_bindings = tuple(sorted(model_tools_by_id.get(capability_id, ())))
        primary_intent, intent_tags = _capability_intents(
            module_id=str(getattr(spec, "module_id")),
            surface_bindings=surface_bindings,
            model_tool_bindings=model_tool_bindings,
            risk_name=str(getattr(getattr(spec, "risk"), "name")).lower(),
        )
        raw = {
            "bindings": list(model_tool_bindings),
            "capability_id": str(getattr(spec, "capability_id")),
            "intent_tags": [item.value for item in intent_tags],
            "minimum_rbac": str(getattr(getattr(spec, "safety_floor"), "name")).lower(),
            "module_id": str(getattr(spec, "module_id")),
            "name": str(getattr(spec, "name") or getattr(spec, "capability_id")),
            "primary_intent": primary_intent.value,
            "risk": str(getattr(getattr(spec, "risk"), "name")).lower(),
            "source_provenance": (
                CapabilityProvenance.RUNTIME_MANIFEST
                if getattr(spec, "source_state", None) == "runtime"
                else CapabilityProvenance.CANONICAL_REGISTRY
            ),
            "surface_bindings": list(surface_bindings),
        }
        entries.append(
            StaticCapabilityMetadata(
                capability_id=raw["capability_id"],
                module_id=raw["module_id"],
                name=raw["name"],
                primary_intent=raw["primary_intent"],
                intent_tags=tuple(raw["intent_tags"]),
                risk=raw["risk"],
                minimum_rbac=raw["minimum_rbac"],
                source_provenance=raw["source_provenance"],
                content_revision=canonical_revision(raw),
                bindings=tuple(raw["bindings"]),
                surface_bindings=tuple(raw["surface_bindings"]),
            )
        )
    return StaticCapabilitySnapshot(tuple(entries))


def fixed_web_search_payload_fragment(toolset: BoundedToolSet) -> dict[str, Any]:
    """Return the only Stage 1 provider schema after mandatory shape validation."""

    if (
        toolset.effective_tools != (WEB_SEARCH_TOOL_ID,)
        or toolset.max_tool_calls != 1
        or toolset.tool_capability_bindings != ((WEB_SEARCH_TOOL_ID, OPENAI_PAID_WEB_SEARCH_CAPABILITY_ID),)
    ):
        raise ValueError("a canonical bounded web_search toolset is required")
    tools = [{"type": "web_search", "search_context_size": "medium"}]
    validate_json_limits(
        tools,
        label="fixed web_search provider schema",
        max_bytes=MAX_TOOL_SCHEMA_BYTES,
    )
    # Stage 1 delegates the fixed provider-native search and accepts no
    # BOT-side arbitrary tool arguments or generic executor calls.
    validate_stage1_tool_arguments({})
    return {
        "tools": tools,
        "tool_choice": "required",
        "include": ["web_search_call.action.sources"],
        "max_tool_calls": 1,
    }


def capability_metadata_transport(toolset: BoundedToolSet) -> tuple[str, ...]:
    """Serialize only the already-bounded static candidates for ContextBuilder."""

    return tuple(canonical_capability_metadata_json(item.canonical_mapping()) for item in toolset.candidates)


def capability_metadata_digest(toolset: BoundedToolSet) -> str:
    """Bind Context authorization to the exact canonical candidate transport."""

    if not isinstance(toolset, BoundedToolSet):
        raise TypeError("toolset must be a BoundedToolSet")
    return capability_metadata_list_digest(capability_metadata_transport(toolset))


def validate_stage1_tool_arguments(arguments: Mapping[str, Any]) -> None:
    if not isinstance(arguments, Mapping) or arguments:
        raise ValueError("Stage 1 does not accept BOT-side tool arguments")
    validate_json_limits(
        dict(arguments),
        label="Stage 1 tool arguments",
        max_bytes=MAX_TOOL_ARGUMENT_BYTES,
    )


def _positive_id(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if not normalized or len(normalized.encode("utf-8")) > 256:
        raise ValueError(f"{label} is invalid")
    if any(character not in _IDENTIFIER_ASCII for character in normalized):
        raise ValueError(f"{label} contains an invalid character")
    return normalized


def _model_tool_identifier(value: object) -> str:
    normalized = _identifier(value, label="model tool")
    if normalized != WEB_SEARCH_TOOL_ID:
        raise ValueError("Stage 1 supports only the web_search model tool")
    return normalized


def _surface_binding(kind: str, value: object) -> str:
    normalized_kind = _identifier(kind, label="surface kind")
    if not isinstance(value, str):
        raise TypeError("surface name must be a string")
    normalized_name = ".".join(value.strip().lower().split())
    return _identifier(
        f"{normalized_kind}:{normalized_name}",
        label="surface binding",
    )


def _capability_intents(
    *,
    module_id: str,
    surface_bindings: tuple[str, ...],
    model_tool_bindings: tuple[str, ...],
    risk_name: str,
) -> tuple[BoundedIntent, tuple[BoundedIntent, ...]]:
    """Assign code-owned tags from module/surface metadata, never request text."""

    module = _identifier(module_id, label="module_id")
    roots = frozenset(binding.split(":", 1)[1].split(".", 1)[0] for binding in surface_bindings if ":" in binding)
    tags: set[BoundedIntent] = set()
    primary = BoundedIntent.UNKNOWN
    if WEB_SEARCH_TOOL_ID in model_tool_bindings:
        primary = BoundedIntent.WEB_RESEARCH
        tags.add(BoundedIntent.WEB_RESEARCH)
    elif "music" in roots or module == "media.music":
        primary = BoundedIntent.MUSIC
        tags.update((BoundedIntent.MUSIC, BoundedIntent.MEDIA))
    elif "voice" in roots or module in {"media.voice", "media.audio-core"}:
        primary = BoundedIntent.MEDIA
        tags.add(BoundedIntent.MEDIA)
    elif "memory" in roots or module.startswith("intelligence.personal-memory"):
        primary = BoundedIntent.MEMORY
        tags.add(BoundedIntent.MEMORY)
    elif "site" in roots or module.startswith("publishing.site-host"):
        primary = BoundedIntent.SITE
        tags.add(BoundedIntent.SITE)
    elif roots.intersection({"mod", "server", "automod", "verify"}) or module.startswith(("moderation.", "security.")):
        primary = BoundedIntent.MODERATION
        tags.add(BoundedIntent.MODERATION)
    elif "evolution" in roots:
        primary = BoundedIntent.SELF_EVOLUTION
        tags.update((BoundedIntent.SELF_EVOLUTION, BoundedIntent.CODE))
    elif roots.intersection({"weather", "warning", "holiday", "earthquake"}) or module in {
        "operations.public-information",
        "operations.earthquake",
    }:
        primary = BoundedIntent.KNOWLEDGE
        tags.update((BoundedIntent.KNOWLEDGE, BoundedIntent.WEB_RESEARCH))
    elif "ai" in roots or any(binding == "event:ai_mention_message" for binding in surface_bindings):
        primary = BoundedIntent.CONVERSATION
        tags.update((BoundedIntent.CONVERSATION, BoundedIntent.KNOWLEDGE))
        if any(binding in {"command:ai.ask", "event:ai_mention_message"} for binding in surface_bindings):
            tags.update((BoundedIntent.CODE, BoundedIntent.SITE, BoundedIntent.MEDIA))
    elif risk_name == CapabilityRisk.LOW.value:
        primary = BoundedIntent.KNOWLEDGE
        tags.add(BoundedIntent.KNOWLEDGE)
    else:
        tags.add(BoundedIntent.UNKNOWN)
    return primary, tuple(sorted(tags, key=lambda item: item.value))


_RISK_RANK = {
    CapabilityRisk.LOW: 0,
    CapabilityRisk.MEDIUM: 1,
    CapabilityRisk.HIGH: 2,
    CapabilityRisk.CRITICAL: 3,
}
_RBAC_RANK = {
    MinimumRBAC.EVERYONE: 0,
    MinimumRBAC.TRUSTED: 1,
    MinimumRBAC.MODERATOR: 2,
    MinimumRBAC.GUILD_ADMIN: 3,
    MinimumRBAC.GUILD_OWNER: 4,
    MinimumRBAC.BOT_OWNER: 5,
}


def _candidate_rank(
    item: StaticCapabilityMetadata,
    intent: BoundedIntent,
) -> tuple[object, ...]:
    return (
        0 if intent is BoundedIntent.WEB_RESEARCH and WEB_SEARCH_TOOL_ID in item.bindings else 1,
        0 if item.primary_intent is intent else 1,
        _RISK_RANK[item.risk],
        _RBAC_RANK[item.minimum_rbac],
        item.module_id,
        item.surface_bindings,
        item.capability_id,
    )


def _lexical_candidate_rank(
    item: StaticCapabilityMetadata,
    intent: BoundedIntent,
    normalized_query: str,
) -> tuple[object, ...]:
    candidate_text = _normalize_lexical_text(
        " ".join(
            (
                item.capability_id,
                item.module_id,
                item.name,
                item.primary_intent.value,
                *(tag.value for tag in item.intent_tags),
                *item.bindings,
                *item.surface_bindings,
            )
        )
    )
    query_tokens = frozenset(_LEXICAL_TOKEN_PATTERN.findall(normalized_query))
    candidate_tokens = frozenset(_LEXICAL_TOKEN_PATTERN.findall(candidate_text))
    token_overlap = len(query_tokens.intersection(candidate_tokens))
    query_token_hits = sum(1 for token in query_tokens if token and token in candidate_text)
    candidate_token_hits = sum(1 for token in candidate_tokens if len(token) >= 2 and token in normalized_query)
    return (
        0 if intent is BoundedIntent.WEB_RESEARCH and WEB_SEARCH_TOOL_ID in item.bindings else 1,
        0 if normalized_query in candidate_text else 1,
        -token_overlap,
        -query_token_hits,
        -candidate_token_hits,
        *_candidate_rank(item, intent),
    )


def _normalize_capability_query(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("capability query must be a string")
    if not 1 <= len(value) <= MAX_CAPABILITY_QUERY_CHARACTERS:
        raise ValueError("capability query must contain between one and 4000 characters")
    normalized = _normalize_lexical_text(value)
    if not normalized or len(normalized) > MAX_CAPABILITY_QUERY_CHARACTERS:
        raise ValueError("capability query must contain between one and 4000 normalized characters")
    return normalized


def _normalize_lexical_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _eligible_capability_ids(
    values: Iterable[str],
    *,
    known_ids: frozenset[str],
) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError("eligible_capability_ids must be an iterable of capability IDs")
    try:
        materialized = tuple(values)
    except TypeError as exc:
        raise TypeError("eligible_capability_ids must be an iterable of capability IDs") from exc
    if len(materialized) > MAX_STATIC_CATALOG_ENTRIES:
        raise ValueError("eligible_capability_ids contains too many entries")
    normalized = tuple(_identifier(value, label="eligible capability ID") for value in materialized)
    if len(normalized) != len(set(normalized)):
        raise ValueError("eligible capability IDs must be unique")
    if not set(normalized).issubset(known_ids):
        raise ValueError("eligible capability IDs must exist in the static snapshot")
    return frozenset(normalized)


def _validate_candidate_limit(limit: object) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_CAPABILITY_CANDIDATES:
        raise ValueError("candidate limit must be between zero and eight")


def _sha256_revision(value: object, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a SHA-256 hex revision")
    lowered = value.lower()
    if any(character not in "0123456789abcdef" for character in lowered):
        raise ValueError(f"{label} must be a SHA-256 hex revision")
    return lowered


def _finite_time(value: object, *, label: str, fail_closed: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        if fail_closed:
            return None
        raise ValueError(f"{label} must be a finite number")
    return float(value)


EMPTY_CAPABILITY_SNAPSHOT = StaticCapabilitySnapshot()
EMPTY_CATALOG_REVISION = _EMPTY_REVISION


__all__ = [
    "BoundedToolSet",
    "BoundedComplexity",
    "BoundedIntent",
    "CapabilityProvenance",
    "CapabilityRisk",
    "EMPTY_CAPABILITY_SNAPSHOT",
    "EMPTY_CATALOG_REVISION",
    "MAX_CAPABILITY_CANDIDATES",
    "MAX_EFFECTIVE_MODEL_TOOLS",
    "MAX_JSON_DEPTH",
    "MAX_JSON_PROPERTIES",
    "MAX_CAPABILITY_QUERY_CHARACTERS",
    "MAX_STATIC_CATALOG_ENTRIES",
    "MAX_TOOL_ARGUMENT_BYTES",
    "MAX_TOOL_METADATA_BYTES",
    "MAX_TOOL_SCHEMA_BYTES",
    "MAX_TOOLSET_TTL_SECONDS",
    "MinimumRBAC",
    "StaticCapabilityMetadata",
    "StaticCapabilitySnapshot",
    "ToolAuthorizationCode",
    "ToolAuthorizationDecision",
    "ToolExecutionAuthorization",
    "ToolScopeBinding",
    "WEB_SEARCH_TOOL_ID",
    "build_static_capability_snapshot",
    "capability_metadata_transport",
    "capability_metadata_digest",
    "canonical_revision",
    "fixed_web_search_payload_fragment",
    "tool_authorization_current",
    "validate_json_limits",
    "validate_stage1_tool_arguments",
]
