"""既定OFF・provider未接続の画像生成Stage 1 module。"""

from __future__ import annotations

from typing import Any

from .adapter import DiscordImageGenerationAdapter, ImageGroup
from .artifacts import (
    CanonicalPng,
    ImageArtifactAuthorizationError,
    ImageArtifactError,
    ImageArtifactStore,
    ImageArtifactValidationError,
    canonicalize_png,
)
from .domain import (
    GENERATED_IMAGE_FILENAME,
    IMAGE_GENERATION_CAPABILITY_ID,
    IMAGE_GENERATION_COMMAND_PATH,
    IMAGE_GENERATION_MODULE_ID,
    IMAGE_GENERATION_PLUGIN_NAME,
    GeneratedImage,
    ImageGenerationAuthorizationError,
    ImageGenerationContractError,
    ImageGenerationError,
    ImageGenerationIdempotencyError,
    ImageGenerationRequest,
    ImageGenerationUnavailableError,
)
from .plugin import ImageGenerationPlugin
from .provider_composition import (
    OPENAI_IMAGE_MODEL,
    OPENAI_IMAGE_MODEL_ALIASES,
    OpenAIImageRuntime,
    compose_openai_image_runtime,
)
from .provider_openai import (
    AiohttpOpenAIImageTransport,
    OPENAI_IMAGES_PROVIDER_ID,
    OpenAIImageProviderAdapter,
)
from .service import ImageGenerationService


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register(IMAGE_GENERATION_PLUGIN_NAME, ImageGenerationPlugin)


__all__ = [
    "GENERATED_IMAGE_FILENAME",
    "IMAGE_GENERATION_CAPABILITY_ID",
    "IMAGE_GENERATION_COMMAND_PATH",
    "IMAGE_GENERATION_MODULE_ID",
    "IMAGE_GENERATION_PLUGIN_NAME",
    "CanonicalPng",
    "DiscordImageGenerationAdapter",
    "GeneratedImage",
    "ImageArtifactAuthorizationError",
    "ImageArtifactError",
    "ImageArtifactStore",
    "ImageArtifactValidationError",
    "ImageGenerationAuthorizationError",
    "ImageGenerationContractError",
    "ImageGenerationError",
    "ImageGenerationIdempotencyError",
    "ImageGenerationPlugin",
    "ImageGenerationRequest",
    "ImageGenerationService",
    "ImageGenerationUnavailableError",
    "ImageGroup",
    "OPENAI_IMAGES_PROVIDER_ID",
    "OPENAI_IMAGE_MODEL",
    "OPENAI_IMAGE_MODEL_ALIASES",
    "AiohttpOpenAIImageTransport",
    "OpenAIImageProviderAdapter",
    "OpenAIImageRuntime",
    "canonicalize_png",
    "compose_openai_image_runtime",
    "setup",
]
