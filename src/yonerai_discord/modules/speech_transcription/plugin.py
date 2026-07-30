from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.provider_registry import LogicalCapability, ProviderRegistry
from yonerai_discord.runtime_readiness import (
    publish_runtime_readiness_probe,
    withdraw_runtime_readiness,
)

from .adapter import DiscordSpeechTranscriptionDelivery
from .artifacts import BoundedSpeechAudioStore
from .domain import SPEECH_TRANSCRIPTION_CAPABILITY_ID
from .service import SpeechTranscriptionService

if TYPE_CHECKING:
    from yonerai_discord.modules.media_provider_composition import (
        MediaProviderRuntime,
        MediaProviderRuntimeComposer,
    )


class SpeechTranscriptionPlugin:
    """実audio入口とproviderが未接続でも、typed serviceをfail closedで公開する。"""

    def __init__(
        self,
        *,
        registry: ProviderRegistry | None = None,
        artifact_store: BoundedSpeechAudioStore | None = None,
        runtime_composer: MediaProviderRuntimeComposer | None = None,
    ) -> None:
        self.registry = registry
        self.artifact_store = artifact_store
        self.service: SpeechTranscriptionService | None = None
        self.adapter: DiscordSpeechTranscriptionDelivery | None = None
        self.audio_store: BoundedSpeechAudioStore | None = None
        self.runtime_composer = runtime_composer
        self._runtime: MediaProviderRuntime | None = None
        self._owns_audio_store = False
        self._published_audio_store = False
        self._bot: Any | None = None

    async def start(self, bot: Any) -> None:
        from yonerai_discord.modules.media_provider_composition import (
            acquire_media_provider_runtime,
            media_provider_capability_ready,
        )

        if self._bot is not None:
            raise RuntimeError("speech transcription plugin is already started")
        for name in ("speech_transcription_service", "speech_transcription_adapter"):
            if hasattr(bot, name):
                raise RuntimeError("speech transcription bot publication is already occupied")
        runtime: MediaProviderRuntime | None = None
        if self.registry is None and self.artifact_store is None:
            runtime = await acquire_media_provider_runtime(
                bot,
                self,
                composer=self.runtime_composer,
            )
            self._runtime = runtime
        registry = self.registry or getattr(bot, "speech_transcription_provider_registry", None)
        store_preexisting = hasattr(bot, "speech_transcription_audio_store")
        published_store = getattr(bot, "speech_transcription_audio_store", None)
        store = self.artifact_store or published_store or BoundedSpeechAudioStore()
        consent_store = getattr(bot, "ai_remote_consent_store", None)
        active_user = getattr(consent_store, "active_user", None)
        owns_store = self.artifact_store is not None or (
            not store_preexisting
            and not (runtime is not None and runtime.stores.get(LogicalCapability.SPEECH_STT) is store)
        )
        service: SpeechTranscriptionService | None = None
        try:
            service = SpeechTranscriptionService(
                registry if isinstance(registry, ProviderRegistry) else None,
                audio_artifact_current=store.current,
                remote_consent_active=active_user if callable(active_user) else None,
            )
            adapter = DiscordSpeechTranscriptionDelivery(
                service,
                capability_check=self._capability_check(bot),
                artifact_store=store,
            )
        except BaseException:
            if service is not None:
                service.begin_close()
            if owns_store:
                store.begin_close()
            if runtime is not None:
                await self._release_runtime(bot, runtime)
            raise
        tree = getattr(bot, "tree", None)
        installed = False
        local_store_published = False
        try:
            if callable(getattr(tree, "add_command", None)) and callable(getattr(tree, "remove_command", None)):
                adapter.install(tree)
                installed = True
            self.service = service
            self.adapter = adapter
            self.audio_store = store
            self._owns_audio_store = owns_store
            self._bot = bot
            bot.speech_transcription_service = service
            bot.speech_transcription_adapter = adapter
            if not hasattr(bot, "speech_transcription_audio_store"):
                bot.speech_transcription_audio_store = store
                local_store_published = True
                self._published_audio_store = True
            elif getattr(bot, "speech_transcription_audio_store") is not store:
                raise RuntimeError("speech transcription store publication is occupied")
            publish_runtime_readiness_probe(
                bot,
                SPEECH_TRANSCRIPTION_CAPABILITY_ID,
                lambda: media_provider_capability_ready(
                    bot,
                    capability=LogicalCapability.SPEECH_STT,
                    registry=registry,
                    store=store,
                    service=service,
                    adapter=adapter,
                    remote_consent_active=active_user,
                    runtime=runtime,
                ),
            )
        except BaseException:
            adapter.begin_close()
            if installed:
                try:
                    adapter.uninstall(tree)
                except BaseException:
                    pass
            try:
                withdraw_runtime_readiness(bot, (SPEECH_TRANSCRIPTION_CAPABILITY_ID,))
            except BaseException:
                pass
            if getattr(bot, "speech_transcription_service", None) is service:
                delattr(bot, "speech_transcription_service")
            if getattr(bot, "speech_transcription_adapter", None) is adapter:
                delattr(bot, "speech_transcription_adapter")
            if local_store_published and getattr(bot, "speech_transcription_audio_store", None) is store:
                delattr(bot, "speech_transcription_audio_store")
            owns_store = self._owns_audio_store
            self.service = None
            self.adapter = None
            self.audio_store = None
            self._owns_audio_store = False
            self._published_audio_store = False
            self._bot = None
            if owns_store:
                store.begin_close()
            if runtime is not None:
                await self._release_runtime(bot, runtime)
            raise

    async def begin_close(self) -> None:
        if self.adapter is not None:
            self.adapter.begin_close()
        elif self.service is not None:
            self.service.begin_close()

    async def stop(self) -> None:
        bot = self._bot
        first_error: BaseException | None = None
        try:
            await self.begin_close()
        except BaseException as exc:
            first_error = exc
        self._bot = None
        adapter, self.adapter = self.adapter, None
        service, self.service = self.service, None
        store, self.audio_store = self.audio_store, None
        owns_store, self._owns_audio_store = self._owns_audio_store, False
        published_store, self._published_audio_store = self._published_audio_store, False
        if bot is not None:
            try:
                withdraw_runtime_readiness(bot, (SPEECH_TRANSCRIPTION_CAPABILITY_ID,))
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            tree = getattr(bot, "tree", None)
            if adapter is not None and callable(getattr(tree, "remove_command", None)):
                try:
                    adapter.uninstall(tree)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if store is not None and owns_store:
                try:
                    store.begin_close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if getattr(bot, "speech_transcription_service", None) is service:
                delattr(bot, "speech_transcription_service")
            if getattr(bot, "speech_transcription_adapter", None) is adapter:
                delattr(bot, "speech_transcription_adapter")
            if published_store and getattr(bot, "speech_transcription_audio_store", None) is store:
                delattr(bot, "speech_transcription_audio_store")
        runtime = self._runtime
        if runtime is not None:
            try:
                await self._release_runtime(bot, runtime)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def _release_runtime(self, bot: Any, runtime: MediaProviderRuntime) -> None:
        from yonerai_discord.modules.media_provider_composition import (
            release_media_provider_runtime,
        )

        await release_media_provider_runtime(bot, runtime, self)
        self._runtime = None

    @staticmethod
    def _capability_check(bot: Any):
        async def check(capability_id: str, interaction: Any) -> bool:
            if capability_id != SPEECH_TRANSCRIPTION_CAPABILITY_ID or bool(getattr(bot, "is_closing", False)):
                return False
            user_id = getattr(getattr(interaction, "user", None), "id", None)
            guild_id = getattr(interaction, "guild_id", None)
            guild = getattr(interaction, "guild", None)
            guard = getattr(bot, "capability_guard", None)
            fetch = getattr(guild, "fetch_member", None)
            evaluate = getattr(guard, "evaluate_fresh_member", None)
            current = getattr(guard, "currently_allowed", None)
            if (
                isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id <= 0
                or isinstance(guild_id, bool)
                or not isinstance(guild_id, int)
                or guild is None
                or getattr(guild, "id", None) != guild_id
                or not callable(fetch)
                or not callable(evaluate)
                or not callable(current)
            ):
                return False
            try:
                member = await fetch(user_id)
                if getattr(member, "id", None) != user_id:
                    return False
                decision = await evaluate(
                    capability_id,
                    guild=guild,
                    member=member,
                )
                level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
                current_result = current(
                    capability_id,
                    guild_id=guild_id,
                    user_id=user_id,
                    actor_level=level,
                    floor=RbacLevel.TRUSTED,
                )
                return (
                    not bool(getattr(bot, "is_closing", False))
                    and getattr(decision, "allowed", False) is True
                    and level >= RbacLevel.TRUSTED
                    and current_result is True
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return False

        return check


__all__ = ["SpeechTranscriptionPlugin"]
