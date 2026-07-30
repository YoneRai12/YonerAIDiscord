"""音声・音楽・動画providerを明示設定だけで組み立てる小さなruntime境界。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from yonerai_discord.modules.music_generation.artifacts import MusicArtifactStore
from yonerai_discord.modules.music_generation.domain import (
    MUSIC_PROFILE_REVISION,
    MUSIC_RIGHTS_REVISION,
    MusicGenerationRequest,
)
from yonerai_discord.modules.music_generation.provider_elevenlabs import (
    AiohttpElevenLabsMusicTransport,
    ELEVEN_MUSIC_ADAPTER_ID,
    ELEVEN_MUSIC_MODEL,
    ELEVEN_MUSIC_PROVIDER_ID,
    ElevenLabsMusicProviderAdapter,
    ElevenLabsMusicTransport,
)
from yonerai_discord.modules.speech_synthesis.provider_voicevox import (
    VOICEVOX_PROVIDER_MODEL,
    VoicevoxSpeechSynthesisProviderAdapter,
    VoicevoxSynthesisPort,
)
from yonerai_discord.modules.speech_transcription.artifacts import BoundedSpeechAudioStore
from yonerai_discord.modules.speech_transcription.provider_openai import (
    AiohttpOpenAITranscriptionTransport,
    OPENAI_STT_MODEL,
    OpenAITranscriptionProviderAdapter,
    OpenAITranscriptionTransport,
)
from yonerai_discord.modules.video_generation.artifacts import VideoArtifactStore
from yonerai_discord.modules.video_generation.provider_gemini_veo import (
    AiohttpGeminiVeoTransport,
    GEMINI_VEO_ADAPTER_ID,
    GEMINI_VEO_MODELS,
    GEMINI_VEO_PROVIDER_ID,
    GeminiVeoProviderAdapter,
    GeminiVeoTransport,
)
from yonerai_discord.modules.voice.voicevox import VoicevoxClient
from yonerai_discord.provider_registry import (
    DEFAULT_CATALOG,
    CapabilityRoute,
    HealthStatus,
    LogicalCapability,
    ModelBinding,
    ModelMaturity,
    MediaGenerationInput,
    ProviderCatalogManifest,
    ProviderKind,
    ProviderManifest,
    ProviderRegistry,
    ProviderInvocation,
    ProviderRequest,
    ProviderResolution,
    QualityTier,
    ResourceProfile,
    ResourceTarget,
    SecretReference,
    TierRoute,
    TimeoutPolicy,
)
from yonerai_discord.provider_registry.domain import ArtifactRef, AuditRecord
from yonerai_discord.secret_policy import is_loopback_endpoint


_OPENAI_STT_PROVIDER_ID = OpenAITranscriptionProviderAdapter.provider_id
_OPENAI_STT_ADAPTER_ID = OpenAITranscriptionProviderAdapter.adapter_id
_VOICEVOX_PROVIDER_ID = VoicevoxSpeechSynthesisProviderAdapter.provider_id
_VOICEVOX_ADAPTER_ID = VoicevoxSpeechSynthesisProviderAdapter.adapter_id
_ACTOR_REF = re.compile(r"discord-user-(?P<actor_id>[1-9][0-9]{0,18})\Z")
_STT_REQUEST = re.compile(r"stt-request-(?P<binding>[0-9a-f]{64})\Z")
_ALIASES: Mapping[LogicalCapability, tuple[str, ...]] = {
    LogicalCapability.SPEECH_STT: ("stt.fast", "stt.balanced", "stt.quality"),
    LogicalCapability.SPEECH_TTS: ("tts.fast", "tts.balanced", "tts.quality"),
    LogicalCapability.MUSIC_GENERATION: ("music.fast", "music.balanced", "music.quality"),
    LogicalCapability.VIDEO_GENERATION: ("video.fast", "video.balanced", "video.quality"),
}
_PROVIDER_BY_CAPABILITY = {
    LogicalCapability.SPEECH_STT: _OPENAI_STT_PROVIDER_ID,
    LogicalCapability.SPEECH_TTS: _VOICEVOX_PROVIDER_ID,
    LogicalCapability.MUSIC_GENERATION: ELEVEN_MUSIC_PROVIDER_ID,
    LogicalCapability.VIDEO_GENERATION: GEMINI_VEO_PROVIDER_ID,
}
_PLUGIN_BY_CAPABILITY = {
    LogicalCapability.SPEECH_STT: "speech_transcription",
    LogicalCapability.SPEECH_TTS: "speech_synthesis",
    LogicalCapability.MUSIC_GENERATION: "music_generation",
    LogicalCapability.VIDEO_GENERATION: "video_generation",
}
_REGISTRY_PUBLICATIONS = (
    "speech_transcription_provider_registry",
    "speech_synthesis_provider_registry",
    "music_generation_provider_registry",
    "video_generation_provider_registry",
)
_STORE_PUBLICATION_BY_CAPABILITY = {
    LogicalCapability.SPEECH_STT: "speech_transcription_audio_store",
    LogicalCapability.SPEECH_TTS: "speech_synthesis_artifact_store",
    LogicalCapability.MUSIC_GENERATION: "music_artifact_store",
    LogicalCapability.VIDEO_GENERATION: "video_artifact_store",
}
_PLUGIN_PUBLICATIONS_BY_CAPABILITY = {
    LogicalCapability.SPEECH_STT: (
        "speech_transcription_service",
        "speech_transcription_adapter",
    ),
    LogicalCapability.SPEECH_TTS: (
        "speech_synthesis_service",
        "speech_synthesis_adapter",
    ),
    LogicalCapability.MUSIC_GENERATION: (
        "music_generation_service",
        "music_generation_adapter",
    ),
    LogicalCapability.VIDEO_GENERATION: (
        "video_generation_service",
        "video_generation_adapter",
    ),
}
_MISSING = object()


class ProviderAuditDatabase(Protocol):
    def append_audit(
        self,
        event: str,
        *,
        actor_id: int,
        details: Mapping[str, object] | None = None,
        plugin: str | None = None,
        guild_id: int | str | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class MediaProviderConfiguration:
    """環境変数を読まない、composition rootから渡す明示設定。"""

    stt_openai_enabled: bool = False
    stt_openai_api_key: str = field(default="", repr=False)
    stt_timeout_seconds: float = 120.0
    tts_voicevox_enabled: bool = False
    tts_artifact_root: Path | None = None
    tts_timeout_seconds: float = 60.0
    music_eleven_enabled: bool = False
    music_eleven_api_key: str = field(default="", repr=False)
    music_artifact_root: Path | None = None
    music_timeout_seconds: float = 180.0
    video_veo_enabled: bool = False
    video_veo_api_key: str = field(default="", repr=False)
    video_artifact_root: Path | None = None
    video_timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        for name in (
            "stt_openai_enabled",
            "tts_voicevox_enabled",
            "music_eleven_enabled",
            "video_veo_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        for name in (
            "stt_timeout_seconds",
            "tts_timeout_seconds",
            "music_timeout_seconds",
            "video_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 5.0 <= float(value) <= 900.0:
                raise ValueError(f"{name} is outside the allowed range")

    @property
    def enabled_capabilities(self) -> tuple[LogicalCapability, ...]:
        values: list[LogicalCapability] = []
        if self.stt_openai_enabled:
            values.append(LogicalCapability.SPEECH_STT)
        if self.tts_voicevox_enabled:
            values.append(LogicalCapability.SPEECH_TTS)
        if self.music_eleven_enabled:
            values.append(LogicalCapability.MUSIC_GENERATION)
        if self.video_veo_enabled:
            values.append(LogicalCapability.VIDEO_GENERATION)
        return tuple(values)


class SQLiteMediaProviderAuditSink:
    """本文・bytes・path・例外本文を保存しないappend-only provider audit。"""

    def __init__(self, database: ProviderAuditDatabase, *, runtime_current: Callable[[], bool]) -> None:
        if not callable(getattr(database, "append_audit", None)) or not callable(runtime_current):
            raise TypeError("durable media provider audit is unavailable")
        self._database = database
        self._runtime_current = runtime_current

    async def append(self, record: AuditRecord) -> None:
        if not isinstance(record, AuditRecord) or self._runtime_current() is not True:
            raise RuntimeError("media provider audit is unavailable")
        actor = _ACTOR_REF.fullmatch(record.actor_ref)
        plugin = _PLUGIN_BY_CAPABILITY.get(record.capability)
        if actor is None or plugin is None:
            raise RuntimeError("media provider audit binding is invalid")
        details: dict[str, object] = {
            "request_id": record.request_id,
            "trace_id": record.trace_id,
            "capability": record.capability.value,
            "outcome": record.outcome.value,
            "outcome_uncertain": record.outcome_uncertain,
            "quality_tier": record.quality_tier.value,
            "artifact_count": len(record.artifact_ids),
        }
        if record.provider_id is not None:
            details["provider_id"] = record.provider_id
        if record.model_alias is not None:
            details["model_alias"] = record.model_alias
        if record.duration_ms is not None:
            details["duration_ms"] = record.duration_ms
        if record.failure_code is not None:
            details["failure_code"] = record.failure_code
        await asyncio.to_thread(
            self._database.append_audit,
            f"provider.{record.outcome.value}",
            actor_id=int(actor.group("actor_id")),
            details=details,
            plugin=plugin,
            guild_id=None,
        )
        if self._runtime_current() is not True:
            raise RuntimeError("media provider audit identity changed")


class BoundSpeechAudioResolver:
    """serviceが発行したfull fingerprintだけでprocess-memory WAVを読む。"""

    def __init__(self, store: BoundedSpeechAudioStore, *, runtime_current: Callable[[], bool]) -> None:
        if not isinstance(store, BoundedSpeechAudioStore) or not callable(runtime_current):
            raise TypeError("speech audio resolver is unavailable")
        self._store = store
        self._runtime_current = runtime_current

    def read_audio(self, request: ProviderRequest, ref: ArtifactRef) -> bytes:
        match = _STT_REQUEST.fullmatch(getattr(request, "request_id", ""))
        if (
            not isinstance(request, ProviderRequest)
            or len(request.input_artifacts) != 1
            or request.input_artifacts[0] is not ref
            or match is None
            or self._runtime_current() is not True
        ):
            raise RuntimeError("speech audio binding is unavailable")
        data = self._store.read_wav(ref, request_binding=match.group("binding"))
        if self._runtime_current() is not True:
            raise RuntimeError("speech audio authorization changed")
        return data


@dataclass(frozen=True, slots=True)
class _MusicExecutionProof:
    token: object
    provider_request: ProviderRequest
    surface_request_id: str
    request_fingerprint: str
    actor_id: int
    guild_id: int
    channel_id: int
    prompt_hash: str
    duration_seconds: int
    profile_revision: str
    rights_revision: str
    rights_confirmed: bool
    provider_id: str
    provider_model: str | None
    model_alias: str | None
    quality_tier: QualityTier
    timeout_seconds: float
    resources: ResourceProfile
    catalog_revision: str


class MusicExecutionProofRegistry:
    """Music serviceがregistry.executeの周囲だけに発行するexact proof。"""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        runtime_current: Callable[[], bool],
    ) -> None:
        if not isinstance(registry, ProviderRegistry) or not callable(runtime_current):
            raise TypeError("music execution proof registry is unavailable")
        self._registry = registry
        self._runtime_current = runtime_current
        self._proofs: dict[int, _MusicExecutionProof] = {}
        self._closing = False

    def issue(
        self,
        request: MusicGenerationRequest,
        provider_request: ProviderRequest,
        resolution: ProviderResolution,
    ) -> object:
        if (
            self._closing
            or self._runtime_current() is not True
            or not isinstance(request, MusicGenerationRequest)
            or not isinstance(provider_request, ProviderRequest)
            or not isinstance(resolution, ProviderResolution)
            or resolution.ready is not True
            or resolution.capability is not LogicalCapability.MUSIC_GENERATION
            or resolution.provider_id != ELEVEN_MUSIC_PROVIDER_ID
            or resolution.provider_model != ELEVEN_MUSIC_MODEL
            or resolution.model_alias not in _ALIASES[LogicalCapability.MUSIC_GENERATION]
            or resolution.quality_tier is not request.tier
            or resolution.resources is None
            or request.rights_confirmed is not True
            or request.profile_revision != MUSIC_PROFILE_REVISION
            or request.rights_revision != MUSIC_RIGHTS_REVISION
            or provider_request.request_id != request.provider_request_id
            or provider_request.trace_id != request.trace_id
            or provider_request.actor_ref != request.actor_ref
            or provider_request.capability is not LogicalCapability.MUSIC_GENERATION
            or provider_request.quality_tier is not request.tier
            or provider_request.model_alias is not None
            or provider_request.input_artifacts
            or not isinstance(provider_request.payload, MediaGenerationInput)
            or provider_request.payload.prompt != request.prompt
            or provider_request.payload.duration_seconds != request.duration_seconds
        ):
            raise RuntimeError("music execution proof input is invalid")
        provider = self._registry.manifest.provider(resolution.provider_id)
        if provider is None:
            raise RuntimeError("music execution proof provider disappeared")
        current_resolution = self._registry.resolve(
            LogicalCapability.MUSIC_GENERATION,
            actor_level="trusted",
            quality_tier=request.tier,
            consent_verified=True,
        )
        if current_resolution != resolution:
            raise RuntimeError("music execution proof route changed")
        key = id(provider_request)
        if key in self._proofs:
            raise RuntimeError("music execution proof already exists")
        token = object()
        self._proofs[key] = _MusicExecutionProof(
            token=token,
            provider_request=provider_request,
            surface_request_id=request.request_id,
            request_fingerprint=request.fingerprint,
            actor_id=request.actor_id,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            prompt_hash=request.prompt_hash,
            duration_seconds=request.duration_seconds,
            profile_revision=request.profile_revision,
            rights_revision=request.rights_revision,
            rights_confirmed=request.rights_confirmed,
            provider_id=resolution.provider_id,
            provider_model=resolution.provider_model,
            model_alias=resolution.model_alias,
            quality_tier=resolution.quality_tier,
            timeout_seconds=provider.timeouts.request_seconds,
            resources=resolution.resources,
            catalog_revision=self._registry.manifest.content_revision,
        )
        return token

    def revoke(self, token: object) -> None:
        for key, proof in tuple(self._proofs.items()):
            if proof.token is token:
                self._proofs.pop(key)
                return
        raise RuntimeError("music execution proof identity changed")

    def current(
        self,
        provider_request: ProviderRequest,
        invocation: ProviderInvocation,
    ) -> bool:
        proof = self._proofs.get(id(provider_request))
        payload = getattr(provider_request, "payload", None)
        try:
            return (
                not self._closing
                and self._runtime_current() is True
                and proof is not None
                and proof.provider_request is provider_request
                and isinstance(payload, MediaGenerationInput)
                and proof.request_fingerprint
                == hashlib.sha256(
                    "\0".join(
                        (
                            proof.surface_request_id,
                            str(proof.guild_id),
                            str(proof.channel_id),
                            str(proof.actor_id),
                            proof.prompt_hash,
                            str(proof.duration_seconds),
                            proof.quality_tier.value,
                            proof.profile_revision,
                            proof.rights_revision,
                        )
                    ).encode()
                ).hexdigest()
                and proof.rights_confirmed is True
                and proof.profile_revision == MUSIC_PROFILE_REVISION
                and proof.rights_revision == MUSIC_RIGHTS_REVISION
                and provider_request.actor_ref == f"discord-user-{proof.actor_id}"
                and provider_request.request_id == f"music-request-{proof.request_fingerprint[:48]}"
                and provider_request.trace_id == f"trace-{proof.surface_request_id}"
                and provider_request.capability is LogicalCapability.MUSIC_GENERATION
                and provider_request.quality_tier is proof.quality_tier
                and provider_request.model_alias is None
                and not provider_request.input_artifacts
                and hashlib.sha256(payload.prompt.encode("utf-8")).hexdigest() == proof.prompt_hash
                and payload.duration_seconds == proof.duration_seconds
                and invocation.provider_id == proof.provider_id
                and invocation.provider_model == proof.provider_model
                and invocation.model_alias == proof.model_alias
                and invocation.quality_tier is proof.quality_tier
                and invocation.timeout_seconds == proof.timeout_seconds
                and invocation.resources == proof.resources
                and self._registry.manifest.content_revision == proof.catalog_revision
            )
        except Exception:
            return False

    def begin_close(self) -> None:
        self._closing = True
        self._proofs.clear()


@dataclass(slots=True)
class _OwnedCandidate:
    value: object
    provider_id: str | None = None
    registered: bool = False
    closed: bool = False
    unregistered: bool = False


class MediaProviderCompositionCleanupError(RuntimeError):
    """構築失敗後のresourceを再cleanupできる、本文非保持のtyped error。"""

    def __init__(self, owner: _StagedProviderOwner) -> None:
        super().__init__("media provider composition cleanup is incomplete")
        self.owner = owner


class _StagedProviderOwner:
    """factory返却直後からclose完了までresource identityを保持する。"""

    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry
        self._entries: list[_OwnedCandidate] = []
        self._close_lock = asyncio.Lock()
        self._closed = False

    def track(self, value: object) -> _OwnedCandidate:
        entry = _OwnedCandidate(value)
        self._entries.append(entry)
        return entry

    def adopt(self, entry: _OwnedCandidate, *, provider_id: str, adapter: object) -> None:
        if entry not in self._entries or entry.closed or entry.provider_id is not None:
            raise RuntimeError("media provider candidate ownership changed")
        entry.value = adapter
        entry.provider_id = provider_id

    def mark_registered(self, entry: _OwnedCandidate) -> None:
        if entry.provider_id is None:
            raise RuntimeError("media provider candidate is not adopted")
        entry.registered = True

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            failures: list[BaseException] = []
            for entry in self._entries:
                if not entry.closed:
                    close = getattr(entry.value, "close", None)
                    if callable(close):
                        try:
                            result = close()
                            if isinstance(result, Awaitable):
                                await result
                        except BaseException as exc:
                            failures.append(exc)
                            continue
                    entry.closed = True
                if entry.registered and not entry.unregistered:
                    assert entry.provider_id is not None
                    if not self.registry.unregister_adapter_if_current(entry.provider_id, entry.value):
                        failures.append(RuntimeError("media provider registry identity changed"))
                        continue
                    entry.unregistered = True
            if failures:
                failure = failures[0]
                if isinstance(failure, asyncio.CancelledError):
                    raise failure
                if not isinstance(failure, Exception):
                    raise failure
                raise RuntimeError("media provider shutdown failed safely") from None
            self._closed = True


class MediaProviderRuntime:
    """有効化されたregistry/adapters/storesを同一identityで所有する。"""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        capabilities: tuple[LogicalCapability, ...],
        adapters: Mapping[str, object],
        stores: Mapping[LogicalCapability, object],
        active_state: list[bool],
        runtime_current: Callable[[], bool],
        owner: _StagedProviderOwner,
        music_execution_proofs: MusicExecutionProofRegistry,
    ) -> None:
        self.registry = registry
        self.capabilities = capabilities
        self.adapters = dict(adapters)
        self.stores = dict(stores)
        self._active_state = active_state
        self._runtime_current = runtime_current
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._owner = owner
        self.music_execution_proofs = music_execution_proofs
        self._consumers: list[object] = []
        self._closing_consumer: object | None = None

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closed(self) -> bool:
        return self._closed

    def ready_for(self, capability: LogicalCapability) -> bool:
        if capability not in self.capabilities or self._closing or self._closed or self._active_state[0] is not True:
            return False
        try:
            current = self._runtime_current() is True
        except Exception:
            current = False
        if not current:
            return False
        if not _store_ready(capability, self.stores.get(capability)):
            return False
        health = self.registry.health_snapshot(_PROVIDER_BY_CAPABILITY[capability])
        return health is not None and health.status is HealthStatus.READY

    @property
    def ready(self) -> bool:
        return bool(self.capabilities) and all(self.ready_for(capability) for capability in self.capabilities)

    def begin_close(self) -> None:
        if self._closing:
            return
        if self._consumers:
            raise RuntimeError("media provider runtime is still in use")
        self._closing = True
        self._active_state[0] = False
        self.music_execution_proofs.begin_close()
        stt_store = self.stores.get(LogicalCapability.SPEECH_STT)
        if isinstance(stt_store, BoundedSpeechAudioStore):
            stt_store.begin_close()

    def acquire_consumer(self, consumer: object) -> None:
        if self._closing or self._closed or any(item is consumer for item in self._consumers):
            raise RuntimeError("media provider runtime consumer is invalid")
        self._consumers.append(consumer)

    def release_consumer(self, consumer: object) -> bool:
        for index, item in enumerate(self._consumers):
            if item is consumer:
                self._consumers.pop(index)
                if not self._consumers:
                    self._closing_consumer = consumer
                    self.begin_close()
                    return True
                return False
        if self._closing and not self._closed and self._closing_consumer is consumer:
            return True
        raise RuntimeError("media provider runtime consumer identity changed")

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self.begin_close()
            await self._owner.close()
            self._closed = True
            self._closing_consumer = None


async def compose_media_provider_runtime(
    configuration: MediaProviderConfiguration,
    database: ProviderAuditDatabase,
    *,
    runtime_current: Callable[[], bool],
    stt_transport_factory: Callable[[str], OpenAITranscriptionTransport] | None = None,
    voicevox_client_factory: Callable[[], VoicevoxSynthesisPort] | None = None,
    music_transport_factory: Callable[[str], ElevenLabsMusicTransport] | None = None,
    video_transport_factory: Callable[[str], GeminiVeoTransport] | None = None,
) -> MediaProviderRuntime | None:
    """明示ONのproviderだけをcomposeし、欠落時はnetworkへ触れずfail closed。"""

    if (
        not isinstance(configuration, MediaProviderConfiguration)
        or not configuration.enabled_capabilities
        or not callable(getattr(database, "append_audit", None))
        or not callable(runtime_current)
    ):
        return None
    if not _configuration_ready(
        configuration,
        voicevox_client_factory=voicevox_client_factory,
    ):
        return None
    canonical_roots = _canonical_artifact_roots(configuration)
    if canonical_roots is None:
        return None

    active_state = [True]

    def candidate_current() -> bool:
        try:
            return active_state[0] is True and runtime_current() is True
        except Exception:
            return False

    adapters: dict[str, object] = {}
    stores: dict[LogicalCapability, object] = {}
    audit_sink = SQLiteMediaProviderAuditSink(database, runtime_current=candidate_current)
    registry = ProviderRegistry(_runtime_catalog(configuration), audit_sink=audit_sink)
    music_execution_proofs = MusicExecutionProofRegistry(
        registry,
        runtime_current=candidate_current,
    )
    owner = _StagedProviderOwner(registry)
    try:
        if configuration.stt_openai_enabled:
            store = BoundedSpeechAudioStore()
            resolver = BoundSpeechAudioResolver(store, runtime_current=candidate_current)
            transport = (stt_transport_factory or _default_stt_transport)(
                configuration.stt_openai_api_key,
            )
            entry = owner.track(transport)
            adapter = OpenAITranscriptionProviderAdapter(
                transport,
                audio_resolver=resolver,
                readiness_current=candidate_current,
            )
            owner.adopt(entry, provider_id=adapter.provider_id, adapter=adapter)
            registry.register_adapter(adapter)
            owner.mark_registered(entry)
            adapters[adapter.provider_id] = adapter
            stores[LogicalCapability.SPEECH_STT] = store
        if configuration.tts_voicevox_enabled:
            root = canonical_roots[LogicalCapability.SPEECH_TTS]
            store = MusicArtifactStore(root)
            client = voicevox_client_factory()
            entry = owner.track(client)
            adapter = VoicevoxSpeechSynthesisProviderAdapter(
                client,
                store,
                readiness_current=candidate_current,
            )
            owner.adopt(entry, provider_id=adapter.provider_id, adapter=adapter)
            registry.register_adapter(adapter)
            owner.mark_registered(entry)
            adapters[adapter.provider_id] = adapter
            stores[LogicalCapability.SPEECH_TTS] = store
        if configuration.music_eleven_enabled:
            root = canonical_roots[LogicalCapability.MUSIC_GENERATION]
            store = MusicArtifactStore(root)
            transport = (music_transport_factory or _default_music_transport)(
                configuration.music_eleven_api_key,
            )
            entry = owner.track(transport)
            adapter = ElevenLabsMusicProviderAdapter(
                transport,
                store,
                readiness_current=candidate_current,
                request_policy_current=music_execution_proofs.current,
            )
            owner.adopt(entry, provider_id=adapter.provider_id, adapter=adapter)
            registry.register_adapter(adapter)
            owner.mark_registered(entry)
            adapters[adapter.provider_id] = adapter
            stores[LogicalCapability.MUSIC_GENERATION] = store
        if configuration.video_veo_enabled:
            root = canonical_roots[LogicalCapability.VIDEO_GENERATION]
            store = VideoArtifactStore(root)
            transport = (video_transport_factory or _default_video_transport)(
                configuration.video_veo_api_key,
            )
            entry = owner.track(transport)
            adapter = GeminiVeoProviderAdapter(
                transport,
                store,
                readiness_current=candidate_current,
            )
            owner.adopt(entry, provider_id=adapter.provider_id, adapter=adapter)
            registry.register_adapter(adapter)
            owner.mark_registered(entry)
            adapters[adapter.provider_id] = adapter
            stores[LogicalCapability.VIDEO_GENERATION] = store

        for provider_id in adapters:
            try:
                await registry.refresh_health(provider_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
        return MediaProviderRuntime(
            registry,
            capabilities=configuration.enabled_capabilities,
            adapters=adapters,
            stores=stores,
            active_state=active_state,
            runtime_current=runtime_current,
            owner=owner,
            music_execution_proofs=music_execution_proofs,
        )
    except asyncio.CancelledError as exc:
        active_state[0] = False
        try:
            await _cleanup_owner(owner)
        except BaseException:
            raise MediaProviderCompositionCleanupError(owner) from exc
        raise
    except Exception:
        active_state[0] = False
        try:
            await _cleanup_owner(owner)
        except BaseException as exc:
            raise MediaProviderCompositionCleanupError(owner) from exc
        return None


MediaProviderRuntimeComposer = Callable[
    [object, object, Callable[[], bool]],
    Awaitable[MediaProviderRuntime | None],
]


def media_provider_configuration_from_settings(settings: object) -> MediaProviderConfiguration:
    return MediaProviderConfiguration(
        stt_openai_enabled=getattr(settings, "stt_openai_enabled", False),
        stt_openai_api_key=getattr(settings, "openai_api_key", ""),
        stt_timeout_seconds=getattr(settings, "stt_openai_timeout_seconds", 120.0),
        tts_voicevox_enabled=getattr(settings, "tts_voicevox_enabled", False),
        tts_artifact_root=getattr(settings, "tts_artifact_root", None),
        tts_timeout_seconds=getattr(settings, "tts_voicevox_timeout_seconds", 60.0),
        music_eleven_enabled=getattr(settings, "music_elevenlabs_enabled", False),
        music_eleven_api_key=getattr(settings, "elevenlabs_api_key", ""),
        music_artifact_root=getattr(settings, "music_generation_artifact_root", None),
        music_timeout_seconds=getattr(settings, "music_elevenlabs_timeout_seconds", 180.0),
        video_veo_enabled=getattr(settings, "video_veo_enabled", False),
        video_veo_api_key=getattr(settings, "gemini_api_key", ""),
        video_artifact_root=getattr(settings, "video_artifact_root", None),
        video_timeout_seconds=getattr(settings, "video_veo_timeout_seconds", 600.0),
    )


async def compose_media_provider_runtime_from_settings(
    settings: object,
    database: object,
    runtime_current: Callable[[], bool],
) -> MediaProviderRuntime | None:
    configuration = media_provider_configuration_from_settings(settings)
    if not configuration.enabled_capabilities:
        return None
    voicevox_endpoint = getattr(settings, "voicevox_url", "http://127.0.0.1:50021")
    if configuration.tts_voicevox_enabled and (
        getattr(settings, "voice_allow_remote", False) is not False
        or not isinstance(voicevox_endpoint, str)
        or not is_loopback_endpoint(voicevox_endpoint)
    ):
        return None

    def voicevox_factory() -> VoicevoxClient:
        return VoicevoxClient(
            endpoint=voicevox_endpoint,
            allow_remote=False,
            timeout_seconds=configuration.tts_timeout_seconds,
            max_response_bytes=getattr(settings, "voice_max_response_bytes", 25 * 1024 * 1024),
        )

    return await compose_media_provider_runtime(
        configuration,
        database,  # type: ignore[arg-type]
        runtime_current=runtime_current,
        voicevox_client_factory=voicevox_factory if configuration.tts_voicevox_enabled else None,
    )


async def acquire_media_provider_runtime(
    bot: object,
    consumer: object,
    *,
    composer: MediaProviderRuntimeComposer | None = None,
) -> MediaProviderRuntime | None:
    published = getattr(bot, "media_provider_runtime", _MISSING)
    if published is not _MISSING:
        if not isinstance(published, MediaProviderRuntime) or not media_provider_runtime_current(bot, published):
            raise RuntimeError("media provider runtime publication is invalid")
        published.acquire_consumer(consumer)
        return published

    configuration = media_provider_configuration_from_settings(getattr(bot, "settings", None))
    if not configuration.enabled_capabilities:
        return None
    if any(hasattr(bot, name) for name in _all_publication_names()):
        raise RuntimeError("media provider runtime publication identity is already occupied")
    if any(
        capability in configuration.enabled_capabilities
        for capability in (
            LogicalCapability.SPEECH_STT,
            LogicalCapability.MUSIC_GENERATION,
            LogicalCapability.VIDEO_GENERATION,
        )
    ):
        consent_store = getattr(bot, "ai_remote_consent_store", None)
        if not callable(getattr(consent_store, "active_user", None)):
            return None

    holder: dict[str, MediaProviderRuntime | None] = {"runtime": None}
    staging = [True]

    def runtime_current() -> bool:
        runtime = holder["runtime"]
        if staging[0]:
            return (
                runtime is None
                and not bool(getattr(bot, "is_closing", False))
                and not any(hasattr(bot, name) for name in _all_publication_names())
            )
        return runtime is not None and media_provider_runtime_current(bot, runtime)

    compose = composer or compose_media_provider_runtime_from_settings
    runtime = await compose(
        getattr(bot, "settings", None),
        getattr(bot, "database", None),
        runtime_current,
    )
    if runtime is None:
        staging[0] = False
        return None
    holder["runtime"] = runtime
    try:
        publish_media_provider_runtime(bot, runtime)
        staging[0] = False
        runtime.acquire_consumer(consumer)
        return runtime
    except BaseException:
        staging[0] = False
        unpublish_media_provider_runtime(bot, runtime)
        try:
            await runtime.close()
        except BaseException:
            pass
        raise


async def release_media_provider_runtime(
    bot: object,
    runtime: MediaProviderRuntime,
    consumer: object,
) -> None:
    if not runtime.release_consumer(consumer):
        return
    unpublish_media_provider_runtime(bot, runtime)
    await runtime.close()


def publish_media_provider_runtime(bot: object, runtime: MediaProviderRuntime) -> None:
    if not isinstance(runtime, MediaProviderRuntime):
        raise TypeError("runtime must be a MediaProviderRuntime")
    values = _runtime_publications(runtime)
    if any(hasattr(bot, name) for name in values):
        raise RuntimeError("media provider runtime identity is already published")
    for name, value in values.items():
        setattr(bot, name, value)


def media_provider_runtime_current(bot: object, runtime: MediaProviderRuntime) -> bool:
    values = _runtime_publications(runtime)
    return (
        all(hasattr(bot, name) and getattr(bot, name) is value for name, value in values.items())
        and not runtime.closing
        and not runtime.closed
        and not bool(getattr(bot, "is_closing", False))
    )


def unpublish_media_provider_runtime(bot: object, runtime: MediaProviderRuntime) -> None:
    for name, value in _runtime_publications(runtime).items():
        if hasattr(bot, name) and getattr(bot, name) is value:
            delattr(bot, name)


def media_provider_capability_ready(
    bot: object,
    *,
    capability: LogicalCapability,
    registry: object,
    store: object,
    service: object,
    adapter: object,
    remote_consent_active: object,
    runtime: MediaProviderRuntime | None,
) -> bool:
    service_publication, adapter_publication = _PLUGIN_PUBLICATIONS_BY_CAPABILITY[capability]
    if (
        not isinstance(registry, ProviderRegistry)
        or service is None
        or adapter is None
        or getattr(bot, service_publication, _MISSING) is not service
        or getattr(bot, adapter_publication, _MISSING) is not adapter
        or bool(getattr(service, "_closing", True))
        or bool(getattr(adapter, "_closing", True))
        or not _store_ready(capability, store)
        or (runtime is not None and not media_provider_runtime_current(bot, runtime))
        or (runtime is not None and not runtime.ready_for(capability))
    ):
        return False
    try:
        resolution = registry.resolve(
            capability,
            actor_level="trusted",
            quality_tier=QualityTier.BALANCED,
            consent_verified=callable(remote_consent_active),
        )
        if resolution.ready is not True or resolution.provider_id is None:
            return False
        provider = registry.manifest.provider(resolution.provider_id)
        health = registry.health_snapshot(resolution.provider_id)
        return (
            provider is not None
            and health is not None
            and health.status is HealthStatus.READY
            and (provider.kind is not ProviderKind.API or callable(remote_consent_active))
        )
    except Exception:
        return False


def _configuration_ready(
    configuration: MediaProviderConfiguration,
    *,
    voicevox_client_factory: Callable[[], VoicevoxSynthesisPort] | None,
) -> bool:
    if configuration.stt_openai_enabled and not _credential(configuration.stt_openai_api_key):
        return False
    if configuration.tts_voicevox_enabled and (
        voicevox_client_factory is None or not _artifact_root(configuration.tts_artifact_root)
    ):
        return False
    if configuration.music_eleven_enabled and (
        not _credential(configuration.music_eleven_api_key) or not _artifact_root(configuration.music_artifact_root)
    ):
        return False
    if configuration.video_veo_enabled and (
        not _credential(configuration.video_veo_api_key) or not _artifact_root(configuration.video_artifact_root)
    ):
        return False
    return True


def _runtime_catalog(configuration: MediaProviderConfiguration) -> ProviderCatalogManifest:
    enabled = frozenset(configuration.enabled_capabilities)
    policies = tuple(
        replace(policy, default_enabled=True) if policy.capability in enabled else policy
        for policy in DEFAULT_CATALOG.capabilities
    )
    manifests = _provider_manifests(configuration)
    provider_ids = frozenset(manifest.provider_id for manifest in manifests)
    providers = tuple(provider for provider in DEFAULT_CATALOG.providers if provider.provider_id not in provider_ids)
    routes = tuple(_enabled_route(route) if route.capability in enabled else route for route in DEFAULT_CATALOG.routes)
    return ProviderCatalogManifest(
        schema_version=DEFAULT_CATALOG.schema_version,
        module_id=DEFAULT_CATALOG.module_id,
        capabilities=policies,
        providers=providers + manifests,
        routes=routes,
        compatibility_aliases=DEFAULT_CATALOG.compatibility_aliases,
    )


def _provider_manifests(configuration: MediaProviderConfiguration) -> tuple[ProviderManifest, ...]:
    values: list[ProviderManifest] = []
    if configuration.stt_openai_enabled:
        values.append(
            _provider_manifest(
                provider_id=_OPENAI_STT_PROVIDER_ID,
                adapter_id=_OPENAI_STT_ADAPTER_ID,
                capability=LogicalCapability.SPEECH_STT,
                kind=ProviderKind.API,
                model_by_tier={tier: OPENAI_STT_MODEL for tier in QualityTier},
                timeout=configuration.stt_timeout_seconds,
                secret="env:OPENAI_API_KEY",
            )
        )
    if configuration.tts_voicevox_enabled:
        values.append(
            _provider_manifest(
                provider_id=_VOICEVOX_PROVIDER_ID,
                adapter_id=_VOICEVOX_ADAPTER_ID,
                capability=LogicalCapability.SPEECH_TTS,
                kind=ProviderKind.LOCAL,
                model_by_tier={tier: VOICEVOX_PROVIDER_MODEL for tier in QualityTier},
                timeout=configuration.tts_timeout_seconds,
            )
        )
    if configuration.music_eleven_enabled:
        values.append(
            _provider_manifest(
                provider_id=ELEVEN_MUSIC_PROVIDER_ID,
                adapter_id=ELEVEN_MUSIC_ADAPTER_ID,
                capability=LogicalCapability.MUSIC_GENERATION,
                kind=ProviderKind.API,
                model_by_tier={tier: ELEVEN_MUSIC_MODEL for tier in QualityTier},
                timeout=configuration.music_timeout_seconds,
                secret="env:ELEVENLABS_API_KEY",
            )
        )
    if configuration.video_veo_enabled:
        video_models = {
            QualityTier.FAST: "veo-3.1-lite-generate-preview",
            QualityTier.BALANCED: "veo-3.1-fast-generate-preview",
            QualityTier.QUALITY: "veo-3.1-generate-preview",
        }
        if frozenset(video_models.values()) != GEMINI_VEO_MODELS:
            raise RuntimeError("Veo model profile changed")
        values.append(
            _provider_manifest(
                provider_id=GEMINI_VEO_PROVIDER_ID,
                adapter_id=GEMINI_VEO_ADAPTER_ID,
                capability=LogicalCapability.VIDEO_GENERATION,
                kind=ProviderKind.API,
                model_by_tier=video_models,
                timeout=configuration.video_timeout_seconds,
                secret="env:GEMINI_API_KEY",
                maturity=ModelMaturity.PREVIEW,
            )
        )
    return tuple(values)


def _provider_manifest(
    *,
    provider_id: str,
    adapter_id: str,
    capability: LogicalCapability,
    kind: ProviderKind,
    model_by_tier: Mapping[QualityTier, str],
    timeout: float,
    secret: str | None = None,
    maturity: ModelMaturity = ModelMaturity.STABLE,
) -> ProviderManifest:
    aliases = _ALIASES[capability]
    models = tuple(
        ModelBinding(
            aliases[index],
            model_by_tier[tier],
            maturity=maturity,
            license_id="provider-terms-review-required",
            probe_required=True,
        )
        for index, tier in enumerate(QualityTier)
    )
    resources = (
        ResourceProfile.remote(max_concurrency=1)
        if kind is ProviderKind.API
        else ResourceProfile(target=ResourceTarget.CPU, max_concurrency=1)
    )
    return ProviderManifest(
        provider_id=provider_id,
        kind=kind,
        adapter_id=adapter_id,
        capabilities=(capability,),
        resources=resources,
        enabled=True,
        models=models,
        secret_refs=() if secret is None else (SecretReference.parse(secret),),
        timeouts=TimeoutPolicy(request_seconds=float(timeout), health_seconds=5.0),
    )


def _enabled_route(route: CapabilityRoute) -> CapabilityRoute:
    provider_id = _PROVIDER_BY_CAPABILITY[route.capability]
    aliases = _ALIASES[route.capability]
    return CapabilityRoute(
        route.capability,
        tuple(TierRoute(tier, (provider_id,), aliases[index]) for index, tier in enumerate(QualityTier)),
        route.default_tier,
    )


async def _cleanup_owner(owner: _StagedProviderOwner) -> None:
    task = asyncio.create_task(owner.close())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            await task
        raise


def _canonical_artifact_roots(
    configuration: MediaProviderConfiguration,
) -> dict[LogicalCapability, Path] | None:
    values: dict[LogicalCapability, Path] = {}
    for capability, enabled, candidate in (
        (LogicalCapability.SPEECH_TTS, configuration.tts_voicevox_enabled, configuration.tts_artifact_root),
        (LogicalCapability.MUSIC_GENERATION, configuration.music_eleven_enabled, configuration.music_artifact_root),
        (LogicalCapability.VIDEO_GENERATION, configuration.video_veo_enabled, configuration.video_artifact_root),
    ):
        if not enabled:
            continue
        if not _artifact_root(candidate):
            return None
        assert isinstance(candidate, Path)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        for existing in values.values():
            try:
                same_root = os.path.samefile(resolved, existing)
            except OSError:
                same_root = os.path.normcase(str(resolved)) == os.path.normcase(str(existing))
            if same_root:
                return None
        values[capability] = resolved
    return values


def _credential(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 4_096
        and all(ord(character) >= 0x20 and ord(character) != 0x7F for character in value)
    )


def _artifact_root(value: object) -> bool:
    return isinstance(value, Path) and value.is_absolute() and value.is_dir() and not value.is_symlink()


def _store_ready(capability: LogicalCapability, store: object) -> bool:
    if capability is LogicalCapability.SPEECH_STT:
        return isinstance(store, BoundedSpeechAudioStore) and not bool(getattr(store, "_closing", True))
    if capability in {LogicalCapability.SPEECH_TTS, LogicalCapability.MUSIC_GENERATION}:
        if not isinstance(store, MusicArtifactStore):
            return False
        root = getattr(store, "root", None)
        return isinstance(root, Path) and root.is_dir() and not root.is_symlink()
    if capability is LogicalCapability.VIDEO_GENERATION:
        if not isinstance(store, VideoArtifactStore):
            return False
        root = getattr(store, "root", None)
        return isinstance(root, Path) and root.is_dir() and not root.is_symlink()
    return False


def _runtime_publications(runtime: MediaProviderRuntime) -> dict[str, object]:
    values: dict[str, object] = {"media_provider_runtime": runtime}
    values.update({name: runtime.registry for name in _REGISTRY_PUBLICATIONS})
    for capability, store in runtime.stores.items():
        publication = _STORE_PUBLICATION_BY_CAPABILITY.get(capability)
        if publication is not None:
            values[publication] = store
    return values


def _all_publication_names() -> tuple[str, ...]:
    return (
        "media_provider_runtime",
        *_REGISTRY_PUBLICATIONS,
        *_STORE_PUBLICATION_BY_CAPABILITY.values(),
    )


def _default_stt_transport(api_key: str) -> OpenAITranscriptionTransport:
    return AiohttpOpenAITranscriptionTransport(api_key=api_key)


def _default_music_transport(api_key: str) -> ElevenLabsMusicTransport:
    return AiohttpElevenLabsMusicTransport(api_key=api_key)


def _default_video_transport(api_key: str) -> GeminiVeoTransport:
    return AiohttpGeminiVeoTransport(api_key=api_key)


__all__ = [
    "BoundSpeechAudioResolver",
    "MediaProviderCompositionCleanupError",
    "MediaProviderConfiguration",
    "MediaProviderRuntime",
    "MediaProviderRuntimeComposer",
    "MusicExecutionProofRegistry",
    "SQLiteMediaProviderAuditSink",
    "acquire_media_provider_runtime",
    "compose_media_provider_runtime",
    "compose_media_provider_runtime_from_settings",
    "media_provider_capability_ready",
    "media_provider_configuration_from_settings",
    "media_provider_runtime_current",
    "publish_media_provider_runtime",
    "release_media_provider_runtime",
    "unpublish_media_provider_runtime",
]
