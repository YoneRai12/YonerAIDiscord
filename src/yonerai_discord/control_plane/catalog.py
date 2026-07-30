from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from .models import CapabilitySpec, ModuleSpec, RbacLevel, RiskLevel, normalize_id
from .registry import Registry
from .state import StateStore


class CatalogLoadError(ValueError):
    """Capability台帳が安全にregistryへ変換できない場合の明示エラー。"""


_RISK_POLICY: dict[str, tuple[RiskLevel, RbacLevel]] = {
    "low": (RiskLevel.LOW, RbacLevel.EVERYONE),
    "medium": (RiskLevel.MEDIUM, RbacLevel.TRUSTED),
    "high": (RiskLevel.HIGH, RbacLevel.MODERATOR),
    "destructive": (RiskLevel.CRITICAL, RbacLevel.GUILD_ADMIN),
}


def load_capability_catalog(
    path: str | Path,
    *,
    connected_capability_ids: Collection[str] = (),
    state_store: StateStore | None = None,
    expected_count: int | None = 656,
) -> Registry:
    """``CAPABILITY_COUNTS.json``をdeny-by-defaultのRegistryへ変換する。

    台帳の ``state`` は旧sourceの根拠であり、このプロセスにhandlerが
    接続されたことを示さない。そのため ``connected_capability_ids`` に
    明示されたIDのみ ``implemented=True`` にする。
    """

    catalog_path = Path(path)
    try:
        raw = catalog_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CatalogLoadError(f"capability catalog not found: {catalog_path}") from exc
    except OSError as exc:
        raise CatalogLoadError(f"capability catalog cannot be read: {catalog_path}: {exc}") from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CatalogLoadError(
            f"capability catalog contains invalid JSON: {catalog_path}:{exc.lineno}:{exc.colno}"
        ) from exc
    if not isinstance(document, dict):
        raise CatalogLoadError("capability catalog root must be an object")

    rows = document.get("canonical_capabilities")
    if not isinstance(rows, list):
        raise CatalogLoadError("canonical_capabilities must be an array")
    if expected_count is not None:
        if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0:
            raise ValueError("expected_count must be a non-negative int or None")
        if len(rows) != expected_count:
            raise CatalogLoadError(
                f"canonical capability count mismatch: expected={expected_count}, actual={len(rows)}"
            )

    declared_count = _declared_count(document)
    if declared_count is not None and declared_count != len(rows):
        raise CatalogLoadError(f"catalog declared count mismatch: declared={declared_count}, actual={len(rows)}")

    connected = {
        normalize_id(capability_id, label="connected capability_id") for capability_id in connected_capability_ids
    }
    parsed_rows = [_parse_row(row, index=index, connected=connected) for index, row in enumerate(rows)]
    catalog_ids = {spec.capability_id for _, spec in parsed_rows}
    if len(catalog_ids) != len(parsed_rows):
        duplicates = sorted(
            capability_id
            for capability_id in catalog_ids
            if sum(spec.capability_id == capability_id for _, spec in parsed_rows) > 1
        )
        raise CatalogLoadError("duplicate canonical capability IDs: " + ", ".join(duplicates))
    unknown_connected = connected - catalog_ids
    if unknown_connected:
        raise CatalogLoadError(
            "connected capability IDs are absent from the catalog: " + ", ".join(sorted(unknown_connected))
        )

    registry = Registry(state_store)
    module_ids = sorted({module_id for module_id, _ in parsed_rows})
    for module_id in module_ids:
        registry.register_module(ModuleSpec(module_id))
    for _, capability in parsed_rows:
        registry.register_capability(capability)
    return registry


def _parse_row(
    row: object,
    *,
    index: int,
    connected: set[str],
) -> tuple[str, CapabilitySpec]:
    if not isinstance(row, dict):
        raise CatalogLoadError(f"canonical_capabilities[{index}] must be an object")

    capability_id = _required_string(row, "id", index=index)
    try:
        normalized_id = normalize_id(capability_id, label=f"canonical_capabilities[{index}].id")
    except (TypeError, ValueError) as exc:
        raise CatalogLoadError(f"canonical_capabilities[{index}].id is invalid: {capability_id}") from exc
    name = _required_string(row, "name", index=index)
    source_state = _required_string(row, "state", index=index).lower()
    risk_name = _required_string(row, "risk", index=index).lower()
    try:
        risk, required_level = _RISK_POLICY[risk_name]
    except KeyError as exc:
        raise CatalogLoadError(f"canonical_capabilities[{index}].risk is unknown: {risk_name}") from exc

    module_id = _module_candidate(row, index=index)
    capability = CapabilitySpec(
        capability_id=normalized_id,
        module_id=module_id,
        name=name,
        source_state=source_state,
        # source_stateと分離し、現runtimeのhandler接続のみを信頼する。
        implemented=normalized_id in connected,
        required_level=required_level,
        risk=risk,
    )
    return module_id, capability


def _module_candidate(row: Mapping[str, Any], *, index: int) -> str:
    decision = row.get("integration_decision")
    candidate: str | None = None
    if isinstance(decision, str) and decision.strip():
        candidate = decision.rsplit(":", 1)[-1].strip()

    if not candidate:
        domain = row.get("domain")
        subdomain = row.get("subdomain")
        if isinstance(domain, str) and domain.strip() and isinstance(subdomain, str) and subdomain.strip():
            candidate = f"{domain.strip()}.{subdomain.strip()}"
    if not candidate:
        raise CatalogLoadError(
            f"canonical_capabilities[{index}] has no module candidate in integration_decision or domain/subdomain"
        )
    try:
        return normalize_id(candidate, label=f"canonical_capabilities[{index}].module_id")
    except (TypeError, ValueError) as exc:
        raise CatalogLoadError(f"canonical_capabilities[{index}] has an invalid module candidate: {candidate}") from exc


def _required_string(row: Mapping[str, Any], field: str, *, index: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CatalogLoadError(f"canonical_capabilities[{index}].{field} must be a non-empty string")
    return value.strip()


def _declared_count(document: Mapping[str, Any]) -> int | None:
    count_layers = document.get("count_layers")
    if not isinstance(count_layers, dict):
        return None
    canonical = count_layers.get("canonical_capabilities")
    if not isinstance(canonical, dict) or "total" not in canonical:
        return None
    total = canonical["total"]
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise CatalogLoadError("count_layers.canonical_capabilities.total must be a non-negative integer")
    return total


load_capability_counts = load_capability_catalog
