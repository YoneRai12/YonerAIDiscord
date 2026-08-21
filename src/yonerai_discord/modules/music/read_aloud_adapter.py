from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from yonerai_discord.capabilities import EVENT_CAPABILITIES
from yonerai_discord.modules.scheduling.focus import (
    FOCUS_TIMER_COMPLETION_MESSAGE,
    FocusDeliveryDisposition,
    FocusReadAloudOverlay,
    FocusReadAloudOverlayStore,
    FocusTimerBinding,
    FocusTimerCurrentState,
)
from yonerai_discord.modules.voice.models import SpeechRequest, SynthesizedSpeech
from yonerai_discord.modules.voice.presets import (
    ResolvedVoicePreset,
    SqliteVoicePresetRepository,
)
from yonerai_discord.modules.voice.read_aloud import (
    ReadAloudBurstCoordinator,
    ReadAloudPolicySnapshot,
    ReadAloudRejectedError,
    ReadAloudRoute,
    SqliteReadAloudRouteRepository,
)
from yonerai_discord.voice_contract import VOICEVOX_SPEAKER_ID

from .authorization import build_music_commit_check
from .models import MusicActor
from .service import MusicService


_READ_ALOUD_EVENT = "music_read_aloud_message"
_READ_ALOUD_CAPABILITY = EVENT_CAPABILITIES[_READ_ALOUD_EVENT]
_DUCKING_CAPABILITY = "cap-run-audio-ducking-core"
_SPEAK_COMMAND = "music speak"

RuntimeCurrent = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class _ReadAloudRouteAuthority:
    route: ReadAloudRoute | None
    focus_store: FocusReadAloudOverlayStore | None = None
    focus_overlay: FocusReadAloudOverlay | None = None
    focus_runtime: Any | None = None

    def __post_init__(self) -> None:
        present = (
            self.focus_store is not None,
            self.focus_overlay is not None,
            self.focus_runtime is not None,
        )
        if any(present) and not all(present):
            raise ValueError("focus route authority must be complete")


