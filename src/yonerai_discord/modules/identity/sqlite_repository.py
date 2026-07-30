from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import threading

from .models import (
    ChallengeBinding,
    ChallengeIntent,
    ChallengeRecord,
    ClaimedChallenge,
    ChallengePurpose,
    GuildVerificationConfig,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_challenges (
    token_digest TEXT PRIMARY KEY CHECK(length(token_digest) = 64),
    purpose TEXT NOT NULL CHECK(purpose IN ('state', 'verification')),
    guild_id INTEGER NOT NULL CHECK(guild_id > 0),
    user_id INTEGER NOT NULL CHECK(user_id > 0),
    intent TEXT NOT NULL CHECK(intent IN ('verify_member', 'link_account')),
    expires_at_us INTEGER NOT NULL,
    created_at_us INTEGER NOT NULL,
    claim_id TEXT UNIQUE,
    claimed_at_us INTEGER,
    claim_expires_at_us INTEGER,
    finalized_at_us INTEGER
);
CREATE INDEX IF NOT EXISTS identity_challenges_expiry
ON identity_challenges(expires_at_us);
CREATE TABLE IF NOT EXISTS identity_guild_verification (
    guild_id INTEGER PRIMARY KEY CHECK(guild_id > 0),
    verified_role_id INTEGER NOT NULL CHECK(verified_role_id > 0),
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    updated_by INTEGER NOT NULL CHECK(updated_by > 0),
    updated_at_us INTEGER NOT NULL
);
"""


def _to_us(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return int(value.timestamp() * 1_000_000)


def _from_us(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000, tz=UTC)


class SQLiteChallengeRepository:
    """再起動と複数process間競合に耐えるSQLite challenge repository。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=5,
                isolation_level=None,
                check_same_thread=False,
            )
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

    def add(self, record: ChallengeRecord) -> None:
        try:
            with self._lock, self._transaction() as connection:
                connection.execute(
                    """INSERT INTO identity_challenges(
                    token_digest,purpose,guild_id,user_id,intent,expires_at_us,created_at_us,
                    claim_id,claimed_at_us,claim_expires_at_us,finalized_at_us
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record.token_digest,
                        record.purpose.value,
                        record.binding.guild_id,
                        record.binding.user_id,
                        record.binding.intent.value,
                        _to_us(record.expires_at),
                        _to_us(record.created_at),
                        record.claim_id,
                        _to_us(record.claimed_at) if record.claimed_at else None,
                        _to_us(record.claim_expires_at) if record.claim_expires_at else None,
                        _to_us(record.finalized_at) if record.finalized_at else None,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("challenge digest already exists") from exc

    def eligible(
        self,
        token_digest: str,
        purpose: ChallengePurpose,
        intent: ChallengeIntent,
        now: datetime,
    ) -> bool:
        now_us = _to_us(now)
        with self._lock:
            row = (
                self._required()
                .execute(
                    """SELECT 1 FROM identity_challenges
                WHERE token_digest=? AND purpose=? AND intent=?
                AND finalized_at_us IS NULL AND expires_at_us>?
                AND (claim_id IS NULL OR claim_expires_at_us<=?)""",
                    (token_digest, purpose.value, intent.value, now_us, now_us),
                )
                .fetchone()
            )
        return row is not None

    def claim(
        self,
        token_digest: str,
        purpose: ChallengePurpose,
        binding: ChallengeBinding,
        claim_id: str,
        now: datetime,
    ) -> ClaimedChallenge | None:
        return self._claim(
            token_digest,
            purpose,
            binding.intent,
            claim_id,
            now,
            lease_seconds=120,
            expected_binding=binding,
        )

    def claim_for_intent(
        self,
        token_digest: str,
        purpose: ChallengePurpose,
        intent: ChallengeIntent,
        claim_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedChallenge | None:
        return self._claim(
            token_digest,
            purpose,
            intent,
            claim_id,
            now,
            lease_seconds=lease_seconds,
            expected_binding=None,
        )

    def _claim(
        self,
        token_digest: str,
        purpose: ChallengePurpose,
        intent: ChallengeIntent,
        claim_id: str,
        now: datetime,
        *,
        lease_seconds: int,
        expected_binding: ChallengeBinding | None,
    ) -> ClaimedChallenge | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now_us = _to_us(now)
        lease_us = _to_us(now + timedelta(seconds=lease_seconds))
        with self._lock, self._transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT purpose,guild_id,user_id,intent,expires_at_us
                FROM identity_challenges
                WHERE token_digest=? AND purpose=? AND intent=? AND finalized_at_us IS NULL
                AND expires_at_us>? AND (claim_id IS NULL OR claim_expires_at_us<=?)""",
                (token_digest, purpose.value, intent.value, now_us, now_us),
            ).fetchone()
            if row is None:
                return None
            binding = ChallengeBinding(
                guild_id=int(row["guild_id"]),
                user_id=int(row["user_id"]),
                intent=ChallengeIntent(str(row["intent"])),
            )
            if expected_binding is not None and binding != expected_binding:
                return None
            updated = connection.execute(
                """UPDATE identity_challenges
                SET claim_id=?,claimed_at_us=?,claim_expires_at_us=?
                WHERE token_digest=? AND finalized_at_us IS NULL AND expires_at_us>?
                AND (claim_id IS NULL OR claim_expires_at_us<=?)""",
                (claim_id, now_us, lease_us, token_digest, now_us, now_us),
            )
            if updated.rowcount != 1:
                return None
            return ClaimedChallenge(
                claim_id=claim_id,
                purpose=ChallengePurpose(str(row["purpose"])),
                binding=binding,
                expires_at=_from_us(int(row["expires_at_us"])),
            )

    def finalize(self, claim_id: str, now: datetime) -> bool:
        with self._lock, self._transaction(immediate=True) as connection:
            return (
                connection.execute(
                    """UPDATE identity_challenges SET finalized_at_us=?
                WHERE claim_id=? AND finalized_at_us IS NULL""",
                    (_to_us(now), claim_id),
                ).rowcount
                == 1
            )

    def release(self, claim_id: str) -> bool:
        with self._lock, self._transaction(immediate=True) as connection:
            return (
                connection.execute(
                    """UPDATE identity_challenges
                SET claim_id=NULL,claimed_at_us=NULL,claim_expires_at_us=NULL
                WHERE claim_id=? AND finalized_at_us IS NULL""",
                    (claim_id,),
                ).rowcount
                == 1
            )

    def purge_expired(self, now: datetime) -> int:
        now_us = _to_us(now)
        with self._lock, self._transaction(immediate=True) as connection:
            return connection.execute(
                """DELETE FROM identity_challenges WHERE expires_at_us<=?
                AND (claim_id IS NULL OR claim_expires_at_us<=?)""",
                (now_us, now_us),
            ).rowcount

    def revoke_unclaimed(self, binding: ChallengeBinding, now: datetime) -> int:
        with self._lock, self._transaction(immediate=True) as connection:
            return connection.execute(
                """DELETE FROM identity_challenges
                WHERE guild_id=? AND user_id=? AND intent=?
                AND claim_id IS NULL AND finalized_at_us IS NULL""",
                (binding.guild_id, binding.user_id, binding.intent.value),
            ).rowcount

    def configure_guild(
        self,
        guild_id: int,
        verified_role_id: int,
        enabled: bool,
        updated_by: int,
        *,
        now: datetime | None = None,
    ) -> GuildVerificationConfig:
        updated_at = now or datetime.now(UTC)
        config = GuildVerificationConfig(
            guild_id=guild_id,
            verified_role_id=verified_role_id,
            enabled=enabled,
            updated_by=updated_by,
            updated_at=updated_at,
        )
        with self._lock, self._transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO identity_guild_verification(
                guild_id,verified_role_id,enabled,updated_by,updated_at_us
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                verified_role_id=excluded.verified_role_id,
                enabled=excluded.enabled,
                updated_by=excluded.updated_by,
                updated_at_us=excluded.updated_at_us""",
                (guild_id, verified_role_id, int(enabled), updated_by, _to_us(updated_at)),
            )
        return config

    def guild_config(self, guild_id: int) -> GuildVerificationConfig | None:
        with self._lock:
            row = (
                self._required()
                .execute(
                    """SELECT guild_id,verified_role_id,enabled,updated_by,updated_at_us
                FROM identity_guild_verification WHERE guild_id=?""",
                    (guild_id,),
                )
                .fetchone()
            )
        if row is None:
            return None
        return GuildVerificationConfig(
            guild_id=int(row["guild_id"]),
            verified_role_id=int(row["verified_role_id"]),
            enabled=bool(row["enabled"]),
            updated_by=int(row["updated_by"]),
            updated_at=_from_us(int(row["updated_at_us"])),
        )

    def snapshot(self) -> tuple[ChallengeRecord, ...]:
        """テスト・診断用。token plaintextはschema上存在しない。"""
        with self._lock:
            rows = (
                self._required()
                .execute(
                    """SELECT token_digest,purpose,guild_id,user_id,intent,expires_at_us,
                created_at_us,claim_id,claimed_at_us,claim_expires_at_us,finalized_at_us
                FROM identity_challenges ORDER BY created_at_us"""
                )
                .fetchall()
            )
        return tuple(self._record(row) for row in rows)

    @staticmethod
    def _record(row: sqlite3.Row) -> ChallengeRecord:
        return ChallengeRecord(
            token_digest=str(row["token_digest"]),
            purpose=ChallengePurpose(str(row["purpose"])),
            binding=ChallengeBinding(
                guild_id=int(row["guild_id"]),
                user_id=int(row["user_id"]),
                intent=ChallengeIntent(str(row["intent"])),
            ),
            expires_at=_from_us(int(row["expires_at_us"])),
            created_at=_from_us(int(row["created_at_us"])),
            claim_id=str(row["claim_id"]) if row["claim_id"] is not None else None,
            claimed_at=_from_us(int(row["claimed_at_us"])) if row["claimed_at_us"] is not None else None,
            claim_expires_at=(
                _from_us(int(row["claim_expires_at_us"])) if row["claim_expires_at_us"] is not None else None
            ),
            finalized_at=(_from_us(int(row["finalized_at_us"])) if row["finalized_at_us"] is not None else None),
        )

    class _Transaction:
        def __init__(self, connection: sqlite3.Connection, immediate: bool) -> None:
            self.connection = connection
            self.immediate = immediate

        def __enter__(self) -> sqlite3.Connection:
            self.connection.execute("BEGIN IMMEDIATE" if self.immediate else "BEGIN")
            return self.connection

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()

    def _transaction(self, *, immediate: bool = False) -> _Transaction:
        return self._Transaction(self._required(), immediate)

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("identity repository is not open")
        return self._connection
