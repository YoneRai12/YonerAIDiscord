from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .adapter import DEFAULT_CAPABILITY_IDS, DiscordJpInformationAdapter, JpInformationCapabilityIds
from .client import CabinetOfficeHolidayClient, JmaClient
from .service import JpInformationService


JP_INFORMATION_PLUGIN_NAME = "jp_information"
ServiceFactory = Callable[[JmaClient, CabinetOfficeHolidayClient], JpInformationService]


class JpInformationPlugin:
    """command 実行時だけ取得する。常駐 worker や定期 poll は開始しない。"""

    def __init__(
        self,
        *,
        capability_ids: JpInformationCapabilityIds = DEFAULT_CAPABILITY_IDS,
        service_factory: ServiceFactory | None = None,
        weather_command_name: str = "weather",
        warning_command_name: str = "warning",
        holiday_command_name: str = "holiday",
    ) -> None:
        self.capability_ids = capability_ids
        self.service_factory = service_factory
        self.weather_command_name = weather_command_name
        self.warning_command_name = warning_command_name
        self.holiday_command_name = holiday_command_name
        self.jma_client: JmaClient | None = None
        self.holiday_client: CabinetOfficeHolidayClient | None = None
        self.service: JpInformationService | None = None
        self.adapter: DiscordJpInformationAdapter | None = None
        self._bot: Any | None = None

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            raise RuntimeError("jp_information plugin is already started")
        settings = bot.settings
        session = getattr(bot, "jp_information_http_session", None)
        total_timeout = float(getattr(settings, "jp_information_total_timeout_seconds", 10.0))
        connect_timeout = float(getattr(settings, "jp_information_connect_timeout_seconds", 3.0))
        self.jma_client = JmaClient(
            session=session,
            total_timeout_seconds=total_timeout,
            connect_timeout_seconds=connect_timeout,
            max_response_bytes=int(getattr(settings, "jp_information_jma_max_response_bytes", 2 * 1024 * 1024)),
        )
        self.holiday_client = CabinetOfficeHolidayClient(
            session=session,
            total_timeout_seconds=total_timeout,
            connect_timeout_seconds=connect_timeout,
            max_response_bytes=int(getattr(settings, "jp_information_holiday_max_response_bytes", 1024 * 1024)),
        )
        try:
            await self.jma_client.start()
            await self.holiday_client.start()
            if self.service_factory is None:
                self.service = JpInformationService(
                    self.jma_client,
                    self.holiday_client,
                    area_ttl_seconds=float(getattr(settings, "jp_information_area_ttl_seconds", 3_600.0)),
                    forecast_ttl_seconds=float(getattr(settings, "jp_information_forecast_ttl_seconds", 1_800.0)),
                    warning_ttl_seconds=float(getattr(settings, "jp_information_warning_ttl_seconds", 900.0)),
                    holiday_ttl_seconds=float(getattr(settings, "jp_information_holiday_ttl_seconds", 86_400.0)),
                )
            else:
                self.service = self.service_factory(self.jma_client, self.holiday_client)
            capability_check = getattr(bot, "jp_information_capability_check", None)
            if capability_check is None:
                capability_check = self._registry_check(bot)
            self.adapter = DiscordJpInformationAdapter(
                self.service,
                capability_check=capability_check,
                capability_ids=self.capability_ids,
                weather_command_name=self.weather_command_name,
                warning_command_name=self.warning_command_name,
                holiday_command_name=self.holiday_command_name,
            )
            self.adapter.install(bot.tree)
        except BaseException:
            if self.adapter is not None:
                try:
                    self.adapter.uninstall(bot.tree)
                except Exception:
                    pass
            await self._close_clients()
            self.service = None
            self.adapter = None
            raise

        self._bot = bot
        bot.jp_information_service = self.service
        bot.jp_information_adapter = self.adapter

    async def begin_close(self) -> None:
        if self.adapter is not None:
            self.adapter.begin_close()
        if self.service is not None:
            await self.service.begin_close()

    async def stop(self) -> None:
        bot = self._bot
        self._bot = None
        await self.begin_close()
        if bot is not None and self.adapter is not None:
            self.adapter.uninstall(bot.tree)
            if getattr(bot, "jp_information_service", None) is self.service:
                delattr(bot, "jp_information_service")
            if getattr(bot, "jp_information_adapter", None) is self.adapter:
                delattr(bot, "jp_information_adapter")
        self.adapter = None
        self.service = None
        await self._close_clients()

    async def _close_clients(self) -> None:
        holiday_client = self.holiday_client
        jma_client = self.jma_client
        self.holiday_client = None
        self.jma_client = None
        if holiday_client is not None:
            await holiday_client.close()
        if jma_client is not None:
            await jma_client.close()

    @staticmethod
    def _registry_check(bot: Any) -> Callable[[str, Any], bool]:
        def check(capability_id: str, interaction: Any) -> bool:
            if bool(getattr(bot, "is_closing", False)):
                return False
            registry = getattr(bot, "capability_registry", None)
            if registry is None:
                return False
            try:
                return bool(registry.capability_status(capability_id, interaction.guild_id).executable)
            except Exception:
                return False

        return check


# 既存 module の Plugin 命名に合わせた短い alias。
InformationPlugin = JpInformationPlugin
