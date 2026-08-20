from __future__ import annotations

import json
import re
from pathlib import Path

from scripts import generate_public_capability_truth as capability_truth


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TEMPLATES = ROOT / "public_release" / "templates"
TEMPLATES = PRIVATE_TEMPLATES if PRIVATE_TEMPLATES.is_dir() else ROOT
PROFILE_PATH = TEMPLATES / "PUBLIC_SELF_HOST_PROFILES.json"
DOC_PATHS = (
    TEMPLATES / "docs" / "VM_AND_SANDBOX.md",
    TEMPLATES / "docs" / "SELF_HOST_PROFILES.md",
    TEMPLATES / "docs" / "SEARCH_SANDBOX.md",
    TEMPLATES / "docs" / "MEDIA_SANDBOX.md",
)

EXPECTED_PROFILE_IDS = {
    "no_vm_local_safe",
    "hyperv_search",
    "hyperv_media",
    "hyperv_execution_sandbox",
    "hybrid_local_core",
}
PROFILE_KEYS = {
    "id",
    "description",
    "topology",
    "default_enabled",
    "live_verified",
    "resource",
    "network",
    "mounts",
    "secrets",
    "capabilities",
    "readiness",
    "doctor",
    "rollback",
}


def _document() -> dict[str, object]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8", errors="strict"))


def _profiles() -> dict[str, dict[str, object]]:
    document = _document()
    return {item["id"]: item for item in document["profiles"]}  # type: ignore[index, misc]


def test_public_self_host_profile_schema_and_exact_profile_set() -> None:
    document = _document()

    assert set(document) == {"schema", "current_truth", "safety_defaults", "profiles"}
    assert document["schema"] == "yonerai.discord.public-self-host-profiles.v1"
    profiles = document["profiles"]
    assert isinstance(profiles, list)
    assert len(profiles) == len(EXPECTED_PROFILE_IDS)
    assert {item["id"] for item in profiles} == EXPECTED_PROFILE_IDS
    assert set(capability_truth.PUBLIC_PROFILES) == EXPECTED_PROFILE_IDS
    assert all(set(item) == PROFILE_KEYS for item in profiles)


def test_all_profiles_are_default_off_live_unverified_and_read_only_preflight_only() -> None:
    document = _document()
    assert document["current_truth"] == {
        "live_verified": False,
        "profile_activation_performed": False,
        "actual_vm_contacted": False,
        "real_core_connected": False,
    }
    assert document["safety_defaults"] == {
        "automatic_mutation": False,
        "dangerous_capabilities_enabled": False,
        "sample_cidr_authoritative": False,
        "paid_fallback": False,
        "implicit_local_fallback_for_nonlocal_profile": False,
    }

    for profile in _profiles().values():
        assert profile["default_enabled"] is False
        assert profile["live_verified"] is False
        assert profile["readiness"]["live_verified"] is False  # type: ignore[index]
        assert profile["readiness"]["failure_mode"].startswith("unavailable")  # type: ignore[index, union-attr]
        assert profile["doctor"]["read_only"] is True  # type: ignore[index]
        assert profile["doctor"]["automatic_mutation"] is False  # type: ignore[index]
        assert profile["doctor"]["live_claim"] is False  # type: ignore[index]
        assert profile["rollback"]["automatic"] is False  # type: ignore[index]
        assert profile["rollback"]["destructive_cleanup_requires_owner"] is True  # type: ignore[index]


def test_profiles_forbid_public_inbound_host_mount_and_guest_secrets() -> None:
    for profile in _profiles().values():
        network = profile["network"]
        mounts = profile["mounts"]
        secrets = profile["secrets"]
        assert network["public_inbound"] is False  # type: ignore[index]
        assert network["sample_cidr_authoritative"] is False  # type: ignore[index]
        assert network["automatic_dns_trust"] is False  # type: ignore[index]
        assert mounts == {
            "host_mount": False,
            "persistent_guest_volume": False,
            "clipboard": False,
            "gpu_passthrough": False,
        }
        assert secrets == {
            "guest_secret_injection": False,
            "discord_token_in_guest": False,
            "provider_key_in_guest": False,
            "tracked_secret_reference": False,
        }


def test_search_and_media_resource_budgets_match_code_owned_assets_without_claiming_readiness() -> None:
    profiles = _profiles()
    search = profiles["hyperv_search"]
    media = profiles["hyperv_media"]

    assert search["resource"] == {
        "vm_required": True,
        "memory_mib": 4096,
        "vcpu": 2,
        "dynamic_vhd_max_bytes": 34359738368,
        "required_free_bytes": 38654705664,
        "allocation": "owner_approved_after_read_only_preflight",
        "evidence": [
            "tools/hyperv-search-sandbox/disk-budget.json",
            "infra/search-sandbox/VERSION.lock",
        ],
    }
    assert media["resource"] == {
        "vm_required": True,
        "memory_mib": 16384,
        "vcpu": 2,
        "dynamic_vhd_max_bytes": 21474836480,
        "required_free_bytes": None,
        "allocation": "owner_approved_after_read_only_preflight",
        "evidence": [
            "docs/MEDIA_SANDBOX.md",
            "src/yonerai_discord/modules/media_inspection/hyperv_contract.py",
        ],
    }
    assert search["readiness"]["state"] == "implemented_unconfigured"  # type: ignore[index]
    assert media["readiness"]["state"] == "implemented_unconfigured"  # type: ignore[index]


