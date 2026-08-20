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


_KEY_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_JOB_ID = re.compile(r"job_[a-z0-9]{16,64}\Z")
_NONCE = re.compile(r"[a-f0-9]{64}\Z")
_MAX_TIMESTAMP = 253_402_300_799
_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_sandbox_replay_v1 (
    job_digest BLOB PRIMARY KEY NOT NULL CHECK(length(job_digest) = 32),
    nonce_digest BLOB UNIQUE NOT NULL CHECK(length(nonce_digest) = 32),
    expires_at INTEGER NOT NULL CHECK(expires_at > 0)
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

    def accept(self, *, key_id: str, job_id: str, nonce: str, expires_at: int) -> bool:
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
                connection.execute(
                    "INSERT INTO execution_sandbox_replay_v1(job_digest, nonce_digest, expires_at) VALUES(?, ?, ?)",
                    (job_digest, nonce_digest, expires_at),
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError:
                _rollback(connection)
                return False
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


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


__all__ = ["ReplayLedgerError", "SqliteExecutionReplayLedger"]
