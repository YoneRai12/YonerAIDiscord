"""既定OFFのGemini remote media URL inspection plugin。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yonerai_discord.runtime_readiness import (
    publish_runtime_readiness,
    publish_runtime_readiness_probe,
    withdraw_runtime_readiness,
)

from .discord_adapter import DiscordMediaInspectionAdapter, MediaInspectionProvider
from .domain import MEDIA_URL_INSPECTION_CAPABILITY_ID
from .hyperv_provider import HyperVMediaInspectionProvider
from .provider import GeminiMediaInspectionProvider

if TYPE_CHECKING:
    from yonerai_discord.capability_broker import (
        BrokeredDiscordMediaInspectionAdapter,
        CapabilityBroker,
        HyperVMediaManagedBackend,
        SQLiteCapabilityAuditSink,
    )


@dataclass(frozen=True, slots=True)
class MediaInspectionStatus:
    configured: bool
    ready: bool
    detail: str


@dataclass(frozen=True, slots=True, repr=False)
class _Configuration:
    api_key: str
    timeout_seconds: float
    max_response_bytes: int
    daily_call_limit: int


class MediaInspectionPlugin:
    """remote opt-inとkeyが揃った時だけproviderを公開する。"""

    def __init__(self) -> None:
        self._bot: Any | None = None
        self.provider: MediaInspectionProvider | None = None
        self.adapter: DiscordMediaInspectionAdapter | BrokeredDiscordMediaInspectionAdapter | None = None
        self.backend: HyperVMediaManagedBackend | None = None
        self.broker: CapabilityBroker | None = None
        self.audit_sink: SQLiteCapabilityAuditSink | None = None

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            raise RuntimeError("media inspection plugin is already started")
        self._bot = bot
        bot.media_url_inspection_provider = None
        bot.media_url_inspection_adapter = None
        bot.media_capability_broker = None
        if bool(getattr(getattr(bot, "settings", None), "media_url_inspection_use_hyperv", False)):
            await self._start_hyperv(bot)
            return
        configuration = _configuration(getattr(bot, "settings", None))
        reserve = getattr(getattr(bot, "database", None), "reserve_media_url_inspection_call", None)
        if configuration is None or not callable(reserve):
            bot.media_url_inspection_status = MediaInspectionStatus(False, False, "未設定またはremote opt-inなし")
            publish_runtime_readiness(bot, {MEDIA_URL_INSPECTION_CAPABILITY_ID: False})
            return
        try:

            async def reserve_call() -> bool:
                if self._bot is not bot:
                    return False
                try:
                    return bool(await asyncio.to_thread(reserve, configuration.daily_call_limit))
                except Exception:
                    return False

            provider = GeminiMediaInspectionProvider(
                api_key=configuration.api_key,
                timeout_seconds=configuration.timeout_seconds,
                max_response_bytes=configuration.max_response_bytes,
                call_reserver=reserve_call,
            )
            adapter = DiscordMediaInspectionAdapter(provider)
            self.provider = provider
            self.adapter = adapter
            bot.media_url_inspection_provider = provider
            bot.media_url_inspection_adapter = adapter
            bot.media_url_inspection_status = MediaInspectionStatus(True, True, "起動済み")
            publish_runtime_readiness(bot, {MEDIA_URL_INSPECTION_CAPABILITY_ID: True})
        except BaseException:
            await self._cleanup(bot, detail="起動失敗")
            raise

    async def _start_hyperv(self, bot: Any) -> None:
        from yonerai_discord.capability_broker import (
            HYPERV_MEDIA_BACKEND_ID,
            HYPERV_MEDIA_IDENTITY_DIGEST,
            BrokeredDiscordMediaInspectionAdapter,
            CapabilityBroker,
            HyperVMediaManagedBackend,
            SQLiteCapabilityAuditSink,
        )

        timeout = getattr(getattr(bot, "settings", None), "media_url_inspection_hyperv_timeout_seconds", 60.0)
        database = getattr(bot, "database", None)
        if not callable(getattr(database, "append_audit", None)):
            bot.media_url_inspection_status = MediaInspectionStatus(True, False, "Hyper-V audit unavailable")
            publish_runtime_readiness(bot, {MEDIA_URL_INSPECTION_CAPABILITY_ID: False})
            return
        try:
            provider = HyperVMediaInspectionProvider(
                project_root=Path.cwd().resolve(strict=True),
                timeout_seconds=timeout,
            )
            self.provider = provider
            audit_sink = SQLiteCapabilityAuditSink(
                database,
                database_current=lambda: (
                    self._bot is bot and self.audit_sink is audit_sink and getattr(bot, "database", None) is database
                ),
            )
            backend = HyperVMediaManagedBackend(provider)
            broker = CapabilityBroker(
                backend=backend,
                audit_sink=audit_sink,
                expected_backend_id=HYPERV_MEDIA_BACKEND_ID,
                expected_identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
            )
            self.audit_sink = audit_sink
            self.backend = backend
            self.broker = broker
            adapter = BrokeredDiscordMediaInspectionAdapter(
                broker,
                broker_current=lambda: (
                    self._bot is bot
                    and self.provider is provider
                    and self.backend is backend
                    and self.broker is broker
                    and self.adapter is adapter
                    and self.audit_sink is audit_sink
                    and getattr(bot, "media_capability_broker", None) is broker
                    and getattr(bot, "media_url_inspection_provider", None) is provider
                    and getattr(bot, "media_url_inspection_adapter", None) is adapter
                    and getattr(bot, "database", None) is database
                ),
            )
            status = await broker.status()
            if not status.ready:
                adapter.begin_close()
                await provider.close()
                self.provider = None
                self.adapter = None
                self.backend = None
                self.broker = None
                self.audit_sink = None
                bot.media_url_inspection_status = MediaInspectionStatus(
                    status.configured,
                    False,
                    f"Hyper-V broker {status.reason_code}",
                )
                publish_runtime_readiness(bot, {MEDIA_URL_INSPECTION_CAPABILITY_ID: False})
                return
            self.adapter = adapter
            bot.media_url_inspection_provider = provider
            bot.media_url_inspection_adapter = adapter
            bot.media_capability_broker = broker
            bot.media_url_inspection_status = MediaInspectionStatus(True, True, "Hyper-V broker ready")
            publish_runtime_readiness_probe(
                bot,
                MEDIA_URL_INSPECTION_CAPABILITY_ID,
                lambda: (
                    self._bot is bot
                    and self.provider is provider
                    and self.backend is backend
                    and self.broker is broker
                    and self.adapter is adapter
                    and self.audit_sink is audit_sink
                    and getattr(bot, "media_capability_broker", None) is broker
                    and getattr(bot, "media_url_inspection_provider", None) is provider
                    and getattr(bot, "media_url_inspection_adapter", None) is adapter
                    and getattr(bot, "database", None) is database
                    and provider.ready
                    and not adapter.closing
                ),
            )
        except BaseException:
            await self._cleanup(bot, detail="Hyper-V startup failed")
            raise

    async def begin_close(self) -> None:
        if self.adapter is not None:
            self.adapter.begin_close()
        if self.provider is not None:
            begin_close = getattr(self.provider, "begin_close", None)
            if callable(begin_close):
                begin_close()

    async def stop(self) -> None:
        bot = self._bot
        if bot is None:
            return
        await self.begin_close()
        await self._cleanup(bot, detail="停止")

    async def _cleanup(self, bot: Any, *, detail: str) -> None:
        adapter, self.adapter = self.adapter, None
        provider, self.provider = self.provider, None
        broker, self.broker = self.broker, None
        self.backend = None
        self.audit_sink = None
        if getattr(bot, "media_capability_broker", None) is broker:
            bot.media_capability_broker = None
        try:
            if provider is not None:
                close = getattr(provider, "close", None)
                if callable(close):
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
        finally:
            self._bot = None
            try:
                withdraw_runtime_readiness(bot, (MEDIA_URL_INSPECTION_CAPABILITY_ID,))
            finally:
                if getattr(bot, "media_url_inspection_provider", None) is provider:
                    bot.media_url_inspection_provider = None
                if getattr(bot, "media_url_inspection_adapter", None) is adapter:
                    bot.media_url_inspection_adapter = None
                bot.media_url_inspection_status = MediaInspectionStatus(False, False, detail)


def _configuration(settings: object) -> _Configuration | None:
    if not bool(getattr(settings, "media_url_inspection_allow_remote", False)):
        return None
    api_key = getattr(settings, "media_url_inspection_api_key", None)
    timeout = getattr(settings, "media_url_inspection_timeout_seconds", 60.0)
    maximum = getattr(settings, "media_url_inspection_max_response_bytes", 256 * 1024)
    daily_limit = getattr(settings, "media_url_inspection_daily_call_limit", 0)
    if (
        not isinstance(api_key, str)
        or not api_key
        or len(api_key) > 512
        or any(character.isspace() or ord(character) < 33 for character in api_key)
    ):
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1.0 <= float(timeout) <= 60.0:
        return None
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 16_384 <= maximum <= 1024 * 1024:
        return None
    if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or not 1 <= daily_limit <= 1_000:
        return None
    return _Configuration(api_key, float(timeout), maximum, daily_limit)


__all__ = ["MediaInspectionPlugin", "MediaInspectionStatus"]
