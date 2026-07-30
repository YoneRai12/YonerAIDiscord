from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, TypeVar

from yonerai_discord.modules.audio_core import (
    GuildAudioSession,
    LocalMediaLibrary,
    LoopMode,
    MediaLibraryError,
    PlayerStateError,
    QueueFullError,
    QueueSnapshot,
    RequesterQueueLimitError,
    SeekUnsupportedError,
    SeekableTrackSourceFactory,
    Track,
    TrackSourceAuthorizationError,
    TrackSourceFactory,
)

from .models import (
    GuildAudioProjection,
    ImportedMusicAsset,
    MusicActor,
    MusicAuthorizationError,
    MusicError,
    MusicRuntimeStatus,
    MusicSeekUnsupportedError,
    MusicSessionError,
    MusicUnavailableError,
    PersistedMusicTrackRef,
    PlaylistError,
    PlaylistRecord,
    SessionBinding,
    control_track,
    normalize_imported_music_title,
)
from .imports import MusicImportStore, MusicImportStoreError
from .repository import MusicPlaylistRepository
from .radio import (
    AuthorizedLocalRelatedTrackAdapter,
    RelatedTrackPort,
    RelatedTrackRequest,
)


MusicCommitResult = bool | MusicActor | None
MusicCommitCheck = Callable[[], MusicCommitResult | Awaitable[MusicCommitResult]]
MusicRuntimeCurrent = Callable[[], bool]
MusicStateObserver = Callable[[int], None]
_ThreadResult = TypeVar("_ThreadResult")
_MAX_DURABLE_TRACKS = 100
_MAX_SEEK_SECONDS = 86_400
_DEFAULT_LISTENER_IDLE_TIMEOUT_SECONDS = 300
_MIN_LISTENER_IDLE_TIMEOUT_SECONDS = 30
_MAX_LISTENER_IDLE_TIMEOUT_SECONDS = 3_600
_MAX_RADIO_RECENT_REFS = 20


logger = logging.getLogger(__name__)


class ListenerLifecycleAction(StrEnum):
    KEEP_ACTIVE = "keep_active"
    WAIT_FOR_LISTENER = "wait_for_listener"
    PRESERVE_AND_DISCONNECT = "preserve_and_disconnect"
    RECONNECT_ELIGIBLE = "reconnect_eligible"
    EXPLICIT_JOIN_REQUIRED = "explicit_join_required"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ListenerLifecycleDecision:
    action: ListenerLifecycleAction
    guild_id: int
    voice_channel_id: int | None
    idle_timeout_seconds: int
    reason: str
    session_identity: object | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class _RadioBinding:
    session: GuildAudioSession = field(repr=False)
    actor: MusicActor = field(repr=False)
    commit_check: MusicCommitCheck = field(repr=False)
    generation: int
    cursor: int = 0
    recent_library_refs: deque[str] = field(
        default_factory=lambda: deque(maxlen=_MAX_RADIO_RECENT_REFS),
        repr=False,
    )


