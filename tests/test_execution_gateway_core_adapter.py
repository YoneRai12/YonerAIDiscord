from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace

import pytest

from yonerai_discord.execution_gateway.core_adapter import (
    CoreAdapterError,
    CoreExecutionGateway,
)
from yonerai_discord.execution_gateway.core_contract import (
    CORE_FACTS_EXTENSION,
    CORE_REQUEST_SCHEMA,
    CoreContractError,
    CoreRunRequest,
    DiscordCoreFacts,
    project_core_request,
)
from yonerai_discord.execution_gateway.models import (
    ArtifactReference,
    CapabilityResult,
    RunEvent,
    RunInput,
)


class _FakeCoreRunPort:
    def __init__(self, events: tuple[Mapping[str, object], ...]) -> None:
        self.events = events
        self.requests: list[CoreRunRequest] = []
        self.yield_count = 0

    def run(self, request: CoreRunRequest) -> AsyncIterator[Mapping[str, object]]:
        self.requests.append(request)

        async def stream() -> AsyncIterator[Mapping[str, object]]:
            for event in self.events:
                self.yield_count += 1
                yield event

        return stream()


def _facts(
    *,
    guild_id: int | None = 100,
    channel_id: int = 200,
    thread_id: int | None = None,
    visibility: str = "guild_channel",
) -> DiscordCoreFacts:
    return DiscordCoreFacts(
        user_id=300,
        guild_id=guild_id,
        channel_id=channel_id,
        thread_id=thread_id,
        message_id=400,
        reply_to_message_id=399,
        request_id="request-400",
        trace_id="trace-400",
        route_mode="conversation",
        trigger="mention" if guild_id is not None else "dm",
        visibility=visibility,
    )


def _request(
    *,
    facts: DiscordCoreFacts | None = None,
    artifacts: tuple[ArtifactReference, ...] = (),
    idempotency_key: str = "discord-message:400",
) -> RunInput:
    facts = facts or _facts()
    if facts.guild_id is None:
        conversation_key = f"dm:{facts.channel_id}:user:{facts.user_id}"
    elif facts.thread_id is not None:
        conversation_key = f"guild:{facts.guild_id}:channel:{facts.channel_id}:thread:{facts.thread_id}"
    else:
        conversation_key = f"guild:{facts.guild_id}:channel:{facts.channel_id}:user:{facts.user_id}"
    return RunInput(
        input_text="Coreへ渡す本文",
        idempotency_key=idempotency_key,
        conversation_key=conversation_key,
        artifacts=artifacts,
        metadata={"surface": "discord"},
        extensions={CORE_FACTS_EXTENSION: facts},
    )


async def _events(gateway: CoreExecutionGateway, run_id: str) -> list[RunEvent]:
    return [event async for event in gateway.events(run_id)]


def test_projection_contains_only_neutral_discord_facts_and_ref_only_attachments() -> None:
    facts = _facts(thread_id=250, visibility="guild_thread")
    request = _request(
        facts=facts,
        artifacts=(
            ArtifactReference(
                artifact_id="attachment-1",
                kind="image",
                name="input.png",
                media_type="image/png",
                size_bytes=123,
            ),
        ),
    )

    projected = project_core_request(request)
    mapping = projected.to_mapping()

    assert tuple(mapping) == (
        "schema",
        "source",
        "content",
        "idempotency_key",
        "context_binding",
        "user_identity",
        "client_context",
        "request_meta",
        "route_hint",
        "attachments",
    )
    assert mapping["schema"] == CORE_REQUEST_SCHEMA
    assert mapping["context_binding"] == {
        "provider": "discord",
        "kind": "thread",
        "external_id": "guild:100:channel:200:thread:250",
    }
    assert mapping["user_identity"] == {"provider": "discord", "id": "300"}
    assert mapping["route_hint"] == {"mode": "conversation"}
    assert mapping["attachments"] == [{"type": "image_ref", "attachment_id": "attachment-1"}]
    serialized = repr(mapping)
    for forbidden in ("route_score", "tools", "admin_verified", "memory", "input.png"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("facts", "expected_kind", "expected_external_id"),
    (
        (
            _facts(guild_id=None, channel_id=201, visibility="dm"),
            "dm",
            "dm:201:user:300",
        ),
        (
            _facts(channel_id=202, thread_id=203, visibility="guild_thread"),
            "thread",
            "guild:100:channel:202:thread:203",
        ),
    ),
)
def test_dm_and_thread_context_bindings_are_deterministic(
    facts: DiscordCoreFacts,
    expected_kind: str,
    expected_external_id: str,
) -> None:
    first = project_core_request(_request(facts=facts)).to_mapping()
    second = project_core_request(_request(facts=facts)).to_mapping()

    assert first == second
    assert first["context_binding"] == {
        "provider": "discord",
        "kind": expected_kind,
        "external_id": expected_external_id,
    }


