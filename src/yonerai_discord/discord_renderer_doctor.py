from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence

from yonerai_discord.discord_markdown import numbered_reference
from yonerai_discord.discord_payload_budget import (
    CONTENT_LIMIT,
    DiscordEmbedFieldText,
    DiscordEmbedText,
    DiscordPayloadUsage,
    fit_embed_description,
    validate_discord_payload_budget,
)
from yonerai_discord.modules.ai.task_progress import (
    AIProgressPlan,
    DiscordAITaskProgressSession,
    ProgressEditPolicy,
    TaskStatusEmojis,
)
from yonerai_discord.modules.music.dashboard import (
    MusicDashboardView,
    render_pending_music_dashboard,
)


_SCHEMA = "yonerai.discord.renderer-doctor.v1"
_COMPONENT_COUNT_LIMIT = 25
_COMPONENT_LABEL_LIMIT = 80
_COMPONENT_CUSTOM_ID_LIMIT = 100


class RendererDoctorStatus(StrEnum):
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RendererDoctorReport:
    status: RendererDoctorStatus
    checks: tuple[tuple[str, bool], ...]
    usage: tuple[tuple[str, dict[str, int]], ...]
    error_code: str | None

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "status": self.status.value,
            "checks": dict(self.checks),
            "usage": {name: dict(values) for name, values in self.usage},
            "error_code": self.error_code,
        }


def _usage_mapping(usage: DiscordPayloadUsage) -> dict[str, int]:
    return {
        "content_characters": usage.content_characters,
        "embed_characters": usage.embed_characters,
        "embed_count": usage.embed_count,
        "file_count": usage.file_count,
    }


def _check_plain() -> dict[str, int]:
    content = ("日本語の長文回答を安全な本文上限で確認します。" * 100)[:CONTENT_LIMIT]
    return _usage_mapping(validate_discord_payload_budget(content=content))


def _check_card() -> dict[str, int]:
    title = "YonerAI 応答カード"
    fields = (
        DiscordEmbedFieldText(name="実行状態", value="検証済み"),
        DiscordEmbedFieldText(name="次の操作", value="必要な場合だけ続きを表示します。"),
    )
    footer = "秘密値・内部path・生の例外本文は表示しません。"
    description, truncated = fit_embed_description(
        "長い日本語の説明をカード全体の上限へ安全に収めます。" * 300,
        title=title,
        fields=fields,
        footer=footer,
    )
    if not truncated:
        raise ValueError("card fixture did not exercise bounded rendering")
    return _usage_mapping(
        validate_discord_payload_budget(
            embeds=(
                DiscordEmbedText(
                    title=title,
                    description=description,
                    fields=fields,
                    footer=footer,
                ),
            )
        )
    )


def _check_sources() -> dict[str, int]:
    content = "\n".join(
        (
            "取得・検証済みの出典",
            numbered_reference(1, "https://docs.python.org/3/", "Python 公式ドキュメント"),
            numbered_reference(2, "https://www.rfc-editor.org/", "RFC Editor"),
            numbered_reference(3, "https://www.w3.org/TR/", "W3C Technical Reports"),
            "出典の取得日と分類は回答カード側の検証済み構造から表示します。",
        )
    )
    return _usage_mapping(validate_discord_payload_budget(content=content))


def _embed_usage(embed: object) -> dict[str, int]:
    title = str(getattr(embed, "title", "") or "")
    description = str(getattr(embed, "description", "") or "")
    footer = str(getattr(getattr(embed, "footer", None), "text", "") or "")
    author = str(getattr(getattr(embed, "author", None), "name", "") or "")
    fields = tuple(
        DiscordEmbedFieldText(name=str(field.name), value=str(field.value))
        for field in tuple(getattr(embed, "fields", ()))
    )
    return _usage_mapping(
        validate_discord_payload_budget(
            embeds=(
                DiscordEmbedText(
                    title=title,
                    description=description,
                    fields=fields,
                    footer=footer,
                    author=author,
                ),
            )
        )
    )


def _check_progress() -> dict[str, int]:
    plan = AIProgressPlan(
        title="複合依頼を実行しています",
        tasks=(
            "依頼を解析",
            "権限を再確認",
            "安全な候補を選択",
            "処理を実行",
            "結果を検証",
            "成果物を準備",
            "Discord向けに整形",
            "配信を確定",
        ),
        active_index=3,
    )
    session = DiscordAITaskProgressSession(
        message=None,
        plan=plan,
        emojis=TaskStatusEmojis(),
        edit_policy=ProgressEditPolicy(),
    )
    return _embed_usage(session._build_embed())  # noqa: SLF001 - doctor validates the production builder.


def _check_audio_dashboard() -> dict[str, int]:
    content = render_pending_music_dashboard(None)
    return _usage_mapping(validate_discord_payload_budget(content=content))


def _component_specs() -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for name in tuple(getattr(MusicDashboardView, "__view_children_items__", ())):
        method = getattr(MusicDashboardView, name, None)
        kwargs = getattr(method, "__discord_ui_model_kwargs__", None)
        if not isinstance(kwargs, dict):
            raise ValueError("component metadata is unavailable")
        label = kwargs.get("label")
        custom_id = kwargs.get("custom_id")
        if not isinstance(label, str) or not isinstance(custom_id, str):
            raise ValueError("component metadata is invalid")
        items.append((label, custom_id))
    return tuple(items)


def _check_components() -> dict[str, int]:
    components = _component_specs()
    custom_ids = tuple(custom_id for _label, custom_id in components)
    if (
        not components
        or len(components) > _COMPONENT_COUNT_LIMIT
        or len(set(custom_ids)) != len(custom_ids)
        or any(not label or len(label) > _COMPONENT_LABEL_LIMIT for label, _custom_id in components)
        or any(not custom_id or len(custom_id) > _COMPONENT_CUSTOM_ID_LIMIT for _label, custom_id in components)
    ):
        raise ValueError("component contract is outside the Discord limit")
    return {
        "component_count": len(components),
        "longest_label": max(len(label) for label, _custom_id in components),
        "longest_custom_id": max(len(custom_id) for _label, custom_id in components),
    }


_CHECKS: tuple[tuple[str, Callable[[], dict[str, int]]], ...] = (
    ("plain", _check_plain),
    ("card", _check_card),
    ("sources", _check_sources),
    ("progress", _check_progress),
    ("audio_dashboard", _check_audio_dashboard),
    ("components", _check_components),
)


def run_renderer_doctor() -> RendererDoctorReport:
    checks: list[tuple[str, bool]] = []
    usage: list[tuple[str, dict[str, int]]] = []
    for name, check in _CHECKS:
        try:
            values = check()
        except Exception:
            checks.append((name, False))
            return RendererDoctorReport(
                status=RendererDoctorStatus.FAILED,
                checks=tuple(checks),
                usage=tuple(usage),
                error_code=f"{name}_payload_invalid",
            )
        checks.append((name, True))
        usage.append((name, values))
    return RendererDoctorReport(
        status=RendererDoctorStatus.READY,
        checks=tuple(checks),
        usage=tuple(usage),
        error_code=None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yonerai-discord-renderer-doctor",
        description="Tokenless Discord renderer payload doctor",
    )
    parser.parse_args(argv)
    report = run_renderer_doctor()
    print(json.dumps(report.to_mapping(), ensure_ascii=False, separators=(",", ":")))
    return 0 if report.status is RendererDoctorStatus.READY else 2


if __name__ == "__main__":
    raise SystemExit(main())
