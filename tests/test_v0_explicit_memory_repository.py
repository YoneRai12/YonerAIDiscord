from __future__ import annotations

from yonerai_discord.modules.ai.state_repository import AIStateRepository
from yonerai_discord.v0_contracts import MemoryVisibility, Scope
from yonerai_discord.v0_runtime.memory_repository import V0ExplicitMemoryRepository


def _repository(tmp_path):
    state = AIStateRepository(tmp_path / "state.sqlite3")
    return state, V0ExplicitMemoryRepository(state, clock=lambda: 1_000)


def test_explicit_memory_round_trips_dm_none_through_sqlite_sentinel(tmp_path) -> None:
    state, repository = _repository(tmp_path)
    scope = Scope(None, 10, dm_channel_id=30, visibility=MemoryVisibility.DIRECT_MESSAGE)
    try:
        created = repository.remember(scope, "DMだけの明示メモ")
        loaded = repository.list(scope)
    finally:
        state.close()

    assert loaded == (created,)
    assert loaded[0].scope.guild_id is None
    assert loaded[0].explicit is True


def test_explicit_memory_is_exactly_isolated_by_user_visibility_channel_and_dm(tmp_path) -> None:
    state, repository = _repository(tmp_path)
    scopes = (
        Scope(1, 10),
        Scope(1, 11),
        Scope(1, 10, channel_id=20, visibility=MemoryVisibility.CHANNEL_SHARED),
        Scope(1, 10, visibility=MemoryVisibility.GUILD_PUBLIC),
        Scope(None, 10, dm_channel_id=30, visibility=MemoryVisibility.DIRECT_MESSAGE),
    )
    try:
        for index, scope in enumerate(scopes):
            repository.remember(scope, f"scope-{index}")
        loaded = tuple(repository.list(scope) for scope in scopes)
    finally:
        state.close()

    assert [[record.content for record in records] for records in loaded] == [
        ["scope-0"],
        ["scope-1"],
        ["scope-2"],
        ["scope-3"],
        ["scope-4"],
    ]


def test_forget_and_clear_cannot_cross_scope_and_legacy_tables_are_untouched(tmp_path) -> None:
    state, repository = _repository(tmp_path)
    owner = Scope(1, 10)
    other = Scope(2, 10)
    try:
        first = repository.remember(owner, "one")
        repository.remember(owner, "two")
        assert repository.forget(other, first.memory_id) is False
        assert repository.forget(owner, first.memory_id) is True
        assert repository.clear(other) == 0
        assert repository.clear(owner) == 1
        assert state.load_conversations() == ()
    finally:
        state.close()


def test_clear_all_user_scopes_removes_old_privacy_rows_only_for_same_actor_and_guild(tmp_path) -> None:
    state, repository = _repository(tmp_path)
    owned = (
        Scope(1, 10),
        Scope(1, 10, channel_id=20, visibility=MemoryVisibility.CHANNEL_SHARED),
        Scope(1, 10, visibility=MemoryVisibility.GUILD_PUBLIC),
    )
    other_user = Scope(1, 11)
    other_guild = Scope(2, 10)
    try:
        for index, scope in enumerate((*owned, other_user, other_guild)):
            repository.remember(scope, f"scope-{index}")

        assert repository.clear_all_user_scopes(owned[0]) == 3
        assert all(repository.list(scope) == () for scope in owned)
        assert len(repository.list(other_user)) == 1
        assert len(repository.list(other_guild)) == 1
    finally:
        state.close()


def test_dm_clear_is_limited_to_actor_and_current_dm_channel(tmp_path) -> None:
    state, repository = _repository(tmp_path)
    active = Scope(None, 10, dm_channel_id=30, visibility=MemoryVisibility.DIRECT_MESSAGE)
    other_dm = Scope(None, 10, dm_channel_id=31, visibility=MemoryVisibility.DIRECT_MESSAGE)
    other_user = Scope(None, 11, dm_channel_id=30, visibility=MemoryVisibility.DIRECT_MESSAGE)
    try:
        for index, scope in enumerate((active, other_dm, other_user)):
            repository.remember(scope, f"dm-{index}")

        assert repository.clear_all_user_scopes(active) == 1
        assert repository.list(active) == ()
        assert len(repository.list(other_dm)) == 1
        assert len(repository.list(other_user)) == 1
    finally:
        state.close()


def test_memory_authorization_token_contains_no_body_and_stays_current(tmp_path) -> None:
    state, repository = _repository(tmp_path)
    scope = Scope(None, 10, dm_channel_id=30, visibility=MemoryVisibility.DIRECT_MESSAGE)
    try:
        record = repository.remember(scope, "tokenへ本文を載せない")
        token = repository.authorization_token(scope, (record,), request_channel_id=30)

        assert token is not None
        assert "tokenへ本文を載せない" not in repr(token)
        assert token.records[0].memory_id == record.memory_id
        assert repository.authorization_current(token) is True
    finally:
        state.close()


