from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest

from yonerai_discord.execution_gateway.core_contract import (
    CORE_FACTS_EXTENSION,
    CORE_V01_OPTIONS_EXTENSION,
    CoreCancelOutcomeV01,
    CoreContractError,
    CoreMessageRequestV01,
    CoreRunOptionsV01,
    CoreRunReferenceV01,
    CoreToolResultV01,
    DiscordCoreFacts,
    project_core_message_v01,
)
from yonerai_discord.execution_gateway.core_v01 import (
    CoreV01AdapterError,
    CoreV01AuthorizationError,
    YonerAIInternalRunGatewayV01,
)
from yonerai_discord.execution_gateway.local import IdempotencyConflictError
from yonerai_discord.execution_gateway.models import CapabilityResult, RunEvent, RunInput


class _FakePort:
    def __init__(
        self,
        events: tuple[Mapping[str, object], ...],
        *,
        wait_for_result: bool = False,
        cancel_outcome: CoreCancelOutcomeV01 | None = None,
    ) -> None:
        self.raw_events = events
        self.wait_for_result = wait_for_result
        self.cancel_outcome = cancel_outcome or CoreCancelOutcomeV01.unsupported()
        self.starts: list[CoreMessageRequestV01] = []
        self.results: list[tuple[str, CoreToolResultV01]] = []
        self.cancels: list[str] = []
        self.result_received = asyncio.Event()
        self.remote_started = asyncio.Event()

    async def start(self, request: CoreMessageRequestV01) -> CoreRunReferenceV01:
        self.starts.append(request)
        self.remote_started.set()
        return CoreRunReferenceV01(f"remote-{len(self.starts)}")

    def events(self, run_id: str) -> AsyncIterator[Mapping[str, object]]:
        async def stream() -> AsyncIterator[Mapping[str, object]]:
            for event in self.raw_events:
                yield event
                if self.wait_for_result and event.get("event") == "tool_start":
                    await self.result_received.wait()

        return stream()

    async def submit_result(self, run_id: str, result: CoreToolResultV01) -> None:
        self.results.append((run_id, result))
        self.result_received.set()

    async def cancel(self, run_id: str) -> CoreCancelOutcomeV01:
        self.cancels.append(run_id)
        return self.cancel_outcome


def _facts(*, user_id: int = 300, message_id: int = 400) -> DiscordCoreFacts:
    return DiscordCoreFacts(
        user_id=user_id,
        guild_id=100,
        channel_id=200,
        message_id=message_id,
        request_id=f"request-{message_id}",
        route_mode="conversation",
        visibility="guild_channel",
    )


def _request(
    *,
    user_id: int = 300,
    message_id: int = 400,
    idempotency_key: str = "discord:message_create:400",
    local_payload: object | None = None,
    authorization_check: object | None = None,
    fresh_authorization_check: object | None = None,
    capability_authorization_check: object | None = None,
) -> RunInput:
    facts = _facts(user_id=user_id, message_id=message_id)
    return RunInput(
        input_text="Core v0.1へ送る本文",
        idempotency_key=idempotency_key,
        conversation_key=f"guild:100:channel:200:user:{user_id}",
        metadata={"surface": "discord"},
        extensions={
            CORE_FACTS_EXTENSION: facts,
            CORE_V01_OPTIONS_EXTENSION: CoreRunOptionsV01(
                preferred_model="gpt-5.6-terra",
                history_override=(
                    {"role": "user", "content": "前の質問"},
                    {"role": "assistant", "content": "前の回答"},
                ),
            ),
        },
        local_payload=local_payload,
        authorization_check=authorization_check,  # type: ignore[arg-type]
        fresh_authorization_check=fresh_authorization_check,  # type: ignore[arg-type]
        capability_authorization_check=capability_authorization_check,  # type: ignore[arg-type]
    )


async def _events(gateway: YonerAIInternalRunGatewayV01, run_id: str) -> list[RunEvent]:
    return [event async for event in gateway.events(run_id)]


