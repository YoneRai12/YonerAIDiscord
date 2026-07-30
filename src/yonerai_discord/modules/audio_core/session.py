from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
from collections import deque
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol

from .mixer import DuckingMixer, PCMSource
from .models import LoopMode, QueueSnapshot, RecentTrack, RecentTrackState, Track
from .source import SeekableTrackSourceFactory, TrackSourceFactory


logger = logging.getLogger(__name__)


StateChangedCallback = Callable[[int, QueueSnapshot], Awaitable[None] | None]


class VoiceClientLike(Protocol):
    def play(self, source: PCMSource, *, after: Any | None = None) -> None: ...

    def stop(self) -> None: ...

    def is_playing(self) -> bool: ...

    async def disconnect(self, *, force: bool = False) -> None: ...


class QueueFullError(RuntimeError):
    pass


class RequesterQueueLimitError(QueueFullError):
    pass


class PlayerStateError(RuntimeError):
    pass


class TrackSourceAuthorizationError(PlayerStateError):
    pass


class SeekUnsupportedError(PlayerStateError):
    pass


class TrackSeekError(PlayerStateError):
    pass


class _EndAction(StrEnum):
    NATURAL = "natural"
    SKIP = "skip"
    STOP = "stop"


class GuildAudioSession:
    def __init__(
        self,
        guild_id: int,
        voice_client: VoiceClientLike,
        source_factory: TrackSourceFactory,
        *,
        track_allowed: Callable[[Track], bool] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        max_queue: int = 100,
        max_tracks_per_requester: int | None = None,
        max_speech_queue: int = 20,
        volume: float = 0.75,
        speech_volume: float = 1.0,
        state_changed: StateChangedCallback | None = None,
    ) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        requester_limit = min(10, max_queue) if max_tracks_per_requester is None else max_tracks_per_requester
        if (
            not 1 <= max_queue <= 1_000
            or type(requester_limit) is not int
            or not 1 <= requester_limit <= min(10, max_queue)
            or not 1 <= max_speech_queue <= 100
        ):
            raise ValueError("queue limits are invalid")
        if not _volume_is_valid(volume) or not _volume_is_valid(speech_volume):
            raise ValueError("volume is invalid")
        self.guild_id = guild_id
        self.voice_client = voice_client
        self.source_factory = source_factory
        self._track_allowed = track_allowed
        self.loop = loop or asyncio.get_running_loop()
        self.max_queue = max_queue
        self.max_tracks_per_requester = requester_limit
        self.max_speech_queue = max_speech_queue
        self._queue: deque[Track] = deque()
        self._recent: deque[RecentTrack] = deque(maxlen=20)
        self._current: Track | None = None
        self._mixer: DuckingMixer | None = None
        self._loop_mode = LoopMode.OFF
        self._volume = volume
        self._speech_volume = speech_volume
        self._state_changed = state_changed
        self._revision = 0
        self._projection_restored = False
        self._restored_paused = False
        self._end_action = _EndAction.NATURAL
        self._generation = 0
        self._projection_frozen = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._finish_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    async def enqueue(self, track: Track) -> int:
        if not isinstance(track, Track):
            raise TypeError("track must be a Track")
        start_error: BaseException | None = None
        change: tuple[int, QueueSnapshot] | None = None
        async with self._lock:
            self._require_open()
            previous_current = self._current
            previous_queue = tuple(self._queue)
            self._require_enqueue_capacity_locked((track,))
            self._queue.append(track)
            position = len(self._queue) + int(self._current is not None)
            try:
                await self._start_next_locked()
            except (TrackSourceAuthorizationError, asyncio.CancelledError) as exc:
                start_error = exc
            except Exception as exc:
                start_error = exc
            if previous_current is not self._current or previous_queue != tuple(self._queue):
                change = self._record_state_change_locked()
        if change is not None:
            await self._notify_state_changed(change)
        if start_error is not None:
            raise start_error
        return position

    async def enqueue_many(self, tracks: tuple[Track, ...]) -> tuple[int, ...]:
        if not isinstance(tracks, tuple) or not tracks or any(not isinstance(track, Track) for track in tracks):
            raise TypeError("tracks must be a non-empty tuple of Track values")
        start_error: BaseException | None = None
        change: tuple[int, QueueSnapshot] | None = None
        async with self._lock:
            self._require_open()
            previous_current = self._current
            previous_queue = tuple(self._queue)
            occupied = len(self._queue) + int(self._current is not None)
            self._require_enqueue_capacity_locked(tracks)
            positions = tuple(range(occupied + 1, occupied + len(tracks) + 1))
            self._queue.extend(tracks)
            try:
                await self._start_next_locked()
            except (TrackSourceAuthorizationError, asyncio.CancelledError) as exc:
                self._queue = deque(previous_queue)
                start_error = exc
            except Exception as exc:
                self._queue = deque(previous_queue)
                start_error = exc
            if previous_current is not self._current or previous_queue != tuple(self._queue):
                change = self._record_state_change_locked()
        if change is not None:
            await self._notify_state_changed(change)
        if start_error is not None:
            raise start_error
        return positions

    async def add_speech(self, source: PCMSource) -> int:
        operation_error: BaseException | None = None
        speech_position: int | None = None
        change: tuple[int, QueueSnapshot] | None = None
        async with self._lock:
            self._require_open()
            previous_current = self._current
            previous_queue = tuple(self._queue)
            try:
                if self._mixer is None:
                    if not self._restored_paused:
                        await self._start_next_locked()
                if self._mixer is None:
                    self._generation += 1
                    generation = self._generation
                    self._mixer = DuckingMixer(
                        music_volume=self._volume,
                        speech_volume=self._speech_volume,
                        on_retire=self._schedule_source_cleanup,
                    )
                    self._play_locked(self._mixer, generation)
                snapshot = self._mixer.snapshot()
                speech_count = snapshot.queued_speech + int(snapshot.speech_active)
                if speech_count >= self.max_speech_queue:
                    await asyncio.to_thread(source.cleanup)
                    raise QueueFullError("speech queue is full")
                self._mixer.add_speech(source)
                speech_position = speech_count + 1
            except BaseException as exc:
                operation_error = exc
            if previous_current is not self._current or previous_queue != tuple(self._queue):
                change = self._record_state_change_locked()
        if change is not None:
            await self._notify_state_changed(change)
        if operation_error is not None:
            raise operation_error
        if speech_position is None:
            raise AssertionError("speech position was not assigned")
        return speech_position

    async def pause(self, *, expected_revision: int | None = None) -> None:
        async with self._lock:
            self._require_open()
            self._require_revision_locked(expected_revision)
            if self._current is None or self._mixer is None:
                raise PlayerStateError("music is not playing")
            self._mixer.pause_music()
            change = self._record_state_change_locked()
        await self._notify_state_changed(change)

    async def resume(self, *, expected_revision: int | None = None) -> None:
        operation_error: BaseException | None = None
        change: tuple[int, QueueSnapshot] | None = None
        async with self._lock:
            self._require_open()
            self._require_revision_locked(expected_revision)
            was_restored_paused = self._restored_paused
            previous_current = self._current
            previous_queue = tuple(self._queue)
            if was_restored_paused:
                self._restored_paused = False
                try:
                    while self._queue and self._mixer is None and self._current is None:
                        try:
                            await self._start_next_locked()
                        except TrackSourceAuthorizationError:
                            logger.warning("audio_track_source_authorization_denied")
                            continue
                        break
                except BaseException as exc:
                    self._restored_paused = True
                    operation_error = exc
            else:
                try:
                    if self._current is None or self._mixer is None:
                        raise PlayerStateError("music is not playing")
                    self._mixer.resume_music()
                except BaseException as exc:
                    operation_error = exc
            if operation_error is None or (
                previous_current is not self._current or previous_queue != tuple(self._queue)
            ):
                change = self._record_state_change_locked()
        if change is not None:
            await self._notify_state_changed(change)
        if operation_error is not None:
            raise operation_error

    async def skip(self, *, expected_revision: int | None = None) -> Track:
        async with self._lock:
            self._require_open()
            self._require_revision_locked(expected_revision)
            if self._current is None or self._mixer is None:
                raise PlayerStateError("music is not playing")
            skipped = self._current
            self._end_action = _EndAction.SKIP
            self._mixer.clear_music()
            change = self._record_state_change_locked()
        await self._notify_state_changed(change)
        return skipped

    async def stop_music(self, *, expected_revision: int | None = None) -> int:
        async with self._lock:
            self._require_open()
            self._require_revision_locked(expected_revision)
            removed = len(self._queue) + int(self._current is not None)
            self._queue.clear()
            self._restored_paused = False
            self._end_action = _EndAction.STOP
            if self._mixer is not None:
                self._mixer.clear_music()
            change = self._record_state_change_locked()
        await self._notify_state_changed(change)
        return removed

    async def remove(self, position: int, *, expected_track_id: str | None = None) -> Track:
        async with self._lock:
            self._require_open()
            if not 1 <= position <= len(self._queue):
                raise IndexError("queue position is out of range")
            items = list(self._queue)
            if expected_track_id is not None and items[position - 1].track_id != expected_track_id:
                raise PlayerStateError("music queue changed")
            removed = items.pop(position - 1)
            self._queue = deque(items)
            change = self._record_state_change_locked()
        await self._notify_state_changed(change)
        return removed

    async def move(self, source_position: int, target_position: int, *, expected_track_id: str | None = None) -> Track:
        change: tuple[int, QueueSnapshot] | None = None
        async with self._lock:
            self._require_open()
            if not 1 <= source_position <= len(self._queue) or not 1 <= target_position <= len(self._queue):
                raise IndexError("queue position is out of range")
            items = list(self._queue)
            if expected_track_id is not None and items[source_position - 1].track_id != expected_track_id:
                raise PlayerStateError("music queue changed")
            moved = items.pop(source_position - 1)
            items.insert(target_position - 1, moved)
            if source_position != target_position:
                self._queue = deque(items)
                change = self._record_state_change_locked()
        if change is not None:
            await self._notify_state_changed(change)
        return moved

    async def clear_requester(self, requester_id: int) -> int:
        if type(requester_id) is not int or requester_id <= 0:
            raise ValueError("requester_id must be positive")
        change: tuple[int, QueueSnapshot] | None = None
        async with self._lock:
            self._require_open()
            before = len(self._queue)
            self._queue = deque(track for track in self._queue if track.requester_id != requester_id)
            removed = before - len(self._queue)
            if removed:
                change = self._record_state_change_locked()
        if change is not None:
            await self._notify_state_changed(change)
        return removed

    async def shuffle(self, *, expected_revision: int | None = None) -> int:
        async with self._lock:
            self._require_open()
            self._require_revision_locked(expected_revision)
            items = list(self._queue)
            random.SystemRandom().shuffle(items)
            self._queue = deque(items)
            change = self._record_state_change_locked()
        await self._notify_state_changed(change)
        return len(items)

    async def set_loop_mode(self, mode: LoopMode, *, expected_revision: int | None = None) -> None:
        if not isinstance(mode, LoopMode):
            raise TypeError("mode must be a LoopMode")
        async with self._lock:
            self._require_open()
            self._require_revision_locked(expected_revision)
            self._loop_mode = mode
            change = self._record_state_change_locked()
        await self._notify_state_changed(change)

    async def set_volume(self, volume: float, *, expected_revision: int | None = None) -> None:
        if not _volume_is_valid(volume):
            raise ValueError("volume must be between 0 and 2")
        async with self._lock:
            self._require_open()
            self._require_revision_locked(expected_revision)
            self._volume = volume
            if self._mixer is not None:
                self._mixer.set_music_volume(volume)
            change = self._record_state_change_locked()
        await self._notify_state_changed(change)

    async def set_speech_volume(self, volume: float, *, expected_revision: int | None = None) -> None:
        if not _volume_is_valid(volume):
            raise ValueError("volume must be between 0 and 2")
        async with self._lock:
            self._require_open()
            self._require_revision_locked(expected_revision)
            self._speech_volume = volume
            if self._mixer is not None:
                self._mixer.set_speech_volume(volume)
            change = self._record_state_change_locked()
        await self._notify_state_changed(change)

    async def seek(
        self,
        seconds: float,
        *,
        expected_revision: int | None = None,
        expected_track_id: str | None = None,
        authorization_current: Callable[[], Awaitable[bool]] | None = None,
    ) -> Track:
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0.0 <= seconds <= 86_400.0:
            raise ValueError("seek position must be between 0 and 86400 seconds")
        if expected_track_id is not None and (
            not isinstance(expected_track_id, str) or not expected_track_id or len(expected_track_id) > 64
        ):
            raise TypeError("expected_track_id is invalid")
        factory = self.source_factory
        if not isinstance(factory, SeekableTrackSourceFactory):
            raise SeekUnsupportedError("track source does not support seeking")

        async with self._lock:
            self._require_open()
            self._require_revision_locked(expected_revision)
            track = self._current
            mixer = self._mixer
            generation = self._generation
            if (
                track is None
                or mixer is None
                or not mixer.snapshot().music_active
                or not self.voice_client.is_playing()
            ):
                raise PlayerStateError("music is not playing")
            if expected_track_id is not None and track.track_id != expected_track_id:
                raise PlayerStateError("music session state changed")
            self._require_track_allowed(track)

        source_task = asyncio.create_task(asyncio.to_thread(factory.create_at, track, float(seconds)))
        try:
            source = await asyncio.shield(source_task)
        except asyncio.CancelledError:
            source_task.add_done_callback(self._cleanup_abandoned_source_task)
            raise
        except Exception as exc:
            raise TrackSeekError("seek source could not be prepared") from exc

        previous_source: PCMSource | None = None
        try:
            async with self._lock:
                self._require_open()
                self._require_revision_locked(expected_revision)
                if (
                    self.source_factory is not factory
                    or self._generation != generation
                    or self._current is not track
                    or self._mixer is not mixer
                    or (expected_track_id is not None and track.track_id != expected_track_id)
                    or not mixer.snapshot().music_active
                    or not self.voice_client.is_playing()
                ):
                    raise PlayerStateError("music session state changed")
                self._require_track_allowed(track)
                if authorization_current is not None:
                    try:
                        authorized = await authorization_current()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise TrackSourceAuthorizationError("track seek is no longer authorized") from exc
                    if authorized is not True:
                        raise TrackSourceAuthorizationError("track seek is no longer authorized")
                    if (
                        self.source_factory is not factory
                        or self._generation != generation
                        or self._current is not track
                        or self._mixer is not mixer
                        or (expected_track_id is not None and track.track_id != expected_track_id)
                        or not mixer.snapshot().music_active
                        or not self.voice_client.is_playing()
                    ):
                        raise PlayerStateError("music session state changed")
                    self._require_track_allowed(track)
                try:
                    previous_source = mixer.replace_music(source)
                except ValueError as exc:
                    raise TrackSeekError("seek source is not valid PCM") from exc
                except RuntimeError as exc:
                    raise PlayerStateError("music session state changed") from exc
                except Exception as exc:
                    raise TrackSeekError("seek source is not valid PCM") from exc
                # seek offset is intentionally live-only and is not part of the durable
                # projection.  Increment the revision synchronously so the source swap is
                # the commit point and no cancellable await can turn it into a false failure.
                self._record_state_change_locked()
        except BaseException:
            self._schedule_source_cleanup(source)
            raise

        if previous_source is None:
            self._schedule_source_cleanup(source)
            raise AssertionError("seek source replacement did not complete")
        self._schedule_source_cleanup(previous_source)
        return track

    async def restore_projection(
        self,
        tracks: tuple[Track, ...],
        *,
        loop_mode: LoopMode,
        paused: bool,
        music_volume: float,
        speech_volume: float,
    ) -> QueueSnapshot:
        if not isinstance(tracks, tuple) or any(not isinstance(track, Track) for track in tracks):
            raise TypeError("tracks must be a tuple of Track values")
        if len(tracks) > self.max_queue:
            raise QueueFullError("music queue is full")
        if not isinstance(loop_mode, LoopMode):
            raise TypeError("loop_mode must be a LoopMode")
        if not isinstance(paused, bool):
            raise TypeError("paused must be a bool")
        if not _volume_is_valid(music_volume) or not _volume_is_valid(speech_volume):
            raise ValueError("volume must be between 0 and 2")
        async with self._lock:
            self._require_open()
            if (
                self._projection_restored
                or self._revision != 0
                or self._current is not None
                or self._mixer is not None
                or self._queue
            ):
                raise PlayerStateError("audio projection was already restored")
            previous_loop_mode = self._loop_mode
            previous_music_volume = self._volume
            previous_speech_volume = self._speech_volume
            self._queue = deque(tracks)
            self._loop_mode = loop_mode
            self._volume = music_volume
            self._speech_volume = speech_volume
            self._restored_paused = paused and bool(tracks)
            try:
                while self._queue and not self._restored_paused:
                    try:
                        await self._start_next_locked()
                    except TrackSourceAuthorizationError:
                        logger.warning("audio_track_source_authorization_denied")
                        continue
                    break
            except BaseException:
                self._queue.clear()
                self._loop_mode = previous_loop_mode
                self._volume = previous_music_volume
                self._speech_volume = previous_speech_volume
                self._restored_paused = False
                raise
            self._projection_restored = True
            change = self._record_state_change_locked()
        await self._notify_state_changed(change)
        return change[1]

    async def snapshot(self) -> QueueSnapshot:
        async with self._lock:
            return self._snapshot_locked()

    async def freeze_projection(self) -> QueueSnapshot:
        """Freeze queue mutation and return the exact shutdown projection."""

        async with self._lock:
            if self._closed:
                raise PlayerStateError("audio session is closed")
            if not self._projection_frozen:
                self._projection_frozen = True
                self._generation += 1
            return self._snapshot_locked()

    async def close(self, *, disconnect: bool = True) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            mixer, self._mixer = self._mixer, None
            self._current = None
            self._queue.clear()
            self._recent.clear()
            self._restored_paused = False
        if mixer is not None:
            mixer.cleanup()
        try:
            self.voice_client.stop()
        except Exception as exc:
            logger.warning("audio_voice_stop_failed", extra={"error_type": type(exc).__name__})
        if disconnect:
            try:
                await self.voice_client.disconnect(force=True)
            except Exception as exc:
                logger.warning("audio_voice_disconnect_failed", extra={"error_type": type(exc).__name__})
        tasks = tuple(self._finish_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        cleanup_tasks = tuple(self._cleanup_tasks)
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    async def _start_next_locked(self) -> None:
        if self._restored_paused or self._mixer is not None or self._current is not None or not self._queue:
            return
        track = self._queue.popleft()
        try:
            source_task = asyncio.create_task(asyncio.to_thread(self._create_track_source, track))
            try:
                source = await asyncio.shield(source_task)
            except asyncio.CancelledError:
                self._queue.appendleft(track)
                source_task.add_done_callback(self._cleanup_abandoned_source_task)
                raise
            mixer = DuckingMixer(
                source,
                music_volume=self._volume,
                speech_volume=self._speech_volume,
                on_retire=self._schedule_source_cleanup,
            )
        except TrackSourceAuthorizationError:
            raise
        except Exception:
            self._queue.appendleft(track)
            raise
        self._generation += 1
        generation = self._generation
        self._current = track
        self._mixer = mixer
        self._restored_paused = False
        self._end_action = _EndAction.NATURAL
        try:
            self._play_locked(mixer, generation)
        except Exception:
            self._current = None
            self._mixer = None
            mixer.cleanup()
            self._queue.appendleft(track)
            raise

    def _create_track_source(self, track: Track) -> PCMSource:
        self._require_track_allowed(track)
        return self.source_factory.create(track)

    def _require_track_allowed(self, track: Track) -> None:
        if self._track_allowed is not None and self._track_allowed(track) is not True:
            raise TrackSourceAuthorizationError("track source is no longer authorized")

    def _play_locked(self, mixer: DuckingMixer, generation: int) -> None:
        def after(error: BaseException | None) -> None:
            self.loop.call_soon_threadsafe(self._spawn_finish, generation, error)

        self.voice_client.play(mixer, after=after)

    def _spawn_finish(self, generation: int, error: BaseException | None) -> None:
        task = asyncio.create_task(self._finish(generation, error))
        self._finish_tasks.add(task)
        task.add_done_callback(self._finish_tasks.discard)

    def _schedule_source_cleanup(self, source: PCMSource) -> None:
        if self.loop.is_closed():
            source.cleanup()
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self.loop:
            self._spawn_source_cleanup(source)
        else:
            try:
                self.loop.call_soon_threadsafe(self._spawn_source_cleanup, source)
            except RuntimeError:
                source.cleanup()

    def _spawn_source_cleanup(self, source: PCMSource) -> None:
        task = asyncio.create_task(asyncio.to_thread(source.cleanup))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def _cleanup_abandoned_source_task(self, task: asyncio.Task[PCMSource]) -> None:
        try:
            source = task.result()
        except BaseException:
            return
        self._schedule_source_cleanup(source)

    async def _finish(self, generation: int, error: BaseException | None) -> None:
        async with self._lock:
            if generation != self._generation or self._closed:
                return
            mixer, self._mixer = self._mixer, None
            track, self._current = self._current, None
            action, self._end_action = self._end_action, _EndAction.NATURAL
            if mixer is not None:
                mixer.cleanup()
            if track is not None:
                state = (
                    RecentTrackState.FAILED
                    if error is not None
                    else {
                        _EndAction.NATURAL: RecentTrackState.COMPLETED,
                        _EndAction.SKIP: RecentTrackState.SKIPPED,
                        _EndAction.STOP: RecentTrackState.STOPPED,
                    }[action]
                )
                self._recent.append(RecentTrack(track.title, track.requester_id, state))
            if error is not None:
                logger.warning("audio_player_failed", extra={"error_type": type(error).__name__})
            elif track is not None and action is _EndAction.NATURAL:
                if self._loop_mode is LoopMode.TRACK:
                    self._queue.appendleft(track)
                elif self._loop_mode is LoopMode.QUEUE:
                    self._queue.append(track)
            while self._queue and self._mixer is None and self._current is None and not self._restored_paused:
                try:
                    await self._start_next_locked()
                except TrackSourceAuthorizationError:
                    logger.warning("audio_track_source_authorization_denied")
                    continue
                except Exception as exc:
                    logger.warning(
                        "audio_track_source_start_failed",
                        extra={"error_type": type(exc).__name__},
                    )
                break
            pending_cleanup = tuple(self._cleanup_tasks)
            if pending_cleanup:
                await asyncio.gather(*pending_cleanup, return_exceptions=True)
            change = self._record_state_change_locked()
        await self._notify_state_changed(change)

    def _snapshot_locked(self) -> QueueSnapshot:
        paused = self._restored_paused or bool(self._mixer is not None and self._mixer.snapshot().music_paused)
        current = self._current if self._end_action is _EndAction.NATURAL else None
        return QueueSnapshot(
            current,
            tuple(self._queue),
            self._loop_mode,
            paused,
            self._volume,
            speech_volume=self._speech_volume,
            revision=self._revision,
            recent=tuple(reversed(self._recent)),
        )

    def _require_enqueue_capacity_locked(self, tracks: tuple[Track, ...]) -> None:
        occupied = len(self._queue) + int(self._current is not None)
        if occupied + len(tracks) > self.max_queue:
            raise QueueFullError("music queue is full")
        counts: dict[int, int] = {}
        if self._current is not None:
            counts[self._current.requester_id] = 1
        for queued in self._queue:
            counts[queued.requester_id] = counts.get(queued.requester_id, 0) + 1
        for track in tracks:
            requester_count = counts.get(track.requester_id, 0) + 1
            if requester_count > self.max_tracks_per_requester:
                raise RequesterQueueLimitError("requester music queue limit reached")
            counts[track.requester_id] = requester_count

    def _record_state_change_locked(self) -> tuple[int, QueueSnapshot]:
        self._revision += 1
        return self._revision, self._snapshot_locked()

    async def _notify_state_changed(self, change: tuple[int, QueueSnapshot]) -> None:
        callback = self._state_changed
        if callback is None:
            return
        revision, snapshot = change
        try:
            result = callback(revision, snapshot)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            logger.warning(
                "audio_session_state_callback_failed",
                extra={"error_type": "CancelledError"},
            )
        except Exception as exc:
            logger.warning(
                "audio_session_state_callback_failed",
                extra={"error_type": type(exc).__name__},
            )

    def _require_open(self) -> None:
        if self._closed:
            raise PlayerStateError("audio session is closed")
        if self._projection_frozen:
            raise PlayerStateError("audio session is closing")

    def _require_revision_locked(self, expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        if type(expected_revision) is not int or expected_revision < 0:
            raise TypeError("expected_revision is invalid")
        if self._revision != expected_revision:
            raise PlayerStateError("audio session state changed")


def _volume_is_valid(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0.0 <= value <= 2.0


class AudioCoordinator:
    def __init__(self, source_factory: TrackSourceFactory) -> None:
        self.source_factory = source_factory
        self._sessions: dict[int, GuildAudioSession] = {}
        self._lock = asyncio.Lock()

    async def attach(self, guild_id: int, voice_client: VoiceClientLike) -> GuildAudioSession:
        async with self._lock:
            previous = self._sessions.pop(guild_id, None)
            if previous is not None:
                await previous.close()
            session = GuildAudioSession(guild_id, voice_client, self.source_factory)
            self._sessions[guild_id] = session
            return session

    def get(self, guild_id: int) -> GuildAudioSession | None:
        return self._sessions.get(guild_id)

    async def detach(self, guild_id: int) -> bool:
        async with self._lock:
            session = self._sessions.pop(guild_id, None)
        if session is None:
            return False
        await session.close()
        return True

    async def close(self) -> None:
        async with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
