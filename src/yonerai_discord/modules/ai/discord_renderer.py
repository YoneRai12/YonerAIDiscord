"""AI応答をDiscordのカードと添付に変換する表示専用モジュール。"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import discord

from yonerai_discord.discord_payload_budget import (
    CONTENT_LIMIT,
    EMBED_DESCRIPTION_LIMIT,
    FILE_COUNT_LIMIT,
    DiscordEmbedFieldText,
    DiscordEmbedText,
    fit_embed_description,
    maximum_embed_description_characters,
    validate_discord_payload_budget,
)
from yonerai_discord.discord_markdown import numbered_link, safe_http_url
from yonerai_discord.modules.media_pipeline.delivery import PreparedMediaAttachment

from .artifacts import ArtifactStore, ArtifactStoreError, StoredArtifact
from .display_preferences import DisplayMode
from .site_delivery import PublishedSite


logger = logging.getLogger(__name__)

_EMBED_DESCRIPTION_LIMIT = EMBED_DESCRIPTION_LIMIT
_SUMMARY_LIMIT = 3_500
_MODEL_FOOTER_LIMIT = 256
_TASK_SUMMARY_LIMIT = 1_024
_INLINE_CODE_LIMIT = 1_200
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
_MAX_ATTACHMENTS_PER_MESSAGE = FILE_COUNT_LIMIT
_MAX_PREPARED_MEDIA_ATTACHMENTS = 4
_PLAIN_CONTENT_LIMIT = CONTENT_LIMIT
_PLAIN_SUMMARY_LIMIT = 1_500
_DISCORD_IO_TIMEOUT_SECONDS = 15.0
_FOLLOWUP_DELIVERY_ERROR = (
    "AI回答は生成しましたが、Discordへ安全に表示できませんでした。少し短くしてもう一度お試しください。"
)
_NUMBERED_LINK = re.compile(r"\[(?P<number>[1-9][0-9]*)\]\((?P<url>https?://[^\s)]+)\)")
_FENCED_CODE = re.compile(r"```(?P<language>[A-Za-z0-9_+.-]{0,32})[ \t]*\r?\n(?P<body>.*?)```", re.DOTALL)
_HTML_DOCUMENT = re.compile(r"(?:<!doctype\s+html\b|<html\b)", re.IGNORECASE)
_EXTENSIONS = {
    "bash": "sh",
    "c++": "cpp",
    "cs": "cs",
    "css": "css",
    "html": "html",
    "java": "java",
    "javascript": "js",
    "js": "js",
    "json": "json",
    "md": "md",
    "php": "php",
    "py": "py",
    "python": "py",
    "rb": "rb",
    "rust": "rs",
    "sh": "sh",
    "sql": "sql",
    "ts": "ts",
    "typescript": "ts",
    "xml": "xml",
    "yaml": "yaml",
    "yml": "yml",
}


@dataclass(frozen=True, slots=True)
class RenderedAIResponse:
    primary_message: discord.Message | None
    full_text: str
    artifact: StoredArtifact | None = None
    attachment_filenames: tuple[str, ...] = ()
    reused_message: bool = False
    published_site: PublishedSite | None = None


@dataclass(slots=True)
class PreparedAIResponse:
    """表示内容とDiscord送信payloadを分離した、送信先非依存の描画結果。"""

    full_text: str
    kwargs: dict[str, Any]
    artifact: StoredArtifact | None = None
    attachment_filenames: tuple[str, ...] = ()
    published_site: PublishedSite | None = None


class DiscordAIResponseRenderer:
    """Discord APIをここだけに閉じ込め、会話履歴には必ず元の全文を返す。"""

    def __init__(self, *, artifact_store: ArtifactStore | None = None, artifact_scope: str = "discord") -> None:
        self.artifact_store = artifact_store
        self.artifact_scope = artifact_scope

    async def reply(
        self,
        source_message: Any,
        content: str,
        *,
        model: str,
        prompt: str = "",
        artifact_scope: str | None = None,
        existing_message: Any | None = None,
        task_summary: str = "",
        published_site: PublishedSite | None = None,
        site_publish_notice: str = "",
        display_mode: DisplayMode = DisplayMode.CARD,
        send_allowed: Callable[[], bool] | None = None,
        fresh_send_allowed: Callable[[], bool | Awaitable[bool]] | None = None,
        media_attachments: tuple[PreparedMediaAttachment, ...] = (),
    ) -> RenderedAIResponse:
        prepared = self.prepare_payload(
            content,
            model=model,
            prompt=prompt,
            artifact_scope=artifact_scope,
            task_summary=task_summary,
            published_site=published_site,
            site_publish_notice=site_publish_notice,
            display_mode=display_mode,
            media_attachments=media_attachments,
        )
        if media_attachments and (
            fresh_send_allowed is None or not await _fresh_send_currently_allowed(fresh_send_allowed)
        ):
            _close_files(prepared.kwargs.get("files", ()))
            return RenderedAIResponse(
                primary_message=None,
                full_text=prepared.full_text,
                artifact=prepared.artifact,
                attachment_filenames=prepared.attachment_filenames,
                published_site=prepared.published_site,
            )
        primary, reused_message = await _edit_or_reply(
            source_message,
            prepared.kwargs,
            existing_message=existing_message,
            send_allowed=send_allowed,
            fresh_send_allowed=fresh_send_allowed,
        )
        return RenderedAIResponse(
            primary_message=primary,
            full_text=prepared.full_text,
            artifact=prepared.artifact,
            attachment_filenames=prepared.attachment_filenames,
            reused_message=reused_message,
            published_site=prepared.published_site,
        )

    def prepare_payload(
        self,
        content: str,
        *,
        model: str,
        prompt: str = "",
        artifact_scope: str | None = None,
        task_summary: str = "",
        published_site: PublishedSite | None = None,
        site_publish_notice: str = "",
        display_mode: DisplayMode = DisplayMode.CARD,
        media_attachments: tuple[PreparedMediaAttachment, ...] = (),
    ) -> PreparedAIResponse:
        """CARD/PLAIN共通の本文・添付・安全な送信payloadを準備する。

        ``reply`` とInteractionのfollowupは送信方法だけが違うため、本文の切詰め、
        UTF-8添付、番号リンク、モデル表記、mention抑止は必ずここを経由させる。
        """
        full_text = _normalise_text(content)
        selected_mode = DisplayMode(display_mode)
        validated_media = _validated_media_attachments(media_attachments)
        files: list[discord.File] = []
        filenames: list[str] = []
        artifact: StoredArtifact | None = None
        code_blocks = _extract_code_blocks(full_text)

        html = _find_html(full_text)
        if html is not None and self.artifact_store is not None:
            try:
                artifact = self.artifact_store.save_html(
                    prompt or "yonerai-web",
                    html,
                    scope=artifact_scope or self.artifact_scope,
                )
            except (ArtifactStoreError, OSError, ValueError) as exc:
                logger.warning("ai_artifact_store_failed", extra={"error_type": type(exc).__name__})
            else:
                html_file = _file_from_text(html, artifact.filename)
                if html_file is not None:
                    files.append(html_file)
                    filenames.append(artifact.filename)
        elif html is not None:
            filename = "yonerai-web.html"
            html_file = _file_from_text(html, filename)
            if html_file is not None:
                files.append(html_file)
                filenames.append(filename)

        try:
            for index, block in enumerate(code_blocks, start=1):
                if block.extension == "html" and html is not None:
                    continue
                if len(block.body) <= _INLINE_CODE_LIMIT and len(code_blocks) == 1:
                    continue
                filename = f"yonerai-code-{index}.{block.extension}"
                file = _file_from_text(block.body, filename)
                if file is not None:
                    files.append(file)
                    filenames.append(filename)
        except BaseException:
            _close_files(files)
            raise

        card_title = ""
        card_footer = ""
        card_fields: tuple[DiscordEmbedFieldText, ...] = ()
        card_description_limit = _EMBED_DESCRIPTION_LIMIT
        try:
            if selected_mode is DisplayMode.CARD:
                card_title = "YonerAI Web成果物" if html is not None else ("YonerAI Code" if code_blocks else "YonerAI")
                field_items: list[DiscordEmbedFieldText] = []
                if published_site is not None:
                    action = "更新" if published_site.updated else "公開"
                    field_items.append(
                        DiscordEmbedFieldText(
                            name=f"サイトを{action}しました",
                            value=numbered_published_site_text(
                                full_text,
                                published_site,
                                reply_target="カード",
                            ),
                        )
                    )
                elif site_publish_notice.strip():
                    field_items.append(
                        DiscordEmbedFieldText(
                            name="サイト公開",
                            value=site_publish_notice.strip()[:1_024],
                        )
                    )
                if task_summary.strip():
                    field_items.append(
                        DiscordEmbedFieldText(
                            name="完了したタスク",
                            value=task_summary.strip()[:_TASK_SUMMARY_LIMIT],
                        )
                    )
                card_fields = tuple(field_items)
                card_footer = f"モデル: {_safe_model(model)}"
                card_description_limit = maximum_embed_description_characters(
                    title=card_title,
                    fields=card_fields,
                    footer=card_footer,
                )
        except BaseException:
            _close_files(files)
            raise

        try:
            card_attachment_suffix = "\n\n関連ファイルを添付しました。"
            card_visible_limit = card_description_limit
            if html is None and (files or validated_media):
                card_visible_limit = max(1, card_visible_limit - len(card_attachment_suffix))
            long_output = len(full_text) > (
                _PLAIN_SUMMARY_LIMIT if selected_mode is DisplayMode.PLAIN else card_visible_limit
            )
            if long_output:
                markdown_file = _file_from_text(full_text, "yonerai-answer.md")
                if markdown_file is None:
                    raise ValueError("full AI response attachment is unavailable")
                files.insert(0, markdown_file)
                filenames.insert(0, "yonerai-answer.md")
        except BaseException:
            _close_files(files)
            raise

        try:
            if validated_media and len(files) + len(validated_media) > _MAX_ATTACHMENTS_PER_MESSAGE:
                raise ValueError("media attachments exceed the Discord attachment limit")
            for attachment in validated_media:
                files.append(_file_from_media_attachment(attachment))
                filenames.append(attachment.filename)

            files, filenames = _bounded_attachments(files, filenames)
        except BaseException:
            _close_files(files)
            raise

        try:
            if selected_mode is DisplayMode.CARD:
                description = (
                    _html_artifact_description(attached=bool(files))
                    if html is not None
                    else _description(
                        full_text,
                        long_output=long_output,
                        attached=bool(files),
                        maximum=card_description_limit,
                    )
                )
                description, description_truncated = fit_embed_description(
                    description,
                    title=card_title,
                    fields=card_fields,
                    footer=card_footer,
                )
                if description_truncated:
                    raise ValueError("AI response exceeds the Discord embed budget")
                embed = discord.Embed(
                    title=card_title,
                    description=description,
                    colour=discord.Colour.blurple(),
                )
                for field in card_fields:
                    embed.add_field(
                        name=field.name,
                        value=field.value,
                        inline=False,
                    )
                embed.set_footer(text=card_footer)
                validate_discord_payload_budget(
                    embeds=(
                        DiscordEmbedText(
                            title=card_title,
                            description=description,
                            fields=card_fields,
                            footer=card_footer,
                        ),
                    ),
                    file_count=len(files),
                )
                kwargs: dict[str, Any] = {
                    "embed": embed,
                    "mention_author": False,
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
            else:
                kwargs = {
                    "content": _plain_content(
                        full_text,
                        model=model,
                        long_output=long_output,
                        task_summary=task_summary,
                        published_site=published_site,
                        site_publish_notice=site_publish_notice,
                    ),
                    "mention_author": False,
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                validate_discord_payload_budget(
                    content=kwargs["content"],
                    file_count=len(files),
                )
            if files:
                kwargs["files"] = files
            return PreparedAIResponse(
                full_text=full_text,
                artifact=artifact,
                attachment_filenames=tuple(filenames),
                published_site=published_site,
                kwargs=kwargs,
            )
        except BaseException:
            _close_files(files)
            raise

    async def send_followup(
        self,
        followup: Any,
        content: str,
        *,
        model: str,
        prompt: str = "",
        artifact_scope: str | None = None,
        task_summary: str = "",
        published_site: PublishedSite | None = None,
        site_publish_notice: str = "",
        display_mode: DisplayMode = DisplayMode.CARD,
        ephemeral: bool = True,
        media_attachments: tuple[PreparedMediaAttachment, ...] = (),
        fresh_send_allowed: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> RenderedAIResponse:
        """Interaction followupへ安全に送信する。

        Webhook.sendはMessage.reply固有の``mention_author``を受け取れないため、
        followup用payloadでは除去する。送信失敗は秘密やAI本文を出さず記録し、
        呼び出し側のCapability/Data Boundaryを変えない。
        """
        prepared = self.prepare_payload(
            content,
            model=model,
            prompt=prompt,
            artifact_scope=artifact_scope,
            task_summary=task_summary,
            published_site=published_site,
            site_publish_notice=site_publish_notice,
            display_mode=display_mode,
            media_attachments=media_attachments,
        )
        kwargs = _followup_kwargs(prepared.kwargs, ephemeral=ephemeral)
        primary: discord.Message | None = None
        if media_attachments and (
            fresh_send_allowed is None or not await _fresh_send_currently_allowed(fresh_send_allowed)
        ):
            _close_files(prepared.kwargs.get("files", ()))
            return RenderedAIResponse(
                primary_message=None,
                full_text=prepared.full_text,
                artifact=prepared.artifact,
                attachment_filenames=prepared.attachment_filenames,
                published_site=prepared.published_site,
            )
        try:
            if media_attachments and not await _fresh_send_currently_allowed(fresh_send_allowed):
                _close_files(prepared.kwargs.get("files", ()))
                return RenderedAIResponse(
                    primary_message=None,
                    full_text=prepared.full_text,
                    artifact=prepared.artifact,
                    attachment_filenames=prepared.attachment_filenames,
                    published_site=prepared.published_site,
                )
            sent = await _discord_io_call(followup.send(**kwargs))
            if sent is not None:
                primary = sent
        except TimeoutError:
            logger.warning("ai_renderer_followup_timeout")
            _close_files(prepared.kwargs.get("files", ()))
            return RenderedAIResponse(
                primary_message=None,
                full_text=prepared.full_text,
                artifact=prepared.artifact,
                attachment_filenames=prepared.attachment_filenames,
                published_site=prepared.published_site,
            )
        except Exception as exc:
            logger.warning("ai_renderer_followup_failed", extra={"error_type": type(exc).__name__})
            _close_files(prepared.kwargs.get("files", ()))
            if media_attachments and not await _fresh_send_currently_allowed(fresh_send_allowed):
                return RenderedAIResponse(
                    primary_message=None,
                    full_text=prepared.full_text,
                    artifact=prepared.artifact,
                    attachment_filenames=prepared.attachment_filenames,
                    published_site=prepared.published_site,
                )
            try:
                sent = await _discord_io_call(
                    followup.send(
                        content=_FOLLOWUP_DELIVERY_ERROR,
                        ephemeral=ephemeral,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                )
                if sent is not None:
                    primary = sent
            except TimeoutError:
                logger.warning("ai_renderer_followup_fallback_timeout")
            except Exception as fallback_exc:
                logger.warning(
                    "ai_renderer_followup_fallback_failed",
                    extra={"error_type": type(fallback_exc).__name__},
                )
        return RenderedAIResponse(
            primary_message=primary,
            full_text=prepared.full_text,
            artifact=prepared.artifact,
            attachment_filenames=prepared.attachment_filenames,
            published_site=prepared.published_site,
        )


@dataclass(frozen=True, slots=True)
class _CodeBlock:
    language: str
    body: str
    extension: str


def _extract_code_blocks(text: str) -> tuple[_CodeBlock, ...]:
    blocks: list[_CodeBlock] = []
    for match in _FENCED_CODE.finditer(text):
        body = match.group("body").strip("\n")
        if not body:
            continue
        language = match.group("language").casefold()
        blocks.append(_CodeBlock(language=language, body=body, extension=_EXTENSIONS.get(language, "txt")))
    return tuple(blocks)


def _find_html(text: str) -> str | None:
    for block in _extract_code_blocks(text):
        if block.extension == "html" and _HTML_DOCUMENT.search(block.body):
            return block.body
    match = _HTML_DOCUMENT.search(text)
    if match is None:
        return None
    end = re.search(r"</html\s*>", text[match.start() :], re.IGNORECASE)
    if end is None:
        return text[match.start() :]
    return text[match.start() : match.start() + end.end()]


def extract_html_document(text: str) -> str | None:
    """AI回答から公開候補となる完全HTML文書を副作用なしで抽出する。"""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return _find_html(text)


def _file_from_text(text: str, filename: str) -> discord.File | None:
    data = text.encode("utf-8")
    if len(data) > _MAX_ATTACHMENT_BYTES:
        logger.warning(
            "ai_renderer_attachment_too_large",
            extra={"attachment_name": filename, "bytes": len(data)},
        )
        return None
    return discord.File(io.BytesIO(data), filename=filename, spoiler=False)


def _validated_media_attachments(
    attachments: tuple[PreparedMediaAttachment, ...],
) -> tuple[PreparedMediaAttachment, ...]:
    if not isinstance(attachments, tuple) or len(attachments) > _MAX_PREPARED_MEDIA_ATTACHMENTS:
        raise ValueError("media attachments are unavailable")
    if any(not isinstance(attachment, PreparedMediaAttachment) for attachment in attachments):
        raise ValueError("media attachments are unavailable")
    filenames = [attachment.filename for attachment in attachments]
    if len(set(filenames)) != len(filenames):
        raise ValueError("media attachments are unavailable")
    for attachment in attachments:
        try:
            PreparedMediaAttachment(
                filename=attachment.filename,
                data=attachment.data,
                media_type=attachment.media_type,
                kind=attachment.kind,
                width=attachment.width,
                height=attachment.height,
            )
        except (TypeError, ValueError):
            raise ValueError("media attachments are unavailable") from None
    return attachments


def _file_from_media_attachment(attachment: PreparedMediaAttachment) -> discord.File:
    return discord.File(io.BytesIO(attachment.data), filename=attachment.filename, spoiler=False)


def _bounded_attachments(
    files: list[discord.File],
    filenames: list[str],
) -> tuple[list[discord.File], list[str]]:
    """Respect Discord's ten-attachment limit while retaining the full-answer file first."""

    if len(files) <= _MAX_ATTACHMENTS_PER_MESSAGE:
        return files, filenames
    omitted = files[_MAX_ATTACHMENTS_PER_MESSAGE:]
    for file in omitted:
        try:
            file.close()
        except Exception:
            continue
    logger.warning(
        "ai_renderer_attachment_count_limited",
        extra={
            "candidate_count": len(files),
            "sent_count": _MAX_ATTACHMENTS_PER_MESSAGE,
        },
    )
    return (
        files[:_MAX_ATTACHMENTS_PER_MESSAGE],
        filenames[:_MAX_ATTACHMENTS_PER_MESSAGE],
    )


