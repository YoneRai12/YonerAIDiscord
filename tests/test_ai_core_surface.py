from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import pytest

from yonerai_discord.execution_gateway import (
    CapabilityResult,
    RunEvent,
    RunInput,
    RunReference,
)
from yonerai_discord.execution_gateway.core_contract import (
    CORE_FACTS_EXTENSION,
    CORE_V01_OPTIONS_EXTENSION,
    CoreCancelOutcomeV01,
    CoreMessageRequestV01,
    CoreRunOptionsV01,
    CoreRunReferenceV01,
    CoreToolResultV01,
    DiscordCoreFacts,
    discord_core_conversation_id,
)
from yonerai_discord.execution_gateway.core_files import (
    CoreArtifactRefV01,
    CoreFileRegistrationV01,
)
from yonerai_discord.execution_gateway.core_v01 import YonerAIInternalRunGatewayV01
from yonerai_discord.modules.ai.core_surface import (
    DiscordCoreSurfaceGateway,
    with_discord_core_facts,
)
from yonerai_discord.modules.ai.gateway_execution import execute_ai_run
from yonerai_discord.modules.ai.models import (
    AIRequest,
    Attachment,
    AttachmentKind,
    DataBoundary,
    MessageRole,
    Turn,
)
from yonerai_discord.modules.ai.service import AIUnavailableError


class _Gateway:
    def __init__(self) -> None:
        self.requests: list[RunInput] = []

    async def start(self, request: RunInput) -> RunReference:
        self.requests.append(request)
        return RunReference("core-run-1", request.idempotency_key)

    async def events(self, run_id: str) -> AsyncIterator[RunEvent]:
        yield RunEvent(kind="final", text="ok", run_id=run_id)

    async def submit_result(self, run_id: str, result: CapabilityResult) -> None:
        del run_id, result

    async def cancel(self, run_id: str) -> None:
        del run_id


class _Files:
    def __init__(self) -> None:
        self.requests: list[CoreFileRegistrationV01] = []

    async def register(self, request: CoreFileRegistrationV01) -> CoreArtifactRefV01:
        self.requests.append(request)
        return CoreArtifactRefV01(
            artifact_id=f"core-artifact-{len(self.requests)}",
            attachment_id=f"core-attachment-{len(self.requests)}",
            kind=request.kind,
            media_type=request.media_type,
            size_bytes=request.size_bytes,
            sha256=request.sha256,
            owner_scope=request.owner_scope,
            backend="yonerai-files-v0.1",
            retention=request.retention,
            provenance=request.provenance,
        )


class _CorePort:
    def __init__(self) -> None:
        self.requests: list[CoreMessageRequestV01] = []

    async def start(self, request: CoreMessageRequestV01) -> CoreRunReferenceV01:
        self.requests.append(request)
        return CoreRunReferenceV01("remote-run-1")

    def events(self, run_id: str) -> AsyncIterator[Mapping[str, object]]:
        async def stream() -> AsyncIterator[Mapping[str, object]]:
            assert run_id == "remote-run-1"
            yield {"event": "future-event", "data": {"ignored": True}}
            yield {"event": "final", "data": {"text": "Coreから完了"}}

        return stream()

    async def submit_result(self, run_id: str, result: CoreToolResultV01) -> None:
        del run_id, result

    async def cancel(self, run_id: str) -> CoreCancelOutcomeV01:
        del run_id
        return CoreCancelOutcomeV01.unsupported()


def _facts() -> DiscordCoreFacts:
    return DiscordCoreFacts(
        user_id=111,
        guild_id=222,
        channel_id=333,
        message_id=444,
        request_id="discord-message:444",
        route_mode="standard",
    )


