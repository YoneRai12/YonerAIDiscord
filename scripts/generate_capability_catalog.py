from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.dont_write_bytecode = True
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SCHEMA_VERSION = "yonerai.discord.capability-catalog.v1"
CATALOG_SCOPE = "static_candidate_metadata"
OUTPUT_RELATIVE = Path("docs/generated/capability-catalog")
ARTIFACT_NAMES = ("catalog.json", "README.md")

# source_revisionの対象は、projectionを組み立てるcode-owned正本だけに固定する。
# 改行はLFへ正規化するため、WindowsとCIで同じrevisionになる。
SOURCE_PATHS = (
    "docs/CAPABILITY_COUNTS.json",
    "scripts/generate_capability_catalog.py",
    "src/yonerai_discord/__init__.py",
    "src/yonerai_discord/capabilities.py",
    "src/yonerai_discord/capability_metadata_contract.py",
    "src/yonerai_discord/config.py",
    "src/yonerai_discord/secret_detection.py",
    "src/yonerai_discord/secret_policy.py",
    "src/yonerai_discord/control_plane/__init__.py",
    "src/yonerai_discord/control_plane/catalog.py",
    "src/yonerai_discord/control_plane/models.py",
    "src/yonerai_discord/control_plane/rbac.py",
    "src/yonerai_discord/control_plane/registry.py",
    "src/yonerai_discord/control_plane/state.py",
    "src/yonerai_discord/modules/ai/bounded_tools.py",
    "src/yonerai_discord/runtime_manifest.py",
    "src/yonerai_discord/runtime_manifests/__init__.py",
    "src/yonerai_discord/runtime_manifests/admin_ui.py",
    "src/yonerai_discord/runtime_manifests/ai_memory.py",
    "src/yonerai_discord/runtime_manifests/browser_rendering.py",
    "src/yonerai_discord/runtime_manifests/community.py",
    "src/yonerai_discord/runtime_manifests/capability_forge.py",
    "src/yonerai_discord/runtime_manifests/discovery.py",
    "src/yonerai_discord/runtime_manifests/earthquake.py",
    "src/yonerai_discord/runtime_manifests/image_editing.py",
    "src/yonerai_discord/runtime_manifests/image_generation.py",
    "src/yonerai_discord/runtime_manifests/media_pipeline.py",
    "src/yonerai_discord/runtime_manifests/media_inspection.py",
    "src/yonerai_discord/runtime_manifests/music_generation.py",
    "src/yonerai_discord/runtime_manifests/speech_synthesis.py",
    "src/yonerai_discord/runtime_manifests/speech_transcription.py",
    "src/yonerai_discord/runtime_manifests/video_generation.py",
    "src/yonerai_discord/runtime_manifests/web_search.py",
    "src/yonerai_discord/runtime_manifests/jp_information.py",
    "src/yonerai_discord/runtime_manifests/moderation_server.py",
    "src/yonerai_discord/runtime_manifests/modules.py",
    "src/yonerai_discord/runtime_manifests/music_audio.py",
    "src/yonerai_discord/runtime_manifests/nasa_apod.py",
    "src/yonerai_discord/runtime_manifests/site_publish.py",
    "src/yonerai_discord/runtime_manifests/system_ops.py",
    "src/yonerai_discord/runtime_manifests/types.py",
    "src/yonerai_discord/runtime_manifests/utility.py",
)

from yonerai_discord.capabilities import (  # noqa: E402
    ACTION_CAPABILITIES,
    COMMAND_CAPABILITIES,
    CATALOG_CONNECTED_CAPABILITY_IDS,
    EVENT_CAPABILITIES,
    MODEL_TOOL_CAPABILITY_BINDINGS,
)
from yonerai_discord.capability_metadata_contract import (  # noqa: E402
    CAPABILITY_METADATA_KEYS,
    canonical_capability_metadata_mapping,
)
from yonerai_discord.control_plane.catalog import load_capability_catalog  # noqa: E402
from yonerai_discord.runtime_manifest import (  # noqa: E402
    RUNTIME_CAPABILITIES,
    register_runtime_capabilities,
    register_runtime_modules,
)


COUNT_ROWS = (
    ("historical_canonical", "歴史canonical"),
    ("runtime_declared", "runtime宣言"),
    ("registry_total", "Registry合計"),
    ("projected_total", "静的projection"),
    ("projected_canonical_provenance", "projection canonical由来"),
    ("projected_runtime_provenance", "projection runtime由来"),
    ("surface_unique", "surface接続unique ID"),
    ("command_capability_ids", "command capability ID"),
    ("command_paths", "command path"),
    ("event_capability_ids", "event capability ID"),
    ("event_paths", "event path"),
    ("action_capability_ids", "planner action capability ID"),
    ("action_paths", "planner action path"),
    ("model_tool_bindings", "model-tool binding"),
    ("direct_surface_unbound_runtime", "command/event/model-tool未接続runtime"),
    ("unbound_runtime", "既知binding未接続runtime"),
)


