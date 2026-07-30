from __future__ import annotations

from pathlib import Path

import pytest

from yonerai_discord.modules.servertools import GuildServerConfig, SqliteServerToolsRepository


@pytest.fixture
def repository(tmp_path: Path):
    value = SqliteServerToolsRepository(tmp_path / "suite.sqlite3")
    value.open()
    yield value
    value.close()


def test_default_config_stores_no_message_content(repository: SqliteServerToolsRepository) -> None:
    config = repository.get(123)
    assert config == GuildServerConfig(123)
    assert config.audit_include_content is False
    assert config.audit_content_limit == 200


def test_config_round_trip_and_guild_isolation(repository: SqliteServerToolsRepository) -> None:
    repository.set_welcome(1, 10, "{mention} さん、{guild}へようこそ")
    repository.set_goodbye(1, 11, "{name} さんが退出しました")
    repository.set_log_channel(1, 12)
    repository.set_audit_content(1, include=True, limit=80)

    first = repository.get(1)
    assert first.welcome_channel_id == 10
    assert first.goodbye_channel_id == 11
    assert first.log_channel_id == 12
    assert first.audit_include_content is True
    assert first.audit_content_limit == 80
    assert repository.get(2) == GuildServerConfig(2)


def test_config_persists_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persistent.sqlite3"
    first = SqliteServerToolsRepository(path)
    first.open()
    first.set_log_channel(99, 100)
    first.close()
    second = SqliteServerToolsRepository(path)
    second.open()
    try:
        assert second.get(99).log_channel_id == 100
    finally:
        second.close()


def test_invalid_template_and_audit_limit_are_rejected(repository: SqliteServerToolsRepository) -> None:
    with pytest.raises(ValueError):
        repository.set_welcome(1, 2, "")
    with pytest.raises(ValueError):
        repository.set_audit_content(1, include=True, limit=501)


def test_repository_lifecycle_is_idempotent(tmp_path: Path) -> None:
    repository = SqliteServerToolsRepository(tmp_path / "state.sqlite3")
    repository.open()
    repository.open()
    repository.close()
    repository.close()
    with pytest.raises(RuntimeError):
        repository.get(1)
