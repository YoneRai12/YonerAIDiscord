from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import yonerai_discord.modules.voice.read_aloud as read_aloud
from yonerai_discord.modules.voice.models import SpeechRequest, SynthesizedSpeech
from yonerai_discord.modules.voice.presets import (
    ResolvedVoicePreset,
    VoicePresetScope,
    VoicePresetValues,
)
from yonerai_discord.modules.voice.read_aloud import (
    ReadAloudBatchStatus,
    ReadAloudBurstCoordinator,
    ReadAloudDictionaryEntry,
    ReadAloudPolicySnapshot,
    ReadAloudRejectedError,
    ReadAloudRepositoryError,
    ReadAloudRoute,
    SqliteReadAloudRouteRepository,
    apply_read_aloud_policy,
)


def _route(
    *,
    guild_id: int = 1,
    source_channel_id: int = 2,
    destination_voice_channel_id: int = 3,
    enabled: bool = True,
    revision: int = 1,
) -> ReadAloudRoute:
    return ReadAloudRoute(
        guild_id=guild_id,
        source_channel_id=source_channel_id,
        destination_voice_channel_id=destination_voice_channel_id,
        enabled=enabled,
        revision=revision,
    )


def _wav() -> SynthesizedSpeech:
    return SynthesizedSpeech(b"RIFF" + b"\x00" * 40)


async def _current_allowed(_route: ReadAloudRoute, _author_ids: tuple[int, ...]) -> bool:
    return True


async def _deliver_noop(
    _route: ReadAloudRoute,
    _author_ids: tuple[int, ...],
    _wav_bytes: bytes,
) -> None:
    return None


async def _synthesize_wav(
    _request: SpeechRequest,
    _route: ReadAloudRoute,
    _author_ids: tuple[int, ...],
) -> SynthesizedSpeech:
    return _wav()


def test_route_repository_persists_restart_and_isolates_guilds(tmp_path: Path) -> None:
    path = tmp_path / "read-aloud.sqlite3"
    repository = SqliteReadAloudRouteRepository(path)
    repository.open()
    first = repository.put(
        guild_id=1,
        source_channel_id=10,
        destination_voice_channel_id=20,
        enabled=True,
    )
    repository.put(
        guild_id=2,
        source_channel_id=10,
        destination_voice_channel_id=30,
        enabled=False,
    )
    updated = repository.put(
        guild_id=1,
        source_channel_id=10,
        destination_voice_channel_id=21,
        enabled=True,
        expected_revision=first.revision,
    )
    repository.close()

    reopened = SqliteReadAloudRouteRepository(path)
    reopened.open()
    assert reopened.get(1, 10) == updated
    assert reopened.list_for_guild(1) == (updated,)
    assert reopened.list_for_guild(2) == (
        _route(
            guild_id=2,
            source_channel_id=10,
            destination_voice_channel_id=30,
            enabled=False,
        ),
    )
    with pytest.raises(ReadAloudRepositoryError, match="route_revision_conflict"):
        reopened.put(
            guild_id=1,
            source_channel_id=10,
            destination_voice_channel_id=22,
            enabled=True,
            expected_revision=1,
        )
    assert reopened.delete(1, 10, expected_revision=updated.revision)
    assert reopened.get(1, 10) is None
    reopened.close()


