from __future__ import annotations

import asyncio

import pytest

from yonerai_discord.modules.ai.conversation import (
    ConversationIndexConflictError,
    ConversationSessionError,
    ConversationStore,
)
from yonerai_discord.modules.ai.models import Attachment, AttachmentKind, MessageRole


def _file(data: bytes, filename: str = "note.txt") -> Attachment:
    return Attachment(
        kind=AttachmentKind.FILE,
        data=data,
        mime_type="text/plain",
        filename=filename,
    )


@pytest.mark.asyncio
async def test_start_always_resets_to_a_new_session_and_drops_reply_index() -> None:
    store = ConversationStore()
    first = await store.start(guild_id=1, channel_id=2, user_id=3)
    await store.append_exchange(
        session_id=first.session_id,
        guild_id=1,
        channel_id=2,
        user_id=3,
        user_text="hello",
        assistant_text="hi",
        bot_message_id=100,
    )

    second = await store.start(guild_id=1, channel_id=2, user_id=3)

    assert second.session_id != first.session_id
    assert second.history == ()
    assert await store.resolve(bot_message_id=100, guild_id=1, channel_id=2, user_id=3) is None
    with pytest.raises(ConversationSessionError):
        await store.append_exchange(
            session_id=first.session_id,
            guild_id=1,
            channel_id=2,
            user_id=3,
            user_text="stale",
            assistant_text="must not be mixed",
        )


@pytest.mark.asyncio
async def test_drop_removes_session_binary_history_and_reply_index_without_recreating_session() -> None:
    store = ConversationStore()
    session = await store.start(guild_id=1, channel_id=2, user_id=3)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=1,
        channel_id=2,
        user_id=3,
        user_text="hello",
        assistant_text="hi",
        attachments=(_file(b"private"),),
        bot_message_id=101,
    )

    assert await store.drop(guild_id=1, channel_id=2, user_id=3) is True
    assert await store.drop(guild_id=1, channel_id=2, user_id=3) is False
    assert await store.get(guild_id=1, channel_id=2, user_id=3) is None
    assert await store.resolve(bot_message_id=101, guild_id=1, channel_id=2, user_id=3) is None
    stats = await store.stats()
    assert stats.session_count == 0
    assert stats.binary_bytes == 0


@pytest.mark.asyncio
async def test_drop_user_removes_all_scopes_and_reply_indices_for_that_user() -> None:
    store = ConversationStore()
    first = await store.start(guild_id=1, channel_id=2, user_id=3)
    second = await store.start(guild_id=4, channel_id=5, user_id=3)
    other = await store.start(guild_id=1, channel_id=2, user_id=9)
    await store.append_exchange(
        session_id=first.session_id,
        guild_id=1,
        channel_id=2,
        user_id=3,
        user_text="a",
        assistant_text="b",
        bot_message_id=101,
    )
    await store.append_exchange(
        session_id=second.session_id,
        guild_id=4,
        channel_id=5,
        user_id=3,
        user_text="c",
        assistant_text="d",
        bot_message_id=102,
    )
    await store.append_exchange(
        session_id=other.session_id,
        guild_id=1,
        channel_id=2,
        user_id=9,
        user_text="keep",
        assistant_text="me",
        bot_message_id=103,
    )

    assert await store.drop_user(user_id=3) == 2
    assert await store.resolve(bot_message_id=101, guild_id=1, channel_id=2, user_id=3) is None
    assert await store.resolve(bot_message_id=102, guild_id=4, channel_id=5, user_id=3) is None
    assert await store.resolve(bot_message_id=103, guild_id=1, channel_id=2, user_id=9) is not None
    assert await store.drop_user(user_id=3) == 0


