from __future__ import annotations

import json
from pathlib import Path

from scripts import generate_public_capability_truth as generator


REQUIRED_CAPABILITY_FIELDS = {
    "audit_class",
    "binding_classification",
    "capability_id",
    "command_paths",
    "configured_state",
    "default_state",
    "display_name_en",
    "display_name_ja",
    "docs_anchor",
    "event_paths",
    "implementation_state",
    "legacy_unbound_without_actions",
    "live_verification_state",
    "minimum_rbac",
    "module_id",
    "owner_only",
    "plugin",
    "public_availability",
    "public_readiness_claim",
    "required_dependencies",
    "required_owner_action",
    "risk",
    "supported_profiles",
    "surfaces",
    "unbound_classification",
}

EXPECTED_ACTION_ONLY = {
    "cap-run-browser-remote-interactive",
    "cap-run-browser-remote-screenshot",
    "cap-run-image-edit",
    "cap-run-media-compose-grid",
    "cap-run-media-discord-asset-inspect",
    "cap-run-media-place-on-canvas",
    "cap-run-media-qr-encode",
    "cap-run-media-quote-card",
    "cap-run-media-url-inspection",
}

EXPECTED_TRUE_UNBOUND = {
    "cap-run-admin-ui-read": "intentionally_internal",
    "cap-run-ai-attachment-understand": "intentionally_internal",
    "cap-run-audio-ducking-core": "intentionally_internal",
    "cap-run-capability-forge-owner-notification": "intentionally_internal",
    "cap-run-memory-context-recall": "intentionally_internal",
    "cap-run-speech-synthesize": "missing_discord_surface",
    "cap-run-speech-transcribe": "missing_discord_surface",
}


def test_capability_matrix_covers_exact_runtime_truth_with_required_fields() -> None:
    document = generator.build_capability_document()
    rows = document["capabilities"]

    assert document["counts"] == {
        "action_paths": 9,
        "command_capability_ids": 164,
        "command_paths": 182,
        "event_capability_ids": 14,
        "event_paths": 14,
        "public_live_verification_claims": 0,
        "runtime_declared": 177,
    }
    assert len(rows) == 177
    assert [row["capability_id"] for row in rows] == sorted(row["capability_id"] for row in rows)
    assert all(set(row) == REQUIRED_CAPABILITY_FIELDS for row in rows)
    assert all(row["display_name_ja"].strip() and row["display_name_en"].strip() for row in rows)
    assert all(row["public_readiness_claim"] is False for row in rows)


def test_legacy_unbound_is_split_into_action_only_and_true_unbound_exactly() -> None:
    document = generator.build_capability_document()
    binding = document["binding_summary"]

    assert binding["legacy_unbound_runtime"] == 16
    assert binding["action_only_runtime"] == 9
    assert set(binding["action_only_capability_ids"]) == EXPECTED_ACTION_ONLY
    assert binding["true_unbound_runtime"] == 7
    assert {
        item["capability_id"]: item["classification"] for item in binding["true_unbound_capabilities"]
    } == EXPECTED_TRUE_UNBOUND
    rows = {row["capability_id"]: row for row in document["capabilities"]}
    assert all(rows[item]["implementation_state"] == "integrated_offline" for item in EXPECTED_ACTION_ONLY)
    assert all(rows[item]["implementation_state"] == "surface_unbound" for item in EXPECTED_TRUE_UNBOUND)


def test_site_publish_is_unavailable_public_and_no_row_claims_live_readiness() -> None:
    capability_document = generator.build_capability_document()
    module_document = generator.build_module_document(capability_document)
    site_rows = [row for row in capability_document["capabilities"] if row["module_id"] == generator.SITE_MODULE_ID]
    site_module = next(row for row in module_document["modules"] if row["module_id"] == generator.SITE_MODULE_ID)

    assert site_rows
    assert {row["public_availability"] for row in site_rows} == {"unavailable_public"}
    assert {row["configured_state"] for row in site_rows} == {"unavailable_public"}
    assert site_module["public_availability"] == "unavailable_public"
    assert site_module["public_readiness_claim"] is False
    assert all(row["live_verification_state"] != "live_verified" for row in capability_document["capabilities"])