def _normalise_text(content: str) -> str:
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    return content.strip() or "回答を生成できませんでした。"


def _description(
    text: str,
    *,
    long_output: bool,
    attached: bool,
    maximum: int = _EMBED_DESCRIPTION_LIMIT,
) -> str:
    if long_output:
        summary = text[:_SUMMARY_LIMIT].rstrip()
        suffixes = ["全文は UTF-8 の Markdown 添付にあります。"]
        if references := _numbered_reference_summary(text):
            suffixes.append(references)
        suffix = "\n\n" + "\n\n".join(suffixes)
        return (summary[: max(0, maximum - len(suffix))].rstrip() + suffix).strip()
    if attached:
        suffix = "\n\n関連ファイルを添付しました。"
        return (text[: max(0, maximum - len(suffix))].rstrip() + suffix).strip()
    return text[:maximum]


def _html_artifact_description(*, attached: bool) -> str:
    description = "HTML成果物を生成しました。"
    if attached:
        description += "\n\n関連ファイルを添付しました。"
    return description


def _safe_model(model: str) -> str:
    value = model.strip() if isinstance(model, str) else "unknown"
    return value[:_MODEL_FOOTER_LIMIT] or "unknown"


def _plain_content(
    text: str,
    *,
    model: str,
    long_output: bool,
    task_summary: str,
    published_site: PublishedSite | None,
    site_publish_notice: str,
) -> str:
    body = text[:_PLAIN_SUMMARY_LIMIT].rstrip() if long_output else text
    suffixes: list[str] = []
    if long_output:
        suffixes.append("全文は UTF-8 の Markdown 添付にあります。")
        if references := _numbered_reference_summary(text):
            suffixes.append(references)
    if published_site is not None:
        suffixes.append(numbered_published_site_text(text, published_site))
    elif site_publish_notice.strip():
        suffixes.append(f"サイト公開: {site_publish_notice.strip()[:500]}")
    if task_summary.strip():
        suffixes.append(f"完了したタスク\n{task_summary.strip()[:500]}")
    suffixes.append(f"-# {_safe_model(model)}")
    suffix = "\n\n" + "\n\n".join(suffixes)
    maximum = max(1, _PLAIN_CONTENT_LIMIT - len(suffix))
    return (body[:maximum].rstrip() + suffix)[:_PLAIN_CONTENT_LIMIT]


