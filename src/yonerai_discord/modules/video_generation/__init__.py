"""既定OFF・provider未接続の動画生成Stage 1 module。"""

from __future__ import annotations

from typing import Any

from .adapter import DiscordVideoGenerationAdapter, VideoGroup
from .artifacts import (
    ValidatedMp4,
    VideoArtifactAuthorizationError,
    VideoArtifactError,
    VideoArtifactStore,
    VideoArtifactValidationError,
    validate_mp4,
)
from .domain import (
    GENERATED_VIDEO_FILENAME,
    VIDEO_GENERATION_CAPABILITY_ID,
    VIDEO_GENERATION_COMMAND_PATH,
    VIDEO_GENERATION_MODULE_ID,
    VIDEO_GENERATION_PLUGIN_NAME,
    GeneratedVideo,
    VideoGenerationAuthorizationError,
    VideoGenerationContractError,
    VideoGenerationError,
    VideoGenerationIdempotencyError,
    VideoGenerationRequest,
    VideoGenerationUnavailableError,
)
from .plugin import VideoGenerationPlugin
from .service import VideoGenerationService


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register(VIDEO_GENERATION_PLUGIN_NAME, VideoGenerationPlugin)


__all__ = [
    "GENERATED_VIDEO_FILENAME",
    "VIDEO_GENERATION_CAPABILITY_ID",
    "VIDEO_GENERATION_COMMAND_PATH",
    "VIDEO_GENERATION_MODULE_ID",
    "VIDEO_GENERATION_PLUGIN_NAME",
    "ValidatedMp4",
    "DiscordVideoGenerationAdapter",
    "GeneratedVideo",
    "VideoArtifactAuthorizationError",
    "VideoArtifactError",
    "VideoArtifactStore",
    "VideoArtifactValidationError",
    "VideoGenerationAuthorizationError",
    "VideoGenerationContractError",
    "VideoGenerationError",
    "VideoGenerationIdempotencyError",
    "VideoGenerationPlugin",
    "VideoGenerationRequest",
    "VideoGenerationService",
    "VideoGenerationUnavailableError",
    "VideoGroup",
    "setup",
    "validate_mp4",
]