def test_networkless_profile_is_not_advertised_for_runtime_modules() -> None:
    capability_document = generator.build_capability_document()
    module_document = generator.build_module_document(capability_document)
    modules = {row["module_id"]: row for row in module_document["modules"]}

    assert all("no_vm_local_safe" not in row["supported_profiles"] for row in capability_document["capabilities"])
    assert all("no_vm_local_safe" not in row["supported_profiles"] for row in module_document["modules"])
    assert modules["integration.api-web"]["supported_profiles"] == [
        "hybrid_local_core",
        "hyperv_search",
    ]
    assert modules["web.browser-rendering"]["supported_profiles"] == ["hybrid_local_core"]


def test_all_five_artifacts_are_deterministic_strict_utf8_lf_and_content_complete() -> None:
    first = generator.expected_artifacts()
    second = generator.expected_artifacts()

    assert first == second
    assert tuple(first) == generator.ARTIFACT_NAMES
    assert all(payload.endswith(b"\n") and b"\r" not in payload for payload in first.values())
    assert all(not payload.startswith(b"\xef\xbb\xbf") for payload in first.values())
    matrix = json.loads(first["PUBLIC_CAPABILITY_MATRIX.json"].decode("utf-8"))
    modules = json.loads(first["PUBLIC_MODULE_MATRIX.json"].decode("utf-8"))
    assert len(matrix["capabilities"]) == 177
    assert modules["module_count"] == 30
    assert "public live-verification claims: **0**" in first["PUBLIC_CAPABILITY_SUMMARY.md"].decode("utf-8")
    command_index = first["PUBLIC_COMMAND_INDEX.md"].decode("utf-8")
    assert "## Typed planner actions" in command_index
    assert sum(line.startswith("| `/") for line in command_index.splitlines()) == 182


def test_command_index_uses_per_command_rbac_floors() -> None:
    command_index = generator.expected_artifacts()["PUBLIC_COMMAND_INDEX.md"].decode("utf-8")
    command_rows = {
        cells[0].removeprefix("/"): cells[4]
        for line in command_index.splitlines()
        if line.startswith("| `/")
        for cells in ([cell.strip().strip("`") for cell in line.strip("|").split("|")],)
    }
    registry = generator._load_registry()

    for path, floor in generator.COMMAND_RBAC_FLOORS.items():
        capability_id = generator.COMMAND_CAPABILITIES[path]
        expected = max(registry.capability(capability_id).safety_floor, floor).name.lower()
        assert command_rows[path] == expected

    assert (
        "| `/music import` | `cap-run-music-import` | `media.music` | `medium` | `guild_admin` | `integrated_offline` |"
    ) in command_index
    assert (
        "| `/music read-aloud enable` | `cap-run-music-read-aloud-message` | `media.music` | "
        "`medium` | `guild_admin` | `integrated_offline` |"
    ) in command_index
    assert (
        "| `/music read-aloud my-preset` | `cap-run-music-read-aloud-message` | `media.music` | "
        "`medium` | `everyone` | `integrated_offline` |"
    ) in command_index
    assert (
        "| `/system health` | `cap-can-0519` | `operations.observability` | `low` | "
        "`guild_admin` | `integrated_offline` |"
    ) in command_index


def test_cli_write_check_and_drift_detection_are_bounded_to_five_outputs(
    tmp_path: Path,
    capsys,
) -> None:
    assert generator.main([], artifact_root=tmp_path) == 0
    output = tmp_path / generator.OUTPUT_RELATIVE
    assert tuple(sorted(path.name for path in output.iterdir())) == tuple(sorted(generator.ARTIFACT_NAMES))
    assert generator.main(["--check"], artifact_root=tmp_path) == 0

    target = output / "PUBLIC_CURRENT_STATUS.md"
    target.write_text("stale\n", encoding="utf-8")
    assert generator.main(["--check"], artifact_root=tmp_path) == 1
    assert "mismatch:PUBLIC_CURRENT_STATUS.md" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "stale\n"


def test_public_export_layout_check_reads_root_outputs_without_writing(tmp_path: Path) -> None:
    artifacts = generator.expected_artifacts()
    for name, payload in artifacts.items():
        (tmp_path / name).write_bytes(payload)

    assert generator.main(["--check-public"], artifact_root=tmp_path) == 0
    assert not (tmp_path / generator.OUTPUT_RELATIVE).exists()


def test_invalid_cli_argument_does_not_write(tmp_path: Path) -> None:
    assert generator.main(["--unknown"], artifact_root=tmp_path) == 2
    assert not (tmp_path / generator.OUTPUT_RELATIVE).exists()