def test_v01_projection_is_exact_and_does_not_relabel_old_custom_schema() -> None:
    projected = project_core_message_v01(_request())
    mapping = projected.to_mapping()

    assert tuple(mapping) == (
        "content",
        "conversation_id",
        "user_identity",
        "attachments",
        "idempotency_key",
        "preferred_model",
        "history_override",
    )
    assert mapping["conversation_id"] == "guild:100:channel:200:user:300"
    assert mapping["user_identity"] == {"provider": "discord", "id": "300"}
    assert mapping["idempotency_key"] == "discord:message_create:400"
    assert mapping["preferred_model"] == "gpt-5.6-terra"
    assert mapping["history_override"] == [
        {"role": "user", "content": "前の質問"},
        {"role": "assistant", "content": "前の回答"},
    ]
    for legacy in ("schema", "source", "context_binding", "client_context", "request_meta", "route_hint"):
        assert legacy not in mapping


def test_projection_rejects_identity_conversation_mismatch_and_unknown_extension() -> None:
    request = _request()
    with pytest.raises(CoreContractError, match="conversation_key"):
        project_core_message_v01(
            RunInput(
                input_text=request.input_text,
                idempotency_key=request.idempotency_key,
                conversation_key="guild:100:channel:200:user:999",
                metadata=request.metadata,
                extensions=request.extensions,
            )
        )
    with pytest.raises(CoreContractError, match="extensions"):
        project_core_message_v01(
            RunInput(
                input_text=request.input_text,
                idempotency_key=request.idempotency_key,
                conversation_key=request.conversation_key,
                metadata=request.metadata,
                extensions={**request.extensions, "route_hint": {"mode": "arbitrary"}},
            )
        )


@pytest.mark.asyncio
async def test_gateway_sanitizes_local_payload_runs_fresh_auth_and_uses_user_scoped_idempotency() -> None:
    fresh_calls = 0
    sync_calls = 0

    def authorized() -> bool:
        nonlocal sync_calls
        sync_calls += 1
        return True

    async def freshly_authorized() -> bool:
        nonlocal fresh_calls
        fresh_calls += 1
        return True

    port = _FakePort(({"event": "final", "data": {"text": "done"}},))
    gateway = YonerAIInternalRunGatewayV01(port)
    first_request = _request(
        local_payload={"must": "stay local"},
        authorization_check=authorized,
        fresh_authorization_check=freshly_authorized,
    )
    first = await gateway.start(first_request)
    duplicate = await gateway.start(first_request)
    other_user = await gateway.start(
        _request(
            user_id=301,
            idempotency_key=first_request.idempotency_key,
        )
    )
    await asyncio.gather(_events(gateway, first.run_id), _events(gateway, other_user.run_id))

    assert duplicate.run_id == first.run_id
    assert duplicate.reused is True
    assert other_user.run_id != first.run_id
    assert len(port.starts) == 2
    assert [request.user_identity["id"] for request in port.starts] == ["300", "301"]
    assert all("local_payload" not in request.to_mapping() for request in port.starts)
    assert sync_calls == 2
    assert fresh_calls == 2


@pytest.mark.asyncio
async def test_tool_continuation_posts_exact_result_once_and_dedupes_action_event() -> None:
    fresh_checks = 0

    async def fresh_authorization() -> bool:
        nonlocal fresh_checks
        fresh_checks += 1
        return True

    port = _FakePort(
        (
            {"event": "future.minor-event", "data": {"opaque": True}},
            {
                "event": "tool_start",
                "data": {
                    "tool": "discord.search.read.v1",
                    "tool_call_id": "tool-call-1",
                    "arguments": {"query": "status"},
                },
            },
            {
                "event": "tool_start",
                "data": {
                    "tool": "discord.search.read.v1",
                    "tool_call_id": "tool-call-1",
                    "arguments": {"query": "status"},
                },
            },
            {"event": "delta", "data": {"text": "continuing"}},
            {"event": "final", "data": {"text": "done"}},
        ),
        wait_for_result=True,
    )
    gateway = YonerAIInternalRunGatewayV01(port)
    reference = await gateway.start(
        _request(
            fresh_authorization_check=fresh_authorization,
            capability_authorization_check=lambda capability: capability == "discord.search.read.v1",
        )
    )
    seen_action = asyncio.Event()
    streamed: list[RunEvent] = []

    async def consume() -> None:
        async for event in gateway.events(reference.run_id):
            streamed.append(event)
            if event.kind == "action_required":
                seen_action.set()

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(seen_action.wait(), timeout=1)
    result = CapabilityResult(
        result_id="tool-call-1",
        capability="discord.search.read.v1",
        output={"matches": 3},
    )
    await gateway.submit_result(reference.run_id, result)
    await gateway.submit_result(reference.run_id, result)
    with pytest.raises(IdempotencyConflictError):
        await gateway.submit_result(
            reference.run_id,
            CapabilityResult(
                result_id="tool-call-1",
                capability="discord.search.read.v1",
                output={"matches": 4},
            ),
        )
    await asyncio.wait_for(consumer, timeout=1)

    assert len(port.results) == 1
    run_id, wire = port.results[0]
    assert run_id == "remote-1"
    assert wire.to_mapping() == {
        "tool": "discord.search.read.v1",
        "result": {
            "output": {"matches": 3},
            "is_error": False,
            "artifacts": [],
        },
        "tool_call_id": "tool-call-1",
    }
    assert [event.kind for event in streamed] == [
        "action_required",
        "tool_result",
        "text_delta",
        "final",
    ]
    assert fresh_checks == 4


