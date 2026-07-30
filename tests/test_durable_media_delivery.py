from __future__ import annotations

import json
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest
import yonerai_discord.modules.ai  # noqa: F401  # delivery.py の既存 package import 順を満たす。

from yonerai_discord.modules.jobs import (
    DurableJobService,
    JobStatus,
    SqliteJobRepository,
)
from yonerai_discord.modules.media_pipeline import (
    ArtifactScope,
    MediaArtifactStore,
    MediaPipelineService,
    QrEncodeRequest,
)
from yonerai_discord.modules.media_pipeline.delivery import MediaArtifactDeliveryPreparer
from yonerai_discord.modules.media_pipeline.durable_delivery import (
    MEDIA_DELIVERY_JOB_KIND,
    DurableMediaDeliveryError,
    DurableMediaDeliveryExecutor,
    DurableMediaDeliveryPayload,
    DurableMediaDeliverySubmitter,
    MediaDeliverySinkReceipt,
    MediaDeliveryTargetLease,
    MediaDeliveryTransientError,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 30, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class FakeSink:
    def __init__(self, *, clock: Clock) -> None:
        self.clock = clock
        self.preflight_calls = 0
        self.edit_calls = 0
        self.preflight_error: Exception | None = None
        self.edit_error: Exception | None = None
        self.receipt_override: MediaDeliverySinkReceipt | None = None
        self.requests = []

    async def preflight(self, request):
        self.preflight_calls += 1
        self.requests.append(request)
        if self.preflight_error is not None:
            raise self.preflight_error
        return MediaDeliveryTargetLease(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            message_id=request.message_id,
            bot_owned=True,
            token=f"lease-{self.preflight_calls}",
        )

    async def edit(self, request, *, lease):
        del lease
        self.edit_calls += 1
        if self.edit_error is not None:
            raise self.edit_error
        return self.receipt_override or MediaDeliverySinkReceipt(
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            message_id=request.message_id,
            delivery_digest=request.delivery_digest,
            attachment_ids=tuple(900 + index for index in range(1, len(request.attachments) + 1)),
            received_at=self.clock(),
        )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    root = tmp_path / "artifacts"
    root.mkdir()
    value = MediaArtifactStore(root, clock=lambda: clock().timestamp())
    yield value
    value.close()


def _scope(request_id: str = "delivery-request-1") -> ArtifactScope:
    return ArtifactScope(request_id=request_id, guild_id=10, channel_id=20, user_id=30)


def _artifact(store: MediaArtifactStore, scope: ArtifactScope, suffix: str = "1"):
    return (
        MediaPipelineService(store)
        .qr_encode(
            QrEncodeRequest(scope=scope, payload=f"https://example.invalid/{suffix}"),
            commit_check=lambda: True,
        )
        .artifact
    )


def _payload(
    store: MediaArtifactStore,
    *,
    scope: ArtifactScope | None = None,
    count: int = 1,
) -> DurableMediaDeliveryPayload:
    bound_scope = scope or _scope()
    return DurableMediaDeliveryPayload(
        scope=bound_scope,
        target_message_id=40,
        artifacts=tuple(_artifact(store, bound_scope, str(index)) for index in range(count)),
        required_action_ids=("media.qr_encode",),
        required_capabilities=(
            ("cap-run-ai-mention-chat", 0),
            ("cap-run-media-qr-encode", 10),
        ),
    )


def _repo(path: Path) -> SqliteJobRepository:
    value = SqliteJobRepository(path)
    value.open()
    return value


def _executor(
    store: MediaArtifactStore,
    sink: FakeSink,
    *,
    current=lambda _payload: True,
    target_current=lambda _payload: True,
    preparer_current=None,
    sink_current=None,
):
    preparer = MediaArtifactDeliveryPreparer(store, store_current=lambda: store)
    return DurableMediaDeliveryExecutor(
        preparer=preparer,
        preparer_current=preparer_current or (lambda: preparer),
        sink=sink,
        sink_current=sink_current or (lambda: sink),
        authorization_current=current,
        target_current=target_current,
        retention_store=store,
        retention_store_current=lambda: store,
    )


def _service(
    repo: SqliteJobRepository,
    executor: DurableMediaDeliveryExecutor,
    clock: Clock,
) -> DurableJobService:
    return DurableJobService(
        repo,
        {MEDIA_DELIVERY_JOB_KIND: executor},
        clock=clock,
        backoff_base_seconds=1,
    )


def _submitter(
    service: DurableJobService,
    store: MediaArtifactStore,
    clock: Clock,
) -> DurableMediaDeliverySubmitter:
    return DurableMediaDeliverySubmitter(
        service,
        store=store,
        store_current=lambda: store,
        clock=clock,
        retention_seconds=3_600,
    )


def _stored_row(path: Path, job_id: str) -> sqlite3.Row:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


def test_payload_binding_and_duplicate_submit_are_deterministic(tmp_path: Path, store, clock) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    try:
        payload = _payload(store, count=2)
        jobs = DurableJobService(repo, {}, clock=clock)
        submitter = _submitter(jobs, store, clock)

        first = submitter.submit(payload)
        clock.now += timedelta(seconds=1)
        duplicate = submitter.submit(payload)

        assert duplicate == first
        assert first.action_key == payload.action_key
        assert first.guild_id == payload.scope.guild_id
        assert first.kind == MEDIA_DELIVERY_JOB_KIND
        assert first.payload["required_action_ids"] == ["media.qr_encode"]
        assert first.payload["required_capabilities"] == [
            {"capability_id": "cap-run-ai-mention-chat", "minimum_level": 0},
            {"capability_id": "cap-run-media-qr-encode", "minimum_level": 10},
        ]
        assert payload.artifacts[0].artifact_id not in repr(payload)
        assert payload.artifacts[0].content_digest not in repr(payload)
    finally:
        repo.close()


def test_parallel_duplicate_submit_returns_one_exact_job(tmp_path: Path, store, clock) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    try:
        payload = _payload(store)
        jobs = DurableJobService(repo, {}, clock=clock)
        submitter = _submitter(jobs, store, clock)
        original_get_by_action = repo.get_by_action
        initial_lookup = Barrier(2)

        def gated_get_by_action(action_key, revision):
            existing = original_get_by_action(action_key, revision)
            if existing is None:
                initial_lookup.wait(timeout=5)
            return existing

        repo.get_by_action = gated_get_by_action  # type: ignore[method-assign]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = tuple(pool.submit(submitter.submit, payload) for _ in range(2))
            submitted = tuple(future.result(timeout=10) for future in futures)

        assert submitted[0].id == submitted[1].id
        assert repo.get(submitted[0].id) is not None
    finally:
        repo.close()


def test_parallel_submit_with_different_attempt_limits_fails_mismatch(
    tmp_path: Path,
    store,
    clock,
) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    try:
        payload = _payload(store)
        jobs = DurableJobService(repo, {}, clock=clock)
        submitter = _submitter(jobs, store, clock)
        original_get_by_action = repo.get_by_action
        initial_lookup = Barrier(2)

        def gated_get_by_action(action_key, revision):
            existing = original_get_by_action(action_key, revision)
            if existing is None:
                initial_lookup.wait(timeout=5)
            return existing

        repo.get_by_action = gated_get_by_action  # type: ignore[method-assign]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(submitter.submit, payload, max_attempts=5),
                pool.submit(submitter.submit, payload, max_attempts=6),
            )
            results: list[object] = []
            for future in futures:
                try:
                    results.append(future.result(timeout=10))
                except DurableMediaDeliveryError as exc:
                    results.append(exc)

        submitted = tuple(result for result in results if not isinstance(result, Exception))
        rejected = tuple(result for result in results if isinstance(result, DurableMediaDeliveryError))
        assert len(submitted) == 1
        assert len(rejected) == 1
        assert str(rejected[0]) == "media delivery job binding is unavailable"
        persisted = repo.get(submitted[0].id)
        assert persisted is not None
        assert persisted.max_attempts in {5, 6}
    finally:
        repo.close()


