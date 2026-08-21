from __future__ import annotations

import asyncio
import struct
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from yonerai_discord.modules.audio_core import (
    PCM_FRAME_BYTES,
    LocalMediaLibrary,
    LoopMode,
    QueueSnapshot,
    Track,
)
from yonerai_discord.modules.music.models import (
    GuildAudioProjection,
    MusicActor,
    MusicAuthorizationError,
    MusicSeekUnsupportedError,
    MusicSessionError,
    MusicSpeechReceipt,
    MusicSpeechStatus,
    MusicUnavailableError,
    PersistedMusicTrackRef,
    PlaylistError,
)
from yonerai_discord.modules.music.repository import MusicPlaylistRepository
from yonerai_discord.modules.music.radio import RelatedTrackSelection
from yonerai_discord.modules.music.service import (
    ListenerLifecycleAction,
    ListenerLifecycleDecision,
    MusicService,
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
        self.music_sources: list[FakeSource] = []
        self.speech_sources: list[FakeSource] = []
        self.speech_payloads: list[bytes] = []
        self.seek_offsets: list[int] = []

    def create(self, _track: Track) -> FakeSource:
        source = FakeSource(_pcm(10_000), _pcm(10_000), _pcm(10_000))
        self.music_sources.append(source)
        return source

    def create_at(self, track: Track, seconds: int) -> FakeSource:
        self.seek_offsets.append(seconds)
        return self.create(track)

    def create_speech(self, wav: bytes) -> FakeSource:
        self.speech_payloads.append(wav)
        source = FakeSource(_pcm(2_000))
        self.speech_sources.append(source)
        return source


class CleanupTrackingSource:
    def __init__(self) -> None:
        self.cleanup_thread_id: int | None = None

    def read(self) -> bytes:
        return b""

    def is_opus(self) -> bool:
        raise RuntimeError("invalid speech source")

    def cleanup(self) -> None:
        self.cleanup_thread_id = threading.get_ident()


class BrokenSpeechFactory(FakeFactory):
    def __init__(self) -> None:
        super().__init__()
        self.broken_source = CleanupTrackingSource()

    def create_speech(self, wav: bytes) -> CleanupTrackingSource:
        self.speech_payloads.append(wav)
        return self.broken_source


class FakeVoiceClient:
    def __init__(self, *, channel_id: int | None = None) -> None:
        self.source = None
        self.after = None
        self.playing = False
        self.disconnected = False
        self.play_calls = 0
        self.channel = None if channel_id is None else SimpleNamespace(id=channel_id)

    def play(self, source, *, after=None) -> None:
        if self.playing:
            raise RuntimeError("already playing")
        self.source = source
        self.after = after
        self.playing = True
        self.play_calls += 1

    def stop(self) -> None:
        callback = self.after
        self.playing = False
        self.source = None
        self.after = None
        if callback is not None:
            callback(None)

    def is_playing(self) -> bool:
        return self.playing

    async def disconnect(self, *, force: bool = False) -> None:
        assert force
        self.disconnected = True


class BlockingDisconnectVoiceClient(FakeVoiceClient):
    def __init__(self, *, channel_id: int) -> None:
        super().__init__(channel_id=channel_id)
        self.disconnect_started = asyncio.Event()
        self.disconnect_release = asyncio.Event()

    async def disconnect(self, *, force: bool = False) -> None:
        self.disconnect_started.set()
        await self.disconnect_release.wait()
        await super().disconnect(force=force)


def _library(tmp_path: Path) -> LocalMediaLibrary:
    root = tmp_path / "library"
    root.mkdir()
    for name in ("first.mp3", "second.mp3", "third.mp3"):
        (root / name).write_bytes(b"authorized local media")
    library = LocalMediaLibrary((root,))
    assert library.refresh() == 3
    return library


def _repository(tmp_path: Path) -> MusicPlaylistRepository:
    repository = MusicPlaylistRepository(tmp_path / "music.sqlite3")
    repository.open()
    return repository


def _service(
    tmp_path: Path,
    *,
    max_queue: int = 5,
    max_tracks_per_requester: int | None = None,
    max_speech_queue: int = 2,
    factory: FakeFactory | None = None,
    approved: bool = True,
    state_observer: Callable[[int], None] | None = None,
) -> tuple[MusicService, FakeFactory, MusicPlaylistRepository]:
    factory = factory or FakeFactory()
    repository = _repository(tmp_path)
    library = _library(tmp_path)
    if approved:
        for name in ("first", "second", "third"):
            track = library.resolve_track(name, requester_id=1)
            track_key, digest = library.track_rights_identity(track)
            repository.grant_track_rights(100, track_key, digest)
    service = MusicService(
        library,
        factory,
        repository,
        available=True,
        reason="ready",
        indexed_tracks=3,
        max_queue=max_queue,
        max_tracks_per_requester=max_tracks_per_requester,
        max_speech_queue=max_speech_queue,
        state_observer=state_observer,
    )
    return service, factory, repository


def _persisted_track(
    library: LocalMediaLibrary,
    name: str,
    *,
    requester_id: int = 10,
    retry_count: int = 0,
) -> PersistedMusicTrackRef:
    track = library.seal_track(library.resolve_track(name, requester_id=requester_id))
    assert track.library_ref is not None and track.content_sha256 is not None
    return PersistedMusicTrackRef(
        library_ref=track.library_ref,
        content_sha256=track.content_sha256,
        requester_id=requester_id,
        retry_count=retry_count,
    )


def test_durable_music_service_rejects_queue_larger_than_projection_contract(tmp_path: Path) -> None:
    library = _library(tmp_path)
    repository = _repository(tmp_path)
    try:
        with pytest.raises(ValueError, match="queue limits"):
            MusicService(
                library,
                FakeFactory(),
                repository,
                available=True,
                reason="ready",
                indexed_tracks=3,
                max_queue=101,
            )
    finally:
        repository.close()


def test_music_service_requester_limit_cannot_exceed_code_owned_cap(tmp_path: Path) -> None:
    library = _library(tmp_path)
    repository = _repository(tmp_path)
    try:
        with pytest.raises(ValueError, match="queue limits"):
            MusicService(
                library,
                FakeFactory(),
                repository,
                available=True,
                reason="ready",
                indexed_tracks=3,
                max_queue=50,
                max_tracks_per_requester=11,
            )
    finally:
        repository.close()


@pytest.mark.parametrize("timeout", [0, 3_601])
def test_music_service_rejects_unbounded_listener_idle_timeout(tmp_path: Path, timeout: int) -> None:
    library = _library(tmp_path)
    repository = _repository(tmp_path)
    try:
        with pytest.raises(ValueError, match="listener_idle_timeout_seconds"):
            MusicService(
                library,
                FakeFactory(),
                repository,
                available=True,
                reason="ready",
                listener_idle_timeout_seconds=timeout,
            )
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_join_restores_pending_projection_without_starting_paused_playback(tmp_path: Path) -> None:
    library = _library(tmp_path)
    repository = _repository(tmp_path)
    persisted = tuple(_persisted_track(library, name) for name in ("first", "second", "third"))
    for track in persisted:
        repository.grant_track_rights(100, track.library_ref, track.content_sha256)
    repository.save_audio_projection(
        GuildAudioProjection(
            guild_id=100,
            tracks=persisted,
            loop_mode=LoopMode.QUEUE,
            paused=True,
            music_volume=0.4,
            speech_volume=0.8,
        )
    )
    factory = FakeFactory()
    service = MusicService(
        library,
        factory,
        repository,
        available=True,
        reason="ready",
        indexed_tracks=3,
        max_queue=3,
        pending_projections=repository.list_audio_projections(),
    )
    actor = MusicActor(10, 500)
    try:
        await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500, commit_check=lambda: actor)

        snapshot = await service.snapshot(100)
        assert snapshot.current is None
        assert [track.title for track in snapshot.upcoming] == ["first", "second", "third"]
        assert snapshot.loop_mode is LoopMode.QUEUE
        assert snapshot.paused
        assert snapshot.volume == 0.4
        assert snapshot.speech_volume == 0.8
        assert factory.music_sources == []
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_pending_restore_aborts_without_overwriting_when_queue_limit_shrinks(tmp_path: Path) -> None:
    library = _library(tmp_path)
    repository = _repository(tmp_path)
    persisted = tuple(_persisted_track(library, name) for name in ("first", "second", "third"))
    for track in persisted:
        repository.grant_track_rights(100, track.library_ref, track.content_sha256)
    original = GuildAudioProjection(guild_id=100, tracks=persisted, paused=True)
    repository.save_audio_projection(original)
    service = MusicService(
        library,
        FakeFactory(),
        repository,
        available=True,
        reason="ready",
        indexed_tracks=3,
        max_queue=2,
        pending_projections=(original,),
    )
    actor = MusicActor(10, 500)
    voice = FakeVoiceClient()
    try:
        with pytest.raises(MusicUnavailableError, match="queue limit"):
            await service.join(100, voice, actor, voice_channel_id=500, commit_check=lambda: actor)

        assert voice.disconnected
        assert service.session_channel_id(100) is None
        assert repository.load_audio_projection(100) == original
        assert tuple(service._pending_projections) == (100,)
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_pending_restore_retries_after_fresh_authorization_failure(tmp_path: Path) -> None:
    library = _library(tmp_path)
    repository = _repository(tmp_path)
    persisted = _persisted_track(library, "first")
    repository.grant_track_rights(100, persisted.library_ref, persisted.content_sha256)
    repository.save_audio_projection(GuildAudioProjection(guild_id=100, tracks=(persisted,), paused=True))
    factory = FakeFactory()
    service = MusicService(
        library,
        factory,
        repository,
        available=True,
        reason="ready",
        indexed_tracks=3,
        pending_projections=repository.list_audio_projections(),
    )
    actor = MusicActor(10, 500)
    checks = 0

    def revoked_after_resolve() -> MusicActor | bool:
        nonlocal checks
        checks += 1
        return actor if checks == 1 else False

    first_voice = FakeVoiceClient()
    second_voice = FakeVoiceClient()
    try:
        with pytest.raises(MusicAuthorizationError, match="policy changed"):
            await service.join(
                100,
                first_voice,
                actor,
                voice_channel_id=500,
                commit_check=revoked_after_resolve,
            )
        assert first_voice.disconnected
        assert service.session_channel_id(100) is None

        await service.join(100, second_voice, actor, voice_channel_id=500, commit_check=lambda: actor)
        snapshot = await service.snapshot(100)
        assert [track.title for track in snapshot.upcoming] == ["first"]
        assert snapshot.paused
        assert factory.music_sources == []
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_pending_restore_retries_after_transient_rights_lookup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _library(tmp_path)
    repository = _repository(tmp_path)
    persisted = _persisted_track(library, "first")
    repository.grant_track_rights(100, persisted.library_ref, persisted.content_sha256)
    original = GuildAudioProjection(guild_id=100, tracks=(persisted,), paused=True)
    repository.save_audio_projection(original)
    service = MusicService(
        library,
        FakeFactory(),
        repository,
        available=True,
        reason="ready",
        indexed_tracks=3,
        pending_projections=(original,),
    )
    actor = MusicActor(10, 500)
    real_check = repository.track_rights_allowed
    failed = False

    def transient_check(*args: object) -> bool:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("private database detail")
        return real_check(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(repository, "track_rights_allowed", transient_check)
    first_voice = FakeVoiceClient()
    try:
        with pytest.raises(MusicUnavailableError, match="could not be restored"):
            await service.join(100, first_voice, actor, voice_channel_id=500, commit_check=lambda: actor)
        assert first_voice.disconnected
        assert repository.load_audio_projection(100) == original
        assert tuple(service._pending_projections) == (100,)

        await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500, commit_check=lambda: actor)
        snapshot = await service.snapshot(100)
        assert [track.title for track in snapshot.upcoming] == ["first"]
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_restore_skips_unavailable_changed_and_rights_revoked_tracks_without_content_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    library = _library(tmp_path)
    repository = _repository(tmp_path)
    persisted = tuple(_persisted_track(library, name) for name in ("first", "second", "third"))
    for track in persisted[:2]:
        repository.grant_track_rights(100, track.library_ref, track.content_sha256)
    repository.save_audio_projection(GuildAudioProjection(guild_id=100, tracks=persisted, paused=True))
    (tmp_path / "library" / "first.mp3").unlink()
    (tmp_path / "library" / "second.mp3").write_bytes(b"changed local media")
    factory = FakeFactory()
    service = MusicService(
        library,
        factory,
        repository,
        available=True,
        reason="ready",
        indexed_tracks=3,
        pending_projections=repository.list_audio_projections(),
    )
    actor = MusicActor(10, 500)
    try:
        await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500, commit_check=lambda: actor)

        snapshot = await service.snapshot(100)
        assert snapshot.current is None
        assert snapshot.upcoming == ()
        assert factory.music_sources == []
        assert str(tmp_path) not in caplog.text
        assert all(track.library_ref not in caplog.text for track in persisted)
        assert all(track.content_sha256 not in caplog.text for track in persisted)
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_listener_lifecycle_decisions_are_guild_scoped_and_bounded(tmp_path: Path) -> None:
    service, _factory, repository = _service(tmp_path)
    first_voice = FakeVoiceClient()
    second_voice = FakeVoiceClient()
    first_actor = MusicActor(10, 500)
    second_actor = MusicActor(20, 600)
    try:
        await service.join(100, first_voice, first_actor, voice_channel_id=500)
        await service.join(200, second_voice, second_actor, voice_channel_id=600)

        waiting = service.listener_lifecycle_decision(
            100,
            connected_voice_channel_id=500,
            human_listener_count=0,
            idle_elapsed_seconds=299,
        )
        leave = service.listener_lifecycle_decision(
            100,
            connected_voice_channel_id=500,
            human_listener_count=0,
            idle_elapsed_seconds=300,
        )
        keep = service.listener_lifecycle_decision(
            200,
            connected_voice_channel_id=600,
            human_listener_count=1,
            idle_elapsed_seconds=0,
        )

        assert waiting.action is ListenerLifecycleAction.WAIT_FOR_LISTENER
        assert waiting.idle_timeout_seconds == 300
        assert leave.action is ListenerLifecycleAction.PRESERVE_AND_DISCONNECT
        assert keep.action is ListenerLifecycleAction.KEEP_ACTIVE
        assert service.session_channel_id(100) == 500
        assert service.session_channel_id(200) == 600
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_voice_free_play_persists_without_source_and_only_requester_can_start_same_process(
    tmp_path: Path,
) -> None:
    service, factory, repository = _service(tmp_path)
    requester = MusicActor(10, None)
    try:
        track, position = await service.play(
            100,
            "first",
            requester,
            commit_check=lambda: requester,
        )

        projection = repository.load_audio_projection(100)
        assert track.title == "first"
        assert position == 1
        assert projection is not None
        assert len(projection.tracks) == 1
        assert projection.tracks[0].requester_id == requester.user_id
        assert factory.music_sources == []
        assert service.session_channel_id(100) is None
        assert (
            await service.listener_lifecycle_decision_current(
                100,
                connected_voice_channel_id=None,
                human_listener_count=1,
                idle_elapsed_seconds=0,
                eligible_listener_voice_channel_id=500,
                eligible_listener_user_id=requester.user_id,
            )
        ).action is ListenerLifecycleAction.RECONNECT_ELIGIBLE
        assert (
            await service.listener_lifecycle_decision_current(
                100,
                connected_voice_channel_id=None,
                human_listener_count=1,
                idle_elapsed_seconds=0,
                eligible_listener_voice_channel_id=500,
                eligible_listener_user_id=99,
                eligible_listener_can_manage=True,
            )
        ).action is ListenerLifecycleAction.EXPLICIT_JOIN_REQUIRED
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_music_service_notifies_dashboard_observer_after_committed_state_change(
    tmp_path: Path,
) -> None:
    observed_guilds: list[int] = []
    service, _factory, repository = _service(
        tmp_path,
        state_observer=observed_guilds.append,
    )
    requester = MusicActor(10, None)
    try:
        await service.play(
            100,
            "first",
            requester,
            commit_check=lambda: requester,
        )

        assert observed_guilds == [100]
        assert repository.load_audio_projection(100) is not None
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_voice_free_play_rejects_fresh_voice_change_without_persistence(tmp_path: Path) -> None:
    service, factory, repository = _service(tmp_path)
    actor = MusicActor(10, None)
    try:
        with pytest.raises(MusicAuthorizationError, match="fresh authorization"):
            await service.play(100, "first", actor)
        with pytest.raises(MusicAuthorizationError, match="voice state changed"):
            await service.play(
                100,
                "first",
                actor,
                commit_check=lambda: MusicActor(actor.user_id, 500),
            )

        assert repository.load_audio_projection(100) is None
        assert service._pending_projections == {}
        assert factory.music_sources == []
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_voice_free_play_keeps_total_and_per_requester_queue_limits(tmp_path: Path) -> None:
    service, factory, repository = _service(
        tmp_path,
        max_queue=2,
        max_tracks_per_requester=1,
    )
    first = MusicActor(10, None)
    second = MusicActor(20, None)
    third = MusicActor(30, None)
    try:
        await service.play(100, "first", first, commit_check=lambda: first)
        with pytest.raises(MusicSessionError, match="requester"):
            await service.play(100, "second", first, commit_check=lambda: first)

        await service.play(100, "second", second, commit_check=lambda: second)
        with pytest.raises(MusicSessionError, match="full"):
            await service.play(100, "third", third, commit_check=lambda: third)

        projection = repository.load_audio_projection(100)
        assert projection is not None
        assert [item.requester_id for item in projection.tracks] == [10, 20]
        assert factory.music_sources == []
        assert (
            await service.listener_lifecycle_decision_current(
                100,
                connected_voice_channel_id=None,
                human_listener_count=1,
                idle_elapsed_seconds=0,
                eligible_listener_voice_channel_id=500,
                eligible_listener_user_id=second.user_id,
            )
        ).action is ListenerLifecycleAction.RECONNECT_ELIGIBLE
    finally:
        await service.close()
        repository.close()


