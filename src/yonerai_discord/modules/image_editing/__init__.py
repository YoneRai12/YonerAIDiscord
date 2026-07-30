"""既定OFF・provider/ingestion未接続の画像編集Stage 1 module。"""

from __future__ import annotations

from typing import Any

from .adapter import ImageEditingDelivery
from .domain import (
    EDITED_IMAGE_FILENAME,
    IMAGE_EDITING_CAPABILITY_ID,
    IMAGE_EDITING_MODULE_ID,
    IMAGE_EDITING_PLUGIN_NAME,
    MAX_EDIT_INSTRUCTION_CHARS,
    EditedImage,
    ImageEditSource,
    ImageEditingAuthorizationError,
    ImageEditingContractError,
    ImageEditingError,
    ImageEditingIdempotencyError,
    ImageEditingRequest,
    ImageEditingUnavailableError,
    image_edit_output_binding,
)
from .plugin import ImageEditingPlugin
from .provider_openai import ImageEditSourceBytesPort, OpenAIImageProviderAdapter
from .service import ImageEditingService
from .source_claims import (
    DEFAULT_MAX_SOURCE_CLAIMS,
    MAX_SOURCE_CLAIMS,
    ImageEditSourceClaimIssuer,
    SourceClaimAuthorizationCheck,
)


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register(IMAGE_EDITING_PLUGIN_NAME, ImageEditingPlugin)


__all__ = [
    "EDITED_IMAGE_FILENAME",
    "DEFAULT_MAX_SOURCE_CLAIMS",
    "IMAGE_EDITING_CAPABILITY_ID",
    "IMAGE_EDITING_MODULE_ID",
    "IMAGE_EDITING_PLUGIN_NAME",
    "MAX_EDIT_INSTRUCTION_CHARS",
    "MAX_SOURCE_CLAIMS",
    "EditedImage",
    "ImageEditSource",
    "ImageEditSourceClaimIssuer",
    "ImageEditingAuthorizationError",
    "ImageEditingContractError",
    "ImageEditingDelivery",
    "ImageEditingError",
    "ImageEditingIdempotencyError",
    "ImageEditingPlugin",
    "ImageEditingRequest",
    "ImageEditingService",
    "ImageEditingUnavailableError",
    "ImageEditSourceBytesPort",
    "OpenAIImageProviderAdapter",
    "SourceClaimAuthorizationCheck",
    "image_edit_output_binding",
    "setup",
]