def test_existing_job_binding_mismatch_fails_before_retention(tmp_path: Path, store, clock) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    try:
        payload = _payload(store)
        jobs = DurableJobService(repo, {}, clock=clock)
        jobs.submit(
            action_key=payload.action_key,
            revision=1,
            kind=MEDIA_DELIVERY_JOB_KIND,
            payload={"unexpected": "value"},
            guild_id=payload.scope.guild_id,
        )

        with pytest.raises(DurableMediaDeliveryError, match="job binding is unavailable"):
            _submitter(jobs, store, clock).submit(payload)

        assert (
            store.release(
                payload.artifacts[0],
                scope=payload.scope,
                delivery_key=payload.action_key,
            )
            is False
        )
    finally:
        repo.close()


def test_submit_failure_keeps_bounded_retention_for_safe_retry(tmp_path: Path, store, clock) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    try:
        payload = _payload(store)
        jobs = DurableJobService(repo, {}, clock=clock)
        submitter = _submitter(jobs, store, clock)
        original_submit = jobs.submit

        def fail_submit(**_kwargs):
            raise sqlite3.OperationalError("simulated local repository failure")

        jobs.submit = fail_submit  # type: ignore[method-assign]
        with pytest.raises(sqlite3.OperationalError):
            submitter.submit(payload)

        jobs.submit = original_submit  # type: ignore[method-assign]
        job = submitter.submit(payload)

        assert job.action_key == payload.action_key
        assert repo.get(job.id) == job
    finally:
        repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uncertain", "expected_status"),
    (
        (False, JobStatus.SUCCEEDED),
        (True, JobStatus.UNCERTAIN),
    ),
)
async def test_terminal_job_resubmit_returns_existing_without_retain(
    tmp_path: Path,
    store,
    clock,
    uncertain: bool,
    expected_status: JobStatus,
) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    sink = FakeSink(clock=clock)
    if uncertain:
        sink.edit_error = TimeoutError("simulated delivery ambiguity")
    try:
        payload = _payload(store)
        service = _service(repo, _executor(store, sink), clock)
        submitter = _submitter(service, store, clock)
        first = submitter.submit(payload)

        assert await service.run_once() == ((first.id, expected_status),)
        duplicate = submitter.submit(payload)

        assert duplicate.id == first.id
        assert duplicate.status is expected_status
        assert sink.edit_calls == 1
    finally:
        repo.close()