class MusicReadAloudAdapter:
    """明示routeの短文だけを既存VOICEVOX・music ducking経路へ渡す。"""

    def __init__(
        self,
        bot: Any,
        service: MusicService,
        repository: SqliteReadAloudRouteRepository,
        preset_repository: SqliteVoicePresetRepository,
        speech_queue: Any,
        *,
        runtime_current: RuntimeCurrent,
        merge_window_seconds: float = 0.25,
    ) -> None:
        if not callable(runtime_current):
            raise TypeError("runtime_current must be callable")
        self._bot = bot
        self._service = service
        self._repository = repository
        self._preset_repository = preset_repository
        self._speech_queue = speech_queue
        self._guard = getattr(bot, "capability_guard", None)
        self._runtime_current = runtime_current
        self._closing = False
        self._session_leases: dict[tuple[int, int], tuple[ReadAloudRoute, object]] = {}
        self._route_authorities: dict[ReadAloudRoute, _ReadAloudRouteAuthority] = {}
        self._coordinator = ReadAloudBurstCoordinator(
            synthesize=self._synthesize,
            deliver=self._deliver,
            route_current=self._route_current,
            synthesize_policy_current=self._synthesize_with_policy,
            deliver_policy_current=self._deliver_with_policy,
            policy_current=self._policy_current,
            synthesize_preset_current=self._synthesize_with_preset,
            deliver_preset_current=self._deliver_with_preset,
            preset_current=self._preset_current,
            batch_finished=self._batch_finished,
            merge_window_seconds=merge_window_seconds,
        )

    @property
    def available(self) -> bool:
        return self._runtime_is_current()

    async def speak(
        self,
        binding: FocusTimerBinding,
        *,
        message_code: str,
        idempotency_key: str,
    ) -> FocusDeliveryDisposition:
        if (
            not isinstance(binding, FocusTimerBinding)
            or message_code != "focus_timer.completed"
            or not _sha256_hex(idempotency_key)
            or not await self._focus_binding_current(binding)
        ):
            raise RuntimeError("focus_voice_delivery_rejected")
        route = ReadAloudRoute(
            guild_id=binding.guild_id,
            source_channel_id=binding.source_channel_id,
            destination_voice_channel_id=binding.destination_channel_id,
            enabled=True,
            revision=binding.revision,
        )
        guild = self._current_guild(binding.guild_id)
        if guild is None:
            raise RuntimeError("focus_voice_delivery_revoked")
        actors = await self._fresh_actors_for_scope(
            guild,
            guild_id=binding.guild_id,
            author_ids=(binding.owner_id,),
            route=route,
            claim_session=False,
        )
        if actors is None:
            raise RuntimeError("focus_voice_delivery_revoked")
        preset = await asyncio.to_thread(
            self._read_preset,
            binding.guild_id,
            binding.owner_id,
        )
        if not await self._preset_current(
            route, (binding.owner_id,), (preset,)
        ) or not await self._focus_binding_current(binding):
            raise RuntimeError("focus_voice_delivery_revoked")
        request = SpeechRequest(
            text=FOCUS_TIMER_COMPLETION_MESSAGE,
            guild_id=binding.guild_id,
            channel_id=binding.source_channel_id,
            speaker_id=VOICEVOX_SPEAKER_ID,
            speed_scale=preset.values.speed_scale,
            volume_scale=preset.values.volume_scale,
        )

        async def synthesis_current() -> bool:
            return bool(
                await self._focus_binding_current(binding)
                and await self._preset_current(route, (binding.owner_id,), (preset,))
                and await self._fresh_actors_for_scope(
                    guild,
                    guild_id=binding.guild_id,
                    author_ids=(binding.owner_id,),
                    route=route,
                    claim_session=False,
                )
                is not None
            )

        queue = self._speech_queue
        if not self._queue_is_current(queue):
            raise RuntimeError("focus_voice_delivery_unavailable")
        speech = await queue.synthesize(request, current_policy=synthesis_current)
        if not isinstance(speech, SynthesizedSpeech) or not await synthesis_current():
            raise RuntimeError("focus_voice_delivery_revoked")
        actor = actors[0]

        async def commit_check() -> MusicActor | None:
            current = await self._fresh_actors_for_scope(
                guild,
                guild_id=binding.guild_id,
                author_ids=(binding.owner_id,),
                route=route,
                claim_session=False,
            )
            if (
                current is None
                or not await self._focus_binding_current(binding)
                or not await self._preset_current(route, (binding.owner_id,), (preset,))
            ):
                return None
            return current[0]

        await self._service.add_speech_wav(
            binding.guild_id,
            actor,
            speech.wav,
            commit_check=commit_check,
        )
        return FocusDeliveryDisposition.DELIVERED

    def receipts(self) -> tuple[object, ...]:
        return self._coordinator.receipts()

    async def wait_idle(self) -> None:
        await self._coordinator.wait_idle()

    async def on_message(self, message: Any) -> None:
        await self.handle_message(message)

    async def handle_message(self, message: Any) -> bool:
        if not self._runtime_is_current():
            return False
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        guild_id = _positive_id(guild)
        channel_id = _positive_id(channel)
        author_id = _positive_id(author)
        message_id = _positive_id(message)
        author_is_bot = bool(getattr(author, "bot", False))
        is_webhook = getattr(message, "webhook_id", None) is not None
        content = getattr(message, "content", None)
        if (
            guild_id is None
            or channel_id is None
            or author_id is None
            or message_id is None
            or not isinstance(content, str)
            or author_is_bot
            or is_webhook
        ):
            return False
        if not self._event_allowed(
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            author_id=author_id,
            author_is_bot=author_is_bot,
        ):
            return False
        current_guild = self._current_guild(guild_id)
        if current_guild is None:
            return False
        initial = await self._fresh_actors_for_scope(
            current_guild,
            guild_id=guild_id,
            author_ids=(author_id,),
            route=None,
        )
        if initial is None:
            return False
        try:
            authority, policy, preset = await asyncio.to_thread(
                self._read_route_policy_and_preset,
                guild_id,
                channel_id,
                author_id,
            )
        except Exception:
            return False
        route = authority.route
        if not isinstance(route, ReadAloudRoute) or not route.enabled:
            return False
        if not self._route_authority_current(authority):
            return False
        if not await self._route_authority_authorized(authority):
            return False
        if not isinstance(policy, ReadAloudPolicySnapshot) or policy.guild_id != guild_id:
            return False
        if not isinstance(preset, ResolvedVoicePreset) or preset.guild_id != guild_id or preset.user_id != author_id:
            return False
        self._route_authorities[route] = authority
        actors = await self._fresh_actors(route, (author_id,), claim_session=False)
        if actors is None:
            self._route_authorities.pop(route, None)
            return False
        try:
            current_policy, current_preset = await asyncio.to_thread(
                self._read_policy_and_preset,
                guild_id,
                author_id,
            )
        except Exception:
            self._route_authorities.pop(route, None)
            return False
        if current_policy != policy or current_preset != preset or not self._runtime_is_current():
            self._route_authorities.pop(route, None)
            return False
        if not self._route_authority_current(authority):
            self._route_authorities.pop(route, None)
            return False
        try:
            await self._coordinator.submit(
                route,
                author_id=author_id,
                text=content,
                author_is_current_voice_member=(actors[0].voice_channel_id == route.destination_voice_channel_id),
                author_is_bot=author_is_bot,
                is_webhook=is_webhook,
                policy=policy,
                preset=preset,
            )
        except (ReadAloudRejectedError, TypeError, ValueError):
            self._route_authorities.pop(route, None)
            return False
        return True

    def _read_route_policy_and_preset(
        self,
        guild_id: int,
        channel_id: int,
        author_id: int,
    ) -> tuple[_ReadAloudRouteAuthority, ReadAloudPolicySnapshot, ResolvedVoicePreset]:
        return (
            self._read_route_authority(guild_id, channel_id),
            self._read_policy(guild_id),
            self._read_preset(guild_id, author_id),
        )

    def _read_route_authority(
        self,
        guild_id: int,
        channel_id: int,
    ) -> _ReadAloudRouteAuthority:
        focus_store = getattr(self._bot, "scheduling_focus_overlay_store", None)
        if focus_store is not None:
            plugin = getattr(self._bot, "scheduling_plugin", None)
            runtime = getattr(self._bot, "scheduling_focus_timer_runtime", None)
            if (
                getattr(plugin, "focus_overlay_store", None) is not focus_store
                or getattr(plugin, "focus_runtime", None) is not runtime
                or getattr(runtime, "overlays", None) is not focus_store
                or not callable(getattr(runtime, "authorization_current", None))
                or bool(getattr(plugin, "closing", True))
                or getattr(focus_store, "is_open", False) is not True
            ):
                raise RuntimeError("focus_route_authority_unavailable")
            resolver = getattr(focus_store, "active_for_source", None)
            if not callable(resolver):
                raise RuntimeError("focus_route_resolver_unavailable")
            overlay = resolver(guild_id, channel_id)
            if overlay is not None:
                if (
                    not isinstance(overlay, FocusReadAloudOverlay)
                    or overlay.binding.guild_id != guild_id
                    or overlay.binding.source_channel_id != channel_id
                ):
                    raise RuntimeError("focus_route_scope_invalid")
                route = ReadAloudRoute(
                    guild_id=guild_id,
                    source_channel_id=channel_id,
                    destination_voice_channel_id=overlay.binding.destination_channel_id,
                    enabled=True,
                    revision=_focus_route_revision(overlay, focus_store),
                )
                return _ReadAloudRouteAuthority(
                    route=route,
                    focus_store=focus_store,
                    focus_overlay=overlay,
                    focus_runtime=runtime,
                )
        return _ReadAloudRouteAuthority(route=self._repository.get(guild_id, channel_id))

    def _route_authority_current(self, authority: _ReadAloudRouteAuthority) -> bool:
        route = authority.route
        if not isinstance(route, ReadAloudRoute) or not self._runtime_is_current():
            return False
        if authority.focus_store is None:
            try:
                return self._repository.get(route.guild_id, route.source_channel_id) == route
            except Exception:
                return False
        store = authority.focus_store
        overlay = authority.focus_overlay
        runtime = authority.focus_runtime
        plugin = getattr(self._bot, "scheduling_plugin", None)
        if (
            not isinstance(overlay, FocusReadAloudOverlay)
            or getattr(self._bot, "scheduling_focus_overlay_store", None) is not store
            or getattr(self._bot, "scheduling_focus_timer_runtime", None) is not runtime
            or getattr(plugin, "focus_overlay_store", None) is not store
            or getattr(plugin, "focus_runtime", None) is not runtime
            or getattr(runtime, "overlays", None) is not store
            or not callable(getattr(runtime, "authorization_current", None))
            or bool(getattr(plugin, "closing", True))
            or getattr(store, "is_open", False) is not True
        ):
            return False
        try:
            current = store.active_for_source(route.guild_id, route.source_channel_id)
        except Exception:
            return False
        return (
            current == overlay
            and route.destination_voice_channel_id == overlay.binding.destination_channel_id
            and route.revision == _focus_route_revision(overlay, store)
        )

    async def _route_authority_authorized(
        self,
        authority: _ReadAloudRouteAuthority,
    ) -> bool:
        if authority.focus_runtime is None:
            return self._route_authority_current(authority)
        overlay = authority.focus_overlay
        current = getattr(authority.focus_runtime, "authorization_current", None)
        if not isinstance(overlay, FocusReadAloudOverlay) or not callable(current):
            return False
        try:
            state = await current(overlay.binding)
        except Exception:
            return False
        return bool(
            isinstance(state, FocusTimerCurrentState)
            and state.binding == overlay.binding
            and state.authorized is True
            and state.closing is False
            and self._route_authority_current(authority)
        )

    async def _focus_binding_current(self, binding: FocusTimerBinding) -> bool:
        runtime = getattr(self._bot, "scheduling_focus_timer_runtime", None)
        plugin = getattr(self._bot, "scheduling_plugin", None)
        current = getattr(runtime, "authorization_current", None)
        if (
            getattr(plugin, "focus_runtime", None) is not runtime
            or bool(getattr(plugin, "closing", True))
            or not callable(current)
        ):
            return False
        try:
            state = await current(binding)
        except Exception:
            return False
        return bool(
            isinstance(state, FocusTimerCurrentState)
            and state.binding == binding
            and state.authorized is True
            and state.closing is False
            and getattr(self._bot, "scheduling_focus_timer_runtime", None) is runtime
            and getattr(plugin, "focus_runtime", None) is runtime
            and self._runtime_is_current()
        )

    def _read_policy_and_preset(
        self,
        guild_id: int,
        author_id: int,
    ) -> tuple[ReadAloudPolicySnapshot, ResolvedVoicePreset]:
        return self._read_policy(guild_id), self._read_preset(guild_id, author_id)

    def _read_policy(self, guild_id: int) -> ReadAloudPolicySnapshot:
        getter = getattr(self._repository, "get_policy", None)
        if not callable(getter):
            raise RuntimeError("read_aloud_policy_unavailable")
        policy = getter(guild_id)
        if not isinstance(policy, ReadAloudPolicySnapshot) or policy.guild_id != guild_id:
            raise RuntimeError("read_aloud_policy_invalid")
        return policy

    def _read_preset(self, guild_id: int, author_id: int) -> ResolvedVoicePreset:
        preset = self._preset_repository.resolve(guild_id, author_id)
        if not isinstance(preset, ResolvedVoicePreset) or preset.guild_id != guild_id or preset.user_id != author_id:
            raise RuntimeError("read_aloud_preset_invalid")
        return preset

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._session_leases.clear()
        self._route_authorities.clear()
        await self._coordinator.close()

    async def _route_current(
        self,
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
    ) -> bool:
        return await self._fresh_actors(route, author_ids) is not None

    async def _policy_current(
        self,
        route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
        policy: ReadAloudPolicySnapshot,
    ) -> bool:
        if (
            not isinstance(route, ReadAloudRoute)
            or not isinstance(policy, ReadAloudPolicySnapshot)
            or route.guild_id != policy.guild_id
            or not self._runtime_is_current()
        ):
            return False
        try:
            current = await asyncio.to_thread(self._read_policy, route.guild_id)
        except Exception:
            return False
        return current == policy and self._runtime_is_current()

    async def _preset_current(
        self,
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
        presets: tuple[ResolvedVoicePreset, ...],
    ) -> bool:
        if (
            not isinstance(route, ReadAloudRoute)
            or not author_ids
            or not presets
            or {preset.user_id for preset in presets} != set(author_ids)
            or any(preset.guild_id != route.guild_id for preset in presets)
            or not self._runtime_is_current()
        ):
            return False
        try:
            current = await asyncio.gather(
                *(
                    asyncio.to_thread(
                        self._read_preset,
                        route.guild_id,
                        preset.user_id,
                    )
                    for preset in presets
                )
            )
        except Exception:
            return False
        return tuple(current) == presets and self._runtime_is_current()

    async def _synthesize(
        self,
        request: SpeechRequest,
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
    ) -> SynthesizedSpeech:
        if await self._fresh_actors(route, author_ids) is None:
            raise RuntimeError("read_aloud_revoked")

        async def current_policy() -> bool:
            return await self._fresh_actors(route, author_ids) is not None

        queue = self._speech_queue
        if not self._queue_is_current(queue):
            raise RuntimeError("speech_queue_unavailable")
        return await queue.synthesize(request, current_policy=current_policy)

    async def _synthesize_with_policy(
        self,
        request: SpeechRequest,
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
        policy: ReadAloudPolicySnapshot,
    ) -> SynthesizedSpeech:
        if await self._fresh_actors(route, author_ids) is None:
            raise RuntimeError("read_aloud_revoked")
        if not await self._policy_current(route, author_ids, policy):
            raise RuntimeError("read_aloud_policy_revoked")

        async def current_policy() -> bool:
            return bool(
                await self._fresh_actors(route, author_ids) is not None
                and await self._policy_current(route, author_ids, policy)
            )

        queue = self._speech_queue
        if not self._queue_is_current(queue):
            raise RuntimeError("speech_queue_unavailable")
        return await queue.synthesize(request, current_policy=current_policy)

    async def _synthesize_with_preset(
        self,
        request: SpeechRequest,
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
        policy: ReadAloudPolicySnapshot,
        presets: tuple[ResolvedVoicePreset, ...],
    ) -> SynthesizedSpeech:
        if await self._fresh_actors(route, author_ids) is None:
            raise RuntimeError("read_aloud_revoked")
        if not await self._policy_current(route, author_ids, policy):
            raise RuntimeError("read_aloud_policy_revoked")
        if not await self._preset_current(route, author_ids, presets):
            raise RuntimeError("read_aloud_preset_revoked")

        async def current_policy() -> bool:
            return bool(
                await self._fresh_actors(route, author_ids) is not None
                and await self._policy_current(route, author_ids, policy)
                and await self._preset_current(route, author_ids, presets)
            )

        queue = self._speech_queue
        if not self._queue_is_current(queue):
            raise RuntimeError("speech_queue_unavailable")
        return await queue.synthesize(request, current_policy=current_policy)

    async def _deliver(
        self,
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
        wav: bytes,
    ) -> None:
        actors = await self._fresh_actors(route, author_ids)
        if actors is None:
            raise RuntimeError("read_aloud_revoked")
        actor = actors[0]

        async def commit_check() -> MusicActor | None:
            current = await self._fresh_actors(route, author_ids)
            if current is None:
                return None
            return current[0]

        await self._service.add_speech_wav(
            route.guild_id,
            actor,
            wav,
            commit_check=commit_check,
        )

    async def _deliver_with_policy(
        self,
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
        wav: bytes,
        policy: ReadAloudPolicySnapshot,
    ) -> None:
        actors = await self._fresh_actors(route, author_ids)
        if actors is None or not await self._policy_current(route, author_ids, policy):
            raise RuntimeError("read_aloud_revoked")
        actor = actors[0]

        async def commit_check() -> MusicActor | None:
            current = await self._fresh_actors(route, author_ids)
            if current is None or not await self._policy_current(route, author_ids, policy):
                return None
            return current[0]

        await self._service.add_speech_wav(
            route.guild_id,
            actor,
            wav,
            commit_check=commit_check,
        )

    async def _deliver_with_preset(
        self,
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
        wav: bytes,
        policy: ReadAloudPolicySnapshot,
        presets: tuple[ResolvedVoicePreset, ...],
    ) -> None:
        actors = await self._fresh_actors(route, author_ids)
        if (
            actors is None
            or not await self._policy_current(route, author_ids, policy)
            or not await self._preset_current(route, author_ids, presets)
        ):
            raise RuntimeError("read_aloud_revoked")
        actor = actors[0]

        async def commit_check() -> MusicActor | None:
            current = await self._fresh_actors(route, author_ids)
            if (
                current is None
                or not await self._policy_current(route, author_ids, policy)
                or not await self._preset_current(route, author_ids, presets)
            ):
                return None
            return current[0]

        await self._service.add_speech_wav(
            route.guild_id,
            actor,
            wav,
            commit_check=commit_check,
        )

    async def _fresh_actors(
        self,
        route: ReadAloudRoute,
        author_ids: tuple[int, ...],
        *,
        claim_session: bool = True,
    ) -> tuple[MusicActor, ...] | None:
        if not isinstance(route, ReadAloudRoute) or not author_ids:
            return None
        guild = self._current_guild(route.guild_id)
        if guild is None or not self._route_channels_current(guild, route):
            return None
        actors = await self._fresh_actors_for_scope(
            guild,
            guild_id=route.guild_id,
            author_ids=author_ids,
            route=route,
            claim_session=claim_session,
        )
        if actors is None:
            return None
        authority = self._route_authorities.get(route)
        if (
            authority is None
            or not self._route_authority_current(authority)
            or not await self._route_authority_authorized(authority)
        ):
            return None
        if not self._session_current(route, claim=claim_session):
            return None
        return actors

    async def _fresh_actors_for_scope(
        self,
        guild: Any,
        *,
        guild_id: int,
        author_ids: tuple[int, ...],
        route: ReadAloudRoute | None,
        claim_session: bool = True,
    ) -> tuple[MusicActor, ...] | None:
        if not self._runtime_is_current() or _positive_id(guild) != guild_id:
            return None
        actors: list[MusicActor] = []
        for author_id in tuple(dict.fromkeys(author_ids)):
            try:
                check = await build_music_commit_check(
                    self._bot,
                    guild,
                    author_id,
                    _SPEAK_COMMAND,
                    extra_capability_ids=(
                        _READ_ALOUD_CAPABILITY,
                        _DUCKING_CAPABILITY,
                    ),
                )
                actor = None if check is None else await check()
            except Exception:
                return None
            if not isinstance(actor, MusicActor) or actor.user_id != author_id or not self._runtime_is_current():
                return None
            if route is not None and actor.voice_channel_id != route.destination_voice_channel_id:
                return None
            actors.append(actor)
        if not actors:
            return None
        if route is not None and not self._session_current(route, claim=claim_session):
            return None
        return tuple(actors)

    def _event_allowed(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int,
        author_is_bot: bool,
    ) -> bool:
        if not self._runtime_is_current():
            return False
        checker = getattr(self._guard, "event_allowed", None)
        if not callable(checker):
            return False
        try:
            return bool(
                checker(
                    _READ_ALOUD_CAPABILITY,
                    surface=_READ_ALOUD_EVENT,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    event_id=message_id,
                    user_id=author_id,
                    author_is_bot=author_is_bot,
                )
            )
        except Exception:
            return False

    def _runtime_is_current(self) -> bool:
        if self._closing:
            return False
        try:
            callback_current = self._runtime_current() is True
        except Exception:
            return False
        return bool(
            callback_current
            and not bool(getattr(self._bot, "is_closing", False))
            and callable(getattr(self._guard, "event_allowed", None))
            and callable(getattr(self._guard, "currently_allowed", None))
            and callable(getattr(self._guard, "actor", None))
            and getattr(self._bot, "capability_guard", None) is self._guard
            and getattr(self._bot, "music_service", None) is self._service
            and getattr(self._bot, "speech_queue", None) is self._speech_queue
            and getattr(self._bot, "music_read_aloud_preset_repository", None) is self._preset_repository
            and self._service.available
            and bool(getattr(self._speech_queue, "available", False))
            and self._repository.is_open
            and self._preset_repository.is_open
        )

    def _queue_is_current(self, queue: Any) -> bool:
        return bool(
            self._runtime_is_current()
            and queue is self._speech_queue
            and getattr(self._bot, "speech_queue", None) is queue
            and bool(getattr(queue, "available", False))
        )

    def _current_guild(self, guild_id: int) -> Any | None:
        get_guild = getattr(self._bot, "get_guild", None)
        if not callable(get_guild):
            return None
        try:
            guild = get_guild(guild_id)
        except Exception:
            return None
        return guild if _positive_id(guild) == guild_id else None

    @staticmethod
    def _route_channels_current(guild: Any, route: ReadAloudRoute) -> bool:
        source_getter = getattr(guild, "get_channel_or_thread", None)
        if not callable(source_getter):
            source_getter = getattr(guild, "get_channel", None)
        voice_getter = getattr(guild, "get_channel", None)
        if not callable(source_getter) or not callable(voice_getter):
            return False
        try:
            source = source_getter(route.source_channel_id)
            destination = voice_getter(route.destination_voice_channel_id)
        except Exception:
            return False
        return bool(
            _positive_id(source) == route.source_channel_id
            and _positive_id(destination) == route.destination_voice_channel_id
            and _channel_guild_id(source) == route.guild_id
            and _channel_guild_id(destination) == route.guild_id
        )

    def _session_current(self, route: ReadAloudRoute, *, claim: bool = True) -> bool:
        if not self._runtime_is_current():
            return False
        session = self._service.listener_session_identity(route.guild_id)
        if session is None or self._service.session_channel_id(route.guild_id) != route.destination_voice_channel_id:
            return False
        key = (route.guild_id, route.source_channel_id)
        current = self._session_leases.get(key)
        if current is None:
            if claim:
                self._session_leases[key] = (route, session)
            return True
        if current[0] != route:
            if not claim:
                return True
            self._session_leases[key] = (route, session)
            return True
        return current[1] is session

    def _batch_finished(
        self,
        route: ReadAloudRoute,
        _author_ids: tuple[int, ...],
    ) -> None:
        key = (route.guild_id, route.source_channel_id)
        current = self._session_leases.get(key)
        if current is not None and current[0] == route:
            self._session_leases.pop(key, None)
        self._route_authorities.pop(route, None)


def _focus_route_revision(
    overlay: FocusReadAloudOverlay,
    store: FocusReadAloudOverlayStore,
) -> int:
    """Keep temporary routes distinct from every SQLite-backed route revision."""

    left = overlay.binding.revision
    right = id(store)
    paired = (left + right) * (left + right + 1) // 2 + right
    return (1 << 128) + paired


def _sha256_hex(value: object) -> bool:
    return bool(
        isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)
    )


def _positive_id(value: Any) -> int | None:
    identifier = getattr(value, "id", value)
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
        return None
    return identifier


def _channel_guild_id(channel: Any) -> int | None:
    return _positive_id(getattr(channel, "guild", None))


__all__ = ["MusicReadAloudAdapter"]
