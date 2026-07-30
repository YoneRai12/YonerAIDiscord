from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field

from yonerai_discord.provider_registry import ArtifactRef, ProviderRequest, QualityTier


VIDEO_GENERATION_MODULE_ID = "media.video-generation"
VIDEO_GENERATION_PLUGIN_NAME = "video_generation"
VIDEO_GENERATION_CAPABILITY_ID = "cap-run-video-generate"
VIDEO_GENERATION_COMMAND_PATH = "video generate"
GENERATED_VIDEO_FILENAME = "generated-video.mp4"


class VideoGenerationError(RuntimeError):
    pass


class VideoGenerationUnavailableError(VideoGenerationError):
    pass


class VideoGenerationAuthorizationError(VideoGenerationError):
    pass


class VideoGenerationContractError(VideoGenerationError):
    pass


class VideoGenerationIdempotencyError(VideoGenerationError):
    pass


@dataclass(frozen=True, slots=True)
class VideoGenerationRequest:
    request_id: str
    guild_id: int
    channel_id: int
    actor_id: int
    prompt: str = field(repr=False)
    tier: QualityTier = QualityTier.BALANCED

    def __post_init__(self) -> None:
        request_id = _identifier(self.request_id, "request_id")
        for name in ("guild_id", "channel_id", "actor_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be a string")
        prompt = self.prompt.strip()
        if (
            not prompt
            or len(prompt) > 4_000
            or any(
                unicodedata.category(character).startswith("C") and character not in {"\n", "\t"}
                for character in prompt
            )
        ):
            raise ValueError("prompt is outside the allowed range")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "tier", QualityTier(self.tier))

    @property
    def trace_id(self) -> str:
        return f"trace-{self.request_id}"

    @property
    def actor_ref(self) -> str:
        return f"discord-user-{self.actor_id}"

    @property
    def fingerprint(self) -> str:
        payload = "\0".join(
            (
                self.request_id,
                str(self.guild_id),
                str(self.channel_id),
                str(self.actor_id),
                self.tier.value,
                self.prompt,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def provider_request_id(self) -> str:
        return f"video-request-{self.fingerprint[:48]}"


@dataclass(frozen=True, slots=True)
class GeneratedVideo:
    artifact: ArtifactRef
    mp4: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ArtifactRef):
            raise TypeError("artifact must be an ArtifactRef")
        if not isinstance(self.mp4, bytes) or not self.mp4:
            raise ValueError("mp4 must contain bytes")


def video_artifact_request_binding(
    request: ProviderRequest,
    *,
    provider_id: str,
    provider_model: str | None,
    model_alias: str | None,
    quality_tier: QualityTier,
) -> str:
    """Artifactをrequest/actor/scope由来IDと実provider invocationへ束縛する。"""

    if not isinstance(request, ProviderRequest):
        raise TypeError("request must be a ProviderRequest")
    payload = "\0".join(
        (
            request.request_id,
            request.trace_id,
            request.actor_ref,
            request.capability.value,
            provider_id,
            provider_model or "",
            model_alias or "",
            QualityTier(quality_tier).value,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    "GENERATED_VIDEO_FILENAME",
    "VIDEO_GENERATION_CAPABILITY_ID",
    "VIDEO_GENERATION_COMMAND_PATH",
    "VIDEO_GENERATION_MODULE_ID",
    "VIDEO_GENERATION_PLUGIN_NAME",
    "GeneratedVideo",
    "VideoGenerationAuthorizationError",
    "VideoGenerationContractError",
    "VideoGenerationError",
    "VideoGenerationIdempotencyError",
    "VideoGenerationRequest",
    "VideoGenerationUnavailableError",
    "video_artifact_request_binding",
]
