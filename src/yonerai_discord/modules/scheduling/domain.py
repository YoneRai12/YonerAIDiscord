from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_aware_datetime(value: str) -> datetime:
    """Slash と明示メンションで共有する offset 必須の日時入力。"""

    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone offset is required")
    return parsed


def require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def as_utc(value: datetime, field_name: str = "datetime") -> datetime:
    return require_aware(value, field_name).astimezone(UTC)


def _validate_timezone_key(value: str) -> None:
    """OSにIANA DBがないWindowsでも安全なキー形式は保持できるようにする。"""
    if not value or "\\" in value or ".." in value or value.startswith("/"):
        raise ValueError("invalid timezone key")
    if value == "UTC":
        return
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        # tzdata未導入環境では正しいIANA keyも検証不能。少なくともArea/Location形式を要求する。
        if "/" not in value or any(not part for part in value.split("/")):
            raise ValueError("unknown timezone")


class RSVPStatus(StrEnum):
    ATTENDING = "attending"
    TENTATIVE = "tentative"
    DECLINED = "declined"


class ReminderStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SENT = "sent"
    FAILED = "failed"


class DeliveryIntentState(StrEnum):
    """Discord送信とSQLite確定の間をdurableに塞ぐ状態。"""

    PREPARED = "prepared"
    UNCERTAIN = "uncertain"


class DeliveryResolution(StrEnum):
    """運用者がDiscord側を確認した後にだけ選べる解決方法。"""

    RETRY = "retry"
    SENT = "sent"


@dataclass(frozen=True, slots=True)
class Meeting:
    id: str
    guild_id: int
    channel_id: int
    creator_id: int
    title: str
    starts_at: datetime
    ends_at: datetime
    timezone: str

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.title.strip():
            raise ValueError("meeting id and title are required")
        if self.guild_id <= 0 or self.channel_id <= 0 or self.creator_id <= 0:
            raise ValueError("Discord IDs must be positive")
        start = as_utc(self.starts_at, "starts_at")
        end = as_utc(self.ends_at, "ends_at")
        if end <= start:
            raise ValueError("ends_at must be after starts_at")
        _validate_timezone_key(self.timezone)
        object.__setattr__(self, "starts_at", start)
        object.__setattr__(self, "ends_at", end)
        object.__setattr__(self, "title", self.title.strip())


@dataclass(frozen=True, slots=True)
class RSVP:
    meeting_id: str
    user_id: int
    status: RSVPStatus
    responded_at: datetime

    def __post_init__(self) -> None:
        if not self.meeting_id.strip() or self.user_id <= 0:
            raise ValueError("meeting_id and a positive user_id are required")
        object.__setattr__(self, "responded_at", as_utc(self.responded_at, "responded_at"))


@dataclass(frozen=True, slots=True)
class NotificationPreferences:
    guild_id: int
    user_id: int
    enabled: bool = True
    direct_message: bool = True
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        if self.guild_id <= 0 or self.user_id <= 0:
            raise ValueError("Discord IDs must be positive")
        _validate_timezone_key(self.timezone)


@dataclass(frozen=True, slots=True)
class Reminder:
    id: str
    meeting_id: str
    action_key: str
    due_at: datetime
    target_user_id: int | None
    status: ReminderStatus = ReminderStatus.PENDING
    attempts: int = 0

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.meeting_id.strip() or not self.action_key.strip():
            raise ValueError("reminder id, meeting_id and action_key are required")
        if self.target_user_id is not None and self.target_user_id <= 0:
            raise ValueError("target_user_id must be positive")
        if self.attempts < 0:
            raise ValueError("attempts must be non-negative")
        object.__setattr__(self, "due_at", as_utc(self.due_at, "due_at"))


@dataclass(frozen=True, slots=True)
class ClaimedReminder:
    reminder: Reminder
    claim_token: str
    claim_expires_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryIntent:
    reminder_id: str
    meeting_id: str
    guild_id: int
    action_key: str
    claim_token: str
    state: DeliveryIntentState
    prepared_at: datetime
    uncertain_at: datetime | None = None
    error_type: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.reminder_id.strip()
            or not self.meeting_id.strip()
            or not self.action_key.strip()
            or not self.claim_token.strip()
            or self.guild_id <= 0
        ):
            raise ValueError("delivery intent identifiers are required")
        object.__setattr__(self, "prepared_at", as_utc(self.prepared_at, "prepared_at"))
        if self.uncertain_at is not None:
            object.__setattr__(self, "uncertain_at", as_utc(self.uncertain_at, "uncertain_at"))


@dataclass(frozen=True, slots=True)
class AllowedMentions:
    """Discord adapterが必ず明示的に変換するメンション許可契約。"""

    user_ids: tuple[int, ...] = ()
    role_ids: tuple[int, ...] = ()
    everyone: bool = False
    replied_user: bool = False


@dataclass(frozen=True, slots=True)
class ReminderDelivery:
    reminder: Reminder
    meeting: Meeting
    allowed_mentions: AllowedMentions


def reminder_action_key(meeting_id: str, due_at: datetime, target_user_id: int | None) -> str:
    timestamp = as_utc(due_at, "due_at").isoformat(timespec="seconds")
    target = str(target_user_id) if target_user_id is not None else "channel"
    return f"meeting:{meeting_id}:reminder:{timestamp}:{target}"
