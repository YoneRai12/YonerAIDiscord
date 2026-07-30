"""Discord 非依存のローカル引用カード生成。

本文やフォントを path へ解決せず、caller が渡した bounded bytes だけを使う。
表示文字列は互換分解を行わない NFC とし、改行以外の Unicode control を除く。
"""

from __future__ import annotations

import hashlib
import io
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from yonerai_discord.modules.image_generation.artifacts import (
    ImageArtifactValidationError,
    canonicalize_png,
)

from .artifacts import canonicalize_image
from .domain import ArtifactRef, MediaValidationError


QUOTE_CARD_WIDTH = 1_200
QUOTE_CARD_HEIGHT = 675
QUOTE_CARD_REVISION = "1"
MAX_DISPLAY_NAME_CHARACTERS = 100
MAX_DISPLAY_NAME_BYTES = 400
MAX_BODY_CHARACTERS = 2_000
MAX_BODY_BYTES = 8_000
MAX_SOURCE_LINES = 32
MAX_RENDERED_BODY_LINES = 10
MAX_AVATAR_PNG_BYTES = 1024 * 1024
MAX_FONT_BYTES = 16 * 1024 * 1024
_AVATAR_SIZE = 104
_BODY_TEXT_WIDTH = 976


class QuoteRenderError(ValueError):
    """引用カード入力または描画契約が成立しない。"""


@dataclass(frozen=True, slots=True, repr=False)
class QuoteCardRequest:
    """秘密を repr に含めない、正規化前の引用カード入力。"""

    display_name: str = field(repr=False)
    body: str = field(repr=False)
    timestamp: datetime
    avatar_png: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.display_name, str):
            raise QuoteRenderError("display name must be text")
        if not isinstance(self.body, str):
            raise QuoteRenderError("quote body must be text")
        if not isinstance(self.timestamp, datetime) or self.timestamp.utcoffset() is None:
            raise QuoteRenderError("timestamp must be timezone-aware")
        if self.avatar_png is not None and not isinstance(self.avatar_png, bytes):
            raise QuoteRenderError("avatar PNG must be bytes")

    def __repr__(self) -> str:
        return "QuoteCardRequest(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class RenderedQuoteCard:
    """配送前の canonical PNG。本文やcontent digestを repr へ出さない。"""

    png: bytes = field(repr=False)
    width: int
    height: int
    sha256: str = field(repr=False)
    rendered_line_count: int
    was_truncated: bool
    used_avatar_placeholder: bool

    def __repr__(self) -> str:
        return (
            "RenderedQuoteCard("
            f"width={self.width}, height={self.height}, "
            f"rendered_line_count={self.rendered_line_count}, "
            f"was_truncated={self.was_truncated}, "
            f"used_avatar_placeholder={self.used_avatar_placeholder})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class QuoteCardArtifactResult:
    """保存済み引用カードのopaque参照だけを返すservice結果。"""

    artifact: ArtifactRef = field(repr=False)
    operation_revision: str = QUOTE_CARD_REVISION

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ArtifactRef):
            raise MediaValidationError("quote card result must contain an ArtifactRef")
        if self.operation_revision != QUOTE_CARD_REVISION:
            raise MediaValidationError("quote card revision is not supported")

    def __repr__(self) -> str:
        return f"QuoteCardArtifactResult(operation_revision={self.operation_revision!r})"


