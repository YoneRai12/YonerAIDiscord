from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .domain import Poll, PollResult, PollStatus, Suggestion, SuggestionStatus, Ticket, TicketStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS community_tickets (
    id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
    subject TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','closed')),
    channel_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS community_one_open_ticket
ON community_tickets(guild_id, owner_id) WHERE status='open';
CREATE UNIQUE INDEX IF NOT EXISTS community_ticket_channel
ON community_tickets(guild_id, channel_id) WHERE channel_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS community_ticket_participants (
    ticket_id TEXT NOT NULL REFERENCES community_tickets(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL, PRIMARY KEY(ticket_id, user_id)
);
CREATE TABLE IF NOT EXISTS community_polls (
    id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, creator_id INTEGER NOT NULL,
    question TEXT NOT NULL, options_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','closed')),
    channel_id INTEGER, message_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS community_poll_votes (
    poll_id TEXT NOT NULL REFERENCES community_polls(id) ON DELETE CASCADE,
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, option_index INTEGER NOT NULL,
    voted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(poll_id, user_id)
);
CREATE TABLE IF NOT EXISTS community_suggestions (
    id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, author_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','implemented')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS community_selfroles (
    guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL,
    added_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(guild_id, role_id)
);
CREATE TABLE IF NOT EXISTS community_selfrole_panels (
    guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    PRIMARY KEY(guild_id, message_id)
);
"""


class CommunityRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.executescript(SCHEMA)
            except sqlite3.Error:
                connection.close()
                raise
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def create_ticket(self, ticket: Ticket) -> bool:
        connection = self._required()
        try:
            with self._lock, connection:
                connection.execute(
                    "INSERT INTO community_tickets(id,guild_id,owner_id,subject,status,channel_id) VALUES(?,?,?,?,?,?)",
                    (
                        ticket.id,
                        ticket.guild_id,
                        ticket.owner_id,
                        ticket.subject,
                        ticket.status.value,
                        ticket.channel_id,
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def bind_ticket_channel(self, guild_id: int, ticket_id: str, channel_id: int) -> bool:
        with self._lock, self._required() as connection:
            cursor = connection.execute(
                "UPDATE community_tickets SET channel_id=? WHERE guild_id=? AND id=? AND status='open'",
                (channel_id, guild_id, ticket_id),
            )
            return cursor.rowcount == 1

    def delete_unbound_ticket(self, guild_id: int, ticket_id: str) -> None:
        with self._lock, self._required() as connection:
            connection.execute(
                "DELETE FROM community_tickets WHERE guild_id=? AND id=? AND channel_id IS NULL", (guild_id, ticket_id)
            )

    def ticket_by_channel(self, guild_id: int, channel_id: int) -> Ticket | None:
        with self._lock:
            row = (
                self._required()
                .execute("SELECT * FROM community_tickets WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
                .fetchone()
            )
        return self._ticket(row) if row else None

    def ticket_participant_ids(self, guild_id: int, ticket_id: str) -> tuple[int, ...]:
        """Return current participants only while the ticket is still open."""
        with self._lock:
            rows = (
                self._required()
                .execute(
                    """SELECT participant.user_id
                    FROM community_ticket_participants AS participant
                    JOIN community_tickets AS ticket ON ticket.id=participant.ticket_id
                    WHERE ticket.guild_id=? AND ticket.id=? AND ticket.status='open'
                    ORDER BY participant.user_id""",
                    (guild_id, ticket_id),
                )
                .fetchall()
            )
        return tuple(int(row[0]) for row in rows)

    def close_ticket(self, guild_id: int, ticket_id: str) -> bool:
        with self._lock, self._required() as connection:
            cursor = connection.execute(
                """UPDATE community_tickets SET status='closed', closed_at=CURRENT_TIMESTAMP
                WHERE guild_id=? AND id=? AND status='open'""",
                (guild_id, ticket_id),
            )
            return cursor.rowcount == 1

    def add_ticket_participant(self, guild_id: int, ticket_id: str, user_id: int) -> bool:
        with self._lock, self._required() as connection:
            ticket = connection.execute(
                "SELECT id FROM community_tickets WHERE guild_id=? AND id=? AND status='open'", (guild_id, ticket_id)
            ).fetchone()
            if ticket is None:
                return False
            return (
                connection.execute(
                    "INSERT OR IGNORE INTO community_ticket_participants(ticket_id,user_id) VALUES(?,?)",
                    (ticket_id, user_id),
                ).rowcount
                == 1
            )

    def has_ticket_participant(self, guild_id: int, ticket_id: str, user_id: int) -> bool:
        with self._lock:
            return (
                self._required()
                .execute(
                    """SELECT 1 FROM community_ticket_participants AS participant
                JOIN community_tickets AS ticket ON ticket.id=participant.ticket_id
                WHERE ticket.guild_id=? AND ticket.id=? AND ticket.status='open' AND participant.user_id=?""",
                    (guild_id, ticket_id, user_id),
                )
                .fetchone()
                is not None
            )

    def remove_ticket_participant(self, guild_id: int, ticket_id: str, user_id: int) -> bool:
        with self._lock, self._required() as connection:
            cursor = connection.execute(
                """DELETE FROM community_ticket_participants WHERE ticket_id=? AND user_id=?
                AND EXISTS(SELECT 1 FROM community_tickets WHERE id=? AND guild_id=? AND status='open')""",
                (ticket_id, user_id, ticket_id, guild_id),
            )
            return cursor.rowcount == 1

    def create_poll(self, poll: Poll) -> None:
        with self._lock, self._required() as connection:
            connection.execute(
                """INSERT INTO community_polls
                (id,guild_id,creator_id,question,options_json,status,channel_id,message_id)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    poll.id,
                    poll.guild_id,
                    poll.creator_id,
                    poll.question,
                    json.dumps(poll.options, ensure_ascii=False),
                    poll.status.value,
                    poll.channel_id,
                    poll.message_id,
                ),
            )

    def bind_poll_message(self, guild_id: int, poll_id: str, channel_id: int, message_id: int) -> bool:
        with self._lock, self._required() as connection:
            return (
                connection.execute(
                    "UPDATE community_polls SET channel_id=?,message_id=? WHERE guild_id=? AND id=? AND status='open'",
                    (channel_id, message_id, guild_id, poll_id),
                ).rowcount
                == 1
            )

    def get_poll(self, guild_id: int, poll_id: str) -> Poll | None:
        with self._lock:
            row = (
                self._required()
                .execute("SELECT * FROM community_polls WHERE guild_id=? AND id=?", (guild_id, poll_id))
                .fetchone()
            )
        return self._poll(row) if row else None

    def open_polls(self) -> tuple[Poll, ...]:
        with self._lock:
            rows = (
                self._required()
                .execute("SELECT * FROM community_polls WHERE status='open' AND message_id IS NOT NULL")
                .fetchall()
            )
        return tuple(self._poll(row) for row in rows)

    def vote(self, guild_id: int, poll_id: str, user_id: int, option_index: int) -> bool:
        try:
            with self._lock, self._required() as connection:
                row = connection.execute(
                    "SELECT * FROM community_polls WHERE guild_id=? AND id=? AND status='open'", (guild_id, poll_id)
                ).fetchone()
                if row is None:
                    return False
                poll = self._poll(row)
                if not 0 <= option_index < len(poll.options):
                    return False
                connection.execute(
                    "INSERT INTO community_poll_votes(poll_id,guild_id,user_id,option_index) VALUES(?,?,?,?)",
                    (poll_id, guild_id, user_id, option_index),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def close_poll(self, guild_id: int, poll_id: str) -> bool:
        with self._lock, self._required() as connection:
            return (
                connection.execute(
                    """UPDATE community_polls SET status='closed',closed_at=CURRENT_TIMESTAMP
                WHERE guild_id=? AND id=? AND status='open'""",
                    (guild_id, poll_id),
                ).rowcount
                == 1
            )

    def poll_results(self, guild_id: int, poll_id: str) -> tuple[PollResult, ...]:
        with self._lock:
            row = (
                self._required()
                .execute("SELECT * FROM community_polls WHERE guild_id=? AND id=?", (guild_id, poll_id))
                .fetchone()
            )
            if row is None:
                return ()
            poll = self._poll(row)
            counts = dict(
                self._required()
                .execute(
                    """SELECT option_index,COUNT(*) FROM community_poll_votes
                    WHERE guild_id=? AND poll_id=? GROUP BY option_index""",
                    (guild_id, poll_id),
                )
                .fetchall()
            )
        return tuple(PollResult(index, option, int(counts.get(index, 0))) for index, option in enumerate(poll.options))

    def create_suggestion(self, suggestion: Suggestion) -> None:
        with self._lock, self._required() as connection:
            connection.execute(
                "INSERT INTO community_suggestions(id,guild_id,author_id,content,status) VALUES(?,?,?,?,?)",
                (suggestion.id, suggestion.guild_id, suggestion.author_id, suggestion.content, suggestion.status.value),
            )

    def get_suggestion(self, guild_id: int, suggestion_id: str) -> Suggestion | None:
        with self._lock:
            row = (
                self._required()
                .execute("SELECT * FROM community_suggestions WHERE guild_id=? AND id=?", (guild_id, suggestion_id))
                .fetchone()
            )
        return (
            Suggestion(row["id"], row["guild_id"], row["author_id"], row["content"], SuggestionStatus(row["status"]))
            if row
            else None
        )

    def update_suggestion(self, guild_id: int, suggestion_id: str, status: SuggestionStatus) -> bool:
        with self._lock, self._required() as connection:
            return (
                connection.execute(
                    "UPDATE community_suggestions SET status=?,updated_at=CURRENT_TIMESTAMP WHERE guild_id=? AND id=?",
                    (status.value, guild_id, suggestion_id),
                ).rowcount
                == 1
            )

    def add_selfrole(self, guild_id: int, role_id: int, actor_id: int) -> bool:
        with self._lock, self._required() as connection:
            return (
                connection.execute(
                    "INSERT OR IGNORE INTO community_selfroles(guild_id,role_id,added_by) VALUES(?,?,?)",
                    (guild_id, role_id, actor_id),
                ).rowcount
                == 1
            )

    def remove_selfrole(self, guild_id: int, role_id: int) -> bool:
        with self._lock, self._required() as connection:
            return (
                connection.execute(
                    "DELETE FROM community_selfroles WHERE guild_id=? AND role_id=?", (guild_id, role_id)
                ).rowcount
                == 1
            )

    def selfroles(self, guild_id: int) -> tuple[int, ...]:
        with self._lock:
            rows = (
                self._required()
                .execute("SELECT role_id FROM community_selfroles WHERE guild_id=? ORDER BY role_id", (guild_id,))
                .fetchall()
            )
        return tuple(int(row[0]) for row in rows)

    def all_selfrole_sets(self) -> tuple[tuple[int, tuple[int, ...]], ...]:
        with self._lock:
            rows = (
                self._required()
                .execute("SELECT guild_id,role_id FROM community_selfroles ORDER BY guild_id,role_id")
                .fetchall()
            )
        grouped: dict[int, list[int]] = {}
        for row in rows:
            grouped.setdefault(int(row["guild_id"]), []).append(int(row["role_id"]))
        return tuple((guild_id, tuple(role_ids)) for guild_id, role_ids in grouped.items())

    @staticmethod
    def _ticket(row: sqlite3.Row) -> Ticket:
        return Ticket(
            row["id"], row["guild_id"], row["owner_id"], row["subject"], TicketStatus(row["status"]), row["channel_id"]
        )

    @staticmethod
    def _poll(row: sqlite3.Row) -> Poll:
        return Poll(
            row["id"],
            row["guild_id"],
            row["creator_id"],
            row["question"],
            tuple(json.loads(row["options_json"])),
            PollStatus(row["status"]),
            row["channel_id"],
            row["message_id"],
        )

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("community repository is not open")
        return self._connection
