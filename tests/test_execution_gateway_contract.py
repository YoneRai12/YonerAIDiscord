from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from yonerai_discord.execution_gateway import (
    ArtifactReference,
    CapabilityResult,
    IdempotencyConflictError,
    LocalExecutionContext,
    LocalExecutionGateway,
    RunEvent,
    RunInput,
    RunTerminalError,
    UnknownRunError,
)


async def _events(gateway: LocalExecutionGateway, run_id: str) -> list[RunEvent]:
    return [event async for event in gateway.events(run_id)]


def _request(key: str = "discord-message:123", *, text: str = "こんにちは") -> RunInput:
    return RunInput(
        input_text=text,
        idempotency_key=key,
        conversation_key="discord:guild:1:channel:2:user:3",
        metadata={"surface": "mention"},
    )


@pytest.mark.asyncio
async def test_same_input_starts_once_and_returns_reused_reference() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def execute(_: RunInput) -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "完了"

    gateway = LocalExecutionGateway(execute)
    first = await gateway.start(_request())
    await asyncio.wait_for(started.wait(), timeout=1)
    second = await gateway.start(_request())

    assert first.run_id == second.run_id
    assert first.reused is False
    assert second.reused is True
    assert calls == 1

    release.set()
    events = await asyncio.wait_for(_events(gateway, first.run_id), timeout=1)
    assert [(event.kind, event.text) for event in events] == [("final", "完了")]


@pytest.mark.asyncio
async def test_concurrent_duplicate_start_is_atomic() -> None:
    release = asyncio.Event()
    calls = 0

    async def execute(_: RunInput) -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return "ok"

    gateway = LocalExecutionGateway(execute)
    first, second = await asyncio.gather(
        gateway.start(_request()),
        gateway.start(_request()),
    )

    assert first.run_id == second.run_id
    assert sorted((first.reused, second.reused)) == [False, True]
    await asyncio.sleep(0)
    assert calls == 1

    release.set()
    await asyncio.wait_for(_events(gateway, first.run_id), timeout=1)


@pytest.mark.asyncio
async def test_idempotency_key_reuse_with_different_neutral_input_is_rejected() -> None:
    async def execute(_: RunInput) -> str:
        return "ok"

    gateway = LocalExecutionGateway(execute)
    await gateway.start(_request(text="first"))

    with pytest.raises(IdempotencyConflictError):
        await gateway.start(_request(text="different"))


@pytest.mark.asyncio
async def test_unknown_event_is_preserved_and_does_not_fail_the_run() -> None:
    async def execute(_: RunInput, context: LocalExecutionContext) -> str:
        await context.emit(RunEvent(kind="future.progress.v2", payload={"opaque": {"step": 1}}))
        return "ok"

    gateway = LocalExecutionGateway(execute, with_context=True)
    reference = await gateway.start(_request())
    events = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)

    assert [event.kind for event in events] == ["future.progress.v2", "final"]
    assert events[0].known is False
    assert events[0].payload["opaque"] == {"step": 1}
    assert [event.sequence for event in events] == [1, 2]
    assert {event.run_id for event in events} == {reference.run_id}


@pytest.mark.asyncio
async def test_final_is_terminal_once_and_later_update_is_rejected() -> None:
    rejected = asyncio.Event()

    async def execute(_: RunInput, context: LocalExecutionContext) -> None:
        await context.emit(RunEvent(kind="final", text="first"))
        with pytest.raises(RunTerminalError):
            await context.emit(RunEvent(kind="text_delta", text="late"))
        rejected.set()

    gateway = LocalExecutionGateway(execute, with_context=True)
    reference = await gateway.start(_request())
    events = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)
    await asyncio.wait_for(rejected.wait(), timeout=1)

    assert [(event.kind, event.text) for event in events] == [("final", "first")]

    with pytest.raises(RunTerminalError):
        await gateway.submit_result(
            reference.run_id,
            CapabilityResult("late-result", "test.capability", output="late"),
        )


@pytest.mark.asyncio
async def test_executor_failure_emits_one_safe_terminal_error() -> None:
    async def execute(_: RunInput) -> None:
        raise RuntimeError("secret detail must not be copied")

    gateway = LocalExecutionGateway(execute)
    reference = await gateway.start(_request())
    events = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)

    assert len(events) == 1
    assert events[0].kind == "error"
    assert events[0].payload == {
        "code": "execution_failed",
        "error_type": "RuntimeError",
    }
    assert "secret detail" not in (events[0].text or "")


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_reaches_terminal_state() -> None:
    started = asyncio.Event()

    async def execute(_: RunInput) -> None:
        started.set()
        await asyncio.Event().wait()

    gateway = LocalExecutionGateway(execute)
    reference = await gateway.start(_request())
    await asyncio.wait_for(started.wait(), timeout=1)

    await gateway.cancel(reference.run_id)
    await gateway.cancel(reference.run_id)
    events = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)

    assert len(events) == 1
    assert events[0].kind == "error"
    assert events[0].payload["code"] == "cancelled"