def numbered_published_site_text(
    text: str,
    published_site: PublishedSite,
    *,
    reply_target: str = "メッセージ",
) -> str:
    return (
        f"公開サイト: {safe_http_url(published_site.site_url)}\n"
        f"版: `v{published_site.revision}` / 公開範囲: `{published_site.visibility}`\n"
        f"この{reply_target}へ返信すると、同じURLの新しい版として編集できます。"
    )


def _numbered_reference_summary(text: str, *, maximum: int = 500) -> str:
    """切詰め表示でも、AI本文内の安全な番号参照を残す。"""

    references: list[str] = []
    seen: set[int] = set()
    for match in _NUMBERED_LINK.finditer(text):
        number = int(match.group("number"))
        if number in seen:
            continue
        try:
            reference = numbered_link(number, safe_http_url(match.group("url")))
        except ValueError:
            continue
        candidate = "参照: " + " ".join((*references, reference))
        if len(candidate) > maximum:
            break
        references.append(reference)
        seen.add(number)
    return "参照: " + " ".join(references) if references else ""


def _followup_kwargs(kwargs: dict[str, Any], *, ephemeral: bool) -> dict[str, Any]:
    """Message.reply固有の引数を取り除き、Webhook.send向けにする。"""

    followup_kwargs = dict(kwargs)
    followup_kwargs.pop("mention_author", None)
    followup_kwargs["ephemeral"] = ephemeral
    return followup_kwargs


