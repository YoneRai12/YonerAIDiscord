from __future__ import annotations

import json
from pathlib import Path

from scripts import generate_capability_catalog as generator
from yonerai_discord.capabilities import (
    COMMAND_CAPABILITIES,
    EVENT_CAPABILITIES,
    MODEL_TOOL_CAPABILITY_BINDINGS,
)
from yonerai_discord.capability_metadata_contract import CAPABILITY_METADATA_KEYS
from yonerai_discord.runtime_manifest import RUNTIME_CAPABILITIES


ROOT = Path(__file__).resolve().parents[1]


def _production_artifacts() -> tuple[generator.Projection, dict[str, object], dict[str, bytes]]:
    projection = generator.load_projection()
    document = generator.build_catalog_document(projection)
    return projection, document, generator.expected_artifacts(document)


def test_production_projection_matches_snapshot_counts_and_revision() -> None:
    projection, document, _ = _production_artifacts()
    expected_entries = sorted(
        (item.canonical_mapping() for item in projection.snapshot.entries),
        key=lambda item: item["capability_id"],
    )
    assert document["entries"] == expected_entries
    assert document["catalog_revision"] == projection.snapshot.content_revision
    assert all(set(entry) == set(CAPABILITY_METADATA_KEYS) for entry in expected_entries)
    assert [entry["capability_id"] for entry in expected_entries] == sorted(
        entry["capability_id"] for entry in expected_entries
    )

    surface_ids = set(COMMAND_CAPABILITIES.values()) | set(EVENT_CAPABILITIES.values())
    projected_ids = surface_ids | set(MODEL_TOOL_CAPABILITY_BINDINGS.values())
    runtime_ids = {item.capability_id for item in RUNTIME_CAPABILITIES}
    assert document["counts"] == {
        "historical_canonical": 656,
        "runtime_declared": len(RUNTIME_CAPABILITIES),
        "registry_total": 656 + len(RUNTIME_CAPABILITIES),
        "projected_total": len(expected_entries),
        "projected_canonical_provenance": sum(
            entry["source_provenance"] == "canonical_registry" for entry in expected_entries
        ),
        "projected_runtime_provenance": sum(
            entry["source_provenance"] == "runtime_manifest" for entry in expected_entries
        ),
        "surface_unique": len(surface_ids),
        "command_capability_ids": len(set(COMMAND_CAPABILITIES.values())),
        "command_paths": len(COMMAND_CAPABILITIES),
        "event_capability_ids": len(set(EVENT_CAPABILITIES.values())),
        "event_paths": len(EVENT_CAPABILITIES),
        "model_tool_bindings": len(MODEL_TOOL_CAPABILITY_BINDINGS),
        "unbound_runtime": len(runtime_ids - projected_ids),
    }
    assert (
        document["counts"]["projected_total"],
        document["counts"]["projected_canonical_provenance"],
        document["counts"]["projected_runtime_provenance"],
    ) == (171, 17, 154)


def test_rendering_is_deterministic_utf8_lf_and_path_free(tmp_path: Path) -> None:
    projection, first_document, first = _production_artifacts()
    _, second_document, second = _production_artifacts()
    assert first_document == second_document
    assert first == second
    assert json.loads(first["catalog.json"].decode("utf-8")) == first_document
    assert generator._canonical_source_bytes(b"a\r\nb\rc\n", relative="sample") == b"a\nb\nc\n"

    combined = b"\n".join(first.values())
    assert b"\xef\xbb\xbf" not in combined
    assert b"\r" not in combined
    assert b"generated_at" not in combined
    assert str(ROOT).encode("utf-8") not in combined
    for payload in first.values():
        assert payload.endswith(b"\n")
        assert not payload.endswith(b"\n\n")

    lf_root = tmp_path / "lf"
    crlf_root = tmp_path / "crlf"
    for relative in generator.SOURCE_PATHS:
        canonical = generator._canonical_source_bytes(
            (ROOT / relative).read_bytes(),
            relative=relative,
        )
        for root, data in ((lf_root, canonical), (crlf_root, canonical.replace(b"\n", b"\r\n"))):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    lf_sources = generator._read_sources(lf_root)
    crlf_sources = generator._read_sources(crlf_root)
    assert lf_sources == crlf_sources
    lf_projection = generator.Projection(
        snapshot=projection.snapshot,
        sources=lf_sources,
        source_revision=generator._sha256(generator._canonical_json_bytes(lf_sources)),
        counts=projection.counts,
    )
    crlf_projection = generator.Projection(
        snapshot=projection.snapshot,
        sources=crlf_sources,
        source_revision=generator._sha256(generator._canonical_json_bytes(crlf_sources)),
        counts=projection.counts,
    )
    assert generator.expected_artifacts(
        generator.build_catalog_document(lf_projection)
    ) == generator.expected_artifacts(generator.build_catalog_document(crlf_projection))


def test_check_reports_current_stale_and_missing_without_writes(tmp_path: Path) -> None:
    _, _, artifacts = _production_artifacts()
    output = tmp_path / generator.OUTPUT_RELATIVE
    output.mkdir(parents=True)
    for name, payload in artifacts.items():
        (output / name).write_bytes(payload)

    before = {path.name: path.read_bytes() for path in output.iterdir()}
    assert generator.check_artifacts(tmp_path, artifacts) == ()
    assert generator.main(["--check"], artifact_root=tmp_path) == 0
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before

    (output / "catalog.json").write_bytes(b"stale\n")
    stale = (output / "catalog.json").read_bytes()
    assert "mismatch:catalog.json" in generator.check_artifacts(tmp_path, artifacts)
    assert generator.main(["--check"], artifact_root=tmp_path) == 1
    assert (output / "catalog.json").read_bytes() == stale

    (output / "README.md").unlink()
    assert "missing:README.md" in generator.check_artifacts(tmp_path, artifacts)
    assert generator.main(["--check"], artifact_root=tmp_path) == 1
    assert not (output / "README.md").exists()


def test_generate_creates_two_files_and_is_idempotent(tmp_path: Path) -> None:
    _, _, expected = _production_artifacts()
    assert generator.main([], artifact_root=tmp_path) == 0
    output = tmp_path / generator.OUTPUT_RELATIVE
    assert sorted(path.name for path in output.iterdir()) == ["README.md", "catalog.json"]
    first = {path.name: path.read_bytes() for path in output.iterdir()}
    assert first == expected

    assert generator.main([], artifact_root=tmp_path) == 0
    assert {path.name: path.read_bytes() for path in output.iterdir()} == first
    assert generator.main(["--check"], artifact_root=tmp_path) == 0


def test_repository_generated_artifacts_are_current() -> None:
    _, _, artifacts = _production_artifacts()
    assert generator.check_artifacts(ROOT, artifacts) == ()
    assert generator.main(["--check"]) == 0


def test_ci_order_and_generated_lf_attributes_are_declared() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    static_index = workflow.index("- name: Static checks")
    catalog_index = workflow.index("python scripts/generate_capability_catalog.py --check")
    security_index = workflow.index("python scripts/security_preflight.py")
    assert static_index < catalog_index < security_index

    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "docs/generated/capability-catalog/catalog.json text eol=lf" in attributes
    assert "docs/generated/capability-catalog/README.md text eol=lf" in attributes
