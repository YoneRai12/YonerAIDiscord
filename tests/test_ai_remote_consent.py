from __future__ import annotations

import sqlite3

import pytest

from yonerai_discord.modules.ai.remote_consent import DEFAULT_DISCLOSURE_VERSION, RemoteConsentStore
from yonerai_discord.modules.ai.state_repository import AIStateRepository


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def test_default_disclosure_version_requires_the_tool_evidence_disclosure() -> None:
    assert DEFAULT_DISCLOSURE_VERSION == "remote-ai-disclosure-v2"


def test_consent_is_global_per_user_but_other_users_stay_unconsented() -> None:
    clock = Clock()
    store = RemoteConsentStore(max_grants=4, clock=clock)
    store.grant(guild_id=1, channel_id=2, user_id=3)

    assert store.active(guild_id=1, channel_id=2, user_id=3) is True
    assert store.active(guild_id=9, channel_id=2, user_id=3) is True
    assert store.active(guild_id=1, channel_id=9, user_id=3) is True
    assert store.active(guild_id=1, channel_id=2, user_id=9) is False


def test_default_consent_has_no_expiry_and_reports_none_ttl() -> None:
    clock = Clock()
    store = RemoteConsentStore(max_grants=4, clock=clock)
    store.grant(guild_id=1, channel_id=2, user_id=3)
    clock.value += 10_000_000

    assert store.active(guild_id=9, channel_id=8, user_id=3) is True
    assert store.stats().ttl_seconds is None


def test_explicit_compatibility_ttl_still_expires() -> None:
    clock = Clock()
    store = RemoteConsentStore(ttl_seconds=60, max_grants=4, clock=clock)
    store.grant(guild_id=1, channel_id=2, user_id=3)
    clock.value += 60
    assert store.active(guild_id=1, channel_id=2, user_id=3) is False


def test_consent_capacity_is_bounded_and_oldest_grant_fails_closed() -> None:
    store = RemoteConsentStore(ttl_seconds=60, max_grants=2)

    store.grant(guild_id=1, channel_id=1, user_id=1)
    store.grant(guild_id=1, channel_id=1, user_id=2)
    store.grant(guild_id=1, channel_id=1, user_id=3)

    assert store.active(guild_id=1, channel_id=1, user_id=1) is False
    assert store.active(guild_id=1, channel_id=1, user_id=2) is True
    assert store.active(guild_id=1, channel_id=1, user_id=3) is True
    assert store.stats().active_grants == 2


def test_revoke_clear_and_stats_never_expose_scope_ids() -> None:
    store = RemoteConsentStore(ttl_seconds=60, max_grants=2)
    store.grant(guild_id=123_456, channel_id=234_567, user_id=345_678)

    stats = store.stats()
    assert (stats.active_grants, stats.capacity, stats.ttl_seconds) == (1, 2, 60)
    assert all(str(value) not in repr(stats) for value in (123_456, 234_567, 345_678))
    assert store.revoke(guild_id=123_456, channel_id=234_567, user_id=345_678) is True
    assert store.revoke(guild_id=123_456, channel_id=234_567, user_id=345_678) is False
    store.grant(guild_id=1, channel_id=2, user_id=3)
    store.clear()
    assert store.stats().active_grants == 0


def test_consent_survives_reopen_until_revoke(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    first = RemoteConsentStore(database_path=path)
    first.grant(guild_id=1, channel_id=2, user_id=345_678)
    first.close()

    second = RemoteConsentStore(database_path=path)
    assert second.active(guild_id=99, channel_id=88, user_id=345_678) is True
    assert second.active(guild_id=1, channel_id=2, user_id=345_679) is False
    assert second.revoke(guild_id=99, channel_id=88, user_id=345_678) is True
    second.close()

    third = RemoteConsentStore(database_path=path)
    assert third.active(guild_id=1, channel_id=2, user_id=345_678) is False
    third.close()


def test_expired_consent_cannot_revive_after_clock_rollback_and_restart(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    clock = Clock()
    first = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=clock)
    first.grant(guild_id=1, channel_id=2, user_id=3)
    first.close()

    clock.value = 160.0
    expired = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=clock)
    assert expired.active(guild_id=1, channel_id=2, user_id=3) is False
    expired.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_remote_consent WHERE user_id = 3").fetchone() == (0,)

    clock.value = 120.0
    rolled_back = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=clock)
    assert rolled_back.active(guild_id=1, channel_id=2, user_id=3) is False
    rolled_back.close()


