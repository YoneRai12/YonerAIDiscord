"""Discord message-link expansion adapter.

The domain service keeps the request and exact-message binding transport
neutral.  This adapter owns only Discord REST facts and the final reply/delete
sinks.  Raw message bodies are never logged or written to audit details.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import discord

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.discord_payload_budget import (
    DiscordEmbedText,
    validate_discord_payload_budget,
)
from yonerai_discord.discord_policy import determine_rbac_level
from yonerai_discord.modules.message_expansion import (
    AuthorizationStage,
    FetchedMessage,
    FreshAuthorization,
    MessageDeliveryReceipt,
    MessageExpansionPayload,
    MessageExpansionRequest,
    MessageExpansionScope,
    MessageExpansionService,
    MessageExpansionStatus,
    MessageLinkTarget,
    SourceMessageSnapshot,
    parse_bare_message_link,
)
from yonerai_discord.runtime_manifests.ai_memory import (
    MESSAGE_LINK_EXPANSION_CAPABILITY_ID,
)


logger = logging.getLogger(__name__)

MESSAGE_LINK_EXPANSION_EVENT = "message_link_expand"
_DISCORD_IO_TIMEOUT_SECONDS = 15.0


class DiscordMessageExpansionAdapter:
    """Canonical bare message linkを同一guild内だけで展開する。"""

    def __init__(
        self,
        bot: Any,
        *,
        timeout_seconds: float = _DISCORD_IO_TIMEOUT_SECONDS,
        closing_current: Callable[[], bool] | None = None,
    ) -> None:
        if not 0.1 <= timeout_seconds <= 30.0:
            raise ValueError("timeout_seconds is out of range")
        if closing_current is not None and not callable(closing_current):
            raise TypeError("closing_current must be callable")
        self._bot = bot
        self._guard = getattr(bot, "capability_guard", None)
        self._registry = getattr(bot, "capability_registry", None)
        self._timeout_seconds = float(timeout_seconds)
        self._closing_current = closing_current or (lambda: bool(getattr(self._bot, "is_closing", False)))

    async def try_expand(self, message: discord.Message) -> bool:
        """明示bare linkなら常にconsumeし、通常AIへ流さない。"""

        content = getattr(message, "content", None)
        target = parse_bare_message_link(content)
        if target is None:
            return False
        if not self._initially_allowed(message):
            return True

        scope = self._scope(message, content)
        if scope is None:
            return True
        request = MessageExpansionRequest(scope=scope, source_content=content)
        runtime: dict[str, object] = {}

        async def authorize(
            current_request: MessageExpansionRequest,
            current_target: MessageLinkTarget,
            stage: AuthorizationStage,
        ) -> FreshAuthorization | None:
            return await self._fresh_authorization(
                message,
                current_request,
                current_target,
                runtime,
                stage,
            )

        async def fetch_target(current_target: MessageLinkTarget) -> FetchedMessage | None:
            channel = runtime.get("target_channel")
            if not self._target_channel_matches(channel, current_target):
                return None
            fetch_message = getattr(channel, "fetch_message", None)
            if not callable(fetch_message):
                return None
            try:
                fetched = await _discord_io_call(
                    fetch_message(current_target.message_id),
                    timeout_seconds=self._timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return None
            projected = self._project_fetched_message(fetched, current_target)
            if projected is not None:
                runtime["target_snapshot"] = projected
            return projected

        async def fetch_source(current_scope: MessageExpansionScope) -> SourceMessageSnapshot | None:
            source_channel = getattr(message, "channel", None)
            fetch_message = getattr(source_channel, "fetch_message", None)
            if not callable(fetch_message):
                return None
            try:
                fetched = await _discord_io_call(
                    fetch_message(current_scope.source_message_id),
                    timeout_seconds=self._timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return None
            return self._project_source_message(fetched, current_scope)

        async def deliver(
            current_scope: MessageExpansionScope,
            payload: MessageExpansionPayload,
        ) -> MessageDeliveryReceipt | None:
            source_channel = getattr(message, "channel", None)
            target_channel = runtime.get("target_channel")
            fetch_source_message = getattr(source_channel, "fetch_message", None)
            fetch_target_message = getattr(target_channel, "fetch_message", None)
            if (
                not callable(fetch_source_message)
                or not self._target_channel_matches(target_channel, target)
                or not callable(fetch_target_message)
            ):
                return None
            try:
                final_source, final_target = await asyncio.gather(
                    _discord_io_call(
                        fetch_source_message(current_scope.source_message_id),
                        timeout_seconds=self._timeout_seconds,
                    ),
                    _discord_io_call(
                        fetch_target_message(target.message_id),
                        timeout_seconds=self._timeout_seconds,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return None
            source_snapshot = self._project_source_message(final_source, current_scope)
            target_snapshot = self._project_fetched_message(final_target, target)
            if (
                source_snapshot is None
                or source_snapshot.revision != current_scope.source_revision
                or source_snapshot.content != request.source_content
                or target_snapshot is None
                or target_snapshot != runtime.get("target_snapshot")
                or not self._sync_authorization_current(current_scope, runtime)
            ):
                return None
            embed_text = DiscordEmbedText(
                title="Discordメッセージ",
                description=payload.content,
                footer="元のリンク投稿は展開成功後だけ削除します。",
            )
            validate_discord_payload_budget(embeds=(embed_text,))
            embed = discord.Embed(
                title=embed_text.title,
                description=embed_text.description,
                colour=discord.Colour.blurple(),
            )
            embed.set_footer(text=embed_text.footer)
            reply = getattr(message, "reply", None)
            if not callable(reply):
                return None
            response = await _discord_io_call(
                reply(
                    embed=embed,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout_seconds=self._timeout_seconds,
            )
            response_id = _discord_id(getattr(response, "id", None))
            response_channel_id = _discord_id(getattr(getattr(response, "channel", None), "id", None))
            response_guild_id = _discord_id(getattr(getattr(response, "guild", None), "id", None))
            if (
                response_id is None
                or response_channel_id != current_scope.channel_id
                or response_guild_id != current_scope.guild_id
            ):
                return None
            return MessageDeliveryReceipt(
                accepted=True,
                guild_id=current_scope.guild_id,
                channel_id=current_scope.channel_id,
                source_message_id=current_scope.source_message_id,
                delivery_id=str(response_id),
            )

        async def delete_source(current_request: MessageExpansionRequest) -> bool:
            source_channel = getattr(message, "channel", None)
            fetch_message = getattr(source_channel, "fetch_message", None)
            if not callable(fetch_message):
                return False
            try:
                source = await _discord_io_call(
                    fetch_message(current_request.scope.source_message_id),
                    timeout_seconds=self._timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return False
            snapshot = self._project_source_message(source, current_request.scope)
            if (
                snapshot is None
                or snapshot.revision != current_request.scope.source_revision
                or snapshot.content != current_request.source_content
                or not self._sync_authorization_current(current_request.scope, runtime)
            ):
                return False
            delete = getattr(source, "delete", None)
            if not callable(delete):
                return False
            await _discord_io_call(delete(), timeout_seconds=self._timeout_seconds)
            return True

        service = MessageExpansionService(
            authorize_current=authorize,
            fetch_target=fetch_target,
            fetch_source=fetch_source,
            deliver=deliver,
            delete_source=delete_source,
            fetch_timeout_seconds=min(self._timeout_seconds, 30.0),
            cache_entries=1,
        )
        try:
            receipt = await service.expand(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("message_link_expansion_failed", extra={"error_type": "unexpected"})
            return True
        await self._audit(message, receipt.status, receipt.reference_id, receipt.source_deleted)
        return True

    def _initially_allowed(self, message: discord.Message) -> bool:
        if (
            self._identity_changed()
            or self._closing_now()
            or getattr(message, "guild", None) is None
            or bool(getattr(getattr(message, "author", None), "bot", False))
            or getattr(message, "webhook_id", None) is not None
        ):
            return False
        is_system = getattr(message, "is_system", None)
        if callable(is_system):
            try:
                if is_system() is True:
                    return False
            except Exception:
                return False
        guild_id = _discord_id(getattr(getattr(message, "guild", None), "id", None))
        channel_id = _discord_id(getattr(getattr(message, "channel", None), "id", None))
        event_id = _discord_id(getattr(message, "id", None))
        author_id = _discord_id(getattr(getattr(message, "author", None), "id", None))
        checker = getattr(self._guard, "event_allowed", None)
        if None in (guild_id, channel_id, event_id, author_id) or not callable(checker):
            return False
        try:
            return (
                checker(
                    MESSAGE_LINK_EXPANSION_CAPABILITY_ID,
                    surface=MESSAGE_LINK_EXPANSION_EVENT,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    event_id=event_id,
                    user_id=author_id,
                    author_is_bot=False,
                    actor_level=self._cached_actor_level(message),
                )
                is True
            )
        except Exception:
            return False

    async def _fresh_authorization(
        self,
        message: discord.Message,
        request: MessageExpansionRequest,
        target: MessageLinkTarget,
        runtime: dict[str, object],
        stage: AuthorizationStage,
    ) -> FreshAuthorization | None:
        if self._identity_changed() or self._closing_now():
            return None
        scope = request.scope
        guild = getattr(message, "guild", None)
        source_channel = getattr(message, "channel", None)
        if (
            _discord_id(getattr(guild, "id", None)) != scope.guild_id
            or _discord_id(getattr(source_channel, "id", None)) != scope.channel_id
            or _discord_id(getattr(getattr(message, "author", None), "id", None)) != scope.caller_id
            or _discord_id(getattr(message, "id", None)) != scope.source_message_id
        ):
            return None
        fetch_member = getattr(guild, "fetch_member", None)
        fetch_channel = getattr(guild, "fetch_channel", None)
        bot_id = _discord_id(getattr(getattr(self._bot, "user", None), "id", None))
        if not callable(fetch_member) or not callable(fetch_channel) or bot_id is None:
            return None
        calls: list[Awaitable[Any]] = [
            _discord_io_call(fetch_member(scope.caller_id), timeout_seconds=self._timeout_seconds),
            _discord_io_call(fetch_member(bot_id), timeout_seconds=self._timeout_seconds),
            _discord_io_call(fetch_channel(target.channel_id), timeout_seconds=self._timeout_seconds),
        ]
        try:
            current = await asyncio.gather(*calls)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        actor, bot_member, target_channel = current[:3]
        if (
            _discord_id(getattr(actor, "id", None)) != scope.caller_id
            or bool(getattr(actor, "bot", False))
            or _discord_id(getattr(bot_member, "id", None)) != bot_id
            or bool(getattr(bot_member, "bot", False)) is not True
            or not self._target_channel_matches(target_channel, target)
        ):
            return None
        evaluate = getattr(self._guard, "evaluate_fresh_member", None)
        currently_allowed = getattr(self._guard, "currently_allowed", None)
        if not callable(evaluate) or not callable(currently_allowed):
            return None
        try:
            decision = await evaluate(
                MESSAGE_LINK_EXPANSION_CAPABILITY_ID,
                guild=guild,
                member=actor,
            )
            actor_level = getattr(decision, "actor_level", None)
            allowed = (
                getattr(decision, "allowed", False) is True
                and isinstance(actor_level, RbacLevel)
                and currently_allowed(
                    MESSAGE_LINK_EXPANSION_CAPABILITY_ID,
                    guild_id=scope.guild_id,
                    user_id=scope.caller_id,
                    actor_level=actor_level,
                    floor=RbacLevel.EVERYONE,
                )
                is True
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if not allowed or self._identity_changed() or self._closing_now():
            return None
        source_permissions_for = getattr(source_channel, "permissions_for", None)
        target_permissions_for = getattr(target_channel, "permissions_for", None)
        if not callable(source_permissions_for) or not callable(target_permissions_for):
            return None
        try:
            actor_source = source_permissions_for(actor)
            actor_target = target_permissions_for(actor)
            bot_source = source_permissions_for(bot_member)
            bot_target = target_permissions_for(bot_member)
        except Exception:
            return None
        runtime["target_channel"] = target_channel
        runtime["actor_level"] = actor_level
        return FreshAuthorization(
            caller_id=scope.caller_id,
            guild_id=scope.guild_id,
            source_channel_id=scope.channel_id,
            target_channel_id=target.channel_id,
            actor_can_view_source=_permission(actor_source, "view_channel"),
            actor_can_read_source=_permission(actor_source, "read_message_history"),
            actor_can_view_target=_permission(actor_target, "view_channel"),
            actor_can_read_target=_permission(actor_target, "read_message_history"),
            bot_can_view_source=_permission(bot_source, "view_channel"),
            bot_can_read_source=_permission(bot_source, "read_message_history"),
            bot_can_view_target=_permission(bot_target, "view_channel"),
            bot_can_read_target=_permission(bot_target, "read_message_history"),
            bot_can_send_source=(
                _permission(bot_source, "send_messages") or _permission(bot_source, "send_messages_in_threads")
            ),
            bot_can_manage_source=_permission(bot_source, "manage_messages"),
            closing=self._closing_now(),
        )

    def _scope(self, message: discord.Message, content: str) -> MessageExpansionScope | None:
        guild_id = _discord_id(getattr(getattr(message, "guild", None), "id", None))
        channel_id = _discord_id(getattr(getattr(message, "channel", None), "id", None))
        message_id = _discord_id(getattr(message, "id", None))
        caller_id = _discord_id(getattr(getattr(message, "author", None), "id", None))
        if None in (guild_id, channel_id, message_id, caller_id):
            return None
        return MessageExpansionScope(
            request_id=f"mx:{message_id}",
            caller_id=caller_id,
            guild_id=guild_id,
            channel_id=channel_id,
            source_message_id=message_id,
            source_revision=_message_revision(message_id, content, getattr(message, "edited_at", None)),
        )

    def _project_fetched_message(
        self,
        fetched: object,
        target: MessageLinkTarget,
    ) -> FetchedMessage | None:
        guild_id = _discord_id(getattr(getattr(fetched, "guild", None), "id", None))
        channel_id = _discord_id(getattr(getattr(fetched, "channel", None), "id", None))
        message_id = _discord_id(getattr(fetched, "id", None))
        author = getattr(fetched, "author", None)
        author_id = _discord_id(getattr(author, "id", None))
        content = getattr(fetched, "content", None)
        if (
            (guild_id, channel_id, message_id) != (target.guild_id, target.channel_id, target.message_id)
            or author_id is None
            or not isinstance(content, str)
        ):
            return None
        author_label = getattr(author, "display_name", None) or getattr(author, "name", None) or "不明"
        return FetchedMessage(
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            author_id=author_id,
            author_label=str(author_label),
            content=content,
        )

    def _project_source_message(
        self,
        fetched: object,
        scope: MessageExpansionScope,
    ) -> SourceMessageSnapshot | None:
        guild_id = _discord_id(getattr(getattr(fetched, "guild", None), "id", None))
        channel_id = _discord_id(getattr(getattr(fetched, "channel", None), "id", None))
        message_id = _discord_id(getattr(fetched, "id", None))
        author_id = _discord_id(getattr(getattr(fetched, "author", None), "id", None))
        content = getattr(fetched, "content", None)
        if (guild_id, channel_id, message_id, author_id) != (
            scope.guild_id,
            scope.channel_id,
            scope.source_message_id,
            scope.caller_id,
        ) or not isinstance(content, str):
            return None
        return SourceMessageSnapshot(
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            author_id=author_id,
            revision=_message_revision(message_id, content, getattr(fetched, "edited_at", None)),
            content=content,
        )

    def _target_channel_matches(self, channel: object, target: MessageLinkTarget) -> bool:
        return bool(
            _discord_id(getattr(channel, "id", None)) == target.channel_id
            and _discord_id(getattr(getattr(channel, "guild", None), "id", None)) == target.guild_id
        )

    def _identity_changed(self) -> bool:
        return bool(
            getattr(self._bot, "capability_guard", None) is not self._guard
            or getattr(self._bot, "capability_registry", None) is not self._registry
            or getattr(self._guard, "registry", None) is not self._registry
        )

    def _closing_now(self) -> bool:
        try:
            return self._closing_current() is not False
        except Exception:
            return True

    def _sync_authorization_current(
        self,
        scope: MessageExpansionScope,
        runtime: dict[str, object],
    ) -> bool:
        actor_level = runtime.get("actor_level")
        currently_allowed = getattr(self._guard, "currently_allowed", None)
        if (
            self._identity_changed()
            or self._closing_now()
            or not isinstance(actor_level, RbacLevel)
            or not callable(currently_allowed)
        ):
            return False
        try:
            return (
                currently_allowed(
                    MESSAGE_LINK_EXPANSION_CAPABILITY_ID,
                    guild_id=scope.guild_id,
                    user_id=scope.caller_id,
                    actor_level=actor_level,
                    floor=RbacLevel.EVERYONE,
                )
                is True
            )
        except Exception:
            return False

    def _cached_actor_level(self, message: discord.Message) -> RbacLevel:
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        settings = getattr(self._bot, "settings", None)
        if guild is None or author is None or settings is None:
            return RbacLevel.EVERYONE
        try:
            roles = getattr(author, "roles", ())
            role_ids = frozenset(int(role.id) for role in roles if _discord_id(getattr(role, "id", None)) is not None)
            return determine_rbac_level(
                user_id=int(author.id),
                guild_owner_id=(
                    int(guild.owner_id) if _discord_id(getattr(guild, "owner_id", None)) is not None else None
                ),
                permissions=getattr(author, "guild_permissions", None),
                role_ids=role_ids,
                settings=settings,
            )
        except Exception:
            return RbacLevel.EVERYONE

    async def _audit(
        self,
        message: discord.Message,
        status: MessageExpansionStatus,
        reference_id: str,
        source_deleted: bool,
    ) -> None:
        database = getattr(self._bot, "database", None)
        append = getattr(database, "append_audit", None)
        guild_id = _discord_id(getattr(getattr(message, "guild", None), "id", None))
        actor_id = _discord_id(getattr(getattr(message, "author", None), "id", None))
        if not callable(append) or guild_id is None or actor_id is None:
            return
        try:
            await asyncio.to_thread(
                append,
                "discord.message_link.expansion",
                actor_id=actor_id,
                guild_id=guild_id,
                plugin="ai",
                details={
                    "status": status.value,
                    "reference_id": reference_id,
                    "source_deleted": source_deleted,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("message_link_expansion_audit_failed")


def _permission(permissions: object, name: str) -> bool:
    return getattr(permissions, name, None) is True


def _discord_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _message_revision(message_id: int, content: str, edited_at: object) -> str:
    edited = edited_at.isoformat() if isinstance(edited_at, datetime) else ""
    encoded = f"{message_id}\0{edited}\0{content}".encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _discord_io_call(awaitable: Awaitable[Any], *, timeout_seconds: float) -> Any:
    return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
