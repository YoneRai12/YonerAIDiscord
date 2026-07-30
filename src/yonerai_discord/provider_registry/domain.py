from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TypeAlias
from urllib.parse import urlparse

from yonerai_discord.control_plane import RbacLevel, RiskLevel


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{label} must be a lowercase identifier")
    return normalized


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LogicalCapability(StrEnum):
    AI_TEXT = "ai.text.generate"
    VISION_UNDERSTANDING = "vision.understand"
    IMAGE_GENERATION = "media.image.generate"
    IMAGE_EDITING = "media.image.edit"
    VIDEO_GENERATION = "media.video.generate"
    MUSIC_GENERATION = "media.music.generate"
    SPEECH_TTS = "speech.tts"
    SPEECH_STT = "speech.stt"
    EMBEDDING = "retrieval.embed"
    RERANK = "retrieval.rerank"
    WEB_SEARCH = "web.search"
    WEB_SEARCH_PAID = "web.search.openai_paid"
    ISOLATED_BROWSER = "web.browser.isolated"


class ProviderKind(StrEnum):
    API = "api"
    LOCAL = "local"


class QualityTier(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    QUALITY = "quality"


class ResourceTarget(StrEnum):
    REMOTE = "remote"
    CPU = "cpu"
    CUDA = "cuda"


class ModelLoadPolicy(StrEnum):
    PROVIDER_MANAGED = "provider_managed"
    ALWAYS_LOADED = "always_loaded"
    ON_DEMAND = "on_demand"
    IDLE_UNLOAD = "idle_unload"


class OffloadPolicy(StrEnum):
    DISABLED = "disabled"
    CPU_ALLOWED = "cpu_allowed"


class ArtifactKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    WEB_PAGE = "web_page"
    SCREENSHOT = "screenshot"


class BrowserActionType(StrEnum):
    """隔離ブラウザ内で許可する操作。PC、OS、shell操作は意図的に存在しない。"""

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE_TEXT = "type_text"
    SELECT_OPTION = "select_option"
    SCROLL = "scroll"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    EXTRACT_TEXT = "extract_text"


class HealthStatus(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ReadinessCode(StrEnum):
    READY = "ready"
    UNKNOWN_CAPABILITY = "unknown_capability"
    CAPABILITY_DISABLED = "capability_disabled"
    INSUFFICIENT_RBAC = "insufficient_rbac"
    ROUTE_UNCONFIGURED = "route_unconfigured"
    PROVIDER_DISABLED = "provider_disabled"
    ADAPTER_MISSING = "adapter_missing"
    HEALTH_UNKNOWN = "health_unknown"
    PROVIDER_UNHEALTHY = "provider_unhealthy"
    MODEL_ALIAS_UNCONFIGURED = "model_alias_unconfigured"
    MODEL_PROBE_REQUIRED = "model_probe_required"
    AUDIT_SINK_MISSING = "audit_sink_missing"
    CONSENT_REQUIRED = "consent_required"
    CONFIRMATION_REQUIRED = "confirmation_required"
    OWNER_OVERRIDE_REQUIRES_OWNER = "owner_override_requires_owner"
    OVERRIDE_PROVIDER_INVALID = "override_provider_invalid"


class AuditOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SecretReference:
    """Secretの値ではなく、composition rootで解決する名前だけを保持する。"""

    name: str
    source: str = "env"

    def __post_init__(self) -> None:
        source = normalize_identifier(self.source, label="secret source")
        if not isinstance(self.name, str) or not _SECRET_NAME.fullmatch(self.name.strip()):
            raise ValueError("secret reference name must be an uppercase setting name")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "name", self.name.strip())

    @classmethod
    def parse(cls, value: str) -> SecretReference:
        if not isinstance(value, str):
            raise TypeError("secret reference must be a string")
        source, separator, name = value.partition(":")
        if not separator:
            raise ValueError("secret reference must use source:NAME format")
        return cls(name=name, source=source)

    def __str__(self) -> str:
        return f"{self.source}:{self.name}"


@dataclass(frozen=True, slots=True)
class SettingReference:
    """Endpoint等の非secret設定も値ではなく参照名でmanifestへ置く。"""

    name: str
    source: str = "env"

    def __post_init__(self) -> None:
        source = normalize_identifier(self.source, label="setting source")
        if not isinstance(self.name, str) or not _SECRET_NAME.fullmatch(self.name.strip()):
            raise ValueError("setting reference name must be an uppercase setting name")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "name", self.name.strip())

    @classmethod
    def parse(cls, value: str) -> SettingReference:
        if not isinstance(value, str):
            raise TypeError("setting reference must be a string")
        source, separator, name = value.partition(":")
        if not separator:
            raise ValueError("setting reference must use source:NAME format")
        return cls(name=name, source=source)

    def __str__(self) -> str:
        return f"{self.source}:{self.name}"


