"""Default-off Cloudflare remote screenshot plugin.

This is not a fallback for the strict local browser sandbox.  It is a separate
remote service whose only output is a bounded screenshot delivered through the
existing Discord sink.
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from typing import Any

from yonerai_discord.browser_sandbox.cloudflare_quick_actions import CloudflareQuickActionsScreenshotProvider
from yonerai_discord.browser_sandbox.policy import BrowserSandboxLimits, BrowserSandboxPolicy
from yonerai_discord.browser_sandbox.remote_service import RemoteBrowserScreenshotService
from yonerai_discord.modules.ai.discord_renderer import DiscordAIResponseRenderer
from yonerai_discord.modules.media_pipeline.artifacts import MediaArtifactStore
from yonerai_discord.modules.web_runtime.browser import (
    AllowedOriginPolicy,
    BoundedBrowserRunner,
    BrowserRunLimits,
)
from yonerai_discord.modules.web_runtime.cloudflare_browser_run import (
    CloudflareBrowserRunConfig,
    CloudflareBrowserRunSessionFactory,
)
from yonerai_discord.runtime_readiness import (
    publish_runtime_readiness,
    publish_runtime_readiness_probe,
    refresh_runtime_readiness,
    withdraw_runtime_readiness,
)

from .discord_adapter import DiscordRemoteBrowserScreenshotAdapter
from .interactive_adapter import BrowserInteractiveCheckpointStore, DiscordRemoteBrowserInteractiveAdapter


REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID = "cap-run-browser-remote-screenshot"
REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID = "cap-run-browser-remote-interactive"
_YOUTUBE_BROWSER_DOMAINS = (
    "www.youtube.com",
    "*.youtube.com",
    "*.googlevideo.com",
    "*.ytimg.com",
    "*.googleusercontent.com",
    "*.gstatic.com",
    "*.google.com",
)


class SocketDnsResolver:
    """Synchronous public-DNS resolver; policy checks every returned address."""

    def resolve(self, hostname: str) -> tuple[str, ...]:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        addresses = tuple(dict.fromkeys(str(record[4][0]) for record in records if record[4]))
        if not addresses:
            raise OSError("hostname did not resolve")
        return addresses


@dataclass(frozen=True, slots=True)
class RemoteBrowserRenderingStatus:
    configured: bool
    ready: bool
    detail: str


class BrowserRenderingPlugin:
    """Lifecycle owner for the optional Cloudflare Quick Actions provider."""

    def __init__(self) -> None:
        self._bot: Any | None = None
        self.service: RemoteBrowserScreenshotService | None = None
        self.adapter: DiscordRemoteBrowserScreenshotAdapter | None = None
        self.interactive_runner: BoundedBrowserRunner | None = None
        self.interactive_adapter: DiscordRemoteBrowserInteractiveAdapter | None = None

    async def start(self, bot: Any) -> None:
        if self._bot is not None:
            raise RuntimeError("browser rendering plugin is already started")
        public_owner_attributes = (
            "browser_rendering_plugin",
            "remote_browser_screenshot_service",
            "browser_rendering_adapter",
            "remote_browser_run_service",
            "browser_run_adapter",
        )
        if any(getattr(bot, attribute, None) is not None for attribute in public_owner_attributes):
            raise RuntimeError("browser rendering public state is already owned")
        self._bot = bot
        bot.remote_browser_screenshot_service = None
        bot.browser_rendering_adapter = None
        bot.browser_rendering_plugin = self
        bot.remote_browser_run_service = None
        bot.browser_run_adapter = None
        settings = getattr(bot, "settings", None)
        configuration = _configuration(settings)
        interactive_configuration = _interactive_configuration(settings)
        screenshot_ready = False
        interactive_ready = False
        bot.remote_browser_rendering_status = RemoteBrowserRenderingStatus(
            configured=configuration is not None,
            ready=False,
            detail="未設定またはremote opt-inなし" if configuration is None else "起動準備中",
        )
        bot.remote_browser_interactive_status = RemoteBrowserRenderingStatus(
            configured=interactive_configuration is not None,
            ready=False,
            detail="未設定またはinteractive opt-inなし" if interactive_configuration is None else "起動準備中",
        )
        try:
            if configuration is not None:
                provider = CloudflareQuickActionsScreenshotProvider(
                    account_id=configuration.account_id,
                    api_token=configuration.api_token,
                    timeout_seconds=configuration.timeout_seconds,
                    max_output_bytes=configuration.max_output_bytes,
                )
                service = RemoteBrowserScreenshotService(
                    policy=BrowserSandboxPolicy(
                        resolver=SocketDnsResolver(),
                        # An empty allowlist deliberately means arbitrary public HTTP(S).
                        # BrowserSandboxPolicy still rejects private/non-global addresses.
                        allowed_domains=(),
                        limits=BrowserSandboxLimits(
                            max_steps=2,
                            # The shared policy has a 1s/64KiB lower bound. The
                            # provider retains a stricter configured sub-second
                            # timeout while output configuration starts at 1MiB.
                            max_duration_seconds=max(1.0, configuration.timeout_seconds),
                            max_total_bytes=max(64 * 1024, configuration.max_output_bytes),
                        ),
                    ),
                    provider=provider,
                )
                # Own the service before its first await so partial start and later
                # composition failures always use the common cleanup path.
                self.service = service
                await service.start()
                adapter = DiscordRemoteBrowserScreenshotAdapter(
                    service,
                    sink=getattr(bot, "remote_browser_screenshot_sink", None),
                )
                adapter.bind_bot(bot)
                self.adapter = adapter
                bot.remote_browser_screenshot_service = service
                bot.browser_rendering_adapter = adapter
                screenshot_ready = callable(getattr(getattr(bot, "database", None), "append_audit", None))
                bot.remote_browser_rendering_status = RemoteBrowserRenderingStatus(
                    configured=True,
                    ready=screenshot_ready,
                    detail="起動済み" if screenshot_ready else "監査保存先が未設定",
                )

            if interactive_configuration is not None:
                factory = CloudflareBrowserRunSessionFactory(
                    config=CloudflareBrowserRunConfig(
                        account_id=interactive_configuration.account_id.lower(),
                        api_token=interactive_configuration.api_token,
                        enabled=True,
                    )
                )
                availability = factory.availability
                store = getattr(bot, "media_pipeline_store", None)
                audit_ready = callable(getattr(getattr(bot, "database", None), "append_audit", None))
                if availability.available is True and isinstance(store, MediaArtifactStore) and audit_ready:
                    policy = BrowserSandboxPolicy(
                        resolver=SocketDnsResolver(),
                        allowed_domains=_YOUTUBE_BROWSER_DOMAINS,
                        allowed_ports={
                            "http": frozenset({80}),
                            "https": frozenset({443}),
                        },
                        limits=BrowserSandboxLimits(
                            max_steps=12,
                            max_redirects=0,
                            max_network_requests=200,
                            max_duration_seconds=max(1.0, interactive_configuration.timeout_seconds),
                            max_total_bytes=64 * 1024 * 1024,
                            max_total_wait_milliseconds=5_000,
                        ),
                    )
                    checkpoint_store = BrowserInteractiveCheckpointStore()
                    runner = BoundedBrowserRunner(
                        policy=policy,
                        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
                        session_factory=factory,
                        checkpoint_store=checkpoint_store,
                        limits=BrowserRunLimits(
                            max_steps=12,
                            max_segments=2,
                            total_timeout_seconds=interactive_configuration.timeout_seconds,
                            max_artifacts=2,
                        ),
                        enabled=True,
                    )
                    interactive_adapter = DiscordRemoteBrowserInteractiveAdapter(
                        runner=runner,
                        store=store,
                        store_current=lambda: getattr(bot, "media_pipeline_store", None),
                        renderer=DiscordAIResponseRenderer(),
                        checkpoint_store=checkpoint_store,
                        runtime_current=lambda: (
                            self._bot is bot
                            and self.interactive_runner is runner
                            and self.interactive_adapter is interactive_adapter
                            and getattr(bot, "browser_rendering_plugin", None) is self
                            and getattr(bot, "remote_browser_run_service", None) is runner
                            and getattr(bot, "browser_run_adapter", None) is interactive_adapter
                            and getattr(bot, "media_pipeline_store", None) is store
                            and interactive_adapter.quarantined is False
                            and not bool(getattr(bot, "is_closing", False))
                        ),
                        quarantine_runtime=lambda: self._quarantine_interactive(bot, interactive_adapter),
                    )
                    interactive_adapter.bind_bot(bot)
                    self.interactive_runner = runner
                    self.interactive_adapter = interactive_adapter
                    bot.remote_browser_run_service = runner
                    bot.browser_run_adapter = interactive_adapter
                    interactive_ready = publish_runtime_readiness_probe(
                        bot,
                        REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID,
                        lambda: (
                            self._bot is bot
                            and self.interactive_runner is runner
                            and self.interactive_adapter is interactive_adapter
                            and getattr(bot, "browser_rendering_plugin", None) is self
                            and getattr(bot, "remote_browser_run_service", None) is runner
                            and getattr(bot, "browser_run_adapter", None) is interactive_adapter
                            and getattr(bot, "media_pipeline_store", None) is store
                            and callable(getattr(getattr(bot, "database", None), "append_audit", None))
                            and interactive_adapter.closing is False
                            and interactive_adapter.quarantined is False
                            and not bool(getattr(bot, "is_closing", False))
                        ),
                    )
                    detail = "構成済み（Browser Run live health未検証）"
                elif availability.available is not True:
                    detail = availability.detail
                elif not isinstance(store, MediaArtifactStore):
                    detail = "Media Artifact Storeが未設定"
                else:
                    detail = "監査保存先が未設定"
                bot.remote_browser_interactive_status = RemoteBrowserRenderingStatus(
                    configured=True,
                    ready=interactive_ready,
                    detail=detail,
                )
            publish_runtime_readiness(bot, {REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID: screenshot_ready})
            if self.interactive_adapter is None:
                publish_runtime_readiness(bot, {REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID: interactive_ready})
        except BaseException:
            await self._cleanup(bot)
            raise

    async def begin_close(self) -> None:
        if self.adapter is not None:
            self.adapter.begin_close()
        if self.interactive_adapter is not None:
            self.interactive_adapter.begin_close()
        bot = self._bot
        if bot is not None:
            publish_runtime_readiness(bot, {REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID: False})
            refresh_runtime_readiness(bot, REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID)

    def _quarantine_interactive(
        self,
        bot: Any,
        adapter: DiscordRemoteBrowserInteractiveAdapter,
    ) -> None:
        if self._bot is not bot or self.interactive_adapter is not adapter:
            return
        bot.remote_browser_interactive_status = RemoteBrowserRenderingStatus(
            configured=True,
            ready=False,
            detail="Browser worker cleanup is unconfirmed; restart is required",
        )
        refresh_runtime_readiness(bot, REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID)

    async def stop(self) -> None:
        bot = self._bot
        if bot is None:
            return
        await self.begin_close()
        await self._cleanup(bot)

    async def _cleanup(self, bot: Any) -> None:
        service = self.service
        adapter, self.adapter = self.adapter, None
        interactive_runner = self.interactive_runner
        interactive_adapter = self.interactive_adapter
        cleanup_complete = False
        try:
            if interactive_adapter is not None:
                await interactive_adapter.close()
            if service is not None:
                await service.close()
            cleanup_complete = True
            self.service = None
            self.interactive_runner = None
            self.interactive_adapter = None
            self._bot = None
        finally:
            try:
                withdraw_runtime_readiness(
                    bot,
                    (
                        REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID,
                        REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID,
                    ),
                )
            finally:
                if getattr(bot, "remote_browser_screenshot_service", None) is service:
                    bot.remote_browser_screenshot_service = None
                if getattr(bot, "browser_rendering_adapter", None) is adapter:
                    bot.browser_rendering_adapter = None
                if getattr(bot, "remote_browser_run_service", None) is interactive_runner:
                    bot.remote_browser_run_service = None
                if getattr(bot, "browser_run_adapter", None) is interactive_adapter:
                    bot.browser_run_adapter = None
                if getattr(bot, "browser_rendering_plugin", None) is self:
                    bot.browser_rendering_plugin = None
                bot.remote_browser_rendering_status = RemoteBrowserRenderingStatus(
                    configured=not cleanup_complete,
                    ready=False,
                    detail="停止" if cleanup_complete else "停止処理未完了",
                )
                bot.remote_browser_interactive_status = RemoteBrowserRenderingStatus(
                    configured=not cleanup_complete,
                    ready=False,
                    detail="停止" if cleanup_complete else "停止処理未完了",
                )


@dataclass(frozen=True, slots=True)
class _Configuration:
    account_id: str
    api_token: str
    timeout_seconds: float
    max_output_bytes: int


@dataclass(frozen=True, slots=True)
class _InteractiveConfiguration:
    account_id: str
    api_token: str
    timeout_seconds: float


def _configuration(settings: object) -> _Configuration | None:
    if not bool(getattr(settings, "cloudflare_browser_rendering_allow_remote", False)):
        return None
    account_id = getattr(settings, "cloudflare_browser_rendering_account_id", None)
    api_token = getattr(settings, "cloudflare_browser_rendering_api_token", None)
    timeout = getattr(settings, "cloudflare_browser_rendering_timeout_seconds", 45.0)
    maximum = getattr(settings, "cloudflare_browser_rendering_max_output_bytes", 8 * 1024 * 1024)
    if (
        not isinstance(account_id, str)
        or re.fullmatch(r"[0-9a-fA-F]{32}", account_id) is None
        or not isinstance(api_token, str)
        or not api_token
        or len(api_token) > 512
        or any(character.isspace() or ord(character) < 33 for character in api_token)
    ):
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= float(timeout) <= 60.0:
        return None
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 * 1024 * 1024 <= maximum <= 8 * 1024 * 1024:
        return None
    return _Configuration(account_id, api_token, float(timeout), maximum)


def _interactive_configuration(settings: object) -> _InteractiveConfiguration | None:
    if not bool(getattr(settings, "cloudflare_browser_run_interactive_enabled", False)):
        return None
    account_id = getattr(settings, "cloudflare_browser_rendering_account_id", None)
    api_token = getattr(settings, "cloudflare_browser_rendering_api_token", None)
    timeout = getattr(settings, "cloudflare_browser_rendering_timeout_seconds", 45.0)
    if (
        not isinstance(account_id, str)
        or re.fullmatch(r"[0-9a-fA-F]{32}", account_id) is None
        or not isinstance(api_token, str)
        or not api_token
        or len(api_token) > 512
        or any(character.isspace() or ord(character) < 33 for character in api_token)
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1.0 <= float(timeout) <= 60.0
    ):
        return None
    return _InteractiveConfiguration(account_id, api_token, float(timeout))


__all__ = [
    "BrowserRenderingPlugin",
    "REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID",
    "REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID",
    "RemoteBrowserRenderingStatus",
    "SocketDnsResolver",
]
