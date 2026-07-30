from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any

import discord

from yonerai_discord.capabilities import EVENT_CAPABILITIES
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.jobs import (
    Attempt,
    DurableJobService,
    ExecutionContext,
    ExplicitExecutorRegistry,
    Job,
    Outcome,
)
from yonerai_discord.runtime_manifests.media_pipeline import (
    MEDIA_COMPOSE_GRID_CAPABILITY_ID,
    MEDIA_DISCORD_ASSET_INSPECT_CAPABILITY_ID,
    MEDIA_PIPELINE_CAPABILITY_IDS,
    MEDIA_PLACE_ON_CANVAS_CAPABILITY_ID,
    MEDIA_QR_ENCODE_CAPABILITY_ID,
    MEDIA_QUOTE_CARD_CAPABILITY_ID,
)
from yonerai_discord.runtime_readiness import publish_runtime_readiness, withdraw_runtime_readiness

from .artifacts import MediaArtifactStore
from .delivery import MediaArtifactDeliveryPreparer
from .discord_delivery import DiscordExactMessageMediaDeliverySink
from .durable_delivery import (
    MEDIA_DELIVERY_JOB_KIND,
    DurableMediaDeliveryExecutor,
    DurableMediaDeliveryPayload,
    DurableMediaDeliverySubmitter,
    MediaDeliverySinkRequest,
)
from .quote import MAX_FONT_BYTES, QuoteCardRenderer, QuoteRenderError
from .service import MediaPipelineService


MEDIA_PIPELINE_PLUGIN_NAME = "media_pipeline"
MEDIA_PIPELINE_MODULE_ID = "media.pipeline"
_MENTION_CAPABILITY_ID = EVENT_CAPABILITIES["ai_mention_message"]
_DELIVERY_DRAIN_TIMEOUT_SECONDS = 5.0
_CODE_OWNED_QUOTE_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/NotoSansJP-VF.ttf"),
    Path("C:/Windows/Fonts/meiryo.ttc"),
    Path("C:/Windows/Fonts/YuGothR.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf"),
)
_BASE_MEDIA_CAPABILITY_IDS = (
    MEDIA_QR_ENCODE_CAPABILITY_ID,
    MEDIA_PLACE_ON_CANVAS_CAPABILITY_ID,
    MEDIA_COMPOSE_GRID_CAPABILITY_ID,
)
_WINDOWS_REPARSE_POINT = 0x400


class _PluginBoundMediaDeliveryExecutor:
    __slots__ = ("_delegate", "_plugin")

    def __init__(
        self,
        plugin: MediaPipelinePlugin,
        delegate: DurableMediaDeliveryExecutor,
    ) -> None:
        self._plugin = plugin
        self._delegate = delegate

    @property
    def retired(self) -> bool:
        return self._plugin._bot is None and self._plugin.closing

    async def execute(self, job: Job, attempt: Attempt, context: ExecutionContext) -> Outcome:
        async with self._plugin.execution_semaphore:
            if (
                self._plugin.closing
                or self._plugin.durable_delivery_executor is not self
                or self._plugin._durable_delivery_delegate is not self._delegate
            ):
                context.cancel()
                context.complete_without_side_effect()
                return Outcome.skipped("media delivery runtime changed")
            return await self._delegate.execute(job, attempt, context)