@pytest.mark.parametrize("revoke", ("capability", "rights", "service"))
@pytest.mark.asyncio
async def test_voice_free_play_rechecks_authorization_after_projection_lock_wait(
    tmp_path: Path,
    revoke: str,
) -> None:
    service, factory, repository = _service(tmp_path)
    assert service.library is not None
    actor = MusicActor(10, None)
    first_check = asyncio.Event()
    allowed = True

    async def commit_check() -> MusicActor | None:
        first_check.set()
        return actor if allowed else None

    projection_lock = service._projection_lock(100)
    await projection_lock.acquire()
    task = asyncio.create_task(service.play(100, "first", actor, commit_check=commit_check))
    try:
        await asyncio.wait_for(first_check.wait(), timeout=1.0)
        if revoke == "capability":
            allowed = False
        elif revoke == "service":
            service.available = False
            service.reason = "policy-off"
        else:
            track = service.library.resolve_track("first", requester_id=actor.user_id)
            track_key, _digest = service.library.track_rights_identity(track)
            assert repository.revoke_track_rights(100, track_key)
        projection_lock.release()

        with pytest.raises((MusicAuthorizationError, MusicUnavailableError)):
            await task
        assert repository.load_audio_projection(100) is None
        assert service._pending_projections == {}
        assert factory.music_sources == []
    finally:
        if projection_lock.locked():
            projection_lock.release()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        service.available = True
        service.reason = "ready"
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_voice_free_play_cancellation_waits_for_commit_then_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, repository = _service(tmp_path)
    actor = MusicActor(10, None)
    started = threading.Event()
    release = threading.Event()
    original_save = repository.save_audio_projection

    def blocking_save(projection: GuildAudioProjection) -> GuildAudioProjection:
        started.set()
        assert release.wait(timeout=2.0)
        return original_save(projection)

    monkeypatch.setattr(repository, "save_audio_projection", blocking_save)
    task = asyncio.create_task(service.play(100, "first", actor, commit_check=lambda: actor))
    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert repository.load_audio_projection(100) is not None
        assert 100 in service._pending_projections
        assert factory.music_sources == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_voice_free_play_inner_save_cancellation_does_not_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, repository = _service(tmp_path)
    actor = MusicActor(10, None)

    def cancel_save(_projection: GuildAudioProjection) -> GuildAudioProjection:
        raise asyncio.CancelledError

    monkeypatch.setattr(repository, "save_audio_projection", cancel_save)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                service.play(100, "first", actor, commit_check=lambda: actor),
                timeout=1.0,
            )
        assert repository.load_audio_projection(100) is None
        assert service._pending_projections == {}
        assert factory.music_sources == []
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_voice_free_play_commit_precedes_policy_close_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, repository = _service(tmp_path)
    actor = MusicActor(10, None)
    started = threading.Event()
    release = threading.Event()
    original_save = repository.save_audio_projection

    def blocking_save(projection: GuildAudioProjection) -> GuildAudioProjection:
        started.set()
        assert release.wait(timeout=2.0)
        return original_save(projection)

    monkeypatch.setattr(repository, "save_audio_projection", blocking_save)
    play_task = asyncio.create_task(service.play(100, "first", actor, commit_check=lambda: actor))
    close_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        close_task = asyncio.create_task(service.close(delete_projections=True))
        await asyncio.sleep(0)
        assert not close_task.done()

        release.set()
        await play_task
        await close_task
        assert repository.load_audio_projection(100) is None
        assert service._pending_projections == {}
        assert factory.music_sources == []
    finally:
        release.set()
        if not play_task.done():
            await play_task
        if close_task is not None and not close_task.done():
            await close_task
        if not service._closed:
            await service.close(delete_projections=True)
        repository.close()