def test_ttl_consent_is_deleted_when_wall_clock_rolls_back_in_same_process(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    clock = Clock()
    clock.value = 1_000.0
    store = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=clock)
    store.grant(guild_id=1, channel_id=2, user_id=3)
    clock.value = 1_050.0
    assert store.active(guild_id=1, channel_id=2, user_id=3) is True

    clock.value = 1_040.0
    assert store.active(guild_id=1, channel_id=2, user_id=3) is False
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_remote_consent WHERE user_id = 3").fetchone() == (0,)
    store.close()


def test_process_only_ttl_consent_fails_closed_on_same_process_clock_rollback() -> None:
    clock = Clock()
    clock.value = 1_000.0
    store = RemoteConsentStore(ttl_seconds=60, clock=clock)
    store.grant(guild_id=1, channel_id=2, user_id=3)
    clock.value = 1_050.0
    assert store.active(guild_id=1, channel_id=2, user_id=3) is True

    clock.value = 1_040.0
    assert store.active(guild_id=1, channel_id=2, user_id=3) is False


def test_ttl_consent_is_deleted_when_clock_rolls_back_across_restart(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    clock = Clock()
    clock.value = 1_000.0
    first = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=clock)
    first.grant(guild_id=1, channel_id=2, user_id=3)
    clock.value = 1_050.0
    assert first.active(guild_id=1, channel_id=2, user_id=3) is True
    first.close()

    clock.value = 1_040.0
    reopened = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=clock)
    assert reopened.active(guild_id=1, channel_id=2, user_id=3) is False
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_remote_consent WHERE user_id = 3").fetchone() == (0,)
    reopened.close()


def test_clock_rollback_in_one_store_invalidates_other_store_cache(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    first_clock = Clock()
    second_clock = Clock()
    first_clock.value = second_clock.value = 1_000.0
    first = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=first_clock)
    first.grant(guild_id=1, channel_id=2, user_id=3)
    second = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=second_clock)

    first_clock.value = 1_050.0
    second_clock.value = 1_050.0
    assert first.active(guild_id=1, channel_id=2, user_id=3) is True
    assert second.active(guild_id=1, channel_id=2, user_id=3) is True

    second_clock.value = 1_040.0
    assert second.active(guild_id=1, channel_id=2, user_id=3) is False
    assert first.active(guild_id=1, channel_id=2, user_id=3) is False
    first.close()
    second.close()


def test_ttl_grant_is_not_inserted_when_clock_is_behind_persisted_floor(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    forward_clock = Clock()
    forward_clock.value = 1_050.0
    forward = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=forward_clock)
    assert forward.active(guild_id=1, channel_id=2, user_id=99) is False
    forward.close()

    rolled_back_clock = Clock()
    rolled_back_clock.value = 1_040.0
    rolled_back = RemoteConsentStore(
        database_path=path,
        ttl_seconds=60,
        clock=rolled_back_clock,
    )
    with pytest.raises(RuntimeError, match="wall clock is rolled back"):
        rolled_back.grant(guild_id=1, channel_id=2, user_id=3)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_remote_consent WHERE user_id = 3").fetchone() == (0,)
    rolled_back.close()