class CatalogGenerationError(RuntimeError):
    """Catalog input or output contract error."""


@dataclass(frozen=True, slots=True)
class Projection:
    snapshot: object
    sources: tuple[dict[str, str], ...]
    source_revision: str
    counts: Mapping[str, int]
    direct_surface_unbound_runtime_ids: tuple[str, ...] = field(default_factory=tuple)
    unbound_runtime_ids: tuple[str, ...] = field(default_factory=tuple)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_source_bytes(data: bytes, *, relative: str) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        raise CatalogGenerationError(f"source contains a UTF-8 BOM: {relative}")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CatalogGenerationError(f"source is not strict UTF-8: {relative}") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _read_sources(root: Path = ROOT) -> tuple[dict[str, str], ...]:
    records: list[dict[str, str]] = []
    for relative in SOURCE_PATHS:
        try:
            data = (root / relative).read_bytes()
        except OSError as exc:
            raise CatalogGenerationError(f"source cannot be read: {relative}") from exc
        records.append(
            {
                "path": relative,
                "sha256": _sha256(_canonical_source_bytes(data, relative=relative)),
            }
        )
    return tuple(records)


def _load_bounded_tools() -> ModuleType:
    module_name = "_yonerai_capability_catalog_bounded_tools"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    source = ROOT / "src/yonerai_discord/modules/ai/bounded_tools.py"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise CatalogGenerationError("bounded tool projection cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def load_projection() -> Projection:
    """Load the formal registry/runtime truth and build its static snapshot."""

    registry = load_capability_catalog(
        ROOT / "docs/CAPABILITY_COUNTS.json",
        expected_count=656,
        connected_capability_ids=CATALOG_CONNECTED_CAPABILITY_IDS,
    )
    historical_count = len(registry.capabilities)
    register_runtime_modules(registry)
    register_runtime_capabilities(registry)
    registry.validate(raise_on_error=True)

    snapshot = _load_bounded_tools().build_static_capability_snapshot(registry)
    entries = tuple(snapshot.entries)
    provenance = Counter(item.source_provenance.value for item in entries)
    surface_ids = set(COMMAND_CAPABILITIES.values()) | set(EVENT_CAPABILITIES.values())
    direct_binding_ids = surface_ids | set(MODEL_TOOL_CAPABILITY_BINDINGS.values())
    projected_binding_ids = direct_binding_ids | set(ACTION_CAPABILITIES.values())
    runtime_ids = {item.capability_id for item in RUNTIME_CAPABILITIES}
    direct_surface_unbound_runtime_ids = tuple(sorted(runtime_ids - direct_binding_ids))
    unbound_runtime_ids = tuple(sorted(runtime_ids - projected_binding_ids))
    sources = _read_sources()
    counts = {
        "historical_canonical": historical_count,
        "runtime_declared": len(RUNTIME_CAPABILITIES),
        "registry_total": len(registry.capabilities),
        "projected_total": len(entries),
        "projected_canonical_provenance": provenance["canonical_registry"],
        "projected_runtime_provenance": provenance["runtime_manifest"],
        "surface_unique": len(surface_ids),
        "command_capability_ids": len(set(COMMAND_CAPABILITIES.values())),
        "command_paths": len(COMMAND_CAPABILITIES),
        "event_capability_ids": len(set(EVENT_CAPABILITIES.values())),
        "event_paths": len(EVENT_CAPABILITIES),
        "action_capability_ids": len(set(ACTION_CAPABILITIES.values())),
        "action_paths": len(ACTION_CAPABILITIES),
        "model_tool_bindings": len(MODEL_TOOL_CAPABILITY_BINDINGS),
        "direct_surface_unbound_runtime": len(direct_surface_unbound_runtime_ids),
        "unbound_runtime": len(unbound_runtime_ids),
    }
    return Projection(
        snapshot=snapshot,
        sources=sources,
        source_revision=_sha256(_canonical_json_bytes(sources)),
        counts=counts,
        direct_surface_unbound_runtime_ids=direct_surface_unbound_runtime_ids,
        unbound_runtime_ids=unbound_runtime_ids,
    )


def build_catalog_document(projection: Projection) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for item in projection.snapshot.entries:
        raw = item.canonical_mapping()
        if set(raw) != set(CAPABILITY_METADATA_KEYS):
            raise CatalogGenerationError("projection entry does not have the exact 11-key schema")
        canonical = canonical_capability_metadata_mapping(raw)
        if canonical != raw:
            raise CatalogGenerationError("projection entry is not canonical")
        entries.append(canonical)
    entries.sort(key=lambda item: item["capability_id"])

    if projection.counts["projected_total"] != len(entries):
        raise CatalogGenerationError("projected count does not match entries")
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_scope": CATALOG_SCOPE,
        "catalog_revision": projection.snapshot.content_revision,
        "source_revision": projection.source_revision,
        "sources": list(projection.sources),
        "counts": dict(projection.counts),
        "runtime_binding_gaps": {
            "direct_surface_unbound_ids": list(projection.direct_surface_unbound_runtime_ids),
            "unbound_ids": list(projection.unbound_runtime_ids),
        },
        "entries": entries,
    }


