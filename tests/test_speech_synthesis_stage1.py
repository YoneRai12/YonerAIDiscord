from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.music_generation.artifacts import (
    MusicArtifactStore,
    validate_wav,
)
from yonerai_discord.modules.speech_synthesis import (
    GENERATED_SPEECH_FILENAME,
    MAX_SPEECH_TEXT_CHARS,
    SPEECH_SYNTHESIS_CAPABILITY_ID,
    SPEECH_SYNTHESIS_MODULE_ID,
    SPEECH_SYNTHESIS_PLUGIN_NAME,
    STANDARD_VOICE_ALIASES,
    SpeechSynthesisAuthorizationError,
    SpeechSynthesisContractError,
    SpeechSynthesisDelivery,
    SpeechSynthesisPlugin,
    SpeechSynthesisRequest,
    SpeechSynthesisService,
    SpeechSynthesisUnavailableError,
    setup,
    speech_artifact_request_binding,
)
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    AuditRecord,
    CapabilityPolicy,
    CapabilityRoute,
    HealthStatus,
    LogicalCapability,
    ModelBinding,
    ProviderCatalogManifest,
    ProviderHealth,
    ProviderInvocation,
    ProviderKind,
    ProviderManifest,
    ProviderRegistry,
    ProviderRequest,
    ProviderResult,
    QualityTier,
    ResourceProfile,
    ResourceTarget,
    SpeechSynthesisInput,
    TierRoute,
    load_default_catalog,
    require_execution_allowed,
)


def _wav(*, sample_rate: int = 44_100, channels: int = 1, seconds: int = 1) -> bytes:
    data = b"\0" * (sample_rate * channels * 2 * seconds)
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 16)
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


@dataclass
class _Audit:
    records: list[AuditRecord] = field(default_factory=list)

    async def append(self, record: AuditRecord) -> None:
        self.records.append(record)


class _Store:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True)
        self.inner = MusicArtifactStore(root)
        self.put_calls = 0
        self.read_calls = 0
        self.read_override: bytes | None = None

    def put_wav(
        self,
        data: bytes,
        *,
        request_binding: str,
        artifact_id: str | None = None,
    ) -> ArtifactRef:
        self.put_calls += 1
        return self.inner.put_wav(
            data,
            request_binding=request_binding,
            artifact_id=artifact_id,
        )

    def read_wav(self, ref: ArtifactRef, *, request_binding: str, read_allowed=None) -> bytes:
        self.read_calls += 1
        if self.read_override is not None:
            return self.read_override
        return self.inner.read_wav(
            ref,
            request_binding=request_binding,
            read_allowed=read_allowed,
        )


