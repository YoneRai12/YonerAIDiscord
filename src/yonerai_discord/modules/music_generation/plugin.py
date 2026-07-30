from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.provider_registry import LogicalCapability, ProviderRegistry
from yonerai_discord.runtime_readiness import (
    publish_runtime_readiness_probe,
    withdraw_runtime_readiness,
)

from .adapter import DiscordMusicGenerationAdapter
from .domain import MUSIC_GENERATION_CAPABILITY_ID
from .ports import MusicArtifactStore
from .service import MusicGenerationService

if TYPE_CHECKING:
    from yonerai_discord.modules.media_provider_composition import (
        MediaProviderRuntime,
        MediaProviderRuntimeComposer,
    )


class MusicGenerationPlugin:
    def __init__(
        self,
        *,
        registry: ProviderRegistry | None = None,
        artifact_store: MusicArtifactStore | None = None,
        runtime_composer: MediaProviderRuntimeComposer | None = None,
    ) -> None:
        self.registry, self.artifact_store, self.service, self.adapter, self._bot = (
            registry,
            artifact_store,
            None,
            None,
            None,
        )
        self.runtime_composer = runtime_composer
        self._runtime: MediaProviderRuntime | None = None
        self._published_artifact_store = False

    async def start(self, bot: Any) -> None:
        from yonerai_discord.modules.media_provider_composition import (
            acquire_media_provider_runtime,
            media_provider_capability_ready,
        )

        if self._bot is not None:
            raise RuntimeError("music generation plugin is already started")
        for name in ("music_generation_service", "music_generation_adapter"):
            if hasattr(bot, name):
                raise RuntimeError("music generation bot publication is already occupied")
        runtime: MediaProviderRuntime | None = None
        if self.registry is None and self.artifact_store is None:
            runtime = await acquire_media_provider_runtime(
                bot,
                self,
                composer=self.runtime_composer,
            )
            self._runtime = runtime
        registry = self.registry or getattr(bot, "music_generation_provider_registry", None)
        store = self.artifact_store or getattr(bot, "music_artifact_store", None)
        consent_store = getattr(bot, "ai_remote_consent_store", None)
        active = getattr(consent_store, "active_user", None)
        service = MusicGenerationService(
            registry if isinstance(registry, ProviderRegistry) else None,
            store,
            remote_consent_active=active if callable(active) else None,
            execution_proofs=(runtime.music_execution_proofs if runtime is not None else None),
        )
        adapter: DiscordMusicGenerationAdapter | None = None
        tree = None
        installed = False
        local_store_published = False
        try:
            adapter = DiscordMusicGenerationAdapter(service, capability_check=self._capability_check(bot))
            tree = bot.tree
            adapter.install(tree)
            installed = True
            self.service, self.adapter, self._bot = service, adapter, bot
            bot.music_generation_service, bot.music_generation_adapter = service, adapter
            if store is not None and not hasattr(bot, "music_artifact_store"):
                bot.music_artifact_store = store
                local_store_published = True
                self._published_artifact_store = True
            elif store is not None and getattr(bot, "music_artifact_store") is not store:
                raise RuntimeError("music artifact store publication is occupied")
            publish_runtime_readiness_probe(
                bot,
                MUSIC_GENERATION_CAPABILITY_ID,
                lambda: media_provider_capability_ready(
                    bot,
                    capability=LogicalCapability.MUSIC_GENERATION,
                    registry=registry,
                    store=store,
                    service=service,
                    adapter=adapter,
                    remote_consent_active=active,
                    runtime=runtime,
                ),
            )
        except BaseException:
            if adapter is not None:
                try:
                    adapter.begin_close()
                except BaseException:
                    pass
            try:
                service.begin_close()
            except BaseException:
                pass
            if installed and adapter is not None:
                try:
                    adapter.uninstall(tree)
                except BaseException:
                    pass
            try:
                withdraw_runtime_readiness(bot, (MUSIC_GENERATION_CAPABILITY_ID,))
            except BaseException:
                pass
            if getattr(bot, "music_generation_service", None) is service:
                delattr(bot, "music_generation_service")
            if adapter is not None and getattr(bot, "music_generation_adapter", None) is adapter:
                delattr(bot, "music_generation_adapter")
            if local_store_published and getattr(bot, "music_artifact_store", None) is store:
                delattr(bot, "music_artifact_store")
            self.service, self.adapter, self._bot = None, None, None
            self._published_artifact_store = False
            if runtime is not None:
                await self._release_runtime(bot, runtime)
            raise

    async def begin_close(self) -> None:
        first_error: BaseException | None = None
        if self.adapter is not None:
            try:
                self.adapter.begin_close()
            except BaseException as exc:
                first_error = exc
        if self.service is not None:
            try:
                self.service.begin_close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def stop(self) -> None:
        bot = self._bot
        runtime = self._runtime
        first_error: BaseException | None = None
        try:
            await self.begin_close()
        except BaseException as exc:
            first_error = exc
        self._bot = None
        adapter, self.adapter = self.adapter, None
        service, self.service = self.service, None
        published_store, self._published_artifact_store = self._published_artifact_store, False
        if bot is not None:
            try:
                withdraw_runtime_readiness(bot, (MUSIC_GENERATION_CAPABILITY_ID,))
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            try:
                if adapter is not None:
                    adapter.uninstall(bot.tree)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            if getattr(bot, "music_generation_service", None) is service:
                delattr(bot, "music_generation_service")
            if getattr(bot, "music_generation_adapter", None) is adapter:
                delattr(bot, "music_generation_adapter")
            if (
                published_store
                and self.artifact_store is not None
                and getattr(bot, "music_artifact_store", None) is self.artifact_store
            ):
                delattr(bot, "music_artifact_store")
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
            if capability_id != MUSIC_GENERATION_CAPABILITY_ID or bool(getattr(bot, "is_closing", False)):
                return False
            user_id, guild_id, guild, guard = (
                getattr(getattr(interaction, "user", None), "id", None),
                getattr(interaction, "guild_id", None),
                getattr(interaction, "guild", None),
                getattr(bot, "capability_guard", None),
            )
            fetch, evaluate, current = (
                getattr(guild, "fetch_member", None),
                getattr(guard, "evaluate_fresh_member", None),
                getattr(guard, "currently_allowed", None),
            )
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
                decision = await evaluate(capability_id, guild=guild, member=member)
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
            except Exception:
                return False

        return check