@pytest.mark.asyncio
async def test_tool_start_rechecks_fresh_authorization_before_action_is_exposed() -> None:
    decisions = iter((True, False))
    port = _FakePort(
        (
            {
                "event": "tool_start",
                "data": {"tool": "discord.read.v1", "tool_call_id": "call-1"},
            },
            {"event": "final", "data": {"text": "late"}},
        )
    )
    gateway = YonerAIInternalRunGatewayV01(port)
    reference = await gateway.start(
        _request(
            fresh_authorization_check=lambda: next(decisions),
            capability_authorization_check=lambda capability: capability == "discord.read.v1",
        )
    )

    events = await _events(gateway, reference.run_id)

    assert [event.kind for event in events] == ["error"]
    assert port.results == []


@pytest.mark.asyncio
async def test_tool_result_secret_is_rejected_before_result_port() -> None:
    port = _FakePort(
        (
            {
                "event": "tool_start",
                "data": {"tool": "discord.read.v1", "tool_call_id": "call-1"},
            },
            {"event": "final", "data": {"text": "done"}},
        ),
        wait_for_result=True,
    )
    gateway = YonerAIInternalRunGatewayV01(port)
    reference = await gateway.start(_request())
    seen_action = asyncio.Event()

    async def consume() -> None:
        async for event in gateway.events(reference.run_id):
            if event.kind == "action_required":
                seen_action.set()

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(seen_action.wait(), timeout=1)
    with pytest.raises(CoreV01AdapterError, match="secret-like"):
        await gateway.submit_result(
            reference.run_id,
            CapabilityResult(
                result_id="call-1",
                capability="discord.read.v1",
                output={"token": "".join(("sk", "-proj-", "0123456789abcdef"))},
            ),
        )
    with pytest.raises(CoreV01AdapterError, match="secret-like"):
        await gateway.submit_result(
            reference.run_id,
            CapabilityResult(
                result_id="call-1",
                capability="discord.read.v1",
                output={"nested": {"_".join(("api", "key")): "abcdefghijklmnop"}},
            ),
        )
    assert port.results == []
    port.result_received.set()
    await consumer


@pytest.mark.asyncio
async def test_duplicate_tool_call_id_with_changed_arguments_fails_closed() -> None:
    port = _FakePort(
        (
            {
                "event": "tool_start",
                "data": {
                    "tool": "discord.read.v1",
                    "tool_call_id": "call-1",
                    "arguments": {"query": "first"},
                },
            },
            {
                "event": "tool_start",
                "data": {
                    "tool": "discord.read.v1",
                    "tool_call_id": "call-1",
                    "arguments": {"query": "changed"},
                },
            },
            {"event": "final", "data": {"text": "late"}},
        )
    )
    gateway = YonerAIInternalRunGatewayV01(port)
    reference = await gateway.start(_request())

    events = await _events(gateway, reference.run_id)

    assert [event.kind for event in events] == ["action_required", "error"]
    assert port.results == []


@pytest.mark.asyncio
async def test_foreign_scope_final_artifact_is_rejected() -> None:
    port = _FakePort(
        (
            {
                "event": "final",
                "data": {
                    "text": "must not be delivered",
                    "artifacts": [
                        {
                            "artifact_id": "core-artifact-foreign",
                            "attachment_id": "core-attachment-foreign",
                            "kind": "file",
                            "media_type": "text/plain",
                            "size_bytes": 4,
                            "sha256": "0" * 64,
                            "owner_scope": {
                                "provider": "discord",
                                "subject_id": "999",
                                "conversation_id": "guild:100:channel:200:user:999",
                            },
                            "backend": "yonerai-files-v0.1",
                            "retention": "conversation",
                            "provenance": "tool-output",
                        }
                    ],
                },
            },
        )
    )
    gateway = YonerAIInternalRunGatewayV01(port)
    reference = await gateway.start(_request())

    events = await _events(gateway, reference.run_id)

    assert [event.kind for event in events] == ["error"]
    assert "must not be delivered" not in repr(events)


