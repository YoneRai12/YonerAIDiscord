"""OpenAI GPT Image 2を既存provider registryへ明示接続するcomposition境界。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from yonerai_discord.modules.image_generation.artifacts import ImageArtifactStore
from yonerai_discord.provider_registry import (
    DEFAULT_CATALOG,
    CapabilityRoute,
    HealthStatus,
    ImageEditingInput,
    LogicalCapability,
    ModelBinding,
    ModelMaturity,
    ProviderCatalogManifest,
    ProviderKind,
    ProviderManifest,
    ProviderRegistry,
    ProviderRequest,
    QualityTier,
    ResourceProfile,
    SecretReference,
    TierRoute,
    TimeoutPolicy,
)
from yonerai_discord.provider_registry.domain import AuditRecord

from .provider_openai import (
    AiohttpOpenAIImageTransport,
    OPENAI_IMAGES_ADAPTER_ID,
    OPENAI_IMAGES_PROVIDER_ID,
    OpenAIImageHttpTransport,
    OpenAIImageProviderAdapter,
)


OPENAI_IMAGE_MODEL = "gpt-image-2"
OPENAI_IMAGE_MODEL_ALIASES = (
    "image.fast",
    "image.balanced",
    "image.quality",
    "image.edit.fast",
    "image.edit.balanced",
    "image.edit.quality",
)
_ACTOR_REF = re.compile(r"discord-user-(?P<actor_id>[1-9][0-9]{0,18})\Z")


class ImageProviderAuditDatabase(Protocol):
    def append_audit(
        self,
        event: str,
        *,
        actor_id: int,
        details: Mapping[str, object] | None = None,
        plugin: str | None = None,
        guild_id: int | str | None = None,
    ) -> int: ...


class SQLiteImageProviderAuditSink:
    """Prompt/画像bytes/provider errorを含めず既存append-only auditへ保存する。"""

    def __init__(
        self,
        database: ImageProviderAuditDatabase,
        *,
        runtime_current: Callable[[], bool],
    ) -> None:
        if not callable(getattr(database, "append_audit", None)) or not callable(runtime_current):
            raise TypeError("durable image provider audit is unavailable")
        self._database = database
        self._runtime_current = runtime_current

    async def append(self, record: AuditRecord) -> None:
        if not isinstance(record, AuditRecord) or self._runtime_current() is not True:
            raise RuntimeError("image provider audit is unavailable")
        actor = _ACTOR_REF.fullmatch(record.actor_ref)
        if actor is None:
            raise RuntimeError("image provider actor binding is invalid")
        details: dict[str, object] = {
            "request_id": record.request_id,
            "trace_id": record.trace_id,
            "capability": record.capability.value,
            "outcome": record.outcome.value,
            "quality_tier": record.quality_tier.value,
            "artifact_count": len(record.artifact_ids),
        }
        if record.provider_id is not None:
            details["provider_id"] = record.provider_id
        if record.model_alias is not None:
            details["model_alias"] = record.model_alias
        if record.duration_ms is not None:
            details["duration_ms"] = record.duration_ms
        if record.failure_code is not None:
            details["failure_code"] = record.failure_code
        plugin = "image_editing" if record.capability is LogicalCapability.IMAGE_EDITING else "image_generation"
        await asyncio.to_thread(
            self._database.append_audit,
            f"provider.{record.outcome.value}",
            actor_id=int(actor.group("actor_id")),
            details=details,
            plugin=plugin,
            guild_id=None,
        )
        if self._runtime_current() is not True:
            raise RuntimeError("image provider audit changed")


class BoundImageEditSourceReader:
    """ProviderRequest内のsource bindingだけでcanonical PNGを再取得する。"""

    def __init__(
        self,
        store: ImageArtifactStore,
        *,
        runtime_current: Callable[[], bool],
    ) -> None:
        if not isinstance(store, ImageArtifactStore) or not callable(runtime_current):
            raise TypeError("image edit source reader is unavailable")
        self._store = store
        self._runtime_current = runtime_current

    async def read_source_png(self, request: ProviderRequest, source: object) -> bytes:
        if (
            not isinstance(request, ProviderRequest)
            or not isinstance(request.payload, ImageEditingInput)
            or request.payload.source_binding_digest is None
            or len(request.input_artifacts) != 1
            or request.input_artifacts[0] is not source
            or self._runtime_current() is not True
        ):
            raise RuntimeError("image edit source is unavailable")
        data = await asyncio.to_thread(
            self._store.read_png,
            request.input_artifacts[0],
            request_binding=request.payload.source_binding_digest,
            read_allowed=self._runtime_current,
        )
        if self._runtime_current() is not True:
            raise RuntimeError("image edit source authorization changed")
        return data


class OpenAIImageRuntime:
    """generation/editingが共有する単一registry/store/adapterの所有者。"""

    def __init__(
        self,
        registry: ProviderRegistry,
        store: ImageArtifactStore,
        adapter: OpenAIImageProviderAdapter,
    ) -> None:
        self.registry = registry
        self.store = store
        self.adapter = adapter
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._consumers: list[object] = []
        self._closing_consumer: object | None = None
        self._adapter_unregistered = False

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def ready(self) -> bool:
        health = self.registry.health_snapshot(OPENAI_IMAGES_PROVIDER_ID)
        return (
            not self._closing
            and not self._closed
            and health is not None
            and health.status is HealthStatus.READY
            and tuple(health.probed_model_aliases) == OPENAI_IMAGE_MODEL_ALIASES
        )

    def begin_close(self) -> None:
        if self._consumers:
            raise RuntimeError("image provider runtime is still in use")
        self._closing = True
        self.adapter.begin_close()

    def acquire_consumer(self, consumer: object) -> None:
        if self._closing or self._closed or any(item is consumer for item in self._consumers):
            raise RuntimeError("image provider runtime consumer is invalid")
        self._consumers.append(consumer)

    def release_consumer(self, consumer: object) -> bool:
        for index, item in enumerate(self._consumers):
            if item is consumer:
                self._consumers.pop(index)
                if not self._consumers:
                    self._closing_consumer = consumer
                    self.begin_close()
                    return True
                return False
        if self._closing and not self._closed and self._closing_consumer is consumer:
            return True
        raise RuntimeError("image provider runtime consumer identity changed")

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self.begin_close()
            removed = self._adapter_unregistered
            if not self._adapter_unregistered:
                removed = self.registry.unregister_adapter_if_current(
                    OPENAI_IMAGES_PROVIDER_ID,
                    self.adapter,
                )
                self._adapter_unregistered = removed
            try:
                await self.adapter.close()
            except BaseException:
                raise
            self._closed = True
            self._closing_consumer = None
            if not removed:
                raise RuntimeError("image provider registry identity changed")


def publish_openai_image_runtime(bot: object, runtime: OpenAIImageRuntime) -> None:
    values = _runtime_publications(runtime)
    if any(hasattr(bot, name) for name in values):
        raise RuntimeError("image provider runtime identity is already published")
    for name, value in values.items():
        setattr(bot, name, value)


def openai_image_runtime_current(bot: object, runtime: OpenAIImageRuntime) -> bool:
    return (
        all(getattr(bot, name, None) is value for name, value in _runtime_publications(runtime).items())
        and not runtime.closing
        and not bool(getattr(bot, "is_closing", False))
    )


def unpublish_openai_image_runtime(bot: object, runtime: OpenAIImageRuntime) -> None:
    for name, value in _runtime_publications(runtime).items():
        if getattr(bot, name, None) is value:
            delattr(bot, name)


async def compose_openai_image_runtime(
    settings: object,
    database: object,
    *,
    runtime_current: Callable[[], bool],
    transport_factory: Callable[[str], OpenAIImageHttpTransport] | None = None,
) -> OpenAIImageRuntime | None:
    """明示opt-in、credential、固定root、model probeが揃った時だけruntimeを返す。"""

    if getattr(settings, "image_openai_enabled", False) is not True:
        return None
    api_key = getattr(settings, "openai_api_key", "")
    root = getattr(settings, "image_artifact_root", None)
    timeout_seconds = getattr(settings, "image_openai_timeout_seconds", 90.0)
    if (
        not isinstance(api_key, str)
        or not api_key
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in api_key)
        or not isinstance(root, Path)
        or not root.is_absolute()
        or not root.is_dir()
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 5.0 <= float(timeout_seconds) <= 900.0
        or not callable(getattr(database, "append_audit", None))
        or not callable(runtime_current)
    ):
        return None

    adapter: OpenAIImageProviderAdapter | None = None
    registry: ProviderRegistry | None = None
    try:
        store = ImageArtifactStore(root)
        audit_sink = SQLiteImageProviderAuditSink(database, runtime_current=runtime_current)
        source_reader = BoundImageEditSourceReader(store, runtime_current=runtime_current)
        factory = transport_factory or AiohttpOpenAIImageTransport
        transport = factory(api_key)
        adapter = OpenAIImageProviderAdapter(
            transport,
            store,
            source_reader,
            health_models=(OPENAI_IMAGE_MODEL,),
            probed_model_aliases=OPENAI_IMAGE_MODEL_ALIASES,
        )
        registry = ProviderRegistry(
            _runtime_catalog(float(timeout_seconds)),
            audit_sink=audit_sink,
        )
        registry.register_adapter(adapter)
        health = await registry.refresh_health(OPENAI_IMAGES_PROVIDER_ID)
        if health.status is not HealthStatus.READY or tuple(health.probed_model_aliases) != OPENAI_IMAGE_MODEL_ALIASES:
            await _close_candidate(registry, adapter)
            return None
        return OpenAIImageRuntime(registry, store, adapter)
    except asyncio.CancelledError:
        if adapter is not None:
            await _close_candidate(registry, adapter)
        raise
    except Exception:
        if adapter is not None:
            await _close_candidate(registry, adapter)
        return None


def _runtime_catalog(timeout_seconds: float) -> ProviderCatalogManifest:
    image_capabilities = (
        LogicalCapability.IMAGE_GENERATION,
        LogicalCapability.IMAGE_EDITING,
    )
    policies = tuple(
        replace(policy, default_enabled=True) if policy.capability in image_capabilities else policy
        for policy in DEFAULT_CATALOG.capabilities
    )
    provider = ProviderManifest(
        provider_id=OPENAI_IMAGES_PROVIDER_ID,
        kind=ProviderKind.API,
        adapter_id=OPENAI_IMAGES_ADAPTER_ID,
        capabilities=tuple(image_capabilities),
        resources=ResourceProfile.remote(max_concurrency=1),
        enabled=True,
        models=tuple(
            ModelBinding(
                alias,
                OPENAI_IMAGE_MODEL,
                maturity=ModelMaturity.STABLE,
                license_id="openai-api-terms",
                probe_required=True,
            )
            for alias in OPENAI_IMAGE_MODEL_ALIASES
        ),
        secret_refs=(SecretReference.parse("env:OPENAI_API_KEY"),),
        timeouts=TimeoutPolicy(
            request_seconds=timeout_seconds,
            health_seconds=5.0,
        ),
    )
    providers = tuple(item for item in DEFAULT_CATALOG.providers if item.provider_id != OPENAI_IMAGES_PROVIDER_ID) + (
        provider,
    )
    routes = tuple(
        _image_route(route) if route.capability in image_capabilities else route for route in DEFAULT_CATALOG.routes
    )
    return ProviderCatalogManifest(
        schema_version=DEFAULT_CATALOG.schema_version,
        module_id=DEFAULT_CATALOG.module_id,
        capabilities=policies,
        providers=providers,
        routes=routes,
        compatibility_aliases=DEFAULT_CATALOG.compatibility_aliases,
    )


def _image_route(route: CapabilityRoute) -> CapabilityRoute:
    prefix = "image.edit" if route.capability is LogicalCapability.IMAGE_EDITING else "image"
    return CapabilityRoute(
        route.capability,
        tuple(
            TierRoute(
                tier,
                (OPENAI_IMAGES_PROVIDER_ID,),
                f"{prefix}.{tier.value}",
            )
            for tier in QualityTier
        ),
        route.default_tier,
    )


async def _close_candidate(
    registry: ProviderRegistry | None,
    adapter: OpenAIImageProviderAdapter,
) -> None:
    if registry is not None:
        registry.unregister_adapter_if_current(OPENAI_IMAGES_PROVIDER_ID, adapter)
    try:
        await adapter.close()
    except Exception:
        pass


def _runtime_publications(runtime: OpenAIImageRuntime) -> dict[str, object]:
    return {
        "image_openai_runtime": runtime,
        "image_openai_provider_adapter": runtime.adapter,
        "image_artifact_store": runtime.store,
        "image_editing_provider_registry": runtime.registry,
        "image_generation_provider_registry": runtime.registry,
    }


__all__ = [
    "OPENAI_IMAGE_MODEL",
    "OPENAI_IMAGE_MODEL_ALIASES",
    "BoundImageEditSourceReader",
    "OpenAIImageRuntime",
    "SQLiteImageProviderAuditSink",
    "compose_openai_image_runtime",
    "openai_image_runtime_current",
    "publish_openai_image_runtime",
    "unpublish_openai_image_runtime",
]
