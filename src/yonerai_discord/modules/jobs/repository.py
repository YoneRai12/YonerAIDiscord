from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from .domain import Claim, Job, JobStatus, Lease, Receipt, Revision


SCHEMA = """
CREATE TABLE IF NOT EXISTS durable_jobs (
    id TEXT PRIMARY KEY,
    action_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    guild_id INTEGER,
    status TEXT NOT NULL CHECK(status IN ('pending','claimed','succeeded','failed','uncertain','skipped')),
    available_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts >= 1),
    claim_token TEXT,
    claim_expires_at TEXT,
    execution_started_at TEXT,
    finished_at TEXT,
    last_error_type TEXT,
    outcome_detail TEXT,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(action_key, revision)
);
CREATE INDEX IF NOT EXISTS durable_jobs_claim_idx
ON durable_jobs(status, available_at, claim_expires_at);
CREATE TABLE IF NOT EXISTS durable_job_attempts (
    job_id TEXT NOT NULL REFERENCES durable_jobs(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    claim_token TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    execution_started_at TEXT,
    finished_at TEXT,
    outcome TEXT,
    error_type TEXT,
    receipt_json TEXT,
    PRIMARY KEY(job_id, attempt_number)
);
"""


@dataclass(frozen=True, slots=True)
class JobSummary:
    """運用表示用の非秘密view。payload/receipt/error詳細を持たない。"""

    id: str
    kind: str
    guild_id: int | None
    status: JobStatus
    available_at: datetime
    attempts: int
    max_attempts: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class SqliteJobRepository:
    """core DBと同じpathへ、専用connectionでatomic claimを提供する。"""

    def __init__(
        self,
        path: Path,
        *,
        connect: Callable[..., sqlite3.Connection] = sqlite3.connect,
    ) -> None:
        self.path = Path(path)
        self._connect = connect
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect(self.path, timeout=5, check_same_thread=False, isolation_level=None)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(SCHEMA)
                columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(durable_jobs)").fetchall()}
                if "guild_id" not in columns:
                    connection.execute("ALTER TABLE durable_jobs ADD COLUMN guild_id INTEGER")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS durable_jobs_guild_status_idx "
                    "ON durable_jobs(guild_id, status, available_at)"
                )
            except BaseException:
                connection.close()
                raise
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def enqueue(self, job: Job, now: datetime | None = None) -> bool:
        if job.status is not JobStatus.PENDING or job.attempts != 0:
            raise ValueError("new jobs must be pending with zero attempts")
        connection = self._required()
        created = _utc(now or datetime.now(UTC))
        payload_json = _json(dict(job.payload))
        if len(payload_json.encode("utf-8")) > 16_384:
            raise ValueError("payload must encode to at most 16384 bytes")
        with self._lock:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO durable_jobs
                (id, action_key, revision, kind, payload_json, guild_id, status, available_at, attempts,
                 max_attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.id,
                    job.action_key,
                    job.revision.value,
                    job.kind,
                    payload_json,
                    job.guild_id,
                    job.status.value,
                    _timestamp(job.available_at),
                    job.attempts,
                    job.max_attempts,
                    _timestamp(created),
                    _timestamp(created),
                ),
            )
            return cursor.rowcount == 1

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._required().execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row is not None else None

    def get_by_action(self, action_key: str, revision: Revision) -> Job | None:
        with self._lock:
            row = (
                self._required()
                .execute(
                    "SELECT * FROM durable_jobs WHERE action_key=? AND revision=?",
                    (action_key, revision.value),
                )
                .fetchone()
            )
        return self._job(row) if row is not None else None

    def status_of(self, job_id: str) -> JobStatus | None:
        with self._lock:
            row = self._required().execute("SELECT status FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
        return JobStatus(row["status"]) if row else None

    def list_summaries(
        self,
        *,
        guild_id: int | None = None,
        status: JobStatus | None = None,
        limit: int = 25,
        allow_global: bool = False,
    ) -> tuple[JobSummary, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        where: list[str] = []
        params: list[object] = []
        if not allow_global:
            if guild_id is None:
                raise ValueError("guild_id is required outside global owner scope")
            where.append("guild_id=?")
            params.append(guild_id)
        if status is not None:
            where.append("status=?")
            params.append(status.value)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        with self._lock:
            rows = (
                self._required()
                .execute(
                    f"""SELECT id, kind, guild_id, status, available_at, attempts, max_attempts
                FROM durable_jobs {clause} ORDER BY created_at DESC, id DESC LIMIT ?""",  # noqa: S608
                    tuple(params),
                )
                .fetchall()
            )
        return tuple(
            JobSummary(
                id=str(row["id"]),
                kind=str(row["kind"]),
                guild_id=int(row["guild_id"]) if row["guild_id"] is not None else None,
                status=JobStatus(str(row["status"])),
                available_at=_datetime(str(row["available_at"])),
                attempts=int(row["attempts"]),
                max_attempts=int(row["max_attempts"]),
            )
            for row in rows
        )

    def count_by_status(
        self,
        *,
        guild_id: int | None = None,
        allow_global: bool = False,
    ) -> dict[JobStatus, int]:
        if not allow_global and guild_id is None:
            raise ValueError("guild_id is required outside global owner scope")
        sql = "SELECT status, COUNT(*) AS count FROM durable_jobs"
        params: tuple[object, ...] = ()
        if not allow_global:
            sql += " WHERE guild_id=?"
            params = (guild_id,)
        sql += " GROUP BY status"
        with self._lock:
            rows = self._required().execute(sql, params).fetchall()
        result = {status: 0 for status in JobStatus}
        result.update({JobStatus(str(row["status"])): int(row["count"]) for row in rows})
        return result

    def claim_due(self, now: datetime, lease: timedelta, limit: int = 50) -> tuple[Claim, ...]:
        if lease <= timedelta(0):
            raise ValueError("lease must be positive")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        connection = self._required()
        now_text = _timestamp(now)
        expiry_text = _timestamp(_utc(now) + lease)
        claims: list[Claim] = []
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # execution開始済みclaimのlease切れは「成功したか不明」なので再実行しない。
                connection.execute(
                    """UPDATE durable_jobs SET status='uncertain', claim_token=NULL, claim_expires_at=NULL,
                    finished_at=?, last_error_type='lease_expired_after_execution_started', updated_at=?
                    WHERE status='claimed' AND claim_expires_at <= ? AND execution_started_at IS NOT NULL""",
                    (now_text, now_text, now_text),
                )
                connection.execute(
                    """UPDATE durable_job_attempts SET finished_at=?, outcome='uncertain',
                    error_type='lease_expired_after_execution_started'
                    WHERE finished_at IS NULL AND EXISTS (
                        SELECT 1 FROM durable_jobs j WHERE j.id=durable_job_attempts.job_id
                        AND j.status='uncertain' AND j.attempts=durable_job_attempts.attempt_number
                    )""",
                    (now_text,),
                )
                connection.execute(
                    """UPDATE durable_jobs SET status='failed', claim_token=NULL, claim_expires_at=NULL,
                    finished_at=?, last_error_type='max_attempts_exhausted', updated_at=?
                    WHERE status='claimed' AND claim_expires_at <= ? AND execution_started_at IS NULL
                    AND attempts >= max_attempts""",
                    (now_text, now_text, now_text),
                )
                connection.execute(
                    """UPDATE durable_job_attempts SET finished_at=?, outcome='failed',
                    error_type='max_attempts_exhausted'
                    WHERE finished_at IS NULL AND EXISTS (
                        SELECT 1 FROM durable_jobs j WHERE j.id=durable_job_attempts.job_id
                        AND j.status='failed' AND j.attempts=durable_job_attempts.attempt_number
                    )""",
                    (now_text,),
                )
                connection.execute(
                    """UPDATE durable_jobs SET status='pending', claim_token=NULL, claim_expires_at=NULL,
                    updated_at=? WHERE status='claimed' AND claim_expires_at <= ?
                    AND execution_started_at IS NULL AND attempts < max_attempts""",
                    (now_text, now_text),
                )
                rows = connection.execute(
                    """SELECT * FROM durable_jobs
                    WHERE status='pending' AND available_at <= ? AND attempts < max_attempts
                    ORDER BY available_at, created_at, id LIMIT ?""",
                    (now_text, limit),
                ).fetchall()
                for row in rows:
                    token = secrets.token_hex(16)
                    attempt_number = int(row["attempts"]) + 1
                    cursor = connection.execute(
                        """UPDATE durable_jobs SET status='claimed', attempts=?, claim_token=?,
                        claim_expires_at=?, execution_started_at=NULL, updated_at=?
                        WHERE id=? AND status='pending' AND available_at <= ?""",
                        (attempt_number, token, expiry_text, now_text, row["id"], now_text),
                    )
                    if cursor.rowcount != 1:
                        continue
                    connection.execute(
                        """INSERT INTO durable_job_attempts
                        (job_id, attempt_number, claim_token, claimed_at)
                        VALUES (?, ?, ?, ?)""",
                        (row["id"], attempt_number, token, now_text),
                    )
                    claimed_row = dict(row)
                    claimed_row.update(status=JobStatus.CLAIMED.value, attempts=attempt_number)
                    claims.append(
                        Claim(
                            self._job(claimed_row),
                            Lease(token=token, expires_at=_datetime(expiry_text)),
                        )
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return tuple(claims)

    def mark_execution_started(self, claim: Claim, started_at: datetime) -> bool:
        value = _timestamp(started_at)
        return self._update_claim(
            claim,
            """UPDATE durable_jobs SET execution_started_at=?, updated_at=?
            WHERE id=? AND status='claimed' AND claim_token=?""",
            (value, value, claim.job.id, claim.lease.token),
            attempt_sql="""UPDATE durable_job_attempts SET execution_started_at=?
            WHERE job_id=? AND attempt_number=? AND claim_token=?""",
            attempt_params=(value, claim.job.id, claim.job.attempts, claim.lease.token),
        )

    def finalize_success(self, claim: Claim, receipt: Receipt, finished_at: datetime) -> bool:
        return self._finish(claim, JobStatus.SUCCEEDED, finished_at, receipt=receipt)

    def finalize_skipped(self, claim: Claim, detail: str, finished_at: datetime) -> bool:
        return self._finish(claim, JobStatus.SKIPPED, finished_at, detail=detail)

    def finalize_failed(self, claim: Claim, error_type: str, detail: str, finished_at: datetime) -> bool:
        return self._finish(
            claim,
            JobStatus.FAILED,
            finished_at,
            error_type=error_type,
            detail=detail,
        )

    def mark_uncertain(self, claim: Claim, error_type: str, detail: str, finished_at: datetime) -> bool:
        return self._finish(
            claim,
            JobStatus.UNCERTAIN,
            finished_at,
            error_type=error_type,
            detail=detail,
        )

    def schedule_retry(
        self,
        claim: Claim,
        available_at: datetime,
        error_type: str,
        detail: str,
        finished_at: datetime,
    ) -> bool:
        connection = self._required()
        finished = _timestamp(finished_at)
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE durable_jobs SET status='pending', available_at=?, claim_token=NULL,
                    claim_expires_at=NULL, execution_started_at=NULL, last_error_type=?,
                    outcome_detail=?, updated_at=? WHERE id=? AND status='claimed' AND claim_token=?""",
                    (
                        _timestamp(available_at),
                        error_type[:100],
                        detail[:500],
                        finished,
                        claim.job.id,
                        claim.lease.token,
                    ),
                )
                if cursor.rowcount == 1:
                    connection.execute(
                        """UPDATE durable_job_attempts SET finished_at=?, outcome='retryable_failure',
                        error_type=? WHERE job_id=? AND attempt_number=? AND claim_token=?""",
                        (finished, error_type[:100], claim.job.id, claim.job.attempts, claim.lease.token),
                    )
                connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                connection.rollback()
                raise

    def defer_claim(self, claim: Claim, available_at: datetime, detail: str, now: datetime) -> bool:
        """実行policyがOFFの間はattemptを消費せずqueueへ戻す。"""

        return self._defer_claim(
            claim,
            available_at,
            detail,
            now,
            require_unstarted=True,
        )

    def defer_before_executor(
        self,
        claim: Claim,
        available_at: datetime,
        detail: str,
        now: datetime,
    ) -> bool:
        """
        execution boundary記録後の最終policy確認でOFFになった場合に戻す。

        呼び出し側はexecutorを1度もinvokeしていないことを保証する。
        """

        return self._defer_claim(
            claim,
            available_at,
            detail,
            now,
            require_unstarted=False,
        )

    def _defer_claim(
        self,
        claim: Claim,
        available_at: datetime,
        detail: str,
        now: datetime,
        *,
        require_unstarted: bool,
    ) -> bool:

        connection = self._required()
        updated = _timestamp(now)
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                execution_clause = "AND execution_started_at IS NULL" if require_unstarted else ""
                cursor = connection.execute(
                    f"""UPDATE durable_jobs SET status='pending', available_at=?,
                    attempts=CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    claim_token=NULL, claim_expires_at=NULL, execution_started_at=NULL,
                    outcome_detail=?, updated_at=?
                    WHERE id=? AND status='claimed' AND claim_token=? {execution_clause}""",  # noqa: S608
                    (
                        _timestamp(available_at),
                        detail[:500],
                        updated,
                        claim.job.id,
                        claim.lease.token,
                    ),
                )
                if cursor.rowcount == 1:
                    connection.execute(
                        """DELETE FROM durable_job_attempts
                        WHERE job_id=? AND attempt_number=? AND claim_token=?""",
                        (claim.job.id, claim.job.attempts, claim.lease.token),
                    )
                connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                connection.rollback()
                raise

    def retry_terminal(
        self,
        job_id: str,
        now: datetime,
        *,
        guild_id: int | None,
        allow_global: bool,
    ) -> bool:
        """failed/skippedのみを再queue。uncertainは重複防止のため対象外。"""

        connection = self._required()
        now_text = _timestamp(now)
        scope_sql, scope_params = self._scope(guild_id, allow_global)
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    f"""UPDATE durable_jobs SET status='pending', available_at=?,
                    max_attempts=CASE WHEN attempts >= max_attempts THEN attempts + 1 ELSE max_attempts END,
                    claim_token=NULL, claim_expires_at=NULL, execution_started_at=NULL, finished_at=NULL,
                    last_error_type=NULL, outcome_detail='operator_retry', receipt_json=NULL, updated_at=?
                    WHERE id=? AND status IN ('failed','skipped') AND attempts < 25
                    {scope_sql}""",  # noqa: S608
                    (now_text, now_text, job_id, *scope_params),
                )
                connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                connection.rollback()
                raise

    def cancel_pending(
        self,
        job_id: str,
        now: datetime,
        *,
        guild_id: int | None,
        allow_global: bool,
    ) -> bool:
        """pendingまたは未実行claimedのみcancel。実行開始後は触らない。"""

        connection = self._required()
        now_text = _timestamp(now)
        scope_sql, scope_params = self._scope(guild_id, allow_global)
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"""SELECT status, attempts, claim_token FROM durable_jobs
                    WHERE id=? AND status IN ('pending','claimed') AND execution_started_at IS NULL
                    {scope_sql}""",  # noqa: S608
                    (job_id, *scope_params),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    """UPDATE durable_jobs SET status='skipped', claim_token=NULL, claim_expires_at=NULL,
                    finished_at=?, last_error_type='operator_cancelled', outcome_detail='operator_cancelled',
                    updated_at=? WHERE id=? AND status=? AND execution_started_at IS NULL""",
                    (now_text, now_text, job_id, str(row["status"])),
                )
                if cursor.rowcount == 1 and row["status"] == JobStatus.CLAIMED.value:
                    connection.execute(
                        """UPDATE durable_job_attempts SET finished_at=?, outcome='skipped',
                        error_type='operator_cancelled' WHERE job_id=? AND attempt_number=? AND claim_token=?""",
                        (now_text, job_id, int(row["attempts"]), str(row["claim_token"])),
                    )
                connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                connection.rollback()
                raise

    def _finish(
        self,
        claim: Claim,
        status: JobStatus,
        finished_at: datetime,
        *,
        receipt: Receipt | None = None,
        error_type: str | None = None,
        detail: str = "",
    ) -> bool:
        connection = self._required()
        finished = _timestamp(finished_at)
        receipt_json = (
            _json(
                {
                    "external_id": receipt.external_id,
                    "details": dict(receipt.details),
                    "received_at": _timestamp(receipt.received_at),
                }
            )
            if receipt is not None
            else None
        )
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE durable_jobs SET status=?, claim_token=NULL, claim_expires_at=NULL,
                    finished_at=?, last_error_type=?, outcome_detail=?, receipt_json=?, updated_at=?
                    WHERE id=? AND status='claimed' AND claim_token=?""",
                    (
                        status.value,
                        finished,
                        error_type[:100] if error_type else None,
                        detail[:500],
                        receipt_json,
                        finished,
                        claim.job.id,
                        claim.lease.token,
                    ),
                )
                if cursor.rowcount == 1:
                    connection.execute(
                        """UPDATE durable_job_attempts SET finished_at=?, outcome=?, error_type=?, receipt_json=?
                        WHERE job_id=? AND attempt_number=? AND claim_token=?""",
                        (
                            finished,
                            status.value,
                            error_type[:100] if error_type else None,
                            receipt_json,
                            claim.job.id,
                            claim.job.attempts,
                            claim.lease.token,
                        ),
                    )
                connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                connection.rollback()
                raise

    def _update_claim(
        self,
        claim: Claim,
        sql: str,
        params: tuple[object, ...],
        *,
        attempt_sql: str,
        attempt_params: tuple[object, ...],
    ) -> bool:
        connection = self._required()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(sql, params)
                if cursor.rowcount == 1:
                    connection.execute(attempt_sql, attempt_params)
                connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _job(row: sqlite3.Row | dict[str, object]) -> Job:
        return Job(
            id=str(row["id"]),
            action_key=str(row["action_key"]),
            revision=Revision(int(row["revision"])),
            kind=str(row["kind"]),
            payload=json.loads(str(row["payload_json"])),
            available_at=_datetime(str(row["available_at"])),
            guild_id=int(row["guild_id"]) if row["guild_id"] is not None else None,
            status=JobStatus(str(row["status"])),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
        )

    def _required(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("job repository is not open")
        return self._connection

    @staticmethod
    def _scope(guild_id: int | None, allow_global: bool) -> tuple[str, tuple[object, ...]]:
        if allow_global:
            return "", ()
        if guild_id is None:
            raise ValueError("guild_id is required outside global owner scope")
        return "AND guild_id=?", (guild_id,)
