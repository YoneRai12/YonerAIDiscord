"""公開YouTube URLを直接理解するdefault-off remote provider module。"""

from __future__ import annotations

from typing import Any

from .discord_adapter import AuthorizationCurrent, DiscordMediaInspectionAdapter, MediaInspectionProvider
from .domain import (
    GEMINI_INTERACTIONS_ENDPOINT,
    GEMINI_MEDIA_INSPECTION_MODEL,
    MAX_INSPECTION_OUTPUT_CHARS,
    MEDIA_URL_INSPECTION_CAPABILITY_ID,
    MEDIA_URL_INSPECTION_MODULE_ID,
    MEDIA_URL_INSPECTION_PLUGIN_NAME,
    MediaInspectionError,
    MediaInspectionInputError,
    MediaInspectionRequest,
    MediaInspectionResponseError,
    MediaInspectionResult,
    MediaInspectionUnavailableError,
)
from .hyperv_provider import HyperVMediaInspectionProvider
from .hyperv_transport import HyperVMediaInspectionTransport
from .plugin import MediaInspectionPlugin, MediaInspectionStatus
from .provider import GeminiMediaInspectionProvider
from .urls import canonicalize_youtube_url


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register(MEDIA_URL_INSPECTION_PLUGIN_NAME, MediaInspectionPlugin)


__all__ = [
    "AuthorizationCurrent",
    "DiscordMediaInspectionAdapter",
    "GEMINI_INTERACTIONS_ENDPOINT",
    "GEMINI_MEDIA_INSPECTION_MODEL",
    "GeminiMediaInspectionProvider",
    "HyperVMediaInspectionProvider",
    "HyperVMediaInspectionTransport",
    "MAX_INSPECTION_OUTPUT_CHARS",
    "MEDIA_URL_INSPECTION_CAPABILITY_ID",
    "MEDIA_URL_INSPECTION_MODULE_ID",
    "MEDIA_URL_INSPECTION_PLUGIN_NAME",
    "MediaInspectionError",
    "MediaInspectionInputError",
    "MediaInspectionPlugin",
    "MediaInspectionProvider",
    "MediaInspectionRequest",
    "MediaInspectionResponseError",
    "MediaInspectionResult",
    "MediaInspectionStatus",
    "MediaInspectionUnavailableError",
    "canonicalize_youtube_url",
    "setup",
]
