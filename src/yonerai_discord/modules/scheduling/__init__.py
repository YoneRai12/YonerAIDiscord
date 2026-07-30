"""Discord SDK非依存の会議・RSVP・リマインダー基盤。"""

from __future__ import annotations

from typing import Any

from .domain import (
    AllowedMentions,
    ClaimedReminder,
    DeliveryIntent,
    DeliveryIntentState,
    DeliveryResolution,
    Meeting,
    NotificationPreferences,
    RSVP,
    RSVPStatus,
    Reminder,
    ReminderDelivery,
    ReminderStatus,
    parse_aware_datetime,
    reminder_action_key,
)
from .plugin import SchedulingPlugin
from .ports import ReminderRepository, ReminderSender, ScheduleIntentParser
from .repository import SqliteReminderRepository
from .service import ReminderDispatcher, ReminderWorker, RetryableDeliveryError


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register("scheduling", SchedulingPlugin)


__all__ = [
    "AllowedMentions",
    "ClaimedReminder",
    "DeliveryIntent",
    "DeliveryIntentState",
    "DeliveryResolution",
    "Meeting",
    "NotificationPreferences",
    "RSVP",
    "RSVPStatus",
    "Reminder",
    "ReminderDelivery",
    "ReminderDispatcher",
    "ReminderRepository",
    "ReminderSender",
    "ReminderStatus",
    "parse_aware_datetime",
    "ReminderWorker",
    "RetryableDeliveryError",
    "ScheduleIntentParser",
    "SchedulingPlugin",
    "SqliteReminderRepository",
    "reminder_action_key",
    "setup",
]
