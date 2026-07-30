from __future__ import annotations

from pathlib import Path

import pytest

from yonerai_discord.modules.automod import (
    AutomodMode,
    GuildAutomodConfig,
    SqliteAutomodRepository,
)


@pytest.fixture
def repository(tmp_path: Path):
    value = SqliteAutomodRepository(tmp_path / "suite.sqlite3")
    value.open()
    yield value
    value.close()


def test_default_is_disabled_report_only_without_content_storage(
    repository: SqliteAutomodRepository,
) -> None:
    config = repository.get(123)
    assert config == GuildAutomodConfig(123)
    assert config.enabled is False
    assert config.mode is AutomodMode.REPORT_ONLY


def test_channel_and_enable_are_guild_isolated(repository: SqliteAutomodRepository) -> None:
    repository.set_report_channel(1, 10)
    repository.set_enabled(1, True)

    assert repository.get(1) == GuildAutomodConfig(1, 10, True)
    assert repository.get(2) == GuildAutomodConfig(2)


def test_enable_requires_channel_and_channel_clear_disables(
    repository: SqliteAutomodRepository,
) -> None:
    with pytest.raises(ValueError, match="report channel"):
        repository.set_enabled(1, True)

    repository.set_report_channel(1, 10)
    repository.set_enabled(1, True)
    cleared = repository.set_report_channel(1, None)
    assert cleared.report_channel_id is None
    assert cleared.enabled is False


@pytest.mark.parametrize("value", [0, -1, True, "1", 1 << 63])
def test_invalid_ids_are_rejected(
    repository: SqliteAutomodRepository,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        repository.get(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        repository.set_report_channel(1, value)  # type: ignore[arg-type]


def test_config_persists_and_schema_has_no_message_table(tmp_path: Path) -> None:
    path = tmp_path / "persistent.sqlite3"
    first = SqliteAutomodRepository(path)
    first.open()
    first.set_report_channel(99, 100)
    first.set_enabled(99, True)
    first.close()

    second = SqliteAutomodRepository(path)
    second.open()
    try:
        assert second.get(99) == GuildAutomodConfig(99, 100, True)
        connection = second._required()
        tables = {
            str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert tables == {"automod_guild_config"}
    finally:
        second.close()


def test_repository_lifecycle_is_idempotent(tmp_path: Path) -> None:
    repository = SqliteAutomodRepository(tmp_path / "state.sqlite3")
    repository.open()
    repository.open()
    repository.close()
    repository.close()
    with pytest.raises(RuntimeError):
        repository.get(1)