def test_schema_v1_migrates_to_v2_without_losing_route(tmp_path: Path) -> None:
    path = tmp_path / "read-aloud-v1.sqlite3"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE voice_read_aloud_schema (
            singleton INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL
        );
        INSERT INTO voice_read_aloud_schema VALUES (1, 1);
        CREATE TABLE voice_read_aloud_routes (
            guild_id INTEGER NOT NULL,
            source_channel_id INTEGER NOT NULL,
            destination_voice_channel_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL,
            speaker_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(guild_id, source_channel_id)
        );
        INSERT INTO voice_read_aloud_routes VALUES (1, 10, 20, 1, 3, 7, '');
        """
    )
    raw.close()

    repository = SqliteReadAloudRouteRepository(path)
    repository.open()
    assert repository.get(1, 10) == _route(
        guild_id=1,
        source_channel_id=10,
        destination_voice_channel_id=20,
        revision=7,
    )
    assert repository.get_policy(1) == ReadAloudPolicySnapshot(guild_id=1, revision=0)
    repository.close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT schema_version FROM voice_read_aloud_schema").fetchone() == (2,)
    raw.close()


def test_policy_persists_isolates_guilds_and_requires_revision_cas(tmp_path: Path) -> None:
    path = tmp_path / "read-aloud.sqlite3"
    repository = SqliteReadAloudRouteRepository(path)
    repository.open()
    first = repository.set_dictionary(1, "ＡＢＣ", "えーびーしー", expected_revision=0)
    second = repository.add_exclusion(1, "秘密", expected_revision=first.revision)
    other = repository.set_dictionary(2, "ABC", "別読み", expected_revision=0)
    with pytest.raises(ReadAloudRepositoryError, match="policy_revision_conflict"):
        repository.delete_dictionary(1, "ABC", expected_revision=first.revision)
    repository.close()

    reopened = SqliteReadAloudRouteRepository(path)
    reopened.open()
    assert reopened.get_policy(1) == second
    assert reopened.get_policy(2) == other
    assert second.dictionary == (ReadAloudDictionaryEntry("ABC", "えーびーしー"),)
    assert second.exclusions == ("秘密",)
    deleted = reopened.delete_dictionary(1, "ABC", expected_revision=second.revision)
    assert deleted.dictionary == ()
    deleted = reopened.delete_exclusion(1, "秘密", expected_revision=deleted.revision)
    assert deleted.exclusions == ()
    reopened.close()


def test_policy_bounds_validation_and_repr_are_content_free(tmp_path: Path) -> None:
    repository = SqliteReadAloudRouteRepository(tmp_path / "read-aloud.sqlite3")
    repository.open()
    policy = repository.set_dictionary(
        1,
        "secret-term",
        "secret-pronunciation",
        expected_revision=0,
    )
    policy = repository.add_exclusion(1, "secret-phrase", expected_revision=policy.revision)
    assert "secret-term" not in repr(policy)
    assert "secret-pronunciation" not in repr(policy)
    assert "secret-phrase" not in repr(policy)
    for value in ("", "x" * 65, "bad\x00value", "bad\nvalue"):
        with pytest.raises((TypeError, ValueError)) as error:
            repository.set_dictionary(1, value, "ok", expected_revision=policy.revision)
        if value:
            assert value not in repr(error.value)
    repository.close()
    with pytest.raises(ReadAloudRepositoryError, match="repository_closed"):
        repository.get_policy(1)


def test_corrupt_policy_fails_closed_without_mutation_or_content_leak(tmp_path: Path) -> None:
    path = tmp_path / "read-aloud.sqlite3"
    repository = SqliteReadAloudRouteRepository(path)
    repository.open()
    snapshot = repository.set_dictionary(
        1,
        "private-term",
        "private-reading",
        expected_revision=0,
    )
    repository.close()
    raw = sqlite3.connect(path)
    raw.execute(
        "UPDATE voice_read_aloud_dictionary SET pronunciation = ? WHERE guild_id = 1",
        ("corrupt\nprivate-reading",),
    )
    raw.commit()
    raw.close()

    repository.open()
    with pytest.raises(ReadAloudRepositoryError, match="policy_corrupt") as error:
        repository.set_dictionary(
            1,
            "another-term",
            "another-reading",
            expected_revision=snapshot.revision,
        )
    assert "private" not in repr(error.value)
    repository.close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT revision FROM voice_read_aloud_policy_revisions WHERE guild_id = 1").fetchone() == (
        snapshot.revision,
    )
    assert raw.execute("SELECT COUNT(*) FROM voice_read_aloud_dictionary WHERE guild_id = 1").fetchone() == (1,)
    raw.close()


def test_policy_dictionary_and_exclusion_are_bounded_to_64_each(tmp_path: Path) -> None:
    repository = SqliteReadAloudRouteRepository(tmp_path / "read-aloud.sqlite3")
    repository.open()
    revision = 0
    for index in range(64):
        snapshot = repository.set_dictionary(
            1,
            f"term-{index}",
            "reading",
            expected_revision=revision,
        )
        revision = snapshot.revision
    with pytest.raises(ReadAloudRepositoryError, match="dictionary_limit_reached"):
        repository.set_dictionary(1, "overflow", "reading", expected_revision=revision)
    for index in range(64):
        snapshot = repository.add_exclusion(
            1,
            f"phrase-{index}",
            expected_revision=revision,
        )
        revision = snapshot.revision
    with pytest.raises(ReadAloudRepositoryError, match="exclusion_limit_reached"):
        repository.add_exclusion(1, "overflow", expected_revision=revision)
    repository.close()


def test_policy_literal_replacement_is_non_cascading_deterministic_and_excludes() -> None:
    policy = ReadAloudPolicySnapshot(
        guild_id=1,
        revision=1,
        dictionary=(
            ReadAloudDictionaryEntry("東京", "首都"),
            ReadAloudDictionaryEntry("東京都", "とうきょうと"),
            ReadAloudDictionaryEntry("首都", "しゅと"),
        ),
        exclusions=("読まない",),
    )
    assert apply_read_aloud_policy("東京都と東京", policy) == "とうきょうとと首都"
    with pytest.raises(ReadAloudRejectedError, match="text_excluded"):
        apply_read_aloud_policy("これは読まない本文", policy)
    expanding = ReadAloudPolicySnapshot(
        guild_id=1,
        revision=2,
        dictionary=(ReadAloudDictionaryEntry("a", "b" * 64),),
    )
    with pytest.raises(ReadAloudRejectedError, match="text_too_long"):
        apply_read_aloud_policy("a" * 8, expanding)


def test_route_repository_never_stores_message_text_and_quarantines_invalid_row(tmp_path: Path) -> None:
    path = tmp_path / "read-aloud.sqlite3"
    repository = SqliteReadAloudRouteRepository(path)
    repository.open()
    repository.put(
        guild_id=1,
        source_channel_id=10,
        destination_voice_channel_id=20,
        enabled=True,
    )
    repository.close()

    raw = sqlite3.connect(path)
    columns = {row[1] for row in raw.execute("PRAGMA table_info(voice_read_aloud_routes)").fetchall()}
    assert not {"text", "content", "message", "wav"} & columns
    raw.execute("PRAGMA ignore_check_constraints = ON")
    raw.execute("UPDATE voice_read_aloud_routes SET speaker_id = 99 WHERE guild_id = 1 AND source_channel_id = 10")
    raw.commit()
    raw.close()

    repository.open()
    assert repository.get(1, 10) is None
    repository.close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT COUNT(*) FROM voice_read_aloud_routes").fetchone()[0] == 0
    quarantine = raw.execute("SELECT reason_code, length(row_sha256) FROM voice_read_aloud_route_quarantine").fetchone()
    assert quarantine == ("route_row_invalid", 64)
    raw.close()


def test_schema_and_existing_revision_require_exact_stored_integers(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema-corrupt.sqlite3"
    repository = SqliteReadAloudRouteRepository(schema_path)
    repository.open()
    repository.close()
    raw = sqlite3.connect(schema_path)
    raw.execute("PRAGMA ignore_check_constraints = ON")
    raw.execute("UPDATE voice_read_aloud_schema SET schema_version = 1.5")
    raw.commit()
    raw.close()
    with pytest.raises(ReadAloudRepositoryError, match="schema_version_corrupt"):
        repository.open()

    revision_path = tmp_path / "revision-corrupt.sqlite3"
    repository = SqliteReadAloudRouteRepository(revision_path)
    repository.open()
    repository.put(
        guild_id=1,
        source_channel_id=10,
        destination_voice_channel_id=20,
        enabled=True,
    )
    repository.close()
    raw = sqlite3.connect(revision_path)
    raw.execute("PRAGMA ignore_check_constraints = ON")
    raw.execute("UPDATE voice_read_aloud_routes SET revision = 'broken' WHERE guild_id = 1 AND source_channel_id = 10")
    raw.commit()
    raw.close()
    repository.open()
    with pytest.raises(ReadAloudRepositoryError, match="route_revision_corrupt"):
        repository.put(
            guild_id=1,
            source_channel_id=10,
            destination_voice_channel_id=21,
            enabled=True,
        )
    repository.close()


@pytest.mark.parametrize("corrupted_revision", [1.5, "not-an-integer"])
def test_route_repository_quarantines_real_and_text_integer_fields(
    tmp_path: Path,
    corrupted_revision: object,
) -> None:
    path = tmp_path / "read-aloud.sqlite3"
    repository = SqliteReadAloudRouteRepository(path)
    repository.open()
    repository.put(
        guild_id=1,
        source_channel_id=10,
        destination_voice_channel_id=20,
        enabled=True,
    )
    repository.close()

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA ignore_check_constraints = ON")
    raw.execute(
        "UPDATE voice_read_aloud_routes SET revision = ? WHERE guild_id = 1 AND source_channel_id = 10",
        (corrupted_revision,),
    )
    raw.commit()
    raw.close()

    repository.open()
    assert repository.get(1, 10) is None
    repository.close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT COUNT(*) FROM voice_read_aloud_routes").fetchone()[0] == 0
    assert raw.execute("SELECT reason_code FROM voice_read_aloud_route_quarantine").fetchone() == ("route_row_invalid",)
    raw.close()


@pytest.mark.asyncio
async def test_burst_merges_normalized_text_into_one_fixed_speaker_synthesis() -> None:
    synthesized: list[SpeechRequest] = []
    delivered: list[tuple[ReadAloudRoute, bytes]] = []

    async def synthesize(
        request: SpeechRequest,
        _route: ReadAloudRoute,
        author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        assert author_ids == (4, 5)
        synthesized.append(request)
        return _wav()

    async def deliver(route: ReadAloudRoute, author_ids: tuple[int, ...], wav: bytes) -> None:
        assert author_ids == (4, 5)
        delivered.append((route, wav))

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=deliver,
        route_current=_current_allowed,
        merge_window_seconds=0.01,
    )
    route = _route()
    await coordinator.submit(
        route,
        author_id=4,
        text="  全角　テスト https://example.com/a  ",
        author_is_current_voice_member=True,
    )
    receipt = await coordinator.submit(
        route,
        author_id=5,
        text="<@12345678901234567> さん、こんにちは",
        author_is_current_voice_member=True,
    )
    await coordinator.wait_idle()

    assert receipt.pending_items == 2
    assert len(synthesized) == 1
    assert synthesized[0].speaker_id == 3
    assert synthesized[0].guild_id == route.guild_id
    assert synthesized[0].channel_id == route.source_channel_id
    assert synthesized[0].text == "全角 テスト URL メンション さん、こんにちは"
    assert delivered == [(route, _wav().wav)]
    assert coordinator.receipts()[0].status is ReadAloudBatchStatus.DELIVERED
    await coordinator.close()


@pytest.mark.asyncio
async def test_user_presets_with_equal_values_and_revisions_preserve_a_b_a_fifo() -> None:
    requests: list[tuple[SpeechRequest, tuple[int, ...]]] = []

    async def synthesize(
        request: SpeechRequest,
        _route: ReadAloudRoute,
        author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        requests.append((request, author_ids))
        return _wav()

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=_deliver_noop,
        route_current=_current_allowed,
        merge_window_seconds=0.01,
    )
    route = _route()
    values = VoicePresetValues(speed_milli=1_250, volume_milli=800)
    preset_a = ResolvedVoicePreset(
        guild_id=route.guild_id,
        user_id=4,
        values=values,
        source=VoicePresetScope.USER,
        revision=7,
    )
    preset_b = ResolvedVoicePreset(
        guild_id=route.guild_id,
        user_id=5,
        values=values,
        source=VoicePresetScope.USER,
        revision=7,
    )

    await coordinator.submit(
        route,
        author_id=4,
        text="first",
        author_is_current_voice_member=True,
        preset=preset_a,
    )
    await coordinator.submit(
        route,
        author_id=5,
        text="second",
        author_is_current_voice_member=True,
        preset=preset_b,
    )
    await coordinator.submit(
        route,
        author_id=4,
        text="third",
        author_is_current_voice_member=True,
        preset=preset_a,
    )
    await coordinator.wait_idle()

    assert [(request.text, author_ids) for request, author_ids in requests] == [
        ("first", (4,)),
        ("second", (5,)),
        ("third", (4,)),
    ]
    assert all(request.speed_scale == 1.25 for request, _author_ids in requests)
    assert all(request.volume_scale == 0.8 for request, _author_ids in requests)
    assert [receipt.status for receipt in coordinator.receipts()] == [
        ReadAloudBatchStatus.DELIVERED,
        ReadAloudBatchStatus.DELIVERED,
        ReadAloudBatchStatus.DELIVERED,
    ]
    await coordinator.close()


@pytest.mark.asyncio
async def test_scope_separation_and_per_route_bounds() -> None:
    requests: list[SpeechRequest] = []

    async def synthesize(
        request: SpeechRequest,
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        requests.append(request)
        return _wav()

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=_deliver_noop,
        route_current=_current_allowed,
        merge_window_seconds=0.03,
        max_pending_items_per_route=1,
        max_pending_characters_per_route=10,
    )
    await coordinator.submit(
        _route(guild_id=1),
        author_id=10,
        text="alpha",
        author_is_current_voice_member=True,
    )
    with pytest.raises(ReadAloudRejectedError, match="pending_item_limit_reached"):
        await coordinator.submit(
            _route(guild_id=1),
            author_id=11,
            text="beta",
            author_is_current_voice_member=True,
        )
    await coordinator.submit(
        _route(guild_id=2),
        author_id=12,
        text="gamma",
        author_is_current_voice_member=True,
    )
    with pytest.raises(ReadAloudRejectedError, match="pending_text_limit_reached"):
        await coordinator.submit(
            _route(guild_id=3),
            author_id=13,
            text="eleven chars",
            author_is_current_voice_member=True,
        )
    await coordinator.wait_idle()
    assert {(item.guild_id, item.text) for item in requests} == {(1, "alpha"), (2, "gamma")}
    await coordinator.close()


@pytest.mark.asyncio
async def test_plain_broadcast_mentions_are_replaced_with_safe_japanese_token() -> None:
    requests: list[SpeechRequest] = []

    async def synthesize(
        request: SpeechRequest,
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        requests.append(request)
        return _wav()

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=_deliver_noop,
        route_current=_current_allowed,
        merge_window_seconds=0.01,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="＠ｅｖｅｒｙｏｎｅ と @here へ連絡",
        author_is_current_voice_member=True,
    )
    await coordinator.wait_idle()

    assert requests[0].text == "全体通知 と 全体通知 へ連絡"
    assert "@everyone" not in requests[0].text
    assert "@here" not in requests[0].text
    await coordinator.close()


@pytest.mark.asyncio
async def test_duplicate_content_same_scope_is_rejected_before_provider_or_sink() -> None:
    calls = {"synthesize": 0, "deliver": 0}

    async def synthesize(
        _request: SpeechRequest,
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        calls["synthesize"] += 1
        return _wav()

    async def deliver(
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
        _wav_bytes: bytes,
    ) -> None:
        calls["deliver"] += 1

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=deliver,
        route_current=_current_allowed,
        merge_window_seconds=0.2,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="同一本文",
        author_is_current_voice_member=True,
    )
    with pytest.raises(ReadAloudRejectedError, match="duplicate_content"):
        await coordinator.submit(
            _route(),
            author_id=4,
            text="同一本文",
            author_is_current_voice_member=True,
        )

    assert calls == {"synthesize": 0, "deliver": 0}
    assert "同一本文" not in repr(coordinator)
    await coordinator.close()


@pytest.mark.asyncio
async def test_content_deduplication_is_scoped_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_aloud, "_CONTENT_DEDUPE_TTL_SECONDS", 0.01)
    coordinator = ReadAloudBurstCoordinator(
        synthesize=_synthesize_wav,
        deliver=_deliver_noop,
        route_current=_current_allowed,
        merge_window_seconds=0.2,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="同一本文",
        author_is_current_voice_member=True,
    )
    await coordinator.submit(
        _route(source_channel_id=20),
        author_id=4,
        text="同一本文",
        author_is_current_voice_member=True,
    )
    with pytest.raises(ReadAloudRejectedError, match="duplicate_content"):
        await coordinator.submit(
            _route(),
            author_id=4,
            text="同一本文",
            author_is_current_voice_member=True,
        )

    await asyncio.sleep(0.02)
    await coordinator.submit(
        _route(),
        author_id=4,
        text="同一本文",
        author_is_current_voice_member=True,
    )
    await coordinator.close()


@pytest.mark.asyncio
async def test_per_author_pending_limit_preserves_route_capacity_for_another_author() -> None:
    coordinator = ReadAloudBurstCoordinator(
        synthesize=_synthesize_wav,
        deliver=_deliver_noop,
        route_current=_current_allowed,
        merge_window_seconds=0.2,
    )
    for text in ("first", "second"):
        await coordinator.submit(
            _route(),
            author_id=4,
            text=text,
            author_is_current_voice_member=True,
        )
    with pytest.raises(ReadAloudRejectedError, match="pending_author_item_limit_reached"):
        await coordinator.submit(
            _route(),
            author_id=4,
            text="third",
            author_is_current_voice_member=True,
        )
    receipt = await coordinator.submit(
        _route(),
        author_id=5,
        text="other author",
        author_is_current_voice_member=True,
    )

    assert receipt.pending_items == 3
    await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "kwargs", "code"),
    [
        ("", {}, "text_empty"),
        ("<@12345678901234567>", {}, "mention_only"),
        ("bad\x00text", {}, "text_control_character"),
        ("x" * 501, {}, "text_too_long"),
        ("hello", {"author_is_bot": True}, "automated_author_rejected"),
        ("hello", {"is_webhook": True}, "automated_author_rejected"),
        ("hello", {"author_is_current_voice_member": False}, "author_not_in_destination_voice"),
    ],
)
async def test_invalid_sources_and_text_are_rejected_without_synthesis(
    text: str,
    kwargs: dict[str, bool],
    code: str,
) -> None:
    calls = 0

    async def synthesize(
        _request: SpeechRequest,
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        nonlocal calls
        calls += 1
        return _wav()

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=_deliver_noop,
        route_current=_current_allowed,
        merge_window_seconds=0.01,
    )
    parameters = {"author_is_current_voice_member": True, **kwargs}
    with pytest.raises(ReadAloudRejectedError, match=code):
        await coordinator.submit(
            _route(),
            author_id=4,
            text=text,
            **parameters,
        )
    assert calls == 0
    await coordinator.close()


@pytest.mark.asyncio
async def test_revoke_before_synthesis_prevents_synthesis_and_delivery() -> None:
    calls = {"current": 0, "synthesize": 0, "deliver": 0}

    async def current(_route: ReadAloudRoute, _author_ids: tuple[int, ...]) -> bool:
        calls["current"] += 1
        return False

    async def synthesize(
        _request: SpeechRequest,
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        calls["synthesize"] += 1
        return _wav()

    async def deliver(
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
        _wav: bytes,
    ) -> None:
        calls["deliver"] += 1

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=deliver,
        route_current=current,
        merge_window_seconds=0.01,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="hello",
        author_is_current_voice_member=True,
    )
    await coordinator.wait_idle()
    assert calls == {"current": 1, "synthesize": 0, "deliver": 0}
    assert coordinator.receipts()[0].error_code == "route_revoked_before_synthesis"
    await coordinator.close()


@pytest.mark.asyncio
async def test_revoke_after_synthesis_prevents_delivery() -> None:
    current_values = iter((True, False))
    delivered = 0

    async def deliver(
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
        _wav: bytes,
    ) -> None:
        nonlocal delivered
        delivered += 1

    async def current(_route: ReadAloudRoute, _author_ids: tuple[int, ...]) -> bool:
        return next(current_values)

    coordinator = ReadAloudBurstCoordinator(
        synthesize=_synthesize_wav,
        deliver=deliver,
        route_current=current,
        merge_window_seconds=0.01,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="hello",
        author_is_current_voice_member=True,
    )
    await coordinator.wait_idle()
    assert delivered == 0
    assert coordinator.receipts()[0].error_code == "route_revoked_after_synthesis"
    await coordinator.close()


@pytest.mark.asyncio
async def test_final_revoke_immediately_before_delivery_prevents_delivery() -> None:
    current_values = iter((True, True, False))
    delivered = 0

    async def deliver(
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
        _wav: bytes,
    ) -> None:
        nonlocal delivered
        delivered += 1

    async def current(_route: ReadAloudRoute, _author_ids: tuple[int, ...]) -> bool:
        return next(current_values)

    coordinator = ReadAloudBurstCoordinator(
        synthesize=_synthesize_wav,
        deliver=deliver,
        route_current=current,
        merge_window_seconds=0.01,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="hello",
        author_is_current_voice_member=True,
    )
    await coordinator.wait_idle()
    assert delivered == 0
    assert coordinator.receipts()[0].error_code == "route_revoked_before_delivery"
    await coordinator.close()


@pytest.mark.asyncio
async def test_close_cancels_inflight_and_clears_raw_payloads() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    delivered = 0

    async def synthesize(
        _request: SpeechRequest,
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def deliver(
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
        _wav: bytes,
    ) -> None:
        nonlocal delivered
        delivered += 1

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=deliver,
        route_current=_current_allowed,
        merge_window_seconds=0.01,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="private text",
        author_is_current_voice_member=True,
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await coordinator.close()
    assert cancelled.is_set()
    assert delivered == 0
    assert len(coordinator.receipts()) == 1
    assert coordinator.receipts()[0].status is ReadAloudBatchStatus.CANCELLED
    assert coordinator.receipts()[0].error_code == "coordinator_closed"
    assert "private text" not in repr(coordinator)


@pytest.mark.asyncio
async def test_external_cancel_records_drained_inflight_batch_exactly_once() -> None:
    started = asyncio.Event()

    async def synthesize(
        _request: SpeechRequest,
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        started.set()
        await asyncio.Event().wait()
        return _wav()

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=_deliver_noop,
        route_current=_current_allowed,
        merge_window_seconds=0.01,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="private inflight text",
        author_is_current_voice_member=True,
    )
    waiter = asyncio.create_task(coordinator.wait_idle())
    await asyncio.wait_for(started.wait(), timeout=1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert len(coordinator.receipts()) == 1
    receipt = coordinator.receipts()[0]
    assert receipt.status is ReadAloudBatchStatus.CANCELLED
    assert receipt.error_code == "external_cancelled"
    assert receipt.item_count == 1
    assert "private inflight text" not in repr(receipt)
    await coordinator.close()
    assert len(coordinator.receipts()) == 1


@pytest.mark.asyncio
async def test_external_cancel_during_merge_wait_is_not_reported_as_close() -> None:
    coordinator = ReadAloudBurstCoordinator(
        synthesize=_synthesize_wav,
        deliver=_deliver_noop,
        route_current=_current_allowed,
        merge_window_seconds=0.2,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="private pending text",
        author_is_current_voice_member=True,
    )
    waiter = asyncio.create_task(coordinator.wait_idle())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert len(coordinator.receipts()) == 1
    receipt = coordinator.receipts()[0]
    assert receipt.status is ReadAloudBatchStatus.CANCELLED
    assert receipt.error_code == "external_cancelled"
    assert receipt.item_count == 1
    assert "private pending text" not in repr(receipt)
    await coordinator.close()
    assert len(coordinator.receipts()) == 1


@pytest.mark.asyncio
async def test_close_records_pending_batch_once_before_clearing_text() -> None:
    synthesized = 0

    async def synthesize(
        _request: SpeechRequest,
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        nonlocal synthesized
        synthesized += 1
        return _wav()

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=_deliver_noop,
        route_current=_current_allowed,
        merge_window_seconds=0.2,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="private pending text",
        author_is_current_voice_member=True,
    )
    await coordinator.close()

    assert synthesized == 0
    assert len(coordinator.receipts()) == 1
    receipt = coordinator.receipts()[0]
    assert receipt.status is ReadAloudBatchStatus.CANCELLED
    assert receipt.error_code == "coordinator_closed"
    assert receipt.item_count == 1
    assert receipt.character_count == len("private pending text")
    assert "private pending text" not in repr(receipt)
    assert "private pending text" not in repr(coordinator)


@pytest.mark.asyncio
async def test_current_callback_timeout_stops_route_task_before_synthesis() -> None:
    synthesize_calls = 0
    deliver_calls = 0

    async def current(_route: ReadAloudRoute, _author_ids: tuple[int, ...]) -> bool:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return True

    async def synthesize(
        _request: SpeechRequest,
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        nonlocal synthesize_calls
        synthesize_calls += 1
        return _wav()

    async def deliver(
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
        _wav: bytes,
    ) -> None:
        nonlocal deliver_calls
        deliver_calls += 1

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=deliver,
        route_current=current,
        merge_window_seconds=0.01,
        current_timeout_seconds=0.01,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="private text",
        author_is_current_voice_member=True,
    )
    await asyncio.wait_for(coordinator.wait_idle(), timeout=1)

    assert synthesize_calls == 0
    assert deliver_calls == 0
    assert coordinator.receipts()[0].status is ReadAloudBatchStatus.FAILED
    assert coordinator.receipts()[0].error_code == "route_current_timeout_before_synthesis"
    await coordinator.close()


@pytest.mark.asyncio
async def test_synthesis_callback_timeout_stops_before_delivery() -> None:
    deliver_calls = 0

    async def synthesize(
        _request: SpeechRequest,
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return _wav()

    async def deliver(
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
        _wav: bytes,
    ) -> None:
        nonlocal deliver_calls
        deliver_calls += 1

    coordinator = ReadAloudBurstCoordinator(
        synthesize=synthesize,
        deliver=deliver,
        route_current=_current_allowed,
        merge_window_seconds=0.01,
        synthesis_timeout_seconds=0.01,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="private text",
        author_is_current_voice_member=True,
    )
    await asyncio.wait_for(coordinator.wait_idle(), timeout=1)

    assert deliver_calls == 0
    assert coordinator.receipts()[0].status is ReadAloudBatchStatus.FAILED
    assert coordinator.receipts()[0].error_code == "synthesis_timeout"
    await coordinator.close()


@pytest.mark.asyncio
async def test_delivery_callback_timeout_is_content_free_and_terminal() -> None:
    delivery_started = 0

    async def deliver(
        _route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
        _wav: bytes,
    ) -> None:
        nonlocal delivery_started
        delivery_started += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    coordinator = ReadAloudBurstCoordinator(
        synthesize=_synthesize_wav,
        deliver=deliver,
        route_current=_current_allowed,
        merge_window_seconds=0.01,
        delivery_timeout_seconds=0.01,
    )
    await coordinator.submit(
        _route(),
        author_id=4,
        text="private text",
        author_is_current_voice_member=True,
    )
    await asyncio.wait_for(coordinator.wait_idle(), timeout=1)

    assert delivery_started == 1
    assert coordinator.receipts()[0].status is ReadAloudBatchStatus.FAILED
    assert coordinator.receipts()[0].error_code == "delivery_timeout"
    assert "private text" not in repr(coordinator.receipts()[0])
    await coordinator.close()


def test_request_and_internal_receipts_do_not_repr_text_or_wav() -> None:
    request = SpeechRequest(text="private text", guild_id=1, channel_id=2)
    assert "private text" not in repr(request)
    speech = SynthesizedSpeech(b"RIFFprivate wav")
    assert "private wav" not in repr(speech)
    route = _route()
    assert route.speaker_id == 3