@pytest.mark.asyncio
async def test_reply_resolution_requires_same_owner_guild_and_channel() -> None:
    store = ConversationStore()
    session = await store.get_or_start(guild_id=10, channel_id=20, user_id=30)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=10,
        channel_id=20,
        user_id=30,
        user_text="question",
        assistant_text="answer",
        bot_message_id=999,
    )

    resolved = await store.resolve(bot_message_id=999, guild_id=10, channel_id=20, user_id=30)

    assert resolved is not None
    assert resolved.session_id == session.session_id
    assert [turn.role for turn in resolved.history] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert [turn.text for turn in resolved.history] == ["question", "answer"]
    assert await store.resolve(bot_message_id=999, guild_id=10, channel_id=20, user_id=31) is None
    assert await store.resolve(bot_message_id=999, guild_id=10, channel_id=21, user_id=30) is None
    assert await store.resolve(bot_message_id=999, guild_id=11, channel_id=20, user_id=30) is None


@pytest.mark.asyncio
async def test_exact_reply_detach_preserves_history_other_index_and_reopen(tmp_path) -> None:
    path = tmp_path / "conversation.sqlite3"
    store = ConversationStore(database_path=path)
    session = await store.start(guild_id=10, channel_id=20, user_id=30)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=10,
        channel_id=20,
        user_id=30,
        user_text="first question",
        assistant_text="first answer",
        bot_message_id=999,
    )
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=10,
        channel_id=20,
        user_id=30,
        user_text="second question",
        assistant_text="second answer",
        bot_message_id=1_000,
    )
    current = store._sessions[session.key]  # noqa: SLF001
    timestamps = (current.created_at, current.updated_at, current.access_order)

    assert await store.detach_bot_message_reference_if_current(
        bot_message_id=999,
        guild_id=10,
        channel_id=20,
        user_id=30,
        session_id=session.session_id,
    )
    assert (current.created_at, current.updated_at, current.access_order) == timestamps
    assert current.exchanges[0].bot_message_id is None
    assert current.exchanges[1].bot_message_id == 1_000
    assert [turn.text for turn in store._snapshot(current).history] == [  # noqa: SLF001
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]
    store.close()

    reopened = ConversationStore(database_path=path)
    assert await reopened.resolve(bot_message_id=999, guild_id=10, channel_id=20, user_id=30) is None
    other = await reopened.resolve(bot_message_id=1_000, guild_id=10, channel_id=20, user_id=30)
    assert other is not None
    assert [turn.text for turn in other.history] == [
        "first question",
        "first answer",
        "second question",
        "second answer",
    ]
    reopened.close()


@pytest.mark.asyncio
async def test_reply_detach_wrong_scope_or_session_is_noop() -> None:
    store = ConversationStore()
    session = await store.start(guild_id=10, channel_id=20, user_id=30)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=10,
        channel_id=20,
        user_id=30,
        user_text="question",
        assistant_text="answer",
        bot_message_id=999,
    )

    for changes in (
        {"guild_id": 11},
        {"channel_id": 21},
        {"user_id": 31},
        {"session_id": "other-session"},
    ):
        parameters = {
            "bot_message_id": 999,
            "guild_id": 10,
            "channel_id": 20,
            "user_id": 30,
            "session_id": session.session_id,
            **changes,
        }
        assert not await store.detach_bot_message_reference_if_current(**parameters)

    assert await store.resolve(bot_message_id=999, guild_id=10, channel_id=20, user_id=30) is not None


@pytest.mark.asyncio
async def test_reply_detach_persist_failure_restores_memory_state() -> None:
    store = ConversationStore()
    session = await store.start(guild_id=10, channel_id=20, user_id=30)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=10,
        channel_id=20,
        user_id=30,
        user_text="question",
        assistant_text="answer",
        bot_message_id=999,
    )
    original_persist = store._persist_locked  # noqa: SLF001

    def fail_persist() -> None:
        raise RuntimeError("simulated persistence failure")

    store._persist_locked = fail_persist  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(RuntimeError, match="simulated persistence failure"):
        await store.detach_bot_message_reference_if_current(
            bot_message_id=999,
            guild_id=10,
            channel_id=20,
            user_id=30,
            session_id=session.session_id,
        )
    store._persist_locked = original_persist  # type: ignore[method-assign]  # noqa: SLF001

    assert await store.resolve(bot_message_id=999, guild_id=10, channel_id=20, user_id=30) is not None