def test_payload_rejects_duplicate_and_cross_scope_refs(store) -> None:
    scope = _scope()
    artifact = _artifact(store, scope)
    with pytest.raises(DurableMediaDeliveryError):
        DurableMediaDeliveryPayload(
            scope=scope,
            target_message_id=40,
            artifacts=(artifact, artifact),
            required_action_ids=("media.qr_encode",),
            required_capabilities=(("cap-run-media-qr-encode", 10),),
        )
    with pytest.raises(DurableMediaDeliveryError):
        DurableMediaDeliveryPayload(
            scope=_scope("another-request"),
            target_message_id=40,
            artifacts=(artifact,),
            required_action_ids=("media.qr_encode",),
            required_capabilities=(("cap-run-media-qr-encode", 10),),
        )


@pytest.mark.asyncio
async def test_success_edits_exact_bot_owned_progress_message_and_persists_safe_receipt(
    tmp_path: Path,
    store,
    clock,
) -> None:
    repo_path = tmp_path / "jobs.sqlite3"
    repo = _repo(repo_path)
    sink = FakeSink(clock=clock)
    try:
        payload = _payload(store, count=2)
        service = _service(repo, _executor(store, sink), clock)
        job = _submitter(service, store, clock).submit(payload)

        assert await service.run_once() == ((job.id, JobStatus.SUCCEEDED),)
        assert sink.preflight_calls == 1
        assert sink.edit_calls == 1
        request = sink.requests[0]
        assert (request.guild_id, request.channel_id, request.user_id, request.message_id) == (10, 20, 30, 40)
        assert request.required_action_ids == ("media.qr_encode",)
        assert len(request.attachments) == 2
        assert payload.artifacts[0].artifact_id not in repr(request)

        stored = _stored_row(repo_path, job.id)
        receipt = json.loads(stored["receipt_json"])
        assert receipt["external_id"] == "40"
        assert receipt["details"] == {
            "attachment_ids": [901, 902],
            "channel_id": 20,
            "delivery_digest": payload.delivery_digest,
            "guild_id": 10,
            "message_id": 40,
            "schema": "yonerai.discord.media-delivery-receipt.v1",
        }
        serialized = stored["receipt_json"] + stored["outcome_detail"]
        for artifact in payload.artifacts:
            assert artifact.artifact_id not in serialized
            assert artifact.content_digest not in serialized
            assert artifact.recipe_digest not in serialized
            assert (
                store.release(
                    artifact,
                    scope=payload.scope,
                    delivery_key=payload.action_key,
                )
                is False
            )
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_markdown_delivery_survives_store_and_job_service_restart(tmp_path: Path, clock: Clock) -> None:
    artifact_root = tmp_path / "documents"
    artifact_root.mkdir()
    index_path = tmp_path / "documents.sqlite3"
    jobs_path = tmp_path / "jobs.sqlite3"
    scope = _scope("document-delivery")
    first_store = MediaArtifactStore(
        artifact_root,
        database_path=index_path,
        clock=lambda: clock().timestamp(),
    )
    document = first_store.commit_markdown(
        "# Evidence\n\n| Source | Class |\n|---|---|\n| Docs | official |",
        scope=scope,
        recipe_digest=hashlib.sha256(b"evidence-table-v1").hexdigest(),
        commit_check=lambda: True,
    )
    payload = DurableMediaDeliveryPayload(
        scope=scope,
        target_message_id=40,
        artifacts=(document,),
        required_action_ids=("artifact.table.create",),
        required_capabilities=(("cap-run-ai-mention-chat", 0),),
    )
    first_repo = _repo(jobs_path)
    try:
        job = _submitter(DurableJobService(first_repo, {}, clock=clock), first_store, clock).submit(payload)
    finally:
        first_repo.close()
        first_store.close()

    store = MediaArtifactStore(
        artifact_root,
        database_path=index_path,
        clock=lambda: clock().timestamp(),
    )
    repo = _repo(jobs_path)
    sink = FakeSink(clock=clock)
    try:
        service = _service(repo, _executor(store, sink), clock)

        assert await service.run_once() == ((job.id, JobStatus.SUCCEEDED),)
        assert sink.edit_calls == 1
        assert sink.requests[0].attachments[0].filename == "media-01.md"
        assert sink.requests[0].attachments[0].data.startswith(b"# Evidence\n")
        assert document.artifact_id not in repr(sink.requests[0])
        assert document.content_digest not in repr(sink.requests[0])
    finally:
        repo.close()
        store.close()


