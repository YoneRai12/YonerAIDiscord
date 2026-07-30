from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from yonerai_discord.modules.jobs import DurableJobService, DurableJobWorker, JobStatus, SqliteJobRepository
from yonerai_discord.modules.jobs.adapter import JobsGroup


class Response:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    async def send_message(self, content: str, **kwargs) -> None:
        self.messages.append((content, kwargs))


class AuditDatabase:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events = []

    def append_audit(self, event, **kwargs):
        if self.fail:
            raise OSError("audit unavailable")
        self.events.append((event, kwargs))
        return len(self.events)


class Bot:
    def __init__(self, database: AuditDatabase, owner_ids=()) -> None:
        self.database = database
        self.owner_ids = set(owner_ids)

    async def is_owner(self, user) -> bool:
        return user.id in self.owner_ids


def interaction(*, user_id=1, guild_id=111, administrator=True):
    return SimpleNamespace(
        user=SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(administrator=administrator, manage_guild=False),
        ),
        guild_id=guild_id,
        response=Response(),
    )


@pytest.fixture
def repo(tmp_path):
    value = SqliteJobRepository(tmp_path / "adapter.sqlite3")
    value.open()
    yield value
    value.close()


def failed_job(repo, *, job_id="failed-job", guild_id=111):
    service = DurableJobService(repo, {})
    created = service.submit(
        action_key=f"failed:{job_id}",
        revision=1,
        kind="unknown",
        payload={"secret": "not-shown"},
        guild_id=guild_id,
        job_id=job_id,
    )
    claim = repo.claim_due(datetime.now(UTC), timedelta(seconds=30))[0]
    repo.finalize_failed(claim, "UnknownExecutor", "hidden detail", datetime.now(UTC))
    return created


@pytest.mark.asyncio
async def test_retry_requires_working_audit_and_preserves_guild_scope(repo) -> None:
    created = failed_job(repo)
    audit = AuditDatabase()
    bot = Bot(audit)
    group = JobsGroup(repo, DurableJobWorker(DurableJobService(repo, {})), bot)
    admin = interaction()
    await group._mutate(admin, "retry", created.id)
    assert repo.status_of(created.id) is JobStatus.PENDING
    assert [event for event, _details in audit.events] == [
        "jobs_operator_request",
        "jobs_operator_result",
    ]
    assert repo.cancel_pending(created.id, datetime.now(UTC), guild_id=111, allow_global=False)

    other = failed_job(repo, job_id="other-guild", guild_id=222)
    await group._mutate(admin, "retry", other.id)
    assert repo.status_of(other.id) is JobStatus.FAILED

    no_audit_job = failed_job(repo, job_id="no-audit")
    blocked = interaction()
    blocked_group = JobsGroup(
        repo,
        DurableJobWorker(DurableJobService(repo, {})),
        Bot(AuditDatabase(fail=True)),
    )
    await blocked_group._mutate(blocked, "retry", no_audit_job.id)
    assert repo.status_of(no_audit_job.id) is JobStatus.FAILED
    assert "変更しません" in blocked.response.messages[-1][0]


@pytest.mark.asyncio
async def test_owner_can_operate_global_job_and_list_hides_payload(repo) -> None:
    created = failed_job(repo, guild_id=None)
    bot = Bot(AuditDatabase(), owner_ids={999})
    group = JobsGroup(repo, DurableJobWorker(DurableJobService(repo, {})), bot)
    owner = interaction(user_id=999, guild_id=None, administrator=False)

    await group.list_jobs.callback(group, owner, "all", 10)
    content, kwargs = owner.response.messages[-1]
    assert created.id in content
    assert "not-shown" not in content
    assert "hidden detail" not in content
    assert kwargs["ephemeral"] is True

    await group._mutate(owner, "retry", created.id)
    assert repo.status_of(created.id) is JobStatus.PENDING


@pytest.mark.asyncio
async def test_cancel_refuses_non_admin_and_started_job(repo) -> None:
    service = DurableJobService(repo, {})
    created = service.submit(
        action_key="started:adapter",
        revision=1,
        kind="unknown",
        payload={},
        guild_id=111,
    )
    claim = repo.claim_due(datetime.now(UTC), timedelta(seconds=30))[0]
    assert repo.mark_execution_started(claim, datetime.now(UTC))

    group = JobsGroup(repo, DurableJobWorker(service), Bot(AuditDatabase()))
    non_admin = interaction(administrator=False)
    await group._mutate(non_admin, "cancel", created.id)
    assert "専用" in non_admin.response.messages[-1][0]

    admin = interaction()
    await group._mutate(admin, "cancel", created.id)
    assert repo.status_of(created.id) is JobStatus.CLAIMED
    assert "実行開始済み" in admin.response.messages[-1][0]