class _Provider:
    provider_id = "static-tts"
    adapter_id = "test.static-tts"

    def __init__(
        self,
        store: _Store,
        *,
        mode: str = "valid",
        before_commit=None,
        provider_id: str | None = None,
        adapter_id: str | None = None,
    ) -> None:
        self.store = store
        self.mode = mode
        self.before_commit = before_commit
        if provider_id is not None:
            self.provider_id = provider_id
        if adapter_id is not None:
            self.adapter_id = adapter_id
        self.calls = 0
        self.last_request: ProviderRequest | None = None
        self.replay_ref: ArtifactRef | None = None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            self.provider_id,
            HealthStatus.READY,
            datetime.now(UTC),
            probed_model_aliases=("tts.fast", "tts.balanced", "tts.quality"),
        )

    async def execute(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        execution_allowed=None,
    ) -> ProviderResult:
        self.calls += 1
        self.last_request = request
        assert request.capability is LogicalCapability.SPEECH_TTS
        assert isinstance(request.payload, SpeechSynthesisInput)
        assert request.input_artifacts == ()
        if self.before_commit is not None:
            self.before_commit()
        await require_execution_allowed(execution_allowed)
        binding = speech_artifact_request_binding(
            request,
            provider_id=self.provider_id,
            provider_model=invocation.provider_model,
            model_alias=invocation.model_alias,
            quality_tier=invocation.quality_tier,
        )
        if self.mode == "invalid-wav":
            invalid_wav = b"not-a-wav"
            self.store.read_override = invalid_wav
            artifact = ArtifactRef(
                "aud-invalid",
                ArtifactKind.AUDIO,
                "audio/wav",
                len(invalid_wav),
                hashlib.sha256(invalid_wav).hexdigest(),
            )
        else:
            artifact = self.replay_ref or self.store.put_wav(
                _wav(sample_rate=48_000),
                request_binding=binding,
            )
        text = ""
        artifacts = (artifact,)
        if self.mode == "text":
            text = "provider text must not be returned"
        elif self.mode == "extra":
            artifacts = (artifact, artifact)
        elif self.mode == "wrong-kind":
            artifacts = (
                ArtifactRef(
                    artifact.artifact_id,
                    ArtifactKind.IMAGE,
                    "image/png",
                    artifact.size_bytes,
                    artifact.sha256,
                ),
            )
        elif self.mode == "wrong-mime":
            artifacts = (
                ArtifactRef(
                    artifact.artifact_id,
                    ArtifactKind.AUDIO,
                    "audio/mpeg",
                    artifact.size_bytes,
                    artifact.sha256,
                ),
            )
        elif self.mode == "wrong-hash":
            artifacts = (replace(artifact, sha256="0" * 64),)
        return ProviderResult(
            request.request_id,
            self.provider_id,
            invocation.provider_model,
            text=text,
            artifacts=artifacts,
        )

    async def close(self) -> None:
        return None


def _manifest(*, kind: ProviderKind = ProviderKind.LOCAL) -> ProviderCatalogManifest:
    aliases = {
        QualityTier.FAST: "tts.fast",
        QualityTier.BALANCED: "tts.balanced",
        QualityTier.QUALITY: "tts.quality",
    }
    resources = (
        ResourceProfile.remote(max_concurrency=1)
        if kind is ProviderKind.API
        else ResourceProfile(
            target=ResourceTarget.CPU,
            max_concurrency=1,
            system_ram_budget_mb=1_024,
        )
    )
    return ProviderCatalogManifest(
        schema_version=1,
        module_id="test.speech-synthesis",
        capabilities=(
            CapabilityPolicy(
                LogicalCapability.SPEECH_TTS,
                True,
                RbacLevel.TRUSTED,
                RiskLevel.MEDIUM,
                requires_consent=kind is ProviderKind.API,
                audit_required=True,
            ),
        ),
        providers=(
            ProviderManifest(
                _Provider.provider_id,
                kind,
                _Provider.adapter_id,
                (LogicalCapability.SPEECH_TTS,),
                resources,
                enabled=True,
                models=tuple(
                    ModelBinding(alias, "static-tts-model", probe_required=False) for alias in aliases.values()
                ),
            ),
        ),
        routes=(
            CapabilityRoute(
                LogicalCapability.SPEECH_TTS,
                tuple(TierRoute(tier, (_Provider.provider_id,), alias) for tier, alias in aliases.items()),
            ),
        ),
    )


def _mixed_kind_manifest() -> ProviderCatalogManifest:
    aliases = {
        QualityTier.FAST: "tts.fast",
        QualityTier.BALANCED: "tts.balanced",
        QualityTier.QUALITY: "tts.quality",
    }
    providers = (
        ProviderManifest(
            "local-tts",
            ProviderKind.LOCAL,
            "test.local-tts",
            (LogicalCapability.SPEECH_TTS,),
            ResourceProfile(
                target=ResourceTarget.CPU,
                max_concurrency=1,
                system_ram_budget_mb=1_024,
            ),
            enabled=True,
            models=tuple(ModelBinding(alias, "local-tts-model", probe_required=False) for alias in aliases.values()),
        ),
        ProviderManifest(
            "api-tts",
            ProviderKind.API,
            "test.api-tts",
            (LogicalCapability.SPEECH_TTS,),
            ResourceProfile.remote(max_concurrency=1),
            enabled=True,
            models=tuple(ModelBinding(alias, "api-tts-model", probe_required=False) for alias in aliases.values()),
        ),
    )
    return ProviderCatalogManifest(
        schema_version=1,
        module_id="test.speech-synthesis-mixed",
        capabilities=(
            CapabilityPolicy(
                LogicalCapability.SPEECH_TTS,
                True,
                RbacLevel.TRUSTED,
                RiskLevel.MEDIUM,
                requires_consent=True,
                audit_required=True,
            ),
        ),
        providers=providers,
        routes=(
            CapabilityRoute(
                LogicalCapability.SPEECH_TTS,
                tuple(TierRoute(tier, ("local-tts", "api-tts"), alias) for tier, alias in aliases.items()),
            ),
        ),
    )