async def _edit_or_reply(
    source_message: Any,
    kwargs: dict[str, Any],
    *,
    existing_message: Any | None,
    send_allowed: Callable[[], bool] | None,
    fresh_send_allowed: Callable[[], bool | Awaitable[bool]] | None,
) -> tuple[discord.Message | None, bool]:
    if not _send_currently_allowed(send_allowed) or not await _fresh_send_currently_allowed(fresh_send_allowed):
        _close_files(kwargs.get("files", ()))
        return None, False
    if existing_message is not None:
        edit = getattr(existing_message, "edit", None)
        if callable(edit):
            edit_kwargs: dict[str, Any] = {"allowed_mentions": kwargs["allowed_mentions"]}
            if "embed" in kwargs:
                edit_kwargs.update(content=None, embed=kwargs["embed"])
            else:
                edit_kwargs.update(content=kwargs["content"], embed=None)
            # discord.py accepts new File objects through Message.edit(attachments=...).
            edit_kwargs["attachments"] = kwargs.get("files", [])
            try:
                edited = await _discord_io_call(edit(**edit_kwargs))
            except TimeoutError:
                logger.warning("ai_renderer_edit_timeout")
                if not _send_currently_allowed(send_allowed) or not await _fresh_send_currently_allowed(
                    fresh_send_allowed
                ):
                    _close_files(kwargs.get("files", ()))
                    return None, False
                _rewind_files(kwargs.get("files", ()))
                try:
                    edited = await _discord_io_call(edit(**edit_kwargs))
                except TimeoutError:
                    logger.warning("ai_renderer_edit_retry_timeout")
                    _close_files(kwargs.get("files", ()))
                    return None, False
                except Exception as retry_exc:
                    logger.warning(
                        "ai_renderer_edit_retry_failed",
                        extra={"error_type": type(retry_exc).__name__},
                    )
                    _close_files(kwargs.get("files", ()))
                    return None, False
                return edited or existing_message, True
            except Exception as exc:
                logger.warning("ai_renderer_edit_failed", extra={"error_type": type(exc).__name__})
                _rewind_files(kwargs.get("files", ()))
            else:
                return edited or existing_message, True

    if not _send_currently_allowed(send_allowed) or not await _fresh_send_currently_allowed(fresh_send_allowed):
        _close_files(kwargs.get("files", ()))
        return None, False
    try:
        return await _discord_io_call(source_message.reply(**kwargs)), False
    except TimeoutError:
        logger.warning("ai_renderer_reply_timeout")
        _close_files(kwargs.get("files", ()))
        return None, False
    except Exception as exc:
        logger.warning("ai_renderer_reply_failed", extra={"error_type": type(exc).__name__})
    if not _send_currently_allowed(send_allowed) or not await _fresh_send_currently_allowed(fresh_send_allowed):
        _close_files(kwargs.get("files", ()))
        return None, False
    channel = getattr(source_message, "channel", None)
    send = getattr(channel, "send", None)
    if not callable(send):
        _close_files(kwargs.get("files", ()))
        return None, False
    fallback_kwargs = dict(kwargs)
    fallback_kwargs.pop("mention_author", None)
    reference = _message_reference(source_message)
    if reference is not None:
        fallback_kwargs["reference"] = reference
    _rewind_files(fallback_kwargs.get("files", ()))
    if not _send_currently_allowed(send_allowed) or not await _fresh_send_currently_allowed(fresh_send_allowed):
        _close_files(fallback_kwargs.get("files", ()))
        return None, False
    try:
        return await _discord_io_call(send(**fallback_kwargs)), False
    except TimeoutError:
        logger.warning("ai_renderer_fallback_timeout")
        _close_files(fallback_kwargs.get("files", ()))
        return None, False
    except Exception as exc:
        logger.warning("ai_renderer_fallback_failed", extra={"error_type": type(exc).__name__})
    if "reference" in fallback_kwargs:
        fallback_kwargs.pop("reference", None)
        _rewind_files(fallback_kwargs.get("files", ()))
        if not _send_currently_allowed(send_allowed) or not await _fresh_send_currently_allowed(fresh_send_allowed):
            _close_files(fallback_kwargs.get("files", ()))
            return None, False
        try:
            return await _discord_io_call(send(**fallback_kwargs)), False
        except TimeoutError:
            logger.warning("ai_renderer_unreferenced_fallback_timeout")
            _close_files(fallback_kwargs.get("files", ()))
            return None, False
        except Exception as exc:
            logger.warning("ai_renderer_unreferenced_fallback_failed", extra={"error_type": type(exc).__name__})
    _close_files(fallback_kwargs.get("files", ()))
    return None, False


