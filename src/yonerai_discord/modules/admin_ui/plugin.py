from __future__ import annotations

from collections.abc import Callable
from typing import Any

from yonerai_discord.runtime_readiness import (
    publish_runtime_readiness,
    withdraw_runtime_readiness,
)

from .config import AdminUiConfig
from .web_adapter import (
    ADMIN_UI_CAPABILITY_ID,
    AdminUiRequestHandler,
    AdminUiServer,
    AdminUiWebServer,
)


AdminUiServerFactory = Callable[
    [AdminUiRequestHandler, AdminUiConfig],
    AdminUiServer,
]


class AdminUiPlugin:
    """注入済み設定と認証境界がある時だけloopback listenerを開始する。"""

    def __init__(
        self,
        *,
        server_factory: AdminUiServerFactory = AdminUiWebServer,
    ) -> None:
        self._server_factory = server_factory
        self._bot: Any | None = None
        self.server: AdminUiServer | None = None

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            raise RuntimeError("admin UI plugin is already started")
        self._bot = bot
        config = getattr(bot, "admin_ui_config", None)
        authenticator = getattr(bot, "admin_ui_authenticator", None)
        audit_source = getattr(bot, "database", None)
        if (
            not isinstance(config, AdminUiConfig)
            or not config.enabled
            or not callable(getattr(authenticator, "current_user_id", None))
            or not callable(getattr(audit_source, "list_guild_audit_summary", None))
        ):
            publish_runtime_readiness(bot, {ADMIN_UI_CAPABILITY_ID: False})
            return

        handler = AdminUiRequestHandler(
            bot=bot,
            authenticator=authenticator,
            audit_source=audit_source,
        )
        publish_runtime_readiness(bot, {ADMIN_UI_CAPABILITY_ID: False})
        server: AdminUiServer | None = None
        try:
            server = self._server_factory(handler, config)
            self.server = server
            await server.start()
            publish_runtime_readiness(bot, {ADMIN_UI_CAPABILITY_ID: True})
        except BaseException:
            cleanup_failed = False
            if server is not None:
                try:
                    await server.stop()
                except BaseException:
                    cleanup_failed = True
                else:
                    self.server = None
            try:
                publish_runtime_readiness(bot, {ADMIN_UI_CAPABILITY_ID: False})
            except BaseException:
                pass
            if not cleanup_failed:
                self._bot = None
            raise
        bot.admin_ui_server = server

    async def stop(self) -> None:
        bot = self._bot
        server = self.server
        if bot is not None:
            withdraw_runtime_readiness(bot, (ADMIN_UI_CAPABILITY_ID,))
            if server is not None and getattr(bot, "admin_ui_server", None) is server:
                delattr(bot, "admin_ui_server")
        if server is None:
            self._bot = None
            return
        await server.stop()
        self.server = None
        self._bot = None


__all__ = ["AdminUiPlugin", "AdminUiServerFactory"]