async def _runtime(
    tmp_path: Path,
    *,
    kind: ProviderKind = ProviderKind.LOCAL,
    mode: str = "valid",
    audit: bool = True,
    health: bool = True,
    before_commit=None,
) -> tuple[ProviderRegistry, _Provider, _Store]:
    store = _Store(tmp_path / "speech")
    registry = ProviderRegistry(
        _manifest(kind=kind),
        audit_sink=_Audit() if audit else None,
    )
    provider = _Provider(store, mode=mode, before_commit=before_commit)
    registry.register_adapter(provider)
    if health:
        await registry.refresh_health(provider.provider_id)
    return registry, provider, store


def _request(
    *,
    request_id: str = "tts-stage1",
    guild_id: int = 10,
    channel_id: int = 20,
    actor_id: int = 30,
    text: str = "秘密を含む読み上げ本文",
    language_code: str = "ja-JP",
    tier: QualityTier = QualityTier.BALANCED,
) -> SpeechSynthesisRequest:
    return SpeechSynthesisRequest(
        request_id,
        guild_id,
        channel_id,
        actor_id,
        text,
        voice_alias="standard",
        language_code=language_code,
        tier=tier,
    )


def test_typed_contract_hides_text_and_rejects_voice_clone_inputs() -> None:
    request = _request()
    assert request.voice_alias in STANDARD_VOICE_ALIASES
    assert request.language_code == "ja-jp"
    assert "秘密を含む読み上げ本文" not in repr(request)
    assert "秘密を含む読み上げ本文" not in repr(SpeechSynthesisInput("秘密を含む読み上げ本文"))

    with pytest.raises(ValueError, match="Stage 1 range"):
        _request(text="x" * (MAX_SPEECH_TEXT_CHARS + 1))
    with pytest.raises(ValueError, match="speech text"):
        SpeechSynthesisInput("x" * (MAX_SPEECH_TEXT_CHARS + 1))
    for alias in ("speaker-1", "alice", "voice-clone", "reference-audio"):
        with pytest.raises(ValueError, match="standard Stage 1 voice"):
            SpeechSynthesisInput("text", voice_alias=alias)
        with pytest.raises(ValueError, match="standard Stage 1 voice"):
            replace(request, voice_alias=alias)

    audio = ArtifactRef("sample", ArtifactKind.AUDIO, "audio/wav", 44, "a" * 64)
    with pytest.raises(ValueError, match="does not accept input artifacts"):
        ProviderRequest(
            request_id="tts-direct",
            trace_id="trace-tts-direct",
            capability=LogicalCapability.SPEECH_TTS,
            actor_ref="discord-user-30",
            payload=SpeechSynthesisInput("text", voice_alias="standard", language_code="ja-JP"),
            input_artifacts=(audio,),
        )

    fingerprints = {
        request.fingerprint,
        replace(request, request_id="tts-other").fingerprint,
        replace(request, guild_id=11).fingerprint,
        replace(request, channel_id=21).fingerprint,
        replace(request, actor_id=31).fingerprint,
        replace(request, text="別の本文").fingerprint,
        replace(request, language_code="en-US").fingerprint,
        replace(request, tier=QualityTier.QUALITY).fingerprint,
    }
    assert len(fingerprints) == 8