@pytest.mark.asyncio
async def test_authorization_revoked_after_preparation_sends_nothing(tmp_path: Path, store, clock) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    sink = FakeSink(clock=clock)
    checks = 0

    def target_current(_payload):
        nonlocal checks
        checks += 1
        return checks < 5

    try:
        service = _service(repo, _executor(store, sink, target_current=target_current), clock)
        payload = _payload(store)
        job = _submitter(service, store, clock).submit(payload)

        assert await service.run_once() == ((job.id, JobStatus.SKIPPED),)
        assert sink.preflight_calls == 0
        assert sink.edit_calls == 0
        assert (
            store.release(
                payload.artifacts[0],
                scope=payload.scope,
                delivery_key=payload.action_key,
            )
            is True
        )
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_authorization_revoked_after_preflight_sends_nothing(tmp_path: Path, store, clock) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    sink = FakeSink(clock=clock)
    checks = 0

    def target_current(_payload):
        nonlocal checks
        checks += 1
        return checks < 6

    try:
        service = _service(repo, _executor(store, sink, target_current=target_current), clock)
        job = _submitter(service, store, clock).submit(_payload(store))

        assert await service.run_once() == ((job.id, JobStatus.FAILED),)
        assert sink.preflight_calls == 1
        assert sink.edit_calls == 0
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_store_or_preparer_identity_change_fails_before_sink(tmp_path: Path, store, clock) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    sink = FakeSink(clock=clock)
    try:
        executor = _executor(store, sink, preparer_current=lambda: None)
        service = _service(repo, executor, clock)
        job = _submitter(service, store, clock).submit(_payload(store))

        assert await service.run_once() == ((job.id, JobStatus.SKIPPED),)
        assert sink.preflight_calls == 0
        assert sink.edit_calls == 0
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_pre_side_effect_transient_retries_after_service_restart(
    tmp_path: Path,
    store,
    clock,
) -> None:
    repo_path = tmp_path / "jobs.sqlite3"
    first_repo = _repo(repo_path)
    first_sink = FakeSink(clock=clock)
    first_sink.preflight_error = MediaDeliveryTransientError("sensitive upstream body")
    payload = _payload(store)
    try:
        first_service = _service(first_repo, _executor(store, first_sink), clock)
        job = _submitter(first_service, store, clock).submit(payload)
        assert await first_service.run_once() == ((job.id, JobStatus.PENDING),)
        assert first_sink.edit_calls == 0
    finally:
        first_repo.close()

    clock.now += timedelta(seconds=1)
    second_repo = _repo(repo_path)
    second_sink = FakeSink(clock=clock)
    try:
        second_service = _service(second_repo, _executor(store, second_sink), clock)
        assert await second_service.run_once() == ((job.id, JobStatus.SUCCEEDED),)
        assert second_sink.edit_calls == 1
        assert second_repo.get(job.id).attempts == 2
        row = _stored_row(repo_path, job.id)
        assert "sensitive upstream body" not in (row["outcome_detail"] or "")
    finally:
        second_repo.close()


