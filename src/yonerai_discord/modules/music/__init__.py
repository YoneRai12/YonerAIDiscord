from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any

import discord

from yonerai_discord.modules.audio_core import FfmpegPCMSourceFactory, LocalMediaLibrary
from yonerai_discord.modules.voice.presets import SqliteVoicePresetRepository
from yonerai_discord.modules.voice.read_aloud import SqliteReadAloudRouteRepository
from yonerai_discord.runtime_readiness import publish_runtime_readiness, withdraw_runtime_readiness

from .adapter import MusicGroup
from .authorization import MusicFreshCheck, build_music_commit_check
from .dashboard import MusicDashboardController
from .imports import MusicImportStore, MusicImportStoreError
from .models import MusicActor
from .read_aloud_adapter import MusicReadAloudAdapter
from .repository import MusicPlaylistRepository
from .service import ListenerLifecycleAction, MusicService


logger = logging.getLogger(__name__)


_ALWAYS_READY_CAPABILITIES = (
    "cap-run-music-status",
    "cap-run-music-search-youtube",
)
_SERVICE_CAPABILITIES = tuple(
    f"cap-run-music-{command}"
    for command in (
        "join",
        "leave",
        "play",
        "search",
        "now",
        "queue",
        "pause",
        "resume",
        "skip",
        "stop",
        "radio",
        "remove",
        "move",
        "clear-mine",
        "shuffle",
        "loop",
        "volume",
    )
)
_PLAYLIST_CAPABILITIES = tuple(f"cap-run-music-playlist-{command}" for command in ("save", "list", "load", "delete"))
_SPEAK_CAPABILITY = "cap-run-music-speak"
_SEEK_CAPABILITY = "cap-run-music-seek"
_IMPORT_CAPABILITY = "cap-run-music-import"
_DUCKING_CORE_CAPABILITY = "cap-run-audio-ducking-core"
_READ_ALOUD_EVENT_CAPABILITY = "cap-run-music-read-aloud-message"
_LONG_LIVED_SESSION_CAPABILITIES = frozenset(
    {
        "cap-run-music-join",
        "cap-run-music-play",
        "cap-run-music-loop",
        "cap-run-music-playlist-load",
        _SPEAK_CAPABILITY,
        _DUCKING_CORE_CAPABILITY,
    }
)
_RUNTIME_CAPABILITIES = (
    *_ALWAYS_READY_CAPABILITIES,
    *_SERVICE_CAPABILITIES,
    *_PLAYLIST_CAPABILITIES,
    _SPEAK_CAPABILITY,
    _SEEK_CAPABILITY,
    _IMPORT_CAPABILITY,
    _DUCKING_CORE_CAPABILITY,
    _READ_ALOUD_EVENT_CAPABILITY,
)