@pytest.mark.asyncio
async def test_active_reference_probe_never_touches_persists_or_cleans_expired_state() -> None:
    now = [100.0]
    store = ConversationStore(ttl_seconds=10, clock=lambda: now[0])
    session = await store.start(guild_id=10, channel_id=20, user_id=30)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=10,
        channel_id=20,
        user_id=30,
        user_text="question",
        assistant_text="answer",
        bot_message_id=999,
    )
    live_session = store._sessions[session.key]  # noqa: SLF001
    access_counter = store._access_counter  # noqa: SLF001
    access_order = live_session.access_order
    updated_at = live_session.updated_at
    persist_calls = 0

    def record_persist() -> None:
        nonlocal persist_calls
        persist_calls += 1

    store._persist_locked = record_persist  # type: ignore[method-assign]  # noqa: SLF001

    assert (
        await store.is_active_reference(
            bot_message_id=999,
            guild_id=10,
            channel_id=20,
            user_id=30,
        )
        is True
    )
    assert store._access_counter == access_counter  # noqa: SLF001
    assert live_session.access_order == access_order
    assert live_session.updated_at == updated_at
    assert persist_calls == 0

    now[0] += 10
    assert (
        await store.is_active_reference(
            bot_message_id=999,
            guild_id=10,
            channel_id=20,
            user_id=30,
        )
        is False
    )
    assert session.key in store._sessions  # noqa: SLF001
    assert 999 in store._message_index  # noqa: SLF001
    assert store._access_counter == access_counter  # noqa: SLF001
    assert live_session.access_order == access_order
    assert live_session.updated_at == updated_at
    assert persist_calls == 0

    assert await store.resolve(bot_message_id=999, guild_id=10, channel_id=20, user_id=30) is None
    assert session.key not in store._sessions  # noqa: SLF001
    assert 999 not in store._message_index  # noqa: SLF001
    assert persist_calls == 1


@pytest.mark.asyncio
async def test_active_scope_probe_is_touchless_and_resolve_renews_exact_scope() -> None:
    now = [100.0]
    store = ConversationStore(ttl_seconds=10, clock=lambda: now[0])
    session = await store.start(guild_id=10, channel_id=20, user_id=30)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=10,
        channel_id=20,
        user_id=30,
        user_text="question",
        assistant_text="answer",
        bot_message_id=999,
    )
    live_session = store._sessions[session.key]  # noqa: SLF001
    access_counter = store._access_counter  # noqa: SLF001
    access_order = live_session.access_order
    updated_at = live_session.updated_at
    persist_calls = 0

    def record_persist() -> None:
        nonlocal persist_calls
        persist_calls += 1

    store._persist_locked = record_persist  # type: ignore[method-assign]  # noqa: SLF001

    assert await store.is_active_scope(guild_id=10, channel_id=20, user_id=30) is True
    assert store._access_counter == access_counter  # noqa: SLF001
    assert live_session.access_order == access_order
    assert live_session.updated_at == updated_at
    assert persist_calls == 0

    now[0] += 9
    resolved = await store.resolve_active(guild_id=10, channel_id=20, user_id=30)
    assert resolved is not None
    assert resolved.session_id == session.session_id
    assert [turn.text for turn in resolved.history] == ["question", "answer"]
    assert store._access_counter == access_counter + 1  # noqa: SLF001
    assert live_session.access_order == access_order + 1
    assert live_session.updated_at == 109.0
    assert persist_calls == 1

    assert await store.is_active_scope(guild_id=10, channel_id=20, user_id=31) is False
    assert await store.resolve_active(guild_id=10, channel_id=21, user_id=30) is None
    assert await store.resolve_active(guild_id=11, channel_id=20, user_id=30) is None
    assert persist_calls == 3

    now[0] += 9
    assert await store.is_active_scope(guild_id=10, channel_id=20, user_id=30) is True
    now[0] += 1
    assert await store.is_active_scope(guild_id=10, channel_id=20, user_id=30) is False
    assert await store.resolve_active(guild_id=10, channel_id=20, user_id=30) is None
    assert session.key not in store._sessions  # noqa: SLF001
    assert 999 not in store._message_index  # noqa: SLF001
    assert persist_calls == 4


