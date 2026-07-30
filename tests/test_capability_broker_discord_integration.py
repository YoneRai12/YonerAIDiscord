from __future__ import annotations

from types import SimpleNamespace

import pytest

from yonerai_discord.capability_broker import (
    ArtifactKind,
    BackendCleanupReceipt,
    BackendExecution,
    BackendStatus,
    CapabilityBinding,
    CapabilityBroker,
    CapabilityKind,
    CapabilityRequest,
    CapabilityAuthorizationError,
    CleanupReason,
    HYPERV_MEDIA_BACKEND_ID,
    HYPERV_MEDIA_IDENTITY_DIGEST,
    InMemoryCapabilityAuditSink,
    MediaCapabilityInput,
)
from yonerai_discord.capability_broker.discord_media import BrokeredDiscordMediaInspectionAdapter


class _Backend:
    def __init__(self) -> None:
        self.executions = 0
        self.current_request = None

    async def status(self, policy_digest: str) -> BackendStatus:
        return BackendStatus(
            configured=True,
            ready=True,
            backend_id=HYPERV_MEDIA_BACKEND_ID,
            identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
            policy_digest=policy_digest,
        )

    async def execute(self, request):
        self.executions += 1
        self.current_request = request
        return BackendExecution(
            request_digest=request.request_digest,
            binding=request.binding,
            backend_id=HYPERV_MEDIA_BACKEND_ID,
            identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
            policy_digest=request.policy_digest,
            artifact_kind=ArtifactKind.INSPECTION_TEXT,
            text="bounded managed inspection evidence",
            cleanup_confirmed=True,
        )

    async def cleanup(self, request, _reason: CleanupReason) -> BackendCleanupReceipt:
        return BackendCleanupReceipt(
            request_digest=request.request_digest,
            backend_id=HYPERV_MEDIA_BACKEND_ID,
            identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
            worker_terminated=True,
            workspace_destroyed=True,
        )


def _message(*, guild_id: int = 10, channel_id: int = 20, actor_id: int = 30, message_id: int = 40):
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=actor_id),
        id=message_id,
    )


def _adapter(
    *,
    current: dict[str, bool] | None = None,
) -> tuple[BrokeredDiscordMediaInspectionAdapter, _Backend, InMemoryCapabilityAuditSink]:
    backend = _Backend()
    audit = InMemoryCapabilityAuditSink()
    broker = CapabilityBroker(
        backend=backend,
        audit_sink=audit,
        expected_backend_id=HYPERV_MEDIA_BACKEND_ID,
        expected_identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
    )
    state = current if current is not None else {"value": True}
    return BrokeredDiscordMediaInspectionAdapter(broker, broker_current=lambda: state["value"]), backend, audit


@pytest.mark.asyncio
async def test_brokered_message_adapter_binds_scope_replays_once_and_never_audits_raw_input() -> None:
    adapter, backend, audit = _adapter()
    message = _message()
    url = "https://www.youtube.com/watch?v=ABCDEFGHIJK"
    instruction = "inspect the spoken topic"

    first = await adapter.inspect_for_message(message, url, instruction, lambda: True)
    second = await adapter.inspect_for_message(message, url, instruction, lambda: True)

    assert first == second == "bounded managed inspection evidence"
    assert adapter.requires_external_ai_consent is False
    assert backend.executions == 1
    assert backend.current_request.binding == CapabilityBinding(actor_id=30, guild_id=10, conversation_id=20)
    assert backend.current_request.request_id == "discord:40"
    assert backend.current_request.idempotency_key == "discord:10:20:40"
    rendered = repr(audit.records)
    assert url not in rendered
    assert instruction not in rendered
    assert first not in rendered


@pytest.mark.asyncio
async def test_brokered_message_adapter_rechecks_authorization_after_backend_before_returning_evidence() -> None:
    adapter, backend, _audit = _adapter()
    # adapter entry, broker entry, post-STARTED audit, artifact-commit checks
    decisions = iter((True, True, True, False))

    result = await adapter.inspect_for_message(
        _message(),
        "https://www.youtube.com/watch?v=ABCDEFGHIJK",
        "inspect the spoken topic",
        lambda: next(decisions),
    )

    assert result is None
    assert backend.executions == 1


@pytest.mark.asyncio
async def test_brokered_message_adapter_fails_closed_before_execution_for_replacement_or_invalid_scope() -> None:
    current = {"value": False}
    adapter, backend, _audit = _adapter(current=current)

    replaced = await adapter.inspect_for_message(
        _message(),
        "https://www.youtube.com/watch?v=ABCDEFGHIJK",
        "inspect the spoken topic",
        lambda: True,
    )
    invalid_scope = await adapter.inspect_for_message(
        _message(channel_id=0),
        "https://www.youtube.com/watch?v=ABCDEFGHIJK",
        "inspect the spoken topic",
        lambda: True,
    )

    assert replaced is None
    assert invalid_scope is None
    assert backend.executions == 0


@pytest.mark.asyncio
async def test_broker_rechecks_authorization_after_started_audit_before_backend_execution() -> None:
    allowed = {"value": True}

    class _RevokingAudit(InMemoryCapabilityAuditSink):
        async def append(self, record) -> None:
            await super().append(record)
            if record.outcome.value == "started":
                allowed["value"] = False

    backend = _Backend()
    audit = _RevokingAudit()
    broker = CapabilityBroker(
        backend=backend,
        audit_sink=audit,
        expected_backend_id=HYPERV_MEDIA_BACKEND_ID,
        expected_identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
    )
    request = CapabilityRequest(
        request_id="discord:40",
        idempotency_key="discord:10:20:40",
        binding=CapabilityBinding(actor_id=30, guild_id=10, conversation_id=20),
        capability=CapabilityKind.MEDIA_INSPECTION,
        payload=MediaCapabilityInput(
            "https://www.youtube.com/watch?v=ABCDEFGHIJK",
            "inspect the spoken topic",
        ),
    )

    with pytest.raises(CapabilityAuthorizationError):
        await broker.execute(
            request,
            permission_current=lambda *_args: True,
            authorization_current=lambda: allowed["value"],
        )

    assert backend.executions == 0
    assert audit.records[-1].failure_code == "authorization_changed_before_execution"
