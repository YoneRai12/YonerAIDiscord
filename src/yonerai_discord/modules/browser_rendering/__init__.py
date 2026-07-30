"""Optional remote browser-rendering surface; disabled until explicitly configured."""

from __future__ import annotations

from typing import Any

from .discord_adapter import (
    DiscordRemoteBrowserScreenshotAdapter,
    RemoteBrowserScreenshotInputError,
    RemoteBrowserScreenshotRequest,
)
from .interactive_adapter import BrowserInteractiveRequest, DiscordRemoteBrowserInteractiveAdapter
from .plugin import (
    REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID,
    REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID,
    BrowserRenderingPlugin,
    RemoteBrowserRenderingStatus,
    SocketDnsResolver,
)


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register("browser_rendering", BrowserRenderingPlugin)


__all__ = [
    "BrowserRenderingPlugin",
    "BrowserInteractiveRequest",
    "DiscordRemoteBrowserInteractiveAdapter",
    "DiscordRemoteBrowserScreenshotAdapter",
    "REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID",
    "REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID",
    "RemoteBrowserRenderingStatus",
    "RemoteBrowserScreenshotInputError",
    "RemoteBrowserScreenshotRequest",
    "SocketDnsResolver",
    "setup",
]