class MediaPipelinePlugin:
    """既定OFFのローカルartifact serviceだけを登録するplugin。"""

    def __init__(self) -> None:
        self._bot: Any | None = None
        self._closing = False
        self.store: MediaArtifactStore | None = None
        self.service: MediaPipelineService | None = None
        self.quote_renderer: QuoteCardRenderer | None = None
        self.execution_semaphore = asyncio.Semaphore(1)
        self.durable_delivery_sink: DiscordExactMessageMediaDeliverySink | None = None
        self.durable_delivery_executor: _PluginBoundMediaDeliveryExecutor | None = None
        self._durable_delivery_delegate: DurableMediaDeliveryExecutor | None = None
        self._owns_durable_executor_mapping = False

    @property
    def closing(self) -> bool:
        return self._closing

    def is_current_for(self, bot: Any) -> bool:
        return self._bot is bot and not self._closing and getattr(bot, "media_pipeline_plugin", None) is self

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            raise RuntimeError("Media Pipeline plugin is already started")
        if bool(getattr(bot, "is_closing", False)):
            raise RuntimeError("Media Pipeline plugin cannot start during shutdown")
        registry = getattr(bot, "capability_registry", None)
        is_module_enabled = getattr(registry, "is_module_enabled", None)
        if not callable(is_module_enabled) or is_module_enabled(MEDIA_PIPELINE_MODULE_ID, None) is not True:
            raise RuntimeError("Media Pipeline module must be explicitly enabled")
        settings = getattr(bot, "settings", None)
        database_path = getattr(settings, "database_path", None)
        if not isinstance(database_path, Path):
            raise TypeError("settings.database_path must be a Path")

        resolved_database_path = database_path.expanduser().resolve(strict=False)
        root = resolved_database_path.parent / "media-pipeline-artifacts"
        root.mkdir(parents=True, exist_ok=True)
        store = MediaArtifactStore(root, database_path=resolved_database_path)
        quote_renderer = _resolve_quote_renderer(bot)
        service = MediaPipelineService(store, quote_renderer=quote_renderer)

        self._bot = bot
        self._closing = False
        self.store = store
        self.service = service
        self.quote_renderer = quote_renderer
        bot.media_pipeline_plugin = self
        bot.media_pipeline_store = store
        bot.media_pipeline_service = service
        try:
            self._register_durable_delivery(bot, store)
            readiness = dict.fromkeys(MEDIA_PIPELINE_CAPABILITY_IDS, False)
            readiness.update(dict.fromkeys(_BASE_MEDIA_CAPABILITY_IDS, True))
            readiness[MEDIA_QUOTE_CARD_CAPABILITY_ID] = quote_renderer is not None
            readiness[MEDIA_DISCORD_ASSET_INSPECT_CAPABILITY_ID] = True
            publish_runtime_readiness(bot, readiness)
        except BaseException:
            await self.stop()
            raise

    async def begin_close(self) -> None:
        self._closing = True
        bot = self._bot
        if bot is not None:
            publish_runtime_readiness(bot, dict.fromkeys(MEDIA_PIPELINE_CAPABILITY_IDS, False))
            self._withdraw_durable_executor(bot)
        await asyncio.wait_for(
            self._drain_deliveries(),
            timeout=_DELIVERY_DRAIN_TIMEOUT_SECONDS,
        )

    async def stop(self) -> None:
        bot = self._bot
        plugin = self
        if bot is None:
            return
        await self.begin_close()
        store, self.store = self.store, None
        service, self.service = self.service, None
        self.quote_renderer = None
        self._bot = None
        try:
            withdraw_runtime_readiness(bot, MEDIA_PIPELINE_CAPABILITY_IDS)
        finally:
            try:
                if store is not None:
                    await _close_store_without_blocking_loop(store)
            finally:
                if getattr(bot, "media_pipeline_service", None) is service:
                    delattr(bot, "media_pipeline_service")
                if getattr(bot, "media_pipeline_store", None) is store:
                    delattr(bot, "media_pipeline_store")
                if getattr(bot, "media_pipeline_plugin", None) is plugin:
                    delattr(bot, "media_pipeline_plugin")
                self.durable_delivery_sink = None
                self.durable_delivery_executor = None
                self._durable_delivery_delegate = None

    def delivery_bindings(
        self,
        action_ids: tuple[str, ...],
        *,
        guild_id: int,
    ) -> tuple[tuple[str, int], ...] | None:
        bot = self._bot
        if bot is None or self._closing or getattr(bot, "media_pipeline_plugin", None) is not self:
            return None
        return _current_delivery_bindings(bot, action_ids, guild_id=guild_id)

    def durable_submitter(self) -> DurableMediaDeliverySubmitter | None:
        bot = self._bot
        store = self.store
        executor = self.durable_delivery_executor
        jobs = getattr(bot, "durable_jobs", None) if bot is not None else None
        registry = getattr(bot, "durable_job_executor_registry", None) if bot is not None else None
        if (
            bot is None
            or self._closing
            or not isinstance(store, MediaArtifactStore)
            or not isinstance(jobs, DurableJobService)
            or not isinstance(registry, ExplicitExecutorRegistry)
            or executor is None
            or registry.get(MEDIA_DELIVERY_JOB_KIND) is not executor
        ):
            return None
        return DurableMediaDeliverySubmitter(
            jobs,
            store=store,
            store_current=lambda: self.store if self._bot is bot and not self._closing else None,
            clock=jobs.clock,
        )

    def durable_payload_current(self, payload: DurableMediaDeliveryPayload) -> bool:
        bot = self._bot
        if (
            bot is None
            or self._closing
            or getattr(bot, "media_pipeline_plugin", None) is not self
            or getattr(bot, "media_pipeline_store", None) is not self.store
            or getattr(bot, "media_pipeline_service", None) is not self.service
        ):
            return False
        bindings = _current_delivery_bindings(
            bot,
            payload.required_action_ids,
            guild_id=payload.scope.guild_id,
        )
        return bindings is not None and bindings == payload.required_capabilities

    def _register_durable_delivery(self, bot: Any, store: MediaArtifactStore) -> None:
        bot_user = getattr(bot, "user", None)
        bot_user_id = getattr(bot_user, "id", None)
        if (
            not isinstance(bot_user_id, int)
            or isinstance(bot_user_id, bool)
            or bot_user_id <= 0
            or getattr(bot_user, "bot", None) is not True
        ):
            return
        preparer = MediaArtifactDeliveryPreparer(
            store,
            store_current=lambda: self.store if self._bot is bot and not self._closing else None,
        )
        sink = DiscordExactMessageMediaDeliverySink(
            bot=bot,
            bot_current=lambda: self._bot,
            target_resolver=lambda request: _resolve_delivery_target(bot, request),
            authorization_current=lambda request: _delivery_authorization_current(self, bot, request),
        )
        delegate = DurableMediaDeliveryExecutor(
            preparer=preparer,
            preparer_current=lambda: preparer if self._bot is bot and not self._closing else None,
            sink=sink,
            sink_current=lambda: self.durable_delivery_sink,
            authorization_current=self.durable_payload_current,
            target_current=self.durable_payload_current,
            retention_store=store,
            retention_store_current=lambda: self.store if self._bot is bot and not self._closing else None,
        )
        executor = _PluginBoundMediaDeliveryExecutor(self, delegate)
        raw_mapping = getattr(bot, "durable_job_executors", None)
        created_mapping = raw_mapping is None
        if raw_mapping is None:
            raw_mapping = {}
            bot.durable_job_executors = raw_mapping
            self._owns_durable_executor_mapping = True
        if not isinstance(raw_mapping, dict) or MEDIA_DELIVERY_JOB_KIND in raw_mapping:
            raise RuntimeError("media delivery executor registration is unavailable")
        raw_mapping[MEDIA_DELIVERY_JOB_KIND] = executor
        registry = getattr(bot, "durable_job_executor_registry", None)
        previous_executor: _PluginBoundMediaDeliveryExecutor | None = None
        try:
            if registry is not None:
                if not isinstance(registry, ExplicitExecutorRegistry):
                    raise RuntimeError("media delivery executor registration is unavailable")
                current = registry.get(MEDIA_DELIVERY_JOB_KIND)
                if current is not None:
                    if not isinstance(current, _PluginBoundMediaDeliveryExecutor) or not current.retired:
                        raise RuntimeError("media delivery executor registration is unavailable")
                    if not registry.unregister_if_current(MEDIA_DELIVERY_JOB_KIND, current):
                        raise RuntimeError("media delivery executor registration is unavailable")
                    previous_executor = current
                try:
                    registry.register(MEDIA_DELIVERY_JOB_KIND, executor)
                except BaseException:
                    if previous_executor is not None:
                        registry.register(MEDIA_DELIVERY_JOB_KIND, previous_executor)
                    raise
        except BaseException:
            if raw_mapping.get(MEDIA_DELIVERY_JOB_KIND) is executor:
                del raw_mapping[MEDIA_DELIVERY_JOB_KIND]
            if created_mapping and raw_mapping == {} and getattr(bot, "durable_job_executors", None) is raw_mapping:
                delattr(bot, "durable_job_executors")
            self._owns_durable_executor_mapping = False
            raise
        self.durable_delivery_sink = sink
        self._durable_delivery_delegate = delegate
        self.durable_delivery_executor = executor

    def _withdraw_durable_executor(self, bot: Any) -> None:
        executor = self.durable_delivery_executor
        raw_mapping = getattr(bot, "durable_job_executors", None)
        if isinstance(raw_mapping, dict) and raw_mapping.get(MEDIA_DELIVERY_JOB_KIND) is executor:
            del raw_mapping[MEDIA_DELIVERY_JOB_KIND]
        if self._owns_durable_executor_mapping and raw_mapping == {}:
            delattr(bot, "durable_job_executors")
        self._owns_durable_executor_mapping = False

    async def _drain_deliveries(self) -> None:
        await self.execution_semaphore.acquire()
        self.execution_semaphore.release()