def render_json(document: Mapping[str, Any]) -> bytes:
    text = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return (text.rstrip("\r\n") + "\n").encode("utf-8")


def _markdown_cell(value: object) -> str:
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item) for item in value)
    else:
        text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", "\\r").replace("\n", "\\n")


def render_markdown(document: Mapping[str, Any]) -> bytes:
    counts = document["counts"]
    lines = [
        "# Capability catalog",
        "",
        "> Generated file — do not edit. `scripts/generate_capability_catalog.py` から再生成してください。",
        "",
        "この一覧はcode-owned正本から生成した静的な候補metadataです。",
        "実行権限、module/plugin readiness、guild override、実Discordや外部依存でのlive成功を示しません。",
        "",
        f"- schema: `{document['schema_version']}`",
        f"- scope: `{document['catalog_scope']}`",
        f"- catalog revision: `{document['catalog_revision']}`",
        f"- source revision: `{document['source_revision']}`",
        "",
        "## Counts",
        "",
        "| 母集団 | 件数 |",
        "| --- | ---: |",
    ]
    for key, label in COUNT_ROWS:
        lines.append(f"| {_markdown_cell(label)} | {counts[key]} |")
    lines.extend(
        [
            "",
            "## Runtime binding gaps",
            "",
            "`direct_surface_unbound_ids` はcommand/event/model-toolへ直接接続していないruntime宣言です。",
            "planner action接続を含む全known bindingの残余は `unbound_ids` です。",
            "",
            "### Direct surface unbound IDs",
            "",
            *(
                f"- `{capability_id}`"
                for capability_id in document["runtime_binding_gaps"]["direct_surface_unbound_ids"]
            ),
            "",
            "### All-known-binding unbound IDs",
            "",
            *(f"- `{capability_id}`" for capability_id in document["runtime_binding_gaps"]["unbound_ids"]),
            "",
            "## Entries",
            "",
            "| capability_id | module_id | name | primary_intent | intent_tags | risk | minimum_rbac | "
            "source_provenance | bindings | surface_bindings | content_revision |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for entry in document["entries"]:
        values = (
            entry["capability_id"],
            entry["module_id"],
            entry["name"],
            entry["primary_intent"],
            entry["intent_tags"],
            entry["risk"],
            entry["minimum_rbac"],
            entry["source_provenance"],
            entry["bindings"],
            entry["surface_bindings"],
            entry["content_revision"],
        )
        lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    return ("\n".join(lines).rstrip("\r\n") + "\n").encode("utf-8")


def expected_artifacts(document: Mapping[str, Any]) -> dict[str, bytes]:
    return {
        "catalog.json": render_json(document),
        "README.md": render_markdown(document),
    }


def check_artifacts(root: Path, artifacts: Mapping[str, bytes]) -> tuple[str, ...]:
    """Compare expected artifact bytes without modifying the filesystem."""

    output = root / OUTPUT_RELATIVE
    if not output.is_dir():
        return tuple(f"missing:{name}" for name in ARTIFACT_NAMES)
    drift: list[str] = []
    existing = {item.name for item in output.iterdir()}
    drift.extend(f"unexpected:{name}" for name in sorted(existing - set(ARTIFACT_NAMES)))
    for name in ARTIFACT_NAMES:
        path = output / name
        if not path.is_file():
            drift.append(f"missing:{name}")
        elif path.read_bytes() != artifacts[name]:
            drift.append(f"mismatch:{name}")
    return tuple(drift)


def _write_artifact(path: Path, data: bytes) -> bool:
    if path.is_file() and path.read_bytes() == data:
        return False
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def write_artifacts(root: Path, artifacts: Mapping[str, bytes]) -> bool:
    """Write each artifact through a closed same-directory temporary file."""

    output = root / OUTPUT_RELATIVE
    output.mkdir(parents=True, exist_ok=True)
    changed = False
    for name in ARTIFACT_NAMES:
        changed = _write_artifact(output / name, artifacts[name]) or changed
    return changed


def main(argv: Sequence[str] | None = None, *, artifact_root: Path = ROOT) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments not in ([], ["--check"]):
        print("usage: generate_capability_catalog.py [--check]", file=sys.stderr)
        return 2
    try:
        document = build_catalog_document(load_projection())
        artifacts = expected_artifacts(document)
        if arguments == ["--check"]:
            drift = check_artifacts(artifact_root, artifacts)
            if drift:
                print("capability catalog drift: " + ", ".join(drift), file=sys.stderr)
                return 1
            return 0
        write_artifacts(artifact_root, artifacts)
        return 0
    except Exception as exc:
        print(f"capability catalog generation failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
