from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from yonerai_discord.capability_forge import execution_sandbox_replay as replay_module
from yonerai_discord.capability_forge.execution_sandbox_replay import (
    ReplayLedgerError,
    SqliteExecutionReplayLedger,
)
from yonerai_discord.capability_forge.execution_sandbox_signing import MAX_CLOCK_SKEW_SECONDS


KEY_ID = "broker-key-v1"
JOB_ID = "job_0123456789abcdef"
NONCE = "a" * 64
EXPIRES_AT = 1_800_000_000
NOW = EXPIRES_AT - 100


def _accept(
    ledger: SqliteExecutionReplayLedger,
    *,
    key_id: str = KEY_ID,
    job_id: str = JOB_ID,
    nonce: str = NONCE,
    expires_at: int = EXPIRES_AT,
    now: int = NOW,
    max_clock_skew_seconds: int = MAX_CLOCK_SKEW_SECONDS,
) -> bool:
    return ledger.accept(
        key_id=key_id,
        job_id=job_id,
        nonce=nonce,
        expires_at=expires_at,
        now=now,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )


def _identity(index: int) -> tuple[str, str]:
    return f"job_{index:016x}", f"{index:064x}"


def test_replay_ledger_consumes_job_and_nonce_once(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as ledger:
        assert _accept(ledger) is True
        assert _accept(ledger) is False
        assert repr(ledger) == "SqliteExecutionReplayLedger(state=opened)"


def test_replay_ledger_rejects_job_or_nonce_reuse_independently(tmp_path: Path) -> None:
    with SqliteExecutionReplayLedger(tmp_path / "replay.sqlite3") as ledger:
        assert _accept(ledger) is True
        assert _accept(ledger, nonce="b" * 64) is False
        assert (
            _accept(
                ledger,
                job_id="job_fedcba9876543210",
                nonce=NONCE,
            )
            is False
        )


def test_replay_ledger_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as first:
        assert _accept(first) is True
    with SqliteExecutionReplayLedger(path) as second:
        assert _accept(second) is False


def test_replay_ledger_prunes_rows_after_expiry_and_permitted_skew(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as ledger:
        assert _accept(ledger, expires_at=100, now=100) is True
        second_job, second_nonce = _identity(2)
        assert _accept(ledger, job_id=second_job, nonce=second_nonce, expires_at=161, now=160) is True
        future_job, future_nonce = _identity(3)
        assert _accept(ledger, job_id=future_job, nonce=future_nonce, expires_at=300, now=160) is True

        with sqlite3.connect(path) as connection:
            before_boundary = [
                row[0]
                for row in connection.execute("SELECT expires_at FROM execution_sandbox_replay_v1 ORDER BY expires_at")
            ]
        assert before_boundary == [100, 161, 300]

        trigger_job, trigger_nonce = _identity(4)
        assert _accept(ledger, job_id=trigger_job, nonce=trigger_nonce, expires_at=300, now=161) is True

    with sqlite3.connect(path) as connection:
        expirations = [
            row[0]
            for row in connection.execute("SELECT expires_at FROM execution_sandbox_replay_v1 ORDER BY expires_at")
        ]
    assert expirations == [161, 300, 300]


def test_replay_ledger_mixed_skew_cannot_retire_a_replay_tombstone_early(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as first:
        assert _accept(first, expires_at=100, now=100, max_clock_skew_seconds=60) is True
        trigger_job, trigger_nonce = _identity(11)
        assert (
            _accept(
                first,
                job_id=trigger_job,
                nonce=trigger_nonce,
                expires_at=300,
                now=160,
                max_clock_skew_seconds=0,
            )
            is True
        )
    with SqliteExecutionReplayLedger(path) as reopened:
        assert _accept(reopened, expires_at=100, now=160, max_clock_skew_seconds=60) is False


def test_replay_ledger_clock_regression_after_durable_prune_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as first:
        assert _accept(first, expires_at=100, now=100) is True
        trigger_job, trigger_nonce = _identity(5)
        assert _accept(first, job_id=trigger_job, nonce=trigger_nonce, expires_at=300, now=161) is True

    with SqliteExecutionReplayLedger(path) as reopened:
        rollback_job, rollback_nonce = _identity(6)
        with pytest.raises(ReplayLedgerError) as caught:
            _accept(
                reopened,
                job_id=rollback_job,
                nonce=rollback_nonce,
                expires_at=300,
                now=160,
            )
        assert str(caught.value) == "execution sandbox replay ledger failed safely"
        assert _accept(
            reopened,
            job_id=rollback_job,
            nonce=rollback_nonce,
            expires_at=300,
            now=161,
        )


def test_replay_ledger_persists_successful_clock_high_water_without_pruning(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as first:
        assert _accept(first, expires_at=300, now=200) is True

    rollback_job, rollback_nonce = _identity(10)
    with SqliteExecutionReplayLedger(path) as reopened:
        with pytest.raises(ReplayLedgerError) as caught:
            _accept(
                reopened,
                job_id=rollback_job,
                nonce=rollback_nonce,
                expires_at=300,
                now=199,
            )
        assert str(caught.value) == "execution sandbox replay ledger failed safely"
        assert _accept(
            reopened,
            job_id=rollback_job,
            nonce=rollback_nonce,
            expires_at=300,
            now=200,
        )


def test_replay_ledger_prune_and_insert_roll_back_together(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as ledger:
        assert _accept(ledger, expires_at=100, now=100) is True
        connection = ledger._connection
        assert connection is not None

        def deny_replay_insert(
            action: int,
            first: str | None,
            _second: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_INSERT and first == "execution_sandbox_replay_v1":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny_replay_insert)
        failed_job, failed_nonce = _identity(7)
        try:
            with pytest.raises(ReplayLedgerError) as caught:
                _accept(
                    ledger,
                    job_id=failed_job,
                    nonce=failed_nonce,
                    expires_at=300,
                    now=161,
                )
        finally:
            connection.set_authorizer(None)
        assert str(caught.value) == "execution sandbox replay ledger failed safely"

        rows = connection.execute("SELECT expires_at FROM execution_sandbox_replay_v1 ORDER BY expires_at").fetchall()
        watermark = connection.execute("SELECT max_seen_now FROM execution_sandbox_replay_clock_v1").fetchall()
        assert rows == [(100,)]
        assert watermark == [(100,)]
        assert _accept(
            ledger,
            job_id=failed_job,
            nonce=failed_nonce,
            expires_at=300,
            now=161,
        )


def test_replay_ledger_pruning_is_indexed_and_bounded_per_accept(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    ledger = SqliteExecutionReplayLedger(path)
    ledger.open()
    seeded = replay_module._PRUNE_BATCH_SIZE + 44
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO execution_sandbox_replay_v1(job_digest, nonce_digest, expires_at) VALUES(?, ?, 100)",
            ((index.to_bytes(32, "big"), (index + seeded).to_bytes(32, "big")) for index in range(1, seeded + 1)),
        )

    first_job, first_nonce = _identity(8)
    assert _accept(ledger, job_id=first_job, nonce=first_nonce, expires_at=300, now=161)
    with sqlite3.connect(path) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM execution_sandbox_replay_v1 WHERE expires_at=100"
        ).fetchone()[0]
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(execution_sandbox_replay_v1)")}
    assert remaining == 44
    assert "execution_sandbox_replay_v1_expires_at_idx" in indexes

    second_job, second_nonce = _identity(9)
    assert _accept(ledger, job_id=second_job, nonce=second_nonce, expires_at=300, now=161)
    ledger.close()
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM execution_sandbox_replay_v1 WHERE expires_at=100").fetchone()[0]
            == 0
        )


def test_replay_ledger_stores_only_fixed_digests_and_expiry(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite3"
    with SqliteExecutionReplayLedger(path) as ledger:
        assert _accept(ledger) is True
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
        ledger.accept(
            key_id=key_id,
            job_id=job_id,
            nonce=nonce,
            expires_at=expires_at,
            now=NOW,
            max_clock_skew_seconds=MAX_CLOCK_SKEW_SECONDS,
        )
    ledger.close()
    rendered = repr(caught.value)
    assert job_id not in rendered
    assert nonce not in rendered


def test_replay_ledger_fails_closed_before_open(tmp_path: Path) -> None:
    ledger = SqliteExecutionReplayLedger(tmp_path / "replay.sqlite3")
    with pytest.raises(ReplayLedgerError):
        _accept(ledger)
    assert repr(ledger) == "SqliteExecutionReplayLedger(state=closed)"


@pytest.mark.parametrize(
    ("now", "max_clock_skew_seconds"),
    ((True, 60), (-1, 60), (NOW, True), (NOW, -1), (NOW, 61)),
)
def test_replay_ledger_rejects_invalid_clock_contract_content_free(
    tmp_path: Path,
    now: int,
    max_clock_skew_seconds: int,
) -> None:
    with SqliteExecutionReplayLedger(tmp_path / "replay.sqlite3") as ledger:
        with pytest.raises(ReplayLedgerError) as caught:
            _accept(
                ledger,
                now=now,
                max_clock_skew_seconds=max_clock_skew_seconds,
            )
    assert str(caught.value) == "execution sandbox replay ledger failed safely"