def test_memory_authorization_token_is_revoked_by_forget_privacy_and_expiry(tmp_path) -> None:
    state = AIStateRepository(tmp_path / "state.sqlite3")
    now = [1_000]
    repository = V0ExplicitMemoryRepository(state, clock=lambda: now[0], retention_seconds=60)
    guild_scope = Scope(1, 10, visibility=MemoryVisibility.GUILD_PUBLIC)
    dm_scope = Scope(None, 10, dm_channel_id=30, visibility=MemoryVisibility.DIRECT_MESSAGE)
    try:
        repository.set_privacy(
            guild_id=1,
            user_id=10,
            visibility=MemoryVisibility.GUILD_PUBLIC,
            channel_id=None,
        )
        guild_record = repository.remember(guild_scope, "guild")
        guild_token = repository.authorization_token(guild_scope, (guild_record,), request_channel_id=20)
        assert guild_token is not None
        repository.set_privacy(
            guild_id=1,
            user_id=10,
            visibility=MemoryVisibility.USER_PRIVATE,
            channel_id=None,
        )
        assert repository.authorization_current(guild_token) is False

        dm_record = repository.remember(dm_scope, "dm")
        forgotten_token = repository.authorization_token(dm_scope, (dm_record,), request_channel_id=30)
        assert forgotten_token is not None
        assert repository.forget(dm_scope, dm_record.memory_id) is True
        assert repository.authorization_current(forgotten_token) is False

        expiring_record = repository.remember(dm_scope, "expires")
        expiring_token = repository.authorization_token(dm_scope, (expiring_record,), request_channel_id=30)
        assert expiring_token is not None
        now[0] += 61
        assert repository.authorization_current(expiring_token) is False
        now[0] -= 31
        assert repository.authorization_current(expiring_token) is False
        assert repository.list(dm_scope) == ()
    finally:
        state.close()


def test_memory_expiry_clock_floor_survives_repository_restart(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    now = [1_000]
    first_state = AIStateRepository(path)
    first = V0ExplicitMemoryRepository(first_state, clock=lambda: now[0], retention_seconds=60)
    scope = Scope(None, 10, dm_channel_id=30, visibility=MemoryVisibility.DIRECT_MESSAGE)
    record = first.remember(scope, "cannot return after expiry")
    token = first.authorization_token(scope, (record,), request_channel_id=30)
    assert token is not None
    now[0] = 1_061
    assert first.authorization_current(token) is False
    first_state.close()

    now[0] = 1_030
    second_state = AIStateRepository(path)
    try:
        second = V0ExplicitMemoryRepository(second_state, clock=lambda: now[0], retention_seconds=60)
        assert second.authorization_current(token) is False
        assert second.list(scope) == ()
    finally:
        second_state.close()


def test_memory_authorization_token_detects_same_id_content_revision_change(tmp_path) -> None:
    state, repository = _repository(tmp_path)
    scope = Scope(None, 10, dm_channel_id=30, visibility=MemoryVisibility.DIRECT_MESSAGE)
    try:
        record = repository.remember(scope, "before")
        token = repository.authorization_token(scope, (record,), request_channel_id=30)
        assert token is not None
        state.v0_connection().execute(
            "UPDATE v0_explicit_memory SET content = ? WHERE memory_id = ?",
            ("after", record.memory_id),
        )
        assert repository.authorization_current(token) is False
    finally:
        state.close()


def test_memory_authorization_token_detects_privacy_change_even_if_value_is_restored(tmp_path) -> None:
    state, repository = _repository(tmp_path)
    scope = Scope(1, 10, visibility=MemoryVisibility.GUILD_PUBLIC)
    try:
        repository.set_privacy(
            guild_id=1,
            user_id=10,
            visibility=MemoryVisibility.GUILD_PUBLIC,
            channel_id=None,
        )
        record = repository.remember(scope, "public")
        token = repository.authorization_token(scope, (record,), request_channel_id=20)
        assert token is not None

        repository.set_privacy(
            guild_id=1,
            user_id=10,
            visibility=MemoryVisibility.USER_PRIVATE,
            channel_id=None,
        )
        repository.set_privacy(
            guild_id=1,
            user_id=10,
            visibility=MemoryVisibility.GUILD_PUBLIC,
            channel_id=None,
        )

        assert repository.recall_scope(guild_id=1, channel_id=20, user_id=10) == scope
        assert repository.authorization_current(token) is False
    finally:
        state.close()
