"""実効policyで絞り込んだread-only command discovery。"""

from __future__ import annotations

from typing import Any

from .adapter import DiscoveryPlugin
from .domain import CommandEntry, CommandPage
from .service import DiscoveryService, render_command_page


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register("discovery", DiscoveryPlugin)


__all__ = [
    "CommandEntry",
    "CommandPage",
    "DiscoveryPlugin",
    "DiscoveryService",
    "render_command_page",
    "setup",
]