class QuoteCardRenderer:
    """YonerAI code-owned layoutで引用カードを生成する。"""

    __slots__ = ("_font_bytes",)

    def __init__(self, font_bytes: bytes) -> None:
        if not isinstance(font_bytes, bytes):
            raise QuoteRenderError("font must be provided as bytes")
        if not 1 <= len(font_bytes) <= MAX_FONT_BYTES:
            raise QuoteRenderError("font bytes are outside the allowed limit")
        self._font_bytes = font_bytes

    def __repr__(self) -> str:
        return "QuoteCardRenderer(font=<redacted>)"

    def render(self, request: QuoteCardRequest) -> RenderedQuoteCard:
        if not isinstance(request, QuoteCardRequest):
            raise QuoteRenderError("request must be a QuoteCardRequest")

        display_name = _normalize_display_name(request.display_name)
        body = _normalize_body(request.body)
        if request.avatar_png is not None and len(request.avatar_png) > MAX_AVATAR_PNG_BYTES:
            raise QuoteRenderError("avatar PNG exceeds the byte limit")

        fonts = self._load_fonts()
        _require_glyph_coverage(fonts.display_name, display_name)
        _require_glyph_coverage(fonts.body, body)
        lines, truncated = _wrap_body(
            body,
            font=fonts.body,
            width=_BODY_TEXT_WIDTH,
            max_lines=MAX_RENDERED_BODY_LINES,
        )

        canvas = Image.new("RGB", (QUOTE_CARD_WIDTH, QUOTE_CARD_HEIGHT), (15, 20, 35))
        avatar: Image.Image | None = None
        try:
            draw = ImageDraw.Draw(canvas)
            _paint_background(draw)
            used_placeholder = True
            avatar = _decode_avatar(request.avatar_png)
            if avatar is None:
                _paint_avatar_placeholder(draw)
            else:
                used_placeholder = False
                _paste_round_avatar(canvas, avatar)

            draw.text((72, 48), "YonerAI QUOTE", font=fonts.label, fill=(124, 156, 255))
            draw.text((205, 105), display_name, font=fonts.display_name, fill=(245, 247, 255))
            timestamp_text = request.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            draw.text((207, 164), timestamp_text, font=fonts.timestamp, fill=(164, 174, 202))
            draw.rounded_rectangle((72, 230, 1128, 598), radius=24, fill=(23, 31, 52))
            draw.rounded_rectangle((92, 258, 100, 568), radius=4, fill=(103, 132, 255))

            y = 267
            for line in lines:
                draw.text((124, y), line, font=fonts.body, fill=(237, 241, 255))
                y += 31
            draw.text((72, 627), "LOCAL QUOTE CARD", font=fonts.footer, fill=(112, 122, 151))

            canonical = canonicalize_image(canvas)
        except QuoteRenderError:
            raise
        except (OSError, ValueError) as exc:
            raise QuoteRenderError("quote card rendering failed") from exc
        finally:
            if avatar is not None:
                avatar.close()
            canvas.close()

        return RenderedQuoteCard(
            png=canonical.data,
            width=canonical.width,
            height=canonical.height,
            sha256=hashlib.sha256(canonical.data).hexdigest(),
            rendered_line_count=len(lines),
            was_truncated=truncated,
            used_avatar_placeholder=used_placeholder,
        )

    def _load_fonts(self) -> _QuoteFonts:
        try:
            return _QuoteFonts(
                label=_load_font(self._font_bytes, 22),
                display_name=_load_font(self._font_bytes, 39),
                timestamp=_load_font(self._font_bytes, 21),
                body=_load_font(self._font_bytes, 29),
                footer=_load_font(self._font_bytes, 17),
            )
        except (OSError, ValueError) as exc:
            raise QuoteRenderError("font bytes cannot be loaded") from exc


@dataclass(frozen=True, slots=True)
class _QuoteFonts:
    label: ImageFont.FreeTypeFont
    display_name: ImageFont.FreeTypeFont
    timestamp: ImageFont.FreeTypeFont
    body: ImageFont.FreeTypeFont
    footer: ImageFont.FreeTypeFont