async def test_local_fake_uses_typed_request_and_real_wav_store_idempotently(tmp_path: Path) -> None:
    registry, provider, store = await _runtime(tmp_path)
    request = _request()
    service = SpeechSynthesisService(registry, store)

    first = await service.synthesize(request, authorization_current=lambda: True)
    second = await service.synthesize(request, authorization_current=lambda: True)

    assert first is second
    assert provider.calls == 1
    assert store.put_calls == 1
    assert provider.last_request is not None
    assert provider.last_request.input_artifacts == ()
    assert provider.last_request.payload == SpeechSynthesisInput(
        request.text,
        voice_alias="standard",
        language_code="ja-JP",
    )
    assert request.text not in repr(first)
    assert validate_wav(first.wav).duration_seconds == 1
    assert first.artifact.size_bytes == len(first.wav)
    assert first.artifact.sha256 == hashlib.sha256(first.wav).hexdigest()


@pytest.mark.parametrize(
    "mode",
    ("registry", "store", "health", "audit", "authorization", "consent", "shutdown"),
)
async def test_unready_or_revoked_boundaries_fail_before_provider(
    tmp_path: Path,
    mode: str,
) -> None:
    request = _request(request_id=f"tts-{mode}")
    consent = True
    authorization = True
    provider: _Provider | None = None
    if mode == "registry":
        registry = None
        store = _Store(tmp_path / "speech")
    else:
        kind = ProviderKind.API if mode == "consent" else ProviderKind.LOCAL
        registry, provider, store = await _runtime(
            tmp_path,
            kind=kind,
            audit=mode != "audit",
            health=mode != "health",
        )
    if mode == "store":
        store = None
    elif mode == "authorization":
        authorization = False
    elif mode == "consent":
        consent = False
    service = SpeechSynthesisService(
        registry,
        store,
        remote_consent_active=lambda _actor: consent,
    )
    if mode == "shutdown":
        service.begin_close()

    with pytest.raises(
        (SpeechSynthesisAuthorizationError, SpeechSynthesisUnavailableError),
    ):
        await service.synthesize(
            request,
            authorization_current=lambda: authorization,
        )
    assert provider is None or provider.calls == 0
    if store is not None:
        assert store.put_calls == 0


async def test_invalid_store_and_mixed_local_api_route_fail_before_provider(tmp_path: Path) -> None:
    registry, provider, _store = await _runtime(tmp_path)
    with pytest.raises(TypeError, match="read_wav"):
        SpeechSynthesisService(registry, object())
    assert provider.calls == 0

    mixed_store = _Store(tmp_path / "mixed")
    mixed_registry = ProviderRegistry(_mixed_kind_manifest(), audit_sink=_Audit())
    local = _Provider(
        mixed_store,
        provider_id="local-tts",
        adapter_id="test.local-tts",
    )
    api = _Provider(
        mixed_store,
        provider_id="api-tts",
        adapter_id="test.api-tts",
    )
    mixed_registry.register_adapter(local)
    mixed_registry.register_adapter(api)
    await mixed_registry.refresh_health(local.provider_id)
    await mixed_registry.refresh_health(api.provider_id)
    mixed_registry.set_provider_enabled(local.provider_id, False)
    service = SpeechSynthesisService(
        mixed_registry,
        mixed_store,
        remote_consent_active=lambda _actor: True,
    )
    with pytest.raises(SpeechSynthesisUnavailableError, match="mixed local and remote"):
        await service.synthesize(
            _request(request_id="tts-no-local-api-fallback"),
            authorization_current=lambda: True,
        )
    assert local.calls == 0
    assert api.calls == 0
    assert mixed_store.put_calls == 0


@pytest.mark.parametrize(
    "mode",
    ("text", "extra", "wrong-kind", "wrong-mime", "wrong-hash", "invalid-wav"),
)
async def test_provider_result_contract_and_integrity_are_fail_closed(
    tmp_path: Path,
    mode: str,
) -> None:
    registry, provider, store = await _runtime(tmp_path, mode=mode)
    service = SpeechSynthesisService(registry, store)

    with pytest.raises((SpeechSynthesisContractError, SpeechSynthesisUnavailableError)):
        await service.synthesize(
            _request(request_id=f"tts-{mode}"),
            authorization_current=lambda: True,
        )
    assert provider.calls == 1


