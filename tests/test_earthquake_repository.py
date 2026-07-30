from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yonerai_discord.modules.earthquake import GuildSubscription, SqliteEarthquakeRepository
from test_earthquake_helpers import isolated_workspace_directory


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def repository():
    with isolated_workspace_directory() as directory:
        value = SqliteEarthquakeRepository(directory / "suite.sqlite3")
        value.open()
        yield value
        value.close()


def test_subscription_is_off_by_default(repository: SqliteEarthquakeRepository) -> None:
    assert repository.get(123) == GuildSubscription(guild_id=123)
    assert repository.list_enabled() == ()


def test_subscription_round_trip_and_guild_isolation(repository: SqliteEarthquakeRepository) -> None:
    saved = repository.subscribe(1, 10, min_scale=55, notify_551=False, notify_556=True)
    assert saved.enabled is True
    assert saved.min_scale == 55
    assert saved.notify_551 is False
    assert saved.notify_556 is True
    assert repository.get(1) == saved
    assert repository.get(2).enabled is False
    assert repository.list_enabled() == (saved,)


def test_unsubscribe_is_idempotent_and_retains_safe_off_state(repository: SqliteEarthquakeRepository) -> None:
    repository.subscribe(1, 10)
    first = repository.unsubscribe(1)
    second = repository.unsubscribe(1)
    assert first.enabled is False
    assert second.enabled is False
    assert repository.list_enabled() == ()


def test_invalid_snowflakes_scale_and_empty_code_selection_are_rejected(repository: SqliteEarthquakeRepository) -> None:
    with pytest.raises(ValueError):
        repository.get(0)
    with pytest.raises(ValueError):
        repository.subscribe(1, 2, min_scale=46)
    with pytest.raises(ValueError):
        repository.subscribe(1, 2, notify_551=False, notify_556=False)


def test_repository_reopens_and_lifecycle_is_idempotent() -> None:
    with isolated_workspace_directory() as directory:
        path = directory / "persistent.sqlite3"
        first = SqliteEarthquakeRepository(path)
        first.open()
        first.open()
        first.subscribe(99, 100, min_scale=45)
        first.close()
        first.close()
        second = SqliteEarthquakeRepository(path)
        second.open()
        try:
            assert second.get(99).enabled is True
            assert second.get(99).min_scale == 45
        finally:
            second.close()


def test_dedupe_survives_reopen_and_expires_after_retention() -> None:
    with isolated_workspace_directory() as directory:
        path = directory / "dedupe.sqlite3"
        first = SqliteEarthquakeRepository(path)
        first.open()
        assert first.claim_event("event-1", "a" * 64, seen_at=NOW, retention_seconds=3600, capacity=10)
        first.close()

        second = SqliteEarthquakeRepository(path)
        second.open()
        try:
            assert not second.claim_event(
                "event-1",
                "a" * 64,
                seen_at=NOW + timedelta(minutes=30),
                retention_seconds=3600,
                capacity=10,
            )
            assert second.claim_event(
                "event-1",
                "a" * 64,
                seen_at=NOW + timedelta(hours=2),
                retention_seconds=3600,
                capacity=10,
            )
        finally:
            second.close()


def test_dedupe_capacity_prunes_oldest_rows() -> None:
    with isolated_workspace_directory() as directory:
        repository = SqliteEarthquakeRepository(directory / "capacity.sqlite3")
        repository.open()
        try:
            for index, digest in enumerate(("a" * 64, "b" * 64, "c" * 64)):
                assert repository.claim_event(
                    f"event-{index}",
                    digest,
                    seen_at=NOW + timedelta(seconds=index),
                    retention_seconds=86_400,
                    capacity=2,
                )
            assert repository.dedupe_count() == 2
            assert repository.claim_event(
                "event-0",
                "a" * 64,
                seen_at=NOW + timedelta(seconds=3),
                retention_seconds=86_400,
                capacity=2,
            )
            assert repository.dedupe_count() == 2
        finally:
            repository.close()