def _load_font(data: bytes, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(io.BytesIO(data), size=size)


def _normalize_display_name(value: str) -> str:
    normalized = _strip_controls(unicodedata.normalize("NFC", value), preserve_newline=False).strip()
    if not normalized:
        raise QuoteRenderError("display name is empty after normalization")
    if len(normalized) > MAX_DISPLAY_NAME_CHARACTERS or len(normalized.encode("utf-8")) > MAX_DISPLAY_NAME_BYTES:
        raise QuoteRenderError("display name exceeds the bounded text limit")
    return normalized


def _normalize_body(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = _strip_controls(normalized, preserve_newline=True).strip()
    if not normalized:
        raise QuoteRenderError("quote body is empty after normalization")
    if len(normalized) > MAX_BODY_CHARACTERS or len(normalized.encode("utf-8")) > MAX_BODY_BYTES:
        raise QuoteRenderError("quote body exceeds the bounded text limit")
    if normalized.count("\n") + 1 > MAX_SOURCE_LINES:
        raise QuoteRenderError("quote body exceeds the source line limit")
    return normalized


def _strip_controls(value: str, *, preserve_newline: bool) -> str:
    return "".join(
        character
        for character in value
        if (preserve_newline and character == "\n") or not unicodedata.category(character).startswith("C")
    )


def _require_glyph_coverage(font: ImageFont.FreeTypeFont, value: str) -> None:
    missing = _mask_signature(font, "\u0378")
    for character in set(value):
        if character.isspace():
            continue
        if _mask_signature(font, character) == missing:
            raise QuoteRenderError("font does not cover all quote text glyphs")


def _mask_signature(font: ImageFont.FreeTypeFont, character: str) -> tuple[tuple[int, int], bytes]:
    mask = font.getmask(character)
    return mask.size, bytes(mask)


def _wrap_body(
    body: str,
    *,
    font: ImageFont.FreeTypeFont,
    width: int,
    max_lines: int,
) -> tuple[tuple[str, ...], bool]:
    lines: list[str] = []
    truncated = False
    source_lines = body.split("\n")
    for source_index, source_line in enumerate(source_lines):
        remaining = source_line or " "
        while remaining:
            if len(lines) == max_lines:
                truncated = True
                break
            take = _fitting_prefix_length(remaining, font=font, width=width)
            lines.append(remaining[:take].rstrip() or " ")
            remaining = remaining[take:].lstrip()
        if truncated:
            break
        if source_index < len(source_lines) - 1 and not remaining and len(lines) == max_lines:
            truncated = True
            break
    if not lines:
        lines.append(" ")
    if truncated:
        lines[-1] = _with_ellipsis(lines[-1], font=font, width=width)
    return tuple(lines), truncated


def _fitting_prefix_length(value: str, *, font: ImageFont.FreeTypeFont, width: int) -> int:
    low = 1
    high = len(value)
    best = 1
    while low <= high:
        middle = (low + high) // 2
        if font.getlength(value[:middle]) <= width:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _with_ellipsis(value: str, *, font: ImageFont.FreeTypeFont, width: int) -> str:
    ellipsis = "…"
    candidate = value.rstrip()
    while candidate and font.getlength(candidate + ellipsis) > width:
        candidate = candidate[:-1].rstrip()
    return f"{candidate}{ellipsis}" if candidate else ellipsis


def _decode_avatar(data: bytes | None) -> Image.Image | None:
    if data is None:
        return None
    try:
        canonical = canonicalize_png(data)
        with Image.open(io.BytesIO(canonical.data)) as source:
            source.load()
            return source.convert("RGB")
    except (ImageArtifactValidationError, UnidentifiedImageError, OSError, ValueError):
        return None


def _paint_background(draw: ImageDraw.ImageDraw) -> None:
    draw.rounded_rectangle((26, 26, 1174, 649), radius=36, fill=(18, 24, 41), outline=(55, 70, 114), width=2)
    draw.ellipse((965, -160, 1290, 165), fill=(35, 52, 100))
    draw.ellipse((-135, 515, 135, 785), fill=(25, 52, 78))


def _paint_avatar_placeholder(draw: ImageDraw.ImageDraw) -> None:
    bounds = (72, 96, 72 + _AVATAR_SIZE, 96 + _AVATAR_SIZE)
    draw.ellipse(bounds, fill=(50, 63, 97), outline=(103, 132, 255), width=3)
    draw.ellipse((107, 119, 141, 153), fill=(130, 151, 222))
    draw.rounded_rectangle((91, 155, 157, 186), radius=15, fill=(130, 151, 222))


def _paste_round_avatar(canvas: Image.Image, avatar: Image.Image) -> None:
    resized = avatar.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.Resampling.LANCZOS)
    mask = Image.new("L", (_AVATAR_SIZE, _AVATAR_SIZE), 0)
    try:
        ImageDraw.Draw(mask).ellipse((0, 0, _AVATAR_SIZE - 1, _AVATAR_SIZE - 1), fill=255)
        canvas.paste(resized, (72, 96), mask)
    finally:
        resized.close()
        mask.close()


__all__ = [
    "MAX_AVATAR_PNG_BYTES",
    "MAX_BODY_BYTES",
    "MAX_BODY_CHARACTERS",
    "MAX_DISPLAY_NAME_BYTES",
    "MAX_DISPLAY_NAME_CHARACTERS",
    "MAX_FONT_BYTES",
    "MAX_RENDERED_BODY_LINES",
    "MAX_SOURCE_LINES",
    "QUOTE_CARD_HEIGHT",
    "QUOTE_CARD_REVISION",
    "QUOTE_CARD_WIDTH",
    "QuoteCardArtifactResult",
    "QuoteCardRenderer",
    "QuoteCardRequest",
    "QuoteRenderError",
    "RenderedQuoteCard",
]
