from __future__ import annotations

from dataclasses import dataclass

from .domain import DEFAULT_AUDIT_CONTENT_LIMIT, MAX_AUDIT_CONTENT_LIMIT


@dataclass(frozen=True, slots=True)
class MemberTemplateValues:
    mention: str
    display_name: str
    guild_name: str
    member_count: int | None = None


def render_member_message(template: str, values: MemberTemplateValues) -> str:
    replacements = {
        "{mention}": values.mention,
        "{name}": values.display_name,
        "{guild}": values.guild_name,
        "{member_count}": str(values.member_count) if values.member_count is not None else "?",
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered[:2_000]


def _one_line(value: str, limit: int) -> str:
    normalized = " ".join(value.replace("\x00", "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)] + "…"


def render_message_delete(
    *,
    author: str,
    channel: str,
    content: str,
    include_content: bool = False,
    content_limit: int = DEFAULT_AUDIT_CONTENT_LIMIT,
) -> str:
    _validate_limit(content_limit)
    message = f"🗑️ メッセージ削除｜投稿者: {author}｜チャンネル: {channel}"
    if include_content and content:
        message += f"\n本文抜粋: {_one_line(content, content_limit)}"
    return message


def render_message_edit(
    *,
    author: str,
    channel: str,
    before: str,
    after: str,
    include_content: bool = False,
    content_limit: int = DEFAULT_AUDIT_CONTENT_LIMIT,
) -> str:
    _validate_limit(content_limit)
    message = f"✏️ メッセージ編集｜投稿者: {author}｜チャンネル: {channel}"
    if include_content:
        message += f"\n変更前: {_one_line(before, content_limit)}\n変更後: {_one_line(after, content_limit)}"
    return message


def _validate_limit(value: int) -> None:
    if not 1 <= value <= MAX_AUDIT_CONTENT_LIMIT:
        raise ValueError(f"content_limit must be 1..{MAX_AUDIT_CONTENT_LIMIT}")