async def test_api_consent_and_adapter_commit_revocation_have_no_fallback(tmp_path: Path) -> None:
    consent = True

    def revoke() -> None:
        nonlocal consent
        consent = False

    registry, provider, store = await _runtime(
        tmp_path,
        kind=ProviderKind.API,
        before_commit=revoke,
    )
    service = SpeechSynthesisService(
        registry,
        store,
        remote_consent_active=lambda _actor: consent,
    )
    with pytest.raises(
        (SpeechSynthesisAuthorizationError, SpeechSynthesisUnavailableError),
    ):
        await service.synthesize(
            _request(request_id="tts-consent-race"),
            authorization_current=lambda: True,
        )
    assert provider.calls == 1
    assert store.put_calls == 0


async def test_cross_request_artifact_replay_is_rejected(tmp_path: Path) -> None:
    registry, provider, store = await _runtime(tmp_path)
    service = SpeechSynthesisService(registry, store)
    first = await service.synthesize(
        _request(request_id="tts-first"),
        authorization_current=lambda: True,
    )
    provider.replay_ref = first.artifact

    with pytest.raises(SpeechSynthesisContractError):
        await service.synthesize(
            _request(request_id="tts-second", channel_id=21),
            authorization_current=lambda: True,
        )
    assert provider.calls == 2
    assert store.put_calls == 1


class _Sink:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_wav(self, interaction: Any, wav: bytes, **kwargs) -> None:
        self.messages.append({"interaction": interaction, "wav": wav, **kwargs})


class _Interaction:
    def __init__(self, *, guild_id: int = 10, channel_id: int = 20, actor_id: int = 30) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user = SimpleNamespace(id=actor_id)


async def test_delivery_is_scope_bound_one_shot_and_rechecks_consent_before_send(tmp_path: Path) -> None:
    registry, provider, store = await _runtime(tmp_path)
    request = _request(request_id="tts-delivery")
    service = SpeechSynthesisService(registry, store)
    synthesized = await service.synthesize(request, authorization_current=lambda: True)
    assert await service.claim_delivery(request, replace(synthesized), authorization_current=lambda: True) is False
    assert await service.claim_delivery(request, synthesized, authorization_current=lambda: True) is True
    assert await service.claim_delivery(request, synthesized, authorization_current=lambda: True) is False

    second = _request(request_id="tts-delivery-ok")
    delivery_service = SpeechSynthesisService(registry, store)
    sink = _Sink()
    delivery = SpeechSynthesisDelivery(
        delivery_service,
        sink,
        capability_check=lambda *_: True,
    )
    interaction = _Interaction()
    assert await delivery.deliver(interaction, second) is True
    assert len(sink.messages) == 1
    sent = sink.messages[0]
    assert sent["filename"] == GENERATED_SPEECH_FILENAME
    assert sent["ephemeral"] is True
    assert sent["mentions_allowed"] is False
    assert second.text not in repr(sent)
    assert await delivery.deliver(_Interaction(channel_id=99), _request(request_id="wrong-scope")) is False

    api_registry, _, api_store = await _runtime(tmp_path / "api", kind=ProviderKind.API)
    third = _request(request_id="tts-final-consent")
    consent = True
    api_service = SpeechSynthesisService(
        api_registry,
        api_store,
        remote_consent_active=lambda _actor: consent,
    )
    after_claim = False
    original_claim = api_service.claim_delivery

    async def mark_after_claim(*args, **kwargs):
        nonlocal after_claim
        claimed = await original_claim(*args, **kwargs)
        after_claim = claimed
        return claimed

    api_service.claim_delivery = mark_after_claim

    def revoke_consent_on_final_capability_check(*_args):
        nonlocal consent
        if after_claim:
            consent = False
        return True

    denied_sink = _Sink()
    denied_delivery = SpeechSynthesisDelivery(
        api_service,
        denied_sink,
        capability_check=revoke_consent_on_final_capability_check,
    )
    assert await denied_delivery.deliver(_Interaction(), third) is False
    assert denied_sink.messages == []

    final_registry, _, final_store = await _runtime(tmp_path / "final-capability")
    final_request = _request(request_id="tts-final-capability")
    final_service = SpeechSynthesisService(final_registry, final_store)
    allowed = True
    original_current = final_service.delivery_current

    async def revoke_after_current(*args, **kwargs):
        nonlocal allowed
        current = await original_current(*args, **kwargs)
        allowed = False
        return current

    final_service.delivery_current = revoke_after_current
    final_sink = _Sink()
    final_delivery = SpeechSynthesisDelivery(
        final_service,
        final_sink,
        capability_check=lambda *_: allowed,
    )
    assert await final_delivery.deliver(_Interaction(), final_request) is False
    assert final_sink.messages == []


