"""Discord payload の公開文字数・個数上限を副作用なしで検証する。"""

from __future__ import annotations

from dataclasses import dataclass


CONTENT_LIMIT = 2_000
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4_096
EMBED_FIELD_COUNT_LIMIT = 25
EMBED_FIELD_NAME_LIMIT = 256
EMBED_FIELD_VALUE_LIMIT = 1_024
EMBED_FOOTER_LIMIT = 2_048
EMBED_AUTHOR_LIMIT = 256
EMBED_TOTAL_LIMIT = 6_000
EMBED_COUNT_LIMIT = 10
FILE_COUNT_LIMIT = 10


class DiscordPayloadBudgetError(ValueError):
    """Discord が拒否する payload budget を表す固定例外。"""


@dataclass(frozen=True, slots=True)
class DiscordEmbedFieldText:
    name: str
    value: str

    def __post_init__(self) -> None:
        _require_text(self.name, "field name")
        _require_text(self.value, "field value")


@dataclass(frozen=True, slots=True)
class DiscordEmbedText:
    title: str = ""
    description: str = ""
    fields: tuple[DiscordEmbedFieldText, ...] = ()
    footer: str = ""
    author: str = ""

    def __post_init__(self) -> None:
        _require_text(self.title, "embed title")
        _require_text(self.description, "embed description")
        _require_text(self.footer, "embed footer")
        _require_text(self.author, "embed author")
        if not isinstance(self.fields, tuple) or any(
            not isinstance(field, DiscordEmbedFieldText) for field in self.fields
        ):
            raise TypeError("embed fields must be a tuple of DiscordEmbedFieldText")

    @property
    def character_count(self) -> int:
        return (
            len(self.title)
            + len(self.description)
            + len(self.footer)
            + len(self.author)
            + sum(len(field.name) + len(field.value) for field in self.fields)
        )


@dataclass(frozen=True, slots=True)
class DiscordPayloadUsage:
    content_characters: int
    embed_characters: int
    embed_count: int
    file_count: int


def maximum_embed_description_characters(
    *,
    title: str = "",
    fields: tuple[DiscordEmbedFieldText, ...] = (),
    footer: str = "",
    author: str = "",
) -> int:
    """他の embed 要素を固定したとき、安全に使える description 長を返す。"""

    shell = DiscordEmbedText(title=title, fields=fields, footer=footer, author=author)
    _validate_embed_elements(shell)
    remaining = EMBED_TOTAL_LIMIT - shell.character_count
    if remaining < 0:
        raise DiscordPayloadBudgetError("embed text exceeds the Discord total limit")
    return min(EMBED_DESCRIPTION_LIMIT, remaining)


def fit_embed_description(
    description: str,
    *,
    title: str = "",
    fields: tuple[DiscordEmbedFieldText, ...] = (),
    footer: str = "",
    author: str = "",
) -> tuple[str, bool]:
    """description を総量上限へ収め、切り詰めの有無を返す。"""

    _require_text(description, "embed description")
    maximum = maximum_embed_description_characters(
        title=title,
        fields=fields,
        footer=footer,
        author=author,
    )
    fitted = description[:maximum]
    return fitted, len(fitted) != len(description)


def validate_discord_payload_budget(
    *,
    content: str = "",
    embeds: tuple[DiscordEmbedText, ...] = (),
    file_count: int = 0,
) -> DiscordPayloadUsage:
    """Discord content/embed/file の hard limit をまとめて検証する。"""

    _require_text(content, "content")
    if not isinstance(embeds, tuple) or any(not isinstance(embed, DiscordEmbedText) for embed in embeds):
        raise TypeError("embeds must be a tuple of DiscordEmbedText")
    if isinstance(file_count, bool) or not isinstance(file_count, int):
        raise TypeError("file_count must be an integer")
    if len(content) > CONTENT_LIMIT:
        raise DiscordPayloadBudgetError("content exceeds the Discord limit")
    if len(embeds) > EMBED_COUNT_LIMIT:
        raise DiscordPayloadBudgetError("embed count exceeds the Discord limit")
    if file_count < 0 or file_count > FILE_COUNT_LIMIT:
        raise DiscordPayloadBudgetError("file count exceeds the Discord limit")
    for embed in embeds:
        _validate_embed_elements(embed)
    embed_characters = sum(embed.character_count for embed in embeds)
    if embed_characters > EMBED_TOTAL_LIMIT:
        raise DiscordPayloadBudgetError("embed text exceeds the Discord total limit")
    return DiscordPayloadUsage(
        content_characters=len(content),
        embed_characters=embed_characters,
        embed_count=len(embeds),
        file_count=file_count,
    )


def _validate_embed_elements(embed: DiscordEmbedText) -> None:
    if len(embed.title) > EMBED_TITLE_LIMIT:
        raise DiscordPayloadBudgetError("embed title exceeds the Discord limit")
    if len(embed.description) > EMBED_DESCRIPTION_LIMIT:
        raise DiscordPayloadBudgetError("embed description exceeds the Discord limit")
    if len(embed.fields) > EMBED_FIELD_COUNT_LIMIT:
        raise DiscordPayloadBudgetError("embed field count exceeds the Discord limit")
    if len(embed.footer) > EMBED_FOOTER_LIMIT:
        raise DiscordPayloadBudgetError("embed footer exceeds the Discord limit")
    if len(embed.author) > EMBED_AUTHOR_LIMIT:
        raise DiscordPayloadBudgetError("embed author exceeds the Discord limit")
    for field in embed.fields:
        if len(field.name) > EMBED_FIELD_NAME_LIMIT:
            raise DiscordPayloadBudgetError("embed field name exceeds the Discord limit")
        if len(field.value) > EMBED_FIELD_VALUE_LIMIT:
            raise DiscordPayloadBudgetError("embed field value exceeds the Discord limit")


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