@pytest.mark.asyncio
async def test_ttl_expires_session_and_reply_index() -> None:
    now = [100.0]
    store = ConversationStore(ttl_seconds=10, clock=lambda: now[0])
    session = await store.start(guild_id=1, channel_id=2, user_id=3)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=1,
        channel_id=2,
        user_id=3,
        user_text="question",
        assistant_text="answer",
        bot_message_id=100,
    )

    now[0] += 10

    assert await store.resolve(bot_message_id=100, guild_id=1, channel_id=2, user_id=3) is None
    assert await store.get(guild_id=1, channel_id=2, user_id=3) is None


@pytest.mark.asyncio
async def test_sessions_are_isolated_by_full_conversation_key() -> None:
    store = ConversationStore()
    first = await store.start(guild_id=1, channel_id=2, user_id=3)
    second = await store.start(guild_id=1, channel_id=2, user_id=4)
    third = await store.start(guild_id=1, channel_id=5, user_id=3)
    for index, snapshot in enumerate((first, second, third), start=1):
        await store.append_exchange(
            session_id=snapshot.session_id,
            guild_id=snapshot.guild_id,
            channel_id=snapshot.channel_id,
            user_id=snapshot.user_id,
            user_text=f"user-{index}",
            assistant_text=f"assistant-{index}",
        )

    assert (await store.get(guild_id=1, channel_id=2, user_id=3)).history[0].text == "user-1"  # type: ignore[union-attr]
    assert (await store.get(guild_id=1, channel_id=2, user_id=4)).history[0].text == "user-2"  # type: ignore[union-attr]
    assert (await store.get(guild_id=1, channel_id=5, user_id=3)).history[0].text == "user-3"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_turn_pruning_removes_old_content_and_old_reply_index() -> None:
    store = ConversationStore(max_turns=2)
    session = await store.start(guild_id=1, channel_id=2, user_id=3)
    for index in range(3):
        await store.append_exchange(
            session_id=session.session_id,
            guild_id=1,
            channel_id=2,
            user_id=3,
            user_text=f"u{index}",
            assistant_text=f"a{index}",
            bot_message_id=100 + index,
        )

    snapshot = await store.get(guild_id=1, channel_id=2, user_id=3)

    assert snapshot is not None
    assert [turn.text for turn in snapshot.history] == ["u1", "a1", "u2", "a2"]
    assert await store.resolve(bot_message_id=100, guild_id=1, channel_id=2, user_id=3) is None
    assert await store.resolve(bot_message_id=101, guild_id=1, channel_id=2, user_id=3) is not None


@pytest.mark.asyncio
async def test_text_and_binary_budgets_prune_oldest_complete_exchange() -> None:
    store = ConversationStore(
        max_turns=12,
        max_text_chars=12,
        max_attachment_bytes=4,
        max_binary_bytes=4,
    )
    session = await store.start(guild_id=1, channel_id=2, user_id=3)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=1,
        channel_id=2,
        user_id=3,
        user_text="old",
        assistant_text="old",
        attachments=(_file(b"1234", "old.txt"),),
        bot_message_id=100,
    )
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=1,
        channel_id=2,
        user_id=3,
        user_text="new",
        assistant_text="new",
        attachments=(_file(b"x", "new.txt"),),
        bot_message_id=101,
    )

    snapshot = await store.get(guild_id=1, channel_id=2, user_id=3)

    assert snapshot is not None
    assert [turn.text for turn in snapshot.history] == ["new", "new"]
    assert await store.resolve(bot_message_id=100, guild_id=1, channel_id=2, user_id=3) is None


