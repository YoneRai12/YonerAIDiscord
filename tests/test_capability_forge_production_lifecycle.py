from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yonerai_discord.capability_forge import (
    ForgePrimitiveRegistry,
    RecipeCandidate,
    RecipeRunStatus,
    RecipeStep,
)
from yonerai_discord.capability_forge.lifecycle import SqliteForgeLifecycleRepository
from yonerai_discord.capability_forge.production_lifecycle import ProductionRecipeLifecycleBridge
from yonerai_discord.capability_forge.static_templates import (
    CodeOwnedTemplateIdentity,
    ProductionRecipeRunner,
    build_production_registry,
)


def _candidate(text: str = "private prompt text") -> RecipeCandidate:
    return RecipeCandidate(
        (
            RecipeStep(
                step_id="first",
                primitive_id="text.identity",
                primitive_revision="1",
                inputs={"text": text},
            ),
        )
    )


def _bridge(tmp_path: Path, *, current=lambda: True):
    repository = SqliteForgeLifecycleRepository(tmp_path / "forge.sqlite3")
    repository.open()
    registry = build_production_registry((CodeOwnedTemplateIdentity("text.identity", "1"),))
    bridge = ProductionRecipeLifecycleBridge(
        runner=ProductionRecipeRunner(registry),
        repository=repository,
        current=current,
        clock=lambda: datetime(2026, 7, 26, tzinfo=UTC),
    )
    return bridge, repository


@pytest.mark.asyncio
async def test_sealed_success_is_recorded_for_existing_poller(tmp_path: Path) -> None:
    bridge, repository = _bridge(tmp_path)
    candidate = _candidate()
    try:
        result = await bridge.run(owner_user_id=42, candidate=candidate)

        assert result.receipt.status is RecipeRunStatus.SUCCEEDED
        stored = repository.get_candidate(candidate.digest)
        assert stored is not None
        assert stored.success_count == 1
        assert stored.notification_state == "pending"
        assert stored.official is False
        assert stored.runtime_ready is False
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_same_owner_digest_reuses_existing_lifecycle_aggregation(tmp_path: Path) -> None:
    bridge, repository = _bridge(tmp_path)
    candidate = _candidate()
    try:
        await bridge.run(owner_user_id=42, candidate=candidate)
        await bridge.run(owner_user_id=42, candidate=candidate)

        stored = repository.get_candidate(candidate.digest)
        summary = repository.get_user_success(candidate.digest, 42)
        assert stored is not None and stored.success_count == 2
        assert summary is not None and summary.success_count == 2
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_rejected_or_cancelled_recipe_records_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, repository = _bridge(tmp_path)
    rejected = RecipeCandidate(
        (
            RecipeStep(
                step_id="first",
                primitive_id="unknown.template",
                primitive_revision="1",
                inputs={"text": "ignored"},
            ),
        )
    )
    try:
        result = await bridge.run(owner_user_id=42, candidate=rejected)
        assert result.receipt.status is RecipeRunStatus.REJECTED
        assert repository.get_candidate(rejected.digest) is None

        async def cancelled(_candidate: RecipeCandidate):
            raise asyncio.CancelledError

        monkeypatch.setattr(bridge._runner, "run", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await bridge.run(owner_user_id=42, candidate=_candidate())
        assert repository.get_candidate(_candidate().digest) is None
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_repository_identity_replacement_prevents_recording(tmp_path: Path) -> None:
    state: dict[str, object] = {}
    bridge, repository = _bridge(tmp_path, current=lambda: state.get("repository") is repository)
    state["repository"] = repository
    candidate = _candidate()
    original_run = bridge._runner.run

    async def lose_current(value: RecipeCandidate):
        result = await original_run(value)
        state["repository"] = object()
        return result

    bridge._runner.run = lose_current
    try:
        result = await bridge.run(owner_user_id=42, candidate=candidate)
        assert result.receipt.status is RecipeRunStatus.SUCCEEDED
        assert repository.get_candidate(candidate.digest) is None
    finally:
        repository.close()


def test_nonsealed_runner_cannot_create_a_lifecycle_bridge(tmp_path: Path) -> None:
    repository = SqliteForgeLifecycleRepository(tmp_path / "forge.sqlite3")
    repository.open()
    forged = object.__new__(ProductionRecipeRunner)
    forged._registry = ForgePrimitiveRegistry(())
    try:
        with pytest.raises(TypeError, match="production sealed"):
            ProductionRecipeLifecycleBridge(runner=forged, repository=repository, current=lambda: True)
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_begin_close_before_recording_prevents_lifecycle_write(tmp_path: Path) -> None:
    bridge, repository = _bridge(tmp_path)
    candidate = _candidate()
    original_run = bridge._runner.run

    async def close_after_run(value: RecipeCandidate):
        result = await original_run(value)
        await bridge.begin_close()
        return result

    bridge._runner.run = close_after_run
    try:
        result = await bridge.run(owner_user_id=42, candidate=candidate)
        assert result.receipt.status is RecipeRunStatus.SUCCEEDED
        assert repository.get_candidate(candidate.digest) is None
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_record_failure_does_not_promote_or_expose_recipe_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, repository = _bridge(tmp_path)
    candidate = _candidate("secret=not-recorded")

    def fail_record(**_kwargs):
        raise RuntimeError("secret=not-recorded")

    monkeypatch.setattr(repository, "record_success", fail_record)
    try:
        result = await bridge.run(owner_user_id=42, candidate=candidate)
        assert result.receipt.status is RecipeRunStatus.SUCCEEDED
        assert repository.get_candidate(candidate.digest) is None
        assert "secret" not in repr(result)
        assert "secret" not in repr(bridge)
    finally:
        repository.close()
