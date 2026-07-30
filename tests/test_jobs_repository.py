from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from yonerai_discord.modules.jobs import Job, JobStatus, Revision, SqliteJobRepository

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def job(
    job_id: str = "job-1",
    *,
    action_key: str = "discord:send:1",
    revision: int = 1,
    available_at: datetime = NOW,
    max_attempts: int = 3,
    guild_id: int | None = None,
) -> Job:
    return Job(
        id=job_id,
        action_key=action_key,
        revision=Revision(revision),
        kind="discord",
        payload={"content": "日本語"},
        available_at=available_at,
        guild_id=guild_id,
        max_attempts=max_attempts,
    )


def repository(path) -> SqliteJobRepository:
    value = SqliteJobRepository(path)
    value.open()
    return value


def test_action_key_and_revision_are_unique(tmp_path) -> None:
    repo = repository(tmp_path / "jobs.sqlite3")
    try:
        assert repo.enqueue(job())
        assert not repo.enqueue(job("duplicate"))
        assert repo.enqueue(job("revision-2", revision=2))
    finally:
        repo.close()


def test_begin_immediate_claim_is_atomic_across_connections(tmp_path) -> None:
    path = tmp_path / "jobs.sqlite3"
    first = repository(path)
    second = repository(path)
    try:
        first.enqueue(job())
        claims = first.claim_due(NOW, timedelta(seconds=30))
        assert len(claims) == 1
        assert second.claim_due(NOW, timedelta(seconds=30)) == ()
        assert claims[0].job.attempts == 1
        assert claims[0].lease.token
    finally:
        first.close()
        second.close()


def test_expired_lease_before_execution_is_recovered(tmp_path) -> None:
    repo = repository(tmp_path / "jobs.sqlite3")
    try:
        repo.enqueue(job())
        first = repo.claim_due(NOW, timedelta(seconds=5))[0]
        second = repo.claim_due(NOW + timedelta(seconds=6), timedelta(seconds=5))[0]
        assert second.job.id == first.job.id
        assert second.job.attempts == 2
        assert second.lease.token != first.lease.token
    finally:
        repo.close()


def test_expired_lease_after_execution_started_becomes_uncertain(tmp_path) -> None:
    repo = repository(tmp_path / "jobs.sqlite3")
    try:
        repo.enqueue(job())
        claim = repo.claim_due(NOW, timedelta(seconds=5))[0]
        assert repo.mark_execution_started(claim, NOW)
        assert repo.claim_due(NOW + timedelta(seconds=6), timedelta(seconds=5)) == ()
        assert repo.status_of(job().id) is JobStatus.UNCERTAIN
    finally:
        repo.close()


def test_future_job_is_not_claimed_early(tmp_path) -> None:
    repo = repository(tmp_path / "jobs.sqlite3")
    try:
        repo.enqueue(job(available_at=NOW + timedelta(minutes=1)))
        assert repo.claim_due(NOW, timedelta(seconds=5)) == ()
        assert len(repo.claim_due(NOW + timedelta(minutes=1), timedelta(seconds=5))) == 1
    finally:
        repo.close()


def test_list_and_counts_are_scoped_without_exposing_payload(tmp_path) -> None:
    repo = repository(tmp_path / "jobs.sqlite3")
    try:
        repo.enqueue(job("guild-1", action_key="one", guild_id=111))
        repo.enqueue(job("guild-2", action_key="two", guild_id=222))
        rows = repo.list_summaries(guild_id=111)
        assert [row.id for row in rows] == ["guild-1"]
        assert not hasattr(rows[0], "payload")
        assert repo.count_by_status(guild_id=111)[JobStatus.PENDING] == 1
        assert len(repo.list_summaries(allow_global=True)) == 2
    finally:
        repo.close()


def test_operator_retry_only_accepts_terminal_non_uncertain_jobs(tmp_path) -> None:
    repo = repository(tmp_path / "jobs.sqlite3")
    try:
        repo.enqueue(job("failed", action_key="failed", guild_id=111, max_attempts=1))
        failed_claim = repo.claim_due(NOW, timedelta(seconds=30))[0]
        assert repo.mark_execution_started(failed_claim, NOW)
        assert repo.finalize_failed(failed_claim, "bad", "", NOW)
        assert repo.retry_terminal("failed", NOW, guild_id=111, allow_global=False)
        retried = repo.get("failed")
        assert retried is not None
        assert retried.status is JobStatus.PENDING
        assert retried.max_attempts == 2
        assert repo.cancel_pending("failed", NOW, guild_id=111, allow_global=False)

        repo.enqueue(job("uncertain", action_key="uncertain", guild_id=111))
        uncertain_claim = repo.claim_due(NOW, timedelta(seconds=30))[0]
        assert repo.mark_execution_started(uncertain_claim, NOW)
        assert repo.mark_uncertain(uncertain_claim, "transport", "", NOW)
        assert not repo.retry_terminal("uncertain", NOW, guild_id=111, allow_global=False)
        assert repo.status_of("uncertain") is JobStatus.UNCERTAIN
        assert not repo.retry_terminal("failed", NOW, guild_id=222, allow_global=False)
    finally:
        repo.close()


def test_cancel_wins_only_before_execution_boundary(tmp_path) -> None:
    repo = repository(tmp_path / "jobs.sqlite3")
    try:
        repo.enqueue(job("cancelled", action_key="cancelled", guild_id=111))
        claim = repo.claim_due(NOW, timedelta(seconds=30))[0]
        assert repo.cancel_pending("cancelled", NOW, guild_id=111, allow_global=False)
        assert not repo.mark_execution_started(claim, NOW)
        assert repo.status_of("cancelled") is JobStatus.SKIPPED

        repo.enqueue(job("started", action_key="started", guild_id=111))
        started = repo.claim_due(NOW, timedelta(seconds=30))[0]
        assert repo.mark_execution_started(started, NOW)
        assert not repo.cancel_pending("started", NOW, guild_id=111, allow_global=False)
        assert repo.status_of("started") is JobStatus.CLAIMED
    finally:
        repo.close()


def test_open_migrates_pre_guild_schema(tmp_path) -> None:
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE durable_jobs (
        id TEXT PRIMARY KEY, action_key TEXT NOT NULL, revision INTEGER NOT NULL,
        kind TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
        available_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL, claim_token TEXT, claim_expires_at TEXT,
        execution_started_at TEXT, finished_at TEXT, last_error_type TEXT,
        outcome_detail TEXT, receipt_json TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(action_key, revision))"""
    )
    connection.commit()
    connection.close()

    repo = repository(path)
    try:
        columns = {row["name"] for row in repo._required().execute("PRAGMA table_info(durable_jobs)").fetchall()}
        assert "guild_id" in columns
    finally:
        repo.close()