@pytest.mark.asyncio
async def test_configured_attachment_limits_are_enforced() -> None:
    store = ConversationStore(
        max_attachment_bytes=3,
        max_binary_bytes=4,
        max_attachments_per_turn=1,
    )
    session = await store.start(guild_id=1, channel_id=2, user_id=3)

    with pytest.raises(ValueError, match="per-file"):
        await store.append_exchange(
            session_id=session.session_id,
            guild_id=1,
            channel_id=2,
            user_id=3,
            user_text="q",
            assistant_text="a",
            attachments=(_file(b"1234"),),
        )
    with pytest.raises(ValueError, match="too many"):
        await store.append_exchange(
            session_id=session.session_id,
            guild_id=1,
            channel_id=2,
            user_id=3,
            user_text="q",
            assistant_text="a",
            attachments=(_file(b"1", "one.txt"), _file(b"2", "two.txt")),
        )


@pytest.mark.asyncio
async def test_concurrent_appends_are_atomic_and_bounded() -> None:
    store = ConversationStore(max_turns=12)
    session = await store.start(guild_id=1, channel_id=2, user_id=3)

    await asyncio.gather(
        *(
            store.append_exchange(
                session_id=session.session_id,
                guild_id=1,
                channel_id=2,
                user_id=3,
                user_text=f"u{index}",
                assistant_text=f"a{index}",
                bot_message_id=1_000 + index,
            )
            for index in range(30)
        )
    )

    snapshot = await store.get(guild_id=1, channel_id=2, user_id=3)

    assert snapshot is not None
    assert len(snapshot.history) == 24
    assert all(
        snapshot.history[index].role is (MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT)
        for index in range(len(snapshot.history))
    )


@pytest.mark.asyncio
async def test_duplicate_bot_message_id_is_rejected() -> None:
    store = ConversationStore()
    session = await store.start(guild_id=1, channel_id=2, user_id=3)
    arguments = {
        "session_id": session.session_id,
        "guild_id": 1,
        "channel_id": 2,
        "user_id": 3,
        "user_text": "q",
        "assistant_text": "a",
        "bot_message_id": 100,
    }
    await store.append_exchange(**arguments)

    with pytest.raises(ConversationIndexConflictError):
        await store.append_exchange(**arguments)


@pytest.mark.asyncio
async def test_append_authorization_is_rechecked_after_lock_wait_before_mutation() -> None:
    store = ConversationStore()
    session = await store.start(guild_id=1, channel_id=2, user_id=3)
    allowed = [True]
    await store._lock.acquire()
    task = asyncio.create_task(
        store.append_exchange(
            session_id=session.session_id,
            guild_id=1,
            channel_id=2,
            user_id=3,
            user_text="private-user-text",
            assistant_text="private-assistant-text",
            attachments=(_file(b"private-binary"),),
            authorization_current=lambda: allowed[0],
        )
    )
    try:
        await asyncio.sleep(0)
        allowed[0] = False
    finally:
        store._lock.release()

    with pytest.raises(ConversationSessionError, match="authorization"):
        await asyncio.wait_for(task, timeout=1.0)
    assert (await store.stats()).exchange_count == 0
    assert (await store.stats()).binary_bytes == 0


@pytest.mark.asyncio
async def test_append_accepts_async_fresh_authorization_after_lock_wait() -> None:
    store = ConversationStore()
    session = await store.start(guild_id=1, channel_id=2, user_id=3)
    allowed = [True]

    async def fresh_allowed() -> bool:
        return allowed[0]

    await store._lock.acquire()
    task = asyncio.create_task(
        store.append_exchange(
            session_id=session.session_id,
            guild_id=1,
            channel_id=2,
            user_id=3,
            user_text="private-user-text",
            assistant_text="private-assistant-text",
            authorization_current=fresh_allowed,
        )
    )
    try:
        await asyncio.sleep(0)
        allowed[0] = False
    finally:
        store._lock.release()

    with pytest.raises(ConversationSessionError, match="authorization"):
        await asyncio.wait_for(task, timeout=1.0)
    assert (await store.stats()).exchange_count == 0


