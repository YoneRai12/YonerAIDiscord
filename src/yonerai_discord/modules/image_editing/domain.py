from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from yonerai_discord.modules.image_generation.artifacts import MAX_PNG_BYTES
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    ImageEditingInput,
    ProviderRequest,
    QualityTier,
)


IMAGE_EDITING_MODULE_ID = "media.image-editing"
IMAGE_EDITING_PLUGIN_NAME = "image_editing"
IMAGE_EDITING_CAPABILITY_ID = "cap-run-image-edit"
EDITED_IMAGE_FILENAME = "edited-image.png"
MAX_EDIT_INSTRUCTION_CHARS = 4_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ImageEditingError(RuntimeError):
    pass


class ImageEditingUnavailableError(ImageEditingError):
    pass


class ImageEditingAuthorizationError(ImageEditingError):
    pass


class ImageEditingContractError(ImageEditingError):
    pass


class ImageEditingIdempotencyError(ImageEditingError):
    pass


@dataclass(frozen=True, slots=True)
class ImageEditSource:
    """信頼済みingestionが編集request専用に発行するsource claim。"""

    artifact: ArtifactRef = field(repr=False)
    edit_request_id: str
    guild_id: int
    channel_id: int
    actor_id: int
    source_binding: str = field(repr=False)

    def __post_init__(self) -> None:
        edit_request_id = _identifier(self.edit_request_id, "edit_request_id")
        for name in ("guild_id", "channel_id", "actor_id"):
            _positive_integer(getattr(self, name), name)
        _complete_png_ref(self.artifact, "source")
        if not isinstance(self.source_binding, str) or not _SHA256.fullmatch(self.source_binding):
            raise ValueError("source_binding must be a SHA-256 digest")
        object.__setattr__(self, "edit_request_id", edit_request_id)

    @property
    def claim_digest(self) -> str:
        return hashlib.sha256(
            "\0".join(
                (
                    self.edit_request_id,
                    str(self.guild_id),
                    str(self.channel_id),
                    str(self.actor_id),
                    self.artifact.artifact_id,
                    self.artifact.kind.value,
                    self.artifact.media_type,
                    str(self.artifact.size_bytes),
                    self.artifact.sha256 or "",
                    self.source_binding,
                )
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ImageEditingRequest:
    request_id: str
    guild_id: int
    channel_id: int
    actor_id: int
    instruction: str = field(repr=False)
    source: ImageEditSource = field(repr=False)
    tier: QualityTier = QualityTier.BALANCED

    def __post_init__(self) -> None:
        request_id = _identifier(self.request_id, "request_id")
        for name in ("guild_id", "channel_id", "actor_id"):
            _positive_integer(getattr(self, name), name)
        if not isinstance(self.instruction, str):
            raise TypeError("instruction must be a string")
        instruction = self.instruction.strip()
        if (
            not instruction
            or len(instruction) > MAX_EDIT_INSTRUCTION_CHARS
            or any(
                unicodedata.category(character).startswith("C") and character not in {"\n", "\t"}
                for character in instruction
            )
        ):
            raise ValueError("instruction is outside the allowed range")
        if not isinstance(self.source, ImageEditSource):
            raise TypeError("source must be an ImageEditSource")
        if (
            self.source.edit_request_id != request_id
            or self.source.guild_id != self.guild_id
            or self.source.channel_id != self.channel_id
            or self.source.actor_id != self.actor_id
        ):
            raise ValueError("source claim scope does not match the edit request")
        typed_input = ImageEditingInput(instruction)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "instruction", typed_input.instruction)
        object.__setattr__(self, "tier", QualityTier(self.tier))

    @property
    def actor_ref(self) -> str:
        return f"discord-user-{self.actor_id}"

    @property
    def trace_id(self) -> str:
        return f"trace-{self.request_id}"

    @property
    def instruction_hash(self) -> str:
        return hashlib.sha256(self.instruction.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            "\0".join(
                (
                    self.request_id,
                    str(self.guild_id),
                    str(self.channel_id),
                    str(self.actor_id),
                    self.tier.value,
                    self.instruction_hash,
                    self.source.claim_digest,
                )
            ).encode("utf-8")
        ).hexdigest()

    @property
    def provider_request_id(self) -> str:
        return f"image-edit-{self.fingerprint[:48]}"


@dataclass(frozen=True, slots=True)
class EditedImage:
    artifact: ArtifactRef
    png: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _complete_png_ref(self.artifact, "output")
        if not isinstance(self.png, bytes) or not self.png:
            raise ValueError("png must contain bytes")


def image_edit_output_binding(
    request: ProviderRequest,
    *,
    provider_id: str,
    provider_model: str | None,
    model_alias: str | None,
    quality_tier: QualityTier,
) -> str:
    """編集scope、source、instruction、実provider invocationを新artifactへ束縛する。"""

    if not isinstance(request, ProviderRequest):
        raise TypeError("request must be a ProviderRequest")
    if not isinstance(request.payload, ImageEditingInput):
        raise TypeError("request payload must be an ImageEditingInput")
    if len(request.input_artifacts) != 1:
        raise ValueError("image editing request requires one source artifact")
    source = request.input_artifacts[0]
    payload = "\0".join(
        (
            request.request_id,
            request.trace_id,
            request.actor_ref,
            request.capability.value,
            request.payload.source_binding_digest or "",
            source.artifact_id,
            source.media_type,
            str(source.size_bytes),
            source.sha256 or "",
            provider_id,
            provider_model or "",
            model_alias or "",
            QualityTier(quality_tier).value,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _complete_png_ref(ref: object, label: str) -> None:
    if not isinstance(ref, ArtifactRef):
        raise TypeError(f"{label} must be an ArtifactRef")
    if (
        ref.kind is not ArtifactKind.IMAGE
        or ref.media_type != "image/png"
        or ref.size_bytes is None
        or not 1 <= ref.size_bytes <= MAX_PNG_BYTES
        or ref.sha256 is None
    ):
        raise ValueError(f"{label} must be one complete PNG ArtifactRef")


def _positive_integer(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if (
        not normalized
        or len(normalized) > 120
        or not normalized[0].isalnum()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in normalized)
    ):
        raise ValueError(f"{label} must be a lowercase identifier")
    return normalized


__all__ = [
    "EDITED_IMAGE_FILENAME",
    "IMAGE_EDITING_CAPABILITY_ID",
    "IMAGE_EDITING_MODULE_ID",
    "IMAGE_EDITING_PLUGIN_NAME",
    "MAX_EDIT_INSTRUCTION_CHARS",
    "EditedImage",
    "ImageEditSource",
    "ImageEditingAuthorizationError",
    "ImageEditingContractError",
    "ImageEditingError",
    "ImageEditingIdempotencyError",
    "ImageEditingRequest",
    "ImageEditingUnavailableError",
    "image_edit_output_binding",
]
