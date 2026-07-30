from __future__ import annotations

import io

import pytest
from PIL import Image, PngImagePlugin

from yonerai_discord.modules.media_pipeline.artifacts import validate_canonical_png
from yonerai_discord.modules.media_pipeline.discord_asset_inspection import (
    AssetPreviewPayload,
    DiscordAssetInspectionError,
    EmojiAssetInspectionRequest,
    StickerAssetFacts,
    StickerAssetInspectionRequest,
    StickerFormat,
    inspect_custom_emoji,
    inspect_sticker,
)
from yonerai_discord.modules.media_pipeline.domain import MAX_PNG_BYTES, MAX_IMAGE_DIMENSION


GUILD_ID = 123456789012345678
ASSET_ID = 123456789012345679


def _image_bytes(*, image_format: str = "PNG", metadata: bool = False) -> bytes:
    image = Image.new("RGBA", (12, 8), (30, 120, 220, 255))
    try:
        buffer = io.BytesIO()
        if image_format == "PNG" and metadata:
            pnginfo = PngImagePlugin.PngInfo()
            pnginfo.add_text("secret", "ASSET_SECRET")
            image.save(buffer, format=image_format, pnginfo=pnginfo)
        else:
            image.save(buffer, format=image_format)
        return buffer.getvalue()
    finally:
        image.close()


def test_custom_emoji_metadata_is_strict_and_mention_safe() -> None:
    result = inspect_custom_emoji(
        EmojiAssetInspectionRequest(guild_id=GUILD_ID, mention=f"<a:party_{ASSET_ID}:{ASSET_ID}>")
    )

    assert result.asset_id == str(ASSET_ID)
    assert result.display_name == f"party_{ASSET_ID}"
    assert result.animated is True
    assert result.format is None
    assert result.preview is None
    assert str(ASSET_ID) not in repr(result)


@pytest.mark.parametrize(
    "mention",
    (
        "<:a:123>",
        "<a:name:123456789012345678901>",
        "<:bad/name:123456789012345678>",
        "https://cdn.discordapp.com/emojis/123456789012345678.png",
        "<:ok:18446744073709551616>",
    ),
)
def test_custom_emoji_parser_rejects_non_contract_input(mention: str) -> None:
    with pytest.raises(DiscordAssetInspectionError):
        EmojiAssetInspectionRequest(guild_id=GUILD_ID, mention=mention)


def test_sticker_metadata_requires_caller_guild_facts_and_suppresses_mention() -> None:
    result = inspect_sticker(
        StickerAssetInspectionRequest(
            StickerAssetFacts(guild_id=GUILD_ID, asset_id=ASSET_ID, name="@everyone sticker", format=StickerFormat.APNG)
        )
    )

    assert result.asset_id == str(ASSET_ID)
    assert result.display_name == "@\u200beveryone sticker"
    assert result.format is StickerFormat.APNG
    assert result.animated is None
    assert "everyone" not in repr(result)


def test_preview_is_opt_in_and_canonicalizes_first_frame_without_metadata() -> None:
    request = EmojiAssetInspectionRequest(guild_id=GUILD_ID, mention=f"<:ok:{ASSET_ID}>", preview_requested=True)
    result = inspect_custom_emoji(
        request,
        preview=AssetPreviewPayload(media_type="image/png", data=_image_bytes(metadata=True)),
    )

    assert result.preview is not None
    canonical = validate_canonical_png(result.preview.png)
    assert (canonical.width, canonical.height) == (12, 8)
    assert b"ASSET_SECRET" not in result.preview.png
    assert "png=" not in repr(result.preview)


def test_gif_preview_uses_first_frame_and_sticker_lottie_rejects_preview() -> None:
    emoji = inspect_custom_emoji(
        EmojiAssetInspectionRequest(guild_id=GUILD_ID, mention=f"<a:ok:{ASSET_ID}>", preview_requested=True),
        preview=AssetPreviewPayload(media_type="image/gif", data=_image_bytes(image_format="GIF")),
    )
    assert emoji.preview is not None
    validate_canonical_png(emoji.preview.png)

    request = StickerAssetInspectionRequest(
        StickerAssetFacts(guild_id=GUILD_ID, asset_id=ASSET_ID, name="sticker", format=StickerFormat.LOTTIE),
        preview_requested=True,
    )
    with pytest.raises(DiscordAssetInspectionError, match="Lottie"):
        inspect_sticker(request, preview=AssetPreviewPayload(media_type="image/png", data=_image_bytes()))


def test_preview_fails_closed_for_implicit_oversize_mime_mismatch_and_broken_data() -> None:
    request = EmojiAssetInspectionRequest(guild_id=GUILD_ID, mention=f"<:ok:{ASSET_ID}>")
    valid = AssetPreviewPayload(media_type="image/png", data=_image_bytes())
    with pytest.raises(DiscordAssetInspectionError, match="explicit"):
        inspect_custom_emoji(request, preview=valid)
    with pytest.raises(DiscordAssetInspectionError, match="allowed limit"):
        AssetPreviewPayload(media_type="image/png", data=b"x" * (MAX_PNG_BYTES + 1))
    with pytest.raises(DiscordAssetInspectionError, match="supported"):
        AssetPreviewPayload(media_type="application/json", data=b"{}")
    with pytest.raises(DiscordAssetInspectionError, match="match"):
        inspect_custom_emoji(
            EmojiAssetInspectionRequest(guild_id=GUILD_ID, mention=f"<:ok:{ASSET_ID}>", preview_requested=True),
            preview=AssetPreviewPayload(media_type="image/gif", data=_image_bytes()),
        )
    with pytest.raises(DiscordAssetInspectionError, match="decoded"):
        inspect_custom_emoji(
            EmojiAssetInspectionRequest(guild_id=GUILD_ID, mention=f"<:ok:{ASSET_ID}>", preview_requested=True),
            preview=AssetPreviewPayload(media_type="image/png", data=b"not-a-png"),
        )


def test_name_and_image_bounds_fail_closed_without_paths_or_controls() -> None:
    with pytest.raises(DiscordAssetInspectionError):
        StickerAssetFacts(guild_id=GUILD_ID, asset_id=ASSET_ID, name="../secret", format=StickerFormat.PNG)
    with pytest.raises(DiscordAssetInspectionError):
        StickerAssetFacts(guild_id=GUILD_ID, asset_id=ASSET_ID, name="bad\x00name", format=StickerFormat.PNG)

    image = Image.new("RGB", (MAX_IMAGE_DIMENSION + 1, 1), (0, 0, 0))
    try:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        oversized_dimensions = buffer.getvalue()
    finally:
        image.close()
    with pytest.raises(DiscordAssetInspectionError, match="decoded"):
        inspect_custom_emoji(
            EmojiAssetInspectionRequest(guild_id=GUILD_ID, mention=f"<:ok:{ASSET_ID}>", preview_requested=True),
            preview=AssetPreviewPayload(media_type="image/png", data=oversized_dimensions),
        )