@pytest.mark.asyncio
async def test_failure_after_begin_side_effect_is_uncertain_and_never_auto_retries(
    tmp_path: Path,
    store,
    clock,
) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    sink = FakeSink(clock=clock)
    sink.edit_error = TimeoutError("sensitive timeout detail")
    try:
        service = _service(repo, _executor(store, sink), clock)
        job = _submitter(service, store, clock).submit(_payload(store))

        assert await service.run_once() == ((job.id, JobStatus.UNCERTAIN),)
        clock.now += timedelta(days=1)
        assert await service.run_once() == ()
        assert sink.edit_calls == 1
        assert repo.status_of(job.id) is JobStatus.UNCERTAIN
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_receipt_binding_mismatch_is_uncertain(tmp_path: Path, store, clock) -> None:
    repo = _repo(tmp_path / "jobs.sqlite3")
    sink = FakeSink(clock=clock)
    payload = _payload(store)
    sink.receipt_override = MediaDeliverySinkReceipt(
        guild_id=payload.scope.guild_id,
        channel_id=payload.scope.channel_id,
        message_id=41,
        delivery_digest=payload.delivery_digest,
        attachment_ids=(901,),
        received_at=clock(),
    )
    try:
        service = _service(repo, _executor(store, sink), clock)
        job = _submitter(service, store, clock).submit(payload)

        assert await service.run_once() == ((job.id, JobStatus.UNCERTAIN),)
        assert sink.edit_calls == 1
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_invalid_persisted_payload_fails_closed_without_identifier_leak(
    tmp_path: Path,
    store,
    clock,
) -> None:
    repo_path = tmp_path / "jobs.sqlite3"
    repo = _repo(repo_path)
    sink = FakeSink(clock=clock)
    payload = _payload(store)
    invalid = payload.to_mapping()
    invalid["artifacts"][0]["unexpected"] = payload.artifacts[0].artifact_id
    try:
        service = _service(repo, _executor(store, sink), clock)
        job = service.submit(
            action_key="discord.media_delivery.v1:" + ("a" * 64),
            revision=1,
            kind=MEDIA_DELIVERY_JOB_KIND,
            payload=invalid,
            guild_id=payload.scope.guild_id,
        )

        assert await service.run_once() == ((job.id, JobStatus.FAILED),)
        assert sink.preflight_calls == 0
        row = _stored_row(repo_path, job.id)
        assert payload.artifacts[0].artifact_id not in (row["outcome_detail"] or "")
        assert payload.artifacts[0].content_digest not in (row["outcome_detail"] or "")
    finally:
        repo.close()
