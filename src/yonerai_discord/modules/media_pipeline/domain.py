"""完全localなMedia Pipeline primitiveの不変契約。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from itertools import islice


MAX_IMAGE_DIMENSION = 4_096
MAX_IMAGE_PIXELS = 16_777_216
MAX_PNG_BYTES = 8 * 1024 * 1024
MAX_MARKDOWN_BYTES = 1 * 1024 * 1024
MAX_RECIPE_INPUTS = 8
MAX_RECIPE_INPUT_PIXELS = MAX_IMAGE_PIXELS
MAX_QR_PAYLOAD_BYTES = 2_048
MIN_QR_BORDER = 4
MAX_QR_BORDER = 32
MAX_QR_SCALE = 64

QR_ENCODE_REVISION = "1"
PLACE_ON_CANVAS_REVISION = "1"
COMPOSE_GRID_REVISION = "1"

_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HEX_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_ARTIFACT_ID = re.compile(r"mp-[a-f0-9]{64}\Z")
_MAX_DISCORD_ID = (1 << 64) - 1


class MediaPipelineError(RuntimeError):
    """Media Pipeline操作を安全に完了できなかった。"""


class MediaValidationError(MediaPipelineError, ValueError):
    """入力または生成物が固定契約を満たしていない。"""


class MediaAuthorizationError(MediaPipelineError):
    """scopeまたはcommit権限が失効した。"""


class MediaIntegrityError(MediaPipelineError):
    """保存済みartifactの完全性を証明できない。"""


class ArtifactKind(StrEnum):
    QR_CODE = "qr_code"
    IMAGE = "image"
    DOCUMENT = "document"


class PrimitiveId(StrEnum):
    QR_ENCODE = "qr.encode"
    IMAGE_PLACE_ON_CANVAS = "image.place_on_canvas"
    IMAGE_COMPOSE_GRID = "image.compose_grid"


@dataclass(frozen=True, slots=True)
class ArtifactScope:
    request_id: str
    guild_id: int | None
    channel_id: int
    user_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not _REQUEST_ID.fullmatch(self.request_id):
            raise MediaValidationError("request_id is outside the bounded identifier contract")
        _validate_discord_id(self.channel_id, "channel_id")
        _validate_discord_id(self.user_id, "user_id")
        if self.guild_id is not None:
            _validate_discord_id(self.guild_id, "guild_id")

    @property
    def digest(self) -> str:
        payload = json.dumps(
            {
                "channel_id": self.channel_id,
                "guild_id": self.guild_id,
                "request_id": self.request_id,
                "user_id": self.user_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(b"yonerai.media.scope.v1\0" + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RgbColor:
    red: int
    green: int
    blue: int

    def __post_init__(self) -> None:
        for name, value in (("red", self.red), ("green", self.green), ("blue", self.blue)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                raise MediaValidationError(f"{name} must be an integer from 0 to 255")

    @property
    def tuple(self) -> tuple[int, int, int]:
        return (self.red, self.green, self.blue)


WHITE = RgbColor(255, 255, 255)


@dataclass(frozen=True, slots=True, repr=False)
class ArtifactRef:
    artifact_id: str
    scope_digest: str
    recipe_digest: str
    content_digest: str
    kind: ArtifactKind
    width: int
    height: int
    byte_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, str) or not _ARTIFACT_ID.fullmatch(self.artifact_id):
            raise MediaValidationError("artifact_id is not an opaque Media Pipeline identifier")
        for name, digest in (
            ("scope_digest", self.scope_digest),
            ("recipe_digest", self.recipe_digest),
            ("content_digest", self.content_digest),
        ):
            if not isinstance(digest, str) or not _HEX_DIGEST.fullmatch(digest):
                raise MediaValidationError(f"{name} must be a lowercase SHA-256 digest")
        if not isinstance(self.kind, ArtifactKind):
            raise MediaValidationError("kind must be an ArtifactKind")
        if self.kind is ArtifactKind.DOCUMENT:
            if self.width != 0 or self.height != 0:
                raise MediaValidationError("document dimensions must be zero")
            byte_limit = MAX_MARKDOWN_BYTES
            limit_name = "Markdown"
        else:
            validate_image_dimensions(self.width, self.height)
            byte_limit = MAX_PNG_BYTES
            limit_name = "PNG"
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or not 1 <= self.byte_size <= byte_limit
        ):
            raise MediaValidationError(f"byte_size is outside the {limit_name} limit")


@dataclass(frozen=True, slots=True)
class QrEncodeRequest:
    scope: ArtifactScope
    payload: str
    scale: int = 8
    border: int = MIN_QR_BORDER

    def __post_init__(self) -> None:
        _validate_scope(self.scope)
        if not isinstance(self.payload, str):
            raise MediaValidationError("QR payload must be text")
        try:
            encoded = self.payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise MediaValidationError("QR payload must be valid UTF-8 text") from exc
        if not encoded or len(encoded) > MAX_QR_PAYLOAD_BYTES:
            raise MediaValidationError("QR payload exceeds the bounded UTF-8 limit")
        _validate_int_range(self.scale, "scale", 1, MAX_QR_SCALE)
        _validate_int_range(self.border, "border", MIN_QR_BORDER, MAX_QR_BORDER)


@dataclass(frozen=True, slots=True)
class PlaceOnCanvasRequest:
    scope: ArtifactScope
    source: ArtifactRef
    canvas_width: int
    canvas_height: int
    background: RgbColor = WHITE
    x: int | None = None
    y: int | None = None

    def __post_init__(self) -> None:
        _validate_scope(self.scope)
        _validate_ref(self.source)
        if self.source.kind is ArtifactKind.DOCUMENT:
            raise MediaValidationError("source must be an image artifact")
        validate_image_dimensions(self.canvas_width, self.canvas_height)
        if not isinstance(self.background, RgbColor):
            raise MediaValidationError("background must be an RgbColor")
        _validate_optional_position(self.x, "x")
        _validate_optional_position(self.y, "y")
        resolved_x = (self.canvas_width - self.source.width) // 2 if self.x is None else self.x
        resolved_y = (self.canvas_height - self.source.height) // 2 if self.y is None else self.y
        if resolved_x < 0 or resolved_y < 0:
            raise MediaValidationError("source image does not fit the canvas")
        if resolved_x + self.source.width > self.canvas_width or resolved_y + self.source.height > self.canvas_height:
            raise MediaValidationError("source position exceeds the canvas")


@dataclass(frozen=True, slots=True)
class ComposeGridRequest:
    scope: ArtifactScope
    sources: tuple[ArtifactRef, ...]
    canvas_width: int
    canvas_height: int
    columns: int
    background: RgbColor = WHITE
    padding: int = 16
    gap: int = 16

    def __post_init__(self) -> None:
        _validate_scope(self.scope)
        if not isinstance(self.sources, tuple):
            try:
                bounded_sources = tuple(islice(iter(self.sources), MAX_RECIPE_INPUTS + 1))
            except TypeError as exc:
                raise MediaValidationError("sources must be an immutable artifact sequence") from exc
            object.__setattr__(self, "sources", bounded_sources)
        if not 1 <= len(self.sources) <= MAX_RECIPE_INPUTS:
            raise MediaValidationError("grid input count is outside the recipe limit")
        if any(not isinstance(ref, ArtifactRef) for ref in self.sources):
            raise MediaValidationError("grid inputs must contain only ArtifactRef values")
        if any(ref.kind is ArtifactKind.DOCUMENT for ref in self.sources):
            raise MediaValidationError("grid inputs must contain only image artifacts")
        validate_image_dimensions(self.canvas_width, self.canvas_height)
        _validate_int_range(self.columns, "columns", 1, len(self.sources))
        _validate_int_range(self.padding, "padding", 0, MAX_IMAGE_DIMENSION)
        _validate_int_range(self.gap, "gap", 0, MAX_IMAGE_DIMENSION)
        if not isinstance(self.background, RgbColor):
            raise MediaValidationError("background must be an RgbColor")
        rows = (len(self.sources) + self.columns - 1) // self.columns
        inner_width = self.canvas_width - (2 * self.padding) - (self.gap * (self.columns - 1))
        inner_height = self.canvas_height - (2 * self.padding) - (self.gap * (rows - 1))
        if inner_width < self.columns or inner_height < rows:
            raise MediaValidationError("grid cells have no drawable area")
        if sum(ref.width * ref.height for ref in self.sources) > MAX_RECIPE_INPUT_PIXELS:
            raise MediaValidationError("grid input pixels exceed the recipe limit")


@dataclass(frozen=True, slots=True)
class PrimitiveResult:
    primitive_id: PrimitiveId
    operation_revision: str
    artifact: ArtifactRef
    inputs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.primitive_id, PrimitiveId):
            raise MediaValidationError("primitive_id must be a PrimitiveId")
        if not isinstance(self.operation_revision, str) or not self.operation_revision.isdigit():
            raise MediaValidationError("operation_revision must be a numeric string")
        _validate_ref(self.artifact)
        if not isinstance(self.inputs, tuple) or any(not isinstance(ref, ArtifactRef) for ref in self.inputs):
            raise MediaValidationError("inputs must be an immutable ArtifactRef tuple")


def validate_image_dimensions(width: int, height: int) -> None:
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_IMAGE_DIMENSION:
            raise MediaValidationError(f"{name} is outside the image dimension limit")
    if width * height > MAX_IMAGE_PIXELS:
        raise MediaValidationError("image pixel count exceeds the limit")


def _validate_scope(scope: ArtifactScope) -> None:
    if not isinstance(scope, ArtifactScope):
        raise MediaValidationError("scope must be an ArtifactScope")


def _validate_ref(ref: ArtifactRef) -> None:
    if not isinstance(ref, ArtifactRef):
        raise MediaValidationError("source must be an ArtifactRef")


def _validate_int_range(value: int, name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise MediaValidationError(f"{name} is outside the allowed range")


def _validate_optional_position(value: int | None, name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise MediaValidationError(f"{name} must be a non-negative integer or None")


def _validate_discord_id(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_DISCORD_ID:
        raise MediaValidationError(f"{name} must be a positive Discord identifier")


__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactScope",
    "COMPOSE_GRID_REVISION",
    "ComposeGridRequest",
    "MAX_IMAGE_DIMENSION",
    "MAX_IMAGE_PIXELS",
    "MAX_MARKDOWN_BYTES",
    "MAX_PNG_BYTES",
    "MAX_QR_PAYLOAD_BYTES",
    "MAX_RECIPE_INPUT_PIXELS",
    "MAX_RECIPE_INPUTS",
    "MediaAuthorizationError",
    "MediaIntegrityError",
    "MediaPipelineError",
    "MediaValidationError",
    "MIN_QR_BORDER",
    "PLACE_ON_CANVAS_REVISION",
    "PrimitiveId",
    "PrimitiveResult",
    "PlaceOnCanvasRequest",
    "QR_ENCODE_REVISION",
    "QrEncodeRequest",
    "RgbColor",
    "WHITE",
    "validate_image_dimensions",
]