@pytest.mark.asyncio
async def test_unrequested_or_wrong_tool_result_is_rejected_before_port() -> None:
    port = _FakePort(
        (
            {
                "event": "tool_start",
                "data": {"tool": "discord.read.v1", "tool_call_id": "call-1"},
            },
            {"event": "final", "data": {"text": "done"}},
        ),
        wait_for_result=True,
    )
    gateway = YonerAIInternalRunGatewayV01(port)
    reference = await gateway.start(_request())
    seen_action = asyncio.Event()

    async def consume() -> None:
        async for event in gateway.events(reference.run_id):
            if event.kind == "action_required":
                seen_action.set()

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(seen_action.wait(), timeout=1)
    with pytest.raises(CoreV01AdapterError, match="requested tool"):
        await gateway.submit_result(
            reference.run_id,
            CapabilityResult("call-1", "discord.write.v1", output="no"),
        )
    with pytest.raises(CoreV01AdapterError, match="not requested"):
        await gateway.submit_result(
            reference.run_id,
            CapabilityResult("unknown-call", "discord.read.v1", output="no"),
        )
    assert port.results == []
    port.result_received.set()
    await consumer


@pytest.mark.asyncio
async def test_error_detail_is_sanitized_and_only_first_terminal_is_observed() -> None:
    port = _FakePort(
        (
            {
                "event": "error",
                "data": {
                    "code": "provider_failed",
                    "error_type": "PrivateProviderError",
                },
            },
            {"event": "final", "data": {"text": "late private detail"}},
        )
    )
    gateway = YonerAIInternalRunGatewayV01(port)
    reference = await gateway.start(_request())
    events = await _events(gateway, reference.run_id)

    assert len(events) == 1
    assert events[0].kind == "error"
    assert events[0].text == "execution failed"
    assert events[0].payload == {
        "code": "provider_failed",
        "error_type": "PrivateProviderError",
    }
    assert "late private detail" not in repr(events)


@pytest.mark.asyncio
async def test_cancel_without_v01_endpoint_reports_local_detach_not_remote_success() -> None:
    release = asyncio.Event()

    class WaitingPort(_FakePort):
        def events(self, run_id: str) -> AsyncIterator[Mapping[str, object]]:
            async def stream() -> AsyncIterator[Mapping[str, object]]:
                await release.wait()
                yield {"event": "final", "data": {"text": "late"}}

            return stream()

    port = WaitingPort(())
    gateway = YonerAIInternalRunGatewayV01(port)
    reference = await gateway.start(_request())
    await asyncio.wait_for(port.remote_started.wait(), timeout=1)

    first = await gateway.cancel(reference.run_id)
    second = await gateway.cancel(reference.run_id)
    events = await _events(gateway, reference.run_id)

    assert first == second
    assert first.disposition.value == "local_detached"
    assert first.remote_confirmed is False
    assert first.local_detached is True
    assert port.cancels == ["remote-1"]
    assert [(event.kind, event.payload["code"]) for event in events] == [("error", "cancelled")]


@pytest.mark.asyncio
async def test_authorization_failure_prevents_remote_start() -> None:
    port = _FakePort(({"event": "final", "data": {}},))
    gateway = YonerAIInternalRunGatewayV01(port)
    with pytest.raises(CoreV01AuthorizationError):
        await gateway.start(_request(authorization_check=lambda: False))
    assert port.starts == []


@pytest.mark.asyncio
async def test_terminal_cache_pruning_does_not_break_duplicate_replay() -> None:
    port = _FakePort(({"event": "final", "data": {"text": "done"}},))
    gateway = YonerAIInternalRunGatewayV01(port, max_runs_per_user=1)
    request = _request()
    first = await gateway.start(request)
    await _events(gateway, first.run_id)

    duplicate = await gateway.start(request)

    assert duplicate.run_id == first.run_id
    assert duplicate.reused is True
    assert await _events(gateway, duplicate.run_id)
    assert len(port.starts) == 1