@dataclass(frozen=True, slots=True)
class ResourceProfile:
    """Provider/modelを実行する計算資源の宣言。resource scheduler/adapterが強制する。"""

    target: ResourceTarget
    max_concurrency: int = 1
    vram_budget_mb: int | None = None
    system_ram_budget_mb: int | None = None
    load_policy: ModelLoadPolicy = ModelLoadPolicy.PROVIDER_MANAGED
    idle_unload_seconds: int | None = None
    offload_policy: OffloadPolicy = OffloadPolicy.DISABLED
    exclusive_gpu_lease: bool = False
    device_ref: SettingReference | None = None

    def __post_init__(self) -> None:
        target = ResourceTarget(self.target)
        load_policy = ModelLoadPolicy(self.load_policy)
        offload_policy = OffloadPolicy(self.offload_policy)
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or not 1 <= self.max_concurrency <= 128
        ):
            raise ValueError("max_concurrency is outside the allowed range")
        for label, value, minimum, maximum in (
            ("vram_budget_mb", self.vram_budget_mb, 256, 262_144),
            ("system_ram_budget_mb", self.system_ram_budget_mb, 256, 1_048_576),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum
            ):
                raise ValueError(f"{label} is outside the allowed range")
        if load_policy is ModelLoadPolicy.IDLE_UNLOAD:
            if (
                isinstance(self.idle_unload_seconds, bool)
                or not isinstance(self.idle_unload_seconds, int)
                or not 10 <= self.idle_unload_seconds <= 86_400
            ):
                raise ValueError("idle_unload requires 10 to 86400 idle_unload_seconds")
        elif self.idle_unload_seconds is not None:
            raise ValueError("idle_unload_seconds is only valid for idle_unload")
        if self.device_ref is not None and not isinstance(self.device_ref, SettingReference):
            raise TypeError("device_ref must be a SettingReference")

        if target is ResourceTarget.REMOTE:
            if (
                self.vram_budget_mb is not None
                or self.system_ram_budget_mb is not None
                or self.exclusive_gpu_lease
                or self.device_ref is not None
                or offload_policy is not OffloadPolicy.DISABLED
                or load_policy is not ModelLoadPolicy.PROVIDER_MANAGED
            ):
                raise ValueError("remote resources cannot declare local memory, loading, offload, or GPU lease")
        elif target is ResourceTarget.CPU:
            if self.vram_budget_mb is not None or self.exclusive_gpu_lease or self.device_ref is not None:
                raise ValueError("CPU resources cannot declare VRAM, GPU device, or exclusive GPU lease")
            if offload_policy is not OffloadPolicy.DISABLED:
                raise ValueError("CPU resources cannot offload to CPU")
        elif self.vram_budget_mb is None:
            raise ValueError("CUDA resources require an explicit vram_budget_mb")

        object.__setattr__(self, "target", target)
        object.__setattr__(self, "load_policy", load_policy)
        object.__setattr__(self, "offload_policy", offload_policy)

    @classmethod
    def remote(cls, *, max_concurrency: int = 8) -> ResourceProfile:
        return cls(target=ResourceTarget.REMOTE, max_concurrency=max_concurrency)

    @property
    def gpu_lease_key(self) -> str | None:
        if self.target is not ResourceTarget.CUDA or not self.exclusive_gpu_lease:
            return None
        return str(self.device_ref) if self.device_ref is not None else "cuda:default"


