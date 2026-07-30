"""Production Discord builders projected into a tokenless, deterministic preview."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sys
from collections.abc import Sequence
from typing import Any

import discord

from yonerai_discord.modules.ai.consent_view import RemoteConsentScope, RemoteConsentView
from yonerai_discord.modules.ai.discord_renderer import DiscordAIResponseRenderer
from yonerai_discord.modules.ai.task_progress import (
    DiscordAITaskProgressRenderer,
    DiscordAITaskProgressSession,
    build_ai_progress_plan,
)
from yonerai_discord.modules.ai.task_routing import classify_ai_task
from yonerai_discord.modules.ai.web_adapter import render_search_outcome
from yonerai_discord.modules.audio_core.models import QueueSnapshot
from yonerai_discord.modules.music.dashboard import render_music_dashboard
from yonerai_discord.modules.operations.failure import classify_failure, format_failure_message
from yonerai_discord.modules.web_runtime.search import WebSearchSource
from yonerai_discord.search_fabric.contracts import (
    SearchCorroborationState,
    SearchEvidenceV1,
    SearchFetchState,
    SearchFreshnessState,
    SearchIntent,
    SearchResultV1,
    SearchSourceClass,
    query_digest,
)
from yonerai_discord.search_fabric.orchestrator import SearchOrchestratorOutcome, SearchVerificationState
from yonerai_discord.search_fabric.receipts import SearchReceiptV1
from yonerai_discord.secret_detection import contains_secret_like


_SCHEMA = "yonerai.discord.offline-preview.v1"
_FIXTURE_TIMESTAMP = "2026-07-30T00:00:00Z"
_SUPPORTED_SURFACES = (
    "ai.card",
    "ai.progress.initial",
    "ai.progress.failure",
    "interaction.error",
    "search.sources",
    "music.dashboard",
    "remote_consent.buttons",
)
_EXPLICITLY_UNSUPPORTED = frozenset(
    {
        "modal",
        "moderation.confirmation",
        "pagination",
        "select",
    }
)
_HOST_PATH = re.compile(r"(?i)(?:^|[\s\"'=:(])(?:[a-z]:[\\/]|\\\\|file:(?:/{0,2})|\.\.[\\/])")
_MAX_MANIFEST_BYTES = 64 * 1024


class OfflinePreviewUnsupportedError(ValueError):
    """The requested surface has no production payload builder in this preview."""


class OfflinePreviewSafetyError(ValueError):
    """A preview projection contained data outside the fixed safe-fixture boundary."""


class _ProgressMessage:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []

    async def edit(self, **kwargs: object) -> _ProgressMessage:
        self.edits.append(dict(kwargs))
        return self

    async def delete(self) -> None:
        return None


class _ProgressSource:
    def __init__(self) -> None:
        self.replies: list[dict[str, object]] = []
        self.message = _ProgressMessage()

    async def reply(self, **kwargs: object) -> _ProgressMessage:
        self.replies.append(dict(kwargs))
        return self.message


async def build_offline_preview_manifest(
    surfaces: Sequence[str] | None = None,
) -> dict[str, object]:
    """Build deterministic previews without Discord credentials, I/O, or user content."""

    selected = _selected_surfaces(surfaces)
    builders = {
        "ai.card": _ai_card_preview,
        "ai.progress.initial": _progress_initial_preview,
        "ai.progress.failure": _progress_failure_preview,
        "interaction.error": _interaction_error_preview,
        "search.sources": _search_sources_preview,
        "music.dashboard": _music_dashboard_preview,
        "remote_consent.buttons": _remote_consent_preview,
    }
    rendered: list[dict[str, object]] = []
    for surface in selected:
        value = builders[surface]()
        if asyncio.iscoroutine(value):
            value = await value
        rendered.append({"surface": surface, "payload": value})
    manifest: dict[str, object] = {
        "schema": _SCHEMA,
        "renderer_version": "production-builders-v1",
        "fixture_timestamp": _FIXTURE_TIMESTAMP,
        "network_used": False,
        "discord_token_required": False,
        "viewports": {
            "mobile": {"width": 390, "height": 844},
            "desktop": {"width": 1280, "height": 720},
        },
        "surfaces": rendered,
    }
    _validate_manifest(manifest)
    return manifest


def render_offline_preview_json(manifest: dict[str, object], *, pretty: bool = False) -> str:
    """Serialize a validated preview manifest without runtime-only Discord objects."""

    _validate_manifest(manifest)
    if pretty:
        return json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yonerai-discord-preview",
        description="Discord token不要のproduction payload preview",
    )
    parser.add_argument("--surface", action="append", default=None, help="表示surface（複数指定可）")
    parser.add_argument("--format", choices=("html", "json"), default="json", help="出力形式")
    parser.add_argument("--pretty", action="store_true", help="読みやすいJSONで表示")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        manifest = asyncio.run(build_offline_preview_manifest(args.surface))
        output = (
            render_offline_preview_html(manifest)
            if args.format == "html"
            else render_offline_preview_json(manifest, pretty=args.pretty)
        )
    except OfflinePreviewUnsupportedError:
        print(
            json.dumps(
                {"schema": _SCHEMA, "status": "failed", "error_code": "unsupported_surface"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"schema": _SCHEMA, "status": "failed", "error_code": "preview_build_failed"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    _configure_stdout_utf8()
    print(output, end="")
    return 0


def _configure_stdout_utf8() -> None:
    encoding = str(getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if encoding in {"utf8", "utf8sig"}:
        return
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")


def _ai_card_preview() -> dict[str, object]:
    prepared = DiscordAIResponseRenderer().prepare_payload(
        "安全な固定fixtureから生成した回答です。",
        model="preview-model",
    )
    return _serialize_discord_kwargs(prepared.kwargs)


async def _progress_initial_preview() -> dict[str, object]:
    plan = _preview_progress_plan()
    source = _ProgressSource()
    session = await DiscordAITaskProgressRenderer().start(source, plan)
    if not isinstance(session, DiscordAITaskProgressSession) or len(source.replies) != 1:
        raise OfflinePreviewSafetyError("production progress builder did not produce one preview")
    return _serialize_discord_kwargs(source.replies[0])


async def _progress_failure_preview() -> dict[str, object]:
    plan = _preview_progress_plan()
    source = _ProgressSource()
    session = await DiscordAITaskProgressRenderer().start(source, plan)
    if session is None or not await session.fail("権限または機能の状態が変わったため停止しました。"):
        raise OfflinePreviewSafetyError("production progress failure builder did not terminate")
    if len(source.message.edits) != 1:
        raise OfflinePreviewSafetyError("production progress failure produced an invalid edit count")
    return _serialize_discord_kwargs(source.message.edits[0])


def _preview_progress_plan():
    instruction = "公式資料を検索して比較表を作って"
    plan = build_ai_progress_plan(
        route=classify_ai_task(instruction, web_search=True),
        instruction=instruction,
        attachment_count=0,
        has_reference=False,
    )
    if plan is None:
        raise OfflinePreviewSafetyError("fixed progress fixture did not produce a plan")
    return plan


def _interaction_error_preview() -> dict[str, object]:
    failure = classify_failure(ValueError("this exception body must never be rendered"))
    return {
        "content": format_failure_message(
            failure,
            error_code=f"discord.{failure.kind.value}",
            reference_id="ERR-000000000000",
        ),
        "ephemeral": True,
        "allowed_mentions": {"parse": []},
    }


def _search_sources_preview() -> dict[str, object]:
    evidence = SearchEvidenceV1(
        source=WebSearchSource(
            title="YonerAI 公式資料の固定fixture",
            url="https://example.org/official-guide",
            snippet="Preview fixture",
            source_id="src_preview_official",
        ),
        source_class=SearchSourceClass.PRIMARY_OFFICIAL,
        fetch_state=SearchFetchState.FETCHED,
        publisher="YonerAI Preview",
        published="2026-07-01T00:00:00Z",
        retrieved=_FIXTURE_TIMESTAMP,
        content_hash="sha256:" + ("a" * 64),
        freshness_state=SearchFreshnessState.CURRENT,
        corroboration=SearchCorroborationState.INDEPENDENT,
        corroboration_group="cg_preview",
        verification_reasons=("official_domain", "direct_fetch"),
    )
    result = SearchResultV1(
        request_id="preview-search",
        query_digest=query_digest("preview query"),
        intent=SearchIntent.OFFICIAL,
        language="ja-JP",
        evidence=(evidence,),
        backend_ids=("searxng.local",),
        engine_errors=(),
        candidate_count=1,
        cache_hits=0,
        latency_ms=1,
    )
    outcome = SearchOrchestratorOutcome(
        result=result,
        receipt=SearchReceiptV1.from_result(result),
        verification_state=SearchVerificationState.VERIFIED,
        evidence_text="",
    )
    return {
        "content": render_search_outcome(outcome),
        "allowed_mentions": {"parse": []},
    }


def _music_dashboard_preview() -> dict[str, object]:
    snapshot = QueueSnapshot(
        current=None,
        upcoming=(),
        loop_mode="off",
        paused=False,
        volume=1.0,
        speech_volume=1.0,
    )
    return {
        "content": render_music_dashboard(snapshot=snapshot, radio_enabled=False),
        "allowed_mentions": {"parse": []},
    }


async def _remote_consent_preview() -> dict[str, object]:
    async def confirm(_interaction: discord.Interaction, _scope: RemoteConsentScope) -> bool:
        raise AssertionError("preview callbacks must never execute")

    view = RemoteConsentView(
        RemoteConsentScope(guild_id=1, channel_id=1, user_id=1, source_message_id=1),
        confirm,
    )
    try:
        components = []
        for child in view.children:
            if not isinstance(child, discord.ui.Button):
                raise OfflinePreviewSafetyError("remote consent preview only supports buttons")
            components.append(
                {
                    "type": "button",
                    "label": child.label,
                    "style": int(child.style.value),
                    "custom_id": child.custom_id,
                    "disabled": child.disabled,
                }
            )
        return {
            "components": components,
            "allowed_mentions": {"parse": []},
        }
    finally:
        view.stop()


def _selected_surfaces(surfaces: Sequence[str] | None) -> tuple[str, ...]:
    if surfaces is None:
        return _SUPPORTED_SURFACES
    if isinstance(surfaces, (str, bytes)) or not isinstance(surfaces, Sequence):
        raise TypeError("surfaces must be a sequence of strings or None")
    normalized: list[str] = []
    for surface in surfaces:
        if not isinstance(surface, str) or not surface.strip():
            raise OfflinePreviewUnsupportedError("preview surface must be a non-empty string")
        value = surface.strip()
        if value in _EXPLICITLY_UNSUPPORTED:
            raise OfflinePreviewUnsupportedError("preview is unsupported until a production payload builder exists")
        if value not in _SUPPORTED_SURFACES:
            raise OfflinePreviewUnsupportedError("unknown preview surface")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise OfflinePreviewUnsupportedError("at least one preview surface is required")
    return tuple(normalized)


def _serialize_discord_kwargs(kwargs: dict[str, Any]) -> dict[str, object]:
    if not isinstance(kwargs, dict):
        raise OfflinePreviewSafetyError("Discord payload must be a dictionary")
    if "file" in kwargs or "files" in kwargs or "attachments" in kwargs:
        raise OfflinePreviewSafetyError("offline preview cannot serialize attachments")
    result: dict[str, object] = {}
    for key, value in sorted(kwargs.items()):
        if key == "embed":
            if not isinstance(value, discord.Embed):
                raise OfflinePreviewSafetyError("embed payload is invalid")
            result[key] = value.to_dict()
        elif key == "embeds":
            if not isinstance(value, (tuple, list)) or any(not isinstance(item, discord.Embed) for item in value):
                raise OfflinePreviewSafetyError("embeds payload is invalid")
            result[key] = [item.to_dict() for item in value]
        elif key == "allowed_mentions":
            if not isinstance(value, discord.AllowedMentions):
                raise OfflinePreviewSafetyError("allowed_mentions payload is invalid")
            result[key] = value.to_dict()
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            raise OfflinePreviewSafetyError(f"runtime-only Discord payload value is unsupported: {key}")
    return result


def _validate_manifest(manifest: dict[str, object]) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema") != _SCHEMA:
        raise OfflinePreviewSafetyError("preview manifest schema is invalid")
    serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise OfflinePreviewSafetyError("preview manifest exceeds the fixed byte limit")
    if contains_secret_like(serialized):
        raise OfflinePreviewSafetyError("preview manifest contains secret-like content")
    if _HOST_PATH.search(serialized) is not None:
        raise OfflinePreviewSafetyError("preview manifest contains a host path")


def render_offline_preview_html(manifest: dict[str, object]) -> str:
    """Render a generic tokenless shell; feature wording stays in production payloads."""

    _validate_manifest(manifest)
    surfaces = manifest.get("surfaces")
    if not isinstance(surfaces, list):
        raise OfflinePreviewSafetyError("preview surfaces are invalid")
    cards = "".join(_render_surface_html(surface) for surface in surfaces)
    timestamp = html.escape(str(manifest["fixture_timestamp"]), quote=True)
    return (
        "<!doctype html>\n"
        '<html lang="ja"><head><meta charset="utf-8">'
        "<title>YonerAI Discord Offline Preview</title>"
        "<style>"
        "body{margin:0;background:#111318;color:#f2f3f5;font:14px system-ui,sans-serif}"
        "main{display:grid;gap:24px;padding:24px;grid-template-columns:minmax(320px,390px) minmax(720px,1fr)}"
        ".viewport{background:#1e1f22;border:1px solid #3f4147;border-radius:12px;padding:16px;overflow:hidden}"
        ".viewport h1{font-size:14px;color:#b5bac1;margin:0 0 12px}"
        ".message{background:#2b2d31;border-radius:8px;margin:0 0 12px;padding:12px;overflow-wrap:anywhere}"
        ".surface{font-size:11px;color:#949ba4;margin-bottom:6px}"
        ".content{white-space:pre-wrap}"
        ".embed{border-left:4px solid #5865f2;background:#232428;margin-top:8px;padding:10px}"
        ".embed h2{font-size:16px;margin:0 0 6px}.embed p{white-space:pre-wrap;margin:4px 0}"
        ".field{margin-top:8px}.field strong{display:block}.footer{color:#949ba4;font-size:11px;margin-top:8px}"
        ".components{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}"
        ".components span{background:#4e5058;border-radius:4px;padding:6px 9px}"
        "footer{padding:0 24px 24px;color:#949ba4}"
        "</style></head><body><main>"
        f'<section class="viewport mobile" data-width="390" data-height="844"><h1>Mobile 390×844</h1>{cards}</section>'
        f'<section class="viewport desktop" data-width="1280" data-height="720"><h1>Desktop 1280×720</h1>{cards}</section>'
        f"</main><footer>fixture {timestamp} / production payload projection</footer></body></html>\n"
    )


def _render_surface_html(value: object) -> str:
    if not isinstance(value, dict):
        raise OfflinePreviewSafetyError("preview surface is invalid")
    surface = value.get("surface")
    payload = value.get("payload")
    if not isinstance(surface, str) or not isinstance(payload, dict):
        raise OfflinePreviewSafetyError("preview surface payload is invalid")
    content = payload.get("content")
    content_html = f'<div class="content">{html.escape(content)}</div>' if isinstance(content, str) and content else ""
    embeds: list[dict[str, object]] = []
    embed = payload.get("embed")
    if isinstance(embed, dict):
        embeds.append(embed)
    more_embeds = payload.get("embeds")
    if isinstance(more_embeds, list):
        embeds.extend(item for item in more_embeds if isinstance(item, dict))
    embed_html = "".join(_render_embed_html(item) for item in embeds)
    components = payload.get("components")
    component_html = ""
    if isinstance(components, list):
        labels = [
            html.escape(str(item["label"]))
            for item in components
            if isinstance(item, dict) and isinstance(item.get("label"), str)
        ]
        if labels:
            component_html = (
                '<div class="components">' + "".join(f"<span>{label}</span>" for label in labels) + "</div>"
            )
    return (
        '<article class="message">'
        f'<div class="surface">{html.escape(surface)}</div>'
        f"{content_html}{embed_html}{component_html}</article>"
    )


def _render_embed_html(value: dict[str, object]) -> str:
    title = value.get("title")
    description = value.get("description")
    footer = value.get("footer")
    fields = value.get("fields")
    parts = ['<div class="embed">']
    if isinstance(title, str):
        parts.append(f"<h2>{html.escape(title)}</h2>")
    if isinstance(description, str):
        parts.append(f"<p>{html.escape(description)}</p>")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            name = field.get("name")
            field_value = field.get("value")
            if isinstance(name, str) and isinstance(field_value, str):
                parts.append(
                    f'<div class="field"><strong>{html.escape(name)}</strong>'
                    f"<span>{html.escape(field_value)}</span></div>"
                )
    if isinstance(footer, dict) and isinstance(footer.get("text"), str):
        parts.append(f'<div class="footer">{html.escape(footer["text"])}</div>')
    parts.append("</div>")
    return "".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OfflinePreviewSafetyError",
    "OfflinePreviewUnsupportedError",
    "build_offline_preview_manifest",
    "main",
    "render_offline_preview_html",
    "render_offline_preview_json",
]
