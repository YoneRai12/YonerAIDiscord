from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.speech_transcription import (
    MAX_AUDIO_BYTES,
    SPEECH_TRANSCRIPTION_CAPABILITY_ID,
    SPEECH_TRANSCRIPTION_MODULE_ID,
    SPEECH_TRANSCRIPTION_PLUGIN_NAME,
    DiscordSpeechTranscriptionDelivery,
    SpeechTranscriptionAuthorizationError,
    SpeechTranscriptionContractError,
    SpeechTranscriptionPlugin,
    SpeechTranscriptionRequest,
    SpeechTranscriptionService,
    SpeechTranscriptionUnavailableError,
    setup,
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
    SpeechTranscriptionInput,
    TierRoute,
    require_execution_allowed,
)


@dataclass
class _Audit:
    records: list[AuditRecord] = field(default_factory=list)

    async def append(self, record: AuditRecord) -> None:
        self.records.append(record)


class _ProviderAdapter:
    provider_id = "static-stt"
    adapter_id = "test.static-stt"

    def __init__(self, *, result: str = "valid") -> None:
        self.result = result
        self.calls = 0
        self.last_request: ProviderRequest | None = None
        self.before_commit = None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            self.provider_id,
            HealthStatus.READY,
            datetime.now(UTC),
            probed_model_aliases=("stt.fast", "stt.balanced", "stt.quality"),
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
        if self.before_commit is not None:
            self.before_commit()
        await require_execution_allowed(execution_allowed)
        assert request.capability is LogicalCapability.SPEECH_STT
        assert isinstance(request.payload, SpeechTranscriptionInput)
        assert len(request.input_artifacts) == 1
        assert request.input_artifacts[0].kind is ArtifactKind.AUDIO

        text = "構造化された文字起こし結果"
        artifacts: tuple[ArtifactRef, ...] = ()
        if self.result == "oversized":
            text = "x" * 1_901
        elif self.result == "artifact":
            artifacts = (_audio("unexpected-audio"),)
        elif self.result == "mismatched-request":
            request = replace(request, request_id="other-request")
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
        QualityTier.FAST: "stt.fast",
        QualityTier.BALANCED: "stt.balanced",
        QualityTier.QUALITY: "stt.quality",
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
        module_id="test.speech-transcription",
        capabilities=(
            CapabilityPolicy(
                LogicalCapability.SPEECH_STT,
                True,
                RbacLevel.TRUSTED,
                RiskLevel.HIGH,
                requires_consent=kind is ProviderKind.API,
                audit_required=True,
            ),
        ),
        providers=(
            ProviderManifest(
                _ProviderAdapter.provider_id,
                kind,
                _ProviderAdapter.adapter_id,
                (LogicalCapability.SPEECH_STT,),
                resources,
                enabled=True,
                models=tuple(
                    ModelBinding(alias, "static-stt-model", probe_required=False) for alias in aliases.values()
                ),
            ),
        ),
        routes=(
            CapabilityRoute(
                LogicalCapability.SPEECH_STT,
                tuple(TierRoute(tier, (_ProviderAdapter.provider_id,), alias) for tier, alias in aliases.items()),
            ),
        ),
    )


async def _ready_runtime(
    *,
    kind: ProviderKind = ProviderKind.LOCAL,
    result: str = "valid",
    audit: bool = True,
) -> tuple[ProviderRegistry, _ProviderAdapter]:
    registry = ProviderRegistry(
        _manifest(kind=kind),
        audit_sink=_Audit() if audit else None,
    )
    adapter = _ProviderAdapter(result=result)
    registry.register_adapter(adapter)
    await registry.refresh_health(adapter.provider_id)
    return registry, adapter


def _audio(artifact_id: str = "audio-input") -> ArtifactRef:
    return ArtifactRef(
        artifact_id,
        ArtifactKind.AUDIO,
        "audio/wav",
        1_024,
        "a" * 64,
    )


