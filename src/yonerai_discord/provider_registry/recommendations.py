from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from .domain import LogicalCapability, ProviderKind, QualityTier, normalize_identifier


RECOMMENDATION_SCHEMA = "yonerai.provider-recommendations.v2"
DEFAULT_RECOMMENDATIONS_RESOURCE = "provider-recommendations.rtx5090.v2.json"
_TIERS = tuple(tier.value for tier in QualityTier)
_MATURITY = frozenset({"stable", "preview", "experimental", "unknown"})
_COMMERCIAL_USE = frozenset({"allowed", "review_required", "not_allowed"})
_PREFERRED_OS = frozenset({"remote", "windows", "wsl2", "linux"})
_VRAM_VALUE_KINDS = frozenset({"not_applicable", "budget", "estimated", "model_card"})
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_STRING_ITEMS = 32
_MAX_STRING_CHARS = 512
_PROMOTION_KEYS = frozenset(
    {
        "live_probe_required",
        "license_review_required",
        "owner_approval_required",
        "api_fallback_requires_remote_consent",
        "preview_requires_owner_policy",
        "active_catalog_unchanged",
    }
)
_PRIMARY_SOURCE_HOSTS = {
    "openai-api": frozenset({"developers.openai.com"}),
    "openai-images-api": frozenset({"developers.openai.com"}),
    "google-gemini-api": frozenset({"ai.google.dev"}),
    "elevenlabs-api": frozenset({"elevenlabs.io"}),
    "cohere-api": frozenset({"docs.cohere.com"}),
    "openai-local": frozenset({"huggingface.co", "github.com"}),
    "qwen-local": frozenset({"huggingface.co", "docs.vllm.ai", "github.com"}),
    "flux-local": frozenset({"huggingface.co"}),
    "qwen-image-local": frozenset({"huggingface.co"}),
    "wan-local": frozenset({"huggingface.co", "github.com"}),
    "ltx-local": frozenset({"github.com", "huggingface.co"}),
    "ace-step-local": frozenset({"github.com", "huggingface.co"}),
    "qwen-tts-local": frozenset({"github.com", "huggingface.co"}),
    "qwen-asr-local": frozenset({"github.com", "huggingface.co"}),
    "qwen-embedding-local": frozenset({"github.com", "huggingface.co"}),
    "qwen-rerank-local": frozenset({"github.com", "huggingface.co"}),
}
_EXPECTED_RECOMMENDATION_GROUPS = frozenset(
    (capability, kind)
    for capability in LogicalCapability
    for kind in ProviderKind
    if not (
        (capability is LogicalCapability.WEB_SEARCH and kind is ProviderKind.API)
        or (capability is LogicalCapability.WEB_SEARCH_PAID and kind is ProviderKind.LOCAL)
    )
)


class RecommendationValidationError(ValueError):
    """The advisory recommendation manifest is not safe to load."""


@dataclass(frozen=True, slots=True)
class RecommendationResources:
    preferred_os: tuple[str, ...]
    engines: tuple[str, ...]
    vram_budget_mb: int | None
    vram_value_kind: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "preferred_os": list(self.preferred_os),
            "engines": list(self.engines),
            "vram_budget_mb": self.vram_budget_mb,
            "vram_value_kind": self.vram_value_kind,
        }


@dataclass(frozen=True, slots=True)
class RecommendationTier:
    provider_id: str
    provider_model: str
    maturity: str
    license_id: str
    commercial_use: str
    checked_at: str
    source_urls: tuple[str, ...]
    resources: RecommendationResources
    constraints: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_model": self.provider_model,
            "maturity": self.maturity,
            "license_id": self.license_id,
            "commercial_use": self.commercial_use,
            "checked_at": self.checked_at,
            "source_urls": list(self.source_urls),
            "resources": self.resources.to_mapping(),
            "constraints": list(self.constraints),
        }


@dataclass(frozen=True, slots=True)
class RecommendationGroup:
    capability: LogicalCapability
    kind: ProviderKind
    active: bool
    probe_required: bool
    tiers: tuple[tuple[QualityTier, RecommendationTier], ...]

    def tier(self, value: QualityTier | str) -> RecommendationTier:
        wanted = QualityTier(value)
        return dict(self.tiers)[wanted]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "capability": self.capability.value,
            "kind": self.kind.value,
            "active": self.active,
            "probe_required": self.probe_required,
            "tiers": {tier.value: recommendation.to_mapping() for tier, recommendation in self.tiers},
        }


