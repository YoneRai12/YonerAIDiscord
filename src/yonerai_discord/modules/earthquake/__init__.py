"""P2PQuakeを入力とする、明示購読型の地震・EEW通知モジュール。"""

from __future__ import annotations

from typing import Any

from .adapter import DiscordEarthquakeNotifier, EarthquakeGroup, render_event
from .client import (
    API_BASE_URL,
    DEFAULT_MAX_HISTORY_RESPONSE_BYTES,
    HISTORY_URL,
    WEBSOCKET_URL,
    HistoryResponseTooLargeError,
    P2PQuakeClient,
)
from .domain import (
    EarthquakeEvent,
    EventKind,
    PayloadValidationError,
    parse_event,
    parse_timestamp,
    try_parse_event,
)
from .plugin import EARTHQUAKE_DELIVERY_CAPABILITY_ID, EarthquakePlugin
from .repository import GuildSubscription, SqliteEarthquakeRepository
from .service import (
    EarthquakeFeedWorker,
    EarthquakeService,
    EarthquakeSnapshot,
    EventDeduplicator,
    ExponentialBackoff,
)


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register("earthquake", EarthquakePlugin)


__all__ = [
    "API_BASE_URL",
    "DEFAULT_MAX_HISTORY_RESPONSE_BYTES",
    "EARTHQUAKE_DELIVERY_CAPABILITY_ID",
    "HISTORY_URL",
    "WEBSOCKET_URL",
    "DiscordEarthquakeNotifier",
    "EarthquakeEvent",
    "EarthquakeFeedWorker",
    "EarthquakeGroup",
    "EarthquakePlugin",
    "EarthquakeService",
    "EarthquakeSnapshot",
    "EventDeduplicator",
    "EventKind",
    "ExponentialBackoff",
    "GuildSubscription",
    "HistoryResponseTooLargeError",
    "P2PQuakeClient",
    "PayloadValidationError",
    "SqliteEarthquakeRepository",
    "parse_event",
    "parse_timestamp",
    "render_event",
    "setup",
    "try_parse_event",
]
