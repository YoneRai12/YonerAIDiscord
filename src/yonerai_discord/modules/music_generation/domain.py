from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from yonerai_discord.provider_registry import ArtifactRef, ProviderRequest, QualityTier

MUSIC_GENERATION_MODULE_ID = "media.music-generation"
MUSIC_GENERATION_PLUGIN_NAME = "music_generation"
MUSIC_GENERATION_CAPABILITY_ID = "cap-run-music-generate"
MUSIC_GENERATION_COMMAND_PATH = "musicgen generate"
GENERATED_MUSIC_FILENAME = "generated-music.wav"
MUSIC_PROFILE_REVISION = "original-instrumental-preview.v1"
MUSIC_RIGHTS_REVISION = "rights-confirmed.v1"


class MusicGenerationError(RuntimeError):
    pass


class MusicGenerationUnavailableError(MusicGenerationError):
    pass


class MusicGenerationAuthorizationError(MusicGenerationError):
    pass


class MusicGenerationContractError(MusicGenerationError):
    pass


class MusicGenerationIdempotencyError(MusicGenerationError):
    pass


class MusicGenerationPolicyError(MusicGenerationError):
    pass


_DISALLOWED = re.compile(
    r"\b(?:lyrics?|vocals?|voice\s*(?:sample|clone)?|cover|remix|reference\s*audio)\b",
    re.IGNORECASE,
)
_DISALLOWED_JAPANESE = (
    "歌詞",
    "ボーカル",
    "ヴォーカル",
    "ボイス",
    "歌声",
    "歌入り",
    "声入り",
    "歌って",
    "歌う",
    "声サンプル",
    "音声サンプル",
    "ボイスクローン",
    "声クローン",
    "声をクローン",
    "カバー",
    "リミックス",
    "編曲",
    "参照音声",
    "参考音声",
)


@dataclass(frozen=True, slots=True)
class MusicGenerationRequest:
    request_id: str
    guild_id: int
    channel_id: int
    actor_id: int
    prompt: str = field(repr=False)
    duration_seconds: int = 15
    tier: QualityTier = QualityTier.BALANCED
    rights_confirmed: bool = False
    profile_revision: str = MUSIC_PROFILE_REVISION
    rights_revision: str = MUSIC_RIGHTS_REVISION

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
            or any(unicodedata.category(c).startswith("C") and c not in {"\n", "\t"} for c in prompt)
        ):
            raise ValueError("prompt is outside the allowed range")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, int)
            or not 1 <= self.duration_seconds <= 30
        ):
            raise ValueError("duration_seconds must be between 1 and 30")
        if self.rights_confirmed is not True:
            raise MusicGenerationPolicyError("rights_confirmed is required")
        normalized_prompt = unicodedata.normalize("NFKC", prompt).casefold()
        if _DISALLOWED.search(normalized_prompt) or any(marker in normalized_prompt for marker in _DISALLOWED_JAPANESE):
            raise MusicGenerationPolicyError("only original instrumental previews are supported")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "tier", QualityTier(self.tier))
        profile_revision = _revision(self.profile_revision, "profile_revision")
        rights_revision = _revision(self.rights_revision, "rights_revision")
        if profile_revision != MUSIC_PROFILE_REVISION or rights_revision != MUSIC_RIGHTS_REVISION:
            raise MusicGenerationPolicyError("music profile or rights policy revision is unsupported")
        object.__setattr__(self, "profile_revision", profile_revision)
        object.__setattr__(self, "rights_revision", rights_revision)

    @property
    def trace_id(self) -> str:
        return f"trace-{self.request_id}"

    @property
    def actor_ref(self) -> str:
        return f"discord-user-{self.actor_id}"

    @property
    def prompt_hash(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            "\0".join(
                (
                    self.request_id,
                    str(self.guild_id),
                    str(self.channel_id),
                    str(self.actor_id),
                    self.prompt_hash,
                    str(self.duration_seconds),
                    self.tier.value,
                    self.profile_revision,
                    self.rights_revision,
                )
            ).encode()
        ).hexdigest()

    @property
    def provider_request_id(self) -> str:
        return f"music-request-{self.fingerprint[:48]}"


@dataclass(frozen=True, slots=True)
class GeneratedMusic:
    artifact: ArtifactRef
    wav: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ArtifactRef):
            raise TypeError("artifact must be an ArtifactRef")
        if not isinstance(self.wav, bytes) or not self.wav:
            raise ValueError("wav must contain bytes")


def music_artifact_request_binding(
    request: ProviderRequest,
    *,
    provider_id: str,
    provider_model: str | None,
    model_alias: str | None,
    quality_tier: QualityTier,
) -> str:
    """Service発行request IDと実provider invocationをartifactへ束縛する。

    music用のprovider request IDは、元requestのactor/guild/channel、prompt hash、
    duration、tier、profile/rights revisionを含むfingerprintから導出される。
    Adapterは生promptやscope IDを複製せず、このopaque IDとinvocationだけで同じ
    bindingを再現する。
    """

    if not isinstance(request, ProviderRequest):
        raise TypeError("request must be a ProviderRequest")
    return hashlib.sha256(
        "\0".join(
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
        ).encode()
    ).hexdigest()


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    value = value.strip().lower()
    if (
        not value
        or len(value) > 120
        or not value[0].isalnum()
        or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for c in value)
    ):
        raise ValueError(f"{label} must be a lowercase identifier")
    return value


def _revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise ValueError(f"{label} is invalid")
    return value.strip()
