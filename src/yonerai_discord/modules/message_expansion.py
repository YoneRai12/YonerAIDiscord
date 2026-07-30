from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

_MESSAGE_LINK_RE = re.compile(
    r"https://discord\.com/channels/"
    r"(?P<guild>[1-9][0-9]{16,19})/"
    r"(?P<channel>[1-9][0-9]{16,19})/"
    r"(?P<message>[1-9][0-9]{16,19})"
)
_REVISION_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_MAX_SOURCE_CONTENT = 256
_MAX_AUTHOR_LABEL = 80
_MAX_QUOTE_TEXT = 1_300
_MAX_DELIVERY_CONTENT = 1_900


class MessageExpansionStatus(StrEnum):
    DELIVERED = "delivered"
    INVALID_INPUT = "invalid_input"
    CROSS_GUILD = "cross_guild"
    DENIED = "denied"
    FETCH_FAILED = "fetch_failed"
    FETCH_TIMEOUT = "fetch_timeout"
    BINDING_MISMATCH = "binding_mismatch"
    DELIVERY_FAILED = "delivery_failed"
    REPLAY_REJECTED = "replay_rejected"


class AuthorizationStage(StrEnum):
    BEFORE_FETCH = "before_fetch"
    AFTER_FETCH = "after_fetch"
    BEFORE_DELIVERY = "before_delivery"
    BEFORE_SOURCE_RECHECK = "before_source_recheck"
    BEFORE_SOURCE_DELETE = "before_source_delete"


class AllowedMentionsPolicy(StrEnum):
    NONE = "none"


