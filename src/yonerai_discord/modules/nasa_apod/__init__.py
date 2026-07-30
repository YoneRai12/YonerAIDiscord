"""既定OFFのread-only NASA APOD module。"""

from __future__ import annotations

from typing import Any

from .adapter import (
    NASA_APOD_CAPABILITY_ID,
    DiscordNasaApodAdapter,
    NasaGroup,
    render_apod_embed,
    render_apod_text,
)
from .domain import (
    APOD_FIRST_DATE,
    APOD_SOURCE_BASE_URL,
    ApodItem,
    canonicalize_apod_item,
    parse_apod_payload,
    validate_apod_request_date,
)
from .errors import (
    ApodConfigurationError,
    ApodDateError,
    ApodNotFoundError,
    ApodRateLimitedError,
    ApodResponseError,
    ApodResponseTooLargeError,
    ApodTransportError,
    NasaApodError,
)
from .plugin import NASA_APOD_PLUGIN_NAME, NasaApodPlugin
from .service import NasaApodService
from .source import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    DEFAULT_TOTAL_TIMEOUT_SECONDS,
    MAX_APOD_RESPONSE_BYTES,
    NASA_APOD_ENDPOINT,
    ApodSource,
    NasaApiApodSource,
    StaticApodSource,
)


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register(NASA_APOD_PLUGIN_NAME, NasaApodPlugin)


__all__ = [
    "APOD_FIRST_DATE",
    "APOD_SOURCE_BASE_URL",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "DEFAULT_TOTAL_TIMEOUT_SECONDS",
    "MAX_APOD_RESPONSE_BYTES",
    "NASA_APOD_CAPABILITY_ID",
    "NASA_APOD_ENDPOINT",
    "NASA_APOD_PLUGIN_NAME",
    "ApodConfigurationError",
    "ApodDateError",
    "ApodItem",
    "ApodNotFoundError",
    "ApodRateLimitedError",
    "ApodResponseError",
    "ApodResponseTooLargeError",
    "ApodSource",
    "ApodTransportError",
    "DiscordNasaApodAdapter",
    "NasaApiApodSource",
    "NasaApodError",
    "NasaApodPlugin",
    "NasaApodService",
    "NasaGroup",
    "StaticApodSource",
    "canonicalize_apod_item",
    "parse_apod_payload",
    "render_apod_embed",
    "render_apod_text",
    "setup",
    "validate_apod_request_date",
]