@pytest.mark.asyncio
async def test_voice_free_play_commit_serializes_listener_decision_and_restart_requires_explicit_join(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, repository = _service(tmp_path)
    assert service.library is not None
    actor = MusicActor(10, None)
    started = threading.Event()
    release = threading.Event()
    original_save = repository.save_audio_projection

    def blocking_save(projection: GuildAudioProjection) -> GuildAudioProjection:
        started.set()
        assert release.wait(timeout=2.0)
        return original_save(projection)

    monkeypatch.setattr(repository, "save_audio_projection", blocking_save)
    play_task = asyncio.create_task(service.play(100, "first", actor, commit_check=lambda: actor))
    decision_task: asyncio.Task[ListenerLifecycleDecision] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        decision_task = asyncio.create_task(
            service.listener_lifecycle_decision_current(
                100,
                connected_voice_channel_id=None,
                human_listener_count=1,
                idle_elapsed_seconds=0,
                eligible_listener_voice_channel_id=500,
                eligible_listener_user_id=actor.user_id,
            )
        )
        await asyncio.sleep(0)
        assert not decision_task.done()
        release.set()
        await play_task
        assert (await decision_task).action is ListenerLifecycleAction.RECONNECT_ELIGIBLE
        assert factory.music_sources == []

        library = service.library
        await service.close()
        restarted_factory = FakeFactory()
        restarted = MusicService(
            library,
            restarted_factory,
            repository,
            available=True,
            reason="ready",
            indexed_tracks=3,
            pending_projections=repository.list_audio_projections(),
        )
        joined_actor = MusicActor(actor.user_id, 500)
        try:
            decision = await restarted.listener_lifecycle_decision_current(
                100,
                connected_voice_channel_id=None,
                human_listener_count=1,
                idle_elapsed_seconds=0,
                eligible_listener_voice_channel_id=500,
                eligible_listener_user_id=actor.user_id,
            )
            assert decision.action is ListenerLifecycleAction.EXPLICIT_JOIN_REQUIRED
            with pytest.raises(MusicSessionError, match="explicit music join"):
                await restarted.play(
                    100,
                    "second",
                    MusicActor(actor.user_id, None),
                    commit_check=lambda: MusicActor(actor.user_id, None),
                )

            await restarted.join(
                100,
                FakeVoiceClient(),
                joined_actor,
                voice_channel_id=500,
                commit_check=lambda: joined_actor,
            )
            assert (await restarted.snapshot(100)).current is not None
            assert len(restarted_factory.music_sources) == 1
        finally:
            await restarted.close()
    finally:
        release.set()
        if not play_task.done():
            await play_task
        if decision_task is not None and not decision_task.done():
            await decision_task
        if not service._closed:
            await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_suspend_preserves_projection_and_requires_same_channel_listener_to_reconnect(
    tmp_path: Path,
) -> None:
    service, _factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    try:
        await service.join(100, voice, actor, voice_channel_id=500)
        track, _position = await service.play(100, "first", actor)
        active = service.listener_lifecycle_decision(
            100,
            connected_voice_channel_id=500,
            human_listener_count=0,
            idle_elapsed_seconds=300,
        )
        assert active.session_identity is not None

        projection = await service.suspend_voice_session(
            100,
            expected_voice_channel_id=500,
            expected_session_identity=active.session_identity,
        )

        assert voice.disconnected
        assert service.session_channel_id(100) is None
        assert projection == repository.load_audio_projection(100)
        assert len(projection.tracks) == 1
        assert projection.tracks[0].requester_id == track.requester_id
        eligible = service.listener_lifecycle_decision(
            100,
            connected_voice_channel_id=None,
            human_listener_count=1,
            idle_elapsed_seconds=0,
            eligible_listener_voice_channel_id=500,
            eligible_listener_user_id=10,
        )
        wrong_channel = service.listener_lifecycle_decision(
            100,
            connected_voice_channel_id=None,
            human_listener_count=1,
            idle_elapsed_seconds=0,
            eligible_listener_voice_channel_id=501,
            eligible_listener_user_id=10,
        )
        unrelated_listener = service.listener_lifecycle_decision(
            100,
            connected_voice_channel_id=None,
            human_listener_count=1,
            idle_elapsed_seconds=0,
            eligible_listener_voice_channel_id=500,
            eligible_listener_user_id=99,
        )
        assert eligible.action is ListenerLifecycleAction.RECONNECT_ELIGIBLE
        assert eligible.voice_channel_id == 500
        assert wrong_channel.action is ListenerLifecycleAction.EXPLICIT_JOIN_REQUIRED
        assert unrelated_listener.action is ListenerLifecycleAction.EXPLICIT_JOIN_REQUIRED

        restored_voice = FakeVoiceClient()
        await service.join(100, restored_voice, actor, voice_channel_id=500)
        assert (await service.snapshot(100)).current is not None
        assert (
            service.listener_lifecycle_decision(
                100,
                connected_voice_channel_id=500,
                human_listener_count=1,
                idle_elapsed_seconds=0,
            ).action
            is ListenerLifecycleAction.KEEP_ACTIVE
        )
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_voice_connection_loss_requests_preserving_disconnect_without_cross_guild_mutation(
    tmp_path: Path,
) -> None:
    service, _factory, repository = _service(tmp_path)
    first_voice = FakeVoiceClient()
    second_voice = FakeVoiceClient()
    try:
        await service.join(100, first_voice, MusicActor(10, 500), voice_channel_id=500)
        await service.join(200, second_voice, MusicActor(20, 600), voice_channel_id=600)

        lost = service.listener_lifecycle_decision(
            100,
            connected_voice_channel_id=None,
            human_listener_count=1,
            idle_elapsed_seconds=0,
        )

        assert lost.action is ListenerLifecycleAction.PRESERVE_AND_DISCONNECT
        assert lost.reason == "voice_connection_lost"
        assert lost.voice_channel_id == 500
        assert service.session_channel_id(200) == 600
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_stale_listener_timer_cannot_suspend_replacement_session_in_same_channel(tmp_path: Path) -> None:
    service, _factory, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    original_voice = FakeVoiceClient()
    replacement_voice = FakeVoiceClient()
    try:
        await service.join(100, original_voice, actor, voice_channel_id=500)
        stale = service.listener_lifecycle_decision(
            100,
            connected_voice_channel_id=500,
            human_listener_count=0,
            idle_elapsed_seconds=300,
        )
        assert stale.session_identity is not None
        assert service.listener_session_identity(100) is stale.session_identity
        assert await service.leave(100, actor)
        await service.join(100, replacement_voice, actor, voice_channel_id=500)

        with pytest.raises(MusicSessionError, match="binding changed"):
            await service.suspend_voice_session(
                100,
                expected_voice_channel_id=500,
                expected_session_identity=stale.session_identity,
            )

        assert service.session_channel_id(100) == 500
        assert not replacement_voice.disconnected
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_existing_join_requires_exact_voice_client_and_current_channel(tmp_path: Path) -> None:
    service, _factory, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    voice = FakeVoiceClient(channel_id=500)
    try:
        session = await service.join(100, voice, actor, voice_channel_id=500)
        assert await service.join(100, voice, actor, voice_channel_id=500) is session

        with pytest.raises(MusicSessionError, match="connection changed"):
            await service.join(
                100,
                FakeVoiceClient(channel_id=500),
                actor,
                voice_channel_id=500,
            )
        voice.channel.id = 501
        with pytest.raises(MusicSessionError, match="connection changed"):
            await service.join(100, voice, actor, voice_channel_id=500)

        assert service.session_channel_id(100) == 500
        assert not voice.disconnected
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_suspend_external_cancellation_waits_for_disconnect_cleanup(tmp_path: Path) -> None:
    service, _factory, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    voice = BlockingDisconnectVoiceClient(channel_id=500)
    await service.join(100, voice, actor, voice_channel_id=500)
    active = service.listener_lifecycle_decision(
        100,
        connected_voice_channel_id=500,
        human_listener_count=0,
        idle_elapsed_seconds=300,
    )
    assert active.session_identity is not None
    task = asyncio.create_task(
        service.suspend_voice_session(
            100,
            expected_voice_channel_id=500,
            expected_session_identity=active.session_identity,
        )
    )
    try:
        await asyncio.wait_for(voice.disconnect_started.wait(), timeout=1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        voice.disconnect_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert voice.disconnected
        assert service.session_channel_id(100) is None
        assert repository.load_audio_projection(100) is not None
    finally:
        voice.disconnect_release.set()
        if not task.done():
            with pytest.raises(asyncio.CancelledError):
                await task
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_suspend_rejects_stale_channel_and_closing_service_without_mutation(tmp_path: Path) -> None:
    service, _factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    try:
        await service.join(100, voice, actor, voice_channel_id=500)
        active = service.listener_lifecycle_decision(
            100,
            connected_voice_channel_id=500,
            human_listener_count=0,
            idle_elapsed_seconds=300,
        )
        assert active.session_identity is not None
        with pytest.raises(MusicSessionError, match="binding changed"):
            await service.suspend_voice_session(
                100,
                expected_voice_channel_id=501,
                expected_session_identity=active.session_identity,
            )
        assert service.session_channel_id(100) == 500
        assert not voice.disconnected

        await service.begin_close()
        decision = service.listener_lifecycle_decision(
            100,
            connected_voice_channel_id=500,
            human_listener_count=0,
            idle_elapsed_seconds=300,
        )
        assert decision.action is ListenerLifecycleAction.UNAVAILABLE
        with pytest.raises(MusicUnavailableError):
            await service.suspend_voice_session(
                100,
                expected_voice_channel_id=500,
                expected_session_identity=active.session_identity,
            )
        assert service.session_channel_id(100) == 500
        assert not voice.disconnected
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_suspend_closes_frozen_session_when_final_projection_cannot_be_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    await service.join(100, voice, actor, voice_channel_id=500)
    active = service.listener_lifecycle_decision(
        100,
        connected_voice_channel_id=500,
        human_listener_count=0,
        idle_elapsed_seconds=300,
    )
    assert active.session_identity is not None

    def fail_save(_projection: GuildAudioProjection) -> GuildAudioProjection:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(repository, "save_audio_projection", fail_save)
    try:
        with pytest.raises(MusicSessionError, match="could not be saved"):
            await service.suspend_voice_session(
                100,
                expected_voice_channel_id=500,
                expected_session_identity=active.session_identity,
            )
        assert voice.disconnected
        assert service.session_channel_id(100) is None
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_projection_save_rejects_out_of_order_revision_and_repository_swap(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    replacement_repository = MusicPlaylistRepository(tmp_path / "replacement.sqlite3")
    replacement_repository.open()
    actor = MusicActor(10, 500)
    await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
    assert service.library is not None
    session = service._sessions[100].session
    sealed = service.library.seal_track(service.library.resolve_track("first", requester_id=actor.user_id))
    newer = QueueSnapshot(
        None,
        (sealed,),
        LoopMode.QUEUE,
        True,
        0.4,
        speech_volume=0.8,
        revision=2,
    )
    older = QueueSnapshot(
        None,
        (),
        LoopMode.OFF,
        False,
        1.0,
        speech_volume=1.0,
        revision=1,
    )
    try:
        assert await service._save_session_projection(
            100,
            expected_session=session,  # type: ignore[arg-type]
            expected_library=service.library,
            expected_repository=repository,
            revision=2,
            snapshot=newer,
        )
        assert not await service._save_session_projection(
            100,
            expected_session=session,  # type: ignore[arg-type]
            expected_library=service.library,
            expected_repository=repository,
            revision=1,
            snapshot=older,
        )
        loaded = repository.load_audio_projection(100)
        assert loaded is not None
        assert loaded.loop_mode is LoopMode.QUEUE
        assert loaded.paused
        assert loaded.music_volume == 0.4
        assert loaded.speech_volume == 0.8
        assert len(loaded.tracks) == 1

        service.repository = replacement_repository
        assert not await service._save_session_projection(
            100,
            expected_session=session,  # type: ignore[arg-type]
            expected_library=service.library,
            expected_repository=repository,
            revision=3,
            snapshot=QueueSnapshot(
                None,
                (),
                LoopMode.OFF,
                False,
                1.0,
                revision=3,
            ),
        )
        assert replacement_repository.load_audio_projection(100) is None
    finally:
        service.repository = repository
        await service.close()
        repository.close()
        replacement_repository.close()


@pytest.mark.asyncio
async def test_normal_close_reports_projection_failure_and_disconnects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    voice = FakeVoiceClient()
    await service.join(100, voice, actor, voice_channel_id=500)

    def fail_save(_projection: GuildAudioProjection) -> GuildAudioProjection:
        raise RuntimeError("private sqlite detail")

    monkeypatch.setattr(repository, "save_audio_projection", fail_save)
    await service.play(100, "first", actor)

    with pytest.raises(MusicSessionError, match="could not be saved"):
        await service.close()

    assert voice.disconnected
    assert service.reason == "closed"
    repository.close()


@pytest.mark.asyncio
async def test_normal_close_freezes_finish_before_persisting_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    voice = FakeVoiceClient()
    await service.join(100, voice, actor, voice_channel_id=500)
    await service.play(100, "first", actor)
    await service.play(100, "second", actor)
    session = service._sessions[100].session
    original_freeze = session.freeze_projection

    async def freeze_then_finish() -> QueueSnapshot:
        snapshot = await original_freeze()
        voice.stop()
        await asyncio.sleep(0)
        return snapshot

    monkeypatch.setattr(session, "freeze_projection", freeze_then_finish)
    await service.close()

    stored = repository.load_audio_projection(100)
    assert stored is not None
    assert len(stored.tracks) == 2
    assert [track.requester_id for track in stored.tracks] == [actor.user_id, actor.user_id]
    repository.close()


@pytest.mark.asyncio
async def test_begin_close_waits_for_active_guild_operation_before_final_snapshot(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
    guild_lock = service._lock(100)
    await guild_lock.acquire()
    close_task = asyncio.create_task(service.begin_close())
    try:
        await asyncio.sleep(0)
        assert service.reason == "closing"
        assert not close_task.done()
    finally:
        guild_lock.release()
    await close_task
    assert repository.load_audio_projection(100) is not None
    await service.close()
    repository.close()


@pytest.mark.asyncio
async def test_policy_projection_delete_failure_remains_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _library(tmp_path)
    repository = _repository(tmp_path)
    persisted = _persisted_track(library, "first")
    original = GuildAudioProjection(guild_id=100, tracks=(persisted,), paused=True)
    repository.save_audio_projection(original)
    service = MusicService(
        library,
        FakeFactory(),
        repository,
        available=True,
        reason="ready",
        indexed_tracks=3,
        pending_projections=(original,),
    )
    real_delete = repository.delete_audio_projection
    calls = 0

    def fail_once(guild_id: int) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private sqlite detail")
        return real_delete(guild_id)

    monkeypatch.setattr(repository, "delete_audio_projection", fail_once)
    with pytest.raises(MusicSessionError, match="could not be deleted"):
        await service.close(delete_projections=True)
    assert repository.load_audio_projection(100) == original
    assert tuple(service._pending_projections) == (100,)

    await service.close(delete_projections=True)
    assert repository.load_audio_projection(100) is None
    assert service._pending_projections == {}
    repository.close()


@pytest.mark.asyncio
async def test_normal_close_preserves_projection_and_policy_close_removes_pending(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
    await service.play(100, "first", actor)
    await service.close()
    stored = repository.load_audio_projection(100)
    assert stored is not None
    assert len(stored.tracks) == 1

    assert service.library is not None and service.source_factory is not None
    policy_service = MusicService(
        service.library,
        service.source_factory,
        repository,
        available=True,
        reason="ready",
        indexed_tracks=3,
        pending_projections=repository.list_audio_projections(),
    )
    try:
        assert await policy_service.close_guild(100)
        assert repository.load_audio_projection(100) is None
    finally:
        await policy_service.close()
        repository.close()


@pytest.mark.asyncio
async def test_policy_close_waits_for_inflight_projection_save_then_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    voice = FakeVoiceClient()
    await service.join(100, voice, actor, voice_channel_id=500)
    assert service.library is not None
    session = service._sessions[100].session
    sealed = service.library.seal_track(service.library.resolve_track("first", requester_id=actor.user_id))
    snapshot = QueueSnapshot(
        None,
        (sealed,),
        LoopMode.OFF,
        True,
        0.65,
        revision=1,
    )
    started = threading.Event()
    release = threading.Event()
    original_save = repository.save_audio_projection

    def blocking_save(projection: GuildAudioProjection) -> GuildAudioProjection:
        started.set()
        assert release.wait(timeout=2.0)
        return original_save(projection)

    monkeypatch.setattr(repository, "save_audio_projection", blocking_save)
    save_task = asyncio.create_task(
        service._save_session_projection(
            100,
            expected_session=session,  # type: ignore[arg-type]
            expected_library=service.library,
            expected_repository=repository,
            revision=1,
            snapshot=snapshot,
        )
    )
    close_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        close_task = asyncio.create_task(service.close(delete_projections=True))
        await asyncio.sleep(0)
        assert service.reason == "closing"
        assert not close_task.done()

        release.set()
        assert not await save_task
        await close_task
        assert repository.load_audio_projection(100) is None
        assert voice.disconnected
    finally:
        release.set()
        if close_task is not None:
            await close_task
        else:
            await service.close(delete_projections=True)
        repository.close()


@pytest.mark.asyncio
async def test_service_requires_same_vc_and_requester_or_manage_guild(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    requester = MusicActor(10, 500)
    another = MusicActor(11, 500)
    wrong_channel_admin = MusicActor(12, 501, manage_guild=True)
    admin = MusicActor(12, 500, manage_guild=True)
    try:
        await service.join(100, voice, requester, voice_channel_id=500)
        track, position = await service.play(100, "first", requester)

        assert track.title == "first"
        assert track.library_ref is not None
        assert track.content_sha256 is not None
        assert position == 1
        with pytest.raises(MusicAuthorizationError, match="requester"):
            await service.pause(100, another)
        with pytest.raises(MusicAuthorizationError, match="same voice"):
            await service.pause(100, wrong_channel_admin)

        await service.pause(100, admin)
        assert (await service.snapshot(100)).paused
        await service.resume(100, requester)
        assert not (await service.snapshot(100)).paused
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_service_seek_replaces_only_music_source_without_restarting_voice(tmp_path: Path) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    try:
        await service.join(100, voice, actor, voice_channel_id=500)
        await service.play(100, "first", actor)

        track = await service.seek(100, actor, 37, commit_check=lambda: actor)
        for _attempt in range(20):
            await asyncio.sleep(0.01)
            if factory.music_sources[0].cleaned:
                break

        assert track.title == "first"
        assert factory.seek_offsets == [37]
        assert voice.play_calls == 1
        assert factory.music_sources[0].cleaned
        assert not factory.music_sources[1].cleaned
        snapshot = await service.snapshot(100)
        assert snapshot.current is track
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_service_seek_fails_closed_when_fresh_authorization_is_revoked(tmp_path: Path) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    calls = 0

    async def revoke_after_source_preflight() -> MusicActor | None:
        nonlocal calls
        calls += 1
        return actor if calls == 1 else None

    try:
        await service.join(100, voice, actor, voice_channel_id=500)
        await service.play(100, "first", actor)

        with pytest.raises(MusicAuthorizationError, match="changed"):
            await service.seek(100, actor, 20, commit_check=revoke_after_source_preflight)
        for _attempt in range(20):
            await asyncio.sleep(0.01)
            if factory.music_sources[1].cleaned:
                break

        assert factory.seek_offsets == [20]
        assert voice.play_calls == 1
        assert not factory.music_sources[0].cleaned
        assert factory.music_sources[1].cleaned
        assert (await service.snapshot(100)).current is not None
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_service_seek_reports_non_seekable_factory_as_unsupported(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    try:
        await service.join(100, voice, actor, voice_channel_id=500)
        await service.play(100, "first", actor)
        service.seek_available = False

        with pytest.raises(MusicSeekUnsupportedError):
            await service.seek(100, actor, 10, commit_check=lambda: actor)
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_unapproved_track_never_enters_queue_or_creates_source(tmp_path: Path) -> None:
    service, factory, repository = _service(tmp_path, approved=False)
    actor = MusicActor(10, 500)
    try:
        await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
        with pytest.raises(MusicAuthorizationError, match="rights"):
            await service.play(100, "first", actor)
        assert factory.music_sources == []
        assert (await service.snapshot(100)).upcoming == ()
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_source_commit_rights_race_rejects_initial_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    checks = 0
    original = service._track_rights_allowed

    def changes_after_queue_check(guild_id: int, track: Track) -> bool:
        nonlocal checks
        checks += 1
        return checks == 1 and original(guild_id, track)

    monkeypatch.setattr(service, "_track_rights_allowed", changes_after_queue_check)
    try:
        await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
        with pytest.raises(MusicAuthorizationError, match="rights"):
            await service.play(100, "first", actor)
        assert checks == 2
        assert factory.music_sources == []
        assert (await service.snapshot(100)).upcoming == ()
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_revoked_or_replaced_queued_track_never_creates_source(tmp_path: Path) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    try:
        await service.join(100, voice, actor, voice_channel_id=500)
        await service.play(100, "first", actor)
        await service.play(100, "second", actor)
        second = service.library.resolve_track("second", requester_id=actor.user_id)  # type: ignore[union-attr]
        track_key, _ = service.library.track_rights_identity(second)  # type: ignore[union-attr]
        assert repository.revoke_track_rights(100, track_key)
        assert voice.after is not None
        voice.after(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(factory.music_sources) == 1
        assert (await service.snapshot(100)).upcoming == ()

        await service.grant_track_rights(100, "second", MusicActor(99, 500, manage_guild=True))
        (tmp_path / "library" / "second.mp3").write_bytes(b"replaced local media")
        with pytest.raises(MusicAuthorizationError, match="rights"):
            await service.play(100, "second", actor)
        assert len(factory.music_sources) == 1
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_service_enforces_queue_and_remove_ownership(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path, max_queue=3)
    voice = FakeVoiceClient()
    first_user = MusicActor(10, 500)
    second_user = MusicActor(11, 500)
    try:
        await service.join(100, voice, first_user, voice_channel_id=500)
        await service.play(100, "first", first_user)
        await service.play(100, "second", second_user)
        await service.play(100, "third", first_user)

        with pytest.raises(MusicSessionError, match="queue is full"):
            await service.play(100, "first", first_user)
        with pytest.raises(MusicAuthorizationError, match="requester"):
            await service.remove(100, first_user, 1)

        removed = await service.remove(100, second_user, 1)
        assert removed.title == "second"
        assert [track.title for track in (await service.snapshot(100)).upcoming] == ["third"]
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_service_moves_owned_track_clears_only_requester_and_sets_speech_volume(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path, max_queue=4)
    voice = FakeVoiceClient()
    first_user = MusicActor(10, 500)
    second_user = MusicActor(11, 500)
    try:
        await service.join(100, voice, first_user, voice_channel_id=500)
        await service.play(100, "first", first_user)
        await service.play(100, "second", second_user)
        await service.play(100, "third", first_user)

        with pytest.raises(MusicAuthorizationError, match="requester"):
            await service.move(100, first_user, 1, 2)
        moved = await service.move(100, second_user, 1, 2, commit_check=lambda: second_user)
        assert moved.title == "second"
        assert [track.title for track in (await service.snapshot(100)).upcoming] == ["third", "second"]

        assert await service.clear_requester(100, first_user, commit_check=lambda: first_user) == 1
        snapshot = await service.snapshot(100)
        assert snapshot.current is not None and snapshot.current.title == "first"
        assert [track.title for track in snapshot.upcoming] == ["second"]

        await service.set_speech_volume(100, first_user, 0.4, commit_check=lambda: first_user)
        assert (await service.snapshot(100)).speech_volume == 0.4
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_control_and_move_reject_queue_revision_change_after_fresh_check(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    first_user = MusicActor(10, 500)
    second_user = MusicActor(11, 500)
    try:
        await service.join(100, voice, first_user, voice_channel_id=500)
        await service.play(100, "first", first_user)
        await service.play(100, "second", second_user)
        await service.play(100, "third", first_user)

        async def finish_during_move_check() -> MusicActor:
            voice.stop()
            for _attempt in range(20):
                await asyncio.sleep(0.01)
                current = (await service.snapshot(100)).current
                if current is not None and current.title == "second":
                    break
            return second_user

        with pytest.raises(MusicSessionError, match="queue changed"):
            await service.move(100, second_user, 1, 2, commit_check=finish_during_move_check)
        shifted = await service.snapshot(100)
        assert shifted.current is not None and shifted.current.title == "second"
        assert [track.title for track in shifted.upcoming] == ["third"]

        async def finish_during_volume_check() -> MusicActor:
            voice.stop()
            for _attempt in range(20):
                await asyncio.sleep(0.01)
                current = (await service.snapshot(100)).current
                if current is not None and current.title == "third":
                    break
            return second_user

        with pytest.raises(MusicSessionError, match="not available"):
            await service.set_speech_volume(100, second_user, 0.2, commit_check=finish_during_volume_check)
        assert (await service.snapshot(100)).speech_volume == 1.0
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_speech_enters_ducking_mixer_without_stopping_music(tmp_path: Path) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    wav = b"RIFF-owner-authorized-voicevox-result"
    try:
        await service.join(100, voice, actor, voice_channel_id=500)
        await service.play(100, "first", actor)
        before = await service.snapshot(100)

        assert await service.add_speech_wav(100, actor, wav) == 1
        mixed_frame = voice.source.read()
        after = await service.snapshot(100)

        assert len(mixed_frame) == PCM_FRAME_BYTES
        # 10,000 * (0.65 * first ducking attack gain 0.93) + 2,000.
        assert struct.unpack_from("<h", mixed_frame)[0] == 8_045
        assert before.current == after.current
        assert after.current.title == "first"
        assert factory.speech_payloads == [wav]
        assert len(factory.music_sources[0].frames) == 2
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_direct_speech_records_only_bounded_scope_receipt_after_enqueue(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    secret_text = "never retain this text"
    secret_wav = b"RIFF-private-wav"
    try:
        await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)

        receipt = await service.add_speech_wav(
            100,
            actor,
            secret_wav,
            receipt_source_channel_id=200,
        )

        assert receipt == MusicSpeechReceipt(100, 200, 10, 500, 1, MusicSpeechStatus.QUEUED)
        assert service.speech_receipts(guild_id=100, source_channel_id=200, requester_id=10) == (receipt,)
        assert service.speech_receipts(guild_id=100, source_channel_id=201, requester_id=10) == ()
        assert service.speech_receipts(guild_id=100, source_channel_id=200, requester_id=11) == ()
        assert secret_text not in repr(receipt)
        assert secret_wav not in repr(receipt).encode()
        assert "path" not in repr(receipt).casefold()
    finally:
        await service.close()
        assert service.speech_receipts(guild_id=100, source_channel_id=200, requester_id=10) == ()
        repository.close()


@pytest.mark.asyncio
async def test_direct_speech_queue_failure_does_not_create_second_receipt(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path, max_speech_queue=1)
    actor = MusicActor(10, 500)
    try:
        await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
        first = await service.add_speech_wav(
            100,
            actor,
            b"RIFF-first",
            receipt_source_channel_id=200,
        )

        with pytest.raises(MusicSessionError, match="speech queue is full"):
            await service.add_speech_wav(
                100,
                actor,
                b"RIFF-second",
                receipt_source_channel_id=200,
            )

        assert service.speech_receipts(guild_id=100, source_channel_id=200, requester_id=10) == (first,)
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_direct_speech_commit_and_close_are_linearized_without_stale_receipt(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    commit_started = asyncio.Event()
    commit_release = asyncio.Event()

    async def fresh_actor() -> MusicActor:
        commit_started.set()
        await commit_release.wait()
        return actor

    await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
    add_task = asyncio.create_task(
        service.add_speech_wav(
            100,
            actor,
            b"RIFF-linearized",
            commit_check=fresh_actor,
            receipt_source_channel_id=200,
        )
    )
    close_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(commit_started.wait(), timeout=1.0)
        close_task = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert not close_task.done()

        commit_release.set()
        receipt = await add_task
        assert isinstance(receipt, MusicSpeechReceipt)
        await close_task
        assert service.speech_receipts(guild_id=100, source_channel_id=200, requester_id=10) == ()
        with pytest.raises(MusicUnavailableError):
            await service.add_speech_wav(
                100,
                actor,
                b"RIFF-after-close",
                receipt_source_channel_id=200,
            )
        assert service.speech_receipts(guild_id=100, source_channel_id=200, requester_id=10) == ()
    finally:
        commit_release.set()
        if not add_task.done():
            await add_task
        if close_task is not None and not close_task.done():
            await close_task
        await service.close()
        repository.close()


def test_speech_receipt_projection_is_bounded_to_latest_one_hundred(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    try:
        service._speech_receipts.extend(
            MusicSpeechReceipt(100, 200, 10, 500, position, MusicSpeechStatus.QUEUED) for position in range(1, 102)
        )

        receipts = service.speech_receipts(guild_id=100, source_channel_id=200, requester_id=10)
        assert len(receipts) == 100
        assert receipts[0].queue_position == 2
        assert receipts[-1].queue_position == 101
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_failed_speech_source_cleanup_runs_off_event_loop_thread(tmp_path: Path) -> None:
    factory = BrokenSpeechFactory()
    service, _, repository = _service(tmp_path, factory=factory)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    event_loop_thread_id = threading.get_ident()
    try:
        await service.join(100, voice, actor, voice_channel_id=500)
        await service.play(100, "first", actor)

        with pytest.raises(RuntimeError, match="invalid speech source"):
            await service.add_speech_wav(100, actor, b"RIFF-broken")

        assert factory.broken_source.cleanup_thread_id is not None
        assert factory.broken_source.cleanup_thread_id != event_loop_thread_id
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_playlist_load_re_resolves_titles_and_cannot_cross_owner(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    owner = MusicActor(10, 500)
    other = MusicActor(11, 500)
    voice = FakeVoiceClient()
    try:
        repository.save(100, owner.user_id, "saved", ("first", "second", "missing"))
        await service.join(100, voice, owner, voice_channel_id=500)

        with pytest.raises(PlaylistError, match="not found"):
            await service.load_playlist(100, other, "saved")
        loaded, missing = await service.load_playlist(100, owner, "saved")

        snapshot = await service.snapshot(100)
        assert (loaded, missing) == (2, 1)
        assert snapshot.current.title == "first"
        assert [track.title for track in snapshot.upcoming] == ["second"]
        assert all(track.requester_id == owner.user_id for track in (snapshot.current, *snapshot.upcoming))
        assert all(track.library_ref is not None for track in (snapshot.current, *snapshot.upcoming))
        assert all(track.content_sha256 is not None for track in (snapshot.current, *snapshot.upcoming))
        assert await service.list_playlists(100, other) == ()
        assert not await service.delete_playlist(100, other, "saved")
        assert repository.load(100, owner.user_id, "saved") is not None
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_service_enforces_per_requester_admission_without_blocking_other_users(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path, max_tracks_per_requester=2)
    owner = MusicActor(10, 500)
    other = MusicActor(11, 500)
    try:
        await service.join(100, FakeVoiceClient(), owner, voice_channel_id=500)
        await service.play(100, "first", owner)
        await service.play(100, "second", owner)
        with pytest.raises(MusicSessionError, match="requester music queue limit"):
            await service.play(100, "third", owner)
        await service.play(100, "third", other)

        snapshot = await service.snapshot(100)
        assert snapshot.current is not None and snapshot.current.requester_id == owner.user_id
        assert [track.requester_id for track in snapshot.upcoming] == [owner.user_id, other.user_id]
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_playlist_requester_limit_is_preflighted_without_partial_enqueue(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path, max_tracks_per_requester=2)
    owner = MusicActor(10, 500)
    try:
        repository.save(100, owner.user_id, "too-many", ("second", "third"))
        await service.join(100, FakeVoiceClient(), owner, voice_channel_id=500)
        await service.play(100, "first", owner)
        before = await service.snapshot(100)

        with pytest.raises(MusicSessionError, match="requester music queue limit"):
            await service.load_playlist(100, owner, "too-many")

        after = await service.snapshot(100)
        assert after.current == before.current
        assert after.upcoming == before.upcoming == ()
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_play_rechecks_policy_after_blocking_resolve_before_queue_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
    started = threading.Event()
    release = threading.Event()
    allowed = True
    assert service.library is not None
    original_resolve = service.library.resolve_track

    def blocking_resolve(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(service.library, "resolve_track", blocking_resolve)
    task = asyncio.create_task(service.play(100, "first", actor, commit_check=lambda: allowed))
    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        allowed = False
        release.set()
        with pytest.raises(MusicAuthorizationError, match="policy changed"):
            await task
        snapshot = await service.snapshot(100)
        assert snapshot.current is None
        assert snapshot.upcoming == ()
    finally:
        release.set()
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_play_rechecks_fresh_voice_after_resolve_before_queue_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
    started = threading.Event()
    release = threading.Event()
    current_voice_channel_id: int | None = 500
    assert service.library is not None
    original_resolve = service.library.resolve_track

    def blocking_resolve(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        return original_resolve(*args, **kwargs)

    async def fresh_actor() -> MusicActor:
        return MusicActor(actor.user_id, current_voice_channel_id)

    monkeypatch.setattr(service.library, "resolve_track", blocking_resolve)
    task = asyncio.create_task(service.play(100, "first", actor, commit_check=fresh_actor))
    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        current_voice_channel_id = 501
        release.set()
        with pytest.raises(MusicAuthorizationError, match="same voice channel"):
            await task
        snapshot = await service.snapshot(100)
        assert snapshot.current is None
        assert snapshot.upcoming == ()
    finally:
        release.set()
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_speech_rechecks_fresh_voice_after_source_creation_before_enqueue(tmp_path: Path) -> None:
    class BlockingSpeechFactory(FakeFactory):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def create_speech(self, wav: bytes) -> FakeSource:
            self.started.set()
            assert self.release.wait(timeout=2.0)
            return super().create_speech(wav)

    factory = BlockingSpeechFactory()
    service, _, repository = _service(tmp_path, factory=factory)
    actor = MusicActor(10, 500)
    current_voice_channel_id: int | None = 500
    await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)

    async def fresh_actor() -> MusicActor:
        return MusicActor(actor.user_id, current_voice_channel_id)

    task = asyncio.create_task(
        service.add_speech_wav(
            100,
            actor,
            b"RIFF-fresh-voice",
            commit_check=fresh_actor,
            receipt_source_channel_id=200,
        )
    )
    try:
        assert await asyncio.to_thread(factory.started.wait, 1.0)
        current_voice_channel_id = None
        factory.release.set()
        with pytest.raises(MusicAuthorizationError, match="same voice channel"):
            await task
        assert len(factory.speech_sources) == 1
        assert factory.speech_sources[0].cleaned
        assert service.speech_receipts(guild_id=100, source_channel_id=200, requester_id=10) == ()
    finally:
        factory.release.set()
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_simple_control_rechecks_commit_before_player_mutation(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
    await service.play(100, "first", actor)
    try:
        with pytest.raises(MusicAuthorizationError, match="policy changed"):
            await service.pause(100, actor, commit_check=lambda: False)
        assert not (await service.snapshot(100)).paused
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_playlist_load_rechecks_policy_after_resolve_before_first_enqueue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    repository.save(100, actor.user_id, "saved", ("first", "second"))
    await service.join(100, FakeVoiceClient(), actor, voice_channel_id=500)
    started = threading.Event()
    release = threading.Event()
    allowed = True
    assert service.library is not None
    original_resolve = service.library.resolve_track

    def blocking_resolve(*args, **kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(timeout=2.0)
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(service.library, "resolve_track", blocking_resolve)
    task = asyncio.create_task(service.load_playlist(100, actor, "saved", commit_check=lambda: allowed))
    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        allowed = False
        release.set()
        with pytest.raises(MusicAuthorizationError, match="policy changed"):
            await task
        snapshot = await service.snapshot(100)
        assert snapshot.current is None
        assert snapshot.upcoming == ()
    finally:
        release.set()
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_leave_disconnects_only_after_control_gate(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    owner = MusicActor(10, 500)
    another = MusicActor(11, 500)
    voice = FakeVoiceClient()
    try:
        await service.join(100, voice, owner, voice_channel_id=500)
        await service.play(100, "first", owner)
        assert repository.load_audio_projection(100) is not None
        with pytest.raises(MusicAuthorizationError, match="requester"):
            await service.leave(100, another)
        assert not voice.disconnected

        assert await service.leave(100, owner)
        await asyncio.sleep(0)
        assert voice.disconnected
        assert service.session_channel_id(100) is None
        assert repository.load_audio_projection(100) is None
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_close_guild_disconnects_only_the_target_session(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    first_voice = FakeVoiceClient()
    second_voice = FakeVoiceClient()
    first_actor = MusicActor(10, 500)
    second_actor = MusicActor(11, 600)
    try:
        await service.join(100, first_voice, first_actor, voice_channel_id=500)
        await service.join(200, second_voice, second_actor, voice_channel_id=600)
        first_receipt = await service.add_speech_wav(
            100,
            first_actor,
            b"RIFF-first-guild",
            receipt_source_channel_id=300,
        )
        second_receipt = await service.add_speech_wav(
            200,
            second_actor,
            b"RIFF-second-guild",
            receipt_source_channel_id=400,
        )

        assert await service.close_guild(100)
        assert first_voice.disconnected
        assert service.session_channel_id(100) is None
        assert service.speech_receipts(guild_id=100, source_channel_id=300, requester_id=10) == ()
        assert not second_voice.disconnected
        assert service.session_channel_id(200) == 600
        assert service.speech_receipts(guild_id=200, source_channel_id=400, requester_id=11) == (second_receipt,)
        assert first_receipt != second_receipt
        assert not await service.close_guild(999)
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_old_service_reference_rejects_new_work(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    voice = FakeVoiceClient()
    await service.join(100, voice, actor, voice_channel_id=500)

    await service.close()
    await service.close()

    assert voice.disconnected
    assert not service.available
    assert service.reason == "closed"
    assert service.session_channel_id(100) is None
    operations = (
        lambda: service.search("first", actor),
        lambda: service.join(100, FakeVoiceClient(), actor, voice_channel_id=500),
        lambda: service.play(100, "first", actor),
        lambda: service.snapshot(100),
        lambda: service.pause(100, actor),
        lambda: service.add_speech_wav(100, actor, b"RIFF-closed"),
        lambda: service.list_playlists(100, actor),
        lambda: service.delete_playlist(100, actor, "saved"),
    )
    try:
        for operation in operations:
            with pytest.raises(MusicUnavailableError, match="closed"):
                await operation()
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_begin_close_rejects_new_work_before_stop_disconnects_sessions(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    voice = FakeVoiceClient()
    await service.join(100, voice, actor, voice_channel_id=500)

    await service.begin_close()

    assert not service.available
    assert service.reason == "closing"
    assert not voice.disconnected
    with pytest.raises(MusicUnavailableError, match="closing"):
        await service.play(100, "first", actor)

    await service.close()
    assert voice.disconnected
    repository.close()


@pytest.mark.asyncio
async def test_close_during_blocking_search_rejects_stale_result(tmp_path: Path) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    started = threading.Event()
    release = threading.Event()
    assert service.library is not None
    original_search = service.library.search

    def blocking_search(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        return original_search(*args, **kwargs)

    service.library.search = blocking_search  # type: ignore[method-assign]
    task = asyncio.create_task(service.search("first", actor))
    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        await service.close()
        release.set()
        with pytest.raises(MusicUnavailableError, match="closed"):
            await task
    finally:
        release.set()
        repository.close()


@pytest.mark.asyncio
async def test_playlist_save_commit_and_close_have_one_ordered_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, repository = _service(tmp_path)
    actor = MusicActor(10, 500)
    voice = FakeVoiceClient()
    await service.join(100, voice, actor, voice_channel_id=500)
    await service.play(100, "first", actor)
    started = threading.Event()
    release = threading.Event()
    original_save = repository.save

    def blocking_save(*args, **kwargs):
        started.set()
        assert release.wait(timeout=2.0)
        return original_save(*args, **kwargs)

    monkeypatch.setattr(repository, "save", blocking_save)
    save_task = asyncio.create_task(service.save_playlist(100, actor, "ordered"))
    close_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        close_task = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert not close_task.done()

        release.set()
        record = await save_task
        await close_task

        assert record.name == "ordered"
        assert repository.load(100, actor.user_id, "ordered") is not None
        with pytest.raises(MusicUnavailableError, match="closed"):
            await service.save_playlist(100, actor, "after-close")
        assert repository.load(100, actor.user_id, "after-close") is None
    finally:
        release.set()
        if close_task is not None:
            await close_task
        else:
            await service.close()
        repository.close()


async def _finish_voice_track(voice: FakeVoiceClient) -> None:
    callback = voice.after
    assert callback is not None
    voice.playing = False
    voice.source = None
    voice.after = None
    callback(None)
    await asyncio.sleep(0)


async def _wait_for_play_calls(voice: FakeVoiceClient, count: int) -> None:
    for _ in range(100):
        if voice.play_calls >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} play calls, got {voice.play_calls}")


@pytest.mark.asyncio
async def test_local_radio_adds_exactly_one_authorized_track_after_queue_becomes_empty(
    tmp_path: Path,
) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    try:
        await service.join(100, voice, actor, voice_channel_id=500, commit_check=lambda: actor)
        await service.play(100, "first", actor, commit_check=lambda: actor)

        assert await service.set_local_radio(100, actor, True, commit_check=lambda: actor)
        assert service.local_radio_enabled(100)
        assert len(factory.music_sources) == 1

        await _finish_voice_track(voice)
        await _wait_for_play_calls(voice, 2)

        snapshot = await service.snapshot(100)
        assert snapshot.current is not None
        assert snapshot.current.title == "second"
        assert snapshot.upcoming == ()
        assert len(factory.music_sources) == 2
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_local_radio_never_overtakes_a_manual_queue_request(tmp_path: Path) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    try:
        await service.join(100, voice, actor, voice_channel_id=500, commit_check=lambda: actor)
        await service.play(100, "first", actor, commit_check=lambda: actor)
        await service.set_local_radio(100, actor, True, commit_check=lambda: actor)
        await service.play(100, "third", actor, commit_check=lambda: actor)

        await _finish_voice_track(voice)
        await _wait_for_play_calls(voice, 2)

        snapshot = await service.snapshot(100)
        assert snapshot.current is not None and snapshot.current.title == "third"
        assert snapshot.upcoming == ()
        assert len(factory.music_sources) == 2
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_local_radio_resumes_when_a_waiting_manual_request_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    manual_started = asyncio.Event()
    release_manual = asyncio.Event()

    async def fail_manual_request(
        _guild_id: int,
        _query: str,
        _actor: MusicActor,
        *,
        commit_check: object,
    ) -> tuple[Track, int]:
        del commit_check
        manual_started.set()
        await release_manual.wait()
        raise MusicSessionError("manual request failed")

    try:
        await service.join(100, voice, actor, voice_channel_id=500, commit_check=lambda: actor)
        await service.play(100, "first", actor, commit_check=lambda: actor)
        await service.set_local_radio(100, actor, True, commit_check=lambda: actor)
        monkeypatch.setattr(service, "_play_current", fail_manual_request)

        manual_task = asyncio.create_task(service.play(100, "missing", actor, commit_check=lambda: actor))
        await manual_started.wait()
        await _finish_voice_track(voice)
        await asyncio.sleep(0.05)
        assert len(factory.music_sources) == 1

        release_manual.set()
        with pytest.raises(MusicSessionError, match="manual request failed"):
            await manual_task
        await _wait_for_play_calls(voice, 2)

        snapshot = await service.snapshot(100)
        assert snapshot.current is not None and snapshot.current.title == "second"
        assert snapshot.upcoming == ()
    finally:
        release_manual.set()
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_local_radio_off_and_music_stop_prevent_future_refill(tmp_path: Path) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    try:
        await service.join(100, voice, actor, voice_channel_id=500, commit_check=lambda: actor)
        await service.play(100, "first", actor, commit_check=lambda: actor)
        await service.set_local_radio(100, actor, True, commit_check=lambda: actor)
        assert not await service.set_local_radio(100, actor, False, commit_check=lambda: actor)
        await _finish_voice_track(voice)
        await asyncio.sleep(0.05)
        assert len(factory.music_sources) == 1

        await service.play(100, "second", actor, commit_check=lambda: actor)
        await service.set_local_radio(100, actor, True, commit_check=lambda: actor)
        await service.stop_music(100, actor, commit_check=lambda: actor)
        assert not service.local_radio_enabled(100)
        await asyncio.sleep(0.05)
        assert len(factory.music_sources) == 2
    finally:
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_local_radio_rechecks_fresh_policy_after_candidate_selection(tmp_path: Path) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    started = asyncio.Event()
    release = asyncio.Event()
    allowed = True
    assert service.library is not None
    candidate = service.library.seal_track(service.library.resolve_track("second", requester_id=actor.user_id))

    class BlockingPort:
        async def select_related(self, _request: object) -> RelatedTrackSelection:
            started.set()
            await release.wait()
            return RelatedTrackSelection(candidate, 1, 1)

    async def current_actor() -> MusicActor | None:
        return actor if allowed else None

    service.related_track_port = BlockingPort()
    try:
        await service.join(100, voice, actor, voice_channel_id=500, commit_check=current_actor)
        await service.play(100, "first", actor, commit_check=current_actor)
        await service.set_local_radio(100, actor, True, commit_check=current_actor)
        await _finish_voice_track(voice)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        allowed = False
        release.set()
        await asyncio.sleep(0.05)

        assert len(factory.music_sources) == 1
        assert not service.local_radio_enabled(100)
        snapshot = await service.snapshot(100)
        assert snapshot.current is None and snapshot.upcoming == ()
    finally:
        release.set()
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_local_radio_rechecks_session_control_after_manage_guild_is_revoked(
    tmp_path: Path,
) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    owner = MusicActor(10, 500)
    manager = MusicActor(20, 500, manage_guild=True)
    downgraded = MusicActor(20, 500)
    started = asyncio.Event()
    release = asyncio.Event()
    current_actor = manager
    assert service.library is not None
    candidate = service.library.seal_track(service.library.resolve_track("second", requester_id=manager.user_id))

    class BlockingPort:
        async def select_related(self, _request: object) -> RelatedTrackSelection:
            started.set()
            await release.wait()
            return RelatedTrackSelection(candidate, 1, 1)

    async def fresh_manager() -> MusicActor:
        return current_actor

    service.related_track_port = BlockingPort()
    try:
        await service.join(100, voice, owner, voice_channel_id=500, commit_check=lambda: owner)
        await service.play(100, "first", owner, commit_check=lambda: owner)
        await service.set_local_radio(100, manager, True, commit_check=fresh_manager)
        await _finish_voice_track(voice)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        current_actor = downgraded
        release.set()
        await asyncio.sleep(0.05)

        assert len(factory.music_sources) == 1
        assert not service.local_radio_enabled(100)
        snapshot = await service.snapshot(100)
        assert snapshot.current is None and snapshot.upcoming == ()
    finally:
        release.set()
        await service.close()
        repository.close()


@pytest.mark.asyncio
async def test_local_radio_close_cancels_inflight_selection_without_zombie_enqueue(
    tmp_path: Path,
) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    started = asyncio.Event()
    never = asyncio.Event()

    class BlockingPort:
        async def select_related(self, _request: object) -> RelatedTrackSelection:
            started.set()
            await never.wait()
            raise AssertionError("cancelled radio selection must not return")

    service.related_track_port = BlockingPort()
    await service.join(100, voice, actor, voice_channel_id=500, commit_check=lambda: actor)
    await service.play(100, "first", actor, commit_check=lambda: actor)
    await service.set_local_radio(100, actor, True, commit_check=lambda: actor)
    await _finish_voice_track(voice)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await service.close()

    assert len(factory.music_sources) == 1
    assert not service.local_radio_enabled(100)
    repository.close()


@pytest.mark.asyncio
async def test_local_radio_close_discards_candidate_cancelled_during_source_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory, repository = _service(tmp_path)
    voice = FakeVoiceClient()
    actor = MusicActor(10, 500)
    source_started = threading.Event()
    release_source = threading.Event()
    original_create = factory.create

    def blocking_create(track: Track) -> FakeSource:
        if track.title == "second":
            source_started.set()
            release_source.wait(timeout=2.0)
        return original_create(track)

    monkeypatch.setattr(factory, "create", blocking_create)
    try:
        await service.join(100, voice, actor, voice_channel_id=500, commit_check=lambda: actor)
        await service.play(100, "first", actor, commit_check=lambda: actor)
        await service.set_local_radio(100, actor, True, commit_check=lambda: actor)
        await _finish_voice_track(voice)
        assert await asyncio.to_thread(source_started.wait, 1.0)

        await service.begin_close()

        projection = repository.load_audio_projection(100)
        assert projection is not None
        assert projection.tracks == ()
        assert not service.local_radio_enabled(100)
    finally:
        release_source.set()
        for _ in range(100):
            if len(factory.music_sources) >= 2 and factory.music_sources[-1].cleaned:
                break
            await asyncio.sleep(0.01)
        await service.close()
        repository.close()
