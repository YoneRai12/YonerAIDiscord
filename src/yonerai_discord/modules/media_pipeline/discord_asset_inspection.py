"""Discord asset metadata と明示 preview を純粋に検証する。

Discord API、network、artifact store、送信はこの module の責務ではない。caller は
現在の guild に束縛済みの sticker facts と、明示 preview 要求時だけ bytes を渡す。
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from PIL import Image, UnidentifiedImageError

from .artifacts import canonicalize_image
from .domain import MAX_PNG_BYTES, MediaValidationError, validate_image_dimensions


MAX_DISCORD_IDENTIFIER = (1 << 64) - 1
MAX_ASSET_NAME_CHARACTERS = 100
MAX_ASSET_NAME_BYTES = 400
_CUSTOM_EMOJI = re.compile(r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):(?P<asset_id>[0-9]{17,20})>\Z")
_ASSET_IDENTIFIER = re.compile(r"[0-9]{17,20}\Z")
_UNSAFE_NAME_FRAGMENTS = ("/", "\\", "://", "..", "*", "?", "[", "]", "{", "}", "|", ";")


class DiscordAssetInspectionError(ValueError):
    """Discord asset metadata または preview が固定契約を満たさない。"""


class StickerFormat(StrEnum):
    PNG = "png"
    APNG = "apng"
    GIF = "gif"
    LOTTIE = "lottie"


class DiscordAssetKind(StrEnum):
    CUSTOM_EMOJI = "custom_emoji"
    STICKER = "sticker"


@dataclass(frozen=True, slots=True, repr=False)
class EmojiAssetInspectionRequest:
    """custom emoji mention と caller が確認済みの guild binding。"""

    guild_id: int
    mention: str = field(repr=False)
    preview_requested: bool = False

    def __post_init__(self) -> None:
        _validate_discord_identifier(self.guild_id, "guild_id")
        if not isinstance(self.mention, str):
            raise DiscordAssetInspectionError("custom emoji mention is invalid")
        match = _CUSTOM_EMOJI.fullmatch(self.mention)
        if match is None:
            raise DiscordAssetInspectionError("custom emoji mention is invalid")
        _validate_identifier_text(match.group("asset_id"))
        if not isinstance(self.preview_requested, bool):
            raise DiscordAssetInspectionError("preview_requested must be boolean")

    def __repr__(self) -> str:
        return "EmojiAssetInspectionRequest(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class StickerAssetFacts:
    """Discord caller が取得済みかつ guild に照合済みの sticker metadata。"""

    guild_id: int
    asset_id: int
    name: str = field(repr=False)
    format: StickerFormat

    def __post_init__(self) -> None:
        _validate_discord_identifier(self.guild_id, "guild_id")
        _validate_discord_identifier(self.asset_id, "asset_id")
        _validate_asset_name(self.name)
        if not isinstance(self.format, StickerFormat):
            raise DiscordAssetInspectionError("sticker format is invalid")

    def __repr__(self) -> str:
        return "StickerAssetFacts(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class StickerAssetInspectionRequest:
    facts: StickerAssetFacts = field(repr=False)
    preview_requested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.facts, StickerAssetFacts):
            raise DiscordAssetInspectionError("sticker facts are required")
        if not isinstance(self.preview_requested, bool):
            raise DiscordAssetInspectionError("preview_requested must be boolean")

    def __repr__(self) -> str:
        return "StickerAssetInspectionRequest(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class AssetPreviewPayload:
    """network fetch を行わない、caller supplied の preview bytes。"""

    media_type: str
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, str) or self.media_type not in {"image/png", "image/apng", "image/gif"}:
            raise DiscordAssetInspectionError("preview media type is not supported")
        if not isinstance(self.data, bytes) or not 1 <= len(self.data) <= MAX_PNG_BYTES:
            raise DiscordAssetInspectionError("preview bytes are outside the allowed limit")

    def __repr__(self) -> str:
        return "AssetPreviewPayload(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalAssetPreview:
    """明示 preview の first frame だけを metadata-free PNG に正規化する。"""

    png: bytes = field(repr=False)
    width: int
    height: int

    def __post_init__(self) -> None:
        if not isinstance(self.png, bytes) or not 1 <= len(self.png) <= MAX_PNG_BYTES:
            raise DiscordAssetInspectionError("canonical preview is invalid")
        try:
            validate_image_dimensions(self.width, self.height)
        except MediaValidationError as exc:
            raise DiscordAssetInspectionError("canonical preview dimensions are invalid") from exc

    def __repr__(self) -> str:
        return f"CanonicalAssetPreview(width={self.width}, height={self.height})"


@dataclass(frozen=True, slots=True, repr=False)
class DiscordAssetInspectionResult:
    """mention-safe な公開 metadata。bytes、digest、scope は公開しない。"""

    kind: DiscordAssetKind
    asset_id: str
    display_name: str
    animated: bool | None
    format: StickerFormat | None
    preview: CanonicalAssetPreview | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DiscordAssetKind):
            raise DiscordAssetInspectionError("asset kind is invalid")
        if not isinstance(self.asset_id, str) or _ASSET_IDENTIFIER.fullmatch(self.asset_id) is None:
            raise DiscordAssetInspectionError("asset identifier is invalid")
        _validate_asset_name(self.display_name.replace("@\u200b", "@"))
        if self.kind is DiscordAssetKind.CUSTOM_EMOJI:
            if not isinstance(self.animated, bool) or self.format is not None:
                raise DiscordAssetInspectionError("custom emoji metadata is invalid")
        elif self.animated is not None or not isinstance(self.format, StickerFormat):
            raise DiscordAssetInspectionError("sticker metadata is invalid")
        if self.preview is not None and not isinstance(self.preview, CanonicalAssetPreview):
            raise DiscordAssetInspectionError("preview is invalid")

    def __repr__(self) -> str:
        return f"DiscordAssetInspectionResult(kind={self.kind.value!r}, preview={self.preview is not None})"


def inspect_custom_emoji(
    request: EmojiAssetInspectionRequest,
    *,
    preview: AssetPreviewPayload | None = None,
) -> DiscordAssetInspectionResult:
    """strict mention を metadata と明示 first-frame preview へ投影する。"""

    if not isinstance(request, EmojiAssetInspectionRequest):
        raise DiscordAssetInspectionError("emoji request is required")
    match = _CUSTOM_EMOJI.fullmatch(request.mention)
    if match is None:
        raise DiscordAssetInspectionError("custom emoji mention is invalid")
    asset_id = match.group("asset_id")
    _validate_identifier_text(asset_id)
    canonical = _require_requested_preview(request.preview_requested, preview)
    return DiscordAssetInspectionResult(
        kind=DiscordAssetKind.CUSTOM_EMOJI,
        asset_id=asset_id,
        display_name=_mention_safe_name(match.group("name")),
        animated=match.group("animated") == "a",
        format=None,
        preview=canonical,
    )


def inspect_sticker(
    request: StickerAssetInspectionRequest,
    *,
    preview: AssetPreviewPayload | None = None,
) -> DiscordAssetInspectionResult:
    """caller supplied sticker facts を検証し、Lottie preview は拒否する。"""

    if not isinstance(request, StickerAssetInspectionRequest):
        raise DiscordAssetInspectionError("sticker request is required")
    facts = request.facts
    if facts.format is StickerFormat.LOTTIE:
        if preview is not None or request.preview_requested:
            raise DiscordAssetInspectionError("Lottie sticker previews are not supported")
        canonical = None
    else:
        canonical = _require_requested_preview(request.preview_requested, preview)
    return DiscordAssetInspectionResult(
        kind=DiscordAssetKind.STICKER,
        asset_id=str(facts.asset_id),
        display_name=_mention_safe_name(facts.name),
        animated=None,
        format=facts.format,
        preview=canonical,
    )


def _require_requested_preview(
    requested: bool,
    preview: AssetPreviewPayload | None,
) -> CanonicalAssetPreview | None:
    if requested:
        if not isinstance(preview, AssetPreviewPayload):
            raise DiscordAssetInspectionError("explicit preview bytes are required")
        return _canonical_preview(preview)
    if preview is not None:
        raise DiscordAssetInspectionError("preview bytes require an explicit request")
    return None


def _canonical_preview(preview: AssetPreviewPayload) -> CanonicalAssetPreview:
    try:
        with Image.open(io.BytesIO(preview.data)) as image:
            if not _mime_matches_image(preview.media_type, image.format):
                raise DiscordAssetInspectionError("preview media type does not match image data")
            image.seek(0)
            image.load()
            validate_image_dimensions(*image.size)
            canonical = canonicalize_image(image)
    except DiscordAssetInspectionError:
        raise
    except (MediaValidationError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise DiscordAssetInspectionError("preview image cannot be decoded") from exc
    return CanonicalAssetPreview(png=canonical.data, width=canonical.width, height=canonical.height)


def _mime_matches_image(media_type: str, image_format: str | None) -> bool:
    return (
        (media_type == "image/png" and image_format == "PNG")
        or (media_type == "image/apng" and image_format == "PNG")
        or (media_type == "image/gif" and image_format == "GIF")
    )


def _validate_discord_identifier(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DISCORD_IDENTIFIER:
        raise DiscordAssetInspectionError(f"{name} is invalid")


def _validate_identifier_text(value: str) -> None:
    if _ASSET_IDENTIFIER.fullmatch(value) is None:
        raise DiscordAssetInspectionError("asset identifier is invalid")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise DiscordAssetInspectionError("asset identifier is invalid") from exc
    _validate_discord_identifier(parsed, "asset_id")


def _validate_asset_name(value: str) -> None:
    if not isinstance(value, str):
        raise DiscordAssetInspectionError("asset name is invalid")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value or not value.strip() or any(unicodedata.category(char).startswith("C") for char in value):
        raise DiscordAssetInspectionError("asset name is invalid")
    if len(value) > MAX_ASSET_NAME_CHARACTERS or len(value.encode("utf-8")) > MAX_ASSET_NAME_BYTES:
        raise DiscordAssetInspectionError("asset name is outside the allowed limit")
    if any(fragment in value for fragment in _UNSAFE_NAME_FRAGMENTS):
        raise DiscordAssetInspectionError("asset name contains unsafe syntax")


def _mention_safe_name(value: str) -> str:
    _validate_asset_name(value)
    return value.replace("@", "@\u200b")


__all__ = [
    "AssetPreviewPayload",
    "CanonicalAssetPreview",
    "DiscordAssetInspectionError",
    "DiscordAssetInspectionResult",
    "DiscordAssetKind",
    "EmojiAssetInspectionRequest",
    "MAX_ASSET_NAME_BYTES",
    "MAX_ASSET_NAME_CHARACTERS",
    "StickerAssetFacts",
    "StickerAssetInspectionRequest",
    "StickerFormat",
    "inspect_custom_emoji",
    "inspect_sticker",
]
