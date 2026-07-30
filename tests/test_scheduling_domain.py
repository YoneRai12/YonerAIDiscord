from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yonerai_discord.modules.scheduling import (
    Meeting,
    NotificationPreferences,
    RSVP,
    RSVPStatus,
    Reminder,
    reminder_action_key,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def meeting() -> Meeting:
    return Meeting("meeting-1", 1, 2, 3, "週次会議", NOW + timedelta(hours=1), NOW + timedelta(hours=2), "Asia/Tokyo")


def test_domain_normalizes_aware_datetimes_to_utc() -> None:
    item = meeting()
    assert item.starts_at.tzinfo is UTC
    response = RSVP(item.id, 100, RSVPStatus.TENTATIVE, NOW)
    assert response.status is RSVPStatus.TENTATIVE
    assert NotificationPreferences(1, 100, timezone="Asia/Tokyo").enabled


def test_naive_or_reversed_meeting_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Meeting("id", 1, 2, 3, "会議", datetime(2026, 1, 1), NOW, "UTC")
    with pytest.raises(ValueError, match="after"):
        Meeting("id", 1, 2, 3, "会議", NOW, NOW, "UTC")


def test_action_key_is_stable_across_equivalent_timezones() -> None:
    other_zone = NOW.astimezone().replace(microsecond=999999)
    assert reminder_action_key("m", NOW, 10) == reminder_action_key("m", other_zone, 10)
    reminder = Reminder("r", "m", reminder_action_key("m", NOW, 10), NOW, 10)
    assert reminder.target_user_id == 10


def test_discord_ids_and_attempt_count_are_strictly_validated() -> None:
    with pytest.raises(ValueError, match="Discord IDs"):
        Meeting("id", 0, 2, 3, "会議", NOW, NOW + timedelta(hours=1), "UTC")
    with pytest.raises(ValueError, match="target_user_id"):
        Reminder("r", "m", "action", NOW, 0)
    with pytest.raises(ValueError, match="non-negative"):
        Reminder("r", "m", "action", NOW, None, attempts=-1)