def test_yonerai_execution_sandbox_has_offline_composition_but_no_live_backend() -> None:
    profile = _profiles()["hyperv_execution_sandbox"]

    assert profile["topology"] == "yonerai_shared_offline_execution"
    assert profile["capabilities"] == []
    assert profile["network"] == {
        "mode": "none",
        "public_inbound": False,
        "egress": "none",
        "addressing": "none",
        "sample_cidr_authoritative": False,
        "automatic_dns_trust": False,
    }
    assert profile["mounts"] == {
        "host_mount": False,
        "persistent_guest_volume": False,
        "clipboard": False,
        "gpu_passthrough": False,
    }
    assert profile["secrets"] == {
        "guest_secret_injection": False,
        "discord_token_in_guest": False,
        "provider_key_in_guest": False,
        "tracked_secret_reference": False,
    }
    readiness = profile["readiness"]
    assert readiness["state"] == "implemented_unconfigured"  # type: ignore[index]
    assert readiness["trusted_data_channel_implemented"] is True  # type: ignore[index]
    assert readiness["offline_runtime_composition_connected"] is True  # type: ignore[index]
    assert readiness["runtime_connected"] is False  # type: ignore[index]
    assert readiness["execution_allowed"] is False  # type: ignore[index]
    assert readiness["blocker"] == "trusted_broker_unavailable"  # type: ignore[index]
    assert readiness["failure_mode"] == "unavailable_trusted_broker"  # type: ignore[index]
    assert profile["resource"]["allocation"] == "blocked_until_protected_backend_and_owner_preflight"  # type: ignore[index]
    assert readiness["required_checks"] == [  # type: ignore[index]
        "fixed_broker_identity",
        "base_image_digest",
        "protected_broker_readiness",
        "protected_worker_readiness",
        "network_adapter_exact_zero",
        "cleanup_destroy_confirmation",
        "vm_guest_handshake",
        "owner_live_canary",
    ]


def test_public_sandbox_truth_does_not_call_implemented_offline_seams_unimplemented() -> None:
    paths = (
        PROFILE_PATH,
        TEMPLATES / "ARCHITECTURE.md",
        TEMPLATES / "PUBLIC_CURRENT_STATUS.md",
        TEMPLATES / "README.md",
        TEMPLATES / "docs" / "VM_AND_SANDBOX.md",
        TEMPLATES / "docs" / "SELF_HOST_PROFILES.md",
    )
    forbidden = (
        "trusted_data_channel_unimplemented",
        "trusted data channel未実装",
        "trusted data channelとruntime compositionが未接続",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="strict")
        assert all(marker not in text for marker in forbidden), path


def test_every_profile_evidence_path_exists_in_the_public_export_projection() -> None:
    policy: dict[str, object] | None = None
    if PRIVATE_TEMPLATES.is_dir():
        from scripts.check_public_boundary import classify_path, load_policy

        policy = load_policy((ROOT / "public_release" / "classification_policy.json").read_bytes())

    for profile in _profiles().values():
        for relative in profile["resource"]["evidence"]:  # type: ignore[index]
            template_projection = TEMPLATES / relative
            source_projection = ROOT / relative
            assert template_projection.is_file() or source_projection.is_file(), (
                f"missing public evidence path: {relative}"
            )
            if policy is not None:
                source_path = f"public_release/templates/{relative}" if template_projection.is_file() else relative
                assert classify_path(source_path, policy) in policy["public_classifications"]  # type: ignore[operator]


def test_capability_profiles_bind_only_existing_narrow_capabilities_and_keep_them_off() -> None:
    profiles = _profiles()
    assert profiles["no_vm_local_safe"]["capabilities"] == []
    assert profiles["hyperv_search"]["capabilities"] == [
        {
            "surface": "web search",
            "capability_id": "cap-can-0153",
            "default_enabled": False,
            "fresh_readiness_required": True,
        },
        {
            "surface": "web fetch",
            "capability_id": "cap-can-0153",
            "default_enabled": False,
            "fresh_readiness_required": True,
        },
        {
            "surface": "web find",
            "capability_id": "cap-can-0153",
            "default_enabled": False,
            "fresh_readiness_required": True,
        },
    ]
    assert profiles["hyperv_media"]["capabilities"] == [
        {
            "surface": "media.url-inspect",
            "capability_id": "cap-run-media-url-inspection",
            "default_enabled": False,
            "fresh_readiness_required": True,
        }
    ]
    assert profiles["hybrid_local_core"]["capabilities"] == [
        {
            "surface": "yonerai status",
            "capability_id": "cap-run-yonerai-status",
            "default_enabled": False,
            "fresh_readiness_required": True,
        }
    ]


def test_public_profile_files_expose_no_machine_identity_address_key_or_absolute_path() -> None:
    texts = [PROFILE_PATH.read_text(encoding="utf-8", errors="strict")]
    texts.extend(path.read_text(encoding="utf-8", errors="strict") for path in DOC_PATHS)
    combined = "\n".join(texts)

    assert "BEGIN OPENSSH" not in combined
    assert "PRIVATE KEY" not in combined
    assert re.search(r"(?i)(?:[a-z]:[\\/]|\\\\[a-z0-9_.-]+[\\/])", combined) is None
    assert re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b", combined) is None
    assert re.search(r"\b[0-9a-f]{64}\b", combined) is None
    assert '"live_verified": true' not in combined


def test_public_profile_docs_are_utf8_lf_and_end_with_newline() -> None:
    for path in (PROFILE_PATH, *DOC_PATHS):
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in data
        assert data.endswith(b"\n")
        data.decode("utf-8", errors="strict")