@pytest.mark.asyncio
async def test_snapshot_repr_hides_conversation_content_and_attachment_bytes() -> None:
    store = ConversationStore()
    session = await store.start(guild_id=1, channel_id=2, user_id=3)
    snapshot = await store.append_exchange(
        session_id=session.session_id,
        guild_id=1,
        channel_id=2,
        user_id=3,
        user_text="private-user-text",
        assistant_text="private-assistant-text",
        attachments=(_file(b"private-binary"),),
    )

    rendered = repr(snapshot)
    assert "private-user-text" not in rendered
    assert "private-assistant-text" not in rendered
    assert "private-binary" not in rendered


@pytest.mark.asyncio
async def test_session_cap_evicts_lru_session_and_all_of_its_reply_indexes() -> None:
    now = [0.0]
    store = ConversationStore(max_sessions=2, clock=lambda: now[0])
    first = await store.start(guild_id=1, channel_id=10, user_id=100)
    await store.append_exchange(
        session_id=first.session_id,
        guild_id=1,
        channel_id=10,
        user_id=100,
        user_text="first",
        assistant_text="answer-first",
        bot_message_id=1_000,
    )
    now[0] += 1
    second = await store.start(guild_id=1, channel_id=20, user_id=200)
    await store.append_exchange(
        session_id=second.session_id,
        guild_id=1,
        channel_id=20,
        user_id=200,
        user_text="second",
        assistant_text="answer-second",
        bot_message_id=2_000,
    )
    now[0] += 1
    assert await store.get(guild_id=1, channel_id=10, user_id=100) is not None
    now[0] += 1

    third = await store.start(guild_id=1, channel_id=30, user_id=300)

    stats = await store.stats()
    assert third.session_id
    assert stats.session_count == 2
    assert stats.evicted_sessions == 1
    assert stats.index_count == 1
    assert await store.resolve(bot_message_id=1_000, guild_id=1, channel_id=10, user_id=100) is not None
    assert await store.resolve(bot_message_id=2_000, guild_id=1, channel_id=20, user_id=200) is None


@pytest.mark.asyncio
async def test_global_binary_cap_preserves_appending_session_and_evicts_others() -> None:
    store = ConversationStore(
        max_sessions=10,
        max_attachment_bytes=4,
        max_binary_bytes=4,
        max_total_binary_bytes=4,
    )
    protected = await store.start(guild_id=1, channel_id=10, user_id=100)
    await store.append_exchange(
        session_id=protected.session_id,
        guild_id=1,
        channel_id=10,
        user_id=100,
        user_text="p1",
        assistant_text="a1",
        attachments=(_file(b"12", "p1.txt"),),
        bot_message_id=1_000,
    )
    victim = await store.start(guild_id=1, channel_id=20, user_id=200)
    await store.append_exchange(
        session_id=victim.session_id,
        guild_id=1,
        channel_id=20,
        user_id=200,
        user_text="v1",
        assistant_text="a1",
        attachments=(_file(b"34", "v1.txt"),),
        bot_message_id=2_000,
    )

    protected_snapshot = await store.append_exchange(
        session_id=protected.session_id,
        guild_id=1,
        channel_id=10,
        user_id=100,
        user_text="p2",
        assistant_text="a2",
        attachments=(_file(b"56", "p2.txt"),),
        bot_message_id=1_001,
    )

    stats = await store.stats()
    assert [turn.text for turn in protected_snapshot.history] == ["p1", "a1", "p2", "a2"]
    assert stats.session_count == 1
    assert stats.binary_bytes == 4
    assert stats.index_count == 2
    assert stats.evicted_sessions == 1
    assert await store.resolve(bot_message_id=2_000, guild_id=1, channel_id=20, user_id=200) is None


