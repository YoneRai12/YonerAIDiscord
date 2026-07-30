from __future__ import annotations

from collections.abc import Callable
from typing import Any

from yonerai_discord.capabilities import COMMAND_RBAC_FLOORS
from yonerai_discord.control_plane import RbacLevel

from .adapter import NASA_APOD_CAPABILITY_ID, DiscordNasaApodAdapter
from .errors import ApodConfigurationError
from .service import NasaApodService
from .source import ApodSource, NasaApiApodSource


NASA_APOD_PLUGIN_NAME = "nasa_apod"
SourceFactory = Callable[[Any], ApodSource]


class NasaApodPlugin:
    """明示要求時だけ1件取得し、常駐workerやscheduleを作らない。"""

    def __init__(self, *, source_factory: SourceFactory | None = None) -> None:
        self.source_factory = source_factory
        self.source: ApodSource | None = None
        self.service: NasaApodService | None = None
        self.adapter: DiscordNasaApodAdapter | None = None
        self._bot: Any | None = None

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            raise RuntimeError("NASA APOD plugin is already started")
        if not self._globally_enabled(bot):
            raise ApodConfigurationError("NASA APOD module and capability must be enabled")
        source = self.source_factory(bot) if self.source_factory is not None else self._api_source(bot)
        start = getattr(source, "start", None)
        try:
            if callable(start):
                await start()
            service = NasaApodService(source)
            adapter = DiscordNasaApodAdapter(
                service,
                capability_check=self._capability_check(bot),
            )
            adapter.install(bot.tree)
        except BaseException:
            close = getattr(source, "close", None)
            if callable(close):
                await close()
            raise
        self.source = source
        self.service = service
        self.adapter = adapter
        self._bot = bot
        bot.nasa_apod_service = service
        bot.nasa_apod_adapter = adapter

    async def begin_close(self) -> None:
        if self.adapter is not None:
            self.adapter.begin_close()

    async def stop(self) -> None:
        bot, self._bot = self._bot, None
        await self.begin_close()
        adapter, self.adapter = self.adapter, None
        service, self.service = self.service, None
        source, self.source = self.source, None
        try:
            if bot is not None and adapter is not None:
                adapter.uninstall(bot.tree)
        finally:
            if bot is not None:
                if getattr(bot, "nasa_apod_service", None) is service:
                    delattr(bot, "nasa_apod_service")
                if getattr(bot, "nasa_apod_adapter", None) is adapter:
                    delattr(bot, "nasa_apod_adapter")
            close = getattr(source, "close", None)
            if callable(close):
                await close()

    @staticmethod
    def _globally_enabled(bot: Any) -> bool:
        registry = getattr(bot, "capability_registry", None)
        if registry is None:
            return False
        try:
            return bool(registry.capability_status(NASA_APOD_CAPABILITY_ID, None).executable)
        except Exception:
            return False

    @staticmethod
    def _api_source(bot: Any) -> NasaApiApodSource:
        settings = bot.settings
        if not bool(getattr(settings, "nasa_apod_allow_remote", False)):
            raise ApodConfigurationError("NASA APOD remote access is not enabled")
        return NasaApiApodSource(
            getattr(settings, "nasa_apod_api_key", ""),
            session=getattr(bot, "nasa_apod_http_session", None),
        )

    @staticmethod
    def _capability_check(bot: Any):
        async def check(capability_id: str, interaction: Any) -> bool:
            if bool(getattr(bot, "is_closing", False)):
                return False
            guard = getattr(bot, "capability_guard", None)
            user_id = getattr(getattr(interaction, "user", None), "id", None)
            guild_id = getattr(interaction, "guild_id", None)
            floor = COMMAND_RBAC_FLOORS.get("nasa apod", RbacLevel.EVERYONE)
            if not isinstance(user_id, int) or user_id <= 0:
                return False

            if isinstance(guild_id, int):
                guild = getattr(interaction, "guild", None)
                fetch_member = getattr(guild, "fetch_member", None)
                evaluate = getattr(guard, "evaluate_fresh_member", None)
                current = getattr(guard, "currently_allowed", None)
                if guild is None or not callable(fetch_member) or not callable(evaluate) or not callable(current):
                    return False
                try:
                    member = await fetch_member(user_id)
                    if getattr(member, "id", None) != user_id:
                        return False
                    decision = await evaluate(capability_id, guild=guild, member=member)
                    actor_level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
                    return (
                        not bool(getattr(bot, "is_closing", False))
                        and bool(getattr(decision, "allowed", False))
                        and actor_level >= floor
                        and bool(
                            current(
                                capability_id,
                                guild_id=guild_id,
                                user_id=user_id,
                                actor_level=actor_level,
                                floor=floor,
                            )
                        )
                    )
                except Exception:
                    return False

            current = getattr(guard, "currently_allowed", None)
            actor_provider = getattr(guard, "actor", None)
            if not callable(current) or not callable(actor_provider):
                return False
            try:
                actor = await actor_provider(interaction)
                if int(actor.actor_id) != user_id:
                    return False
                return bool(
                    current(
                        capability_id,
                        guild_id=guild_id,
                        user_id=user_id,
                        actor_level=actor.level,
                        floor=floor,
                    )
                )
            except Exception:
                return False

        return check


__all__ = ["NASA_APOD_PLUGIN_NAME", "NasaApodPlugin"]