@dataclass(frozen=True, slots=True)
class ProviderRecommendations:
    schema: str
    advisory_only: bool
    active: bool
    checked_at: str
    hardware_profile: Mapping[str, Any]
    tier_mapping: Mapping[str, str]
    promotion_policy: Mapping[str, bool]
    candidates: tuple[RecommendationGroup, ...]

    def group(
        self,
        capability: LogicalCapability | str,
        kind: ProviderKind | str,
    ) -> RecommendationGroup:
        wanted = (LogicalCapability(capability), ProviderKind(kind))
        return next(item for item in self.candidates if (item.capability, item.kind) == wanted)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "advisory_only": self.advisory_only,
            "active": self.active,
            "checked_at": self.checked_at,
            "hardware_profile": dict(self.hardware_profile),
            "tier_mapping": dict(self.tier_mapping),
            "promotion_policy": dict(self.promotion_policy),
            "candidates": [item.to_mapping() for item in self.candidates],
        }

    @property
    def recommendation_revision(self) -> str:
        encoded = json.dumps(
            self.to_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ProviderRecommendations:
        _require_keys(
            raw,
            {
                "schema",
                "advisory_only",
                "active",
                "checked_at",
                "hardware_profile",
                "tier_mapping",
                "promotion_policy",
                "candidates",
            },
            "recommendations",
        )
        if raw["schema"] != RECOMMENDATION_SCHEMA:
            raise RecommendationValidationError("recommendation schema is unsupported")
        if raw["advisory_only"] is not True or raw["active"] is not False:
            raise RecommendationValidationError("recommendations must remain advisory and inactive")
        checked_at = _iso_date(raw["checked_at"], "checked_at")
        hardware_profile = _hardware_profile(_mapping(raw["hardware_profile"], "hardware_profile"))
        tier_mapping = _tier_mapping(_mapping(raw["tier_mapping"], "tier_mapping"))
        promotion_policy = _promotion_policy(_mapping(raw["promotion_policy"], "promotion_policy"))

        raw_candidates = raw["candidates"]
        if not isinstance(raw_candidates, list):
            raise RecommendationValidationError("candidates must be an array")
        candidates = tuple(_group(_mapping(item, "candidate"), checked_at) for item in raw_candidates)
        expected = set(_EXPECTED_RECOMMENDATION_GROUPS)
        actual = {(item.capability, item.kind) for item in candidates}
        if len(actual) != len(candidates):
            raise RecommendationValidationError("candidate groups must be unique")
        if actual != expected:
            raise RecommendationValidationError("candidate groups must cover every capability and provider kind")
        capability_order = {value: index for index, value in enumerate(LogicalCapability)}
        kind_order = {value: index for index, value in enumerate(ProviderKind)}
        candidates = tuple(
            sorted(candidates, key=lambda item: (capability_order[item.capability], kind_order[item.kind]))
        )
        return cls(
            schema=RECOMMENDATION_SCHEMA,
            advisory_only=True,
            active=False,
            checked_at=checked_at,
            hardware_profile=MappingProxyType(hardware_profile),
            tier_mapping=MappingProxyType(tier_mapping),
            promotion_policy=MappingProxyType(promotion_policy),
            candidates=candidates,
        )


def load_recommendations(path: str | Path) -> ProviderRecommendations:
    try:
        raw = _decode_manifest(Path(path).read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecommendationValidationError("recommendation manifest is not valid UTF-8 JSON") from exc
    if not isinstance(raw, Mapping):
        raise RecommendationValidationError("recommendation manifest root must be an object")
    return ProviderRecommendations.from_mapping(raw)


def load_default_recommendations() -> ProviderRecommendations:
    resource = (
        files("yonerai_discord.provider_registry").joinpath("manifests").joinpath(DEFAULT_RECOMMENDATIONS_RESOURCE)
    )
    try:
        raw = _decode_manifest(resource.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecommendationValidationError("recommendation manifest is not valid UTF-8 JSON") from exc
    if not isinstance(raw, Mapping):
        raise RecommendationValidationError("recommendation manifest root must be an object")
    return ProviderRecommendations.from_mapping(raw)


def _group(raw: Mapping[str, Any], catalog_checked_at: str) -> RecommendationGroup:
    _require_keys(raw, {"capability", "kind", "active", "probe_required", "tiers"}, "candidate")
    try:
        capability = LogicalCapability(raw["capability"])
        kind = ProviderKind(raw["kind"])
    except (TypeError, ValueError) as exc:
        raise RecommendationValidationError("candidate capability or kind is invalid") from exc
    if raw["active"] is not False or raw["probe_required"] is not True:
        raise RecommendationValidationError("every candidate must be inactive and probe-required")
    raw_tiers = _mapping(raw["tiers"], "tiers")
    if set(raw_tiers) != set(_TIERS):
        raise RecommendationValidationError("candidate tiers must be exactly fast, balanced, and quality")
    tiers = tuple(
        (
            tier,
            _tier(_mapping(raw_tiers[tier.value], f"{tier.value} tier"), kind, catalog_checked_at),
        )
        for tier in QualityTier
    )
    return RecommendationGroup(capability, kind, False, True, tiers)


def _tier(raw: Mapping[str, Any], kind: ProviderKind, catalog_checked_at: str) -> RecommendationTier:
    _require_keys(
        raw,
        {
            "provider_id",
            "provider_model",
            "maturity",
            "license_id",
            "commercial_use",
            "checked_at",
            "source_urls",
            "resources",
            "constraints",
        },
        "tier",
    )
    provider_id = _identifier(raw["provider_id"], "provider_id")
    provider_model = _string(raw["provider_model"], "provider_model")
    if len(provider_model) > 256 or any(ord(character) < 32 for character in provider_model):
        raise RecommendationValidationError("provider_model is invalid")
    maturity = _choice(raw["maturity"], _MATURITY, "maturity")
    license_id = _identifier(raw["license_id"], "license_id")
    commercial_use = _choice(raw["commercial_use"], _COMMERCIAL_USE, "commercial_use")
    checked_at = _iso_date(raw["checked_at"], "tier checked_at")
    if checked_at > catalog_checked_at:
        raise RecommendationValidationError("tier checked_at cannot be newer than the catalog")
    source_urls = _https_urls(
        raw["source_urls"],
        provider_id=provider_id,
        provider_model=provider_model,
        kind=kind,
    )
    resources = _resources(_mapping(raw["resources"], "resources"), kind)
    constraints = _strings(raw["constraints"], "constraints")
    if not constraints:
        raise RecommendationValidationError("constraints must not be empty")
    return RecommendationTier(
        provider_id,
        provider_model,
        maturity,
        license_id,
        commercial_use,
        checked_at,
        source_urls,
        resources,
        constraints,
    )


def _resources(raw: Mapping[str, Any], kind: ProviderKind) -> RecommendationResources:
    _require_keys(raw, {"preferred_os", "engines", "vram_budget_mb", "vram_value_kind"}, "resources")
    preferred_os = _strings(raw["preferred_os"], "preferred_os")
    if not preferred_os or any(value not in _PREFERRED_OS for value in preferred_os):
        raise RecommendationValidationError("preferred_os is invalid")
    engines = tuple(_identifier(value, "engine") for value in _strings(raw["engines"], "engines"))
    if not engines or len(set(engines)) != len(engines):
        raise RecommendationValidationError("engines must be non-empty and unique")
    vram = raw["vram_budget_mb"]
    if vram is not None and (isinstance(vram, bool) or not isinstance(vram, int) or not 512 <= vram <= 65_536):
        raise RecommendationValidationError("vram_budget_mb is invalid")
    value_kind = _choice(raw["vram_value_kind"], _VRAM_VALUE_KINDS, "vram_value_kind")
    if kind is ProviderKind.API:
        if preferred_os != ("remote",) or vram is not None or value_kind != "not_applicable":
            raise RecommendationValidationError("API resources must remain remote and omit VRAM")
    elif "remote" in preferred_os or vram is None or value_kind == "not_applicable":
        raise RecommendationValidationError("local resources require a local OS and a VRAM value")
    return RecommendationResources(preferred_os, engines, vram, value_kind)


def _hardware_profile(raw: Mapping[str, Any]) -> dict[str, Any]:
    _require_keys(raw, {"id", "gpu", "vram_mb"}, "hardware_profile")
    profile_id = _identifier(raw["id"], "hardware profile id")
    gpu = _string(raw["gpu"], "gpu")
    vram = raw["vram_mb"]
    if isinstance(vram, bool) or not isinstance(vram, int) or vram <= 0:
        raise RecommendationValidationError("hardware profile vram_mb is invalid")
    return {"id": profile_id, "gpu": gpu, "vram_mb": vram}


def _tier_mapping(raw: Mapping[str, Any]) -> dict[str, str]:
    if set(raw) != set(_TIERS):
        raise RecommendationValidationError("tier_mapping must contain the three logical tiers")
    expected = {"fast": "light", "balanced": "mid", "quality": "high"}
    if dict(raw) != expected:
        raise RecommendationValidationError("tier_mapping must map fast/balanced/quality to light/mid/high")
    return expected


def _promotion_policy(raw: Mapping[str, Any]) -> dict[str, bool]:
    if set(raw) != _PROMOTION_KEYS or any(raw[key] is not True for key in _PROMOTION_KEYS):
        raise RecommendationValidationError("promotion_policy must keep every safety gate enabled")
    return {key: True for key in sorted(_PROMOTION_KEYS)}


def _require_keys(raw: Mapping[str, Any], exact: set[str], label: str) -> None:
    if set(raw) != exact:
        raise RecommendationValidationError(f"{label} must use the exact schema")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecommendationValidationError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecommendationValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    try:
        return normalize_identifier(_string(value, label), label=label)
    except (TypeError, ValueError) as exc:
        raise RecommendationValidationError(f"{label} is invalid") from exc


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise RecommendationValidationError(f"{label} must be an array of non-empty strings")
    normalized = tuple(item.strip() for item in value)
    if len(normalized) > _MAX_STRING_ITEMS or any(len(item) > _MAX_STRING_CHARS for item in normalized):
        raise RecommendationValidationError(f"{label} exceeds the advisory manifest limits")
    if len(set(normalized)) != len(normalized):
        raise RecommendationValidationError(f"{label} must be unique")
    return normalized


def _choice(value: Any, allowed: frozenset[str], label: str) -> str:
    normalized = _string(value, label)
    if normalized not in allowed:
        raise RecommendationValidationError(f"{label} is unsupported")
    return normalized


def _iso_date(value: Any, label: str) -> str:
    normalized = _string(value, label)
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError as exc:
        raise RecommendationValidationError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != normalized:
        raise RecommendationValidationError(f"{label} must be an ISO date")
    return normalized


def _https_urls(
    value: Any,
    *,
    provider_id: str,
    provider_model: str,
    kind: ProviderKind,
) -> tuple[str, ...]:
    urls = _strings(value, "source_urls")
    if not urls:
        raise RecommendationValidationError("source_urls must not be empty")
    allowed_hosts = _PRIMARY_SOURCE_HOSTS.get(provider_id)
    if allowed_hosts is None:
        raise RecommendationValidationError("provider_id has no code-owned primary source policy")
    for url in urls:
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise RecommendationValidationError("source_urls must contain valid HTTPS URLs") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise RecommendationValidationError("source_urls must contain HTTPS primary sources")
        if parsed.hostname.casefold() not in allowed_hosts:
            raise RecommendationValidationError("source_urls must match the provider primary sources")
    if kind is ProviderKind.LOCAL:
        model_subjects = {component.split("#", 1)[0] for component in provider_model.split("+") if component != "no-lm"}
        for subject in model_subjects:
            expected_path = f"/{subject}"
            if not any(
                parsed.hostname.casefold() == "huggingface.co"
                and (parsed.path == expected_path or parsed.path.startswith(f"{expected_path}/"))
                for parsed in map(urlsplit, urls)
            ):
                raise RecommendationValidationError("local provider_model must cite its exact model card")
    return urls


def _decode_manifest(payload: bytes) -> Any:
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise RecommendationValidationError("recommendation manifest is too large")
    return json.loads(payload.decode("utf-8"))