def _request(
    *,
    request_id: str = "stt-request",
    guild_id: int = 10,
    channel_id: int = 20,
    actor_id: int = 30,
    audio: ArtifactRef | None = None,
    prompt: str = "sensitive pronunciation hint",
) -> SpeechTranscriptionRequest:
    return SpeechTranscriptionRequest(
        request_id,
        guild_id,
        channel_id,
        actor_id,
        audio or _audio(),
        language_code="ja-JP",
        prompt=prompt,
    )


def _audio_current(request: SpeechTranscriptionRequest):
    def current(ref: ArtifactRef, binding: str) -> bool:
        return ref is request.audio and binding == request.audio_binding

    return current


def test_typed_contract_requires_one_bounded_audio_and_hides_sensitive_input() -> None:
    request = _request()
    assert request.language_code == "ja-jp"
    assert request.tier is QualityTier.BALANCED
    assert request.audio_binding == request.fingerprint
    assert request.provider_request_id == f"stt-request-{request.audio_binding}"
    assert "sensitive pronunciation hint" not in repr(request)
    assert request.audio.artifact_id not in repr(request)
    assert "sensitive pronunciation hint" not in repr(SpeechTranscriptionInput(prompt="sensitive pronunciation hint"))

    for audio in (
        ArtifactRef("image", ArtifactKind.IMAGE, "image/png", 10, "a" * 64),
        ArtifactRef(
            "too-large",
            ArtifactKind.AUDIO,
            "audio/wav",
            MAX_AUDIO_BYTES + 1,
            "a" * 64,
        ),
        ArtifactRef("unknown-type", ArtifactKind.AUDIO, "audio/aac", 10, "a" * 64),
    ):
        with pytest.raises(ValueError, match="Stage 1 contract"):
            _request(audio=audio)

    provider_fields = dict(
        request_id="provider-request",
        trace_id="trace-provider-request",
        capability=LogicalCapability.SPEECH_STT,
        actor_ref="discord-user-30",
        payload=SpeechTranscriptionInput(),
    )
    with pytest.raises(ValueError, match="exactly one audio"):
        ProviderRequest(**provider_fields)
    with pytest.raises(ValueError, match="exactly one audio"):
        ProviderRequest(
            **provider_fields,
            input_artifacts=(_audio(), _audio("audio-two")),
        )


async def test_local_fake_adapter_uses_typed_input_and_is_idempotent() -> None:
    registry, adapter = await _ready_runtime()
    request = _request()
    service = SpeechTranscriptionService(
        registry,
        audio_artifact_current=_audio_current(request),
    )

    first = await service.transcribe(
        request,
        authorization_current=lambda: True,
    )
    second = await service.transcribe(
        request,
        authorization_current=lambda: True,
    )

    assert first is second
    assert first.text == "構造化された文字起こし結果"
    assert first.text not in repr(first)
    assert first.request_binding != request.audio_binding
    assert adapter.calls == 1
    assert adapter.last_request is not None
    assert adapter.last_request.input_artifacts == (request.audio,)
    assert adapter.last_request.payload == SpeechTranscriptionInput(
        language_code="ja-JP",
        prompt=request.prompt,
    )


@pytest.mark.parametrize(
    "mode",
    ("unbound", "health", "audit", "consent", "authorization", "audio"),
)
async def test_unready_or_revoked_inputs_fail_before_provider(mode: str) -> None:
    request = _request()
    adapter: _ProviderAdapter | None = None
    consent = True
    authorization = True
    audio_allowed = True
    if mode == "unbound":
        registry = None
    else:
        kind = ProviderKind.API if mode == "consent" else ProviderKind.LOCAL
        registry = ProviderRegistry(
            _manifest(kind=kind),
            audit_sink=None if mode == "audit" else _Audit(),
        )
        adapter = _ProviderAdapter()
        registry.register_adapter(adapter)
        if mode != "health":
            await registry.refresh_health(adapter.provider_id)
    if mode == "consent":
        consent = False
    elif mode == "authorization":
        authorization = False
    elif mode == "audio":
        audio_allowed = False
    service = SpeechTranscriptionService(
        registry,
        audio_artifact_current=lambda *_: audio_allowed,
        remote_consent_active=lambda _actor: consent,
    )

    with pytest.raises((SpeechTranscriptionAuthorizationError, SpeechTranscriptionUnavailableError)):
        await service.transcribe(
            request,
            authorization_current=lambda: authorization,
        )
    assert adapter is None or adapter.calls == 0