@pytest.mark.parametrize(
    "mutate",
    (
        lambda request: replace(request, local_payload=b"raw attachment bytes"),
        lambda request: replace(
            request,
            extensions={**request.extensions, "tools": ("web_search",)},
        ),
        lambda request: replace(
            request,
            artifacts=(
                ArtifactReference(
                    artifact_id="attachment-uri",
                    kind="file",
                    uri="https://example.invalid/file",
                ),
            ),
        ),
        lambda request: replace(
            request,
            artifacts=(
                ArtifactReference(
                    artifact_id="attachment-bytes",
                    kind="file",
                    metadata={"raw": b"secret bytes"},
                ),
            ),
        ),
    ),
)
@pytest.mark.asyncio
async def test_forbidden_fields_and_raw_bytes_fail_before_core_delegation(
    mutate: Callable[[RunInput], RunInput],
) -> None:
    port = _FakeCoreRunPort(({"kind": "final", "text": "unused"},))
    gateway = CoreExecutionGateway(port)
    request = mutate(_request())

    with pytest.raises(CoreContractError):
        await gateway.start(request)

    assert port.requests == []


@pytest.mark.asyncio
async def test_adapter_delegates_once_and_normalizes_both_event_shapes() -> None:
    port = _FakeCoreRunPort(
        (
            {"kind": "progress", "payload": {"step": 1}},
            {"event": "delta", "data": {"text": "途中"}},
            {
                "event": "future.event",
                "data": {"payload": {"ignored": b"raw future bytes"}},
                "future_field": "ignored",
            },
            {
                "event": "final",
                "data": {
                    "text": "完了",
                    "payload": {"provider": "fake-core"},
                },
            },
        )
    )
    gateway = CoreExecutionGateway(port)
    request = _request()

    first = await gateway.start(request)
    duplicate = await gateway.start(request)
    events = await asyncio.wait_for(_events(gateway, first.run_id), timeout=1)

    assert duplicate.run_id == first.run_id
    assert duplicate.reused is True
    assert len(port.requests) == 1
    assert [(event.kind, event.text) for event in events] == [
        ("status", None),
        ("text_delta", "途中"),
        ("final", "完了"),
    ]
    assert [event.sequence for event in events] == [1, 2, 3]
    assert {event.run_id for event in events} == {first.run_id}


@pytest.mark.asyncio
async def test_duplicate_terminal_is_ignored_after_first_terminal() -> None:
    port = _FakeCoreRunPort(
        (
            {"kind": "final", "text": "first"},
            {"event": "error", "data": {"text": "secret detail"}},
        )
    )
    gateway = CoreExecutionGateway(port)
    reference = await gateway.start(_request())

    events = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)
    replay = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)

    assert events == replay
    assert [(event.kind, event.text) for event in events] == [("final", "first")]
    assert port.yield_count == 1
    assert "secret detail" not in (events[0].text or "")


@pytest.mark.parametrize(
    "events",
    (
        ({"kind": "progress", "payload": {"step": 1}},),
        ({"kind": "final", "payload": {"raw": b"secret bytes"}},),
    ),
)
@pytest.mark.asyncio
async def test_missing_terminal_or_raw_response_bytes_fail_closed(
    events: tuple[Mapping[str, object], ...],
) -> None:
    gateway = CoreExecutionGateway(_FakeCoreRunPort(events))
    reference = await gateway.start(_request())

    streamed = await asyncio.wait_for(_events(gateway, reference.run_id), timeout=1)

    assert streamed[-1].kind == "error"
    assert streamed[-1].payload["code"] == "execution_failed"
    assert sum(event.terminal for event in streamed) == 1
    assert all("secret bytes" not in (event.text or "") for event in streamed)
    with pytest.raises(CoreAdapterError, match="not wired"):
        await gateway.submit_result(
            reference.run_id,
            CapabilityResult("result-1", "web_search"),
        )
