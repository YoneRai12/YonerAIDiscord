"""VOICEVOXなどへ接続できる、Discord SDK非依存の音声プラグイン。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from yonerai_discord.runtime_readiness import publish_runtime_readiness, withdraw_runtime_readiness

from .adapter import VoiceGroup
from .models import SpeechRequest, SynthesizedSpeech
from .process import VoicevoxOwnedProcessLifecycle, VoicevoxProcessError
from .service import SpeechQueue, SpeechUnavailableError
from .voicevox import VoicevoxClient, VoicevoxConfigurationError


logger = logging.getLogger(__name__)


class _ReadinessSpeechQueue(SpeechQueue):
    def __init__(self, synthesizer: Any, *, readiness_current: Callable[[], bool]) -> None:
        super().__init__(synthesizer)
        self._readiness_current = readiness_current

    @property
    def available(self) -> bool:
        try:
            current = self._readiness_current() is True
        except Exception:
            current = False
        return super().available and current

    def _require_open(self) -> None:
        super()._require_open()
        try:
            current = self._readiness_current() is True
        except Exception:
            current = False
        if not current:
            raise SpeechUnavailableError("speech provider is not ready")


class VoicePlugin:
    def __init__(
        self,
        *,
        client_factory: Callable[..., VoicevoxClient] = VoicevoxClient,
        process_lifecycle_factory: Callable[..., VoicevoxOwnedProcessLifecycle] = VoicevoxOwnedProcessLifecycle,
        readiness_poll_seconds: float = 1.0,
    ) -> None:
        if (
            isinstance(readiness_poll_seconds, bool)
            or not isinstance(readiness_poll_seconds, (int, float))
            or not 0.01 <= float(readiness_poll_seconds) <= 30.0
        ):
            raise ValueError("readiness_poll_seconds is outside the fixed range")
        self._client_factory = client_factory
        self._process_lifecycle_factory = process_lifecycle_factory
        self._readiness_poll_seconds = float(readiness_poll_seconds)
        self.queue: SpeechQueue | None = None
        self._client: VoicevoxClient | None = None
        self._process_lifecycle: VoicevoxOwnedProcessLifecycle | None = None
        self._readiness_task: asyncio.Task[None] | None = None
        self._bot: Any | None = None
        self._command_registered = False
        self._provider_ready = False
        self._closing = False
        self._quarantined = False

    async def start(self, bot: Any) -> None:
        if self._bot is not None or self._closing or self._quarantined or self._process_lifecycle is not None:
            raise RuntimeError("voice plugin is already started")
        self._bot = bot
        self._provider_ready = False
        settings = bot.settings
        client = None
        lifecycle = None
        if settings.voice_enabled:
            try:
                client = self._client_factory(
                    endpoint=settings.voicevox_url,
                    allow_remote=settings.voice_allow_remote,
                    timeout_seconds=settings.voice_timeout_seconds,
                    max_response_bytes=settings.voice_max_response_bytes,
                )
                self._client = client
                managed = getattr(settings, "voicevox_managed_process_enabled", False)
                if managed is True:
                    executable = getattr(settings, "voicevox_managed_executable", None)
                    if executable is not None and not isinstance(executable, Path):
                        raise VoicevoxProcessError("voicevox_process_configuration_invalid")
                    lifecycle = self._process_lifecycle_factory(
                        managed_enabled=True,
                        endpoint=settings.voicevox_url,
                        executable=executable,
                        startup_timeout_seconds=getattr(
                            settings,
                            "voicevox_managed_startup_timeout_seconds",
                            15.0,
                        ),
                        shutdown_timeout_seconds=getattr(
                            settings,
                            "voicevox_managed_shutdown_timeout_seconds",
                            8.0,
                        ),
                    )
                    self._process_lifecycle = lifecycle
                    ready = await lifecycle.ensure_ready(lambda timeout: client.probe_version(timeout_seconds=timeout))
                elif managed is False:
                    ready = await client.probe_version(timeout_seconds=min(float(settings.voice_timeout_seconds), 30.0))
                else:
                    raise VoicevoxProcessError("voicevox_process_configuration_invalid")
                if ready is not True:
                    await client.close()
                    client = None
                    self._client = None
                else:
                    self._provider_ready = True
            except asyncio.CancelledError:
                if lifecycle is not None:
                    await lifecycle.stop()
                if client is not None:
                    await client.close()
                raise
            except (VoicevoxConfigurationError, VoicevoxProcessError):
                logger.warning(
                    "voice_provider_unavailable",
                    extra={"error_code": "voicevox_configuration_or_process_unavailable"},
                )
                cleanup_unconfirmed = False
                if lifecycle is not None:
                    try:
                        await lifecycle.stop()
                    except VoicevoxProcessError:
                        cleanup_unconfirmed = True
                        logger.error(
                            "voice_provider_cleanup_unconfirmed",
                            extra={"error_code": "voicevox_process_cleanup_unconfirmed"},
                        )
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        cleanup_unconfirmed = True
                    else:
                        client = None
                        self._client = None
                if cleanup_unconfirmed:
                    self._provider_ready = False
                    self._closing = True
                    self._quarantined = True
                    self._process_lifecycle = lifecycle
                    self._client = client
                    return
            except Exception:
                logger.warning(
                    "voice_provider_unavailable",
                    extra={"error_code": "voicevox_probe_failed"},
                )
                cleanup_unconfirmed = False
                if lifecycle is not None:
                    try:
                        await lifecycle.stop()
                    except VoicevoxProcessError:
                        cleanup_unconfirmed = True
                        logger.error(
                            "voice_provider_cleanup_unconfirmed",
                            extra={"error_code": "voicevox_process_cleanup_unconfirmed"},
                        )
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        cleanup_unconfirmed = True
                    else:
                        client = None
                        self._client = None
                if cleanup_unconfirmed:
                    self._provider_ready = False
                    self._closing = True
                    self._quarantined = True
                    self._process_lifecycle = lifecycle
                    self._client = client
                    return
        self._client = client
        self._process_lifecycle = lifecycle
        self.queue = _ReadinessSpeechQueue(client, readiness_current=self._provider_is_current)
        try:
            setattr(bot, "speech_queue", self.queue)
            publish_runtime_readiness(bot, {"cap-can-0416": self.queue.available})
            bot.tree.add_command(VoiceGroup(bot, self.queue))
            self._command_registered = True
            if client is not None:
                self._readiness_task = asyncio.create_task(
                    self._watch_readiness(bot, client),
                    name="yonerai-voicevox-readiness",
                )
        except BaseException:
            try:
                await self.stop()
            except BaseException:
                logger.error(
                    "voice_plugin_start_cleanup_failed",
                    extra={"error_code": "voice_plugin_start_cleanup_unconfirmed"},
                )
            raise

    async def begin_close(self) -> None:
        """新規合成を拒否し、共有中のprovider taskをstop()より先にcancelする。"""

        self._closing = True
        self._provider_ready = False
        await self._stop_readiness_watch()
        if self.queue is not None:
            await self.queue.close()

    async def stop(self) -> None:
        first_error: BaseException | None = None
        queue_cleanup_ok = True
        client_cleanup_ok = True
        lifecycle_cleanup_ok = True
        public_cleanup_ok = True
        try:
            await self.begin_close()
        except BaseException as exc:
            first_error = exc
            queue_cleanup_ok = False
        try:
            if self._client is not None:
                await self._client.close()
        except BaseException as exc:
            client_cleanup_ok = False
            if first_error is None:
                first_error = exc
        try:
            if self._process_lifecycle is not None:
                await self._process_lifecycle.stop()
        except BaseException as exc:
            lifecycle_cleanup_ok = False
            if first_error is None:
                first_error = exc
        bot = self._bot
        queue = self.queue
        command_registered = self._command_registered
        self._provider_ready = False
        queue_surface_cleanup_ok = True
        if bot is not None:
            try:
                if queue is not None and getattr(bot, "speech_queue", None) is queue:
                    delattr(bot, "speech_queue")
            except BaseException as exc:
                queue_surface_cleanup_ok = False
                public_cleanup_ok = False
                if first_error is None:
                    first_error = exc
            try:
                withdraw_runtime_readiness(bot, ("cap-can-0416",))
            except BaseException as exc:
                public_cleanup_ok = False
                if first_error is None:
                    first_error = exc
            if command_registered:
                try:
                    bot.tree.remove_command("voice")
                    self._command_registered = False
                except BaseException as exc:
                    public_cleanup_ok = False
                    if first_error is None:
                        first_error = exc
        if queue_cleanup_ok and queue_surface_cleanup_ok:
            self.queue = None
        if client_cleanup_ok:
            self._client = None
        if lifecycle_cleanup_ok:
            self._process_lifecycle = None
        resources_clean = queue_cleanup_ok and client_cleanup_ok and lifecycle_cleanup_ok and public_cleanup_ok
        if resources_clean:
            self._bot = None
            self._closing = False
            self._quarantined = False
        else:
            self._closing = True
            self._quarantined = True
        if first_error is not None:
            raise first_error

    def _provider_is_current(self) -> bool:
        if not self._provider_ready or self._closing or self._quarantined:
            return False
        lifecycle = self._process_lifecycle
        if lifecycle is not None and getattr(lifecycle, "owned_process_present", False):
            return getattr(lifecycle, "owns_process", False) is True
        return True

    async def _watch_readiness(self, bot: Any, client: VoicevoxClient) -> None:
        while self._bot is bot and self._client is client and not self._closing and not self._quarantined:
            await asyncio.sleep(self._readiness_poll_seconds)
            if self._bot is not bot or self._client is not client or self._closing or self._quarantined:
                return
            lifecycle = self._process_lifecycle
            process_alive = True
            if lifecycle is not None and getattr(lifecycle, "owned_process_present", False):
                process_alive = getattr(lifecycle, "owns_process", False) is True
            if process_alive:
                try:
                    ready = await client.probe_version(timeout_seconds=min(self._readiness_poll_seconds, 5.0)) is True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    ready = False
            else:
                ready = False
            if self._bot is not bot or self._client is not client or self._closing or self._quarantined:
                return
            self._provider_ready = ready
            try:
                publish_runtime_readiness(bot, {"cap-can-0416": self._provider_is_current()})
            except Exception:
                self._provider_ready = False

    async def _stop_readiness_watch(self) -> None:
        task, self._readiness_task = self._readiness_task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("voice", VoicePlugin)


__all__ = [
    "SpeechQueue",
    "SpeechRequest",
    "SpeechUnavailableError",
    "SynthesizedSpeech",
    "VoicevoxClient",
    "VoicevoxConfigurationError",
    "setup",
]
