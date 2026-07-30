from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .models import Attachment, MessageRole, Turn
from .state_repository import AIStateRepository, StoredConversation


DEFAULT_TTL_SECONDS = 2 * 60 * 60
DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_TEXT_CHARS = 24_000
DEFAULT_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_BINARY_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_ATTACHMENTS_PER_TURN = 4
DEFAULT_MAX_SESSIONS = 128
DEFAULT_MAX_TOTAL_BINARY_BYTES = 64 * 1024 * 1024


class ConversationSessionError(RuntimeError):
    """会話sessionが期限切れ、reset済み、または別sessionだった。"""


class ConversationIndexConflictError(RuntimeError):
    """同じDiscord message IDを複数の会話へ関連付けようとした。"""


@dataclass(frozen=True, slots=True)
class ConversationKey:
    guild_id: int | None
    channel_id: int
    user_id: int

    def __post_init__(self) -> None:
        if self.guild_id is not None and (
            isinstance(self.guild_id, bool) or not isinstance(self.guild_id, int) or self.guild_id <= 0
        ):
            raise ValueError("guild_id must be a positive integer or None")
        for name, value in (("channel_id", self.channel_id), ("user_id", self.user_id)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    session_id: str
    key: ConversationKey
    history: tuple[Turn, ...] = field(repr=False)
    created_at: float
    updated_at: float

    @property
    def guild_id(self) -> int | None:
        return self.key.guild_id

    @property
    def channel_id(self) -> int:
        return self.key.channel_id

    @property
    def user_id(self) -> int:
        return self.key.user_id


@dataclass(frozen=True, slots=True)
class ConversationStoreStats:
    """本文・添付・識別子を含めないprocess全体の集計snapshot。"""

    session_count: int
    exchange_count: int
    message_count: int
    text_chars: int
    binary_bytes: int
    index_count: int
    max_sessions: int
    max_total_binary_bytes: int
    evicted_sessions: int
    expired_sessions: int


@dataclass(slots=True)
class _Exchange:
    user: Turn
    assistant: Turn
    bot_message_id: int | None

    @property
    def text_chars(self) -> int:
        return len(self.user.text) + len(self.assistant.text)

    @property
    def binary_bytes(self) -> int:
        return self.user.binary_bytes


@dataclass(slots=True)
class _Session:
    session_id: str
    key: ConversationKey
    exchanges: deque[_Exchange]
    created_at: float
    updated_at: float
    access_order: int
    text_chars: int = 0
    binary_bytes: int = 0


class ConversationStore:
    """process内限定の、TTL・容量上限付き会話履歴。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        max_binary_bytes: int = DEFAULT_MAX_BINARY_BYTES,
        max_attachments_per_turn: int = DEFAULT_MAX_ATTACHMENTS_PER_TURN,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_total_binary_bytes: int = DEFAULT_MAX_TOTAL_BINARY_BYTES,
        clock: Callable[[], float] | None = None,
        repository: AIStateRepository | None = None,
        database_path: str | Path | None = None,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(float(ttl_seconds))
            or float(ttl_seconds) <= 0
        ):
            raise ValueError("ttl_seconds must be a positive finite number")
        _validate_limit("max_turns", max_turns, minimum=1, maximum=128)
        _validate_limit("max_text_chars", max_text_chars, minimum=1, maximum=1_000_000)
        _validate_limit(
            "max_attachment_bytes",
            max_attachment_bytes,
            minimum=1,
            maximum=50 * 1024 * 1024,
        )
        _validate_limit(
            "max_binary_bytes",
            max_binary_bytes,
            minimum=1,
            maximum=100 * 1024 * 1024,
        )
        if max_attachment_bytes > max_binary_bytes:
            raise ValueError("max_attachment_bytes must not exceed max_binary_bytes")
        _validate_limit("max_attachments_per_turn", max_attachments_per_turn, minimum=1, maximum=32)
        _validate_limit("max_sessions", max_sessions, minimum=1, maximum=4_096)
        _validate_limit(
            "max_total_binary_bytes",
            max_total_binary_bytes,
            minimum=1,
            maximum=1024 * 1024 * 1024,
        )
        if max_total_binary_bytes < max_binary_bytes:
            raise ValueError("max_total_binary_bytes must not be less than max_binary_bytes")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if repository is not None and database_path is not None:
            raise ValueError("repository and database_path are mutually exclusive")

        self._ttl_seconds = float(ttl_seconds)
        self._max_turns = max_turns
        self._max_text_chars = max_text_chars
        self._max_attachment_bytes = max_attachment_bytes
        self._max_binary_bytes = max_binary_bytes
        self._max_attachments_per_turn = max_attachments_per_turn
        self._max_sessions = max_sessions
        self._max_total_binary_bytes = max_total_binary_bytes
        # Persistent timestamps must survive a process restart. A caller-supplied
        # test clock remains supported for deterministic expiry tests.
        self._clock = clock or time.time
        self._repository = repository or (AIStateRepository(database_path) if database_path is not None else None)
        self._owns_repository = repository is None and database_path is not None
        self._sessions: dict[ConversationKey, _Session] = {}
        self._message_index: dict[int, tuple[ConversationKey, str]] = {}
        self._access_counter = 0
        self._evicted_sessions = 0
        self._expired_sessions = 0
        self._lock = asyncio.Lock()
        self._load_persisted()

    async def start(self, *, guild_id: int | None, channel_id: int, user_id: int) -> ConversationSnapshot:
        """既存会話を破棄し、必ず新しいsessionを開始する。"""

        key = ConversationKey(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        async with self._lock:
            now = self._now()
            self._expire_locked(now)
            self._drop_session_locked(key)
            session = self._new_session(key, now)
            self._sessions[key] = session
            self._touch_locked(session, now)
            self._enforce_global_limits_locked(protected_key=key)
            self._persist_locked()
            return self._snapshot(session)

    async def reset(self, *, guild_id: int | None, channel_id: int, user_id: int) -> ConversationSnapshot:
        """start()の明示的なreset名。旧message indexも同時に破棄する。"""

        return await self.start(guild_id=guild_id, channel_id=channel_id, user_id=user_id)

    async def drop(self, *, guild_id: int | None, channel_id: int, user_id: int) -> bool:
        """対象scopeのsessionと過去bot message indexを残さず破棄する。"""

        key = ConversationKey(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        async with self._lock:
            self._expire_locked(self._now())
            existed = key in self._sessions
            self._drop_session_locked(key)
            self._persist_locked()
            return existed

    async def drop_user(self, *, user_id: int) -> int:
        """ユーザーの全guild/channel会話とbot reply indexを破棄する。"""

        _validate_discord_id("user_id", user_id)
        async with self._lock:
            self._expire_locked(self._now())
            keys = tuple(key for key in self._sessions if key.user_id == user_id)
            for key in keys:
                self._drop_session_locked(key)
            self._persist_locked()
            return len(keys)

    async def get_or_start(self, *, guild_id: int | None, channel_id: int, user_id: int) -> ConversationSnapshot:
        key = ConversationKey(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        async with self._lock:
            now = self._now()
            self._expire_locked(now)
            session = self._sessions.get(key)
            if session is None:
                session = self._new_session(key, now)
                self._sessions[key] = session
            self._touch_locked(session, now)
            self._enforce_global_limits_locked(protected_key=key)
            self._persist_locked()
            return self._snapshot(session)

    async def get(self, *, guild_id: int | None, channel_id: int, user_id: int) -> ConversationSnapshot | None:
        key = ConversationKey(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        async with self._lock:
            now = self._now()
            self._expire_locked(now)
            session = self._sessions.get(key)
            if session is None:
                self._persist_locked()
                return None
            self._touch_locked(session, now)
            self._enforce_global_limits_locked(protected_key=key)
            self._persist_locked()
            return self._snapshot(session)

    async def resolve(
        self,
        *,
        bot_message_id: int,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
    ) -> ConversationSnapshot | None:
        """reply先のbot messageを、同一owner/guild/channelのsessionにだけ解決する。"""

        _validate_discord_id("bot_message_id", bot_message_id)
        key = ConversationKey(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        async with self._lock:
            now = self._now()
            self._expire_locked(now)
            indexed = self._message_index.get(bot_message_id)
            if indexed is None or indexed[0] != key:
                self._persist_locked()
                return None
            session = self._sessions.get(key)
            if session is None or session.session_id != indexed[1]:
                self._message_index.pop(bot_message_id, None)
                self._persist_locked()
                return None
            self._touch_locked(session, now)
            self._enforce_global_limits_locked(protected_key=key)
            self._persist_locked()
            return self._snapshot(session)

    async def peek_reference(
        self,
        *,
        bot_message_id: int,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
    ) -> ConversationSnapshot | None:
        """返信indexを状態変更なしで完全一致scopeのsnapshotへ解決する。"""

        _validate_discord_id("bot_message_id", bot_message_id)
        key = ConversationKey(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        async with self._lock:
            now = self._now()
            indexed = self._message_index.get(bot_message_id)
            if indexed is None or indexed[0] != key:
                return None
            session = self._sessions.get(key)
            if session is None or session.session_id != indexed[1]:
                return None
            if now - session.updated_at >= self._ttl_seconds:
                return None
            return self._snapshot(session)

    async def is_active_reference(
        self,
        *,
        bot_message_id: int,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
    ) -> bool:
        """返信indexの所有権とTTLだけを、状態を変更せず軽量確認する。"""

        return (
            await self.peek_reference(
                bot_message_id=bot_message_id,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            is not None
        )

    async def detach_bot_message_reference_if_current(
        self,
        *,
        bot_message_id: int,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        session_id: str,
        authorization_current: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> bool:
        """完全一致する現行exchangeから、staleなbot reply indexだけをCASで外す。"""

        _validate_discord_id("bot_message_id", bot_message_id)
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise ValueError("session_id must be a non-empty bounded string")
        key = ConversationKey(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        async with self._lock:
            indexed = self._message_index.get(bot_message_id)
            if indexed != (key, session_id):
                return False
            session = self._sessions.get(key)
            if session is None or session.session_id != session_id:
                return False
            matches = tuple(exchange for exchange in session.exchanges if exchange.bot_message_id == bot_message_id)
            if len(matches) != 1:
                return False
            if authorization_current is not None:
                try:
                    detach_allowed = authorization_current()
                    if hasattr(detach_allowed, "__await__"):
                        detach_allowed = await detach_allowed
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return False
                if detach_allowed is not True:
                    return False
                if self._message_index.get(bot_message_id) != (key, session_id):
                    return False
                if self._sessions.get(key) is not session or session.session_id != session_id:
                    return False
            exchange = matches[0]
            exchange.bot_message_id = None
            self._message_index.pop(bot_message_id, None)
            try:
                self._persist_locked()
            except BaseException:
                exchange.bot_message_id = bot_message_id
                self._message_index[bot_message_id] = indexed
                raise
            return True

    async def is_active_scope(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
    ) -> bool:
        """完全一致する会話scopeが有効か、状態を変更せず確認する。"""

        key = ConversationKey(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        async with self._lock:
            session = self._sessions.get(key)
            return session is not None and self._now() - session.updated_at < self._ttl_seconds

    async def resolve_active(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
    ) -> ConversationSnapshot | None:
        """完全一致する有効な会話scopeを解決し、処理中のTTLを更新する。"""

        key = ConversationKey(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        async with self._lock:
            now = self._now()
            self._expire_locked(now)
            session = self._sessions.get(key)
            if session is None:
                self._persist_locked()
                return None
            self._touch_locked(session, now)
            self._enforce_global_limits_locked(protected_key=key)
            self._persist_locked()
            return self._snapshot(session)

    async def append_exchange(
        self,
        *,
        session_id: str,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        user_text: str,
        assistant_text: str,
        attachments: Iterable[Attachment] = (),
        bot_message_id: int | None = None,
        authorization_current: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> ConversationSnapshot:
        """一往復を原子的に追記し、上限超過時は古い往復から削る。"""

        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise ValueError("session_id must be a non-empty bounded string")
        key = ConversationKey(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        attachment_tuple = tuple(attachments)
        if len(attachment_tuple) > self._max_attachments_per_turn:
            raise ValueError("too many attachments for one turn")
        if any(not isinstance(attachment, Attachment) for attachment in attachment_tuple):
            raise TypeError("attachments must contain only Attachment instances")
        if any(attachment.byte_length > self._max_attachment_bytes for attachment in attachment_tuple):
            raise ValueError("an attachment exceeds the configured per-file limit")

        user_turn = Turn(role=MessageRole.USER, text=user_text, attachments=attachment_tuple)
        assistant_turn = Turn(role=MessageRole.ASSISTANT, text=assistant_text)
        exchange = _Exchange(user=user_turn, assistant=assistant_turn, bot_message_id=bot_message_id)
        if exchange.text_chars > self._max_text_chars:
            raise ValueError("one exchange exceeds the configured text limit")
        if exchange.binary_bytes > self._max_binary_bytes:
            raise ValueError("one exchange exceeds the configured binary limit")
        if bot_message_id is not None:
            _validate_discord_id("bot_message_id", bot_message_id)

        async with self._lock:
            now = self._now()
            self._expire_locked(now)
            if authorization_current is not None:
                try:
                    append_allowed = authorization_current()
                    if hasattr(append_allowed, "__await__"):
                        append_allowed = await append_allowed
                    append_allowed = append_allowed is True
                except Exception:
                    append_allowed = False
                if not append_allowed:
                    raise ConversationSessionError("conversation authorization changed")
            session = self._sessions.get(key)
            if session is None or session.session_id != session_id:
                raise ConversationSessionError("conversation session is no longer active")
            if bot_message_id is not None and bot_message_id in self._message_index:
                raise ConversationIndexConflictError("bot message is already indexed")

            session.exchanges.append(exchange)
            session.text_chars += exchange.text_chars
            session.binary_bytes += exchange.binary_bytes
            self._touch_locked(session, now)
            if bot_message_id is not None:
                self._message_index[bot_message_id] = (key, session.session_id)
            self._prune_locked(session)
            self._enforce_global_limits_locked(protected_key=key)
            self._persist_locked()
            return self._snapshot(session)

    async def clear_expired(self) -> int:
        """期限切れsessionを明示清掃し、削除件数を返す。"""

        async with self._lock:
            expired = self._expire_locked(self._now())
            self._persist_locked()
            return expired

    async def stats(self) -> ConversationStoreStats:
        """TTL清掃後のcontent-freeなresource使用量を返す。"""

        async with self._lock:
            self._expire_locked(self._now())
            self._enforce_global_limits_locked(protected_key=None)
            self._persist_locked()
            sessions = tuple(self._sessions.values())
            exchange_count = sum(len(session.exchanges) for session in sessions)
            return ConversationStoreStats(
                session_count=len(sessions),
                exchange_count=exchange_count,
                message_count=exchange_count * 2,
                text_chars=sum(session.text_chars for session in sessions),
                binary_bytes=sum(session.binary_bytes for session in sessions),
                index_count=len(self._message_index),
                max_sessions=self._max_sessions,
                max_total_binary_bytes=self._max_total_binary_bytes,
                evicted_sessions=self._evicted_sessions,
                expired_sessions=self._expired_sessions,
            )

    def close(self) -> None:
        if self._owns_repository and self._repository is not None:
            self._repository.close()

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise RuntimeError("conversation clock returned a non-finite value")
        return now

    @staticmethod
    def _new_session(key: ConversationKey, now: float) -> _Session:
        return _Session(
            session_id=uuid.uuid4().hex,
            key=key,
            exchanges=deque(),
            created_at=now,
            updated_at=now,
            access_order=0,
        )

    @staticmethod
    def _snapshot(session: _Session) -> ConversationSnapshot:
        history = tuple(turn for exchange in session.exchanges for turn in (exchange.user, exchange.assistant))
        return ConversationSnapshot(
            session_id=session.session_id,
            key=session.key,
            history=history,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )

    def _expire_locked(self, now: float) -> int:
        expired_keys = [key for key, session in self._sessions.items() if now - session.updated_at >= self._ttl_seconds]
        for key in expired_keys:
            self._drop_session_locked(key)
        self._expired_sessions += len(expired_keys)
        return len(expired_keys)

    def _drop_session_locked(self, key: ConversationKey) -> None:
        session = self._sessions.pop(key, None)
        if session is None:
            return
        for exchange in session.exchanges:
            if exchange.bot_message_id is not None:
                self._message_index.pop(exchange.bot_message_id, None)

    def _prune_locked(self, session: _Session) -> None:
        while (
            len(session.exchanges) > self._max_turns
            or session.text_chars > self._max_text_chars
            or session.binary_bytes > self._max_binary_bytes
        ):
            removed = session.exchanges.popleft()
            session.text_chars -= removed.text_chars
            session.binary_bytes -= removed.binary_bytes
            if removed.bot_message_id is not None:
                self._message_index.pop(removed.bot_message_id, None)

    def _touch_locked(self, session: _Session, now: float) -> None:
        self._access_counter += 1
        session.updated_at = now
        session.access_order = self._access_counter

    def _enforce_global_limits_locked(self, *, protected_key: ConversationKey | None) -> None:
        while (
            len(self._sessions) > self._max_sessions or self._total_binary_bytes_locked() > self._max_total_binary_bytes
        ):
            candidates = [session for key, session in self._sessions.items() if key != protected_key]
            if not candidates:
                raise RuntimeError("protected conversation exceeds the configured process resource cap")
            victim = min(candidates, key=lambda session: (session.access_order, session.created_at, session.session_id))
            self._drop_session_locked(victim.key)
            self._evicted_sessions += 1

    def _total_binary_bytes_locked(self) -> int:
        return sum(session.binary_bytes for session in self._sessions.values())

    def _load_persisted(self) -> None:
        if self._repository is None:
            return
        for stored in self._repository.load_conversations():
            key = ConversationKey(None if stored.guild_id == 0 else stored.guild_id, stored.channel_id, stored.user_id)
            exchanges: deque[_Exchange] = deque()
            text_chars = 0
            for user_text, assistant_text, bot_message_id in stored.exchanges:
                exchange = _Exchange(
                    user=Turn(role=MessageRole.USER, text=user_text),
                    assistant=Turn(role=MessageRole.ASSISTANT, text=assistant_text),
                    bot_message_id=bot_message_id,
                )
                exchanges.append(exchange)
                text_chars += exchange.text_chars
                if bot_message_id is not None:
                    self._message_index[bot_message_id] = (key, stored.session_id)
            self._sessions[key] = _Session(
                session_id=stored.session_id,
                key=key,
                exchanges=exchanges,
                created_at=stored.created_at,
                updated_at=stored.updated_at,
                access_order=stored.access_order,
                text_chars=text_chars,
                # Attachment bytes intentionally never cross a restart boundary.
                binary_bytes=0,
            )
            self._access_counter = max(self._access_counter, stored.access_order)
        self._expire_locked(self._now())
        for session in tuple(self._sessions.values()):
            self._prune_locked(session)
        self._enforce_global_limits_locked(protected_key=None)
        self._persist_locked()

    def _persist_locked(self) -> None:
        if self._repository is None:
            return
        self._repository.replace_conversations(
            StoredConversation(
                session_id=session.session_id,
                guild_id=0 if session.key.guild_id is None else session.key.guild_id,
                channel_id=session.key.channel_id,
                user_id=session.key.user_id,
                created_at=session.created_at,
                updated_at=session.updated_at,
                access_order=session.access_order,
                exchanges=tuple(
                    (exchange.user.text, exchange.assistant.text, exchange.bot_message_id)
                    for exchange in session.exchanges
                ),
            )
            for session in self._sessions.values()
        )


def _validate_limit(name: str, value: int, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _validate_discord_id(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
