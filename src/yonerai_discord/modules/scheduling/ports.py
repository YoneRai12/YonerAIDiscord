from __future__ import annotations

from datetime import datetime, timedelta
from collections.abc import Callable
from typing import Protocol

from .domain import (
    ClaimedReminder,
    DeliveryIntent,
    DeliveryResolution,
    Meeting,
    NotificationPreferences,
    RSVP,
    Reminder,
    ReminderDelivery,
)


class ReminderRepository(Protocol):
    def save_meeting(self, meeting: Meeting) -> bool: ...
    def get_meeting(self, meeting_id: str) -> Meeting | None: ...
    def list_meetings(self, guild_id: int, now: datetime, limit: int = 10) -> tuple[Meeting, ...]: ...
    def cancel_meeting(self, meeting_id: str, guild_id: int, actor_id: int, *, can_manage: bool) -> bool: ...
    def save_rsvp(self, rsvp: RSVP) -> None: ...
    def save_preferences(self, preferences: NotificationPreferences) -> None: ...
    def get_preferences(self, guild_id: int, user_id: int) -> NotificationPreferences | None: ...
    def schedule(self, reminder: Reminder) -> bool: ...
    def claim_due(self, now: datetime, lease: timedelta, limit: int = 50) -> tuple[ClaimedReminder, ...]: ...
    def mark_sent(self, reminder_id: str, claim_token: str, sent_at: datetime) -> bool: ...
    def mark_failed(self, reminder_id: str, claim_token: str, error_type: str) -> bool: ...
    def release(self, reminder_id: str, claim_token: str, error_type: str) -> bool: ...
    def defer(
        self,
        reminder_id: str,
        claim_token: str,
        not_before: datetime,
        error_type: str,
    ) -> bool: ...
    def prepare_delivery(self, reminder_id: str, claim_token: str, prepared_at: datetime) -> bool: ...
    def complete_delivery(self, reminder_id: str, claim_token: str, sent_at: datetime) -> bool: ...
    def mark_delivery_uncertain(
        self,
        reminder_id: str,
        claim_token: str,
        uncertain_at: datetime,
        error_type: str,
    ) -> bool: ...
    def release_prepared(
        self,
        reminder_id: str,
        claim_token: str,
        not_before: datetime,
        error_type: str,
    ) -> bool: ...
    def list_delivery_intents(
        self,
        *,
        guild_id: int | None = None,
        limit: int = 50,
    ) -> tuple[DeliveryIntent, ...]: ...
    def get_delivery_intent(self, reminder_id: str) -> DeliveryIntent | None: ...
    def resolve_delivery_intent(
        self,
        reminder_id: str,
        resolution: DeliveryResolution,
        resolved_at: datetime,
    ) -> bool: ...


class ReminderSender(Protocol):
    """adapterはaction_keyを冪等キーとして扱い、AllowedMentionsを明示変換する。"""

    async def send(
        self,
        delivery: ReminderDelivery,
        *,
        still_allowed: Callable[[], bool],
    ) -> None: ...


class ScheduleIntentParser(Protocol):
    """自然言語解析は後段adapterが実装するための境界のみ定義する。"""

    async def parse(self, text: str, timezone: str) -> Meeting: ...