@dataclass(frozen=True, slots=True)
class TimeoutPolicy:
    request_seconds: float = 60.0
    health_seconds: float = 5.0

    def __post_init__(self) -> None:
        for label, value, minimum, maximum in (
            ("request_seconds", self.request_seconds, 1.0, 900.0),
            ("health_seconds", self.health_seconds, 0.2, 30.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{label} must be numeric")
            normalized = float(value)
            if not minimum <= normalized <= maximum:
                raise ValueError(f"{label} is outside the allowed range")
            object.__setattr__(self, label, normalized)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Provider境界を越える成果物。bytes、ローカルpath、署名URLは保持しない。"""

    artifact_id: str
    kind: ArtifactKind
    media_type: str
    size_bytes: int | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        artifact_id = normalize_identifier(self.artifact_id, label="artifact_id")
        kind = ArtifactKind(self.kind)
        if not isinstance(self.media_type, str) or "/" not in self.media_type:
            raise ValueError("media_type must be a MIME type")
        media_type = self.media_type.strip().lower()
        if self.size_bytes is not None:
            if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
                raise ValueError("size_bytes must be a non-negative integer")
        if self.sha256 is not None and not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "media_type", media_type)


@dataclass(frozen=True, slots=True)
class AITextInput:
    prompt: str
    system_prompt: str = ""

    def __post_init__(self) -> None:
        _validate_text(self.prompt, label="prompt", maximum=200_000)
        _validate_text(self.system_prompt, label="system_prompt", maximum=100_000, allow_empty=True)


@dataclass(frozen=True, slots=True)
class MediaGenerationInput:
    prompt: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    seed: int | None = None

    def __post_init__(self) -> None:
        _validate_text(self.prompt, label="prompt", maximum=50_000)
        if self.duration_seconds is not None:
            if (
                isinstance(self.duration_seconds, bool)
                or not isinstance(self.duration_seconds, (int, float))
                or not 0.1 <= float(self.duration_seconds) <= 3_600.0
            ):
                raise ValueError("duration_seconds is outside the allowed range")
            object.__setattr__(self, "duration_seconds", float(self.duration_seconds))
        for label in ("width", "height"):
            value = getattr(self, label)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 64 <= value <= 16_384
            ):
                raise ValueError(f"{label} is outside the allowed range")
        if self.seed is not None and (
            isinstance(self.seed, bool) or not isinstance(self.seed, int) or not 0 <= self.seed <= 2**63 - 1
        ):
            raise ValueError("seed is outside the allowed range")


@dataclass(frozen=True, slots=True)
class ImageEditingInput:
    instruction: str = field(repr=False)
    source_binding_digest: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_text(self.instruction, label="image editing instruction", maximum=4_000)
        source_binding_digest = self.source_binding_digest
        if source_binding_digest is not None and not _SHA256.fullmatch(source_binding_digest):
            raise ValueError("source_binding_digest must be a SHA-256 digest")
        object.__setattr__(self, "instruction", self.instruction.strip())


@dataclass(frozen=True, slots=True)
class WebSearchInput:
    query: str
    allowed_domains: tuple[str, ...] = ()
    max_results: int = 8

    def __post_init__(self) -> None:
        _validate_text(self.query, label="query", maximum=8_000)
        if (
            isinstance(self.max_results, bool)
            or not isinstance(self.max_results, int)
            or not 1 <= self.max_results <= 20
        ):
            raise ValueError("max_results is outside the allowed range")
        domains = tuple(_normalize_domain(value) for value in self.allowed_domains)
        if len(domains) > 50 or len(set(domains)) != len(domains):
            raise ValueError("allowed_domains must contain at most 50 unique domains")
        object.__setattr__(self, "allowed_domains", domains)


@dataclass(frozen=True, slots=True)
class SpeechSynthesisInput:
    text: str = field(repr=False)
    voice_alias: str = "standard"
    language_code: str | None = None

    def __post_init__(self) -> None:
        _validate_text(self.text, label="speech text", maximum=500)
        voice_alias = _optional_identifier(self.voice_alias, label="voice_alias")
        if voice_alias != "standard":
            raise ValueError("voice_alias must be the standard Stage 1 voice")
        language_code = _optional_language_code(self.language_code)
        object.__setattr__(self, "voice_alias", voice_alias)
        object.__setattr__(self, "language_code", language_code)


@dataclass(frozen=True, slots=True)
class SpeechTranscriptionInput:
    language_code: str | None = None
    prompt: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        language_code = _optional_language_code(self.language_code)
        _validate_text(self.prompt, label="transcription prompt", maximum=2_000, allow_empty=True)
        object.__setattr__(self, "language_code", language_code)


@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    texts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        texts = tuple(self.texts)
        if len(texts) > 256:
            raise ValueError("embedding input must contain at most 256 texts")
        for value in texts:
            _validate_text(value, label="embedding text", maximum=50_000)
        if sum(len(value) for value in texts) > 200_000:
            raise ValueError("embedding input text is too large")
        object.__setattr__(self, "texts", texts)


@dataclass(frozen=True, slots=True)
class RerankInput:
    query: str
    documents: tuple[str, ...]
    top_n: int = 10

    def __post_init__(self) -> None:
        _validate_text(self.query, label="rerank query", maximum=8_000)
        documents = tuple(self.documents)
        if not documents or len(documents) > 1_000:
            raise ValueError("rerank input must contain 1 to 1000 documents")
        for document in documents:
            _validate_text(document, label="rerank document", maximum=50_000)
        if sum(len(value) for value in documents) > 1_000_000:
            raise ValueError("rerank documents are too large")
        if isinstance(self.top_n, bool) or not isinstance(self.top_n, int) or not 1 <= self.top_n <= len(documents):
            raise ValueError("top_n must be between 1 and the document count")
        object.__setattr__(self, "documents", documents)


@dataclass(frozen=True, slots=True)
class BrowserStep:
    action: BrowserActionType
    selector: str | None = None
    text: str | None = None
    url: str | None = None
    amount: int | None = None
    wait_seconds: float | None = None

    def __post_init__(self) -> None:
        action = BrowserActionType(self.action)
        selector = _optional_text(self.selector, label="selector", maximum=2_000)
        text = _optional_text(self.text, label="text", maximum=20_000)
        url = _safe_web_url(self.url) if self.url is not None else None
        if self.amount is not None and (
            isinstance(self.amount, bool) or not isinstance(self.amount, int) or not -100_000 <= self.amount <= 100_000
        ):
            raise ValueError("amount is outside the allowed range")
        if self.wait_seconds is not None and (
            isinstance(self.wait_seconds, bool)
            or not isinstance(self.wait_seconds, (int, float))
            or not 0.0 <= float(self.wait_seconds) <= 30.0
        ):
            raise ValueError("wait_seconds is outside the allowed range")

        if action is BrowserActionType.NAVIGATE and url is None:
            raise ValueError("navigate requires an http(s) url")
        if (
            action in {BrowserActionType.CLICK, BrowserActionType.TYPE_TEXT, BrowserActionType.SELECT_OPTION}
            and not selector
        ):
            raise ValueError(f"{action.value} requires a selector")
        if action in {BrowserActionType.TYPE_TEXT, BrowserActionType.SELECT_OPTION} and text is None:
            raise ValueError(f"{action.value} requires text")
        if action is BrowserActionType.SCROLL and self.amount is None:
            raise ValueError("scroll requires amount")
        if action is BrowserActionType.WAIT and self.wait_seconds is None:
            raise ValueError("wait requires wait_seconds")

        object.__setattr__(self, "action", action)
        object.__setattr__(self, "selector", selector)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "url", url)
        if self.wait_seconds is not None:
            object.__setattr__(self, "wait_seconds", float(self.wait_seconds))


@dataclass(frozen=True, slots=True)
class IsolatedBrowserInput:
    start_url: str
    steps: tuple[BrowserStep, ...]

    def __post_init__(self) -> None:
        start_url = _safe_web_url(self.start_url)
        steps = tuple(self.steps)
        if not steps or len(steps) > 100:
            raise ValueError("browser request must contain 1 to 100 steps")
        if any(not isinstance(step, BrowserStep) for step in steps):
            raise TypeError("steps must contain BrowserStep values")
        object.__setattr__(self, "start_url", start_url)
        object.__setattr__(self, "steps", steps)


ProviderInput: TypeAlias = (
    AITextInput
    | MediaGenerationInput
    | ImageEditingInput
    | WebSearchInput
    | SpeechSynthesisInput
    | SpeechTranscriptionInput
    | EmbeddingInput
    | RerankInput
    | IsolatedBrowserInput
)


_EXPECTED_INPUT: dict[LogicalCapability, type[object]] = {
    LogicalCapability.AI_TEXT: AITextInput,
    LogicalCapability.VISION_UNDERSTANDING: AITextInput,
    LogicalCapability.IMAGE_GENERATION: MediaGenerationInput,
    LogicalCapability.IMAGE_EDITING: ImageEditingInput,
    LogicalCapability.VIDEO_GENERATION: MediaGenerationInput,
    LogicalCapability.MUSIC_GENERATION: MediaGenerationInput,
    LogicalCapability.SPEECH_TTS: SpeechSynthesisInput,
    LogicalCapability.SPEECH_STT: SpeechTranscriptionInput,
    LogicalCapability.EMBEDDING: EmbeddingInput,
    LogicalCapability.RERANK: RerankInput,
    LogicalCapability.WEB_SEARCH: WebSearchInput,
    LogicalCapability.WEB_SEARCH_PAID: WebSearchInput,
    LogicalCapability.ISOLATED_BROWSER: IsolatedBrowserInput,
}


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    request_id: str
    trace_id: str
    capability: LogicalCapability
    actor_ref: str
    payload: ProviderInput
    model_alias: str | None = None
    quality_tier: QualityTier | None = None
    input_artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        request_id = normalize_identifier(self.request_id, label="request_id")
        trace_id = normalize_identifier(self.trace_id, label="trace_id")
        capability = LogicalCapability(self.capability)
        actor_ref = normalize_identifier(self.actor_ref, label="actor_ref")
        expected = _EXPECTED_INPUT[capability]
        if not isinstance(self.payload, expected):
            raise TypeError(f"{capability.value} requires {expected.__name__}")
        input_artifacts = tuple(self.input_artifacts)
        if len(input_artifacts) > 32 or any(not isinstance(ref, ArtifactRef) for ref in input_artifacts):
            raise ValueError("input_artifacts must contain at most 32 ArtifactRef values")
        if capability is LogicalCapability.SPEECH_STT and (
            len(input_artifacts) != 1 or input_artifacts[0].kind is not ArtifactKind.AUDIO
        ):
            raise ValueError("speech.stt requires exactly one audio ArtifactRef")
        if capability is LogicalCapability.SPEECH_TTS and input_artifacts:
            raise ValueError("speech.tts does not accept input artifacts")
        if capability is LogicalCapability.IMAGE_EDITING and (
            len(input_artifacts) != 1
            or input_artifacts[0].kind is not ArtifactKind.IMAGE
            or input_artifacts[0].media_type != "image/png"
            or input_artifacts[0].size_bytes is None
            or input_artifacts[0].size_bytes <= 0
            or input_artifacts[0].sha256 is None
        ):
            raise ValueError("media.image.edit requires exactly one complete PNG ArtifactRef")
        if capability is LogicalCapability.EMBEDDING and not self.payload.texts and not input_artifacts:
            raise ValueError("retrieval.embed requires text or an ArtifactRef")
        model_alias = None
        if self.model_alias is not None:
            model_alias = normalize_identifier(self.model_alias, label="model_alias")
        quality_tier = QualityTier(self.quality_tier) if self.quality_tier is not None else None
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "actor_ref", actor_ref)
        object.__setattr__(self, "model_alias", model_alias)
        object.__setattr__(self, "quality_tier", quality_tier)
        object.__setattr__(self, "input_artifacts", input_artifacts)


@dataclass(frozen=True, slots=True)
class ProviderInvocation:
    provider_id: str
    quality_tier: QualityTier
    model_alias: str | None
    provider_model: str | None
    timeout_seconds: float
    resources: ResourceProfile

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", normalize_identifier(self.provider_id, label="provider_id"))
        object.__setattr__(self, "quality_tier", QualityTier(self.quality_tier))
        if not isinstance(self.resources, ResourceProfile):
            raise TypeError("resources must be a ResourceProfile")


@dataclass(frozen=True, slots=True)
class OwnerRouteOverride:
    """Provider/model実名ではなく、configured provider IDとlogical aliasだけを上書きする。"""

    quality_tier: QualityTier | None = None
    provider_id: str | None = None
    model_alias: str | None = None

    def __post_init__(self) -> None:
        if self.quality_tier is not None:
            object.__setattr__(self, "quality_tier", QualityTier(self.quality_tier))
        if self.provider_id is not None:
            object.__setattr__(
                self,
                "provider_id",
                normalize_identifier(self.provider_id, label="override provider_id"),
            )
        if self.model_alias is not None:
            object.__setattr__(
                self,
                "model_alias",
                normalize_identifier(self.model_alias, label="override model_alias"),
            )
        if self.quality_tier is None and self.provider_id is None and self.model_alias is None:
            raise ValueError("owner override must specify a tier, provider, or model alias")


@dataclass(frozen=True, slots=True)
class ProviderResult:
    request_id: str
    provider_id: str
    provider_model: str | None = None
    text: str = ""
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        request_id = normalize_identifier(self.request_id, label="request_id")
        provider_id = normalize_identifier(self.provider_id, label="provider_id")
        if not isinstance(self.text, str) or len(self.text) > 1_000_000:
            raise ValueError("text is outside the allowed range")
        artifacts = tuple(self.artifacts)
        if len(artifacts) > 64 or any(not isinstance(ref, ArtifactRef) for ref in artifacts):
            raise ValueError("artifacts must contain at most 64 ArtifactRef values")
        if not self.text and not artifacts:
            raise ValueError("provider result must contain text or an artifact ref")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "artifacts", artifacts)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider_id: str
    status: HealthStatus
    checked_at: datetime
    latency_ms: int | None = None
    detail_code: str | None = None
    probed_model_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        provider_id = normalize_identifier(self.provider_id, label="provider_id")
        status = HealthStatus(self.status)
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        if self.latency_ms is not None and (
            isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, int) or self.latency_ms < 0
        ):
            raise ValueError("latency_ms must be a non-negative integer")
        detail_code = None
        if self.detail_code is not None:
            detail_code = normalize_identifier(self.detail_code, label="detail_code")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "detail_code", detail_code)
        aliases = tuple(normalize_identifier(value, label="probed model alias") for value in self.probed_model_aliases)
        if len(set(aliases)) != len(aliases):
            raise ValueError("probed_model_aliases must be unique")
        object.__setattr__(self, "probed_model_aliases", aliases)

    @property
    def usable(self) -> bool:
        return self.status in {HealthStatus.READY, HealthStatus.DEGRADED}


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    capability: LogicalCapability
    default_enabled: bool
    required_rbac: RbacLevel
    risk: RiskLevel
    requires_consent: bool = False
    requires_confirmation: bool = False
    audit_required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", LogicalCapability(self.capability))
        object.__setattr__(self, "required_rbac", RbacLevel.parse(self.required_rbac))
        object.__setattr__(self, "risk", RiskLevel.parse(self.risk))


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    provider_id: str
    code: ReadinessCode


@dataclass(frozen=True, slots=True)
class ProviderResolution:
    ready: bool
    capability: LogicalCapability
    code: ReadinessCode
    provider_id: str | None = None
    quality_tier: QualityTier = QualityTier.BALANCED
    model_alias: str | None = None
    provider_model: str | None = None
    resources: ResourceProfile | None = None
    attempts: tuple[ProviderAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class AuditRecord:
    request_id: str
    trace_id: str
    actor_ref: str
    capability: LogicalCapability
    outcome: AuditOutcome
    occurred_at: datetime
    quality_tier: QualityTier = QualityTier.BALANCED
    provider_id: str | None = None
    model_alias: str | None = None
    duration_ms: int | None = None
    artifact_ids: tuple[str, ...] = ()
    failure_code: str | None = None
    outcome_uncertain: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", normalize_identifier(self.request_id, label="request_id"))
        object.__setattr__(self, "trace_id", normalize_identifier(self.trace_id, label="trace_id"))
        object.__setattr__(self, "actor_ref", normalize_identifier(self.actor_ref, label="actor_ref"))
        object.__setattr__(self, "capability", LogicalCapability(self.capability))
        object.__setattr__(self, "outcome", AuditOutcome(self.outcome))
        object.__setattr__(self, "quality_tier", QualityTier(self.quality_tier))
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.provider_id is not None:
            object.__setattr__(
                self,
                "provider_id",
                normalize_identifier(self.provider_id, label="provider_id"),
            )
        if self.model_alias is not None:
            object.__setattr__(
                self,
                "model_alias",
                normalize_identifier(self.model_alias, label="model_alias"),
            )
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        artifact_ids = tuple(normalize_identifier(value, label="artifact_id") for value in self.artifact_ids)
        object.__setattr__(self, "artifact_ids", artifact_ids)
        if self.failure_code is not None:
            object.__setattr__(
                self,
                "failure_code",
                normalize_identifier(self.failure_code, label="failure_code"),
            )
        if not isinstance(self.outcome_uncertain, bool):
            raise TypeError("outcome_uncertain must be a bool")


def _validate_text(value: str, *, label: str, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{label} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds the allowed length")
    return value


def _optional_text(value: str | None, *, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    _validate_text(value, label=label, maximum=maximum)
    return value


def _optional_identifier(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    return normalize_identifier(value, label=label)


def _optional_language_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", normalized):
        raise ValueError("language_code must be a BCP-47-like language tag")
    return normalized


def _safe_web_url(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("url must be a string")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("browser URLs must use http or https")
    if parsed.username or parsed.password:
        raise ValueError("browser URLs must not contain credentials")
    return value.strip()


def _normalize_domain(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("domain must be a string")
    domain = value.strip().lower().rstrip(".")
    if not domain or "/" in domain or ":" in domain or "@" in domain:
        raise ValueError("allowed_domains must contain hostnames only")
    parsed = urlparse(f"//{domain}")
    if parsed.hostname != domain:
        raise ValueError("allowed_domains must contain hostnames only")
    return domain
