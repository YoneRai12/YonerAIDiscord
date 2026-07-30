from __future__ import annotations
from typing import Any

from .adapter import DiscordMusicGenerationAdapter, MusicGenGroup
from .artifacts import (
    MusicArtifactAuthorizationError,
    MusicArtifactError,
    MusicArtifactStore,
    MusicArtifactValidationError,
    ValidatedWav,
    validate_wav,
)
from .domain import (
    GENERATED_MUSIC_FILENAME,
    MUSIC_GENERATION_CAPABILITY_ID,
    MUSIC_GENERATION_COMMAND_PATH,
    MUSIC_GENERATION_MODULE_ID,
    MUSIC_GENERATION_PLUGIN_NAME,
    GeneratedMusic,
    MusicGenerationAuthorizationError,
    MusicGenerationContractError,
    MusicGenerationError,
    MusicGenerationIdempotencyError,
    MusicGenerationPolicyError,
    MusicGenerationRequest,
    MusicGenerationUnavailableError,
)
from .plugin import MusicGenerationPlugin
from .service import MusicGenerationService


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register(MUSIC_GENERATION_PLUGIN_NAME, MusicGenerationPlugin)


__all__ = [
    "DiscordMusicGenerationAdapter",
    "GENERATED_MUSIC_FILENAME",
    "GeneratedMusic",
    "MUSIC_GENERATION_CAPABILITY_ID",
    "MUSIC_GENERATION_COMMAND_PATH",
    "MUSIC_GENERATION_MODULE_ID",
    "MUSIC_GENERATION_PLUGIN_NAME",
    "MusicGenGroup",
    "MusicArtifactAuthorizationError",
    "MusicArtifactError",
    "MusicArtifactStore",
    "MusicArtifactValidationError",
    "MusicGenerationAuthorizationError",
    "MusicGenerationContractError",
    "MusicGenerationError",
    "MusicGenerationIdempotencyError",
    "MusicGenerationPolicyError",
    "MusicGenerationPlugin",
    "MusicGenerationRequest",
    "MusicGenerationService",
    "MusicGenerationUnavailableError",
    "ValidatedWav",
    "setup",
    "validate_wav",
]
