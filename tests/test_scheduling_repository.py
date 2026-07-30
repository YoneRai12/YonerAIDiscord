from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from yonerai_discord.modules.scheduling import (
    DeliveryIntentState,
    DeliveryResolution,
    Meeting,
    NotificationPreferences,
    Reminder,
    ReminderStatus,
    SqliteReminderRepository,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def add_meeting(repository: SqliteReminderRepository) -> None:
    repository.save_meeting(Meeting("m1", 1, 2, 3, "運営会議", NOW, NOW + timedelta(hours=1), "UTC"))


def test_action_key_prevents_duplicate_scheduling(tmp_path) -> None:
    repository = SqliteReminderRepository(tmp_path / "schedule.sqlite3")
    repository.open()
    try:
        add_meeting(repository)
        first = Reminder("r1", "m1", "same-action", NOW, 10)
        duplicate = Reminder("r2", "m1", "same-action", NOW, 10)
        assert repository.schedule(first)
        assert not repository.schedule(duplicate)
    finally:
        repository.close()


def test_meeting_id_collision_never_overwrites_existing_owner_or_guild(tmp_path) -> None:
    repository = SqliteReminderRepository(tmp_path / "schedule.sqlite3")
    repository.open()
    try:
        original = Meeting("m1", 1, 2, 3, "原本", NOW, NOW + timedelta(hours=1), "UTC")
        collision = Meeting("m1", 99, 98, 97, "上書き", NOW, NOW + timedelta(hours=2), "UTC")
        assert repository.save_meeting(original)
        assert not repository.save_meeting(collision)
        assert repository.get_meeting("m1") == original
    finally:
        repository.close()


def test_due_time_boundary_is_inclusive(tmp_path) -> None:
    repository = SqliteReminderRepository(tmp_path / "schedule.sqlite3")
    repository.open()
    try:
        add_meeting(repository)
        repository.schedule(Reminder("r1", "m1", "boundary", NOW, None))
        assert repository.claim_due(NOW - timedelta(microseconds=1), timedelta(minutes=1)) == ()
        claims = repository.claim_due(NOW, timedelta(minutes=1))
        assert [claim.reminder.id for claim in claims] == ["r1"]
        assert repository.claim_due(NOW, timedelta(minutes=1)) == ()
    finally:
        repository.close()


def test_expired_claim_is_recovered_after_restart(tmp_path) -> None:
    path = tmp_path / "schedule.sqlite3"
    first = SqliteReminderRepository(path)
    first.open()
    add_meeting(first)
    first.schedule(Reminder("r1", "m1", "recover", NOW, None))
    original = first.claim_due(NOW, timedelta(minutes=1))[0]
    first.close()

    restarted = SqliteReminderRepository(path)
    restarted.open()
    try:
        assert restarted.claim_due(NOW + timedelta(seconds=59), timedelta(minutes=1)) == ()
        recovered = restarted.claim_due(NOW + timedelta(minutes=1), timedelta(minutes=1))
        assert len(recovered) == 1
        assert recovered[0].claim_token != original.claim_token
        assert recovered[0].reminder.attempts == 2
    finally:
        restarted.close()


def test_two_repository_connections_cannot_claim_the_same_reminder(tmp_path) -> None:
    path = tmp_path / "schedule.sqlite3"
    first = SqliteReminderRepository(path)
    second = SqliteReminderRepository(path)
    first.open()
    second.open()
    try:
        add_meeting(first)
        first.schedule(Reminder("r1", "m1", "single-claim", NOW, None))
        assert len(first.claim_due(NOW, timedelta(minutes=1))) == 1
        assert second.claim_due(NOW, timedelta(minutes=1)) == ()
    finally:
        second.close()
        first.close()


def test_stale_claim_token_cannot_finish_delivery(tmp_path) -> None:
    repository = SqliteReminderRepository(tmp_path / "schedule.sqlite3")
    repository.open()
    try:
        add_meeting(repository)
        repository.schedule(Reminder("r1", "m1", "token", NOW, None))
        claim = repository.claim_due(NOW, timedelta(minutes=1))[0]
        assert not repository.mark_sent("r1", "wrong-token", NOW)
        assert repository.status_of("r1") is ReminderStatus.CLAIMED
        assert repository.mark_sent("r1", claim.claim_token, NOW)
        assert repository.status_of("r1") is ReminderStatus.SENT
    finally:
        repository.close()


def test_personal_notification_preferences_round_trip(tmp_path) -> None:
    repository = SqliteReminderRepository(tmp_path / "schedule.sqlite3")
    repository.open()
    try:
        preferences = NotificationPreferences(1, 42, enabled=False, direct_message=False, timezone="Asia/Tokyo")
        repository.save_preferences(preferences)
        assert repository.get_preferences(1, 42) == preferences
        assert repository.get_preferences(1, 999) is None
    finally:
        repository.close()


def test_delivery_intent_blocks_expired_lease_and_becomes_operator_visible(tmp_path) -> None:
    repository = SqliteReminderRepository(tmp_path / "schedule.sqlite3")
    repository.open()
    try:
        add_meeting(repository)
        repository.schedule(Reminder("r1", "m1", "intent", NOW, None))
        claim = repository.claim_due(NOW, timedelta(minutes=1))[0]
        assert repository.prepare_delivery("r1", claim.claim_token, NOW)
        assert not repository.release("r1", claim.claim_token, "unsafe-release")
        assert not repository.mark_sent("r1", claim.claim_token, NOW)

        assert repository.claim_due(NOW + timedelta(minutes=1), timedelta(minutes=1)) == ()
        intent = repository.get_delivery_intent("r1")
        assert intent is not None
        assert intent.state is DeliveryIntentState.UNCERTAIN
        assert intent.error_type == "LeaseExpiredAfterPrepare"
    finally:
        repository.close()


def test_uncertain_delivery_requires_explicit_resolution(tmp_path) -> None:
    repository = SqliteReminderRepository(tmp_path / "schedule.sqlite3")
    repository.open()
    try:
        add_meeting(repository)
        repository.schedule(Reminder("r1", "m1", "resolve-retry", NOW, None))
        claim = repository.claim_due(NOW, timedelta(minutes=1))[0]
        assert repository.prepare_delivery("r1", claim.claim_token, NOW)
        assert repository.mark_delivery_uncertain("r1", claim.claim_token, NOW, "ResponseUnknown")
        assert repository.resolve_delivery_intent("r1", DeliveryResolution.RETRY, NOW)
        assert repository.status_of("r1") is ReminderStatus.PENDING
        assert repository.get_delivery_intent("r1") is None

        retry = repository.claim_due(NOW, timedelta(minutes=1))[0]
        assert repository.prepare_delivery("r1", retry.claim_token, NOW)
        assert repository.mark_delivery_uncertain("r1", retry.claim_token, NOW, "FinalizeUnknown")
        assert repository.resolve_delivery_intent("r1", DeliveryResolution.SENT, NOW)
        assert repository.status_of("r1") is ReminderStatus.SENT
        assert repository.get_delivery_intent("r1") is None
        assert not repository.resolve_delivery_intent("r1", DeliveryResolution.RETRY, NOW)
    finally:
        repository.close()


def test_list_and_cancel_are_guild_scoped_and_refuse_in_flight_delivery(tmp_path) -> None:
    repository = SqliteReminderRepository(tmp_path / "schedule.sqlite3")
    repository.open()
    try:
        add_meeting(repository)
        repository.save_meeting(Meeting("m2", 2, 20, 30, "別サーバー", NOW, NOW + timedelta(hours=1), "UTC"))
        assert [item.id for item in repository.list_meetings(1, NOW)] == ["m1"]
        assert not repository.cancel_meeting("m1", 1, 999, can_manage=False)

        repository.schedule(Reminder("r1", "m1", "in-flight", NOW, None))
        claim = repository.claim_due(NOW, timedelta(minutes=1))[0]
        assert not repository.cancel_meeting("m1", 1, 3, can_manage=False)
        assert repository.defer("r1", claim.claim_token, NOW + timedelta(minutes=1), "test")
        assert repository.cancel_meeting("m1", 1, 3, can_manage=False)
        assert repository.get_meeting("m1") is None
        assert repository.list_meetings(1, NOW) == ()
        assert repository.status_of("r1") is ReminderStatus.FAILED
    finally:
        repository.close()


def test_legacy_schema_is_migrated_without_data_loss(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE scheduling_meetings (
            id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL, title TEXT NOT NULL, starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL, timezone TEXT NOT NULL
        );
        CREATE TABLE scheduling_reminders (
            id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES scheduling_meetings(id),
            action_key TEXT NOT NULL UNIQUE, due_at TEXT NOT NULL, target_user_id INTEGER,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT,
            claim_expires_at TEXT, sent_at TEXT, last_error_type TEXT
        );
        """
    )
    connection.execute(
        """INSERT INTO scheduling_meetings
        (id, guild_id, channel_id, creator_id, title, starts_at, ends_at, timezone)
        VALUES ('m1', 1, 2, 3, '旧予定', ?, ?, 'UTC')""",
        (NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat()),
    )
    connection.execute(
        """INSERT INTO scheduling_reminders
        (id, meeting_id, action_key, due_at, status, attempts)
        VALUES ('r1', 'm1', 'legacy', ?, 'pending', 0)""",
        (NOW.isoformat(),),
    )
    connection.commit()
    connection.close()

    repository = SqliteReminderRepository(path)
    repository.open()
    try:
        assert repository.get_meeting("m1") is not None
        claim = repository.claim_due(NOW, timedelta(minutes=1))
        assert [item.reminder.id for item in claim] == ["r1"]
    finally:
        repository.close()
