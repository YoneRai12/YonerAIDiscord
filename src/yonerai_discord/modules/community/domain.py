from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


_MAX_SQLITE_INTEGER = 2**63 - 1


def _require_snowflake(value: int, field: str) -> None:
    """Discord の snowflake として SQLite に安全に保存できる値だけを受け付ける。"""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _MAX_SQLITE_INTEGER:
        raise ValueError(f"invalid {field}")


def _require_identifier(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    normalized = value.strip()
    if not normalized or len(normalized) > 64 or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"invalid {field}")
    return normalized


class TicketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class PollStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class SuggestionStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IMPLEMENTED = "implemented"


@dataclass(frozen=True, slots=True)
class Ticket:
    id: str
    guild_id: int
    owner_id: int
    subject: str
    status: TicketStatus = TicketStatus.OPEN
    channel_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str):
            raise ValueError("invalid ticket")
        subject = self.subject.strip()
        identifier = _require_identifier(self.id, "ticket id")
        _require_snowflake(self.guild_id, "guild id")
        _require_snowflake(self.owner_id, "owner id")
        if self.channel_id is not None:
            _require_snowflake(self.channel_id, "channel id")
        if not subject or len(subject) > 200:
            raise ValueError("invalid ticket")
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "subject", subject)


@dataclass(frozen=True, slots=True)
class Poll:
    id: str
    guild_id: int
    creator_id: int
    question: str
    options: tuple[str, ...]
    status: PollStatus = PollStatus.OPEN
    channel_id: int | None = None
    message_id: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.question, str)
            or not isinstance(self.options, tuple)
            or not all(isinstance(option, str) for option in self.options)
        ):
            raise ValueError("invalid poll")
        question = self.question.strip()
        options = tuple(option.strip() for option in self.options)
        identifier = _require_identifier(self.id, "poll id")
        _require_snowflake(self.guild_id, "guild id")
        _require_snowflake(self.creator_id, "creator id")
        if self.channel_id is not None:
            _require_snowflake(self.channel_id, "channel id")
        if self.message_id is not None:
            _require_snowflake(self.message_id, "message id")
        if not question or len(question) > 300:
            raise ValueError("invalid poll question")
        if not 2 <= len(options) <= 5 or any(not option or len(option) > 100 for option in options):
            raise ValueError("poll requires 2-5 non-empty options")
        if len(set(options)) != len(options):
            raise ValueError("poll options must be unique")
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "question", question)
        object.__setattr__(self, "options", options)


@dataclass(frozen=True, slots=True)
class PollResult:
    option_index: int
    option: str
    votes: int


@dataclass(frozen=True, slots=True)
class Suggestion:
    id: str
    guild_id: int
    author_id: int
    content: str
    status: SuggestionStatus = SuggestionStatus.PENDING

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValueError("invalid suggestion")
        content = self.content.strip()
        identifier = _require_identifier(self.id, "suggestion id")
        _require_snowflake(self.guild_id, "guild id")
        _require_snowflake(self.author_id, "author id")
        if not content or len(content) > 1500:
            raise ValueError("invalid suggestion")
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "content", content)


@dataclass(frozen=True, slots=True)
class ActorPolicy:
    actor_id: int
    guild_owner_id: int
    administrator: bool = False
    manage_guild: bool = False
    manage_channels: bool = False
    manage_roles: bool = False
    manage_messages: bool = False

    def __post_init__(self) -> None:
        _require_snowflake(self.actor_id, "actor id")
        _require_snowflake(self.guild_owner_id, "guild owner id")

    @property
    def is_owner_or_admin(self) -> bool:
        return self.actor_id == self.guild_owner_id or self.administrator


def utc_now() -> datetime:
    return datetime.now(UTC)