class MusicService:
    def __init__(
        self,
        library: LocalMediaLibrary | None,
        source_factory: TrackSourceFactory | None,
        repository: MusicPlaylistRepository | None,
        *,
        available: bool,
        reason: str,
        indexed_tracks: int = 0,
        max_queue: int = 50,
        max_tracks_per_requester: int | None = None,
        max_speech_queue: int = 10,
        default_volume: float = 0.65,
        listener_idle_timeout_seconds: int = _DEFAULT_LISTENER_IDLE_TIMEOUT_SECONDS,
        pending_projections: Sequence[GuildAudioProjection] = (),
        related_track_port: RelatedTrackPort | None = None,
        import_store: MusicImportStore | None = None,
        state_observer: MusicStateObserver | None = None,
    ) -> None:
        requester_limit = min(10, max_queue) if max_tracks_per_requester is None else max_tracks_per_requester
        if (
            not 1 <= max_queue <= _MAX_DURABLE_TRACKS
            or type(requester_limit) is not int
            or not 1 <= requester_limit <= min(10, max_queue)
            or not 1 <= max_speech_queue <= 100
        ):
            raise ValueError("queue limits are invalid")
        if not 0.0 <= default_volume <= 2.0:
            raise ValueError("default_volume is invalid")
        if (
            isinstance(listener_idle_timeout_seconds, bool)
            or not isinstance(listener_idle_timeout_seconds, int)
            or not _MIN_LISTENER_IDLE_TIMEOUT_SECONDS
            <= listener_idle_timeout_seconds
            <= _MAX_LISTENER_IDLE_TIMEOUT_SECONDS
        ):
            raise ValueError("listener_idle_timeout_seconds is invalid")
        self.library = library
        self.source_factory = source_factory
        self.repository = repository
        self.available = bool(
            available and library is not None and source_factory is not None and repository is not None
        )
        self.seek_available = self.available and isinstance(source_factory, SeekableTrackSourceFactory)
        self.reason = reason
        self.indexed_tracks = max(0, int(indexed_tracks))
        self.max_queue = max_queue
        self.max_tracks_per_requester = requester_limit
        self.max_speech_queue = max_speech_queue
        self.default_volume = default_volume
        self.listener_idle_timeout_seconds = listener_idle_timeout_seconds
        self.import_store = import_store
        if state_observer is not None and not callable(state_observer):
            raise TypeError("state_observer must be callable")
        self._state_observer = state_observer
        self.related_track_port = (
            related_track_port
            if related_track_port is not None
            else (
                AuthorizedLocalRelatedTrackAdapter(library, repository)
                if library is not None and repository is not None
                else None
            )
        )
        self._sessions: dict[int, SessionBinding] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._projection_locks: dict[int, asyncio.Lock] = {}
        self._projection_revisions: dict[int, int] = {}
        self._pending_projections = _pending_projection_map(pending_projections)
        self._suspended_voice_channels: dict[int, int] = {}
        self._voice_free_queue_requesters: dict[int, set[int]] = {}
        self._radio_bindings: dict[int, _RadioBinding] = {}
        self._radio_fill_tasks: dict[int, asyncio.Task[None]] = {}
        self._radio_generation = 0
        self._manual_enqueue_waiters: dict[int, int] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._import_lock = asyncio.Lock()
        self._closing = False
        self._closed = False

    @classmethod
    def unavailable(cls, reason: str) -> MusicService:
        return cls(None, None, None, available=False, reason=reason)

    def status(self, *, speech_available: bool) -> MusicRuntimeStatus:
        return MusicRuntimeStatus(
            available=self.available,
            reason=self.reason,
            indexed_tracks=self.indexed_tracks,
            active_sessions=len(self._sessions),
            speech_available=speech_available,
        )

    @property
    def import_available(self) -> bool:
        return self.available and self.import_store is not None

    def session_channel_id(self, guild_id: int) -> int | None:
        if self._closed:
            return None
        binding = self._sessions.get(guild_id)
        return binding.voice_channel_id if binding is not None else None

    def listener_session_identity(self, guild_id: int) -> object | None:
        """Return an opaque identity token for exact lifecycle commit checks."""

        if self._closed or self._closing or not self.available:
            return None
        binding = self._sessions.get(guild_id)
        return None if binding is None else binding.session

    def listener_lifecycle_decision(
        self,
        guild_id: int,
        *,
        connected_voice_channel_id: int | None,
        human_listener_count: int,
        idle_elapsed_seconds: float,
        eligible_listener_voice_channel_id: int | None = None,
        eligible_listener_user_id: int | None = None,
        eligible_listener_can_manage: bool = False,
    ) -> ListenerLifecycleDecision:
        if isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
            raise ValueError("guild_id is invalid")
        for label, channel_id in (
            ("connected_voice_channel_id", connected_voice_channel_id),
            ("eligible_listener_voice_channel_id", eligible_listener_voice_channel_id),
        ):
            if channel_id is not None and (
                isinstance(channel_id, bool) or not isinstance(channel_id, int) or channel_id <= 0
            ):
                raise ValueError(f"{label} is invalid")
        if eligible_listener_user_id is not None and (
            isinstance(eligible_listener_user_id, bool)
            or not isinstance(eligible_listener_user_id, int)
            or eligible_listener_user_id <= 0
        ):
            raise ValueError("eligible_listener_user_id is invalid")
        if not isinstance(eligible_listener_can_manage, bool):
            raise TypeError("eligible_listener_can_manage must be a boolean")
        if (
            isinstance(human_listener_count, bool)
            or not isinstance(human_listener_count, int)
            or not 0 <= human_listener_count <= 10_000
        ):
            raise ValueError("human_listener_count is invalid")
        if (
            isinstance(idle_elapsed_seconds, bool)
            or not isinstance(idle_elapsed_seconds, (int, float))
            or not math.isfinite(float(idle_elapsed_seconds))
            or not 0.0 <= float(idle_elapsed_seconds) <= 86_400.0
        ):
            raise ValueError("idle_elapsed_seconds is invalid")

        timeout = self.listener_idle_timeout_seconds
        if self._closed or self._closing or not self.available:
            return ListenerLifecycleDecision(
                ListenerLifecycleAction.UNAVAILABLE,
                guild_id,
                None,
                timeout,
                "runtime_unavailable",
            )
        binding = self._sessions.get(guild_id)
        if binding is not None:
            channel_id = binding.voice_channel_id
            if connected_voice_channel_id != channel_id:
                return ListenerLifecycleDecision(
                    ListenerLifecycleAction.PRESERVE_AND_DISCONNECT,
                    guild_id,
                    channel_id,
                    timeout,
                    "voice_connection_lost",
                    _session(binding),
                )
            if human_listener_count == 0:
                action = (
                    ListenerLifecycleAction.PRESERVE_AND_DISCONNECT
                    if float(idle_elapsed_seconds) >= timeout
                    else ListenerLifecycleAction.WAIT_FOR_LISTENER
                )
                return ListenerLifecycleDecision(
                    action,
                    guild_id,
                    channel_id,
                    timeout,
                    "no_eligible_listeners",
                    _session(binding),
                )
            return ListenerLifecycleDecision(
                ListenerLifecycleAction.KEEP_ACTIVE,
                guild_id,
                channel_id,
                timeout,
                "eligible_listener_present",
                _session(binding),
            )

        pending = self._pending_projections.get(guild_id)
        if pending is None:
            return ListenerLifecycleDecision(
                ListenerLifecycleAction.UNAVAILABLE,
                guild_id,
                None,
                timeout,
                "no_preserved_queue",
            )
        suspended_channel_id = self._suspended_voice_channels.get(guild_id)
        requester_ids = {track.requester_id for track in pending.tracks}
        listener_controls_queue = eligible_listener_can_manage or (
            eligible_listener_user_id is not None and eligible_listener_user_id in requester_ids
        )
        if (
            suspended_channel_id is not None
            and eligible_listener_voice_channel_id == suspended_channel_id
            and human_listener_count > 0
            and listener_controls_queue
        ):
            return ListenerLifecycleDecision(
                ListenerLifecycleAction.RECONNECT_ELIGIBLE,
                guild_id,
                suspended_channel_id,
                timeout,
                "eligible_listener_returned",
            )
        voice_free_requester_ids = self._voice_free_queue_requesters.get(guild_id, set())
        if (
            suspended_channel_id is None
            and eligible_listener_user_id in voice_free_requester_ids
            and eligible_listener_user_id in requester_ids
            and eligible_listener_voice_channel_id is not None
            and human_listener_count > 0
        ):
            return ListenerLifecycleDecision(
                ListenerLifecycleAction.RECONNECT_ELIGIBLE,
                guild_id,
                eligible_listener_voice_channel_id,
                timeout,
                "voice_free_requester_joined",
            )
        return ListenerLifecycleDecision(
            ListenerLifecycleAction.EXPLICIT_JOIN_REQUIRED,
            guild_id,
            suspended_channel_id,
            timeout,
            "explicit_join_required",
        )

    async def listener_lifecycle_decision_current(
        self,
        guild_id: int,
        *,
        connected_voice_channel_id: int | None,
        human_listener_count: int,
        idle_elapsed_seconds: float,
        eligible_listener_voice_channel_id: int | None = None,
        eligible_listener_user_id: int | None = None,
        eligible_listener_can_manage: bool = False,
    ) -> ListenerLifecycleDecision:
        """Serialize a pending-queue decision behind the guild mutation lock."""

        async with self._lock(guild_id):
            return self.listener_lifecycle_decision(
                guild_id,
                connected_voice_channel_id=connected_voice_channel_id,
                human_listener_count=human_listener_count,
                idle_elapsed_seconds=idle_elapsed_seconds,
                eligible_listener_voice_channel_id=eligible_listener_voice_channel_id,
                eligible_listener_user_id=eligible_listener_user_id,
                eligible_listener_can_manage=eligible_listener_can_manage,
            )

    async def suspend_voice_session(
        self,
        guild_id: int,
        *,
        expected_voice_channel_id: int,
        expected_session_identity: object,
    ) -> GuildAudioProjection:
        """Persist a queue and close its voice binding without deleting it."""

        self._require_available()
        if (
            isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or isinstance(expected_voice_channel_id, bool)
            or not isinstance(expected_voice_channel_id, int)
            or expected_voice_channel_id <= 0
        ):
            raise ValueError("voice session scope is invalid")

        session: GuildAudioSession | None = None
        projection: GuildAudioProjection | None = None
        radio_task: asyncio.Task[None] | None = None
        frozen = False
        failure: BaseException | None = None
        async with self._lock(guild_id):
            self._require_available()
            binding = self._binding(guild_id)
            if (
                binding.voice_channel_id != expected_voice_channel_id
                or binding.session is not expected_session_identity
            ):
                raise MusicSessionError("voice channel binding changed")
            session = _session(binding)
            expected_library = self.library
            expected_repository = self.repository
            assert expected_library is not None and expected_repository is not None
            try:
                snapshot = await session.freeze_projection()
                frozen = True
                saved = await self._save_session_projection(
                    guild_id,
                    expected_session=session,
                    expected_library=expected_library,
                    expected_repository=expected_repository,
                    revision=snapshot.revision,
                    snapshot=snapshot,
                )
                if not saved and self._projection_revisions.get(guild_id, -1) < snapshot.revision:
                    raise MusicSessionError("audio projection could not be saved")
                projection = await asyncio.to_thread(expected_repository.load_audio_projection, guild_id)
                self._require_runtime_identity(
                    guild_id,
                    expected_session=session,
                    expected_library=expected_library,
                    expected_repository=expected_repository,
                    allow_closing=False,
                )
                if projection is None:
                    raise MusicSessionError("audio projection could not be loaded")
                self._pending_projections[guild_id] = projection
                self._suspended_voice_channels[guild_id] = expected_voice_channel_id
                self._sessions.pop(guild_id, None)
            except BaseException as exc:
                failure = exc
                if frozen and self._sessions.get(guild_id) is binding:
                    self._sessions.pop(guild_id, None)
                    self._suspended_voice_channels.pop(guild_id, None)
            if self._sessions.get(guild_id) is not binding:
                self._radio_bindings.pop(guild_id, None)
                radio_task = self._radio_fill_tasks.pop(guild_id, None)
                if radio_task is not None:
                    radio_task.cancel()
            if projection is not None or frozen:
                await _close_session_cancellation_safe(session)
        await _cancel_task(radio_task)
        if failure is not None:
            if isinstance(failure, asyncio.CancelledError):
                raise failure
            if isinstance(failure, MusicError):
                raise failure
            raise MusicSessionError("voice session could not be preserved") from failure
        assert projection is not None
        self._notify_state_changed(guild_id)
        return projection

    async def join(
        self,
        guild_id: int,
        voice_client: Any,
        actor: MusicActor,
        *,
        voice_channel_id: int,
        commit_check: MusicCommitCheck | None = None,
    ) -> GuildAudioSession:
        self._require_available()
        if guild_id <= 0 or voice_channel_id <= 0 or actor.voice_channel_id != voice_channel_id:
            raise MusicAuthorizationError("same voice channel is required")
        async with self._lock(guild_id):
            self._require_available()
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            if fresh_actor.voice_channel_id != voice_channel_id:
                raise MusicAuthorizationError("same voice channel is required")
            existing = self._sessions.get(guild_id)
            if existing is not None:
                if existing.voice_channel_id != voice_channel_id:
                    raise MusicSessionError("music is active in another voice channel")
                existing_session = _session(existing)
                current_channel_id = getattr(getattr(voice_client, "channel", None), "id", None)
                if (
                    existing_session.voice_client is not voice_client
                    or isinstance(current_channel_id, bool)
                    or not isinstance(current_channel_id, int)
                    or current_channel_id != voice_channel_id
                ):
                    raise MusicSessionError("music voice connection changed")
                return existing_session
            assert self.source_factory is not None and self.library is not None and self.repository is not None
            expected_library = self.library
            expected_repository = self.repository
            session_holder: list[GuildAudioSession] = []

            async def state_changed(revision: int, snapshot: QueueSnapshot) -> None:
                if not session_holder:
                    return
                await self._save_session_projection(
                    guild_id,
                    expected_session=session_holder[0],
                    expected_library=expected_library,
                    expected_repository=expected_repository,
                    revision=revision,
                    snapshot=snapshot,
                )
                self._schedule_radio_fill(guild_id, session_holder[0], snapshot)
                self._notify_state_changed(guild_id)

            session = GuildAudioSession(
                guild_id,
                voice_client,
                self.source_factory,
                track_allowed=lambda track: self._track_rights_allowed(guild_id, track),
                max_queue=self.max_queue,
                max_tracks_per_requester=self.max_tracks_per_requester,
                max_speech_queue=self.max_speech_queue,
                volume=self.default_volume,
                state_changed=state_changed,
            )
            session_holder.append(session)
            binding = SessionBinding(session, voice_channel_id, fresh_actor.user_id)
            self._sessions[guild_id] = binding
            pending = self._pending_projections.get(guild_id)
            if pending is not None:
                try:
                    tracks = await self._resolve_pending_projection(
                        pending,
                        expected_session=session,
                        expected_library=expected_library,
                        expected_repository=expected_repository,
                    )
                    fresh_actor = await self._require_fresh_actor(commit_check, actor)
                    if fresh_actor.voice_channel_id != voice_channel_id:
                        raise MusicAuthorizationError("same voice channel is required")
                    self._require_runtime_identity(
                        guild_id,
                        expected_session=session,
                        expected_library=expected_library,
                        expected_repository=expected_repository,
                        allow_closing=False,
                    )
                    await session.restore_projection(
                        tracks,
                        loop_mode=pending.loop_mode,
                        paused=pending.paused,
                        music_volume=pending.music_volume,
                        speech_volume=pending.speech_volume,
                    )
                except BaseException:
                    if self._sessions.get(guild_id) is binding:
                        self._sessions.pop(guild_id, None)
                    await _close_session_cancellation_safe(session)
                    raise
                else:
                    if self._pending_projections.get(guild_id) is pending:
                        self._pending_projections.pop(guild_id, None)
                    self._suspended_voice_channels.pop(guild_id, None)
                    self._voice_free_queue_requesters.pop(guild_id, None)
            return session

    async def leave(
        self,
        guild_id: int,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> bool:
        self._require_available()
        radio_task: asyncio.Task[None] | None = None
        async with self._lock(guild_id):
            binding = self._binding(guild_id)
            snapshot = await _session(binding).snapshot()
            self._require_same_channel(binding, actor)
            self._require_control(binding, snapshot, actor)
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            self._require_same_channel(binding, fresh_actor)
            self._require_control(binding, snapshot, fresh_actor)
            self._require_available()
            self._sessions.pop(guild_id, None)
            try:
                await self._delete_audio_projection(guild_id, required=True)
            except BaseException:
                if not self._closed and not self._closing:
                    self._sessions[guild_id] = binding
                raise
            else:
                self._pending_projections.pop(guild_id, None)
                self._suspended_voice_channels.pop(guild_id, None)
                self._radio_bindings.pop(guild_id, None)
                radio_task = self._radio_fill_tasks.pop(guild_id, None)
                if radio_task is not None:
                    radio_task.cancel()
        await _cancel_task(radio_task)
        await _close_session_cancellation_safe(_session(binding))
        self._notify_state_changed(guild_id)
        return True

    async def search(
        self,
        query: str,
        actor: MusicActor,
        *,
        guild_id: int | None = None,
        limit: int = 10,
    ) -> tuple[Track, ...]:
        self._require_available()
        assert self.library is not None and self.repository is not None
        library = self.library
        repository = self.repository
        try:
            imported_assets = (
                ()
                if guild_id is None
                else await asyncio.to_thread(
                    repository.search_imported_assets_for_guild,
                    guild_id,
                    query,
                    limit=limit,
                )
            )
            imported_tracks: list[Track] = []
            for asset in imported_assets:
                track = await asyncio.to_thread(
                    library.resolve_persisted_track,
                    asset.library_ref,
                    asset.content_sha256,
                    actor.user_id,
                    0,
                )
                imported_tracks.append(replace(track, title=asset.display_title))
            regular_tracks = await asyncio.to_thread(
                library.search,
                query,
                requester_id=actor.user_id,
                limit=limit,
            )
            all_imported = await asyncio.to_thread(repository.list_imported_assets)
            imported_identities = {(asset.library_ref, asset.content_sha256) for asset in all_imported}
            filtered_regular: list[Track] = []
            for track in regular_tracks:
                sealed = await asyncio.to_thread(library.seal_track, track)
                if (sealed.library_ref, sealed.content_sha256) not in imported_identities:
                    filtered_regular.append(sealed)
            tracks = tuple((imported_tracks + filtered_regular)[:limit])
        except (MediaLibraryError, PlaylistError) as exc:
            raise MusicSessionError("local library search failed") from exc
        if self.library is not library or self.repository is not repository:
            raise MusicUnavailableError("music runtime identity changed")
        self._require_available()
        return tracks

    async def _resolve_track_for_guild(
        self,
        guild_id: int,
        query: str,
        requester_id: int,
        *,
        library: LocalMediaLibrary,
        repository: MusicPlaylistRepository | None,
    ) -> Track:
        if repository is None:
            raise MusicUnavailableError("music repository is unavailable")
        imported = await asyncio.to_thread(
            repository.search_imported_assets_for_guild,
            guild_id,
            query,
            limit=1,
        )
        if imported:
            asset = imported[0]
            track = await asyncio.to_thread(
                library.resolve_persisted_track,
                asset.library_ref,
                asset.content_sha256,
                requester_id,
                0,
            )
            return replace(track, title=asset.display_title)
        track = await asyncio.to_thread(library.resolve_track, query, requester_id=requester_id)
        sealed = await asyncio.to_thread(library.seal_track, track)
        imported_assets = await asyncio.to_thread(repository.list_imported_assets)
        if any(
            item.library_ref == sealed.library_ref and item.content_sha256 == sealed.content_sha256
            for item in imported_assets
        ):
            raise MusicAuthorizationError("track rights are not approved")
        return sealed

    async def import_wav(
        self,
        guild_id: int,
        data: bytes,
        display_title: str,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
        runtime_current: MusicRuntimeCurrent | None = None,
    ) -> ImportedMusicAsset:
        """Persist one explicitly rights-confirmed WAV without mutating playback."""

        self._require_available()
        _require_runtime_current(runtime_current)
        if not actor.manage_guild:
            raise MusicAuthorizationError("Manage Guild is required")
        display_title = normalize_imported_music_title(display_title)
        library = self.library
        repository = self.repository
        store = self.import_store
        if library is None or repository is None or store is None:
            raise MusicUnavailableError("music import is unavailable")
        fresh_actor = await self._require_fresh_actor(commit_check, actor)
        _require_runtime_current(runtime_current)
        if not fresh_actor.manage_guild:
            raise MusicAuthorizationError("Manage Guild is required")
        await self._import_lock.acquire()
        created = False
        receipt = None
        asset: ImportedMusicAsset | None = None
        stored_asset: ImportedMusicAsset | None = None
        deferred_cancellation: asyncio.CancelledError | None = None
        database_started = False
        database_completed = False
        try:
            put_result, cancellation = await _run_thread_operation(store.put_wav, data)
            receipt, created = put_result
            deferred_cancellation = deferred_cancellation or cancellation
            if self.library is not library or self.repository is not repository or self.import_store is not store:
                raise MusicUnavailableError("music import runtime identity changed")
            _require_runtime_current(runtime_current)
            self._require_available()
            indexed_tracks, cancellation = await _run_thread_operation(library.refresh)
            self.indexed_tracks = indexed_tracks
            deferred_cancellation = deferred_cancellation or cancellation
            provisional = Track(display_title, receipt.path, actor.user_id)
            sealed, cancellation = await _run_thread_operation(library.seal_track, provisional)
            deferred_cancellation = deferred_cancellation or cancellation
            if sealed.library_ref is None or sealed.content_sha256 is None:
                raise MusicUnavailableError("music import identity is unavailable")
            asset = ImportedMusicAsset(
                library_ref=sealed.library_ref,
                content_sha256=sealed.content_sha256,
                display_title=display_title,
                size_bytes=receipt.size_bytes,
                duration_milliseconds=round(receipt.duration_seconds * 1_000),
            )
            if (
                self.library is not library
                or self.repository is not repository
                or self.import_store is not store
                or receipt.content_sha256 != asset.content_sha256
            ):
                raise MusicUnavailableError("music import runtime identity changed")
            _require_runtime_current(runtime_current)
            if deferred_cancellation is not None:
                raise deferred_cancellation
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            _require_runtime_current(runtime_current)
            if not fresh_actor.manage_guild:
                raise MusicAuthorizationError("Manage Guild is required")
            database_started = True
            try:
                stored_asset, cancellation = await _run_thread_operation(
                    repository.register_imported_asset_and_grant,
                    guild_id,
                    asset,
                    runtime_current,
                )
            finally:
                database_completed = True
            deferred_cancellation = deferred_cancellation or cancellation
            if self.library is not library or self.repository is not repository or self.import_store is not store:
                raise MusicUnavailableError("music import runtime identity changed")
            _require_runtime_current(runtime_current)
            if deferred_cancellation is not None:
                logger.info(
                    "music_import_commit_completed_after_cancellation",
                    extra={"guild_id": guild_id},
                )
                raise deferred_cancellation
            return stored_asset
        except asyncio.CancelledError:
            raise
        except MusicError:
            raise
        except (MediaLibraryError, MusicImportStoreError, ValueError) as exc:
            raise MusicSessionError("music import failed") from exc
        except Exception as exc:
            raise MusicSessionError("music import failed") from exc
        finally:
            if receipt is not None and created and stored_asset is None:
                with suppress(Exception):
                    reference_count, _ = await _run_thread_operation(
                        repository.imported_digest_reference_count,
                        receipt.content_sha256,
                        missing_is_zero=(
                            (not database_started or database_completed) and _runtime_is_current(runtime_current)
                        ),
                    )
                    await _run_thread_operation(store.discard_if_unreferenced, receipt, reference_count)
                    await _run_thread_operation(library.refresh)
            self._import_lock.release()

    async def play(
        self,
        guild_id: int,
        query: str,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> tuple[Track, int]:
        self._require_available()
        async with self._manual_enqueue_scope(guild_id):
            if actor.voice_channel_id is None:
                if commit_check is None:
                    raise MusicAuthorizationError("fresh authorization is required")
                async with self._lifecycle_lock:
                    return await self._play_current(
                        guild_id,
                        query,
                        actor,
                        commit_check=commit_check,
                    )
            return await self._play_current(
                guild_id,
                query,
                actor,
                commit_check=commit_check,
            )

    async def _play_current(
        self,
        guild_id: int,
        query: str,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None,
    ) -> tuple[Track, int]:
        self._require_available()
        assert self.library is not None
        library = self.library
        async with self._lock(guild_id):
            binding = self._sessions.get(guild_id)
            pending = self._pending_projections.get(guild_id)
            if binding is not None:
                self._require_same_channel(binding, actor)
            elif actor.voice_channel_id is not None:
                raise MusicSessionError("music is not connected")
            elif pending is not None and guild_id not in self._voice_free_queue_requesters:
                raise MusicSessionError("explicit music join is required")
            try:
                track = await self._resolve_track_for_guild(
                    guild_id,
                    query,
                    actor.user_id,
                    library=library,
                    repository=self.repository,
                )
            except (MediaLibraryError, PlaylistError) as exc:
                raise MusicSessionError("track was not found") from exc
            if self.library is not library:
                raise MusicUnavailableError("music runtime identity changed")
            self._require_available()
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            if binding is not None:
                self._require_same_channel(binding, fresh_actor)
            elif fresh_actor.voice_channel_id is not None:
                raise MusicAuthorizationError("voice state changed before queue commit")
            if self.library is not library or self._sessions.get(guild_id) is not binding:
                raise MusicUnavailableError("music runtime identity changed")
            self._require_track_rights(guild_id, track)
            if binding is None:
                repository = self.repository
                if repository is None or track.library_ref is None or track.content_sha256 is None:
                    raise MusicUnavailableError("music runtime identity changed")
                existing = () if pending is None else pending.tracks
                if len(existing) >= self.max_queue:
                    raise MusicSessionError("music queue is full")
                requester_count = sum(1 for item in existing if item.requester_id == actor.user_id)
                if requester_count >= self.max_tracks_per_requester:
                    raise MusicSessionError("requester music queue limit reached")
                projection = GuildAudioProjection(
                    guild_id=guild_id,
                    tracks=(
                        *existing,
                        PersistedMusicTrackRef(
                            library_ref=track.library_ref,
                            content_sha256=track.content_sha256,
                            requester_id=actor.user_id,
                            retry_count=track.retry_count,
                        ),
                    ),
                    loop_mode=LoopMode.OFF if pending is None else pending.loop_mode,
                    paused=False if pending is None else pending.paused,
                    music_volume=self.default_volume if pending is None else pending.music_volume,
                    speech_volume=1.0 if pending is None else pending.speech_volume,
                )
                async with self._projection_lock(guild_id):
                    if (
                        self._closed
                        or self._closing
                        or not self.available
                        or self.library is not library
                        or self.repository is not repository
                        or self._sessions.get(guild_id) is not None
                        or self._pending_projections.get(guild_id) is not pending
                    ):
                        raise MusicUnavailableError("music runtime identity changed")
                    fresh_actor = await self._require_fresh_actor(commit_check, actor)
                    if fresh_actor.voice_channel_id is not None:
                        raise MusicAuthorizationError("voice state changed before queue commit")
                    if (
                        self._closed
                        or self._closing
                        or not self.available
                        or self.library is not library
                        or self.repository is not repository
                        or self._sessions.get(guild_id) is not None
                        or self._pending_projections.get(guild_id) is not pending
                    ):
                        raise MusicUnavailableError("music runtime identity changed")
                    self._require_track_rights(guild_id, track)
                    save_task = asyncio.create_task(asyncio.to_thread(repository.save_audio_projection, projection))
                    deferred_cancellation: asyncio.CancelledError | None = None
                    while True:
                        try:
                            await asyncio.shield(save_task)
                            break
                        except asyncio.CancelledError as exc:
                            if save_task.cancelled():
                                raise
                            if deferred_cancellation is None:
                                deferred_cancellation = exc
                            current_task = asyncio.current_task()
                            if current_task is not None:
                                current_task.uncancel()
                    if (
                        self.library is not library
                        or self.repository is not repository
                        or self._sessions.get(guild_id) is not None
                        or self._pending_projections.get(guild_id) is not pending
                    ):
                        raise MusicUnavailableError("music runtime identity changed")
                    self._pending_projections[guild_id] = projection
                    self._projection_revisions.pop(guild_id, None)
                    self._voice_free_queue_requesters.setdefault(guild_id, set()).add(actor.user_id)
                    self._notify_state_changed(guild_id)
                    if deferred_cancellation is not None:
                        logger.info(
                            "music_voice_free_queue_commit_completed_after_cancellation",
                            extra={"guild_id": guild_id},
                        )
                        raise deferred_cancellation
                return track, len(projection.tracks)
            try:
                position = await _session(binding).enqueue(track)
            except RequesterQueueLimitError as exc:
                raise MusicSessionError("requester music queue limit reached") from exc
            except QueueFullError as exc:
                raise MusicSessionError("music queue is full") from exc
            except TrackSourceAuthorizationError as exc:
                raise MusicAuthorizationError("track rights are no longer approved") from exc
            return track, position

    async def snapshot(self, guild_id: int) -> QueueSnapshot:
        self._require_available()
        binding = self._binding(guild_id)
        return await _session(binding).snapshot()

    def local_radio_enabled(self, guild_id: int) -> bool:
        return not self._closed and not self._closing and guild_id in self._radio_bindings

    async def set_local_radio(
        self,
        guild_id: int,
        actor: MusicActor,
        enabled: bool,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> bool:
        self._require_available()
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        if commit_check is None:
            raise MusicAuthorizationError("fresh authorization is required")
        previous_task: asyncio.Task[None] | None = None
        session: GuildAudioSession | None = None
        snapshot: QueueSnapshot | None = None
        async with self._lock(guild_id):
            self._require_available()
            binding = self._binding(guild_id)
            self._require_same_channel(binding, actor)
            session = _session(binding)
            snapshot = await session.snapshot()
            self._require_control(binding, snapshot, actor)
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            self._require_same_channel(binding, fresh_actor)
            self._require_control(binding, snapshot, fresh_actor)
            self._require_available()
            if enabled:
                provider = self.related_track_port
                if provider is None:
                    raise MusicUnavailableError("local radio provider is unavailable")
                self._require_radio_provider_identity(provider)
                self._radio_generation += 1
                previous_task = self._radio_fill_tasks.pop(guild_id, None)
                self._radio_bindings[guild_id] = _RadioBinding(
                    session=session,
                    actor=fresh_actor,
                    commit_check=commit_check,
                    generation=self._radio_generation,
                )
            else:
                self._radio_bindings.pop(guild_id, None)
                previous_task = self._radio_fill_tasks.pop(guild_id, None)
        await _cancel_task(previous_task)
        if enabled:
            assert session is not None and snapshot is not None
            self._schedule_radio_fill(guild_id, session, snapshot)
        self._notify_state_changed(guild_id)
        return enabled

    def _schedule_radio_fill(
        self,
        guild_id: int,
        session: GuildAudioSession,
        snapshot: QueueSnapshot,
    ) -> None:
        radio = self._radio_bindings.get(guild_id)
        existing = self._radio_fill_tasks.get(guild_id)
        binding = self._sessions.get(guild_id)
        if (
            self._closed
            or self._closing
            or not self.available
            or radio is None
            or radio.session is not session
            or binding is None
            or binding.session is not session
            or snapshot.current is not None
            or snapshot.upcoming
            or self._manual_enqueue_waiters.get(guild_id, 0) > 0
            or (existing is not None and not existing.done())
        ):
            return
        task = asyncio.create_task(self._fill_local_radio_once(guild_id, radio.generation))
        self._radio_fill_tasks[guild_id] = task
        task.add_done_callback(lambda completed: self._radio_task_done(guild_id, completed))

    def _radio_task_done(self, guild_id: int, task: asyncio.Task[None]) -> None:
        if self._radio_fill_tasks.get(guild_id) is task:
            self._radio_fill_tasks.pop(guild_id, None)
        if task.cancelled():
            return
        with suppress(Exception):
            task.result()

    async def _fill_local_radio_once(self, guild_id: int, generation: int) -> None:
        try:
            async with self._lock(guild_id):
                radio = self._radio_bindings.get(guild_id)
                binding = self._sessions.get(guild_id)
                provider = self.related_track_port
                if (
                    radio is None
                    or radio.generation != generation
                    or binding is None
                    or binding.session is not radio.session
                    or provider is None
                    or self._manual_enqueue_waiters.get(guild_id, 0) > 0
                ):
                    return
                self._require_available()
                self._require_radio_provider_identity(provider)
                snapshot = await radio.session.snapshot()
                if snapshot.current is not None or snapshot.upcoming:
                    return
                excluded_refs = {
                    track.library_ref
                    for track in ((snapshot.current,) if snapshot.current is not None else ())
                    if track.library_ref is not None
                }
                excluded_refs.update(track.library_ref for track in snapshot.upcoming if track.library_ref is not None)
                excluded_refs.update(radio.recent_library_refs)
                excluded_titles = {item.title for item in snapshot.recent}
                excluded_titles.update(
                    track.title for track in ((snapshot.current,) if snapshot.current is not None else ())
                )
                excluded_titles.update(track.title for track in snapshot.upcoming)
                seed_title = snapshot.recent[0].title if snapshot.recent else None
                request = RelatedTrackRequest(
                    guild_id=guild_id,
                    requester_id=radio.actor.user_id,
                    cursor=radio.cursor,
                    max_candidates=25,
                    seed_title=seed_title,
                    excluded_library_refs=frozenset(excluded_refs),
                    excluded_titles=frozenset(excluded_titles),
                )
                actor = radio.actor
                commit_check = radio.commit_check
                voice_channel_id = binding.voice_channel_id

            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            if fresh_actor.voice_channel_id != voice_channel_id:
                raise MusicAuthorizationError("same voice channel is required")
            self._require_control(binding, snapshot, fresh_actor)
            selection = await provider.select_related(request)

            async with self._lock(guild_id):
                radio = self._radio_bindings.get(guild_id)
                binding = self._sessions.get(guild_id)
                if (
                    radio is None
                    or radio.generation != generation
                    or binding is None
                    or binding.session is not radio.session
                    or self.related_track_port is not provider
                ):
                    return
                self._require_available()
                self._require_radio_provider_identity(provider)
                fresh_actor = await self._require_fresh_actor(commit_check, actor)
                self._require_same_channel(binding, fresh_actor)
                snapshot = await radio.session.snapshot()
                self._require_control(binding, snapshot, fresh_actor)
                if (
                    snapshot.current is not None
                    or snapshot.upcoming
                    or self._manual_enqueue_waiters.get(guild_id, 0) > 0
                ):
                    return
                candidate = selection.candidate
                radio.cursor = selection.next_cursor
                if candidate is None:
                    return
                if (
                    candidate.requester_id != fresh_actor.user_id
                    or candidate.library_ref is None
                    or candidate.content_sha256 is None
                    or candidate.library_ref in request.excluded_library_refs
                    or candidate.title.strip().casefold() in request.excluded_titles
                ):
                    raise MusicAuthorizationError("radio candidate binding is invalid")
                self._require_track_rights(guild_id, candidate)
                try:
                    await radio.session.enqueue(candidate)
                except asyncio.CancelledError:
                    await _discard_queued_track_cancellation_safe(
                        radio.session,
                        candidate.track_id,
                    )
                    raise
                radio.recent_library_refs.append(candidate.library_ref)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._lock(guild_id):
                radio = self._radio_bindings.get(guild_id)
                if radio is not None and radio.generation == generation:
                    self._radio_bindings.pop(guild_id, None)
            logger.warning(
                "music_radio_fill_failed",
                extra={"error_type": type(exc).__name__},
            )

    def _require_radio_provider_identity(self, provider: RelatedTrackPort) -> None:
        if self.related_track_port is not provider:
            raise MusicUnavailableError("local radio provider identity changed")
        if isinstance(provider, AuthorizedLocalRelatedTrackAdapter) and (
            provider.library is not self.library or provider.repository is not self.repository
        ):
            raise MusicUnavailableError("local radio provider identity changed")

    async def pause(
        self,
        guild_id: int,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> None:
        await self._control(guild_id, actor, "pause", commit_check=commit_check)

    async def resume(
        self,
        guild_id: int,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> None:
        await self._control(guild_id, actor, "resume", commit_check=commit_check)

    async def skip(
        self,
        guild_id: int,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> Track:
        result = await self._control(guild_id, actor, "skip", commit_check=commit_check)
        assert isinstance(result, Track)
        return result

    async def stop_music(
        self,
        guild_id: int,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> int:
        result = await self._control(guild_id, actor, "stop", commit_check=commit_check)
        return int(result)

    async def shuffle(
        self,
        guild_id: int,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> int:
        result = await self._control(guild_id, actor, "shuffle", commit_check=commit_check)
        return int(result)

    async def set_loop(
        self,
        guild_id: int,
        actor: MusicActor,
        mode: LoopMode,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> None:
        await self._control(guild_id, actor, "loop", value=mode, commit_check=commit_check)

    async def set_volume(
        self,
        guild_id: int,
        actor: MusicActor,
        volume: float,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> None:
        await self._control(guild_id, actor, "volume", value=volume, commit_check=commit_check)

    async def set_speech_volume(
        self,
        guild_id: int,
        actor: MusicActor,
        volume: float,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> None:
        await self._control(guild_id, actor, "speech-volume", value=volume, commit_check=commit_check)

    async def seek(
        self,
        guild_id: int,
        actor: MusicActor,
        seconds: int,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> Track:
        self._require_available()
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= _MAX_SEEK_SECONDS:
            raise MusicSessionError("seek position is out of range")
        async with self._lock(guild_id):
            binding = self._binding(guild_id)
            self._require_same_channel(binding, actor)
            session = _session(binding)
            snapshot = await session.snapshot()
            track = snapshot.current
            if track is None:
                raise MusicSessionError("music is not playing")
            self._require_control(binding, snapshot, actor)
            self._require_track_rights(guild_id, track)
            source_factory = self.source_factory
            if (
                not self.seek_available
                or not isinstance(source_factory, SeekableTrackSourceFactory)
                or session.source_factory is not source_factory
            ):
                raise MusicSeekUnsupportedError("seek is not supported")
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            self._require_same_channel(binding, fresh_actor)
            self._require_control(binding, snapshot, fresh_actor)
            self._require_available()
            self._require_track_rights(guild_id, track)

            async def authorization_current() -> bool:
                try:
                    self._require_available()
                    if (
                        self.source_factory is not source_factory
                        or self._sessions.get(guild_id) is not binding
                        or session.source_factory is not source_factory
                    ):
                        return False
                    current_actor = await self._require_fresh_actor(commit_check, actor)
                    self._require_same_channel(binding, current_actor)
                    if not current_actor.manage_guild and track.requester_id != current_actor.user_id:
                        return False
                    return self._track_rights_allowed(guild_id, track)
                except MusicError:
                    return False

            try:
                await session.seek(
                    seconds,
                    expected_revision=snapshot.revision,
                    expected_track_id=track.track_id,
                    authorization_current=authorization_current,
                )
            except SeekUnsupportedError as exc:
                raise MusicSeekUnsupportedError("seek is not supported") from exc
            except TrackSourceAuthorizationError as exc:
                raise MusicAuthorizationError("track rights or capability changed") from exc
            except (PlayerStateError, QueueFullError, TypeError, ValueError) as exc:
                raise MusicSessionError("seek is not available") from exc
            return track

    async def remove(
        self,
        guild_id: int,
        actor: MusicActor,
        position: int,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> Track:
        self._require_available()
        if type(position) is not int:
            raise MusicSessionError("queue position is out of range")
        async with self._lock(guild_id):
            binding = self._binding(guild_id)
            self._require_same_channel(binding, actor)
            snapshot = await _session(binding).snapshot()
            if not 1 <= position <= len(snapshot.upcoming):
                raise MusicSessionError("queue position is out of range")
            target = snapshot.upcoming[position - 1]
            if not actor.manage_guild and target.requester_id != actor.user_id:
                raise MusicAuthorizationError("only the requester or Manage Guild can remove this track")
            try:
                self._require_available()
                fresh_actor = await self._require_fresh_actor(commit_check, actor)
                self._require_same_channel(binding, fresh_actor)
                if not fresh_actor.manage_guild and target.requester_id != fresh_actor.user_id:
                    raise MusicAuthorizationError("only the requester or Manage Guild can remove this track")
                self._require_available()
                return await _session(binding).remove(position, expected_track_id=target.track_id)
            except (IndexError, PlayerStateError) as exc:
                raise MusicSessionError("queue position is out of range") from exc

    async def move(
        self,
        guild_id: int,
        actor: MusicActor,
        source_position: int,
        target_position: int,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> Track:
        self._require_available()
        if type(source_position) is not int or type(target_position) is not int:
            raise MusicSessionError("queue position is out of range")
        async with self._lock(guild_id):
            binding = self._binding(guild_id)
            self._require_same_channel(binding, actor)
            snapshot = await _session(binding).snapshot()
            if not 1 <= source_position <= len(snapshot.upcoming) or not 1 <= target_position <= len(snapshot.upcoming):
                raise MusicSessionError("queue position is out of range")
            target = snapshot.upcoming[source_position - 1]
            if not actor.manage_guild and target.requester_id != actor.user_id:
                raise MusicAuthorizationError("only the requester or Manage Guild can move this track")
            self._require_available()
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            self._require_same_channel(binding, fresh_actor)
            if not fresh_actor.manage_guild and target.requester_id != fresh_actor.user_id:
                raise MusicAuthorizationError("only the requester or Manage Guild can move this track")
            self._require_available()
            try:
                return await _session(binding).move(
                    source_position,
                    target_position,
                    expected_track_id=target.track_id,
                )
            except (IndexError, PlayerStateError) as exc:
                raise MusicSessionError("music queue changed") from exc

    async def clear_requester(
        self,
        guild_id: int,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> int:
        self._require_available()
        async with self._lock(guild_id):
            binding = self._binding(guild_id)
            self._require_same_channel(binding, actor)
            self._require_available()
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            self._require_same_channel(binding, fresh_actor)
            self._require_available()
            return await _session(binding).clear_requester(actor.user_id)

    async def add_speech_wav(
        self,
        guild_id: int,
        actor: MusicActor,
        wav: bytes,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> int:
        self._require_available()
        assert self.source_factory is not None
        async with self._lock(guild_id):
            binding = self._binding(guild_id)
            self._require_same_channel(binding, actor)
            try:
                source = await asyncio.to_thread(self.source_factory.create_speech, wav)
            except Exception as exc:
                raise MusicSessionError("speech source could not be prepared") from exc
            self._require_available()
            try:
                fresh_actor = await self._require_fresh_actor(commit_check, actor)
                self._require_same_channel(binding, fresh_actor)
                return await _session(binding).add_speech(source)
            except QueueFullError as exc:
                with suppress(Exception):
                    await asyncio.to_thread(source.cleanup)
                raise MusicSessionError("speech queue is full") from exc
            except Exception:
                with suppress(Exception):
                    await asyncio.to_thread(source.cleanup)
                raise

    async def save_playlist(
        self,
        guild_id: int,
        actor: MusicActor,
        name: str,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> PlaylistRecord:
        self._require_available()
        assert self.repository is not None
        async with self._lifecycle_lock:
            self._require_available()
            async with self._lock(guild_id):
                binding = self._binding(guild_id)
                self._require_same_channel(binding, actor)
                snapshot = await _session(binding).snapshot()
                titles = tuple(track.title for track in ([snapshot.current] if snapshot.current else [])) + tuple(
                    track.title for track in snapshot.upcoming
                )
                if not titles:
                    raise PlaylistError("music queue is empty")
                fresh_actor = await self._require_fresh_actor(commit_check, actor)
                self._require_same_channel(binding, fresh_actor)
                self._require_available()
                return await asyncio.to_thread(self.repository.save, guild_id, actor.user_id, name, titles)

    async def list_playlists(self, guild_id: int, actor: MusicActor) -> tuple[PlaylistRecord, ...]:
        self._require_available()
        assert self.repository is not None
        records = await asyncio.to_thread(self.repository.list, guild_id, actor.user_id)
        self._require_available()
        return records

    async def load_playlist(
        self,
        guild_id: int,
        actor: MusicActor,
        name: str,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> tuple[int, int]:
        async with self._manual_enqueue_scope(guild_id):
            return await self._load_playlist_current(
                guild_id,
                actor,
                name,
                commit_check=commit_check,
            )

    async def _load_playlist_current(
        self,
        guild_id: int,
        actor: MusicActor,
        name: str,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> tuple[int, int]:
        self._require_available()
        assert self.repository is not None and self.library is not None
        repository = self.repository
        library = self.library
        async with self._lock(guild_id):
            binding = self._binding(guild_id)
            self._require_same_channel(binding, actor)
            record = await asyncio.to_thread(repository.load, guild_id, actor.user_id, name)
            if record is None:
                raise PlaylistError("playlist was not found")
            snapshot = await _session(binding).snapshot()
            occupied = len(snapshot.upcoming) + int(snapshot.current is not None)
            if occupied + len(record.track_titles) > self.max_queue:
                raise MusicSessionError("playlist does not fit in the music queue")
            resolved: list[Track] = []
            missing = 0
            for title in record.track_titles:
                try:
                    track = await self._resolve_track_for_guild(
                        guild_id,
                        title,
                        actor.user_id,
                        library=library,
                        repository=repository,
                    )
                except (MediaLibraryError, PlaylistError):
                    missing += 1
                else:
                    resolved.append(track)
            if not resolved:
                raise PlaylistError("playlist tracks are not present in the local library")
            if self.library is not library or self.repository is not repository:
                raise MusicUnavailableError("music runtime identity changed")
            self._require_available()
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            self._require_same_channel(binding, fresh_actor)
            if self.library is not library or self.repository is not repository:
                raise MusicUnavailableError("music runtime identity changed")
            for track in resolved:
                self._require_track_rights(guild_id, track)
            try:
                await _session(binding).enqueue_many(tuple(resolved))
            except RequesterQueueLimitError as exc:
                raise MusicSessionError("requester music queue limit reached") from exc
            except QueueFullError as exc:
                raise MusicSessionError("music queue is full") from exc
            except TrackSourceAuthorizationError as exc:
                raise MusicAuthorizationError("track rights are no longer approved") from exc
            return len(resolved), missing

    async def grant_track_rights(
        self,
        guild_id: int,
        query: str,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> None:
        self._require_available()
        if not actor.manage_guild:
            raise MusicAuthorizationError("Manage Guild is required")
        assert self.library is not None and self.repository is not None
        async with self._lock(guild_id):
            try:
                track = await asyncio.to_thread(self.library.resolve_track, query, requester_id=actor.user_id)
                track_key, digest = await asyncio.to_thread(self.library.track_rights_identity, track)
            except MediaLibraryError as exc:
                raise MusicSessionError("track was not found") from exc
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            if not fresh_actor.manage_guild:
                raise MusicAuthorizationError("Manage Guild is required")
            self._require_available()
            await asyncio.to_thread(self.repository.grant_track_rights, guild_id, track_key, digest)

    async def revoke_track_rights(
        self,
        guild_id: int,
        query: str,
        actor: MusicActor,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> bool:
        self._require_available()
        if not actor.manage_guild:
            raise MusicAuthorizationError("Manage Guild is required")
        assert self.library is not None and self.repository is not None
        async with self._lock(guild_id):
            try:
                track = await asyncio.to_thread(self.library.resolve_track, query, requester_id=actor.user_id)
                track_key, _ = await asyncio.to_thread(self.library.track_rights_identity, track)
            except MediaLibraryError as exc:
                raise MusicSessionError("track was not found") from exc
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            if not fresh_actor.manage_guild:
                raise MusicAuthorizationError("Manage Guild is required")
            self._require_available()
            return await asyncio.to_thread(self.repository.revoke_track_rights, guild_id, track_key)

    async def delete_playlist(
        self,
        guild_id: int,
        actor: MusicActor,
        name: str,
        *,
        commit_check: MusicCommitCheck | None = None,
    ) -> bool:
        self._require_available()
        assert self.repository is not None
        async with self._lifecycle_lock:
            self._require_available()
            await self._require_fresh_actor(commit_check, actor)
            return await asyncio.to_thread(self.repository.delete, guild_id, actor.user_id, name)

    async def begin_close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closing = True
            self.available = False
            self.reason = "closing"
            radio_tasks = tuple(self._radio_fill_tasks.values())
            self._radio_fill_tasks.clear()
            self._radio_bindings.clear()
            for task in radio_tasks:
                task.cancel()
            if radio_tasks:
                await asyncio.gather(*radio_tasks, return_exceptions=True)
            bindings = tuple(self._sessions.items())
            await self._save_final_projections(bindings)

    async def close(self, *, delete_projections: bool = False) -> None:
        operation_error: BaseException | None = None
        async with self._lifecycle_lock:
            if self._closed and not delete_projections:
                return
            bindings = tuple(self._sessions.items())
            self._closing = True
            self.available = False
            self.reason = "closing"
            radio_tasks = tuple(self._radio_fill_tasks.values())
            self._radio_fill_tasks.clear()
            self._radio_bindings.clear()
            for task in radio_tasks:
                task.cancel()
            try:
                if radio_tasks:
                    await asyncio.gather(*radio_tasks, return_exceptions=True)
                if delete_projections:
                    await self._delete_all_audio_projections()
                else:
                    await self._save_final_projections(bindings)
            except BaseException as exc:
                operation_error = exc
            finally:
                self._closed = True
                self._closing = True
                self.available = False
                self.reason = "closed"
                sessions, self._sessions = tuple(self._sessions.values()), {}
                self._suspended_voice_channels.clear()
                self._voice_free_queue_requesters.clear()
                self._manual_enqueue_waiters.clear()
        if sessions:
            await asyncio.gather(
                *(_close_session_cancellation_safe(_session(binding)) for binding in sessions),
                return_exceptions=True,
            )
        if operation_error is not None:
            raise operation_error

    async def close_guild(self, guild_id: int) -> bool:
        if guild_id <= 0:
            return False
        operation_error: BaseException | None = None
        radio_task: asyncio.Task[None] | None = None
        async with self._lock(guild_id):
            binding = self._sessions.pop(guild_id, None)
            pending = self._pending_projections.pop(guild_id, None)
            self._suspended_voice_channels.pop(guild_id, None)
            self._voice_free_queue_requesters.pop(guild_id, None)
            self._radio_bindings.pop(guild_id, None)
            radio_task = self._radio_fill_tasks.pop(guild_id, None)
            if radio_task is not None:
                radio_task.cancel()
            try:
                deleted = await self._delete_audio_projection(
                    guild_id,
                    required=self.repository is not None,
                )
            except BaseException as exc:
                operation_error = exc
                deleted = False
        await _cancel_task(radio_task)
        if binding is not None:
            await _close_session_cancellation_safe(_session(binding))
        if operation_error is not None:
            raise operation_error
        self._notify_state_changed(guild_id)
        return binding is not None or pending is not None or deleted

    async def _resolve_pending_projection(
        self,
        projection: GuildAudioProjection,
        *,
        expected_session: GuildAudioSession,
        expected_library: LocalMediaLibrary,
        expected_repository: MusicPlaylistRepository,
    ) -> tuple[Track, ...]:
        tracks: list[Track] = []
        restore_limit = min(_MAX_DURABLE_TRACKS, self.max_queue)
        if len(projection.tracks) > restore_limit:
            logger.warning(
                "music_projection_restore_failed",
                extra={
                    "reason": "queue_limit",
                    "skipped_count": len(projection.tracks) - restore_limit,
                },
            )
            raise MusicUnavailableError("audio projection exceeds current queue limit")
        for persisted in projection.tracks[:restore_limit]:
            self._require_runtime_identity(
                projection.guild_id,
                expected_session=expected_session,
                expected_library=expected_library,
                expected_repository=expected_repository,
                allow_closing=False,
            )
            try:
                track = await asyncio.to_thread(
                    expected_library.resolve_persisted_track,
                    persisted.library_ref,
                    persisted.content_sha256,
                    persisted.requester_id,
                    persisted.retry_count,
                )
            except MediaLibraryError as exc:
                logger.warning(
                    "music_projection_track_skipped",
                    extra={"reason": _persisted_resolution_reason(exc)},
                )
                continue
            self._require_runtime_identity(
                projection.guild_id,
                expected_session=expected_session,
                expected_library=expected_library,
                expected_repository=expected_repository,
                allow_closing=False,
            )
            try:
                rights_allowed = await asyncio.to_thread(
                    expected_repository.track_rights_allowed,
                    projection.guild_id,
                    persisted.library_ref,
                    persisted.content_sha256,
                )
            except Exception as exc:
                logger.warning(
                    "music_projection_restore_failed",
                    extra={"reason": "rights_check_failed", "error_type": type(exc).__name__},
                )
                raise MusicUnavailableError("audio projection could not be restored") from None
            self._require_runtime_identity(
                projection.guild_id,
                expected_session=expected_session,
                expected_library=expected_library,
                expected_repository=expected_repository,
                allow_closing=False,
            )
            if rights_allowed is not True:
                logger.warning(
                    "music_projection_track_skipped",
                    extra={"reason": "rights_revoked"},
                )
                continue
            tracks.append(track)
        return tuple(tracks)

    async def _save_session_projection(
        self,
        guild_id: int,
        *,
        expected_session: GuildAudioSession,
        expected_library: LocalMediaLibrary,
        expected_repository: MusicPlaylistRepository,
        revision: int,
        snapshot: QueueSnapshot,
        allow_closing: bool = False,
    ) -> bool:
        try:
            async with self._projection_lock(guild_id):
                self._require_runtime_identity(
                    guild_id,
                    expected_session=expected_session,
                    expected_library=expected_library,
                    expected_repository=expected_repository,
                    allow_closing=allow_closing,
                )
                if snapshot.revision != revision or revision <= self._projection_revisions.get(guild_id, -1):
                    return False
                persisted: list[PersistedMusicTrackRef] = []
                ordered = (((snapshot.current,) if snapshot.current is not None else ()) + snapshot.upcoming)[
                    :_MAX_DURABLE_TRACKS
                ]
                for track in ordered:
                    try:
                        sealed = await asyncio.to_thread(expected_library.seal_track, track)
                    except MediaLibraryError as exc:
                        logger.warning(
                            "music_projection_track_skipped",
                            extra={"reason": _persisted_resolution_reason(exc)},
                        )
                        continue
                    self._require_runtime_identity(
                        guild_id,
                        expected_session=expected_session,
                        expected_library=expected_library,
                        expected_repository=expected_repository,
                        allow_closing=allow_closing,
                    )
                    if sealed.library_ref is None or sealed.content_sha256 is None:
                        continue
                    persisted.append(
                        PersistedMusicTrackRef(
                            library_ref=sealed.library_ref,
                            content_sha256=sealed.content_sha256,
                            requester_id=sealed.requester_id,
                            retry_count=sealed.retry_count,
                        )
                    )
                projection = GuildAudioProjection(
                    guild_id=guild_id,
                    tracks=tuple(persisted),
                    loop_mode=snapshot.loop_mode,
                    paused=snapshot.paused,
                    music_volume=snapshot.volume,
                    speech_volume=snapshot.speech_volume,
                )
                self._require_runtime_identity(
                    guild_id,
                    expected_session=expected_session,
                    expected_library=expected_library,
                    expected_repository=expected_repository,
                    allow_closing=allow_closing,
                )
                await asyncio.to_thread(expected_repository.save_audio_projection, projection)
                self._require_runtime_identity(
                    guild_id,
                    expected_session=expected_session,
                    expected_library=expected_library,
                    expected_repository=expected_repository,
                    allow_closing=allow_closing,
                )
                self._projection_revisions[guild_id] = revision
                return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "music_projection_save_failed",
                extra={"error_type": type(exc).__name__},
            )
            return False

    async def _save_final_projections(
        self,
        bindings: Sequence[tuple[int, SessionBinding]],
    ) -> None:
        expected_library = self.library
        expected_repository = self.repository
        if expected_library is None or expected_repository is None:
            return
        operation_error: BaseException | None = None
        for guild_id, binding in bindings:
            try:
                async with self._lock(guild_id):
                    if self._sessions.get(guild_id) is not binding or guild_id in self._pending_projections:
                        continue
                    session = _session(binding)
                    snapshot = await session.freeze_projection()
                    saved = await self._save_session_projection(
                        guild_id,
                        expected_session=session,
                        expected_library=expected_library,
                        expected_repository=expected_repository,
                        revision=snapshot.revision,
                        snapshot=snapshot,
                        allow_closing=True,
                    )
                    if not saved and self._projection_revisions.get(guild_id, -1) < snapshot.revision:
                        raise MusicSessionError("audio projection could not be saved")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "music_projection_final_save_failed",
                    extra={"error_type": type(exc).__name__},
                )
                if operation_error is None:
                    operation_error = exc
        if operation_error is not None:
            raise MusicSessionError("audio projections could not be saved") from None

    async def _delete_audio_projection(self, guild_id: int, *, required: bool = False) -> bool:
        repository = self.repository
        if repository is None:
            if required:
                raise MusicSessionError("audio projection could not be deleted")
            return False
        try:
            async with self._projection_lock(guild_id):
                if self.repository is not repository:
                    raise RuntimeError("repository identity changed")
                deleted = await asyncio.to_thread(repository.delete_audio_projection, guild_id)
                if self.repository is not repository:
                    raise RuntimeError("repository identity changed")
                self._projection_revisions.pop(guild_id, None)
                return deleted
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "music_projection_delete_failed",
                extra={"error_type": type(exc).__name__},
            )
            if required:
                raise MusicSessionError("audio projection could not be deleted") from exc
            return False

    async def _delete_all_audio_projections(self) -> None:
        repository = self.repository
        persisted = ()
        if repository is not None:
            persisted = await asyncio.to_thread(repository.list_audio_projections)
        guild_ids = tuple(
            sorted(
                set(self._pending_projections)
                | set(self._sessions)
                | set(self._projection_revisions)
                | {projection.guild_id for projection in persisted}
            )
        )
        operation_error: BaseException | None = None
        for guild_id in guild_ids:
            try:
                await self._delete_audio_projection(guild_id, required=True)
                self._pending_projections.pop(guild_id, None)
                self._suspended_voice_channels.pop(guild_id, None)
                self._voice_free_queue_requesters.pop(guild_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if operation_error is None:
                    operation_error = exc
        if operation_error is not None:
            raise operation_error

    def _require_runtime_identity(
        self,
        guild_id: int,
        *,
        expected_session: GuildAudioSession,
        expected_library: LocalMediaLibrary,
        expected_repository: MusicPlaylistRepository,
        allow_closing: bool,
    ) -> None:
        binding = self._sessions.get(guild_id)
        if (
            self._closed
            or (self._closing and not allow_closing)
            or self.library is not expected_library
            or self.repository is not expected_repository
            or binding is None
            or binding.session is not expected_session
        ):
            raise MusicUnavailableError("music runtime identity changed")

    async def _control(
        self,
        guild_id: int,
        actor: MusicActor,
        action: str,
        *,
        value: Any = None,
        commit_check: MusicCommitCheck | None = None,
    ) -> Any:
        self._require_available()
        async with self._lock(guild_id):
            binding = self._binding(guild_id)
            self._require_same_channel(binding, actor)
            session = _session(binding)
            snapshot = await session.snapshot()
            self._require_control(binding, snapshot, actor)
            self._require_available()
            fresh_actor = await self._require_fresh_actor(commit_check, actor)
            self._require_same_channel(binding, fresh_actor)
            self._require_control(binding, snapshot, fresh_actor)
            self._require_available()
            try:
                if action == "pause":
                    return await session.pause(expected_revision=snapshot.revision)
                if action == "resume":
                    return await session.resume(expected_revision=snapshot.revision)
                if action == "skip":
                    return await session.skip(expected_revision=snapshot.revision)
                if action == "stop":
                    self._radio_bindings.pop(guild_id, None)
                    radio_task = self._radio_fill_tasks.pop(guild_id, None)
                    if radio_task is not None:
                        radio_task.cancel()
                    return await session.stop_music(expected_revision=snapshot.revision)
                if action == "shuffle":
                    return await session.shuffle(expected_revision=snapshot.revision)
                if action == "loop":
                    return await session.set_loop_mode(value, expected_revision=snapshot.revision)
                if action == "volume":
                    return await session.set_volume(float(value), expected_revision=snapshot.revision)
                if action == "speech-volume":
                    return await session.set_speech_volume(float(value), expected_revision=snapshot.revision)
            except (PlayerStateError, QueueFullError, ValueError, TypeError) as exc:
                raise MusicSessionError("music operation is not available") from exc
            raise ValueError("unknown music control action")

    def _binding(self, guild_id: int) -> SessionBinding:
        if guild_id <= 0:
            raise MusicSessionError("guild is required")
        binding = self._sessions.get(guild_id)
        if binding is None:
            raise MusicSessionError("music is not connected")
        return binding

    @staticmethod
    def _require_same_channel(binding: SessionBinding, actor: MusicActor) -> None:
        if actor.voice_channel_id is None or actor.voice_channel_id != binding.voice_channel_id:
            raise MusicAuthorizationError("same voice channel is required")

    @staticmethod
    def _require_control(binding: SessionBinding, snapshot: QueueSnapshot, actor: MusicActor) -> None:
        if actor.manage_guild:
            return
        track = control_track(snapshot, binding)
        requester_id = track.requester_id if track is not None else binding.joined_by_id
        if requester_id != actor.user_id:
            raise MusicAuthorizationError("only the requester or Manage Guild can control playback")

    def _require_available(self) -> None:
        if self._closed or self._closing or not self.available:
            raise MusicUnavailableError(self.reason)

    def _track_rights_allowed(self, guild_id: int, track: Track) -> bool:
        if self._closed or not self.available or self.library is None or self.repository is None:
            return False
        try:
            track_key, digest = self.library.track_rights_identity(track)
            return self.repository.track_rights_allowed(guild_id, track_key, digest)
        except (MediaLibraryError, PlaylistError, RuntimeError, ValueError):
            return False

    def _require_track_rights(self, guild_id: int, track: Track) -> None:
        if not self._track_rights_allowed(guild_id, track):
            raise MusicAuthorizationError("track rights are not approved")

    @staticmethod
    async def _require_fresh_actor(
        commit_check: MusicCommitCheck | None,
        actor: MusicActor,
    ) -> MusicActor:
        if commit_check is None:
            return actor
        try:
            result = commit_check()
            if isinstance(result, Awaitable):
                result = await result
        except Exception:
            result = None
        if isinstance(result, MusicActor):
            if result.user_id == actor.user_id:
                return result
            result = None
        if result is True:
            return actor
        if result is not True:
            raise MusicAuthorizationError("capability policy changed")
        return actor

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def _notify_state_changed(self, guild_id: int) -> None:
        observer = self._state_observer
        if observer is None:
            return
        try:
            observer(guild_id)
        except Exception as exc:
            logger.warning(
                "music_state_observer_failed",
                extra={"error_type": type(exc).__name__},
            )

    @asynccontextmanager
    async def _manual_enqueue_scope(self, guild_id: int) -> AsyncIterator[None]:
        self._manual_enqueue_waiters[guild_id] = self._manual_enqueue_waiters.get(guild_id, 0) + 1
        try:
            yield
        finally:
            remaining = self._manual_enqueue_waiters.get(guild_id, 1) - 1
            if remaining > 0:
                self._manual_enqueue_waiters[guild_id] = remaining
            else:
                self._manual_enqueue_waiters.pop(guild_id, None)
                await self._resume_radio_after_manual(guild_id)

    async def _resume_radio_after_manual(self, guild_id: int) -> None:
        if self._closed or self._closing or not self.available:
            return
        async with self._lock(guild_id):
            radio = self._radio_bindings.get(guild_id)
            binding = self._sessions.get(guild_id)
            if (
                radio is None
                or binding is None
                or binding.session is not radio.session
                or self._manual_enqueue_waiters.get(guild_id, 0) > 0
            ):
                return
            snapshot = await radio.session.snapshot()
            self._schedule_radio_fill(guild_id, radio.session, snapshot)

    def _projection_lock(self, guild_id: int) -> asyncio.Lock:
        return self._projection_locks.setdefault(guild_id, asyncio.Lock())


def _session(binding: SessionBinding) -> GuildAudioSession:
    session = binding.session
    if not isinstance(session, GuildAudioSession):
        raise TypeError("invalid guild audio session")
    return session


async def _close_session_cancellation_safe(session: GuildAudioSession) -> None:
    close_task = asyncio.create_task(session.close())
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        try:
            await close_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "audio_session_close_failed_after_cancellation",
                extra={"error_type": type(exc).__name__},
            )
        raise


async def _run_thread_operation(
    operation: Callable[..., _ThreadResult],
    *arguments: object,
    **keyword_arguments: object,
) -> tuple[_ThreadResult, asyncio.CancelledError | None]:
    """Wait for a started thread operation before propagating cancellation."""

    task = asyncio.create_task(asyncio.to_thread(operation, *arguments, **keyword_arguments))
    deferred_cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), deferred_cancellation
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            if deferred_cancellation is None:
                deferred_cancellation = exc
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()


def _require_runtime_current(current: MusicRuntimeCurrent | None) -> None:
    if not _runtime_is_current(current):
        raise MusicUnavailableError("music import runtime identity changed")


def _runtime_is_current(current: MusicRuntimeCurrent | None) -> bool:
    if current is None:
        return True
    try:
        return current() is True
    except Exception:
        return False


async def _cancel_task(task: asyncio.Task[Any] | None) -> None:
    if task is None or task is asyncio.current_task():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _discard_queued_track_cancellation_safe(
    session: GuildAudioSession,
    track_id: str,
) -> None:
    discard_task = asyncio.create_task(session.remove(1, expected_track_id=track_id))
    while True:
        try:
            await asyncio.shield(discard_task)
            return
        except asyncio.CancelledError:
            if discard_task.cancelled():
                return
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
        except (IndexError, PlayerStateError):
            return
        except Exception as exc:
            logger.warning(
                "music_radio_cancelled_candidate_discard_failed",
                extra={"error_type": type(exc).__name__},
            )
            return


def queue_titles(snapshot: QueueSnapshot, *, limit: int = 20) -> tuple[str, ...]:
    if limit < 1:
        raise ValueError("limit must be positive")
    return tuple(track.title for track in snapshot.upcoming[:limit])


def playlist_titles(records: Sequence[PlaylistRecord], *, limit: int = 50) -> tuple[str, ...]:
    if limit < 1:
        raise ValueError("limit must be positive")
    return tuple(record.name for record in records[:limit])


def _pending_projection_map(
    projections: Sequence[GuildAudioProjection],
) -> dict[int, GuildAudioProjection]:
    values: dict[int, GuildAudioProjection] = {}
    for projection in projections:
        if not isinstance(projection, GuildAudioProjection):
            raise TypeError("pending projections must contain GuildAudioProjection values")
        if projection.guild_id in values:
            raise ValueError("pending projections contain a duplicate guild")
        values[projection.guild_id] = projection
    return values


def _persisted_resolution_reason(exc: MediaLibraryError) -> str:
    reason = getattr(exc, "reason", None)
    value = getattr(reason, "value", reason)
    if value in {"library_ref_unavailable", "content_changed"}:
        return str(value)
    return "library_ref_unavailable"