@pytest.mark.parametrize(
    ("result", "expected_error"),
    (
        ("oversized", SpeechTranscriptionContractError),
        ("artifact", SpeechTranscriptionContractError),
        ("mismatched-request", SpeechTranscriptionUnavailableError),
    ),
)
async def test_adapter_commit_revocation_and_invalid_results_are_rejected(
    result: str,
    expected_error: type[Exception],
) -> None:
    registry, adapter = await _ready_runtime(result=result)
    request = _request(request_id=f"stt-{result}")
    service = SpeechTranscriptionService(
        registry,
        audio_artifact_current=_audio_current(request),
    )
    with pytest.raises(expected_error):
        await service.transcribe(request, authorization_current=lambda: True)
    assert adapter.calls == 1

    registry, adapter = await _ready_runtime()
    request = _request(request_id="stt-revoked")
    allowed = True

    def revoke() -> None:
        nonlocal allowed
        allowed = False

    adapter.before_commit = revoke
    service = SpeechTranscriptionService(
        registry,
        audio_artifact_current=_audio_current(request),
    )
    with pytest.raises((SpeechTranscriptionAuthorizationError, SpeechTranscriptionUnavailableError)):
        await service.transcribe(
            request,
            authorization_current=lambda: allowed,
        )
    assert adapter.calls == 1

    registry, adapter = await _ready_runtime(kind=ProviderKind.API)
    request = _request(request_id="stt-consent-revoked")
    consent_allowed = True

    def revoke_consent() -> None:
        nonlocal consent_allowed
        consent_allowed = False

    adapter.before_commit = revoke_consent
    service = SpeechTranscriptionService(
        registry,
        audio_artifact_current=_audio_current(request),
        remote_consent_active=lambda _actor_id: consent_allowed,
    )
    with pytest.raises((SpeechTranscriptionAuthorizationError, SpeechTranscriptionUnavailableError)):
        await service.transcribe(
            request,
            authorization_current=lambda: True,
        )
    assert adapter.calls == 1


class _Followup:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, *args, **kwargs) -> None:
        self.messages.append({"args": args, **kwargs})


class _Interaction:
    def __init__(
        self,
        *,
        guild_id: int = 10,
        channel_id: int = 20,
        actor_id: int = 30,
    ) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user = SimpleNamespace(id=actor_id)
        self.followup = _Followup()