class MusicPlugin:
    def __init__(self) -> None:
        self.bot: Any | None = None
        self.service: MusicService = MusicService.unavailable("not-started")
        self.repository: MusicPlaylistRepository | None = None
        self.import_store: MusicImportStore | None = None
        self.group: MusicGroup | None = None
        self.dashboard_controller: MusicDashboardController | None = None
        self.read_aloud_repository: SqliteReadAloudRouteRepository | None = None
        self.read_aloud_preset_repository: SqliteVoicePresetRepository | None = None
        self.read_aloud_adapter: MusicReadAloudAdapter | None = None
        self._command_registered = False
        self._ready_listener_registered = False
        self._voice_listener_registered = False
        self._read_aloud_listener_registered = False
        self._voice_lifecycle_enabled = False
        self._voice_lifecycle_tasks: dict[int, asyncio.Task[None]] = {}
        self._voice_lifecycle_locks: dict[int, asyncio.Lock] = {}
        self._read_aloud_lifecycle_lock = asyncio.Lock()
        self._read_aloud_generation = 0
        self._read_aloud_accepting_starts = False

    async def start(self, bot: Any) -> None:
        if self.bot is not None:
            return
        self.bot = bot
        self._read_aloud_accepting_starts = True
        self.group = MusicGroup(bot, self.service)
        bot.tree.add_command(self.group)
        self._command_registered = True

        try:
            self.service = await self._build_service(bot)
        except Exception as exc:
            logger.warning("music_runtime_unavailable", extra={"error_type": type(exc).__name__})
            if self.repository is not None:
                with suppress(Exception):
                    await asyncio.to_thread(self.repository.close)
            self.repository = None
            self.import_store = None
            self.service = MusicService.unavailable(f"initialization-error:{type(exc).__name__}")
        self.group.bind_service(self.service)
        setattr(bot, "music_service", self.service)
        if self.service.available and self.repository is not None:
            service = self.service
            repository = self.repository

            def dashboard_current() -> bool:
                return bool(
                    self.bot is bot
                    and self.service is service
                    and self.repository is repository
                    and self.dashboard_controller is controller
                    and self.group is not None
                    and getattr(bot, "music_service", None) is service
                    and not bool(getattr(bot, "is_closing", False))
                    and service.available
                )

            controller = MusicDashboardController(
                bot,
                service,
                repository,
                runtime_current=dashboard_current,
            )
            self.dashboard_controller = controller
            self.group.bind_dashboard(controller)
            try:
                await controller.restore_views()
            except Exception as exc:
                logger.warning(
                    "music_dashboard_restore_failed",
                    extra={"error_type": type(exc).__name__},
                )
                self.dashboard_controller = None
                self.group.bind_dashboard(None)
                await controller.close()
        self._publish_readiness(bot)
        add_listener = getattr(bot, "add_listener", None)
        if callable(add_listener):
            try:
                add_listener(self._refresh_readiness_on_ready, "on_ready")
                self._ready_listener_registered = True
                add_listener(self._on_voice_state_update, "on_voice_state_update")
                self._voice_listener_registered = True
                self._voice_lifecycle_enabled = True
            except BaseException:
                await self._disable_voice_lifecycle(bot)
                if self._ready_listener_registered:
                    remove_listener = getattr(bot, "remove_listener", None)
                    if callable(remove_listener):
                        with suppress(Exception):
                            remove_listener(self._refresh_readiness_on_ready, "on_ready")
                    self._ready_listener_registered = False
                await self.stop()
                raise
        await self._start_read_aloud(bot)
        self._publish_readiness(bot)

    async def stop(self) -> None:
        bot = self.bot
        self._read_aloud_accepting_starts = False
        if bot is not None:
            publish_runtime_readiness(
                bot,
                {
                    **dict.fromkeys(_SERVICE_CAPABILITIES, False),
                    **dict.fromkeys(_PLAYLIST_CAPABILITIES, False),
                    _SPEAK_CAPABILITY: False,
                    _SEEK_CAPABILITY: False,
                    _IMPORT_CAPABILITY: False,
                    _DUCKING_CORE_CAPABILITY: False,
                    _READ_ALOUD_EVENT_CAPABILITY: False,
                },
            )
        await self._disable_read_aloud(bot)
        await self._disable_voice_lifecycle(bot)
        await self._close_dashboard()
        try:
            await self.service.close()
        finally:
            if self.repository is not None:
                with suppress(Exception):
                    await self._cleanup_unreferenced_imports()
                with suppress(Exception):
                    await asyncio.to_thread(self.repository.close)
            if bot is not None:
                if getattr(bot, "music_service", None) is self.service:
                    delattr(bot, "music_service")
                if self._ready_listener_registered:
                    remove_listener = getattr(bot, "remove_listener", None)
                    if callable(remove_listener):
                        remove_listener(self._refresh_readiness_on_ready, "on_ready")
                self._ready_listener_registered = False
                withdraw_runtime_readiness(bot, _RUNTIME_CAPABILITIES)
                publish_runtime_readiness(bot, {_DUCKING_CORE_CAPABILITY: False})
                if self._command_registered:
                    with suppress(Exception):
                        bot.tree.remove_command("music", type=discord.AppCommandType.chat_input)
            self.bot = None
            self.repository = None
            self.import_store = None
            self.group = None
            self.dashboard_controller = None
            self.read_aloud_repository = None
            self.read_aloud_preset_repository = None
            self.read_aloud_adapter = None
            self.service = MusicService.unavailable("stopped")
            self._command_registered = False
            self._voice_lifecycle_locks.clear()

    async def begin_close(self) -> None:
        bot = self.bot
        self._read_aloud_accepting_starts = False
        if bot is not None:
            publish_runtime_readiness(
                bot,
                {
                    **dict.fromkeys(_SERVICE_CAPABILITIES, False),
                    **dict.fromkeys(_PLAYLIST_CAPABILITIES, False),
                    _SPEAK_CAPABILITY: False,
                    _SEEK_CAPABILITY: False,
                    _IMPORT_CAPABILITY: False,
                    _DUCKING_CORE_CAPABILITY: False,
                    _READ_ALOUD_EVENT_CAPABILITY: False,
                },
            )
        await self._disable_read_aloud(bot)
        await self._disable_voice_lifecycle(bot)
        await self._close_dashboard()
        await self.service.begin_close()

    async def _close_dashboard(self) -> None:
        controller, self.dashboard_controller = self.dashboard_controller, None
        if self.group is not None:
            self.group.bind_dashboard(None)
        if controller is not None:
            await controller.close()

    async def _start_read_aloud(self, bot: Any) -> bool:
        async with self._read_aloud_lifecycle_lock:
            return await self._start_read_aloud_locked(bot)

    async def _start_read_aloud_locked(self, bot: Any) -> bool:
        if self.read_aloud_adapter is not None:
            if self.read_aloud_adapter.available:
                return True
            await self._disable_read_aloud_locked(bot)
        settings = getattr(bot, "settings", None)
        speech_queue = getattr(bot, "speech_queue", None)
        if (
            not _bool_setting(settings, "music_read_aloud_enabled", False)
            or not self.service.available
            or self.repository is None
            or not bool(getattr(speech_queue, "available", False))
            or self.bot is not bot
            or not self._read_aloud_accepting_starts
        ):
            return False
        self._read_aloud_generation += 1
        generation = self._read_aloud_generation
        repository = SqliteReadAloudRouteRepository(_database_path(settings))
        preset_repository = SqliteVoicePresetRepository(_database_path(settings))
        try:
            await asyncio.to_thread(repository.open)
            await asyncio.to_thread(preset_repository.open)
        except Exception as exc:
            logger.warning(
                "music_read_aloud_repository_unavailable",
                extra={"error_type": type(exc).__name__},
            )
            with suppress(Exception):
                await asyncio.to_thread(repository.close)
            with suppress(Exception):
                await asyncio.to_thread(preset_repository.close)
            return False
        if not self._read_aloud_accepting_starts or self.bot is not bot or self._read_aloud_generation != generation:
            await asyncio.to_thread(repository.close)
            await asyncio.to_thread(preset_repository.close)
            return False

        adapter: MusicReadAloudAdapter

        def runtime_current() -> bool:
            return bool(
                self.bot is bot
                and self._read_aloud_generation == generation
                and self._read_aloud_accepting_starts
                and self.service is service
                and self.repository is music_repository
                and self.read_aloud_repository is repository
                and self.read_aloud_preset_repository is preset_repository
                and self.read_aloud_adapter is adapter
                and getattr(bot, "music_service", None) is service
                and getattr(bot, "speech_queue", None) is speech_queue
                and getattr(bot, "music_read_aloud_repository", None) is repository
                and getattr(bot, "music_read_aloud_preset_repository", None) is preset_repository
                and getattr(bot, "music_read_aloud_service", None) is adapter
                and not bool(getattr(bot, "is_closing", False))
                and service.available
                and repository.is_open
                and preset_repository.is_open
                and bool(getattr(speech_queue, "available", False))
            )

        service = self.service
        music_repository = self.repository
        adapter = MusicReadAloudAdapter(
            bot,
            service,
            repository,
            preset_repository,
            speech_queue,
            runtime_current=runtime_current,
        )
        add_listener = getattr(bot, "add_listener", None)
        if not callable(add_listener):
            await adapter.close()
            await asyncio.to_thread(repository.close)
            await asyncio.to_thread(preset_repository.close)
            return False
        try:
            add_listener(adapter.on_message, "on_message")
            self._read_aloud_listener_registered = True
            self.read_aloud_repository = repository
            self.read_aloud_preset_repository = preset_repository
            self.read_aloud_adapter = adapter
            setattr(bot, "music_read_aloud_repository", repository)
            setattr(bot, "music_read_aloud_preset_repository", preset_repository)
            setattr(bot, "music_read_aloud_service", adapter)
        except BaseException as exc:
            if self._read_aloud_listener_registered:
                remove_listener = getattr(bot, "remove_listener", None)
                if callable(remove_listener):
                    with suppress(Exception):
                        remove_listener(adapter.on_message, "on_message")
            self._read_aloud_listener_registered = False
            self.read_aloud_repository = None
            self.read_aloud_preset_repository = None
            self.read_aloud_adapter = None
            if getattr(bot, "music_read_aloud_repository", None) is repository:
                delattr(bot, "music_read_aloud_repository")
            if getattr(bot, "music_read_aloud_service", None) is adapter:
                delattr(bot, "music_read_aloud_service")
            if getattr(bot, "music_read_aloud_preset_repository", None) is preset_repository:
                delattr(bot, "music_read_aloud_preset_repository")
            await adapter.close()
            await asyncio.to_thread(repository.close)
            await asyncio.to_thread(preset_repository.close)
            if not isinstance(exc, Exception):
                raise
            logger.warning(
                "music_read_aloud_listener_unavailable",
                extra={"error_type": type(exc).__name__},
            )
            return False
        return adapter.available

    async def _disable_read_aloud(self, bot: Any | None) -> None:
        async with self._read_aloud_lifecycle_lock:
            await self._disable_read_aloud_locked(bot)

    async def _disable_read_aloud_locked(self, bot: Any | None) -> None:
        self._read_aloud_generation += 1
        adapter = self.read_aloud_adapter
        repository = self.read_aloud_repository
        preset_repository = self.read_aloud_preset_repository
        self.read_aloud_adapter = None
        self.read_aloud_repository = None
        self.read_aloud_preset_repository = None
        if bot is not None:
            if adapter is not None and getattr(bot, "music_read_aloud_service", None) is adapter:
                delattr(bot, "music_read_aloud_service")
            if repository is not None and getattr(bot, "music_read_aloud_repository", None) is repository:
                delattr(bot, "music_read_aloud_repository")
            if (
                preset_repository is not None
                and getattr(bot, "music_read_aloud_preset_repository", None) is preset_repository
            ):
                delattr(bot, "music_read_aloud_preset_repository")
            if adapter is not None and self._read_aloud_listener_registered:
                remove_listener = getattr(bot, "remove_listener", None)
                if callable(remove_listener):
                    with suppress(Exception):
                        remove_listener(adapter.on_message, "on_message")
        self._read_aloud_listener_registered = False
        if adapter is not None:
            with suppress(Exception):
                await adapter.close()
        if repository is not None:
            with suppress(Exception):
                await asyncio.to_thread(repository.close)
        if preset_repository is not None:
            with suppress(Exception):
                await asyncio.to_thread(preset_repository.close)

    async def _disable_voice_lifecycle(self, bot: Any | None) -> None:
        self._voice_lifecycle_enabled = False
        if bot is not None and self._voice_listener_registered:
            remove_listener = getattr(bot, "remove_listener", None)
            if callable(remove_listener):
                with suppress(Exception):
                    remove_listener(self._on_voice_state_update, "on_voice_state_update")
        self._voice_listener_registered = False
        tasks = tuple(self._voice_lifecycle_tasks.values())
        self._voice_lifecycle_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_voice_lifecycle_task(self, guild_id: int) -> None:
        task = self._voice_lifecycle_tasks.pop(guild_id, None)
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _start_voice_lifecycle_task(self, guild_id: int, operation: Any) -> None:
        task = asyncio.create_task(operation)
        self._voice_lifecycle_tasks[guild_id] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._voice_lifecycle_tasks.get(guild_id) is completed:
                self._voice_lifecycle_tasks.pop(guild_id, None)

        task.add_done_callback(discard)

    async def _on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        bot = self.bot
        service = self.service
        guild = getattr(member, "guild", None)
        guild_id = getattr(guild, "id", None)
        member_id = getattr(member, "id", None)
        if (
            not _listener_runtime_current(self, bot, service)
            or isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or isinstance(member_id, bool)
            or not isinstance(member_id, int)
            or member_id <= 0
        ):
            return
        lock = self._voice_lifecycle_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            if not _listener_runtime_current(self, bot, service):
                return
            before_channel = getattr(before, "channel", None)
            after_channel = getattr(after, "channel", None)
            before_channel_id = _positive_id(before_channel)
            after_channel_id = _positive_id(after_channel)
            bound_channel_id = service.session_channel_id(guild_id)
            bot_id = _positive_id(getattr(bot, "user", None))
            member_is_bot = bool(getattr(member, "bot", False))
            relevant = (
                member_id == bot_id
                and bound_channel_id is not None
                and (
                    after_channel_id != bound_channel_id
                    or (after_channel_id == bound_channel_id and before_channel_id != bound_channel_id)
                )
            ) or (
                member_id != bot_id
                and not member_is_bot
                and (
                    (
                        bound_channel_id is not None
                        and (before_channel_id == bound_channel_id or after_channel_id == bound_channel_id)
                    )
                    or (bound_channel_id is None and after_channel_id is not None)
                )
            )
            if not relevant:
                return
            if bound_channel_id is None:
                pending_check = await _listener_commit_check(bot, guild, member_id)
                pending_actor = await _checked_actor(pending_check)
                if pending_actor is None or pending_actor.voice_channel_id != after_channel_id:
                    return
                pending_decision = await service.listener_lifecycle_decision_current(
                    guild_id,
                    connected_voice_channel_id=None,
                    human_listener_count=1,
                    idle_elapsed_seconds=0,
                    eligible_listener_voice_channel_id=pending_actor.voice_channel_id,
                    eligible_listener_user_id=pending_actor.user_id,
                    eligible_listener_can_manage=pending_actor.manage_guild,
                )
                if (
                    pending_decision.action is not ListenerLifecycleAction.RECONNECT_ELIGIBLE
                    or pending_decision.voice_channel_id != after_channel_id
                ):
                    return
            await self._cancel_voice_lifecycle_task(guild_id)
            if (
                not _listener_runtime_current(self, bot, service)
                or service.session_channel_id(guild_id) != bound_channel_id
            ):
                return

            if member_id == bot_id:
                if bound_channel_id is None:
                    return
                if after_channel_id != bound_channel_id:
                    decision = service.listener_lifecycle_decision(
                        guild_id,
                        connected_voice_channel_id=after_channel_id,
                        human_listener_count=0,
                        idle_elapsed_seconds=0,
                    )
                    if (
                        decision.action is ListenerLifecycleAction.PRESERVE_AND_DISCONNECT
                        and decision.voice_channel_id is not None
                        and decision.session_identity is not None
                    ):
                        self._start_voice_lifecycle_task(
                            guild_id,
                            self._preserve_voice_session(
                                bot,
                                service,
                                guild,
                                decision.voice_channel_id,
                                decision.session_identity,
                                delay_seconds=0,
                            ),
                        )
                    return

            if bound_channel_id is not None:
                if before_channel_id != bound_channel_id and after_channel_id != bound_channel_id:
                    return
                channel = _cached_channel(guild, bound_channel_id, before_channel, after_channel)
                human_count = _human_listener_count(channel)
                if human_count is None or human_count > 0:
                    return
                connected_channel_id = _positive_id(getattr(getattr(guild, "voice_client", None), "channel", None))
                decision = service.listener_lifecycle_decision(
                    guild_id,
                    connected_voice_channel_id=connected_channel_id,
                    human_listener_count=0,
                    idle_elapsed_seconds=0,
                )
                if decision.session_identity is None or decision.voice_channel_id is None:
                    return
                if decision.action is ListenerLifecycleAction.PRESERVE_AND_DISCONNECT:
                    delay = 0
                elif decision.action is ListenerLifecycleAction.WAIT_FOR_LISTENER:
                    delay = decision.idle_timeout_seconds
                else:
                    return
                self._start_voice_lifecycle_task(
                    guild_id,
                    self._preserve_voice_session(
                        bot,
                        service,
                        guild,
                        decision.voice_channel_id,
                        decision.session_identity,
                        delay_seconds=delay,
                    ),
                )
                return

            if after_channel_id is not None:
                self._start_voice_lifecycle_task(
                    guild_id,
                    self._reconnect_voice_session(bot, service, guild, member_id, after_channel_id),
                )

    async def _preserve_voice_session(
        self,
        bot: Any,
        service: MusicService,
        guild: Any,
        voice_channel_id: int,
        session_identity: object,
        *,
        delay_seconds: int,
    ) -> None:
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            guild_id = int(guild.id)
            lock = self._voice_lifecycle_locks.setdefault(guild_id, asyncio.Lock())
            async with lock:
                if not _listener_runtime_current(self, bot, service):
                    return
                channel = _cached_channel(guild, voice_channel_id)
                human_count = _human_listener_count(channel)
                connected_channel_id = _positive_id(getattr(getattr(guild, "voice_client", None), "channel", None))
                if human_count is None and connected_channel_id == voice_channel_id:
                    return
                decision = service.listener_lifecycle_decision(
                    guild_id,
                    connected_voice_channel_id=connected_channel_id,
                    human_listener_count=0 if human_count is None else human_count,
                    idle_elapsed_seconds=delay_seconds,
                )
                if (
                    decision.action is not ListenerLifecycleAction.PRESERVE_AND_DISCONNECT
                    or decision.voice_channel_id != voice_channel_id
                    or decision.session_identity is not session_identity
                ):
                    return
                await service.suspend_voice_session(
                    guild_id,
                    expected_voice_channel_id=voice_channel_id,
                    expected_session_identity=session_identity,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("music_listener_preserve_failed", extra={"error_type": type(exc).__name__})

    async def _reconnect_voice_session(
        self,
        bot: Any,
        service: MusicService,
        guild: Any,
        user_id: int,
        voice_channel_id: int,
    ) -> None:
        voice_client = None
        try:
            guild_id = int(guild.id)
            lock = self._voice_lifecycle_locks.setdefault(guild_id, asyncio.Lock())
            async with lock:
                if not _listener_runtime_current(self, bot, service):
                    return
                current_check = await _listener_commit_check(bot, guild, user_id)
                actor = await _checked_actor(current_check)
                if actor is None or actor.voice_channel_id != voice_channel_id:
                    return
                channel, bot_member = await _fresh_voice_channel_and_bot_member(
                    bot,
                    guild,
                    voice_channel_id,
                )
                actor = await _checked_actor(current_check)
                if (
                    actor is None
                    or actor.voice_channel_id != voice_channel_id
                    or channel is None
                    or bot_member is None
                    or not _bot_can_use_voice(channel, bot_member)
                    or not _listener_runtime_current(self, bot, service)
                ):
                    return
                if getattr(guild, "voice_client", None) is not None:
                    return
                human_count = _human_listener_count(channel)
                if human_count is None:
                    return
                decision = await service.listener_lifecycle_decision_current(
                    guild_id,
                    connected_voice_channel_id=None,
                    human_listener_count=human_count,
                    idle_elapsed_seconds=0,
                    eligible_listener_voice_channel_id=actor.voice_channel_id,
                    eligible_listener_user_id=actor.user_id,
                    eligible_listener_can_manage=actor.manage_guild,
                )
                if (
                    decision.action is not ListenerLifecycleAction.RECONNECT_ELIGIBLE
                    or decision.voice_channel_id != voice_channel_id
                ):
                    return
                voice_client = await channel.connect(self_deaf=True, reconnect=False)
                if (
                    getattr(guild, "voice_client", None) is not voice_client
                    or _positive_id(getattr(voice_client, "channel", None)) != voice_channel_id
                ):
                    return

                async def reconnect_commit_check() -> MusicActor | None:
                    fresh_actor = await _checked_actor(current_check)
                    fresh_channel, fresh_bot_member = await _fresh_voice_channel_and_bot_member(
                        bot,
                        guild,
                        voice_channel_id,
                    )
                    if (
                        fresh_actor is None
                        or fresh_actor.voice_channel_id != voice_channel_id
                        or fresh_channel is None
                        or fresh_bot_member is None
                        or not _bot_can_use_voice(fresh_channel, fresh_bot_member)
                        or not _listener_runtime_current(self, bot, service)
                        or getattr(guild, "voice_client", None) is not voice_client
                        or _positive_id(getattr(voice_client, "channel", None)) != voice_channel_id
                    ):
                        return None
                    return fresh_actor

                actor = await reconnect_commit_check()
                if actor is None:
                    return
                decision = await service.listener_lifecycle_decision_current(
                    guild_id,
                    connected_voice_channel_id=voice_channel_id,
                    human_listener_count=1,
                    idle_elapsed_seconds=0,
                    eligible_listener_voice_channel_id=actor.voice_channel_id,
                    eligible_listener_user_id=actor.user_id,
                    eligible_listener_can_manage=actor.manage_guild,
                )
                if (
                    decision.action is not ListenerLifecycleAction.RECONNECT_ELIGIBLE
                    or decision.voice_channel_id != voice_channel_id
                ):
                    return
                await service.join(
                    guild_id,
                    voice_client,
                    actor,
                    voice_channel_id=voice_channel_id,
                    commit_check=reconnect_commit_check,
                )
                voice_client = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("music_listener_reconnect_failed", extra={"error_type": type(exc).__name__})
        finally:
            if voice_client is not None:
                with suppress(Exception):
                    await voice_client.disconnect(force=True)

    async def on_module_policy_changed(
        self,
        module_id: str,
        enabled: bool,
        guild_id: int | None,
    ) -> None:
        if enabled or module_id not in {"media.music", "media.audio-core"}:
            return
        if guild_id is None:
            await self._disable_read_aloud(self.bot)
            await self.service.close(delete_projections=True)
        else:
            await self.service.close_guild(guild_id)

    async def on_capability_policy_changed(
        self,
        capability_id: str,
        enabled: bool,
        guild_id: int | None,
    ) -> None:
        """長寿命のVC副作用を作る能力がOFFになったら対象sessionを即時終了する。"""

        if capability_id == _READ_ALOUD_EVENT_CAPABILITY:
            if guild_id is None:
                if enabled and self.bot is not None:
                    await self._start_read_aloud(self.bot)
                elif not enabled:
                    await self._disable_read_aloud(self.bot)
                if self.bot is not None:
                    self._publish_readiness(self.bot)
            return
        if enabled or capability_id not in _LONG_LIVED_SESSION_CAPABILITIES:
            return
        if guild_id is None:
            await self.service.close(delete_projections=True)
        else:
            await self.service.close_guild(guild_id)

    async def _refresh_readiness_on_ready(self) -> None:
        bot = self.bot
        if bot is not None:
            await self._start_read_aloud(bot)
            self._publish_readiness(bot)
            controller = self.dashboard_controller
            if controller is not None:
                await controller.schedule_all()

    def _publish_readiness(self, bot: Any) -> None:
        service_ready = self.service.available
        speech_queue = getattr(bot, "speech_queue", None)
        speech_ready = service_ready and bool(getattr(speech_queue, "available", False))
        seek_ready = service_ready and self.service.seek_available
        publish_runtime_readiness(
            bot,
            {
                **dict.fromkeys(_ALWAYS_READY_CAPABILITIES, True),
                **dict.fromkeys(_SERVICE_CAPABILITIES, service_ready),
                **dict.fromkeys(_PLAYLIST_CAPABILITIES, service_ready),
                _SPEAK_CAPABILITY: speech_ready,
                _SEEK_CAPABILITY: seek_ready,
                _IMPORT_CAPABILITY: service_ready and self.service.import_available,
                _DUCKING_CORE_CAPABILITY: service_ready,
                _READ_ALOUD_EVENT_CAPABILITY: bool(
                    self.read_aloud_adapter is not None and self.read_aloud_adapter.available
                ),
            },
        )

    async def _build_service(self, bot: Any) -> MusicService:
        settings = getattr(bot, "settings", None)
        if not _bool_setting(settings, "music_enabled", False):
            return MusicService.unavailable("disabled")
        roots = _root_setting(settings)
        if not roots:
            return MusicService.unavailable("library-not-configured")
        max_queue = _int_setting(settings, "music_max_queue", 50, minimum=1, maximum=1_000)
        if max_queue > 100:
            return MusicService.unavailable("queue-limit-exceeds-durable-projection")

        repository = MusicPlaylistRepository(
            _database_path(settings),
            max_playlists_per_user=_int_setting(
                settings,
                "music_max_playlists_per_user",
                50,
                minimum=1,
                maximum=500,
            ),
            max_tracks_per_playlist=_int_setting(
                settings,
                "music_max_tracks_per_playlist",
                100,
                minimum=1,
                maximum=1_000,
            ),
        )
        await asyncio.to_thread(repository.open)
        self.repository = repository
        pending_projections = await asyncio.to_thread(repository.list_audio_projections)
        import_store: MusicImportStore | None = None
        import_root = roots[0] / ".yonerai-imports"
        try:
            await asyncio.to_thread(import_root.mkdir, mode=0o700, exist_ok=True)
            import_store = MusicImportStore(import_root)
        except (MusicImportStoreError, OSError):
            import_store = None

        library = getattr(bot, "music_library", None)
        if library is None:
            library = LocalMediaLibrary(
                roots,
                max_files=_int_setting(settings, "music_library_max_files", 5_000, minimum=1, maximum=50_000),
                max_file_bytes=_int_setting(
                    settings,
                    "music_library_max_file_bytes",
                    512 * 1024 * 1024,
                    minimum=1_024,
                    maximum=2 * 1024 * 1024 * 1024,
                ),
            )
        indexed = await asyncio.to_thread(library.refresh)
        if indexed < 1 and import_store is None:
            return MusicService.unavailable("library-empty")

        source_factory = getattr(bot, "music_source_factory", None)
        if source_factory is None:
            executable = _ffmpeg_path(settings)
            if executable is None:
                return MusicService.unavailable("ffmpeg-unavailable")
            source_factory = FfmpegPCMSourceFactory(executable, roots)

        self.import_store = import_store
        return MusicService(
            library,
            source_factory,
            repository,
            available=True,
            reason="ready",
            indexed_tracks=indexed,
            max_queue=max_queue,
            max_speech_queue=_int_setting(settings, "music_max_speech_queue", 10, minimum=1, maximum=100),
            default_volume=_float_setting(settings, "music_default_volume", 0.65, minimum=0.0, maximum=2.0),
            pending_projections=pending_projections,
            import_store=import_store,
            state_observer=self._on_music_state_changed,
        )

    def _on_music_state_changed(self, guild_id: int) -> None:
        controller = self.dashboard_controller
        if controller is not None:
            controller.schedule_update(guild_id)

    async def _cleanup_unreferenced_imports(self) -> None:
        repository = self.repository
        store = self.import_store
        if repository is None or store is None:
            return
        assets = await asyncio.to_thread(repository.list_imported_assets)
        for asset in assets:
            references = await asyncio.to_thread(repository.imported_asset_reference_count, asset)
            if references != 0:
                continue
            receipt = await asyncio.to_thread(store.current_receipt, asset.content_sha256)
            if receipt is not None:
                removed = await asyncio.to_thread(store.discard_if_unreferenced, receipt, references)
                if not removed:
                    continue
            await asyncio.to_thread(repository.delete_imported_asset_if_unreferenced, asset)


def _listener_runtime_current(plugin: MusicPlugin, bot: Any, service: MusicService) -> bool:
    return bool(
        bot is not None
        and plugin._voice_lifecycle_enabled
        and plugin.bot is bot
        and plugin.service is service
        and getattr(bot, "music_service", None) is service
        and not bool(getattr(bot, "is_closing", False))
        and service.available
    )


def _positive_id(value: Any) -> int | None:
    identifier = getattr(value, "id", value)
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
        return None
    return identifier


def _cached_channel(guild: Any, channel_id: int, *candidates: Any) -> Any | None:
    get_channel = getattr(guild, "get_channel", None)
    if callable(get_channel):
        channel = get_channel(channel_id)
        if _positive_id(channel) == channel_id:
            return channel
    for channel in candidates:
        if _positive_id(channel) == channel_id:
            return channel
    return None


def _human_listener_count(channel: Any | None) -> int | None:
    if channel is None:
        return None
    members = getattr(channel, "members", None)
    if not isinstance(members, (tuple, list)):
        return None
    return sum(1 for member in members if not bool(getattr(member, "bot", False)))


async def _listener_commit_check(
    bot: Any,
    guild: Any,
    user_id: int,
) -> MusicFreshCheck | None:
    guard = getattr(bot, "capability_guard", None)
    play_check = await build_music_commit_check(
        bot,
        guild,
        user_id,
        "music play",
        extra_capability_ids=(_DUCKING_CORE_CAPABILITY,),
    )
    join_check = await build_music_commit_check(bot, guild, user_id, "music join")
    if play_check is None or join_check is None:
        return None

    async def current() -> MusicActor | None:
        if getattr(bot, "capability_guard", None) is not guard:
            return None
        play_actor = await play_check()
        if getattr(bot, "capability_guard", None) is not guard:
            return None
        join_actor = await join_check()
        if (
            getattr(bot, "capability_guard", None) is not guard
            or not isinstance(play_actor, MusicActor)
            or play_actor != join_actor
        ):
            return None
        return play_actor

    return current


async def _checked_actor(check: MusicFreshCheck | None) -> MusicActor | None:
    if check is None:
        return None
    try:
        actor = await check()
    except Exception:
        return None
    return actor if isinstance(actor, MusicActor) else None


async def _fresh_voice_channel_and_bot_member(
    bot: Any,
    guild: Any,
    channel_id: int,
) -> tuple[Any | None, Any | None]:
    fetch_channel = getattr(guild, "fetch_channel", None)
    fetch_member = getattr(guild, "fetch_member", None)
    bot_id = _positive_id(getattr(bot, "user", None))
    guild_id = _positive_id(guild)
    if not callable(fetch_channel) or not callable(fetch_member) or bot_id is None or guild_id is None:
        return None, None
    try:
        channel = await fetch_channel(channel_id)
        bot_member = await fetch_member(bot_id)
    except Exception:
        return None, None
    channel_guild_id = _positive_id(getattr(channel, "guild", None))
    if _positive_id(channel) != channel_id or channel_guild_id != guild_id or _positive_id(bot_member) != bot_id:
        return None, None
    return channel, bot_member


def _bot_can_use_voice(channel: Any, bot_member: Any) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    try:
        permissions = permissions_for(bot_member)
    except Exception:
        return False
    return bool(
        getattr(permissions, "view_channel", False)
        and getattr(permissions, "connect", False)
        and getattr(permissions, "speak", False)
    )


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("music", MusicPlugin)


def _bool_setting(settings: Any, name: str, default: bool) -> bool:
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _int_setting(settings: Any, name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return default
    return normalized if minimum <= normalized <= maximum else default


def _float_setting(settings: Any, name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        normalized = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return normalized if math.isfinite(normalized) and minimum <= normalized <= maximum else default


def _root_setting(settings: Any) -> tuple[Path, ...]:
    value = getattr(settings, "music_library_roots", ())
    if isinstance(value, (str, os.PathLike)):
        raw_values = [part.strip() for part in str(value).replace("\r", "\n").replace(";", "\n").split("\n")]
    else:
        try:
            raw_values = [str(part).strip() for part in value]
        except TypeError:
            return ()
    roots = tuple(dict.fromkeys(Path(part).expanduser().resolve(strict=False) for part in raw_values if part))
    return roots[:20]


def _database_path(settings: Any) -> Path:
    value = getattr(settings, "music_database_path", getattr(settings, "database_path", "data/yonerai.sqlite3"))
    try:
        return Path(value)
    except TypeError:
        return Path("data/yonerai.sqlite3")


def _ffmpeg_path(settings: Any) -> Path | None:
    configured = str(getattr(settings, "music_ffmpeg_path", "") or "").strip()
    candidate = configured or shutil.which("ffmpeg")
    if not candidate:
        return None
    path = Path(candidate).expanduser().resolve(strict=False)
    return path if path.is_file() else None


__all__ = ["MusicPlugin", "MusicService", "setup"]
