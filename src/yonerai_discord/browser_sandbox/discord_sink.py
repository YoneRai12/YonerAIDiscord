from __future__ import annotations

from io import BytesIO
from typing import Any

import discord

from .models import BrowserOutput, BrowserOutputKind


MAX_DISCORD_BROWSER_SCREENSHOT_BYTES = 8 * 1024 * 1024
MAX_DISCORD_BROWSER_TEXT_BYTES = 8 * 1024 * 1024
MAX_DISCORD_BROWSER_TEXT_CHARACTERS = 1_900
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_START = b"\xff\xd8\xff"
_JPEG_END = b"\xff\xd9"
_TEXT_MEDIA_TYPE = "text/plain; charset=utf-8"
_TEXT_FILENAME = "browser-extracted-text.txt"
_INVALID_OUTPUT_MESSAGE = "browser output is invalid or exceeds the delivery limit"
_SOURCE_REPLY_MESSAGE = "source Discord message reply is unavailable"
_DELIVERY_FAILED_MESSAGE = "Discord browser output delivery failed"
_FILENAME_BY_MEDIA_TYPE = {
    "image/png": "browser-screenshot.png",
    "image/jpeg": "browser-screenshot.jpg",
}


class BrowserOutputDeliveryError(RuntimeError):
    """BrowserOutputをDiscordへ安全に配送できなかった。"""


class BrowserScreenshotDeliveryError(BrowserOutputDeliveryError):
    """検証済みscreenshotを元Discord messageへ安全に配送できなかった。"""


class DiscordBrowserScreenshotSink:
    """検証済みBrowserOutputを元Discord messageへのreplyとして配送する。"""

    async def send_output(self, message: Any, output: BrowserOutput) -> None:
        if type(output) is not BrowserOutput:
            raise BrowserOutputDeliveryError(_INVALID_OUTPUT_MESSAGE)
        try:
            kind = output.kind
        except AttributeError as exc:
            raise BrowserOutputDeliveryError(_INVALID_OUTPUT_MESSAGE) from exc
        if kind is BrowserOutputKind.SCREENSHOT:
            try:
                await self.send_screenshot(message, output)
            except BrowserOutputDeliveryError:
                raise
            except Exception as exc:
                raise BrowserOutputDeliveryError(_INVALID_OUTPUT_MESSAGE) from exc
            return
        if kind is not BrowserOutputKind.TEXT:
            raise BrowserOutputDeliveryError(_INVALID_OUTPUT_MESSAGE)

        try:
            text = _validate_text(output)
        except (AttributeError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise BrowserOutputDeliveryError(_INVALID_OUTPUT_MESSAGE) from exc
        reply = _require_output_reply(message)
        if len(text) <= MAX_DISCORD_BROWSER_TEXT_CHARACTERS:
            try:
                await reply(
                    content=text,
                    suppress_embeds=True,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as exc:
                raise BrowserOutputDeliveryError(_DELIVERY_FAILED_MESSAGE) from exc
            return

        buffer = BytesIO(output.data)
        file: discord.File | None = None
        try:
            file = discord.File(buffer, filename=_TEXT_FILENAME)
            await reply(
                file=file,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            raise BrowserOutputDeliveryError(_DELIVERY_FAILED_MESSAGE) from exc
        finally:
            if file is not None:
                file.close()
            buffer.close()

    async def send_screenshot(self, message: Any, output: BrowserOutput) -> None:
        message_id = getattr(message, "id", None)
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise BrowserScreenshotDeliveryError("source Discord message is unavailable")
        reply = getattr(message, "reply", None)
        if not callable(reply):
            raise BrowserScreenshotDeliveryError("source Discord message reply is unavailable")

        filename = _validate_screenshot(output)
        buffer = BytesIO(output.data)
        file: discord.File | None = None
        try:
            file = discord.File(buffer, filename=filename)
            await reply(
                file=file,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            raise BrowserScreenshotDeliveryError("Discord screenshot delivery failed") from exc
        finally:
            if file is not None:
                file.close()
            buffer.close()


def _validate_screenshot(output: BrowserOutput) -> str:
    if type(output) is not BrowserOutput:
        raise TypeError("output must be an exact BrowserOutput")
    if output.kind is not BrowserOutputKind.SCREENSHOT:
        raise ValueError("output must be a screenshot")
    if type(output.data) is not bytes or not output.data:
        raise ValueError("screenshot data must be non-empty bytes")
    if len(output.data) > MAX_DISCORD_BROWSER_SCREENSHOT_BYTES:
        raise ValueError("screenshot exceeds the Discord delivery byte limit")
    if type(output.media_type) is not str:
        raise TypeError("screenshot media type must be an exact string")

    filename = _FILENAME_BY_MEDIA_TYPE.get(output.media_type)
    if filename is None:
        raise ValueError("screenshot media type is not allowed")
    if output.media_type == "image/png" and not output.data.startswith(_PNG_SIGNATURE):
        raise ValueError("PNG screenshot signature is invalid")
    if output.media_type == "image/jpeg" and not (
        output.data.startswith(_JPEG_START) and output.data.endswith(_JPEG_END)
    ):
        raise ValueError("JPEG screenshot signature is invalid")
    return filename


def _validate_text(output: BrowserOutput) -> str:
    if type(output) is not BrowserOutput:
        raise TypeError("output must be an exact BrowserOutput")
    if output.kind is not BrowserOutputKind.TEXT:
        raise ValueError("output must be text")
    if type(output.data) is not bytes or not output.data:
        raise ValueError("text data must be non-empty bytes")
    if len(output.data) > MAX_DISCORD_BROWSER_TEXT_BYTES:
        raise ValueError("text exceeds the Discord delivery byte limit")
    if type(output.media_type) is not str or output.media_type != _TEXT_MEDIA_TYPE:
        raise ValueError("text media type is not allowed")
    return output.data.decode("utf-8", errors="strict")


def _require_output_reply(message: Any) -> Any:
    message_id = getattr(message, "id", None)
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise BrowserOutputDeliveryError(_SOURCE_REPLY_MESSAGE)
    reply = getattr(message, "reply", None)
    if not callable(reply):
        raise BrowserOutputDeliveryError(_SOURCE_REPLY_MESSAGE)
    return reply


DiscordBrowserOutputSink = DiscordBrowserScreenshotSink


__all__ = [
    "MAX_DISCORD_BROWSER_SCREENSHOT_BYTES",
    "MAX_DISCORD_BROWSER_TEXT_BYTES",
    "MAX_DISCORD_BROWSER_TEXT_CHARACTERS",
    "BrowserOutputDeliveryError",
    "BrowserScreenshotDeliveryError",
    "DiscordBrowserOutputSink",
    "DiscordBrowserScreenshotSink",
]
