from __future__ import annotations

import json
from pathlib import Path

import pytest

from yonerai_discord.control_plane import (
    CatalogLoadError,
    DecisionCode,
    RbacLevel,
    load_capability_catalog,
)


def write_catalog(path: Path, rows: list[dict[str, object]], *, declared: int | None = None) -> Path:
    document: dict[str, object] = {"canonical_capabilities": rows}
    if declared is not None:
        document["count_layers"] = {"canonical_capabilities": {"total": declared}}
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def row(
    capability_id: str,
    *,
    decision: str,
    state: str = "implemented",
    risk: str = "low",
) -> dict[str, object]:
    return {
        "id": capability_id,
        "name": f"name of {capability_id}",
        "domain": "fallback",
        "subdomain": "module",
        "state": state,
        "risk": risk,
        "integration_decision": decision,
    }


def test_loader_uses_integration_decision_tail_and_never_trusts_source_state(tmp_path: Path) -> None:
    path = write_catalog(
        tmp_path / "counts.json",
        [
            row("CAP-1", decision="adopt-and-modularize:utility.tools"),
            row("CAP-2", decision="approval-gated-module:gaming.minecraft", risk="high"),
        ],
        declared=2,
    )
    registry = load_capability_catalog(path, expected_count=2, connected_capability_ids={"CAP-2"})

    cap1 = registry.capability("cap-1")
    assert cap1.source_state == "implemented"
    assert cap1.module_id == "utility.tools"
    assert not cap1.implemented
    assert registry.capability_status("cap-1").code == DecisionCode.CAPABILITY_UNIMPLEMENTED

    cap2 = registry.capability("cap-2")
    assert cap2.implemented
    assert cap2.required_level == RbacLevel.MODERATOR
    assert registry.capability_status("cap-2").code == DecisionCode.MODULE_DISABLED


def test_loader_rejects_missing_broken_or_inconsistent_catalog(tmp_path: Path) -> None:
    with pytest.raises(CatalogLoadError, match="not found"):
        load_capability_catalog(tmp_path / "missing.json", expected_count=0)

    broken = tmp_path / "broken.json"
    broken.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CatalogLoadError, match="invalid JSON"):
        load_capability_catalog(broken, expected_count=0)

    mismatch = write_catalog(tmp_path / "mismatch.json", [row("CAP-1", decision="x:utility")], declared=2)
    with pytest.raises(CatalogLoadError, match="declared count mismatch"):
        load_capability_catalog(mismatch, expected_count=1)


def test_loader_rejects_connected_id_absent_from_catalog(tmp_path: Path) -> None:
    path = write_catalog(tmp_path / "counts.json", [row("CAP-1", decision="x:utility")])
    with pytest.raises(CatalogLoadError, match="absent"):
        load_capability_catalog(path, expected_count=1, connected_capability_ids={"CAP-404"})


def test_real_catalog_loads_all_656_as_unconnected() -> None:
    path = Path(__file__).parents[1] / "docs" / "CAPABILITY_COUNTS.json"
    registry = load_capability_catalog(path)
    assert len(registry.capabilities) == 656
    assert all(not capability.implemented for capability in registry.capabilities)
    assert any(module.module_id == "gaming.minecraft" and not module.default_enabled for module in registry.modules)
