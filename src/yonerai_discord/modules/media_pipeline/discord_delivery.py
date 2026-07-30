"""Prepared media を bot-owned Discord message へ正確に一度だけ反映する sink。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import inspect
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import discord

from .delivery import PreparedMediaAttachment
from .domain import ArtifactKind
from .durable_delivery import (
    DurableMediaDeliveryError,
    MediaDeliveryRejectedError,
    MediaDeliverySinkReceipt,
    MediaDeliverySinkRequest,
    MediaDeliveryTargetLease,
    MediaDeliveryTransientError,
)


BotCurrent = Callable[[], object | None]
TargetResolver = Callable[[MediaDeliverySinkRequest], Awaitable[object]]
AuthorizationCurrent = Callable[[MediaDeliverySinkRequest], Awaitable[bool]]

_MAX_DISCORD_ID = (1 << 64) - 1


class DiscordMediaDeliveryCommitUncertainError(RuntimeError):
    """Discord edit の開始後に確定 receipt を得られなかった。"""


class DiscordExactMessageMediaDeliverySink:
    """1つの bot-owned message だけを置換する Discord transport 境界。"""

    __slots__ = (
        "_authorization_current",
        "_bot",
        "_bot_user_id",
        "_bot_current",
        "_lease_secret",
        "_target_resolver",
    )

    def __init__(
        self,
        *,
        bot: object,
        bot_current: BotCurrent,
        target_resolver: TargetResolver,
        authorization_current: AuthorizationCurrent,
    ) -> None:
        bot_user = getattr(bot, "user", None)
        bot_user_id = _object_id(bot_user)
        if bot_user_id is None or getattr(bot_user, "bot", None) is not True:
            raise TypeError("Discord media delivery bot identity is unavailable")
        if not all(callable(value) for value in (bot_current, target_resolver, authorization_current)):
            raise TypeError("Discord media delivery current checks are unavailable")
        self._bot = bot
        self._bot_user_id = bot_user_id
        self._bot_current = bot_current
        self._target_resolver = target_resolver
        self._authorization_current = authorization_current
        self._lease_secret = secrets.token_bytes(32)

    async def preflight(self, request: MediaDeliverySinkRequest) -> MediaDeliveryTargetLease:
        try:
            request = _validated_request(request)
            target = await self._resolve_current_target(request)
            token = self._lease_token(request, target)
        except (ConnectionError, TimeoutError):
            raise MediaDeliveryTransientError("Discord media delivery target is temporarily unavailable") from None
        except MediaDeliveryRejectedError:
            raise
        except Exception:
            raise MediaDeliveryRejectedError("Discord media delivery target is unavailable") from None
        return MediaDeliveryTargetLease(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            message_id=request.message_id,
            bot_owned=True,
            token=token,
        )

    async def edit(
        self,
        request: MediaDeliverySinkRequest,
        *,
        lease: MediaDeliveryTargetLease,
    ) -> MediaDeliverySinkReceipt:
        if not isinstance(lease, MediaDeliveryTargetLease):
            raise MediaDeliveryRejectedError("Discord media delivery commit is unavailable")
        try:
            request = _validated_request(request)
            target = await self._resolve_current_target(request)
            expected_token = self._lease_token(request, target)
            if (
                lease.guild_id != request.guild_id
                or lease.channel_id != request.channel_id
                or lease.message_id != request.message_id
                or lease.bot_owned is not True
                or not hmac.compare_digest(lease.token, expected_token)
            ):
                raise MediaDeliveryRejectedError("Discord media delivery lease is unavailable")
            attachments = self._validated_attachments(request)
        except MediaDeliveryRejectedError:
            raise
        except Exception:
            raise MediaDeliveryRejectedError("Discord media delivery commit is unavailable") from None

        buffers: list[io.BytesIO] = []
        files: list[discord.File] = []
        edit_started = False
        try:
            for attachment in attachments:
                buffer = io.BytesIO(attachment.data)
                buffers.append(buffer)
                files.append(discord.File(buffer, filename=attachment.filename, spoiler=False))

            if await self._authorization_current(request) is not True:
                raise MediaDeliveryRejectedError("Discord media delivery authorization changed")
            self._require_bot_current()
            edit = getattr(target, "edit", None)
            if not callable(edit):
                raise MediaDeliveryRejectedError("Discord media delivery target is unavailable")
            pending = edit(
                attachments=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if not inspect.isawaitable(pending):
                raise MediaDeliveryRejectedError("Discord media delivery target is unavailable")
            edit_started = True
            result = await pending
            if await self._authorization_current(request) is not True:
                raise DiscordMediaDeliveryCommitUncertainError("Discord media delivery outcome is uncertain")
            self._require_bot_current()
            attachment_ids = self._validate_result(result, request, attachments)
            return MediaDeliverySinkReceipt(
                guild_id=request.guild_id,
                channel_id=request.channel_id,
                message_id=request.message_id,
                delivery_digest=request.delivery_digest,
                attachment_ids=attachment_ids,
                received_at=datetime.now(UTC),
            )
        except asyncio.CancelledError:
            raise
        except DiscordMediaDeliveryCommitUncertainError:
            raise
        except Exception as exc:
            if edit_started:
                raise DiscordMediaDeliveryCommitUncertainError("Discord media delivery outcome is uncertain") from None
            if isinstance(exc, MediaDeliveryRejectedError):
                raise
            raise MediaDeliveryRejectedError("Discord media delivery commit is unavailable") from None
        finally:
            for file in files:
                file.close()
            for buffer in buffers:
                buffer.close()

    async def _resolve_current_target(self, request: MediaDeliverySinkRequest) -> object:
        self._require_bot_current()
        if await self._authorization_current(request) is not True:
            raise MediaDeliveryRejectedError("Discord media delivery authorization changed")
        target = await self._target_resolver(request)
        self._require_bot_current()
        if await self._authorization_current(request) is not True:
            raise MediaDeliveryRejectedError("Discord media delivery authorization changed")
        self._require_bot_current()
        if not _target_matches(target, request=request, bot_user_id=self._bot_user_id):
            raise MediaDeliveryRejectedError("Discord media delivery target is unavailable")
        return target

    def _require_bot_current(self) -> None:
        if self._bot_current() is not self._bot:
            raise MediaDeliveryRejectedError("Discord media delivery bot changed")
        bot_user = getattr(self._bot, "user", None)
        if _object_id(bot_user) != self._bot_user_id or getattr(bot_user, "bot", None) is not True:
            raise MediaDeliveryRejectedError("Discord media delivery bot changed")

    def _lease_token(self, request: MediaDeliverySinkRequest, target: object) -> str:
        digest = hmac.new(self._lease_secret, digestmod=hashlib.sha256)
        digest.update(_request_binding(request))
        digest.update(b"\0")
        digest.update(_target_snapshot(target))
        return digest.hexdigest()

    @staticmethod
    def _validated_attachments(
        request: MediaDeliverySinkRequest,
    ) -> tuple[PreparedMediaAttachment, ...]:
        validated: list[PreparedMediaAttachment] = []
        for index, value in enumerate(request.attachments, start=1):
            if type(value) is not PreparedMediaAttachment:
                raise MediaDeliveryRejectedError("Discord media delivery attachment is unavailable")
            suffix = ".md" if value.kind is ArtifactKind.DOCUMENT else ".png"
            if value.filename != f"media-{index:02d}{suffix}":
                raise MediaDeliveryRejectedError("Discord media delivery attachment is unavailable")
            try:
                validated.append(
                    PreparedMediaAttachment(
                        filename=value.filename,
                        data=value.data,
                        media_type=value.media_type,
                        kind=value.kind,
                        width=value.width,
                        height=value.height,
                    )
                )
            except (TypeError, ValueError):
                raise MediaDeliveryRejectedError("Discord media delivery attachment is unavailable") from None
        return tuple(validated)

    @staticmethod
    def _validate_result(
        result: object,
        request: MediaDeliverySinkRequest,
        expected: tuple[PreparedMediaAttachment, ...],
    ) -> tuple[int, ...]:
        if (
            _object_id(result) != request.message_id
            or _object_id(getattr(result, "channel", None)) != request.channel_id
            or _optional_guild_id(getattr(result, "guild", None)) != request.guild_id
        ):
            raise DiscordMediaDeliveryCommitUncertainError("Discord media delivery receipt is unavailable")
        raw_attachments = getattr(result, "attachments", None)
        if not isinstance(raw_attachments, (list, tuple)) or len(raw_attachments) != len(expected):
            raise DiscordMediaDeliveryCommitUncertainError("Discord media delivery receipt is unavailable")
        attachment_ids: list[int] = []
        for actual, prepared in zip(raw_attachments, expected, strict=True):
            attachment_id = _object_id(actual)
            if attachment_id is None or getattr(actual, "filename", None) != prepared.filename:
                raise DiscordMediaDeliveryCommitUncertainError("Discord media delivery receipt is unavailable")
            attachment_ids.append(attachment_id)
        if len(set(attachment_ids)) != len(attachment_ids):
            raise DiscordMediaDeliveryCommitUncertainError("Discord media delivery receipt is unavailable")
        return tuple(attachment_ids)


def _request_binding(request: MediaDeliverySinkRequest) -> bytes:
    digest = hashlib.sha256()
    digest.update(str(request.guild_id).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(request.channel_id).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(request.user_id).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(request.message_id).encode("ascii"))
    digest.update(b"\0")
    digest.update(request.delivery_digest.encode("ascii"))
    for action_id in request.required_action_ids:
        digest.update(b"\0")
        digest.update(action_id.encode("ascii"))
    for capability_id, minimum_level in request.required_capabilities:
        digest.update(b"\0")
        digest.update(capability_id.encode("ascii"))
        digest.update(b":")
        digest.update(str(minimum_level).encode("ascii"))
    for attachment in DiscordExactMessageMediaDeliverySink._validated_attachments(request):
        digest.update(b"\0")
        digest.update(attachment.filename.encode("ascii"))
        digest.update(b"\0")
        digest.update(attachment.media_type.encode("ascii"))
        digest.update(b"\0")
        digest.update(attachment.kind.value.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(attachment.width).encode("ascii"))
        digest.update(b"x")
        digest.update(str(attachment.height).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(attachment.data).digest())
    return digest.digest()


def _validated_request(value: object) -> MediaDeliverySinkRequest:
    if type(value) is not MediaDeliverySinkRequest:
        raise MediaDeliveryRejectedError("Discord media delivery request is unavailable")
    try:
        return MediaDeliverySinkRequest(
            guild_id=value.guild_id,
            channel_id=value.channel_id,
            user_id=value.user_id,
            message_id=value.message_id,
            delivery_digest=value.delivery_digest,
            required_action_ids=value.required_action_ids,
            required_capabilities=value.required_capabilities,
            attachments=value.attachments,
        )
    except (DurableMediaDeliveryError, TypeError, ValueError):
        raise MediaDeliveryRejectedError("Discord media delivery request is unavailable") from None


def _target_snapshot(target: object) -> bytes:
    content = getattr(target, "content", "")
    edited_at = getattr(target, "edited_at", None)
    attachments = getattr(target, "attachments", ())
    if not isinstance(content, str) or not isinstance(attachments, (list, tuple)):
        raise MediaDeliveryRejectedError("Discord media delivery target is unavailable")
    if edited_at is not None and (not isinstance(edited_at, datetime) or edited_at.tzinfo is None):
        raise MediaDeliveryRejectedError("Discord media delivery target is unavailable")
    digest = hashlib.sha256()
    digest.update(hashlib.sha256(content.encode("utf-8")).digest())
    digest.update(b"\0")
    digest.update(b"" if edited_at is None else edited_at.isoformat().encode("ascii"))
    for attachment in attachments:
        attachment_id = _object_id(attachment)
        filename = getattr(attachment, "filename", None)
        if attachment_id is None or not isinstance(filename, str):
            raise MediaDeliveryRejectedError("Discord media delivery target is unavailable")
        digest.update(b"\0")
        digest.update(str(attachment_id).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(filename.encode("utf-8")).digest())
    return digest.digest()


def _target_matches(
    target: object,
    *,
    request: MediaDeliverySinkRequest,
    bot_user_id: int,
) -> bool:
    author = getattr(target, "author", None)
    attachments = getattr(target, "attachments", None)
    return (
        _object_id(target) == request.message_id
        and _object_id(getattr(target, "channel", None)) == request.channel_id
        and _optional_guild_id(getattr(target, "guild", None)) == request.guild_id
        and _object_id(author) == bot_user_id
        and getattr(author, "bot", None) is True
        and isinstance(attachments, (list, tuple))
        and not attachments
        and callable(getattr(target, "edit", None))
    )


def _object_id(value: object) -> int | None:
    identifier = getattr(value, "id", None)
    return identifier if _is_discord_id(identifier) else None


def _optional_guild_id(value: object) -> int | None:
    if value is None:
        return None
    return _object_id(value)


def _is_discord_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_DISCORD_ID


__all__ = [
    "DiscordExactMessageMediaDeliverySink",
    "DiscordMediaDeliveryCommitUncertainError",
]
