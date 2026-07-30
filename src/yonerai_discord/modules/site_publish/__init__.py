"""公開初版のsite publishing unavailable stub。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yonerai_discord.runtime_readiness import publish_runtime_readiness, withdraw_runtime_readiness

from .domain import (
    ActorRole,
    DiscordMessageBinding,
    PublishAction,
    PublishReceipt,
    PublishRequest,
    Site,
    SiteActivation,
    SiteActor,
    SiteRelease,
    SiteStatus,
    SiteVisibility,
)


SITE_CAPABILITY_IDS = (
    "cap-run-site-status",
    "cap-run-site-list",
    "cap-run-site-show",
    "cap-run-site-publish",
    "cap-run-site-update",
    "cap-run-site-rollback",
    "cap-run-site-visibility",
    "cap-run-site-archive",
    "cap-run-site-auto-publish",
    "cap-run-site-domain-manage",
)


@dataclass(frozen=True, slots=True)
class SitePublishRuntimeStatus:
    configured: bool
    ready: bool
    detail: str


class SitePublishPlugin:
    """Networkや永続化portを持たない、明示的なunavailable plugin。"""

    def __init__(self) -> None:
        self.bot: Any | None = None

    async def start(self, bot: Any) -> None:
        if self.bot is not None:
            raise RuntimeError("site publish unavailable plugin is already started")
        self.bot = bot
        bot.site_publish_service = None
        bot.site_publish_refresh = None
        bot.site_publish_status = SitePublishRuntimeStatus(
            configured=False,
            ready=False,
            detail="public初版ではofficial site-hostを同梱していません",
        )
        publish_runtime_readiness(bot, {capability_id: False for capability_id in SITE_CAPABILITY_IDS})

    async def stop(self) -> None:
        bot = self.bot
        if bot is None:
            return
        self.bot = None
        withdraw_runtime_readiness(bot, SITE_CAPABILITY_IDS)
        bot.site_publish_service = None
        bot.site_publish_refresh = None
        bot.site_publish_status = SitePublishRuntimeStatus(
            configured=False,
            ready=False,
            detail="public site publishing unavailable pluginは停止済みです",
        )


def setup(registry: object) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("site_publish", SitePublishPlugin)


__all__ = [
    "ActorRole",
    "DiscordMessageBinding",
    "PublishAction",
    "PublishReceipt",
    "PublishRequest",
    "SITE_CAPABILITY_IDS",
    "Site",
    "SiteActivation",
    "SiteActor",
    "SitePublishPlugin",
    "SitePublishRuntimeStatus",
    "SiteRelease",
    "SiteStatus",
    "SiteVisibility",
    "setup",
]
