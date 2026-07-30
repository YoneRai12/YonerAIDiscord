from __future__ import annotations

from datetime import datetime, timezone

from yonerai_discord.modules.modtools import ModAction, ModtoolsRepository

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def test_warning_and_case_are_written_atomically(tmp_path) -> None:
    repository = ModtoolsRepository(tmp_path / "state.sqlite3")
    repository.open()
    try:
        warning = repository.add_warning(
            guild_id=1,
            target_id=20,
            moderator_id=10,
            reason="ルール違反",
            created_at=NOW,
        )
        case = repository.get_case(1, warning.case_id)
        assert case is not None
        assert case.action is ModAction.WARN
        assert case.reason == "ルール違反"
        assert repository.warnings_for(1, 20) == (warning,)
    finally:
        repository.close()


def test_cases_are_isolated_by_guild_and_preserve_metadata(tmp_path) -> None:
    repository = ModtoolsRepository(tmp_path / "state.sqlite3")
    repository.open()
    try:
        item = repository.record_case(
            guild_id=1,
            action=ModAction.PURGE,
            target_id=None,
            moderator_id=10,
            reason="スパム削除",
            metadata={"requested": 20, "deleted": 18},
            created_at=NOW,
        )
        assert repository.get_case(2, item.id) is None
        loaded = repository.get_case(1, item.id)
        assert loaded == item
        assert loaded.metadata["deleted"] == 18
        assert repository.update_case(1, item.id, status="failed", metadata={"deleted": 7})
        updated = repository.get_case(1, item.id)
        assert updated.status == "failed"
        assert updated.metadata == {"deleted": 7}
    finally:
        repository.close()


def test_warning_query_defaults_to_active_only(tmp_path) -> None:
    repository = ModtoolsRepository(tmp_path / "state.sqlite3")
    repository.open()
    try:
        first = repository.add_warning(
            guild_id=1,
            target_id=20,
            moderator_id=10,
            reason="first",
            created_at=NOW,
        )
        second = repository.add_warning(
            guild_id=1,
            target_id=20,
            moderator_id=10,
            reason="second",
            created_at=NOW,
        )
        repository._required().execute("UPDATE modtools_warnings SET active=0 WHERE id=?", (first.id,))
        assert repository.warnings_for(1, 20) == (second,)
        all_warnings = repository.warnings_for(1, 20, active_only=False)
        assert tuple(item.id for item in all_warnings) == (second.id, first.id)
        assert not all_warnings[1].active
    finally:
        repository.close()
