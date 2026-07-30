"""YonerAIへ後から登録できる完全local Media Pipeline primitive。"""

from .artifacts import (
    CanonicalMarkdown,
    CanonicalPng,
    CommitCheck,
    MARKDOWN_MEDIA_TYPE,
    MediaArtifactStore,
    PNG_MEDIA_TYPE,
    PNG_SIGNATURE,
    canonicalize_image,
    canonicalize_markdown,
    validate_canonical_markdown,
    validate_canonical_png,
)
from .domain import (
    ArtifactKind,
    ArtifactRef,
    ArtifactScope,
    COMPOSE_GRID_REVISION,
    ComposeGridRequest,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    MAX_MARKDOWN_BYTES,
    MAX_PNG_BYTES,
    MAX_QR_PAYLOAD_BYTES,
    MAX_RECIPE_INPUT_PIXELS,
    MAX_RECIPE_INPUTS,
    MediaAuthorizationError,
    MediaIntegrityError,
    MediaPipelineError,
    MediaValidationError,
    MIN_QR_BORDER,
    PLACE_ON_CANVAS_REVISION,
    PrimitiveId,
    PrimitiveResult,
    PlaceOnCanvasRequest,
    QR_ENCODE_REVISION,
    QrEncodeRequest,
    RgbColor,
    WHITE,
)
from .service import MediaPipelineService
from .plugin import MEDIA_PIPELINE_PLUGIN_NAME, MediaPipelinePlugin


def setup(manager) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register(MEDIA_PIPELINE_PLUGIN_NAME, MediaPipelinePlugin)


__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactScope",
    "COMPOSE_GRID_REVISION",
    "CanonicalMarkdown",
    "CanonicalPng",
    "CommitCheck",
    "ComposeGridRequest",
    "MAX_IMAGE_DIMENSION",
    "MAX_IMAGE_PIXELS",
    "MAX_MARKDOWN_BYTES",
    "MAX_PNG_BYTES",
    "MAX_QR_PAYLOAD_BYTES",
    "MAX_RECIPE_INPUT_PIXELS",
    "MAX_RECIPE_INPUTS",
    "MIN_QR_BORDER",
    "MARKDOWN_MEDIA_TYPE",
    "MediaArtifactStore",
    "MediaAuthorizationError",
    "MediaIntegrityError",
    "MediaPipelineError",
    "MediaPipelineService",
    "MediaPipelinePlugin",
    "MediaValidationError",
    "PLACE_ON_CANVAS_REVISION",
    "PNG_MEDIA_TYPE",
    "PNG_SIGNATURE",
    "PrimitiveId",
    "PrimitiveResult",
    "PlaceOnCanvasRequest",
    "QR_ENCODE_REVISION",
    "QrEncodeRequest",
    "RgbColor",
    "WHITE",
    "canonicalize_image",
    "canonicalize_markdown",
    "validate_canonical_markdown",
    "validate_canonical_png",
    "setup",
]