def _current_delivery_bindings(
    bot: Any,
    action_ids: tuple[str, ...],
    *,
    guild_id: int | None,
) -> tuple[tuple[str, int], ...] | None:
    if (
        not isinstance(action_ids, tuple)
        or not 1 <= len(action_ids) <= 20
        or len(set(action_ids)) != len(action_ids)
        or bool(getattr(bot, "is_closing", False))
    ):
        return None
    router = getattr(bot, "ai_action_router", None)
    action_registry = getattr(router, "registry", None)
    capability_registry = getattr(bot, "capability_registry", None)
    guard = getattr(bot, "capability_guard", None)
    readiness = getattr(bot, "runtime_capability_readiness", None)
    if (
        action_registry is None
        or capability_registry is None
        or getattr(guard, "registry", None) is not capability_registry
        or not isinstance(readiness, dict)
    ):
        return None
    requirements: dict[str, RbacLevel] = {}
    try:
        requirements[_MENTION_CAPABILITY_ID] = RbacLevel.parse(
            capability_registry.required_level(_MENTION_CAPABILITY_ID, guild_id)
        )
        for action_id in action_ids:
            spec = action_registry.get(action_id)
            if (
                getattr(spec, "action_id", None) != action_id
                or getattr(getattr(spec, "mode", None), "value", None) != "execute"
                or getattr(spec, "planner_contract", None) is None
            ):
                return None
            for _path, capability_id, floor in spec.capability_requirements:
                required = RbacLevel.parse(floor)
                previous = requirements.get(capability_id)
                if previous is None or required > previous:
                    requirements[capability_id] = required
        for capability_id, floor in tuple(requirements.items()):
            requirements[capability_id] = max(
                floor,
                RbacLevel.parse(capability_registry.required_level(capability_id, guild_id)),
            )
            status = capability_registry.capability_status(capability_id, guild_id)
            if (
                status.subject_id != capability_id
                or status.executable is not True
                or capability_registry.runtime_available(capability_id) is not True
                or readiness.get(capability_id) is not True
            ):
                return None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return tuple((capability_id, int(level)) for capability_id, level in sorted(requirements.items()))


