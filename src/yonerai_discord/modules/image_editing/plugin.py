from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.provider_registry import LogicalCapability, ProviderKind, ProviderRegistry, QualityTier
from yonerai_discord.runtime_readiness import (
    publish_runtime_readiness_probe,
    withdraw_runtime_readiness,
)

from .adapter import ImageEditingDelivery
from .domain import IMAGE_EDITING_CAPABILITY_ID
from .service import ImageEditingService
from .source_claims import ImageEditSourceClaimIssuer
from yonerai_discord.modules.image_generation.provider_composition import (
    OpenAIImageRuntime,
    compose_openai_image_runtime,
    openai_image_runtime_current,
    publish_openai_image_runtime,
    unpublish_openai_image_runtime,
)
from yonerai_discord.modules.image_generation.provider_openai import OpenAIImageHttpTransport


class ImageEditingPlugin:
    """provider、source issuer、delivery sinkが未接続でもserviceをfail closedで公開する。"""

    def __init__(
        self,
        *,
        registry: ProviderRegistry | None = None,
        artifact_store: Any | None = None,
        openai_transport_factory: Callable[[str], OpenAIImageHttpTransport] | None = None,
    ) -> None:
        self.registry = registry
        self.artifact_store = artifact_store
        self.openai_transport_factory = openai_transport_factory
        self.service: ImageEditingService | None = None
        self.adapter: ImageEditingDelivery | None = None
        self.source_claim_issuer: ImageEditSourceClaimIssuer | None = None
        self._runtime: OpenAIImageRuntime | None = None
        self._bot: Any | None = None

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            raise RuntimeError("image editing plugin is already started")
        registry = self.registry or getattr(bot, "image_editing_provider_registry", None)
        store = self.artifact_store or getattr(bot, "image_artifact_store", None)
        runtime = getattr(bot, "image_openai_runtime", None)
        if runtime is not None and (
            not isinstance(runtime, OpenAIImageRuntime) or not openai_image_runtime_current(bot, runtime)
        ):
            raise RuntimeError("image provider runtime publication is invalid")
        if runtime is not None and (registry is not runtime.registry or store is not runtime.store):
            raise RuntimeError("image provider runtime publication is inconsistent")
        if registry is None and store is None:
            holder: dict[str, OpenAIImageRuntime | None] = {"runtime": None}

            def runtime_current() -> bool:
                candidate = holder["runtime"]
                return candidate is not None and openai_image_runtime_current(bot, candidate)

            runtime = await compose_openai_image_runtime(
                getattr(bot, "settings", None),
                getattr(bot, "database", None),
                runtime_current=runtime_current,
                transport_factory=self.openai_transport_factory,
            )
            if runtime is not None:
                holder["runtime"] = runtime
                self._runtime = runtime
                self._bot = bot
                try:
                    publish_openai_image_runtime(bot, runtime)
                except BaseException:
                    unpublish_openai_image_runtime(bot, runtime)
                    self._runtime = None
                    self._bot = None
                    await runtime.close()
                    raise
                registry, store = runtime.registry, runtime.store
        if runtime is not None:
            try:
                runtime.acquire_consumer(self)
            except BaseException:
                if self._runtime is runtime:
                    unpublish_openai_image_runtime(bot, runtime)
                    self._runtime = None
                    self._bot = None
                    await runtime.close()
                raise
            self._runtime = runtime
        issuer = None
        if all(callable(getattr(store, name, None)) for name in ("put_png", "protect_png", "read_png", "discard_png")):
            issuer = ImageEditSourceClaimIssuer(store)
        consent_store = getattr(bot, "ai_remote_consent_store", None)
        active_user = getattr(consent_store, "active_user", None)
        service: ImageEditingService | None = None
        adapter: ImageEditingDelivery | None = None
        try:
            service = ImageEditingService(
                registry if isinstance(registry, ProviderRegistry) else None,
                store,
                source_artifact_current=issuer.current if issuer is not None else None,
                remote_consent_active=active_user if callable(active_user) else None,
            )
            sink = getattr(bot, "image_editing_delivery_sink", None)
            if issuer is not None:
                adapter = ImageEditingDelivery(
                    service,
                    sink if callable(getattr(sink, "send_png", None)) else None,
                    capability_check=self._capability_check(bot),
                    source_claim_issuer=issuer,
                )
            self.service = service
            self.adapter = adapter
            self.source_claim_issuer = issuer
            self._bot = bot
            bot.image_editing_service = service
            if issuer is not None:
                bot.image_editing_source_claim_issuer = issuer
            if adapter is not None:
                bot.image_editing_adapter = adapter
            publish_runtime_readiness_probe(
                bot,
                IMAGE_EDITING_CAPABILITY_ID,
                lambda: self._runtime_ready(
                    registry,
                    issuer,
                    adapter,
                    active_user,
                    runtime=runtime,
                ),
            )
        except BaseException:
            try:
                withdraw_runtime_readiness(bot, (IMAGE_EDITING_CAPABILITY_ID,))
            except BaseException:
                pass
            if getattr(bot, "image_editing_service", None) is service:
                delattr(bot, "image_editing_service")
            if getattr(bot, "image_editing_adapter", None) is adapter:
                delattr(bot, "image_editing_adapter")
            if getattr(bot, "image_editing_source_claim_issuer", None) is issuer:
                delattr(bot, "image_editing_source_claim_issuer")
            self.service = None
            self.adapter = None
            self.source_claim_issuer = None
            self._bot = None
            if runtime is not None:
                await self._release_runtime(bot, runtime)
            raise

    async def begin_close(self) -> None:
        first_error: BaseException | None = None
        closers = (
            getattr(self.source_claim_issuer, "begin_close", None),
            getattr(self.adapter, "begin_close", None),
            getattr(self.service, "begin_close", None),
        )
        for close in closers:
            if not callable(close):
                continue
            try:
                close()
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
        finally:
            self._bot = None
            adapter, self.adapter = self.adapter, None
            service, self.service = self.service, None
            issuer, self.source_claim_issuer = self.source_claim_issuer, None
            if bot is not None:
                try:
                    withdraw_runtime_readiness(bot, (IMAGE_EDITING_CAPABILITY_ID,))
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                if getattr(bot, "image_editing_service", None) is service:
                    delattr(bot, "image_editing_service")
                if hasattr(bot, "image_editing_adapter") and getattr(bot, "image_editing_adapter") is adapter:
                    delattr(bot, "image_editing_adapter")
                if hasattr(bot, "image_editing_source_claim_issuer") and (
                    getattr(bot, "image_editing_source_claim_issuer") is issuer
                ):
                    delattr(bot, "image_editing_source_claim_issuer")
        if runtime is not None:
            try:
                await self._release_runtime(bot, runtime)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    @staticmethod
    def _runtime_ready(
        registry: object,
        issuer: object,
        adapter: object,
        active_user: object,
        *,
        runtime: OpenAIImageRuntime | None = None,
    ) -> bool:
        if (
            not isinstance(registry, ProviderRegistry)
            or issuer is None
            or adapter is None
            or bool(getattr(adapter, "_closing", True))
            or (runtime is not None and not runtime.ready)
        ):
            return False
        try:
            resolution = registry.resolve(
                LogicalCapability.IMAGE_EDITING,
                actor_level=RbacLevel.TRUSTED,
                quality_tier=QualityTier.BALANCED,
                consent_verified=callable(active_user),
            )
            if resolution.ready is not True or resolution.provider_id is None:
                return False
            provider = registry.manifest.provider(resolution.provider_id)
            return provider is not None and (provider.kind is not ProviderKind.API or callable(active_user))
        except Exception:
            return False

    @staticmethod
    def _capability_check(bot: Any):
        async def check(capability_id: str, interaction: Any) -> bool:
            if capability_id != IMAGE_EDITING_CAPABILITY_ID or bool(getattr(bot, "is_closing", False)):
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
            except asyncio.CancelledError:
                raise
            except Exception:
                return False

        return check

    async def _release_runtime(self, bot: Any, runtime: OpenAIImageRuntime) -> None:
        last_consumer = runtime.release_consumer(self)
        if not last_consumer:
            self._runtime = None
            return
        unpublish_openai_image_runtime(bot, runtime)
        try:
            await runtime.close()
        except BaseException:
            if runtime.closed:
                self._runtime = None
            raise
        self._runtime = None


__all__ = ["ImageEditingPlugin"]