@dataclass(frozen=True, slots=True)
class MessageLinkTarget:
    guild_id: int
    channel_id: int
    message_id: int

    @property
    def canonical_url(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"


@dataclass(frozen=True, slots=True)
class MessageExpansionScope:
    request_id: str
    caller_id: int
    guild_id: int
    channel_id: int
    source_message_id: int
    source_revision: str

    def __post_init__(self) -> None:
        if not _REQUEST_ID_RE.fullmatch(self.request_id):
            raise ValueError("request_id is invalid")
        for value in (
            self.caller_id,
            self.guild_id,
            self.channel_id,
            self.source_message_id,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("scope identifiers must be positive integers")
        if not _REVISION_RE.fullmatch(self.source_revision):
            raise ValueError("source_revision is invalid")


@dataclass(frozen=True, slots=True)
class MessageExpansionRequest:
    scope: MessageExpansionScope
    source_content: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source_content, str):
            raise ValueError("source_content must be text")


@dataclass(frozen=True, slots=True)
class FreshAuthorization:
    caller_id: int
    guild_id: int
    source_channel_id: int
    target_channel_id: int
    actor_can_view_source: bool
    actor_can_read_source: bool
    actor_can_view_target: bool
    actor_can_read_target: bool
    bot_can_view_source: bool
    bot_can_read_source: bool
    bot_can_view_target: bool
    bot_can_read_target: bool
    bot_can_send_source: bool
    bot_can_manage_source: bool = False
    closing: bool = False


@dataclass(frozen=True, slots=True)
class FetchedMessage:
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    author_label: str = field(repr=False)
    content: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SourceMessageSnapshot:
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    revision: str
    content: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MessageExpansionPayload:
    content: str = field(repr=False)
    allowed_mentions: AllowedMentionsPolicy = field(
        default=AllowedMentionsPolicy.NONE,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class MessageDeliveryReceipt:
    accepted: bool
    guild_id: int
    channel_id: int
    source_message_id: int
    delivery_id: str


@dataclass(frozen=True, slots=True)
class MessageExpansionReceipt:
    request_id: str
    status: MessageExpansionStatus
    reference_id: str
    payload: MessageExpansionPayload | None = None
    delivery: MessageDeliveryReceipt | None = None
    source_deleted: bool = False

    def __repr__(self) -> str:
        return (
            "MessageExpansionReceipt("
            f"request_id={self.request_id!r}, status={self.status!r}, "
            f"reference_id={self.reference_id!r}, "
            f"has_payload={self.payload is not None}, "
            f"has_delivery={self.delivery is not None}, "
            f"source_deleted={self.source_deleted!r})"
        )


class FreshAuthorizationPort(Protocol):
    async def __call__(
        self,
        request: MessageExpansionRequest,
        target: MessageLinkTarget,
        stage: AuthorizationStage,
    ) -> FreshAuthorization | None: ...


class TargetMessageFetchPort(Protocol):
    async def __call__(self, target: MessageLinkTarget) -> FetchedMessage | None: ...


class SourceMessageFetchPort(Protocol):
    async def __call__(
        self,
        scope: MessageExpansionScope,
    ) -> SourceMessageSnapshot | None: ...


class MessageDeliveryPort(Protocol):
    async def __call__(
        self,
        scope: MessageExpansionScope,
        payload: MessageExpansionPayload,
    ) -> MessageDeliveryReceipt | None: ...


class SourceMessageDeletePort(Protocol):
    async def __call__(self, request: MessageExpansionRequest) -> bool: ...


def parse_bare_message_link(value: object) -> MessageLinkTarget | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_SOURCE_CONTENT:
        return None
    matched = _MESSAGE_LINK_RE.fullmatch(value)
    if matched is None:
        return None
    values = tuple(int(matched.group(name)) for name in ("guild", "channel", "message"))
    if any(value > (2**64 - 1) for value in values):
        return None
    return MessageLinkTarget(*values)


class MessageExpansionService:
    def __init__(
        self,
        *,
        authorize_current: FreshAuthorizationPort,
        fetch_target: TargetMessageFetchPort,
        fetch_source: SourceMessageFetchPort,
        deliver: MessageDeliveryPort,
        delete_source: SourceMessageDeletePort,
        fetch_timeout_seconds: float = 5.0,
        cache_entries: int = 128,
    ) -> None:
        if not 0.1 <= fetch_timeout_seconds <= 30.0:
            raise ValueError("fetch_timeout_seconds is out of range")
        if not 1 <= cache_entries <= 1_024:
            raise ValueError("cache_entries is out of range")
        self._authorize_current = authorize_current
        self._fetch_target = fetch_target
        self._fetch_source = fetch_source
        self._deliver = deliver
        self._delete_source = delete_source
        self._fetch_timeout_seconds = float(fetch_timeout_seconds)
        self._cache_entries = cache_entries
        self._cache: OrderedDict[str, tuple[str, MessageExpansionReceipt]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def expand(self, request: MessageExpansionRequest) -> MessageExpansionReceipt:
        digest = _request_digest(request)
        async with self._lock:
            cached = self._cache.get(request.scope.request_id)
            if cached is not None:
                self._cache.move_to_end(request.scope.request_id)
                if cached[0] == digest:
                    return cached[1]
                return _receipt(request, MessageExpansionStatus.REPLAY_REJECTED)
            receipt = await self._expand_once(request)
            self._store(request.scope.request_id, digest, receipt)
            return receipt

    async def _expand_once(
        self,
        request: MessageExpansionRequest,
    ) -> MessageExpansionReceipt:
        target = parse_bare_message_link(request.source_content)
        if target is None:
            return _receipt(request, MessageExpansionStatus.INVALID_INPUT)
        if target.guild_id != request.scope.guild_id:
            return _receipt(request, MessageExpansionStatus.CROSS_GUILD)
        if not await self._authorized(request, target, AuthorizationStage.BEFORE_FETCH):
            return _receipt(request, MessageExpansionStatus.DENIED)

        try:
            fetched = await asyncio.wait_for(
                self._fetch_target(target),
                timeout=self._fetch_timeout_seconds,
            )
        except TimeoutError:
            return _receipt(request, MessageExpansionStatus.FETCH_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _receipt(request, MessageExpansionStatus.FETCH_FAILED)

        if not _fetched_matches_target(fetched, target):
            return _receipt(request, MessageExpansionStatus.BINDING_MISMATCH)
        if not await self._authorized(request, target, AuthorizationStage.AFTER_FETCH):
            return _receipt(request, MessageExpansionStatus.DENIED)

        payload = _build_payload(target, fetched)
        if not await self._authorized(request, target, AuthorizationStage.BEFORE_DELIVERY):
            return _receipt(request, MessageExpansionStatus.DENIED)
        try:
            delivery = await self._deliver(request.scope, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _receipt(request, MessageExpansionStatus.DELIVERY_FAILED)
        if not _delivery_matches_scope(delivery, request.scope):
            return _receipt(request, MessageExpansionStatus.DELIVERY_FAILED)

        delivered = _receipt(
            request,
            MessageExpansionStatus.DELIVERED,
            payload=payload,
            delivery=delivery,
        )
        # Delivery is already an external side effect. Cache it before any
        # deletion-related await so cancellation cannot cause a duplicate send.
        digest = _request_digest(request)
        self._store(request.scope.request_id, digest, delivered)

        if not await self._authorized(
            request,
            target,
            AuthorizationStage.BEFORE_SOURCE_RECHECK,
        ):
            return delivered
        source = await self._safe_fetch_source(request.scope)
        if not _source_matches_request(source, request):
            return delivered
        if not await self._authorized(
            request,
            target,
            AuthorizationStage.BEFORE_SOURCE_DELETE,
            require_manage=True,
        ):
            return delivered
        try:
            # The sink receives the complete immutable binding so its final
            # Discord-side commit can recheck author, body, and revision.
            deleted = await self._delete_source(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            deleted = False
        if deleted is not True:
            return delivered
        completed = replace(delivered, source_deleted=True)
        self._store(request.scope.request_id, digest, completed)
        return completed

    async def _authorized(
        self,
        request: MessageExpansionRequest,
        target: MessageLinkTarget,
        stage: AuthorizationStage,
        *,
        require_manage: bool = False,
    ) -> bool:
        try:
            current = await self._authorize_current(request, target, stage)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        if current is None:
            return False
        scope = request.scope
        return (
            current.caller_id == scope.caller_id
            and current.guild_id == scope.guild_id
            and current.source_channel_id == scope.channel_id
            and current.target_channel_id == target.channel_id
            and current.actor_can_view_source is True
            and current.actor_can_read_source is True
            and current.actor_can_view_target is True
            and current.actor_can_read_target is True
            and current.bot_can_view_source is True
            and current.bot_can_read_source is True
            and current.bot_can_view_target is True
            and current.bot_can_read_target is True
            and current.bot_can_send_source is True
            and (not require_manage or current.bot_can_manage_source is True)
            and current.closing is False
        )

    async def _safe_fetch_source(
        self,
        scope: MessageExpansionScope,
    ) -> SourceMessageSnapshot | None:
        try:
            return await asyncio.wait_for(
                self._fetch_source(scope),
                timeout=self._fetch_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    def _store(
        self,
        request_id: str,
        digest: str,
        receipt: MessageExpansionReceipt,
    ) -> None:
        self._cache[request_id] = (digest, receipt)
        self._cache.move_to_end(request_id)
        while len(self._cache) > self._cache_entries:
            self._cache.popitem(last=False)


def _request_digest(request: MessageExpansionRequest) -> str:
    scope = request.scope
    payload = {
        "request_id": scope.request_id,
        "caller_id": scope.caller_id,
        "guild_id": scope.guild_id,
        "channel_id": scope.channel_id,
        "source_message_id": scope.source_message_id,
        "source_revision": scope.source_revision,
        "source_content": request.source_content,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _receipt(
    request: MessageExpansionRequest,
    status: MessageExpansionStatus,
    *,
    payload: MessageExpansionPayload | None = None,
    delivery: MessageDeliveryReceipt | None = None,
) -> MessageExpansionReceipt:
    reference = hashlib.sha256(
        f"message-expansion:{request.scope.request_id}:{status.value}".encode("ascii")
    ).hexdigest()[:16]
    return MessageExpansionReceipt(
        request_id=request.scope.request_id,
        status=status,
        reference_id=f"mx_{reference}",
        payload=payload,
        delivery=delivery,
    )


def _fetched_matches_target(
    fetched: FetchedMessage | None,
    target: MessageLinkTarget,
) -> bool:
    return bool(
        fetched is not None
        and fetched.guild_id == target.guild_id
        and fetched.channel_id == target.channel_id
        and fetched.message_id == target.message_id
        and isinstance(fetched.author_id, int)
        and not isinstance(fetched.author_id, bool)
        and fetched.author_id > 0
        and isinstance(fetched.author_label, str)
        and isinstance(fetched.content, str)
    )


def _delivery_matches_scope(
    delivery: MessageDeliveryReceipt | None,
    scope: MessageExpansionScope,
) -> bool:
    return bool(
        delivery is not None
        and delivery.accepted is True
        and delivery.guild_id == scope.guild_id
        and delivery.channel_id == scope.channel_id
        and delivery.source_message_id == scope.source_message_id
        and isinstance(delivery.delivery_id, str)
        and _REQUEST_ID_RE.fullmatch(delivery.delivery_id)
    )


def _source_matches_request(
    source: SourceMessageSnapshot | None,
    request: MessageExpansionRequest,
) -> bool:
    scope = request.scope
    return bool(
        source is not None
        and source.guild_id == scope.guild_id
        and source.channel_id == scope.channel_id
        and source.message_id == scope.source_message_id
        and source.author_id == scope.caller_id
        and source.revision == scope.source_revision
        and source.content == request.source_content
    )


def _build_payload(
    target: MessageLinkTarget,
    fetched: FetchedMessage,
) -> MessageExpansionPayload:
    author = _safe_discord_text(fetched.author_label, _MAX_AUTHOR_LABEL) or "不明"
    quote = _safe_discord_text(fetched.content, _MAX_QUOTE_TEXT) or "（本文なし）"
    quoted_lines = "\n".join(f"> {line}" for line in quote.splitlines()) or "> （本文なし）"
    content = f"[元メッセージを開く]({target.canonical_url})\n投稿者: **{author}**\n{quoted_lines}"
    if len(content) > _MAX_DELIVERY_CONTENT:
        content = content[: _MAX_DELIVERY_CONTENT - 1] + "…"
    return MessageExpansionPayload(content=content)


def _safe_discord_text(value: str, maximum: int) -> str:
    cleaned = "".join(
        character
        for character in value.replace("\r\n", "\n").replace("\r", "\n")
        if character == "\n" or (ord(character) >= 0x20 and ord(character) != 0x7F)
    )
    cleaned = cleaned.replace("@", "@\u200b")
    for character in ("\\", "`", "*", "_", "~", "|", ">", "[", "]", "<", "#"):
        cleaned = cleaned.replace(character, f"\\{character}")
    if len(cleaned) > maximum:
        cleaned = cleaned[: maximum - 1] + "…"
    return cleaned