async def _resolve_delivery_target(
    bot: Any,
    request: MediaDeliverySinkRequest,
) -> object:
    if bool(getattr(bot, "is_closing", False)):
        raise RuntimeError("media delivery target is unavailable")
    fetch_channel = getattr(bot, "fetch_channel", None)
    if not callable(fetch_channel):
        raise RuntimeError("media delivery target is unavailable")
    channel = await fetch_channel(request.channel_id)
    if (
        getattr(channel, "id", None) != request.channel_id
        or getattr(getattr(channel, "guild", None), "id", None) != request.guild_id
    ):
        raise RuntimeError("media delivery target is unavailable")
    fetch_message = getattr(channel, "fetch_message", None)
    if not callable(fetch_message):
        raise RuntimeError("media delivery target is unavailable")
    target = await fetch_message(request.message_id)
    if bool(getattr(bot, "is_closing", False)):
        raise RuntimeError("media delivery target is unavailable")
    return target


async def _delivery_authorization_current(
    plugin: MediaPipelinePlugin,
    bot: Any,
    request: MediaDeliverySinkRequest,
) -> bool:
    if (
        plugin._bot is not bot
        or plugin.closing
        or bool(getattr(bot, "is_closing", False))
        or request.guild_id is None
        or _current_delivery_bindings(
            bot,
            request.required_action_ids,
            guild_id=request.guild_id,
        )
        != request.required_capabilities
    ):
        return False
    try:
        from yonerai_discord.modules.ai.capability_rag import (
            project_authorized_capabilities_for_discord_actor,
        )

        fetch_channel = getattr(bot, "fetch_channel", None)
        if not callable(fetch_channel):
            return False
        channel = await fetch_channel(request.channel_id)
        guild = getattr(channel, "guild", None)
        if getattr(channel, "id", None) != request.channel_id or getattr(guild, "id", None) != request.guild_id:
            return False
        minimum_levels = {
            capability_id: RbacLevel(minimum_level) for capability_id, minimum_level in request.required_capabilities
        }
        guard = getattr(bot, "capability_guard", None)
        projection = await project_authorized_capabilities_for_discord_actor(
            guard=guard,
            guild=guild,
            channel=channel,
            user_id=request.user_id,
            capability_ids=tuple(minimum_levels),
            minimum_levels=minimum_levels,
        )
        if projection.allowed_capability_ids != frozenset(minimum_levels):
            return False
        bot_user = getattr(bot, "user", None)
        bot_user_id = getattr(bot_user, "id", None)
        fetch_member = getattr(guild, "fetch_member", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if (
            not isinstance(bot_user_id, int)
            or isinstance(bot_user_id, bool)
            or bot_user_id <= 0
            or not callable(fetch_member)
            or not callable(permissions_for)
        ):
            return False
        bot_member = await fetch_member(bot_user_id)
        if getattr(bot_member, "id", None) != bot_user_id:
            return False
        permissions = permissions_for(bot_member)
        send_allowed = (
            getattr(permissions, "send_messages_in_threads", False)
            if isinstance(channel, discord.Thread)
            else getattr(permissions, "send_messages", False)
        )
        return (
            getattr(permissions, "view_channel", False) is True
            and getattr(permissions, "read_message_history", False) is True
            and getattr(permissions, "attach_files", False) is True
            and send_allowed is True
            and plugin._bot is bot
            and not plugin.closing
            and not bool(getattr(bot, "is_closing", False))
            and _current_delivery_bindings(
                bot,
                request.required_action_ids,
                guild_id=request.guild_id,
            )
            == request.required_capabilities
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def _close_store_without_blocking_loop(store: MediaArtifactStore) -> None:
    close_task = asyncio.create_task(asyncio.to_thread(store.close))
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        try:
            await close_task
        except Exception:
            pass
        raise


def _resolve_quote_renderer(bot: Any) -> QuoteCardRenderer | None:
    injected = getattr(bot, "media_quote_card_renderer", None)
    if injected is not None:
        return injected if isinstance(injected, QuoteCardRenderer) else None
    for candidate in _CODE_OWNED_QUOTE_FONT_CANDIDATES:
        data = _read_fixed_regular_font(candidate)
        if data is None:
            continue
        try:
            return QuoteCardRenderer(data)
        except QuoteRenderError:
            continue
    return None


def _read_fixed_regular_font(path: Path) -> bytes | None:
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 1
            or before.st_size > MAX_FONT_BYTES
            or bool(getattr(before, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT)
        ):
            return None
        data = path.read_bytes()
        after = os.lstat(path)
    except OSError:
        return None
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or len(data) != before.st_size:
        return None
    return data


__all__ = [
    "MEDIA_PIPELINE_MODULE_ID",
    "MEDIA_PIPELINE_PLUGIN_NAME",
    "MediaPipelinePlugin",
]
