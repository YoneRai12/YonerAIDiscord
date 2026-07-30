from __future__ import annotations

import hashlib

import pytest

from tests.test_ai_mention import (
    FakeMessage,
    _FreshSynthesisService,
    _enable_media_synthesis_action,
    _listener,
)
from yonerai_discord.capability_broker import (
    AuditOutcome,
    BrokerResult,
    CapabilityBinding,
    CapabilityBroker,
    CapabilityRequest,
    HYPERV_MEDIA_BACKEND_ID,
    HyperVMediaManagedBackend,
    InMemoryCapabilityAuditSink,
)
from yonerai_discord.capability_broker.contract import AuthorizationCurrent, PermissionCurrent
from yonerai_discord.capability_broker.discord_media import (
    BrokeredDiscordMediaInspectionAdapter,
)
from yonerai_discord.modules.ai.discord_renderer import DiscordAIResponseRenderer
from yonerai_discord.modules.ai.task_progress import DiscordAITaskProgressRenderer
from yonerai_discord.modules.media_inspection.domain import MediaInspectionResult
from yonerai_discord.modules.media_inspection.hyperv_contract import (
    HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
    HYPERV_MEDIA_IDENTITY_DIGEST,
    HyperVMediaExecutionResult,
    HyperVMediaProbeResult,
)


_PROMPT = "<@99> https://youtube.com/shorts/TG9KgEss-TE を字幕と画像で詳しく分析して"
_EVIDENCE = "隔離Hyper-V workerが返した、範囲内の字幕・OCR根拠です。"


class _TrustedHyperVProvider:
    def __init__(self, evidence: str) -> None:
        self.evidence = evidence
        self.probe_calls = 0
        self.inspect_calls = 0
        self.close_calls = 0
        self.attestation: HyperVMediaProbeResult | HyperVMediaExecutionResult | None = None

    async def probe(self) -> HyperVMediaProbeResult:
        self.probe_calls += 1
        result = HyperVMediaProbeResult(
            ready=True,
            cleanup_confirmed=True,
            identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
            effective_policy_revision=HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
            effective_policy_digest=HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
        )
        self.attestation = result
        return result

    async def inspect(self, url: str, instruction: str) -> MediaInspectionResult:
        assert url == "https://youtube.com/shorts/TG9KgEss-TE"
        assert instruction
        self.inspect_calls += 1
        self.attestation = HyperVMediaExecutionResult(
            text=self.evidence,
            cleanup_confirmed=True,
            identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
            effective_policy_revision=HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
            effective_policy_digest=HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
        )
        return MediaInspectionResult(self.evidence)

    async def close(self) -> None:
        self.close_calls += 1


class _RecordingCapabilityBroker(CapabilityBroker):
    def __init__(
        self,
        backend: HyperVMediaManagedBackend,
        audit_sink: InMemoryCapabilityAuditSink,
    ) -> None:
        super().__init__(
            backend=backend,
            audit_sink=audit_sink,
            expected_backend_id=HYPERV_MEDIA_BACKEND_ID,
            expected_identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
        )
        self.results: list[BrokerResult] = []

    async def execute(
        self,
        request: CapabilityRequest,
        *,
        permission_current: PermissionCurrent,
        authorization_current: AuthorizationCurrent,
    ) -> BrokerResult:
        result = await super().execute(
            request,
            permission_current=permission_current,
            authorization_current=authorization_current,
        )
        self.results.append(result)
        return result


def _vertical(
    message: FakeMessage,
    service: _FreshSynthesisService,
) -> tuple[
    object,
    _TrustedHyperVProvider,
    _RecordingCapabilityBroker,
    InMemoryCapabilityAuditSink,
    object,
]:
    listener = _listener(
        service,  # type: ignore[arg-type]
        provider_is_local=True,
        provider_available=True,
        response_renderer=DiscordAIResponseRenderer(),
        task_progress_renderer=DiscordAITaskProgressRenderer(),
    )
    router, spec = _enable_media_synthesis_action(listener, message, evidence="fixture-placeholder")
    provider = _TrustedHyperVProvider(_EVIDENCE)
    audit = InMemoryCapabilityAuditSink()
    broker = _RecordingCapabilityBroker(HyperVMediaManagedBackend(provider), audit)
    adapter = BrokeredDiscordMediaInspectionAdapter(broker, broker_current=lambda: True)
    listener.bot.media_url_inspection_adapter = adapter
    return listener, provider, broker, audit, spec


def _rendered_text(message: FakeMessage) -> str:
    parts: list[str] = []
    for content, kwargs in message.replies:
        parts.append(content)
        embed = kwargs.get("embed")
        parts.append(str(getattr(embed, "description", "")))
    for reply in message.reply_messages:
        for edit in reply.edits:
            parts.append(str(edit.get("content", "")))
            parts.append(str(getattr(edit.get("embed"), "description", "")))
    for content, kwargs in message.channel.sent:
        parts.append(content)
        parts.append(str(getattr(kwargs.get("embed"), "description", "")))
    return "\n".join(parts)


@pytest.mark.asyncio
async def test_hyperv_vertical_synthesizes_on_one_progress_message_and_replays_once() -> None:
    message = FakeMessage(_PROMPT, message_id=8_301)
    service = _FreshSynthesisService()
    listener, provider, broker, audit, _spec = _vertical(message, service)

    await listener.on_message(message)  # type: ignore[arg-type]

    assert provider.probe_calls == 1
    assert provider.inspect_calls == 1
    assert provider.close_calls == 0
    assert service.provider_calls == 1
    assert len(broker.results) == 1
    result = broker.results[0]
    descriptor = result.receipt.artifact
    assert descriptor.owner == CapabilityBinding(actor_id=20, guild_id=10, conversation_id=30)
    assert descriptor.sha256 == hashlib.sha256(_EVIDENCE.encode("utf-8")).hexdigest()
    assert result.artifact.read_text(binding=descriptor.owner) == _EVIDENCE
    assert len(message.replies) == 1
    assert len(message.reply_messages) == 1
    assert message.channel.sent == []
    assert "タスク / ステータス" in message.replies[0][1]["embed"].title
    assert message.reply_messages[0].edits[-1]["embed"].description == "SYNTHESIZED_FINAL"
    first_render = _rendered_text(message)
    first_edit_count = len(message.reply_messages[0].edits)

    await listener.on_message(message)  # type: ignore[arg-type]

    assert provider.probe_calls == 1
    assert provider.inspect_calls == 1
    assert service.provider_calls == 1
    assert len(message.replies) == 1
    assert len(message.reply_messages[0].edits) == first_edit_count
    assert _rendered_text(message) == first_render
    assert [record.outcome for record in audit.records] == [
        AuditOutcome.STARTED,
        AuditOutcome.SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_hyperv_vertical_fresh_revoke_after_synthesis_blocks_final_content() -> None:
    message = FakeMessage(_PROMPT, message_id=8_302)
    service = _FreshSynthesisService()
    listener, provider, _broker, _audit, spec = _vertical(message, service)
    service.after_provider = lambda: listener.bot.capability_guard.capability_states.__setitem__(
        spec.capability_id,
        False,
    )

    await listener.on_message(message)  # type: ignore[arg-type]

    assert provider.inspect_calls == 1
    assert service.provider_calls == 1
    assert "SYNTHESIZED_FINAL" not in _rendered_text(message)
    assert message.channel.sent == []