@pytest.mark.asyncio
async def test_stats_expire_sessions_and_never_contain_conversation_content() -> None:
    now = [0.0]
    store = ConversationStore(ttl_seconds=5, clock=lambda: now[0])
    session = await store.start(guild_id=1, channel_id=2, user_id=3)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=1,
        channel_id=2,
        user_id=3,
        user_text="secret-user-body",
        assistant_text="secret-assistant-body",
        attachments=(_file(b"secret-bytes"),),
        bot_message_id=100,
    )
    live = await store.stats()
    assert live.session_count == 1
    assert live.exchange_count == 1
    assert live.message_count == 2
    assert live.text_chars == len("secret-user-body") + len("secret-assistant-body")
    assert live.binary_bytes == len(b"secret-bytes")
    assert live.index_count == 1
    assert "secret" not in repr(live)

    now[0] += 5
    expired = await store.stats()

    assert expired.session_count == 0
    assert expired.text_chars == 0
    assert expired.binary_bytes == 0
    assert expired.index_count == 0
    assert expired.expired_sessions == 1


@pytest.mark.asyncio
async def test_concurrent_session_creation_never_exceeds_process_cap() -> None:
    store = ConversationStore(max_sessions=8)

    await asyncio.gather(
        *(store.get_or_start(guild_id=1, channel_id=10_000 + index, user_id=20_000 + index) for index in range(100))
    )

    stats = await store.stats()
    assert stats.session_count == 8
    assert stats.evicted_sessions == 92
    assert stats.binary_bytes == 0
    assert stats.index_count == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_sessions": 0},
        {"max_sessions": True},
        {"max_total_binary_bytes": 0},
        {"max_binary_bytes": 5, "max_total_binary_bytes": 4},
    ],
)
def test_process_resource_limit_configuration_is_validated(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ConversationStore(**arguments)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_text_history_and_reply_index_survive_reopen_without_attachment_bytes(tmp_path) -> None:
    path = tmp_path / "suite.sqlite3"
    first = ConversationStore(database_path=path, ttl_seconds=600)
    session = await first.start(guild_id=10, channel_id=20, user_id=30)
    await first.append_exchange(
        session_id=session.session_id,
        guild_id=10,
        channel_id=20,
        user_id=30,
        user_text="画像についての質問",
        assistant_text="前回の回答",
        attachments=(_file(b"not-persisted"),),
        bot_message_id=123456789012345678,
    )
    first.close()

    reopened = ConversationStore(database_path=path, ttl_seconds=600)
    resolved = await reopened.resolve(
        bot_message_id=123456789012345678,
        guild_id=10,
        channel_id=20,
        user_id=30,
    )

    assert resolved is not None
    assert resolved.session_id == session.session_id
    assert [turn.text for turn in resolved.history] == ["画像についての質問", "前回の回答"]
    assert all(turn.attachments == () for turn in resolved.history)
    assert (
        await reopened.resolve(
            bot_message_id=123456789012345678,
            guild_id=10,
            channel_id=20,
            user_id=31,
        )
        is None
    )
    assert (
        await reopened.resolve(
            bot_message_id=123456789012345678,
            guild_id=10,
            channel_id=21,
            user_id=30,
        )
        is None
    )
    reopened.close()


@pytest.mark.asyncio
async def test_persisted_conversation_expires_on_reopen(tmp_path) -> None:
    now = [100.0]
    path = tmp_path / "suite.sqlite3"
    first = ConversationStore(database_path=path, ttl_seconds=10, clock=lambda: now[0])
    session = await first.start(guild_id=1, channel_id=2, user_id=3)
    await first.append_exchange(
        session_id=session.session_id,
        guild_id=1,
        channel_id=2,
        user_id=3,
        user_text="question",
        assistant_text="answer",
        bot_message_id=100,
    )
    first.close()
    now[0] += 10

    reopened = ConversationStore(database_path=path, ttl_seconds=10, clock=lambda: now[0])
    assert await reopened.resolve(bot_message_id=100, guild_id=1, channel_id=2, user_id=3) is None
    assert (await reopened.stats()).session_count == 0
    reopened.close()
