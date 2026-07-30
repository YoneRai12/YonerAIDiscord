from __future__ import annotations

import sqlite3

import pytest

from yonerai_discord.db import MIGRATIONS, Database


def test_migrations_are_idempotent(tmp_path) -> None:
    path = tmp_path / "nested" / "suite.sqlite3"
    database = Database(path)
    database.open()
    try:
        assert database.migrate() == 5
        assert database.migrate() == 5
        assert database.ping()
    finally:
        database.close()

    with sqlite3.connect(path) as connection:
        versions = connection.execute("SELECT version FROM schema_migrations").fetchall()
        assert versions == [(1,), (2,), (3,), (4,), (5,)]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "plugin_state",
            "audit_log",
            "schema_migrations",
            "module_override",
            "capability_override",
            "permission_override",
            "capability_actor_grant",
            "evolution_proposal",
            "media_url_inspection_daily_usage",
            "autonomy_checkpoint_journal",
        } <= tables


def test_database_lifecycle_is_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "suite.sqlite3")
    database.open()
    database.open()
    assert database.is_open
    database.close()
    database.close()
    assert not database.is_open
    with pytest.raises(RuntimeError, match="not open"):
        database.migrate()


def test_online_backup_is_consistent_and_never_overwrites(tmp_path) -> None:
    database = Database(tmp_path / "live" / "suite.sqlite3")
    database.open()
    database.migrate()
    database.append_audit("backup.test", actor_id=1, details={"safe": True})
    destination = tmp_path / "backups" / "suite-copy.sqlite3"
    try:
        assert database.quick_check() == ("ok",)
        assert database.online_backup(destination) == destination.resolve()
        assert destination.is_file()
        with sqlite3.connect(destination) as connection:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert connection.execute("SELECT event FROM audit_log").fetchone() == ("backup.test",)
        with pytest.raises(FileExistsError):
            database.online_backup(destination)
        with pytest.raises(ValueError, match="differ"):
            database.online_backup(database.path)
    finally:
        database.close()


def test_existing_v1_database_upgrades_to_current_schema(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    database = Database(path)
    database.open()
    try:
        assert database.migrate((MIGRATIONS[0],)) == 1
        database.append_audit("legacy.created", actor_id=1, details={"source": "v1"})
        assert database.migrate() == 5
        assert database.list_audit()[0].details == {"source": "v1"}
    finally:
        database.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
        ]


def test_media_url_inspection_daily_quota_is_reserved_before_the_limit(tmp_path) -> None:
    path = tmp_path / "quota.sqlite3"
    database = Database(path)
    database.open()
    database.migrate()
    try:
        assert database.reserve_media_url_inspection_call(0) is False
        assert database.reserve_media_url_inspection_call(2) is True
        assert database.reserve_media_url_inspection_call(2) is True
        assert database.reserve_media_url_inspection_call(2) is False
    finally:
        database.close()

    reopened = Database(path)
    reopened.open()
    reopened.migrate()
    try:
        assert reopened.reserve_media_url_inspection_call(2) is False
    finally:
        reopened.close()
