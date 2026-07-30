from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from hashlib import sha256
from typing import Mapping


MAX_SQLITE_SNOWFLAKE = (1 << 63) - 1


class EventKind(str, Enum):
    MESSAGE_CREATE = "message_create"
    MESSAGE_EDIT = "message_edit"


class Severity(IntEnum):
    INFO = 0
    LOW = 10
    MEDIUM = 20
    HIGH = 30
    CRITICAL = 40


class ActionType(str, Enum):
    AUDIT = "audit"


class AutomodMode(str, Enum):
    """Discord adapterが受け付ける唯一の実行モード。"""

    REPORT_ONLY = "report_only"


@dataclass(frozen=True, slots=True)
class MessageEvent:
    kind: EventKind
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    content: str
    author_role_ids: frozenset[int] = frozenset()
    mention_user_ids: frozenset[int] = frozenset()
    mention_role_ids: frozenset[int] = frozenset()
    mentions_everyone: bool = False
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def content_fingerprint(self) -> str:
        return sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoricalMessage:
    channel_id: int
    normalized_content: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class DetectionContext:
    recent_messages: tuple[HistoricalMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class Detection:
    detector: str
    rule: str
    severity: Severity
    confidence: float
    evidence: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Policy:
    ignored_user_ids: frozenset[int] = frozenset()
    ignored_role_ids: frozenset[int] = frozenset()
    ignored_channel_ids: frozenset[int] = frozenset()
    allowed_domains: frozenset[str] = frozenset()
    minimum_severity: Severity = Severity.LOW
    timeout_seconds: int = 600


@dataclass(frozen=True, slots=True)
class GuildAutomodConfig:
    """サーバーごとのAutoMod設定。

    enforcementモードをデータモデル自体に持たせず、保存値から破壊的処理へ
    移行できないようにする。
    """

    guild_id: int
    report_channel_id: int | None = None
    enabled: bool = False
    mode: AutomodMode = AutomodMode.REPORT_ONLY

    def __post_init__(self) -> None:
        if (
            not isinstance(self.guild_id, int)
            or isinstance(self.guild_id, bool)
            or not 0 < self.guild_id <= MAX_SQLITE_SNOWFLAKE
        ):
            raise ValueError("guild_id must be a positive integer")
        if self.report_channel_id is not None and (
            not isinstance(self.report_channel_id, int)
            or isinstance(self.report_channel_id, bool)
            or not 0 < self.report_channel_id <= MAX_SQLITE_SNOWFLAKE
        ):
            raise ValueError("report_channel_id must be a positive integer or None")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be bool")
        if self.mode is not AutomodMode.REPORT_ONLY:
            raise ValueError("only report_only mode is supported")
        if self.enabled and self.report_channel_id is None:
            raise ValueError("report channel is required before enabling automod")


@dataclass(frozen=True, slots=True)
class Decision:
    should_act: bool
    severity: Severity
    reason: str
    detections: tuple[Detection, ...]
    strike_count: int = 0


@dataclass(frozen=True, slots=True)
class PlannedAction:
    action_type: ActionType
    action_key: str
    reason: str
    duration_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ActionPlan:
    event: MessageEvent
    decision: Decision
    actions: tuple[PlannedAction, ...]


@dataclass(frozen=True, slots=True)
class AuditRecord:
    action_key: str
    guild_id: int
    channel_id: int
    message_id: int
    actor_id: int
    action_type: ActionType
    severity: Severity
    reason: str
    succeeded: bool
    detail: str = ""