@pytest.mark.asyncio
async def test_capability_result_is_streamed_once_and_continues_executor() -> None:
    action_emitted = asyncio.Event()
    result_received = asyncio.Event()
    release = asyncio.Event()

    async def execute(_: RunInput, context: LocalExecutionContext) -> str:
        await context.emit(
            RunEvent(
                kind="action_required",
                payload={"result_id": "tool-call-1", "capability": "search.web"},
            )
        )
        action_emitted.set()
        result = await context.next_result()
        assert result.output == {"answer": 42}
        result_received.set()
        await release.wait()
        return "continued"

    gateway = LocalExecutionGateway(execute, with_context=True)
    reference = await gateway.start(_request())
    await asyncio.wait_for(action_emitted.wait(), timeout=1)
    result = CapabilityResult(
        "tool-call-1",
        "search.web",
        output={"answer": 42},
    )
    await gateway.submit_result(reference.run_id, result)
    await gateway.submit_result(reference.run_id, result)
    await asyncio.wait_for(result_received.wait(), timeout=1)

    with pytest.raises(IdempotencyConflictError):
        await gateway.submit_result(
            reference.run_id,
            CapabilityResult("tool-call-1", "search.web", output={"answer": 43}),
        )

    release.set()
    events = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)
    assert [event.kind for event in events] == ["action_required", "tool_result", "final"]
    assert sum(event.kind == "tool_result" for event in events) == 1
    assert events[-1].text == "continued"


@pytest.mark.asyncio
async def test_artifact_reference_can_be_streamed_before_final() -> None:
    artifact = ArtifactReference(
        artifact_id="artifact-1",
        kind="image",
        uri="local-artifact://artifact-1",
        name="result.png",
        media_type="image/png",
        size_bytes=123,
    )

    async def execute(_: RunInput) -> tuple[RunEvent, RunEvent]:
        return (
            RunEvent(kind="artifact", artifact=artifact),
            RunEvent(kind="final", text="画像を生成しました"),
        )

    gateway = LocalExecutionGateway(execute)
    reference = await gateway.start(_request())
    events = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)

    assert [event.kind for event in events] == ["artifact", "final"]
    assert events[0].artifact is artifact


@pytest.mark.asyncio
async def test_events_are_replayable_without_reexecuting_the_run() -> None:
    calls = 0

    async def execute(_: RunInput) -> str:
        nonlocal calls
        calls += 1
        return "one"

    gateway = LocalExecutionGateway(execute)
    reference = await gateway.start(_request())
    first = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)
    second = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)

    assert first == second
    assert calls == 1


@pytest.mark.asyncio
async def test_terminal_run_releases_request_payload_and_keeps_only_fingerprint() -> None:
    secret_text = "private conversation body"
    local_payload = object()
    gateway = LocalExecutionGateway(lambda _: "ok")
    reference = await gateway.start(
        RunInput(
            input_text=secret_text,
            idempotency_key="discord-message:private",
            local_payload=local_payload,
        )
    )

    await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)
    await asyncio.sleep(0)

    state = gateway._runs[reference.run_id]  # noqa: SLF001 - privacy retention invariant
    assert state.request is None
    assert secret_text not in repr(state)
    duplicate = await gateway.start(
        RunInput(
            input_text=secret_text,
            idempotency_key="discord-message:private",
            local_payload=object(),
        )
    )
    assert duplicate.reused is True


@pytest.mark.asyncio
async def test_terminal_run_cache_is_bounded_and_prunes_oldest_completed_run() -> None:
    gateway = LocalExecutionGateway(lambda _: "ok", max_runs=2)
    first = await gateway.start(_request("run-1"))
    await _events(gateway, first.run_id)
    second = await gateway.start(_request("run-2"))
    await _events(gateway, second.run_id)
    third = await gateway.start(_request("run-3"))
    await _events(gateway, third.run_id)

    assert len(gateway._runs) == 2  # noqa: SLF001 - bounded retention invariant
    with pytest.raises(UnknownRunError):
        await anext(gateway.events(first.run_id))


@pytest.mark.asyncio
async def test_ai_service_wrapper_preserves_request_authorization_and_reply_metadata() -> None:
    request_payload = object()
    checks = 0

    @dataclass(frozen=True)
    class Reply:
        text: str
        model: str
        provider: str
        sources: tuple[object, ...] = ()

    class Service:
        async def ask(self, request: object, *, provider_call_allowed: object) -> Reply:
            nonlocal checks
            assert request is request_payload
            assert callable(provider_call_allowed)
            checks += 1
            assert provider_call_allowed() is True
            return Reply("service reply", "logical-model", "configured-provider")

    gateway = LocalExecutionGateway.from_ai_service(Service())
    request = RunInput(
        input_text="hello",
        idempotency_key="discord-message:ai-service",
        local_payload=request_payload,
        authorization_check=lambda: True,
    )
    reference = await gateway.start(request)
    events = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)

    assert checks == 1
    assert len(events) == 1
    assert events[0].kind == "final"
    assert events[0].text == "service reply"
    assert events[0].payload == {
        "model": "logical-model",
        "provider": "configured-provider",
    }


@pytest.mark.asyncio
async def test_ai_service_wrapper_keeps_legacy_one_argument_ask_compatible() -> None:
    request_payload = object()

    @dataclass(frozen=True)
    class Reply:
        text: str
        model: str
        provider: str

    class LegacyService:
        async def ask(self, request: object) -> Reply:
            assert request is request_payload
            return Reply("legacy reply", "legacy-model", "local-provider")

    gateway = LocalExecutionGateway.from_ai_service(LegacyService())
    reference = await gateway.start(
        RunInput(
            input_text="hello",
            idempotency_key="discord-message:legacy-service",
            local_payload=request_payload,
        )
    )
    events = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)

    assert len(events) == 1
    assert events[0].kind == "final"
    assert events[0].text == "legacy reply"


@pytest.mark.asyncio
async def test_unknown_run_is_rejected_by_all_mutating_operations() -> None:
    gateway = LocalExecutionGateway(lambda _: "ok")

    with pytest.raises(UnknownRunError):
        await gateway.cancel("missing")
    with pytest.raises(UnknownRunError):
        await gateway.submit_result(
            "missing",
            CapabilityResult("result-1", "test.capability"),
        )
    with pytest.raises(UnknownRunError):
        await anext(gateway.events("missing"))