async def test_delivery_is_scope_bound_one_shot_and_rechecks_before_send() -> None:
    registry, adapter = await _ready_runtime()
    request = _request()
    service = SpeechTranscriptionService(
        registry,
        audio_artifact_current=_audio_current(request),
    )
    transcript = await service.transcribe(
        request,
        authorization_current=lambda: True,
    )
    assert (
        await service.claim_delivery(
            request,
            replace(transcript),
            authorization_current=lambda: True,
        )
        is False
    )
    assert (
        await service.claim_delivery(
            request,
            transcript,
            authorization_current=lambda: True,
        )
        is True
    )
    assert (
        await service.claim_delivery(
            request,
            transcript,
            authorization_current=lambda: True,
        )
        is False
    )

    second = _request(request_id="stt-delivery")
    delivery_service = SpeechTranscriptionService(
        registry,
        audio_artifact_current=_audio_current(second),
    )
    capability_allowed = True
    delivery = DiscordSpeechTranscriptionDelivery(
        delivery_service,
        capability_check=lambda *_: capability_allowed,
    )
    interaction = _Interaction()
    assert await delivery.deliver(interaction, second) is True
    assert adapter.calls == 2
    assert len(interaction.followup.messages) == 1
    sent = interaction.followup.messages[0]
    assert sent["args"] == ("構造化された文字起こし結果",)
    assert sent["ephemeral"] is True
    assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert second.prompt not in repr(sent)

    scoped = _request(request_id="stt-wrong-scope")
    assert (
        await delivery.deliver(
            _Interaction(channel_id=scoped.channel_id + 1),
            scoped,
        )
        is False
    )
    assert adapter.calls == 2

    third = _request(request_id="stt-final-recheck")
    final_service = SpeechTranscriptionService(
        registry,
        audio_artifact_current=_audio_current(third),
    )
    original_claim = final_service.claim_delivery

    async def revoke_after_claim(*args, **kwargs):
        nonlocal capability_allowed
        claimed = await original_claim(*args, **kwargs)
        capability_allowed = False
        return claimed

    final_service.claim_delivery = revoke_after_claim
    final_delivery = DiscordSpeechTranscriptionDelivery(
        final_service,
        capability_check=lambda *_: capability_allowed,
    )
    final_interaction = _Interaction()
    assert await final_delivery.deliver(final_interaction, third) is False
    assert final_interaction.followup.messages == []

    capability_allowed = True
    fourth = _request(request_id="stt-after-delivery-current")
    last_service = SpeechTranscriptionService(
        registry,
        audio_artifact_current=_audio_current(fourth),
    )
    original_delivery_current = last_service.delivery_current

    async def revoke_after_delivery_current(*args, **kwargs):
        nonlocal capability_allowed
        current = await original_delivery_current(*args, **kwargs)
        capability_allowed = False
        return current

    last_service.delivery_current = revoke_after_delivery_current
    last_delivery = DiscordSpeechTranscriptionDelivery(
        last_service,
        capability_check=lambda *_: capability_allowed,
    )
    last_interaction = _Interaction()
    assert await last_delivery.deliver(last_interaction, fourth) is False
    assert last_interaction.followup.messages == []


async def test_plugin_lifecycle_stays_unready_and_uses_fresh_trusted_member() -> None:
    bot = SimpleNamespace(is_closing=False)
    plugin = SpeechTranscriptionPlugin()
    await plugin.start(bot)
    assert bot.runtime_capability_readiness[SPEECH_TRANSCRIPTION_CAPABILITY_ID] is False
    assert bot.speech_transcription_service is plugin.service
    assert bot.speech_transcription_adapter is plugin.adapter
    assert SPEECH_TRANSCRIPTION_MODULE_ID == "media.speech-transcription"
    assert SPEECH_TRANSCRIPTION_PLUGIN_NAME == "speech_transcription"

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

        def currently_allowed(self, *_args, **_kwargs) -> bool:
            return True

    guard = _Guard()
    check = SpeechTranscriptionPlugin._capability_check(SimpleNamespace(is_closing=False, capability_guard=guard))
    interaction = SimpleNamespace(
        user=member,
        guild_id=10,
        guild=_Guild(),
    )
    assert await check(SPEECH_TRANSCRIPTION_CAPABILITY_ID, interaction) is False
    guard.actor_level = RbacLevel.TRUSTED
    assert await check(SPEECH_TRANSCRIPTION_CAPABILITY_ID, interaction) is True
    interaction.guild = SimpleNamespace(id=11, fetch_member=_Guild().fetch_member)
    assert await check(SPEECH_TRANSCRIPTION_CAPABILITY_ID, interaction) is False

    await plugin.stop()
    assert SPEECH_TRANSCRIPTION_CAPABILITY_ID not in bot.runtime_capability_readiness
    assert not hasattr(bot, "speech_transcription_service")
    assert not hasattr(bot, "speech_transcription_adapter")

    registrations: list[tuple[str, object]] = []
    setup(SimpleNamespace(register_plugin=lambda name, factory: registrations.append((name, factory))))
    assert registrations == [(SPEECH_TRANSCRIPTION_PLUGIN_NAME, SpeechTranscriptionPlugin)]
