"""Durable single-use replay guard for signed execution-sandbox jobs.

This is deliberately narrower than Durable Jobs.  It stores only opaque,
domain-separated digests needed to ensure that a broker or worker never
executes the same signed job ID or nonce twice.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from pathlib import Path

from .execution_sandbox_signing import MAX_CLOCK_SKEW_SECONDS


_KEY_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_JOB_ID = re.compile(r"job_[a-z0-9]{16,64}\Z")
_NONCE = re.compile(r"[a-f0-9]{64}\Z")
_MAX_TIMESTAMP = 253_402_300_799
_PRUNE_BATCH_SIZE = 256
_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_sandbox_replay_v1 (
    job_digest BLOB PRIMARY KEY NOT NULL CHECK(length(job_digest) = 32),
    nonce_digest BLOB UNIQUE NOT NULL CHECK(length(nonce_digest) = 32),
    expires_at INTEGER NOT NULL CHECK(expires_at > 0)
) STRICT;
"""
_EXPIRY_INDEX = """
CREATE INDEX IF NOT EXISTS execution_sandbox_replay_v1_expires_at_idx
ON execution_sandbox_replay_v1(expires_at, job_digest);
"""
_CLOCK_WATERMARK_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_sandbox_replay_clock_v1 (
    singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton = 1),
    max_seen_now INTEGER NOT NULL CHECK(max_seen_now >= 0)
) STRICT;
"""


class ReplayLedgerError(RuntimeError):
    """A content-free replay-ledger failure."""

    def __init__(self) -> None:
        super().__init__("execution sandbox replay ledger failed safely")


class SqliteExecutionReplayLedger:
    """Atomic, restart-safe implementation of the signing ReplayLedger port."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or path.is_symlink() or path.exists() and not path.is_file():
            raise ReplayLedgerError
        self._path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            try:
                connection = sqlite3.connect(self._path, timeout=5, isolation_level=None, check_same_thread=False)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(_SCHEMA)
                connection.execute(_EXPIRY_INDEX)
                connection.execute(_CLOCK_WATERMARK_SCHEMA)
            except sqlite3.Error:
                raise ReplayLedgerError from None
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    raise ReplayLedgerError from None

    def accept(
        self,
        *,
        key_id: str,
        job_id: str,
        nonce: str,
        expires_at: int,
        now: int,
        max_clock_skew_seconds: int,
    ) -> bool:
        """Consume both opaque job and nonce identities exactly once."""
        if (
            not isinstance(key_id, str)
            or _KEY_ID.fullmatch(key_id) is None
            or not isinstance(job_id, str)
            or _JOB_ID.fullmatch(job_id) is None
            or not isinstance(nonce, str)
            or _NONCE.fullmatch(nonce) is None
            or type(expires_at) is not int
            or not 0 < expires_at <= _MAX_TIMESTAMP
            or type(now) is not int
            or not 0 <= now <= _MAX_TIMESTAMP
            or type(max_clock_skew_seconds) is not int
            or not 0 <= max_clock_skew_seconds <= MAX_CLOCK_SKEW_SECONDS
        ):
            raise ReplayLedgerError
        job_digest = _digest(b"job", key_id, job_id)
        nonce_digest = _digest(b"nonce", key_id, nonce)
        with self._lock:
            connection = self._connection
            if connection is None:
                raise ReplayLedgerError
            try:
                connection.execute("BEGIN IMMEDIATE")
                _validate_clock_high_water(connection, now=now)
                _prune_expired(connection, now=now)
                try:
                    connection.execute(
                        "INSERT INTO execution_sandbox_replay_v1(job_digest, nonce_digest, expires_at) VALUES(?, ?, ?)",
                        (job_digest, nonce_digest, expires_at),
                    )
                except sqlite3.IntegrityError:
                    _rollback(connection)
                    return False
                _record_clock_high_water(connection, now=now)
                connection.execute("COMMIT")
            except ReplayLedgerError:
                _rollback(connection)
                raise
            except sqlite3.Error:
                _rollback(connection)
                raise ReplayLedgerError from None
        return True

    def __enter__(self) -> SqliteExecutionReplayLedger:
        self.open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            "SqliteExecutionReplayLedger(state=opened)"
            if self._connection is not None
            else "SqliteExecutionReplayLedger(state=closed)"
        )


def _digest(domain: bytes, key_id: str, value: str) -> bytes:
    return hashlib.sha256(
        b"yonerai.execution-sandbox.replay.v1\0"
        + domain
        + b"\0"
        + key_id.encode("ascii")
        + b"\0"
        + value.encode("ascii")
    ).digest()


def _prune_expired(
    connection: sqlite3.Connection,
    *,
    now: int,
) -> None:
    # A later verifier call may select any permitted skew, so retain every
    # tombstone through the protocol-wide maximum rather than this call's skew.
    prune_before = now - MAX_CLOCK_SKEW_SECONDS
    if prune_before <= 1:
        return
    selected = connection.execute(
        """
        SELECT MAX(expires_at) FROM (
            SELECT expires_at
            FROM execution_sandbox_replay_v1
            WHERE expires_at < ?
            ORDER BY expires_at, job_digest
            LIMIT ?
        )
        """,
        (prune_before, _PRUNE_BATCH_SIZE),
    ).fetchone()
    max_pruned_expires_at = None if selected is None else selected[0]
    if max_pruned_expires_at is None:
        return
    if type(max_pruned_expires_at) is not int or not 0 < max_pruned_expires_at < prune_before:
        raise ReplayLedgerError
    deleted = connection.execute(
        """
        DELETE FROM execution_sandbox_replay_v1
        WHERE job_digest IN (
            SELECT job_digest
            FROM execution_sandbox_replay_v1
            WHERE expires_at < ?
            ORDER BY expires_at, job_digest
            LIMIT ?
        )
        """,
        (prune_before, _PRUNE_BATCH_SIZE),
    ).rowcount
    if not 1 <= deleted <= _PRUNE_BATCH_SIZE:
        raise ReplayLedgerError


def _validate_clock_high_water(connection: sqlite3.Connection, *, now: int) -> None:
    watermark = connection.execute(
        "SELECT max_seen_now FROM execution_sandbox_replay_clock_v1 WHERE singleton=1"
    ).fetchone()
    if watermark is None:
        return
    max_seen_now = watermark[0]
    if type(max_seen_now) is not int or not 0 <= max_seen_now <= _MAX_TIMESTAMP or now < max_seen_now:
        raise ReplayLedgerError


def _record_clock_high_water(connection: sqlite3.Connection, *, now: int) -> None:
    connection.execute(
        """
        INSERT INTO execution_sandbox_replay_clock_v1(singleton, max_seen_now)
        VALUES(1, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            max_seen_now=MAX(max_seen_now, excluded.max_seen_now)
        """,
        (now,),
    )


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


__all__ = ["ReplayLedgerError", "SqliteExecutionReplayLedger"]