def test_clock_floor_migration_deletes_legacy_ttl_rows_once(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE ai_remote_consent (
                user_id INTEGER PRIMARY KEY,
                policy_version TEXT NOT NULL,
                disclosure_version TEXT NOT NULL,
                granted_at REAL NOT NULL,
                expires_at REAL
            )
            """
        )
        connection.execute(
            "INSERT INTO ai_remote_consent VALUES (?, ?, ?, ?, ?)",
            (3, "remote-ai-policy-v1", "remote-ai-disclosure-v1", 1_000.0, 1_060.0),
        )
        connection.execute(
            "INSERT INTO ai_remote_consent VALUES (?, ?, ?, ?, ?)",
            (4, "remote-ai-policy-v1", "remote-ai-disclosure-v1", 1_000.0, None),
        )

    repository = AIStateRepository(path)
    assert repository.get_consent(3) is None
    assert repository.get_consent(4) is not None
    repository.close()

    reopened = AIStateRepository(path)
    assert reopened.get_consent(4) is not None
    reopened.close()


def test_non_expiring_consent_survives_observed_clock_rollback(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    clock = Clock()
    clock.value = 1_000.0
    first = RemoteConsentStore(database_path=path, clock=clock)
    first.grant(guild_id=1, channel_id=2, user_id=3)
    clock.value = 1_050.0
    assert first.active(guild_id=1, channel_id=2, user_id=3) is True
    first.close()

    clock.value = 1_040.0
    reopened = RemoteConsentStore(database_path=path, clock=clock)
    assert reopened.active(guild_id=1, channel_id=2, user_id=3) is True
    reopened.close()


def test_stale_ttl_cache_cannot_delete_new_non_expiring_grant_on_rollback(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    ttl_clock = Clock()
    durable_clock = Clock()
    ttl_clock.value = durable_clock.value = 1_000.0
    ttl_store = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=ttl_clock)
    ttl_store.grant(guild_id=1, channel_id=2, user_id=3)
    ttl_clock.value = 1_050.0
    assert ttl_store.active(guild_id=1, channel_id=2, user_id=3) is True

    durable_clock.value = 1_050.0
    durable_store = RemoteConsentStore(database_path=path, clock=durable_clock)
    durable_store.grant(guild_id=1, channel_id=2, user_id=3)

    ttl_clock.value = 1_040.0
    assert ttl_store.active(guild_id=1, channel_id=2, user_id=3) is True
    assert durable_store.active(guild_id=1, channel_id=2, user_id=3) is True
    ttl_store.close()
    durable_store.close()


def test_stale_expired_cache_cannot_delete_new_non_expiring_grant(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    ttl_clock = Clock()
    durable_clock = Clock()
    ttl_clock.value = durable_clock.value = 1_000.0
    ttl_store = RemoteConsentStore(database_path=path, ttl_seconds=60, clock=ttl_clock)
    ttl_store.grant(guild_id=1, channel_id=2, user_id=3)

    durable_store = RemoteConsentStore(database_path=path, clock=durable_clock)
    durable_store.grant(guild_id=1, channel_id=2, user_id=3)
    ttl_clock.value = 1_061.0

    assert ttl_store.active(guild_id=1, channel_id=2, user_id=3) is True
    assert durable_store.active(guild_id=1, channel_id=2, user_id=3) is True
    ttl_store.close()
    durable_store.close()


@pytest.mark.parametrize("invalid_now", [float("nan"), float("inf"), -1.0])
def test_invalid_persistent_consent_clock_fails_closed(tmp_path, invalid_now: float) -> None:
    clock = Clock()
    clock.value = invalid_now
    with pytest.raises(RuntimeError, match="clock"):
        RemoteConsentStore(database_path=tmp_path / "suite.sqlite3", clock=clock)


def test_policy_or_disclosure_version_change_fails_closed(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    original = RemoteConsentStore(
        database_path=path,
        policy_version="policy-v1",
        disclosure_version="disclosure-v1",
    )
    original.grant(guild_id=1, channel_id=2, user_id=3)
    original.close()

    changed_policy = RemoteConsentStore(
        database_path=path,
        policy_version="policy-v2",
        disclosure_version="disclosure-v1",
    )
    assert changed_policy.active(guild_id=1, channel_id=2, user_id=3) is False
    changed_policy.close()

    changed_disclosure = RemoteConsentStore(
        database_path=path,
        policy_version="policy-v1",
        disclosure_version="disclosure-v2",
    )
    assert changed_disclosure.active(guild_id=1, channel_id=2, user_id=3) is False
    changed_disclosure.close()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ttl_seconds": 59}, "ttl_seconds"),
        ({"max_grants": 0}, "max_grants"),
    ],
)
def test_invalid_store_bounds_are_rejected(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        RemoteConsentStore(**kwargs)
