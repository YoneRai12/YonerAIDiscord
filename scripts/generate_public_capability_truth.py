from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.dont_write_bytecode = True
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yonerai_discord.capabilities import (  # noqa: E402
    ACTION_CAPABILITIES,
    CATALOG_CONNECTED_CAPABILITY_IDS,
    COMMAND_CAPABILITIES,
    EVENT_CAPABILITIES,
    MODEL_TOOL_CAPABILITY_BINDINGS,
)
from yonerai_discord.control_plane import Registry, load_capability_catalog  # noqa: E402
from yonerai_discord.runtime_manifest import (  # noqa: E402
    RUNTIME_CAPABILITIES,
    register_runtime_capabilities,
    register_runtime_modules,
)


SCHEMA_VERSION = "yonerai.discord.public-capability-truth.v1"
OUTPUT_RELATIVE = Path("public_release/templates")
ARTIFACT_NAMES = (
    "PUBLIC_CAPABILITY_SUMMARY.md",
    "PUBLIC_CAPABILITY_MATRIX.json",
    "PUBLIC_MODULE_MATRIX.json",
    "PUBLIC_COMMAND_INDEX.md",
    "PUBLIC_CURRENT_STATUS.md",
)
SITE_MODULE_ID = "publishing.site-host"
PUBLIC_PROFILES = (
    "no_vm_local_safe",
    "hyperv_search",
    "hyperv_media",
    "hybrid_local_core",
)
STATE_VOCABULARY = frozenset(
    {
        "live_verified",
        "environment_verified",
        "integrated_offline",
        "implemented_unconfigured",
        "surface_unbound",
        "owner_blocked",
        "external_blocked",
        "unavailable_public",
        "spec_only",
    }
)

_TRUE_UNBOUND_CLASSIFICATIONS: Mapping[str, str] = {
    "cap-run-admin-ui-read": "intentionally_internal",
    "cap-run-ai-attachment-understand": "intentionally_internal",
    "cap-run-audio-ducking-core": "intentionally_internal",
    "cap-run-capability-forge-owner-notification": "intentionally_internal",
    "cap-run-memory-context-recall": "intentionally_internal",
    "cap-run-speech-synthesize": "missing_discord_surface",
    "cap-run-speech-transcribe": "missing_discord_surface",
}

_GROUP_ORDER = (
    "AI / conversation / provider routing",
    "Search / fetch / browser",
    "Audio / voice / read-aloud",
    "Moderation / security / permissions",
    "Scheduling / community / operations",
    "Image / video / music / media",
    "VM / Sandbox / capability execution",
    "Memory / files / artifact delivery",
    "YonerAI Core / topology / profile",
    "Diagnostics / preview / status",
)