def _input(
    request: AIRequest,
    *,
    facts: DiscordCoreFacts | None = None,
    authorization_check=None,
    fresh_authorization_check=None,
) -> RunInput:
    return with_discord_core_facts(
        RunInput(
            input_text=request.prompt,
            idempotency_key="discord-message:444",
            conversation_key="guild:222:channel:333:user:111",
            metadata={"surface": "discord"},
            local_payload=request,
            authorization_check=authorization_check,
            fresh_authorization_check=fresh_authorization_check,
        ),
        facts or _facts(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("facts", "surface_key", "channel_id"),
    (
        (
            DiscordCoreFacts(
                user_id=111,
                guild_id=222,
                channel_id=333,
                message_id=444,
                request_id="discord-message:444",
                route_mode="standard",
                trigger="mention",
            ),
            "guild:222:channel:333:user:111",
            333,
        ),
        (
            DiscordCoreFacts(
                user_id=111,
                channel_id=333,
                message_id=445,
                request_id="discord-message:445",
                route_mode="standard",
                trigger="dm",
                visibility="dm",
            ),
            "dm:channel:333:user:111",
            333,
        ),
        (
            DiscordCoreFacts(
                user_id=111,
                guild_id=222,
                channel_id=333,
                thread_id=444,
                message_id=446,
                request_id="discord-message:446",
                route_mode="standard",
                trigger="mention",
                visibility="guild_thread",
            ),
            "guild:222:channel:444:user:111",
            444,
        ),
        (
            DiscordCoreFacts(
                user_id=111,
                guild_id=222,
                channel_id=333,
                thread_id=444,
                message_id=447,
                request_id="discord-interaction:447",
                route_mode="standard",
                trigger="slash",
                visibility="guild_thread",
            ),
            "guild:222:channel:444:user:111",
            444,
        ),
    ),
    ids=("guild-channel", "dm", "mention-thread", "slash-thread"),
)
async def test_core_surface_rebinds_observed_discord_facts_to_the_canonical_conversation_id(
    facts: DiscordCoreFacts,
    surface_key: str,
    channel_id: int,
) -> None:
    downstream = _Gateway()
    request = AIRequest(
        prompt="conversation binding",
        guild_id=facts.guild_id,
        channel_id=channel_id,
        user_id=facts.user_id,
        boundary=DataBoundary.REMOTE_OPT_IN,
    )
    run = with_discord_core_facts(
        RunInput(
            input_text=request.prompt,
            idempotency_key=facts.request_id,
            conversation_key=surface_key,
            metadata={"surface": "discord"},
            local_payload=request,
        ),
        facts,
    )

    await DiscordCoreSurfaceGateway(downstream).start(run)

    assert downstream.requests[0].conversation_key == discord_core_conversation_id(facts)


@pytest.mark.asyncio
async def test_core_surface_binds_files_to_the_same_canonical_thread_conversation() -> None:
    facts = DiscordCoreFacts(
        user_id=111,
        guild_id=222,
        channel_id=333,
        thread_id=444,
        message_id=445,
        request_id="discord-message:445",
        route_mode="standard",
        trigger="mention",
        visibility="guild_thread",
    )
    request = AIRequest(
        prompt="attachment binding",
        guild_id=222,
        channel_id=444,
        user_id=111,
        boundary=DataBoundary.REMOTE_OPT_IN,
        attachments=(
            Attachment(
                kind=AttachmentKind.FILE,
                data=b"safe attachment",
                mime_type="text/plain",
                filename="note.txt",
            ),
        ),
    )
    files = _Files()
    downstream = _Gateway()
    run = with_discord_core_facts(
        RunInput(
            input_text=request.prompt,
            idempotency_key=facts.request_id,
            conversation_key="guild:222:channel:444:user:111",
            metadata={"surface": "discord"},
            local_payload=request,
        ),
        facts,
    )

    await DiscordCoreSurfaceGateway(downstream, files=files).start(run)

    conversation_id = discord_core_conversation_id(facts)
    assert downstream.requests[0].conversation_key == conversation_id
    assert files.requests[0].owner_scope.conversation_id == conversation_id


@pytest.mark.asyncio
async def test_core_surface_runs_through_strict_v01_and_returns_neutral_reply() -> None:
    port = _CorePort()
    gateway = DiscordCoreSurfaceGateway(YonerAIInternalRunGatewayV01(port))
    facts = _facts()
    request = AIRequest(
        prompt="Coreへ接続",
        guild_id=222,
        channel_id=333,
        user_id=111,
        boundary=DataBoundary.REMOTE_OPT_IN,
        required_model_alias="ai.balanced",
    )

    reply = await execute_ai_run(
        gateway,
        request,
        idempotency_key="discord-message:444",
        conversation_key="guild:222:channel:333:user:111",
        authorization_check=lambda: True,
        fresh_authorization_check=lambda: True,
        discord_core_facts=facts,
    )

    assert reply.text == "Coreから完了"
    assert reply.model == "ai.balanced"
    assert reply.provider == "yonerai-internal-run-v0.1"
    assert len(port.requests) == 1
    assert tuple(port.requests[0].to_mapping()) == (
        "content",
        "conversation_id",
        "user_identity",
        "attachments",
        "idempotency_key",
        "preferred_model",
        "history_override",
    )


@pytest.mark.asyncio
async def test_core_surface_projects_history_and_model_without_serializing_callbacks() -> None:
    downstream = _Gateway()

    def callback() -> bool:
        return True

    request = AIRequest(
        prompt="続けて",
        guild_id=222,
        channel_id=333,
        user_id=111,
        boundary=DataBoundary.REMOTE_OPT_IN,
        required_model_alias="ai.balanced",
        history=(Turn(MessageRole.USER, "前の質問"), Turn(MessageRole.ASSISTANT, "前の回答")),
    )

    reference = await DiscordCoreSurfaceGateway(downstream).start(_input(request, authorization_check=callback))

    assert reference.run_id == "core-run-1"
    projected = downstream.requests[0]
    assert projected.local_payload is request
    assert projected.authorization_check is callback
    assert set(projected.extensions) == {CORE_FACTS_EXTENSION, CORE_V01_OPTIONS_EXTENSION}
    options = projected.extensions[CORE_V01_OPTIONS_EXTENSION]
    assert options == CoreRunOptionsV01(
        preferred_model="ai.balanced",
        history_override=(
            {"role": "user", "content": "前の質問"},
            {"role": "assistant", "content": "前の回答"},
        ),
    )


@pytest.mark.asyncio
async def test_core_surface_registers_current_discord_attachment_as_remote_ref() -> None:
    downstream = _Gateway()
    files = _Files()
    calls = 0

    async def fresh() -> bool:
        nonlocal calls
        calls += 1
        return True

    payload = b"\x89PNG\r\n\x1a\ncanonical"
    request = AIRequest(
        prompt="この画像を説明して",
        guild_id=222,
        channel_id=333,
        user_id=111,
        boundary=DataBoundary.REMOTE_OPT_IN,
        attachments=(
            Attachment(
                kind=AttachmentKind.IMAGE,
                data=payload,
                mime_type="image/png",
                filename="input.png",
            ),
        ),
    )

    await DiscordCoreSurfaceGateway(downstream, files=files).start(
        _input(
            request,
            authorization_check=lambda: True,
            fresh_authorization_check=fresh,
        )
    )

    assert calls == 3
    assert len(files.requests) == 1
    assert files.requests[0].content == payload
    assert files.requests[0].owner_scope.to_mapping() == {
        "provider": "discord",
        "subject_id": "111",
        "conversation_id": "guild:222:channel:333:user:111",
    }
    artifact = downstream.requests[0].artifacts[0]
    assert artifact.artifact_id == "core-artifact-1"
    assert artifact.artifact_id != files.requests[0].local_artifact_id
    assert artifact.uri is None


@pytest.mark.asyncio
async def test_core_surface_fails_closed_when_files_or_scope_is_unavailable() -> None:
    request = AIRequest(
        prompt="添付",
        guild_id=222,
        channel_id=333,
        user_id=111,
        boundary=DataBoundary.REMOTE_OPT_IN,
        attachments=(
            Attachment(
                kind=AttachmentKind.FILE,
                data=b"safe text",
                mime_type="text/plain",
                filename="note.txt",
            ),
        ),
    )
    downstream = _Gateway()

    with pytest.raises(AIUnavailableError, match="Files"):
        await DiscordCoreSurfaceGateway(downstream).start(_input(request))

    mismatched = DiscordCoreFacts(
        user_id=999,
        guild_id=222,
        channel_id=333,
        message_id=444,
        request_id="discord-message:444",
        route_mode="standard",
    )
    files = _Files()
    with pytest.raises(AIUnavailableError, match="scope"):
        await DiscordCoreSurfaceGateway(downstream, files=files).start(_input(request, facts=mismatched))

    assert downstream.requests == []
    assert files.requests == []


@pytest.mark.asyncio
async def test_core_surface_revocation_after_registration_prevents_remote_start() -> None:
    downstream = _Gateway()
    files = _Files()
    decisions = iter((True, False))
    request = AIRequest(
        prompt="添付",
        guild_id=222,
        channel_id=333,
        user_id=111,
        boundary=DataBoundary.REMOTE_OPT_IN,
        attachments=(
            Attachment(
                kind=AttachmentKind.FILE,
                data=b"safe text",
                mime_type="text/plain",
                filename="note.txt",
            ),
        ),
    )

    with pytest.raises(AIUnavailableError, match="authorization changed"):
        await DiscordCoreSurfaceGateway(downstream, files=files).start(
            _input(
                request,
                authorization_check=lambda: True,
                fresh_authorization_check=lambda: next(decisions),
            )
        )

    assert len(files.requests) == 1
    assert downstream.requests == []


@pytest.mark.asyncio
async def test_core_surface_rejects_local_only_payload_before_remote_start() -> None:
    downstream = _Gateway()
    request = AIRequest(
        prompt="local only",
        guild_id=222,
        channel_id=333,
        user_id=111,
    )

    with pytest.raises(AIUnavailableError, match="scope"):
        await DiscordCoreSurfaceGateway(downstream).start(_input(request))

    assert downstream.requests == []


def test_core_surface_rejects_occupied_extensions_and_hides_attachment_bytes() -> None:
    request = RunInput(
        input_text="safe",
        idempotency_key="safe-key",
        extensions={"other": "value"},
    )

    with pytest.raises(AIUnavailableError, match="occupied"):
        with_discord_core_facts(request, _facts())

    attachment = Attachment(
        kind=AttachmentKind.FILE,
        data=b"super-secret-body",
        mime_type="text/plain",
        filename="note.txt",
    )
    assert "super-secret-body" not in repr(attachment)
