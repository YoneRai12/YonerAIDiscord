from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import Any

import discord

from yonerai_discord.modules.ai.discord_inputs import AttachmentLimits, DiscordInputError, collect_discord_attachments
from yonerai_discord.modules.image_generation.artifacts import canonicalize_png
from yonerai_discord.provider_registry import QualityTier

from .domain import (
    EDITED_IMAGE_FILENAME,
    IMAGE_EDITING_CAPABILITY_ID,
    ImageEditingError,
    ImageEditingRequest,
)
from .ports import ImageEditingSink
from .service import ImageEditingService
from .source_claims import ImageEditSourceClaimIssuer


_MENTION_SOURCE_ERROR = "編集元は、このメッセージまたは本人の同じチャンネルの返信元にある PNG 1枚だけにしてください。"


class ImageEditingDelivery:
    """未登録の将来Discord入口向けdelivery。Stage 1ではfake sinkだけを使う。"""

    def __init__(
        self,
        service: ImageEditingService,
        sink: ImageEditingSink | None,
        *,
        capability_check: Callable[[str, Any], bool | Awaitable[bool]],
        source_claim_issuer: ImageEditSourceClaimIssuer | None = None,
    ) -> None:
        if not isinstance(service, ImageEditingService):
            raise TypeError("service must be an ImageEditingService")
        if sink is not None and not callable(getattr(sink, "send_png", None)):
            raise TypeError("sink must provide send_png")
        if not callable(capability_check):
            raise TypeError("capability_check is required")
        self.service = service
        self.sink = sink
        self.source_claim_issuer = source_claim_issuer
        self._mention_sources: OrderedDict[str, tuple[str, Any, Any]] = OrderedDict()
        self.capability_check = capability_check
        self._closing = False

    def begin_close(self) -> None:
        self._closing = True
        self._mention_sources.clear()
        self.service.begin_close()

    async def deliver(self, interaction: Any, request: ImageEditingRequest) -> bool:
        if self.sink is None:
            return False
        if not self._scope_matches(interaction, request):
            return False
        authorization = self._authorization(interaction, request)
        if not await authorization():
            return False
        try:
            edited = await self.service.edit(request, authorization_current=authorization)
            if not await self.service.claim_delivery(request, edited, authorization_current=authorization):
                return False
            if not await authorization() or not await self.service.delivery_current(
                request, edited, authorization_current=authorization
            ):
                return False
            await self.sink.send_png(
                interaction, edited.png, filename=EDITED_IMAGE_FILENAME, ephemeral=True, mentions_allowed=False
            )
            return True
        except asyncio.CancelledError:
            raise
        except (ImageEditingError, TypeError, ValueError):
            return False
        except Exception:
            return False

    async def edit_for_message(
        self,
        message: Any,
        *,
        instruction: str,
        authorization_current: Callable[[], bool | Awaitable[bool]],
        settings: Any,
    ) -> bool:
        """@mention 専用の PNG ingress。外部 URL や非 PNG は service へ渡さない。"""
        if self._closing or not callable(authorization_current) or self.source_claim_issuer is None:
            return False
        guild, channel, author = (
            getattr(message, "guild", None),
            getattr(message, "channel", None),
            getattr(message, "author", None),
        )
        message_id = getattr(message, "id", None)
        ids = (getattr(guild, "id", None), getattr(channel, "id", None), getattr(author, "id", None), message_id)
        if not all(isinstance(value, int) and value > 0 for value in ids) or not await self._authorized(
            authorization_current
        ):
            return False
        try:
            source_message = await self._source_message(message)
            if source_message is None or not await self._authorized(authorization_current):
                return False
            limits = AttachmentLimits(
                max_files=1,
                max_file_bytes=min(
                    8 * 1024 * 1024, int(getattr(settings, "ai_attachment_max_file_bytes", 8 * 1024 * 1024))
                ),
                max_total_bytes=min(
                    8 * 1024 * 1024, int(getattr(settings, "ai_attachment_max_total_bytes", 8 * 1024 * 1024))
                ),
                read_timeout_seconds=min(15.0, max(1.0, float(getattr(settings, "ai_timeout_seconds", 15.0)))),
            )
            bundle = await collect_discord_attachments(
                (source_message,), enabled=True, limits=limits, read_allowed=authorization_current
            )
            if bundle.attachment_count != 1 or len(bundle.attachments) != 1:
                return False
            attachment = bundle.attachments[0]
            if getattr(attachment, "mime_type", None) != "image/png":
                return False
            canonical = canonicalize_png(attachment.data)
            request_id = f"image-edit-mention-{message_id}"
            source_binding = hashlib.sha256(
                f"image-edit-source:{guild.id}:{channel.id}:{author.id}:{message_id}".encode()
            ).hexdigest()
            source_digest = canonical.sha256
            cached = self._mention_sources.get(request_id)
            if cached is not None and cached[0] == source_digest and self.source_claim_issuer.current(cached[2]):
                artifact, claim = cached[1], cached[2]
                self._mention_sources.move_to_end(request_id)
            else:
                store = self.service.artifact_store
                put_png = getattr(store, "put_png", None)
                discard_png = getattr(store, "discard_png", None)
                if not callable(put_png) or not callable(discard_png):
                    return False
                artifact = put_png(canonical.data, request_binding=source_binding)
                try:
                    claim = await self.source_claim_issuer.issue(
                        artifact,
                        source_binding=source_binding,
                        edit_request_id=request_id,
                        guild_id=guild.id,
                        channel_id=channel.id,
                        actor_id=author.id,
                        authorization_current=authorization_current,
                    )
                except BaseException:
                    try:
                        discard_png(artifact, request_binding=source_binding)
                    except Exception:
                        pass
                    raise
                self._mention_sources[request_id] = (source_digest, artifact, claim)
                while len(self._mention_sources) > 64:
                    self._mention_sources.popitem(last=False)
            request = ImageEditingRequest(
                request_id=request_id,
                guild_id=guild.id,
                channel_id=channel.id,
                actor_id=author.id,
                instruction=instruction,
                source=claim,
                tier=QualityTier.BALANCED,
            )
            edited = await self.service.edit(request, authorization_current=authorization_current)
            if not await self.service.claim_delivery(request, edited, authorization_current=authorization_current):
                return False
            if not await self.service.delivery_current(request, edited, authorization_current=authorization_current):
                return False
            if not await self._authorized(authorization_current):
                return False
            reply = getattr(message, "reply", None)
            if not callable(reply):
                return False
            await reply(
                file=discord.File(BytesIO(edited.png), filename=EDITED_IMAGE_FILENAME),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except asyncio.CancelledError:
            raise
        except (DiscordInputError, ImageEditingError, TypeError, ValueError):
            return False
        except Exception:
            return False

    async def _source_message(self, message: Any) -> Any | None:
        """返信元を使う場合も同一 guild/channel・本人・PNG source のみを認める。"""
        current_attachments = getattr(message, "attachments", ()) or ()
        reference = getattr(message, "reference", None)
        if current_attachments:
            return message
        reference_id = getattr(reference, "message_id", None)
        if not isinstance(reference_id, int) or reference_id <= 0:
            return None
        guild, channel, author = (
            getattr(message, "guild", None),
            getattr(message, "channel", None),
            getattr(message, "author", None),
        )
        if getattr(reference, "guild_id", None) not in (None, getattr(guild, "id", None)) or getattr(
            reference, "channel_id", None
        ) not in (None, getattr(channel, "id", None)):
            return None
        resolved = getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)
        if resolved is None:
            fetch = getattr(channel, "fetch_message", None)
            if not callable(fetch):
                return None
            try:
                resolved = await fetch(reference_id)
            except (discord.HTTPException, AttributeError, TypeError, ValueError):
                return None
        if (
            getattr(resolved, "id", None) != reference_id
            or getattr(getattr(resolved, "guild", None), "id", None) != getattr(guild, "id", None)
            or getattr(getattr(resolved, "channel", None), "id", None) != getattr(channel, "id", None)
            or getattr(getattr(resolved, "author", None), "id", None) != getattr(author, "id", None)
            or bool(getattr(getattr(resolved, "author", None), "bot", False))
        ):
            return None
        return resolved

    async def _authorized(self, check: Callable[[], bool | Awaitable[bool]]) -> bool:
        try:
            allowed = check()
            if inspect.isawaitable(allowed):
                allowed = await allowed
            return not self._closing and allowed is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    def _authorization(self, interaction: Any, request: ImageEditingRequest):
        async def current() -> bool:
            return self._scope_matches(interaction, request) and await self._allowed(interaction)

        return current

    async def _allowed(self, interaction: Any) -> bool:
        if self._closing or getattr(interaction, "guild_id", None) is None:
            return False
        try:
            value = self.capability_check(IMAGE_EDITING_CAPABILITY_ID, interaction)
            value = await value if inspect.isawaitable(value) else value
            return not self._closing and value is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    @staticmethod
    def _scope_matches(interaction: Any, request: ImageEditingRequest) -> bool:
        if not isinstance(request, ImageEditingRequest):
            return False
        values = (
            getattr(interaction, "guild_id", None),
            getattr(interaction, "channel_id", None),
            getattr(getattr(interaction, "user", None), "id", None),
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            return False
        guild_id, channel_id, actor_id = values
        return guild_id == request.guild_id and channel_id == request.channel_id and actor_id == request.actor_id


__all__ = ["ImageEditingDelivery"]