async def test_plugin_stays_unready_and_does_not_touch_existing_voice_runtime() -> None:
    speech_queue = object()
    music_service = object()
    ducking_mixer = object()
    bot = SimpleNamespace(
        is_closing=False,
        speech_queue=speech_queue,
        music_service=music_service,
        ducking_mixer=ducking_mixer,
    )
    plugin = SpeechSynthesisPlugin()
    await plugin.start(bot)
    assert bot.runtime_capability_readiness[SPEECH_SYNTHESIS_CAPABILITY_ID] is False
    assert bot.speech_synthesis_service is plugin.service
    assert plugin.adapter is not None
    assert bot.speech_queue is speech_queue
    assert bot.music_service is music_service
    assert bot.ducking_mixer is ducking_mixer
    assert SPEECH_SYNTHESIS_MODULE_ID == "media.speech-synthesis"
    assert SPEECH_SYNTHESIS_PLUGIN_NAME == "speech_synthesis"

    member = SimpleNamespace(id=30)

    class _Guild:
        id = 10

        async def fetch_member(self, user_id: int):
            assert user_id == member.id
            return member

    class _Guard:
        actor_level = RbacLevel.EVERYONE

        async def evaluate_fresh_member(self, *_args, **_kwargs):
            return SimpleNamespace(allowed=True, actor_level=self.actor_level)

        def currently_allowed(self, *_args, **_kwargs):
            return True

    guard = _Guard()
    check = SpeechSynthesisPlugin._capability_check(
        SimpleNamespace(is_closing=False, capability_guard=guard),
    )
    interaction = SimpleNamespace(user=member, guild_id=10, guild=_Guild())
    assert await check(SPEECH_SYNTHESIS_CAPABILITY_ID, interaction) is False
    guard.actor_level = RbacLevel.TRUSTED
    assert await check(SPEECH_SYNTHESIS_CAPABILITY_ID, interaction) is True
    interaction.guild = SimpleNamespace(id=11, fetch_member=_Guild().fetch_member)
    assert await check(SPEECH_SYNTHESIS_CAPABILITY_ID, interaction) is False

    catalog = load_default_catalog()
    policy = next(item for item in catalog.capabilities if item.capability is LogicalCapability.SPEECH_TTS)
    route = catalog.route(LogicalCapability.SPEECH_TTS)
    assert policy.default_enabled is False
    assert policy.required_rbac is RbacLevel.TRUSTED
    assert policy.risk is RiskLevel.MEDIUM
    assert route is not None
    assert all(not tier.provider_ids for tier in route.tiers)

    await plugin.stop()
    assert SPEECH_SYNTHESIS_CAPABILITY_ID not in bot.runtime_capability_readiness
    assert not hasattr(bot, "speech_synthesis_service")
    assert bot.speech_queue is speech_queue
    assert bot.music_service is music_service
    assert bot.ducking_mixer is ducking_mixer

    registrations: list[tuple[str, object]] = []
    setup(SimpleNamespace(register_plugin=lambda name, factory: registrations.append((name, factory))))
    assert registrations == [(SPEECH_SYNTHESIS_PLUGIN_NAME, SpeechSynthesisPlugin)]
