from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from yonerai_discord.capability_forge.execution_sandbox_replay import (
    ReplayLedgerError,
    SqliteExecutionReplayLedger,
)


KEY_ID = "broker-key-v1"
JOB_ID = "job_0123456789abcdef"
NONCE = "a" * 64
EXPIRES_AT = 1_800_000_000


def test_replay_ledger_consumes_job_and_nonce_once(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as ledger:
        assert ledger.accept(key_id=KEY_ID, job_id=JOB_ID, nonce=NONCE, expires_at=EXPIRES_AT) is True
        assert ledger.accept(key_id=KEY_ID, job_id=JOB_ID, nonce=NONCE, expires_at=EXPIRES_AT) is False
        assert repr(ledger) == "SqliteExecutionReplayLedger(state=opened)"


def test_replay_ledger_rejects_job_or_nonce_reuse_independently(tmp_path: Path) -> None:
    with SqliteExecutionReplayLedger(tmp_path / "replay.sqlite3") as ledger:
        assert ledger.accept(key_id=KEY_ID, job_id=JOB_ID, nonce=NONCE, expires_at=EXPIRES_AT) is True
        assert ledger.accept(key_id=KEY_ID, job_id=JOB_ID, nonce="b" * 64, expires_at=EXPIRES_AT) is False
        assert (
            ledger.accept(
                key_id=KEY_ID,
                job_id="job_fedcba9876543210",
                nonce=NONCE,
                expires_at=EXPIRES_AT,
            )
            is False
        )


def test_replay_ledger_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as first:
        assert first.accept(key_id=KEY_ID, job_id=JOB_ID, nonce=NONCE, expires_at=EXPIRES_AT) is True
    with SqliteExecutionReplayLedger(path) as second:
        assert second.accept(key_id=KEY_ID, job_id=JOB_ID, nonce=NONCE, expires_at=EXPIRES_AT) is False


def test_replay_ledger_stores_only_fixed_digests_and_expiry(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as ledger:
        assert ledger.accept(key_id=KEY_ID, job_id=JOB_ID, nonce=NONCE, expires_at=EXPIRES_AT) is True
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT job_digest, nonce_digest, expires_at FROM execution_sandbox_replay_v1"
        ).fetchone()
        columns = [item[1] for item in connection.execute("PRAGMA table_info(execution_sandbox_replay_v1)")]
    assert row is not None
    assert len(row[0]) == len(row[1]) == 32
    assert row[2] == EXPIRES_AT
    assert columns == ["job_digest", "nonce_digest", "expires_at"]
    raw = path.read_bytes()
    assert JOB_ID.encode() not in raw
    assert NONCE.encode() not in raw


@pytest.mark.parametrize(
    ("key_id", "job_id", "nonce", "expires_at"),
    [
        ("bad key", JOB_ID, NONCE, EXPIRES_AT),
        (KEY_ID, "job_bad", NONCE, EXPIRES_AT),
        (KEY_ID, JOB_ID, "z" * 64, EXPIRES_AT),
        (KEY_ID, JOB_ID, NONCE, 0),
        (KEY_ID, JOB_ID, NONCE, True),
    ],
)
def test_replay_ledger_rejects_invalid_bindings_content_free(
    tmp_path: Path,
    key_id: str,
    job_id: str,
    nonce: str,
    expires_at: int,
) -> None:
    ledger = SqliteExecutionReplayLedger(tmp_path / "replay.sqlite3")
    ledger.open()
    with pytest.raises(ReplayLedgerError) as caught:
        ledger.accept(key_id=key_id, job_id=job_id, nonce=nonce, expires_at=expires_at)
    ledger.close()
    rendered = repr(caught.value)
    assert job_id not in rendered
    assert nonce not in rendered


def test_replay_ledger_fails_closed_before_open(tmp_path: Path) -> None:
    ledger = SqliteExecutionReplayLedger(tmp_path / "replay.sqlite3")
    with pytest.raises(ReplayLedgerError):
        ledger.accept(key_id=KEY_ID, job_id=JOB_ID, nonce=NONCE, expires_at=EXPIRES_AT)
    assert repr(ledger) == "SqliteExecutionReplayLedger(state=closed)"
