from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Lock


class GateDecision(StrEnum):
    ALLOW = "allow"
    BOT_AUTHOR = "bot_author"
    GUILD_BLOCKED = "guild_blocked"
    CHANNEL_BLOCKED = "channel_blocked"
    DUPLICATE = "duplicate"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class InputEnvelope:
    event_id: str
    guild_id: int | None
    channel_id: int
    user_id: int
    author_is_bot: bool
    received_at: datetime
    bucket: str = "global"

    def __post_init__(self) -> None:
        if (
            not self.event_id.strip()
            or (self.guild_id is not None and self.guild_id <= 0)
            or min(self.channel_id, self.user_id) <= 0
        ):
            raise ValueError("invalid input envelope")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        if not self.bucket.strip() or len(self.bucket) > 128:
            raise ValueError("bucket must contain 1 to 128 characters")


@dataclass(frozen=True, slots=True)
class InputPolicy:
    guild_allowlist: frozenset[int] = frozenset()
    channel_allowlist: frozenset[int] = frozenset()
    events_per_user_per_minute: int = 30
    dedupe_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if self.events_per_user_per_minute <= 0 or self.dedupe_ttl_seconds <= 0:
            raise ValueError("gate limits must be positive")


class InputGate:
    """Bot投稿、allowlist、重複event、ユーザー単位sliding windowを一元判定する。"""

    def __init__(self, policy: InputPolicy) -> None:
        self.policy = policy
        self._events: dict[str, datetime] = {}
        self._users: dict[tuple[int | None, int, str], deque[datetime]] = defaultdict(deque)
        self._lock = Lock()

    def evaluate(self, envelope: InputEnvelope, *, rate_limit: int | None = None) -> GateDecision:
        now = envelope.received_at.astimezone(UTC)
        effective_limit = self.policy.events_per_user_per_minute if rate_limit is None else rate_limit
        if isinstance(effective_limit, bool) or not isinstance(effective_limit, int) or effective_limit <= 0:
            raise ValueError("rate_limit must be a positive integer")
        if envelope.author_is_bot:
            return GateDecision.BOT_AUTHOR
        if envelope.guild_id is not None:
            if self.policy.guild_allowlist and envelope.guild_id not in self.policy.guild_allowlist:
                return GateDecision.GUILD_BLOCKED
            if self.policy.channel_allowlist and envelope.channel_id not in self.policy.channel_allowlist:
                return GateDecision.CHANNEL_BLOCKED
        with self._lock:
            duplicate_cutoff = now - timedelta(seconds=self.policy.dedupe_ttl_seconds)
            self._events = {key: seen for key, seen in self._events.items() if seen > duplicate_cutoff}
            if envelope.event_id in self._events:
                return GateDecision.DUPLICATE

            key = (envelope.guild_id, envelope.user_id, envelope.bucket)
            window = self._users[key]
            rate_cutoff = now - timedelta(minutes=1)
            while window and window[0] <= rate_cutoff:
                window.popleft()
            if len(window) >= effective_limit:
                return GateDecision.RATE_LIMITED

            self._events[envelope.event_id] = now
            window.append(now)
        return GateDecision.ALLOW