async def _discord_io_call(awaitable: Awaitable[Any]) -> Any:
    """Discordのedit/reply/sendが無期限に止まり、会話leaseを保持し続けるのを防ぐ。"""

    async with asyncio.timeout(_DISCORD_IO_TIMEOUT_SECONDS):
        return await awaitable


def _send_currently_allowed(check: Callable[[], bool] | None) -> bool:
    if check is None:
        return True
    try:
        return check() is True
    except Exception:
        return False


async def _fresh_send_currently_allowed(
    check: Callable[[], bool | Awaitable[bool]] | None,
) -> bool:
    if check is None:
        return True
    try:
        current = check()
        if hasattr(current, "__await__"):
            current = await current
        return current is True
    except Exception:
        return False


def _message_reference(source_message: Any) -> Any | None:
    to_reference = getattr(source_message, "to_reference", None)
    if not callable(to_reference):
        return None
    try:
        return to_reference(fail_if_not_exists=False)
    except (AttributeError, TypeError, ValueError):
        return None


def _rewind_files(files: object) -> None:
    if not isinstance(files, (list, tuple)):
        return
    for file in files:
        stream = getattr(file, "fp", None)
        seek = getattr(stream, "seek", None)
        if callable(seek):
            try:
                seek(0)
            except (OSError, ValueError):
                continue


def _close_files(files: object) -> None:
    if not isinstance(files, (list, tuple)):
        return
    for file in files:
        close = getattr(file, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        stream = getattr(file, "fp", None)
        stream_close = getattr(stream, "close", None)
        if callable(stream_close):
            try:
                stream_close()
            except Exception:
                pass
