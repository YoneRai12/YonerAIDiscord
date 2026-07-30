"""report-only AutoModのドメイン、設定repository、Discord adapter。"""

from __future__ import annotations

from typing import Any

from .detectors import CrosspostDetector, FloodDetector, KeywordDetector, LinkDetector, MentionDetector
from .domain import (
    AutomodMode,
    DetectionContext,
    EventKind,
    GuildAutomodConfig,
    MessageEvent,
    Policy,
    Severity,
)
from .pipeline import ActionExecutor, ActionPlanner, AutomodPipeline, DetectionEngine, PolicyDecider
from .plugin import AutomodPlugin
from .ports import ModerationPort
from .repository import SqliteAutomodRepository


def setup(registry: Any) -> None:
    """coreの公開登録契約へAutoMod pluginを登録する。"""

    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("automod", AutomodPlugin)


__all__ = [
    "ActionExecutor",
    "ActionPlanner",
    "AutomodMode",
    "AutomodPipeline",
    "AutomodPlugin",
    "CrosspostDetector",
    "DetectionContext",
    "DetectionEngine",
    "EventKind",
    "FloodDetector",
    "GuildAutomodConfig",
    "KeywordDetector",
    "LinkDetector",
    "MentionDetector",
    "MessageEvent",
    "ModerationPort",
    "Policy",
    "PolicyDecider",
    "Severity",
    "SqliteAutomodRepository",
    "setup",
]
