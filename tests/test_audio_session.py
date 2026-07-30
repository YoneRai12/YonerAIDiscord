from __future__ import annotations

import asyncio
import struct
import threading
from collections import deque

import pytest

from yonerai_discord.modules.audio_core import (
    PCM_FRAME_BYTES,
    GuildAudioSession,
    LoopMode,
    PlayerStateError,
    QueueFullError,
    RecentTrackState,
    RequesterQueueLimitError,
    SeekUnsupportedError,
    Track,
    TrackSeekError,
    TrackSourceAuthorizationError,
)


def _pcm(value: int) -> bytes:
    return struct.pack("<h", value) * (PCM_FRAME_BYTES // 2)


class FakeSource:
    def __init__(self, *frames: bytes) -> None:
        self.frames = deque(frames)
        self.cleaned = False

    def read(self) -> bytes:
        return self.frames.popleft() if self.frames else b""

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.cleaned = True


class FakeFactory:
    def __init__(self) -> None:
        self.created: list[Track] = []
        self.seek_created: list[tuple[Track, float]] = []
        self.sources: list[FakeSource] = []

    def create(self, track: Track) -> FakeSource:
        self.created.append(track)
        source = FakeSource(_pcm(10_000), _pcm(10_000))
        self.sources.append(source)
        return source

    def create_speech(self, _wav: bytes) -> FakeSource:
        return FakeSource(_pcm(2_000))

    def create_at(self, track: Track, seconds: float) -> FakeSource:
        self.seek_created.append((track, seconds))
        source = FakeSource(_pcm(20_000), _pcm(20_000))
        self.sources.append(source)
        return source


class FailingFactory(FakeFactory):
    def __init__(self, *failed_titles: str) -> None:
        super().__init__()
        self.failed_titles = set(failed_titles)

    def create(self, track: Track) -> FakeSource:
        if track.title in self.failed_titles:
            raise RuntimeError("private-source-detail")
        return super().create(track)


class FailingSeekFactory(FakeFactory):
    def create_at(self, track: Track, seconds: float) -> FakeSource:
        raise RuntimeError("private-seek-source-detail")


class BlockingFactory(FakeFactory):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.abandoned_source: FakeSource | None = None

    def create(self, track: Track) -> FakeSource:
        self.started.set()
        if not self.release.wait(timeout=1.0):
            raise RuntimeError("source creation timed out")
        source = super().create(track)
        self.abandoned_source = source
        return source


class BlockingSeekFactory(FakeFactory):
    def __init__(self) -> None:
        super().__init__()
        self.seek_started = threading.Event()
        self.seek_release = threading.Event()
        self.seek_source: FakeSource | None = None

    def create_at(self, track: Track, seconds: float) -> FakeSource:
        self.seek_started.set()
        if not self.seek_release.wait(timeout=1.0):
            raise RuntimeError("seek source creation timed out")
        source = super().create_at(track, seconds)
        self.seek_source = source
        return source


class NonSeekableFactory:
    def create(self, track: Track) -> FakeSource:
        return FakeSource(_pcm(10_000), _pcm(10_000))

    def create_speech(self, _wav: bytes) -> FakeSource:
        return FakeSource(_pcm(2_000))


class FakeVoiceClient:
    def __init__(self) -> None:
        self.source = None
        self.after = None
        self.playing = False
        self.disconnected = False
        self.play_calls = 0

    def play(self, source, *, after=None) -> None:
        if self.playing:
            raise RuntimeError("already playing")
        self.source = source
        self.after = after
        self.playing = True
        self.play_calls += 1

    def stop(self) -> None:
        if self.playing:
            self.finish()

    def is_playing(self) -> bool:
        return self.playing

    async def disconnect(self, *, force: bool = False) -> None:
        assert force
        self.disconnected = True

    def drain(self) -> list[bytes]:
        frames = []
        while self.playing:
            frame = self.source.read()
            if not frame:
                self.finish()
                break
            frames.append(frame)
        return frames

    def finish(self, error=None) -> None:
        callback = self.after
        self.playing = False
        self.source = None
        self.after = None
        if callback is not None:
            callback(error)


def _track(name: str, requester_id: int = 10) -> Track:
    return Track(name, __file__, requester_id, track_id=name)


async def _turn() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_session_plays_queue_in_order_and_cleans_sources() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(1, voice, factory)
    await session.enqueue(_track("first"))
    await session.enqueue(_track("second"))

    voice.drain()
    await _turn()
    assert (await session.snapshot()).current.title == "second"
    voice.drain()
    await _turn()

    assert (await session.snapshot()).current is None
    assert [track.title for track in factory.created] == ["first", "second"]
    assert all(source.cleaned for source in factory.sources)


@pytest.mark.asyncio
async def test_tts_is_mixed_while_music_is_paused_without_consuming_music() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(1, voice, factory)
    await session.enqueue(_track("music"))
    await session.pause()
    speech = FakeSource(_pcm(2_000))

    assert await session.add_speech(speech) == 1
    frame = voice.source.read()

    assert struct.unpack_from("<h", frame)[0] == 2_000
    assert len(factory.sources[0].frames) == 2
    assert (await session.snapshot()).paused


@pytest.mark.asyncio
async def test_skip_ignores_track_loop_and_starts_next_track() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(1, voice, factory)
    await session.enqueue(_track("first"))
    await session.enqueue(_track("second"))
    await session.set_loop_mode(LoopMode.TRACK)

    skipped = await session.skip()
    voice.drain()
    await _turn()

    assert skipped.title == "first"
    assert (await session.snapshot()).current.title == "second"


@pytest.mark.asyncio
async def test_queue_and_speech_limits_are_enforced() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(1, voice, factory, max_queue=1, max_speech_queue=1)
    await session.enqueue(_track("one"))
    with pytest.raises(QueueFullError):
        await session.enqueue(_track("two"))

    await session.add_speech(FakeSource(_pcm(1_000)))
    rejected = FakeSource(_pcm(1_000))
    with pytest.raises(QueueFullError):
        await session.add_speech(rejected)
    assert rejected.cleaned


@pytest.mark.asyncio
async def test_requester_queue_limit_cannot_exceed_code_owned_cap() -> None:
    with pytest.raises(ValueError, match="queue limits"):
        GuildAudioSession(
            1,
            FakeVoiceClient(),
            FakeFactory(),
            max_queue=50,
            max_tracks_per_requester=11,
        )


@pytest.mark.asyncio
async def test_requester_queue_limit_preserves_fifo_and_allows_other_requesters() -> None:
    voice = FakeVoiceClient()
    session = GuildAudioSession(
        1,
        voice,
        FakeFactory(),
        max_queue=4,
        max_tracks_per_requester=2,
    )

    await session.enqueue(_track("one", 10))
    await session.enqueue(_track("two", 10))
    with pytest.raises(RequesterQueueLimitError):
        await session.enqueue(_track("three", 10))
    await session.enqueue(_track("other", 11))

    snapshot = await session.snapshot()
    assert snapshot.current is not None and snapshot.current.title == "one"
    assert [(track.title, track.requester_id) for track in snapshot.upcoming] == [
        ("two", 10),
        ("other", 11),
    ]


@pytest.mark.asyncio
async def test_requester_queue_limit_is_atomic_for_concurrent_enqueue() -> None:
    session = GuildAudioSession(
        1,
        FakeVoiceClient(),
        FakeFactory(),
        max_queue=3,
        max_tracks_per_requester=2,
    )
    await session.enqueue(_track("current", 10))

    results = await asyncio.gather(
        session.enqueue(_track("candidate-a", 10)),
        session.enqueue(_track("candidate-b", 10)),
        return_exceptions=True,
    )

    assert sum(isinstance(result, int) for result in results) == 1
    assert sum(isinstance(result, RequesterQueueLimitError) for result in results) == 1
    snapshot = await session.snapshot()
    assert snapshot.current is not None
    assert len(snapshot.upcoming) == 1


@pytest.mark.asyncio
async def test_restored_requester_over_limit_is_grandfathered_but_cannot_grow() -> None:
    session = GuildAudioSession(
        1,
        FakeVoiceClient(),
        FakeFactory(),
        max_queue=5,
        max_tracks_per_requester=2,
    )
    restored = tuple(_track(f"restored-{index}", 10) for index in range(3))

    await session.restore_projection(
        restored,
        loop_mode=LoopMode.OFF,
        paused=True,
        music_volume=0.65,
        speech_volume=1.0,
    )
    with pytest.raises(RequesterQueueLimitError):
        await session.enqueue(_track("same-requester", 10))
    await session.enqueue(_track("other-requester", 11))

    snapshot = await session.snapshot()
    assert [track.requester_id for track in snapshot.upcoming] == [10, 10, 10, 11]


@pytest.mark.asyncio
async def test_enqueue_many_rolls_back_entire_batch_when_first_source_is_rejected() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(
        1,
        voice,
        factory,
        track_allowed=lambda track: track.title != "blocked",
    )

    with pytest.raises(TrackSourceAuthorizationError):
        await session.enqueue_many((_track("blocked"), _track("allowed")))

    snapshot = await session.snapshot()
    assert snapshot.current is None
    assert snapshot.upcoming == ()
    assert factory.created == []


@pytest.mark.asyncio
async def test_recent_history_is_bounded_newest_first_and_contains_no_source_metadata() -> None:
    voice = FakeVoiceClient()
    session = GuildAudioSession(1, voice, FakeFactory())

    await session.enqueue(_track("completed"))
    voice.finish()
    await _turn()
    await session.enqueue(_track("skipped"))
    await session.skip()
    voice.drain()
    await _turn()
    await session.enqueue(_track("stopped"))
    await session.stop_music()
    voice.drain()
    await _turn()
    await session.enqueue(_track("failed"))
    voice.finish(RuntimeError("private failure"))
    await _turn()

    recent = (await session.snapshot()).recent
    assert [(item.title, item.state) for item in recent[:4]] == [
        ("failed", RecentTrackState.FAILED),
        ("stopped", RecentTrackState.STOPPED),
        ("skipped", RecentTrackState.SKIPPED),
        ("completed", RecentTrackState.COMPLETED),
    ]
    for index in range(21):
        await session.enqueue(_track(f"bounded-{index}"))
        voice.finish()
        await _turn()
    recent = (await session.snapshot()).recent
    assert len(recent) == 20
    assert recent[0].title == "bounded-20"
    assert recent[-1].title == "bounded-1"
    assert "source" not in repr(recent)
    assert "library_ref" not in repr(recent)
    assert "content_sha256" not in repr(recent)


@pytest.mark.asyncio
async def test_stop_keeps_tts_alive_and_close_disconnects() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(1, voice, factory)
    await session.enqueue(_track("one"))
    speech = FakeSource(_pcm(2_000), _pcm(2_000))
    await session.add_speech(speech)

    assert await session.stop_music() == 1
    assert voice.drain()
    await _turn()
    await session.close()

    assert speech.cleaned
    assert voice.disconnected
    with pytest.raises(PlayerStateError):
        await session.pause()


@pytest.mark.asyncio
async def test_state_change_callback_runs_after_lock_release_with_monotonic_revision() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    events = []
    session = None

    async def state_changed(revision, snapshot) -> None:
        assert session is not None
        current = await asyncio.wait_for(session.snapshot(), timeout=0.2)
        assert current.revision >= revision
        events.append((revision, snapshot))

    session = GuildAudioSession(1, voice, factory, state_changed=state_changed)
    await session.enqueue(_track("first"))
    await session.pause()
    await session.resume()
    await session.enqueue(_track("second"))
    await session.set_loop_mode(LoopMode.QUEUE)
    await session.set_volume(0.5)
    await session.set_speech_volume(0.6)
    await session.shuffle()
    await session.remove(1)
    await session.stop_music()

    assert [revision for revision, _snapshot in events] == list(range(1, 11))
    assert events[-1][1].speech_volume == 0.6
    assert events[-1][1].current is None
    assert events[-1][1].revision == 10

    await session.close()
    assert len(events) == 10


@pytest.mark.asyncio
async def test_move_and_requester_clear_mutate_only_the_waiting_queue() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(1, voice, factory)
    await session.enqueue(_track("current", requester_id=10))
    await session.enqueue(_track("second", requester_id=11))
    await session.enqueue(_track("third", requester_id=10))
    await session.enqueue(_track("fourth", requester_id=11))
    before = await session.snapshot()

    with pytest.raises(PlayerStateError, match="queue changed"):
        await session.move(2, 1, expected_track_id="wrong-track")
    assert await session.snapshot() == before

    moved = await session.move(2, 1, expected_track_id=before.upcoming[1].track_id)
    assert moved.title == "third"
    assert [track.title for track in (await session.snapshot()).upcoming] == ["third", "second", "fourth"]

    assert await session.clear_requester(11) == 2
    snapshot = await session.snapshot()
    assert snapshot.current is not None and snapshot.current.title == "current"
    assert [track.title for track in snapshot.upcoming] == ["third"]
    assert await session.clear_requester(99) == 0
    with pytest.raises(ValueError):
        await session.clear_requester(True)
    await session.close()


@pytest.mark.asyncio
async def test_control_revision_mismatch_rejects_mutation() -> None:
    session = GuildAudioSession(1, FakeVoiceClient(), FakeFactory())
    await session.enqueue(_track("current"))
    snapshot = await session.snapshot()

    with pytest.raises(PlayerStateError, match="state changed"):
        await session.set_speech_volume(0.4, expected_revision=snapshot.revision + 1)

    assert (await session.snapshot()).speech_volume == 1.0
    await session.set_speech_volume(0.4, expected_revision=snapshot.revision)
    assert (await session.snapshot()).speech_volume == 0.4
    await session.close()


@pytest.mark.asyncio
async def test_seek_replaces_only_music_and_preserves_pause_speech_and_voice_play() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(1, voice, factory)
    await session.enqueue(_track("current"))
    old_source = factory.sources[0]
    await session.add_speech(FakeSource(_pcm(2_000), _pcm(2_000)))
    await session.pause()
    before = await session.snapshot()
    mixer = voice.source
    authorization_calls = 0

    async def authorization_current() -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        return True

    current = await session.seek(
        12.5,
        expected_revision=before.revision,
        expected_track_id=before.current.track_id,
        authorization_current=authorization_current,
    )
    for _attempt in range(20):
        await asyncio.sleep(0.01)
        if old_source.cleaned:
            break

    assert current is before.current
    assert factory.seek_created == [(before.current, 12.5)]
    assert voice.source is mixer
    assert voice.play_calls == 1
    assert mixer.snapshot().music_paused
    assert mixer.snapshot().queued_speech == 1
    assert old_source.cleaned
    assert not factory.sources[-1].cleaned
    assert authorization_calls == 1
    assert (await session.snapshot()).revision == before.revision + 1


@pytest.mark.asyncio
async def test_seek_unsupported_and_source_failure_leave_current_music_unchanged() -> None:
    unsupported_voice = FakeVoiceClient()
    unsupported = GuildAudioSession(1, unsupported_voice, NonSeekableFactory())
    await unsupported.enqueue(_track("unsupported"))
    unsupported_before = await unsupported.snapshot()

    with pytest.raises(SeekUnsupportedError):
        await unsupported.seek(
            1,
            expected_revision=unsupported_before.revision,
            expected_track_id=unsupported_before.current.track_id,
        )
    assert await unsupported.snapshot() == unsupported_before
    assert unsupported_voice.playing

    failing_voice = FakeVoiceClient()
    failing_factory = FailingSeekFactory()
    failing = GuildAudioSession(2, failing_voice, failing_factory)
    await failing.enqueue(_track("failing"))
    old_source = failing_factory.sources[0]
    failing_before = await failing.snapshot()

    with pytest.raises(TrackSeekError) as caught:
        await failing.seek(
            2,
            expected_revision=failing_before.revision,
            expected_track_id=failing_before.current.track_id,
        )
    assert "private-seek-source-detail" not in str(caught.value)
    assert await failing.snapshot() == failing_before
    assert not old_source.cleaned
    assert failing_voice.playing


@pytest.mark.asyncio
async def test_seek_rechecks_authorization_and_cleans_rejected_new_source() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    allowed = True
    session = GuildAudioSession(
        1,
        voice,
        factory,
        track_allowed=lambda _track: allowed,
    )
    await session.enqueue(_track("current"))
    before = await session.snapshot()
    old_source = factory.sources[0]

    async def authorization_current() -> bool:
        nonlocal allowed
        allowed = False
        return True

    with pytest.raises(TrackSourceAuthorizationError):
        await session.seek(
            3,
            expected_revision=before.revision,
            expected_track_id=before.current.track_id,
            authorization_current=authorization_current,
        )
    for _attempt in range(20):
        await asyncio.sleep(0.01)
        if factory.sources[-1].cleaned:
            break

    assert await session.snapshot() == before
    assert voice.playing
    assert not old_source.cleaned
    assert factory.sources[-1].cleaned


@pytest.mark.asyncio
async def test_seek_treats_cancel_queued_after_final_authorization_as_committed() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(1, voice, factory)
    await session.enqueue(_track("current"))
    before = await session.snapshot()
    seek_task: asyncio.Task[Track] | None = None

    async def authorization_current() -> bool:
        assert seek_task is not None
        asyncio.get_running_loop().call_soon(seek_task.cancel)
        return True

    seek_task = asyncio.create_task(
        session.seek(
            3,
            expected_revision=before.revision,
            expected_track_id=before.current.track_id,
            authorization_current=authorization_current,
        )
    )
    current = await seek_task

    assert current is before.current
    assert seek_task.done() and not seek_task.cancelled()
    assert factory.seek_created == [(before.current, 3.0)]
    assert voice.play_calls == 1
    assert (await session.snapshot()).revision == before.revision + 1


@pytest.mark.asyncio
async def test_seek_rejects_track_finish_race_and_cleans_abandoned_source() -> None:
    voice = FakeVoiceClient()
    factory = BlockingSeekFactory()
    session = GuildAudioSession(1, voice, factory)
    await session.enqueue(_track("current"))
    before = await session.snapshot()

    seek_task = asyncio.create_task(
        session.seek(
            4,
            expected_revision=before.revision,
            expected_track_id=before.current.track_id,
        )
    )
    assert await asyncio.to_thread(factory.seek_started.wait, 1.0)
    voice.finish()
    await _turn()
    factory.seek_release.set()

    with pytest.raises(PlayerStateError, match="state changed"):
        await seek_task
    for _attempt in range(20):
        await asyncio.sleep(0.01)
        if factory.seek_source is not None and factory.seek_source.cleaned:
            break
    assert factory.seek_source is not None
    assert factory.seek_source.cleaned
    assert (await session.snapshot()).current is None


@pytest.mark.asyncio
async def test_seek_revision_mismatch_rejects_before_source_creation() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(1, voice, factory)
    await session.enqueue(_track("current"))
    before = await session.snapshot()

    with pytest.raises(PlayerStateError, match="state changed"):
        await session.seek(
            5,
            expected_revision=before.revision + 1,
            expected_track_id=before.current.track_id,
        )

    assert factory.seek_created == []
    assert await session.snapshot() == before


@pytest.mark.asyncio
async def test_automatic_finish_and_skip_emit_durable_state_changes() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    events = []

    async def state_changed(revision, snapshot) -> None:
        events.append((revision, snapshot.current, snapshot.upcoming))

    session = GuildAudioSession(1, voice, factory, state_changed=state_changed)
    await session.enqueue(_track("first"))
    await session.enqueue(_track("second"))
    before_skip = events[-1][0]

    await session.skip()
    assert events[-1][0] == before_skip + 1
    assert events[-1][1] is None
    assert [track.title for track in events[-1][2]] == ["second"]

    voice.drain()
    for _attempt in range(20):
        await asyncio.sleep(0.01)
        if events[-1][0] == before_skip + 2:
            break
    assert events[-1][0] == before_skip + 2
    assert events[-1][1].title == "second"


@pytest.mark.asyncio
async def test_paused_projection_restore_is_one_shot_and_starts_only_on_resume() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    events = []

    async def state_changed(revision, snapshot) -> None:
        events.append((revision, snapshot))

    session = GuildAudioSession(1, voice, factory, state_changed=state_changed)
    restored = await session.restore_projection(
        (_track("first"), _track("second")),
        loop_mode=LoopMode.QUEUE,
        paused=True,
        music_volume=0.4,
        speech_volume=0.7,
    )

    assert factory.created == []
    assert not voice.playing
    assert restored.paused
    assert restored.current is None
    assert [track.title for track in restored.upcoming] == ["first", "second"]
    assert restored.loop_mode is LoopMode.QUEUE
    assert restored.volume == 0.4
    assert restored.speech_volume == 0.7
    assert restored.revision == 1

    await session.add_speech(FakeSource(_pcm(2_000)))
    assert factory.created == []
    voice.drain()
    await asyncio.sleep(0.05)
    assert (await session.snapshot()).paused
    assert factory.created == []

    await session.resume()

    assert [track.title for track in factory.created] == ["first"]
    assert voice.playing
    assert (await session.snapshot()).current.title == "first"
    assert [revision for revision, _snapshot in events] == [1, 2, 3]
    with pytest.raises(PlayerStateError):
        await session.restore_projection(
            (),
            loop_mode=LoopMode.OFF,
            paused=False,
            music_volume=0.5,
            speech_volume=1.0,
        )


@pytest.mark.asyncio
async def test_paused_projection_enqueue_does_not_start_source_or_voice() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    events = []

    async def state_changed(revision, snapshot) -> None:
        events.append((revision, snapshot))

    session = GuildAudioSession(1, voice, factory, state_changed=state_changed)
    await session.restore_projection(
        (_track("first"),),
        loop_mode=LoopMode.OFF,
        paused=True,
        music_volume=0.5,
        speech_volume=1.0,
    )
    assert await session.enqueue(_track("second")) == 2

    assert factory.created == []
    assert not voice.playing
    snapshot = await session.snapshot()
    assert snapshot.paused
    assert [track.title for track in snapshot.upcoming] == ["first", "second"]
    assert [revision for revision, _snapshot in events] == [1, 2]


@pytest.mark.asyncio
async def test_enqueue_start_failure_keeps_queue_and_emits_revision() -> None:
    voice = FakeVoiceClient()
    factory = FailingFactory("queued")
    events = []

    async def state_changed(revision, snapshot) -> None:
        events.append((revision, snapshot))

    session = GuildAudioSession(1, voice, factory, state_changed=state_changed)
    with pytest.raises(RuntimeError, match="private-source-detail"):
        await session.enqueue(_track("queued"))

    snapshot = await session.snapshot()
    assert snapshot.current is None
    assert [track.title for track in snapshot.upcoming] == ["queued"]
    assert snapshot.revision == 1
    assert [(revision, item.revision) for revision, item in events] == [(1, 1)]


@pytest.mark.asyncio
async def test_add_speech_notifies_when_retained_music_queue_starts() -> None:
    voice = FakeVoiceClient()
    factory = FailingFactory("music")
    events = []

    async def state_changed(revision, snapshot) -> None:
        events.append((revision, snapshot))

    session = GuildAudioSession(1, voice, factory, state_changed=state_changed)
    with pytest.raises(RuntimeError):
        await session.enqueue(_track("music"))
    factory.failed_titles.clear()

    assert await session.add_speech(FakeSource(_pcm(2_000))) == 1

    snapshot = await session.snapshot()
    assert snapshot.current is not None
    assert snapshot.current.title == "music"
    assert snapshot.upcoming == ()
    assert snapshot.revision == 2
    assert [revision for revision, _snapshot in events] == [1, 2]


@pytest.mark.asyncio
async def test_enqueue_drops_preexisting_unauthorized_track_and_notifies_remaining_queue() -> None:
    voice = FakeVoiceClient()
    factory = FailingFactory("old")
    denied_titles: set[str] = set()
    events = []

    async def state_changed(revision, snapshot) -> None:
        events.append((revision, snapshot))

    session = GuildAudioSession(
        1,
        voice,
        factory,
        track_allowed=lambda track: track.title not in denied_titles,
        state_changed=state_changed,
    )
    with pytest.raises(RuntimeError):
        await session.enqueue(_track("old"))
    factory.failed_titles.clear()
    denied_titles.add("old")

    with pytest.raises(TrackSourceAuthorizationError):
        await session.enqueue(_track("good"))

    snapshot = await session.snapshot()
    assert snapshot.current is None
    assert [track.title for track in snapshot.upcoming] == ["good"]
    assert snapshot.revision == 2
    assert [revision for revision, _snapshot in events] == [1, 2]


@pytest.mark.asyncio
async def test_cancelled_source_creation_rolls_back_track_and_notifies_truth() -> None:
    voice = FakeVoiceClient()
    factory = BlockingFactory()
    events = []

    async def state_changed(revision, snapshot) -> None:
        events.append((revision, snapshot))

    session = GuildAudioSession(1, voice, factory, state_changed=state_changed)
    task = asyncio.create_task(session.enqueue(_track("cancelled")))
    assert await asyncio.to_thread(factory.started.wait, 1.0)
    task.cancel()
    factory.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = await session.snapshot()
    assert snapshot.current is None
    assert [track.title for track in snapshot.upcoming] == ["cancelled"]
    assert snapshot.revision == 1
    assert [revision for revision, _snapshot in events] == [1]
    for _attempt in range(20):
        await asyncio.sleep(0.01)
        if factory.abandoned_source is not None and factory.abandoned_source.cleaned:
            break
    assert factory.abandoned_source is not None
    assert factory.abandoned_source.cleaned


@pytest.mark.asyncio
async def test_resume_notifies_partial_auth_drop_before_source_creation_cancel() -> None:
    voice = FakeVoiceClient()
    factory = BlockingFactory()
    events = []

    async def state_changed(revision, snapshot) -> None:
        events.append((revision, snapshot))

    session = GuildAudioSession(
        1,
        voice,
        factory,
        track_allowed=lambda track: track.title != "denied",
        state_changed=state_changed,
    )
    await session.restore_projection(
        (_track("denied"), _track("remaining")),
        loop_mode=LoopMode.OFF,
        paused=True,
        music_volume=0.75,
        speech_volume=1.0,
    )
    task = asyncio.create_task(session.resume())
    assert await asyncio.to_thread(factory.started.wait, 1.0)
    task.cancel()
    factory.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = await session.snapshot()
    assert snapshot.paused
    assert snapshot.current is None
    assert [track.title for track in snapshot.upcoming] == ["remaining"]
    assert snapshot.revision == 2
    assert [revision for revision, _snapshot in events] == [1, 2]
    for _attempt in range(20):
        await asyncio.sleep(0.01)
        if factory.abandoned_source is not None and factory.abandoned_source.cleaned:
            break
    assert factory.abandoned_source is not None
    assert factory.abandoned_source.cleaned


@pytest.mark.asyncio
async def test_automatic_start_failure_keeps_queue_notifies_and_does_not_escape_task(caplog) -> None:
    voice = FakeVoiceClient()
    factory = FailingFactory("second")
    events = []

    async def state_changed(revision, snapshot) -> None:
        events.append((revision, snapshot))

    session = GuildAudioSession(1, voice, factory, state_changed=state_changed)
    await session.enqueue(_track("first"))
    await session.enqueue(_track("second"))

    voice.drain()
    for _attempt in range(20):
        await asyncio.sleep(0.01)
        if events[-1][0] == 3:
            break

    snapshot = await session.snapshot()
    assert snapshot.current is None
    assert [track.title for track in snapshot.upcoming] == ["second"]
    assert snapshot.revision == 3
    assert "private-source-detail" not in caplog.text
    assert "second" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_volume", [True, float("nan"), float("inf"), float("-inf")])
async def test_invalid_speech_volume_is_rejected_before_mutation(invalid_volume) -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    events = []

    async def state_changed(revision, snapshot) -> None:
        events.append((revision, snapshot))

    with pytest.raises(ValueError):
        GuildAudioSession(1, voice, factory, speech_volume=invalid_volume)
    with pytest.raises(ValueError):
        GuildAudioSession(1, voice, factory, volume=invalid_volume)

    session = GuildAudioSession(1, voice, factory, state_changed=state_changed)
    with pytest.raises(ValueError):
        await session.set_speech_volume(invalid_volume)
    with pytest.raises(ValueError):
        await session.set_volume(invalid_volume)
    with pytest.raises(ValueError):
        await session.restore_projection(
            (_track("first"),),
            loop_mode=LoopMode.OFF,
            paused=False,
            music_volume=0.5,
            speech_volume=invalid_volume,
        )
    with pytest.raises(ValueError):
        await session.restore_projection(
            (_track("first"),),
            loop_mode=LoopMode.OFF,
            paused=False,
            music_volume=invalid_volume,
            speech_volume=1.0,
        )

    snapshot = await session.snapshot()
    assert snapshot.revision == 0
    assert snapshot.volume == 0.75
    assert snapshot.speech_volume == 1.0
    assert snapshot.current is None
    assert snapshot.upcoming == ()
    assert events == []


@pytest.mark.asyncio
async def test_callback_failure_does_not_corrupt_session_and_close_does_not_notify(caplog) -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    calls = 0

    async def state_changed(_revision, _snapshot) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("private-track-title")

    session = GuildAudioSession(1, voice, factory, state_changed=state_changed)
    await session.enqueue(_track("do-not-log-this-title"))

    assert voice.playing
    assert (await session.snapshot()).current.title == "do-not-log-this-title"
    assert "private-track-title" not in caplog.text
    assert "do-not-log-this-title" not in caplog.text

    await session.close()
    assert calls == 1


@pytest.mark.asyncio
async def test_freeze_projection_prevents_finish_race_and_future_mutation() -> None:
    voice = FakeVoiceClient()
    factory = FakeFactory()
    session = GuildAudioSession(1, voice, factory)
    await session.enqueue(_track("first"))
    await session.enqueue(_track("second"))

    frozen = await session.freeze_projection()
    voice.finish()
    await _turn()

    assert (await session.snapshot()) == frozen
    assert frozen.current is not None and frozen.current.title == "first"
    assert [track.title for track in frozen.upcoming] == ["second"]
    with pytest.raises(PlayerStateError, match="closing"):
        await session.enqueue(_track("third"))

    await session.close()