class PublicCapabilityTruthError(RuntimeError):
    """Generated public truth cannot be derived without an explicit classification."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _revision(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _display_name_en(
    capability_id: str,
    *,
    commands: tuple[str, ...],
    events: tuple[str, ...],
    actions: tuple[str, ...],
    model_tools: tuple[str, ...],
) -> str:
    if commands:
        return f"Discord command /{commands[0]}"
    if events:
        return f"Discord event {events[0]}"
    if actions:
        return f"Typed planner action {actions[0]}"
    if model_tools:
        return f"Bounded model tool {model_tools[0]}"
    return f"Internal capability {capability_id}"


def _profiles(module_id: str) -> tuple[str, ...]:
    if module_id == SITE_MODULE_ID:
        return ()
    if module_id == "media.url-inspection":
        return ("hyperv_media", "hybrid_local_core")
    if module_id == "integration.api-web":
        return ("no_vm_local_safe", "hyperv_search", "hybrid_local_core")
    return ("no_vm_local_safe", "hybrid_local_core")


def _group(module_id: str, capability_id: str) -> str:
    if module_id == "intelligence.personal-memory":
        return "Memory / files / artifact delivery"
    if module_id == "intelligence.capability-forge":
        return "VM / Sandbox / capability execution"
    if module_id.startswith("intelligence."):
        return "AI / conversation / provider routing"
    if module_id.startswith(("web.", "integration.api-web")):
        return "Search / fetch / browser"
    if module_id.startswith(("moderation.", "security.")):
        return "Moderation / security / permissions"
    if module_id.startswith(("collaboration.", "community.", "operations.")):
        if capability_id.startswith(("cap-run-system-", "cap-run-runtime-")):
            return "Diagnostics / preview / status"
        return "Scheduling / community / operations"
    if module_id in {"media.audio-core", "media.voice"} or module_id.startswith("media.speech"):
        return "Audio / voice / read-aloud"
    if module_id.startswith("media."):
        return "Image / video / music / media"
    if module_id.startswith(("platform.", "execution.")):
        return "YonerAI Core / topology / profile"
    if module_id.startswith(("data.", "files.", "artifact.")):
        return "Memory / files / artifact delivery"
    return "Diagnostics / preview / status"


def _anchor(module_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", module_id.lower()).strip("-")
    return f"PUBLIC_CAPABILITY_SUMMARY.md#module-{slug}"


def _audit_class(risk: str) -> str:
    return {
        "low": "audit_standard",
        "medium": "audit_sensitive",
        "high": "audit_high_risk",
        "critical": "audit_critical",
    }[risk]


def _owner_action(
    *,
    module_id: str,
    default_enabled: bool,
    unbound_classification: str | None,
) -> str:
    if module_id == SITE_MODULE_ID:
        return "use_private_distribution"
    if unbound_classification == "missing_discord_surface":
        return "connect_and_review_public_surface"
    if not default_enabled:
        return "configure_and_enable"
    return "run_preflight_before_live_use"


def _binding_maps() -> tuple[dict[str, tuple[str, ...]], ...]:
    def reverse(values: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
        rows: dict[str, list[str]] = {}
        for path, capability_id in values.items():
            rows.setdefault(capability_id, []).append(path)
        return {key: tuple(sorted(items)) for key, items in rows.items()}

    return (
        reverse(COMMAND_CAPABILITIES),
        reverse(EVENT_CAPABILITIES),
        reverse(ACTION_CAPABILITIES),
        reverse(MODEL_TOOL_CAPABILITY_BINDINGS),
    )


def build_capability_rows() -> tuple[dict[str, Any], ...]:
    command_map, event_map, action_map, model_tool_map = _binding_maps()
    rows: list[dict[str, Any]] = []
    true_unbound_seen: set[str] = set()
    for definition in sorted(RUNTIME_CAPABILITIES, key=lambda item: item.capability_id):
        capability_id = definition.capability_id
        commands = command_map.get(capability_id, ())
        events = event_map.get(capability_id, ())
        actions = action_map.get(capability_id, ())
        model_tools = model_tool_map.get(capability_id, ())
        legacy_unbound = not commands and not events and not model_tools
        if actions and legacy_unbound:
            binding_classification = "action_only"
            unbound_classification: str | None = "action_only"
        elif not commands and not events and not actions and not model_tools:
            binding_classification = "true_unbound"
            try:
                unbound_classification = _TRUE_UNBOUND_CLASSIFICATIONS[capability_id]
            except KeyError as exc:
                raise PublicCapabilityTruthError(
                    f"unclassified true-unbound runtime capability: {capability_id}"
                ) from exc
            true_unbound_seen.add(capability_id)
        else:
            binding_classification = "command_event_or_model_tool"
            unbound_classification = None

        public_availability = "unavailable_public" if definition.module_id == SITE_MODULE_ID else "integrated_offline"
        implementation_state = (
            "unavailable_public"
            if definition.module_id == SITE_MODULE_ID
            else "surface_unbound"
            if binding_classification == "true_unbound"
            else "integrated_offline"
        )
        risk = definition.risk.name.lower()
        surfaces = tuple(
            sorted(
                (
                    *(f"command:{path}" for path in commands),
                    *(f"event:{name}" for name in events),
                    *(f"action:{path}" for path in actions),
                    *(f"model_tool:{tool_id}" for tool_id in model_tools),
                )
            )
        )
        row = {
            "audit_class": _audit_class(risk),
            "binding_classification": binding_classification,
            "capability_id": capability_id,
            "command_paths": list(commands),
            "configured_state": (
                "unavailable_public" if definition.module_id == SITE_MODULE_ID else "implemented_unconfigured"
            ),
            "default_state": "enabled_by_manifest" if definition.default_enabled else "disabled_by_manifest",
            "display_name_en": _display_name_en(
                capability_id,
                commands=commands,
                events=events,
                actions=actions,
                model_tools=model_tools,
            ),
            "display_name_ja": definition.name,
            "docs_anchor": _anchor(definition.module_id),
            "event_paths": list(events),
            "implementation_state": implementation_state,
            "legacy_unbound_without_actions": legacy_unbound,
            "live_verification_state": (
                "unavailable_public" if definition.module_id == SITE_MODULE_ID else "integrated_offline"
            ),
            "minimum_rbac": definition.level.name.lower(),
            "module_id": definition.module_id,
            "owner_only": definition.owner_only,
            "plugin": definition.plugin,
            "public_availability": public_availability,
            "public_readiness_claim": False,
            "required_dependencies": list(definition.dependencies),
            "required_owner_action": _owner_action(
                module_id=definition.module_id,
                default_enabled=definition.default_enabled,
                unbound_classification=unbound_classification,
            ),
            "risk": risk,
            "supported_profiles": list(_profiles(definition.module_id)),
            "surfaces": list(surfaces),
            "unbound_classification": unbound_classification,
        }
        for state_field in (
            "configured_state",
            "implementation_state",
            "live_verification_state",
            "public_availability",
        ):
            if row[state_field] not in STATE_VOCABULARY:
                raise PublicCapabilityTruthError(f"unsupported state value: {row[state_field]}")
        rows.append(row)

    expected_true_unbound = set(_TRUE_UNBOUND_CLASSIFICATIONS)
    if true_unbound_seen != expected_true_unbound:
        raise PublicCapabilityTruthError("true-unbound classification contains stale capability IDs")
    return tuple(rows)


def _binding_summary(rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    action_only = tuple(row["capability_id"] for row in rows if row["binding_classification"] == "action_only")
    true_unbound = tuple(
        {
            "capability_id": row["capability_id"],
            "classification": row["unbound_classification"],
        }
        for row in rows
        if row["binding_classification"] == "true_unbound"
    )
    legacy_unbound = tuple(row["capability_id"] for row in rows if row["legacy_unbound_without_actions"])
    return {
        "action_only_capability_ids": list(action_only),
        "action_only_runtime": len(action_only),
        "legacy_unbound_capability_ids": list(legacy_unbound),
        "legacy_unbound_runtime": len(legacy_unbound),
        "true_unbound_capabilities": list(true_unbound),
        "true_unbound_runtime": len(true_unbound),
    }


def build_capability_document() -> dict[str, Any]:
    rows = build_capability_rows()
    counts = {
        "action_paths": len(ACTION_CAPABILITIES),
        "command_capability_ids": len(set(COMMAND_CAPABILITIES.values())),
        "command_paths": len(COMMAND_CAPABILITIES),
        "event_capability_ids": len(set(EVENT_CAPABILITIES.values())),
        "event_paths": len(EVENT_CAPABILITIES),
        "public_live_verification_claims": sum(bool(row["public_readiness_claim"]) for row in rows),
        "runtime_declared": len(rows),
    }
    return {
        "binding_summary": _binding_summary(rows),
        "capabilities": list(rows),
        "catalog_revision": _revision(rows),
        "counts": counts,
        "public_alpha_claim": "implemented_offline_and_live_unverified",
        "schema_version": SCHEMA_VERSION,
    }


def _load_registry() -> Registry:
    registry = load_capability_catalog(
        ROOT / "docs/CAPABILITY_COUNTS.json",
        expected_count=656,
        connected_capability_ids=CATALOG_CONNECTED_CAPABILITY_IDS,
    )
    register_runtime_modules(registry)
    register_runtime_capabilities(registry)
    registry.validate(raise_on_error=True)
    return registry


def build_module_document(capability_document: Mapping[str, Any]) -> dict[str, Any]:
    registry = _load_registry()
    capability_rows = tuple(capability_document["capabilities"])
    modules: list[dict[str, Any]] = []
    for module_id in sorted({str(row["module_id"]) for row in capability_rows}):
        rows = tuple(row for row in capability_rows if row["module_id"] == module_id)
        spec = registry.module(module_id)
        unavailable = module_id == SITE_MODULE_ID
        modules.append(
            {
                "action_paths": sum(sum(surface.startswith("action:") for surface in row["surfaces"]) for row in rows),
                "capability_count": len(rows),
                "command_paths": sum(len(row["command_paths"]) for row in rows),
                "default_state": "enabled_by_manifest" if spec.default_enabled else "disabled_by_manifest",
                "dependencies": list(spec.dependencies),
                "event_paths": sum(len(row["event_paths"]) for row in rows),
                "implementation_state": "unavailable_public" if unavailable else "integrated_offline",
                "live_verification_state": "unavailable_public" if unavailable else "integrated_offline",
                "module_id": module_id,
                "public_availability": "unavailable_public" if unavailable else "integrated_offline",
                "public_readiness_claim": False,
                "supported_profiles": sorted({profile for row in rows for profile in row["supported_profiles"]}),
            }
        )
    return {
        "module_count": len(modules),
        "modules": modules,
        "schema_version": "yonerai.discord.public-module-truth.v1",
        "source_capability_revision": capability_document["catalog_revision"],
    }


def _markdown_cell(value: object) -> str:
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item) for item in value)
    else:
        text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", "\\r").replace("\n", "\\n")


def render_capability_summary(document: Mapping[str, Any]) -> bytes:
    rows = tuple(document["capabilities"])
    group_counts = Counter(_group(str(row["module_id"]), str(row["capability_id"])) for row in rows)
    binding = document["binding_summary"]
    counts = document["counts"]
    lines = [
        "# Public Capability Summary",
        "",
        "この一覧はcode-owned runtime manifestから生成したpublic-safe alphaの静的現在値です。",
        "設定済み、稼働中、live確認済みを意味しません。",
        "",
        "## Counts",
        "",
        f"- runtime-declared capabilities: **{counts['runtime_declared']}**",
        f"- command paths: **{counts['command_paths']}**",
        f"- event paths: **{counts['event_paths']}**",
        f"- legacy unbound without planner actions: **{binding['legacy_unbound_runtime']}**",
        f"- planner action-only capabilities within that legacy count: **{binding['action_only_runtime']}**",
        f"- true surface-unbound capabilities: **{binding['true_unbound_runtime']}**",
        "- public live-verification claims: **0**",
        "",
        "## Groups",
        "",
        "| group | runtime capabilities |",
        "| --- | ---: |",
    ]
    for group in _GROUP_ORDER:
        lines.append(f"| {group} | {group_counts[group]} |")
    lines.extend(
        [
            "",
            "## True surface-unbound classification",
            "",
            "| capability_id | classification |",
            "| --- | --- |",
        ]
    )
    for item in binding["true_unbound_capabilities"]:
        lines.append(f"| `{item['capability_id']}` | `{item['classification']}` |")
    lines.extend(["", "## Modules", ""])
    for module_id in sorted({str(row["module_id"]) for row in rows}):
        slug = _anchor(module_id).split("#", 1)[1]
        module_rows = tuple(row for row in rows if row["module_id"] == module_id)
        lines.extend(
            [
                f'<a id="{slug}"></a>',
                f"### {module_id}",
                "",
                f"- runtime capabilities: {len(module_rows)}",
                f"- public availability: `{module_rows[0]['public_availability']}`",
                "- live verified: `false`",
                "",
            ]
        )
    return ("\n".join(lines).rstrip("\r\n") + "\n").encode("utf-8")


def render_command_index(document: Mapping[str, Any]) -> bytes:
    rows_by_id = {str(row["capability_id"]): row for row in document["capabilities"]}
    registry = _load_registry()

    def section(title: str, mapping: Mapping[str, str], prefix: str) -> list[str]:
        lines = [
            f"## {title}",
            "",
            "| path | capability_id | module | risk | minimum RBAC | public availability |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for path, capability_id in sorted(mapping.items()):
            row = rows_by_id.get(capability_id)
            if row is None:
                spec = registry.capability(capability_id)
                module_id = spec.module_id
                risk = spec.risk.name.lower()
                minimum_rbac = spec.safety_floor.name.lower()
                public_availability = "unavailable_public" if module_id == SITE_MODULE_ID else "integrated_offline"
            else:
                module_id = row["module_id"]
                risk = row["risk"]
                minimum_rbac = row["minimum_rbac"]
                public_availability = row["public_availability"]
            lines.append(
                "| "
                + " | ".join(
                    (
                        f"`{prefix}{_markdown_cell(path)}`",
                        f"`{capability_id}`",
                        f"`{module_id}`",
                        f"`{risk}`",
                        f"`{minimum_rbac}`",
                        f"`{public_availability}`",
                    )
                )
                + " |"
            )
        return lines

    lines = [
        "# Public Command and Surface Index",
        "",
        "静的binding一覧です。Discord登録、設定、runtime readiness、live成功は別の証拠です。",
        "",
        *section("Command paths", COMMAND_CAPABILITIES, "/"),
        "",
        *section("Event paths", EVENT_CAPABILITIES, ""),
        "",
        *section("Typed planner actions", ACTION_CAPABILITIES, ""),
        "",
    ]
    return ("\n".join(lines).rstrip("\r\n") + "\n").encode("utf-8")


def render_current_status(
    capability_document: Mapping[str, Any],
    module_document: Mapping[str, Any],
) -> bytes:
    counts = capability_document["counts"]
    binding = capability_document["binding_summary"]
    unavailable_modules = tuple(
        row["module_id"] for row in module_document["modules"] if row["public_availability"] == "unavailable_public"
    )
    lines = [
        "# Public Current Status",
        "",
        "この文書はpublic export時点の静的実装状態です。live runtimeの状態を主張しません。",
        "",
        "## Static implementation truth",
        "",
        f"- runtime-declared capabilities: {counts['runtime_declared']}",
        f"- command paths: {counts['command_paths']}",
        f"- event paths: {counts['event_paths']}",
        f"- typed planner action paths: {counts['action_paths']}",
        f"- legacy unbound count: {binding['legacy_unbound_runtime']}",
        f"- action-only within legacy unbound: {binding['action_only_runtime']}",
        f"- true surface-unbound: {binding['true_unbound_runtime']}",
        "- public live-verification claims: 0",
        "",
        "## Public alpha boundaries",
        "",
        "- Real Discord, credentials, external providers, SearchSandbox, VM, and YonerAI Core are live未検証 (live-unverified).",
        "- Dangerous or externally impactful capabilities require explicit configuration and fresh authorization.",
        "- Generated rows marked `integrated_offline` are not configured or production-ready claims.",
        f"- unavailable public modules: {', '.join(unavailable_modules) if unavailable_modules else 'none'}",
        "",
    ]
    return ("\n".join(lines).rstrip("\r\n") + "\n").encode("utf-8")


def expected_artifacts() -> dict[str, bytes]:
    capability_document = build_capability_document()
    module_document = build_module_document(capability_document)
    return {
        "PUBLIC_CAPABILITY_SUMMARY.md": render_capability_summary(capability_document),
        "PUBLIC_CAPABILITY_MATRIX.json": (
            json.dumps(capability_document, ensure_ascii=False, indent=2, sort_keys=True).rstrip() + "\n"
        ).encode("utf-8"),
        "PUBLIC_MODULE_MATRIX.json": (
            json.dumps(module_document, ensure_ascii=False, indent=2, sort_keys=True).rstrip() + "\n"
        ).encode("utf-8"),
        "PUBLIC_COMMAND_INDEX.md": render_command_index(capability_document),
        "PUBLIC_CURRENT_STATUS.md": render_current_status(capability_document, module_document),
    }


def check_artifacts(
    root: Path,
    artifacts: Mapping[str, bytes],
    *,
    output_relative: Path = OUTPUT_RELATIVE,
) -> tuple[str, ...]:
    output = root / output_relative
    drift: list[str] = []
    for name in ARTIFACT_NAMES:
        path = output / name
        if not path.is_file():
            drift.append(f"missing:{name}")
        elif path.read_bytes() != artifacts[name]:
            drift.append(f"mismatch:{name}")
    return tuple(drift)


def write_artifacts(root: Path, artifacts: Mapping[str, bytes]) -> None:
    output = root / OUTPUT_RELATIVE
    output.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACT_NAMES:
        path = output / name
        data = artifacts[name]
        if path.is_file() and path.read_bytes() == data:
            continue
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None, *, artifact_root: Path = ROOT) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in ([], ["--check"], ["--check-public"]):
        print(
            "usage: generate_public_capability_truth.py [--check|--check-public]",
            file=sys.stderr,
        )
        return 2
    try:
        artifacts = expected_artifacts()
        if arguments == ["--check"]:
            drift = check_artifacts(artifact_root, artifacts)
            if drift:
                print("public capability truth drift: " + ", ".join(drift), file=sys.stderr)
                return 1
            return 0
        if arguments == ["--check-public"]:
            drift = check_artifacts(artifact_root, artifacts, output_relative=Path("."))
            if drift:
                print("public capability truth drift: " + ", ".join(drift), file=sys.stderr)
                return 1
            return 0
        write_artifacts(artifact_root, artifacts)
        return 0
    except Exception as exc:
        print(f"public capability truth generation failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
