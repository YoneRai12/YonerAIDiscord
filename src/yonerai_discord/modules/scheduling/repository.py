from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .domain import (
    ClaimedReminder,
    DeliveryIntent,
    DeliveryIntentState,
    DeliveryResolution,
    Meeting,
    NotificationPreferences,
    RSVP,
    Reminder,
    ReminderStatus,
    as_utc,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduling_meetings (
    id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    creator_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    cancelled_at TEXT,
    cancelled_by INTEGER
);
CREATE TABLE IF NOT EXISTS scheduling_rsvps (
    meeting_id TEXT NOT NULL REFERENCES scheduling_meetings(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('attending', 'tentative', 'declined')),
    responded_at TEXT NOT NULL,
    PRIMARY KEY(meeting_id, user_id)
);
CREATE TABLE IF NOT EXISTS scheduling_notification_preferences (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    direct_message INTEGER NOT NULL CHECK(direct_message IN (0, 1)),
    timezone TEXT NOT NULL,
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS scheduling_reminders (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES scheduling_meetings(id) ON DELETE CASCADE,
    action_key TEXT NOT NULL UNIQUE,
    due_at TEXT NOT NULL,
    target_user_id INTEGER,
    status TEXT NOT NULL CHECK(status IN ('pending', 'claimed', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT,
    claim_expires_at TEXT,
    sent_at TEXT,
    last_error_type TEXT,
    not_before TEXT
);
CREATE TABLE IF NOT EXISTS scheduling_delivery_intents (
    reminder_id TEXT PRIMARY KEY REFERENCES scheduling_reminders(id) ON DELETE CASCADE,
    claim_token TEXT NOT NULL,
    action_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('prepared', 'uncertain')),
    prepared_at TEXT NOT NULL,
    uncertain_at TEXT,
    last_error_type TEXT
);
"""


INDEXES = """
CREATE INDEX IF NOT EXISTS scheduling_reminders_due_idx
ON scheduling_reminders(status, not_before, due_at, claim_expires_at);
CREATE INDEX IF NOT EXISTS scheduling_delivery_intents_state_idx
ON scheduling_delivery_intents(state, prepared_at);
"""


def _timestamp(value: datetime) -> str:
    return as_utc(value).isoformat(timespec="microseconds")


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC)


class SqliteReminderRepository:
    """SQLiteを唯一の状態源としてclaimを直列化するrepository。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False, isolation_level=None)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(SCHEMA)
                self._migrate_schema(connection)
                connection.executescript(INDEXES)
            except BaseException:
                connection.close()
                raise
            self._connection = connection

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        """旧SQLiteを破壊せず、送信保留と取消メタデータを追加する。"""

        meeting_columns = {row["name"] for row in connection.execute("PRAGMA table_info(scheduling_meetings)")}
        reminder_columns = {row["name"] for row in connection.execute("PRAGMA table_info(scheduling_reminders)")}
        with connection:
            if "cancelled_at" not in meeting_columns:
                connection.execute("ALTER TABLE scheduling_meetings ADD COLUMN cancelled_at TEXT")
            if "cancelled_by" not in meeting_columns:
                connection.execute("ALTER TABLE scheduling_meetings ADD COLUMN cancelled_by INTEGER")
            if "not_before" not in reminder_columns:
                connection.execute("ALTER TABLE scheduling_reminders ADD COLUMN not_before TEXT")
            connection.execute("UPDATE scheduling_reminders SET not_before=due_at WHERE not_before IS NULL")

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def save_meeting(self, meeting: Meeting) -> bool:
        connection = self._connection_required()
        with self._lock, connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO scheduling_meetings
                (id, guild_id, channel_id, creator_id, title, starts_at, ends_at, timezone)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    meeting.id,
                    meeting.guild_id,
                    meeting.channel_id,
                    meeting.creator_id,
                    meeting.title,
                    _timestamp(meeting.starts_at),
                    _timestamp(meeting.ends_at),
                    meeting.timezone,
                ),
            )
            return cursor.rowcount == 1

    def get_meeting(self, meeting_id: str) -> Meeting | None:
        with self._lock:
            row = (
                self._connection_required()
                .execute(
                    "SELECT * FROM scheduling_meetings WHERE id = ? AND cancelled_at IS NULL",
                    (meeting_id,),
                )
                .fetchone()
            )
        if row is None:
            return None
        return self._meeting_from_row(row)

    def list_meetings(
        self,
        guild_id: int,
        now: datetime,
        limit: int = 10,
    ) -> tuple[Meeting, ...]:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25")
        with self._lock:
            rows = (
                self._connection_required()
                .execute(
                    """SELECT * FROM scheduling_meetings
                WHERE guild_id=? AND cancelled_at IS NULL AND ends_at>=?
                ORDER BY starts_at, id LIMIT ?""",
                    (guild_id, _timestamp(now), limit),
                )
                .fetchall()
            )
        return tuple(self._meeting_from_row(row) for row in rows)

    def cancel_meeting(
        self,
        meeting_id: str,
        guild_id: int,
        actor_id: int,
        *,
        can_manage: bool,
    ) -> bool:
        """作成者/管理者だけが、送信処理中でない予定を原子的に取り消す。"""

        if not meeting_id.strip() or guild_id <= 0 or actor_id <= 0:
            return False
        connection = self._connection_required()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """SELECT creator_id FROM scheduling_meetings
                    WHERE id=? AND guild_id=? AND cancelled_at IS NULL""",
                    (meeting_id, guild_id),
                ).fetchone()
                if row is None or (row["creator_id"] != actor_id and not can_manage):
                    connection.rollback()
                    return False
                in_flight = connection.execute(
                    """SELECT 1 FROM scheduling_reminders r
                    LEFT JOIN scheduling_delivery_intents i ON i.reminder_id=r.id
                    WHERE r.meeting_id=? AND (r.status='claimed' OR i.reminder_id IS NOT NULL)
                    LIMIT 1""",
                    (meeting_id,),
                ).fetchone()
                if in_flight is not None:
                    connection.rollback()
                    return False
                changed = connection.execute(
                    """UPDATE scheduling_meetings SET cancelled_at=?, cancelled_by=?
                    WHERE id=? AND guild_id=? AND cancelled_at IS NULL""",
                    (_timestamp(datetime.now(UTC)), actor_id, meeting_id, guild_id),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    return False
                connection.execute(
                    """UPDATE scheduling_reminders SET status='failed', claim_token=NULL,
                    claim_expires_at=NULL, last_error_type='MeetingCancelled'
                    WHERE meeting_id=? AND status='pending'""",
                    (meeting_id,),
                )
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def save_rsvp(self, rsvp: RSVP) -> None:
        connection = self._connection_required()
        with self._lock, connection:
            connection.execute(
                """INSERT INTO scheduling_rsvps(meeting_id, user_id, status, responded_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(meeting_id, user_id) DO UPDATE SET status=excluded.status, responded_at=excluded.responded_at""",
                (rsvp.meeting_id, rsvp.user_id, rsvp.status.value, _timestamp(rsvp.responded_at)),
            )

    def save_preferences(self, preferences: NotificationPreferences) -> None:
        connection = self._connection_required()
        with self._lock, connection:
            connection.execute(
                """INSERT INTO scheduling_notification_preferences
                (guild_id, user_id, enabled, direct_message, timezone) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET enabled=excluded.enabled,
                direct_message=excluded.direct_message, timezone=excluded.timezone""",
                (
                    preferences.guild_id,
                    preferences.user_id,
                    int(preferences.enabled),
                    int(preferences.direct_message),
                    preferences.timezone,
                ),
            )

    def get_preferences(self, guild_id: int, user_id: int) -> NotificationPreferences | None:
        with self._lock:
            row = (
                self._connection_required()
                .execute(
                    """SELECT guild_id, user_id, enabled, direct_message, timezone
                FROM scheduling_notification_preferences WHERE guild_id=? AND user_id=?""",
                    (guild_id, user_id),
                )
                .fetchone()
            )
        if row is None:
            return None
        return NotificationPreferences(
            guild_id=row["guild_id"],
            user_id=row["user_id"],
            enabled=bool(row["enabled"]),
            direct_message=bool(row["direct_message"]),
            timezone=row["timezone"],
        )

    def schedule(self, reminder: Reminder) -> bool:
        connection = self._connection_required()
        with self._lock, connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO scheduling_reminders
                (id, meeting_id, action_key, due_at, target_user_id, status, attempts, not_before)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    reminder.id,
                    reminder.meeting_id,
                    reminder.action_key,
                    _timestamp(reminder.due_at),
                    reminder.target_user_id,
                    reminder.status.value,
                    reminder.attempts,
                    _timestamp(reminder.due_at),
                ),
            )
            return cursor.rowcount == 1

    def claim_due(self, now: datetime, lease: timedelta, limit: int = 50) -> tuple[ClaimedReminder, ...]:
        now = as_utc(now, "now")
        if lease <= timedelta(0):
            raise ValueError("lease must be positive")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        connection = self._connection_required()
        now_text = _timestamp(now)
        expires = now + lease
        claims: list[ClaimedReminder] = []
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._promote_expired_intents(connection, now_text)
                rows = connection.execute(
                    """SELECT r.* FROM scheduling_reminders r
                    LEFT JOIN scheduling_delivery_intents i ON i.reminder_id=r.id
                    WHERE i.reminder_id IS NULL AND (
                        (r.status='pending' AND r.due_at<=? AND COALESCE(r.not_before, r.due_at)<=?)
                        OR (r.status='claimed' AND r.claim_expires_at<=?)
                    )
                    ORDER BY r.due_at, r.id LIMIT ?""",
                    (now_text, now_text, now_text, limit),
                ).fetchall()
                for row in rows:
                    token = uuid.uuid4().hex
                    cursor = connection.execute(
                        """UPDATE scheduling_reminders SET status='claimed', claim_token=?, claim_expires_at=?,
                        attempts=attempts+1 WHERE id=? AND
                        ((status='pending' AND due_at<=? AND COALESCE(not_before, due_at)<=?)
                        OR (status='claimed' AND claim_expires_at<=?))
                        AND NOT EXISTS (
                            SELECT 1 FROM scheduling_delivery_intents WHERE reminder_id=?
                        )""",
                        (
                            token,
                            _timestamp(expires),
                            row["id"],
                            now_text,
                            now_text,
                            now_text,
                            row["id"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        continue
                    reminder = Reminder(
                        id=row["id"],
                        meeting_id=row["meeting_id"],
                        action_key=row["action_key"],
                        due_at=_datetime(row["due_at"]),
                        target_user_id=row["target_user_id"],
                        status=ReminderStatus.CLAIMED,
                        attempts=row["attempts"] + 1,
                    )
                    claims.append(ClaimedReminder(reminder, token, expires))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return tuple(claims)

    def mark_sent(self, reminder_id: str, claim_token: str, sent_at: datetime) -> bool:
        return self._finish(reminder_id, claim_token, ReminderStatus.SENT, sent_at=sent_at)

    def mark_failed(self, reminder_id: str, claim_token: str, error_type: str) -> bool:
        return self._finish(reminder_id, claim_token, ReminderStatus.FAILED, error_type=error_type)

    def release(self, reminder_id: str, claim_token: str, error_type: str) -> bool:
        connection = self._connection_required()
        with self._lock, connection:
            cursor = connection.execute(
                """UPDATE scheduling_reminders SET status='pending', claim_token=NULL, claim_expires_at=NULL,
                last_error_type=? WHERE id=? AND status='claimed' AND claim_token=?
                AND NOT EXISTS (
                    SELECT 1 FROM scheduling_delivery_intents WHERE reminder_id=?
                )""",
                (_safe_error_type(error_type), reminder_id, claim_token, reminder_id),
            )
            return cursor.rowcount == 1

    def defer(
        self,
        reminder_id: str,
        claim_token: str,
        not_before: datetime,
        error_type: str,
    ) -> bool:
        connection = self._connection_required()
        with self._lock, connection:
            cursor = connection.execute(
                """UPDATE scheduling_reminders SET status='pending', claim_token=NULL,
                claim_expires_at=NULL, not_before=?, last_error_type=?,
                attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END
                WHERE id=? AND status='claimed' AND claim_token=?
                AND NOT EXISTS (
                    SELECT 1 FROM scheduling_delivery_intents WHERE reminder_id=?
                )""",
                (
                    _timestamp(not_before),
                    _safe_error_type(error_type),
                    reminder_id,
                    claim_token,
                    reminder_id,
                ),
            )
            return cursor.rowcount == 1

    def prepare_delivery(
        self,
        reminder_id: str,
        claim_token: str,
        prepared_at: datetime,
    ) -> bool:
        """外部送信前にintentを書き、これ以後のlease再claimを禁止する。"""

        connection = self._connection_required()
        with self._lock, connection:
            cursor = connection.execute(
                """INSERT INTO scheduling_delivery_intents
                (reminder_id, claim_token, action_key, state, prepared_at)
                SELECT id, claim_token, action_key, 'prepared', ?
                FROM scheduling_reminders
                WHERE id=? AND status='claimed' AND claim_token=?
                AND NOT EXISTS (
                    SELECT 1 FROM scheduling_delivery_intents WHERE reminder_id=?
                )""",
                (_timestamp(prepared_at), reminder_id, claim_token, reminder_id),
            )
            return cursor.rowcount == 1

    def complete_delivery(
        self,
        reminder_id: str,
        claim_token: str,
        sent_at: datetime,
    ) -> bool:
        """送信済み確定とintent削除を1つのSQLite transactionで行う。"""

        connection = self._connection_required()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                intent = connection.execute(
                    """SELECT 1 FROM scheduling_delivery_intents
                    WHERE reminder_id=? AND claim_token=? AND state='prepared'""",
                    (reminder_id, claim_token),
                ).fetchone()
                if intent is None:
                    connection.rollback()
                    return False
                changed = connection.execute(
                    """UPDATE scheduling_reminders SET status='sent', claim_token=NULL,
                    claim_expires_at=NULL, sent_at=?, last_error_type=NULL
                    WHERE id=? AND status='claimed' AND claim_token=?""",
                    (_timestamp(sent_at), reminder_id, claim_token),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    return False
                deleted = connection.execute(
                    """DELETE FROM scheduling_delivery_intents
                    WHERE reminder_id=? AND claim_token=? AND state='prepared'""",
                    (reminder_id, claim_token),
                ).rowcount
                if deleted != 1:
                    connection.rollback()
                    return False
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def mark_delivery_uncertain(
        self,
        reminder_id: str,
        claim_token: str,
        uncertain_at: datetime,
        error_type: str,
    ) -> bool:
        connection = self._connection_required()
        with self._lock, connection:
            cursor = connection.execute(
                """UPDATE scheduling_delivery_intents
                SET state='uncertain', uncertain_at=?, last_error_type=?
                WHERE reminder_id=? AND claim_token=? AND state='prepared'""",
                (
                    _timestamp(uncertain_at),
                    _safe_error_type(error_type),
                    reminder_id,
                    claim_token,
                ),
            )
            return cursor.rowcount == 1

    def release_prepared(
        self,
        reminder_id: str,
        claim_token: str,
        not_before: datetime,
        error_type: str,
    ) -> bool:
        """送信前に失敗したと証明できる場合だけ、再試行へ戻す。"""

        connection = self._connection_required()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                intent = connection.execute(
                    """SELECT 1 FROM scheduling_delivery_intents
                    WHERE reminder_id=? AND claim_token=? AND state='prepared'""",
                    (reminder_id, claim_token),
                ).fetchone()
                if intent is None:
                    connection.rollback()
                    return False
                changed = connection.execute(
                    """UPDATE scheduling_reminders SET status='pending', claim_token=NULL,
                    claim_expires_at=NULL, not_before=?, last_error_type=?,
                    attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END
                    WHERE id=? AND status='claimed' AND claim_token=?""",
                    (
                        _timestamp(not_before),
                        _safe_error_type(error_type),
                        reminder_id,
                        claim_token,
                    ),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    return False
                deleted = connection.execute(
                    """DELETE FROM scheduling_delivery_intents
                    WHERE reminder_id=? AND claim_token=? AND state='prepared'""",
                    (reminder_id, claim_token),
                ).rowcount
                if deleted != 1:
                    connection.rollback()
                    return False
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def list_delivery_intents(
        self,
        *,
        guild_id: int | None = None,
        limit: int = 50,
    ) -> tuple[DeliveryIntent, ...]:
        if guild_id is not None and guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        where = "" if guild_id is None else "WHERE m.guild_id=?"
        parameters: tuple[int, ...] = () if guild_id is None else (guild_id,)
        with self._lock:
            rows = (
                self._connection_required()
                .execute(
                    f"""SELECT i.*, r.meeting_id, m.guild_id
                FROM scheduling_delivery_intents i
                JOIN scheduling_reminders r ON r.id=i.reminder_id
                JOIN scheduling_meetings m ON m.id=r.meeting_id
                {where}
                ORDER BY i.prepared_at, i.reminder_id LIMIT ?""",
                    (*parameters, limit),
                )
                .fetchall()
            )
        return tuple(self._delivery_intent_from_row(row) for row in rows)

    def get_delivery_intent(self, reminder_id: str) -> DeliveryIntent | None:
        with self._lock:
            row = (
                self._connection_required()
                .execute(
                    """SELECT i.*, r.meeting_id, m.guild_id
                FROM scheduling_delivery_intents i
                JOIN scheduling_reminders r ON r.id=i.reminder_id
                JOIN scheduling_meetings m ON m.id=r.meeting_id
                WHERE i.reminder_id=?""",
                    (reminder_id,),
                )
                .fetchone()
            )
        return None if row is None else self._delivery_intent_from_row(row)

    def resolve_delivery_intent(
        self,
        reminder_id: str,
        resolution: DeliveryResolution,
        resolved_at: datetime,
    ) -> bool:
        """UNCERTAINだけを運用者確認後にretry/sentで解決する。"""

        if not isinstance(resolution, DeliveryResolution):
            raise TypeError("resolution must be DeliveryResolution")
        connection = self._connection_required()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                intent = connection.execute(
                    """SELECT claim_token FROM scheduling_delivery_intents
                    WHERE reminder_id=? AND state='uncertain'""",
                    (reminder_id,),
                ).fetchone()
                if intent is None:
                    connection.rollback()
                    return False
                if resolution is DeliveryResolution.SENT:
                    changed = connection.execute(
                        """UPDATE scheduling_reminders SET status='sent', claim_token=NULL,
                        claim_expires_at=NULL, sent_at=?, last_error_type='OperatorConfirmedSent'
                        WHERE id=? AND status='claimed' AND claim_token=?""",
                        (_timestamp(resolved_at), reminder_id, intent["claim_token"]),
                    ).rowcount
                else:
                    changed = connection.execute(
                        """UPDATE scheduling_reminders SET status='pending', claim_token=NULL,
                        claim_expires_at=NULL, not_before=?, last_error_type='OperatorConfirmedRetry'
                        WHERE id=? AND status='claimed' AND claim_token=?""",
                        (_timestamp(resolved_at), reminder_id, intent["claim_token"]),
                    ).rowcount
                if changed != 1:
                    connection.rollback()
                    return False
                deleted = connection.execute(
                    "DELETE FROM scheduling_delivery_intents WHERE reminder_id=? AND state='uncertain'",
                    (reminder_id,),
                ).rowcount
                if deleted != 1:
                    connection.rollback()
                    return False
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def _finish(
        self,
        reminder_id: str,
        claim_token: str,
        status: ReminderStatus,
        *,
        sent_at: datetime | None = None,
        error_type: str | None = None,
    ) -> bool:
        connection = self._connection_required()
        with self._lock, connection:
            cursor = connection.execute(
                """UPDATE scheduling_reminders SET status=?, claim_token=NULL, claim_expires_at=NULL,
                sent_at=?, last_error_type=? WHERE id=? AND status='claimed' AND claim_token=?
                AND NOT EXISTS (
                    SELECT 1 FROM scheduling_delivery_intents WHERE reminder_id=?
                )""",
                (
                    status.value,
                    _timestamp(sent_at) if sent_at is not None else None,
                    _safe_error_type(error_type) if error_type else None,
                    reminder_id,
                    claim_token,
                    reminder_id,
                ),
            )
            return cursor.rowcount == 1

    def status_of(self, reminder_id: str) -> ReminderStatus | None:
        with self._lock:
            row = (
                self._connection_required()
                .execute("SELECT status FROM scheduling_reminders WHERE id=?", (reminder_id,))
                .fetchone()
            )
        return ReminderStatus(row[0]) if row else None

    @staticmethod
    def _promote_expired_intents(connection: sqlite3.Connection, now_text: str) -> None:
        connection.execute(
            """UPDATE scheduling_delivery_intents
            SET state='uncertain', uncertain_at=COALESCE(uncertain_at, ?),
                last_error_type=COALESCE(last_error_type, 'LeaseExpiredAfterPrepare')
            WHERE state='prepared' AND EXISTS (
                SELECT 1 FROM scheduling_reminders r
                WHERE r.id=scheduling_delivery_intents.reminder_id
                AND r.status='claimed' AND r.claim_expires_at<=?
            )""",
            (now_text, now_text),
        )

    @staticmethod
    def _meeting_from_row(row: sqlite3.Row) -> Meeting:
        return Meeting(
            id=row["id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            creator_id=row["creator_id"],
            title=row["title"],
            starts_at=_datetime(row["starts_at"]),
            ends_at=_datetime(row["ends_at"]),
            timezone=row["timezone"],
        )

    @staticmethod
    def _delivery_intent_from_row(row: sqlite3.Row) -> DeliveryIntent:
        return DeliveryIntent(
            reminder_id=row["reminder_id"],
            meeting_id=row["meeting_id"],
            guild_id=row["guild_id"],
            action_key=row["action_key"],
            claim_token=row["claim_token"],
            state=DeliveryIntentState(row["state"]),
            prepared_at=_datetime(row["prepared_at"]),
            uncertain_at=(_datetime(row["uncertain_at"]) if row["uncertain_at"] is not None else None),
            error_type=row["last_error_type"],
        )

    def _connection_required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("scheduling repository is not open")
        return self._connection


def _safe_error_type(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else type(value).__name__
    return (normalized or "UnknownError")[:100]
