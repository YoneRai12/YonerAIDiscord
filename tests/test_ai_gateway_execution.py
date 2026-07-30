from __future__ import annotations

import asyncio
import hashlib
import logging

import pytest

from yonerai_discord.execution_gateway.core_contract import (
    CORE_FACTS_EXTENSION,
    DiscordCoreFacts,
    discord_core_conversation_id,
)
from yonerai_discord.execution_gateway.core_files import (
    CoreArtifactOwnerScopeV01,
    CoreArtifactRefV01,
    artifact_reference_from_core_v01,
)
from yonerai_discord.execution_gateway.models import (
    ArtifactReference,
    RunEvent,
    RunInput,
    RunReference,
)
from yonerai_discord.modules.ai.gateway_execution import (
    DuplicateDiscordRun,
    build_discord_core_facts,
    execute_ai_run,
)
from yonerai_discord.modules.ai.models import AIRequest
from yonerai_discord.modules.ai.service import AIUnavailableError, PrivacyBoundaryError


class ScriptedGateway:
    def __init__(self, events: tuple[RunEvent, ...], *, reused: bool = False) -> None:
        self.script = events
        self.reused = reused
        self.started: list[RunInput] = []
        self.cancelled: list[str] = []

    async def start(self, request: RunInput) -> RunReference:
        self.started.append(request)
        return RunReference("run-1", request.idempotency_key, reused=self.reused)

    async def events(self, run_id: str):
        assert run_id == "run-1"
        for event in self.script:
            yield event

    async def submit_result(self, run_id: str, result: object) -> None:
        del run_id, result

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


def test_discord_core_facts_bind_dm_and_thread_deterministically() -> None:
    dm = build_discord_core_facts(
        user_id=2,
        guild_id=None,
        channel_id=3,
        message_id=40,
        request_id="discord-message:40",
        route_mode="general",
        trigger="dm",
    )
    thread = build_discord_core_facts(
        user_id=2,
        guild_id=1,
        channel_id=3,
        thread_id=4,
        message_id=40,
        reply_to_message_id=39,
        request_id="discord-message:40",
        route_mode="general",
        trigger="reply",
    )

    assert dm.visibility == "dm"
    assert dm.guild_id is None
    assert thread.visibility == "guild_thread"
    assert thread.channel_id == 3
    assert thread.thread_id == 4
    assert thread.reply_to_message_id == 39


@pytest.mark.asyncio
async def test_execute_ai_run_can_attach_code_owned_discord_core_facts() -> None:
    gateway = ScriptedGateway(
        (
            RunEvent(
                kind="final",
                text="完了",
                payload={"model": "balanced", "provider": "core"},
            ),
        )
    )
    facts = DiscordCoreFacts(
        user_id=2,
        guild_id=1,
        channel_id=3,
        message_id=40,
        request_id="discord-message:40",
        route_mode="standard",
    )

    await execute_ai_run(
        gateway,
        AIRequest(prompt="調べて", guild_id=1, channel_id=3, user_id=2),
        idempotency_key="discord-message:40",
        conversation_key="guild:1:channel:3:user:2",
        discord_core_facts=facts,
    )

    assert gateway.started[0].extensions == {CORE_FACTS_EXTENSION: facts}


@pytest.mark.asyncio
async def test_execute_ai_run_ignores_unknown_event_and_renders_safe_artifacts() -> None:
    gateway = ScriptedGateway(
        (
            RunEvent(kind="future_extension", payload={"opaque": True}),
            RunEvent(
                kind="artifact",
                artifact=ArtifactReference(
                    artifact_id="private",
                    kind="file",
                    name="内部[資料]",
                    uri="file:///private/report.md",
                ),
            ),
            RunEvent(
                kind="artifact",
                artifact=ArtifactReference(
                    artifact_id="public",
                    kind="file",
                    name="公開資料",
                    uri="https://example.com/report.md",
                ),
            ),
            RunEvent(
                kind="artifact",
                artifact=ArtifactReference(
                    artifact_id="credentialed",
                    kind="file",
                    name="期限付き資料",
                    uri="https://example.com/private?token=abcdefghijklmnop",
                ),
            ),
            RunEvent(
                kind="final",
                text="完了しました。",
                payload={
                    "model": "logical-balanced",
                    "provider": "local-test",
                    "sources": ({"title": "根拠", "url": "https://example.com/source"},),
                },
            ),
        )
    )

    reply = await execute_ai_run(
        gateway,
        AIRequest(prompt="調べて", guild_id=1, user_id=2),
        idempotency_key="discord-message:40",
        conversation_key="session:abc",
    )

    assert reply.model == "logical-balanced"
    assert reply.provider == "local-test"
    assert "内部\\[資料\\]" in reply.text
    assert "file:///" not in reply.text
    assert "[公開資料](https://example.com/report.md)" in reply.text
    assert "期限付き資料" in reply.text
    assert "token=" not in reply.text
    assert reply.sources[0].url == "https://example.com/source"
    assert gateway.started[0].conversation_key == "session:abc"
    assert gateway.started[0].local_payload.prompt == "調べて"


@pytest.mark.asyncio
async def test_execute_ai_run_rejects_core_artifact_until_reverse_delivery_is_connected() -> None:
    gateway = ScriptedGateway(
        (
            RunEvent(
                kind="final",
                text="生成しました。",
                payload={
                    "model": "core-selected",
                    "provider": "yonerai-internal-run-v0.1",
                    "artifacts": (
                        ArtifactReference(
                            artifact_id="opaque-core-ref",
                            kind="image",
                        ),
                    ),
                },
            ),
        )
    )

    with pytest.raises(AIUnavailableError, match="artifact delivery"):
        await execute_ai_run(
            gateway,
            AIRequest(prompt="生成して", guild_id=1, user_id=2),
            idempotency_key="discord-message:core-artifact",
            conversation_key="session:core-artifact",
        )

    assert gateway.cancelled == ["run-1"]


@pytest.mark.asyncio
async def test_execute_ai_run_returns_only_scope_bound_core_refs_when_delivery_is_explicitly_connected() -> None:
    facts = DiscordCoreFacts(
        user_id=2,
        guild_id=1,
        channel_id=3,
        message_id=40,
        request_id="discord-message:core-artifact",
        route_mode="standard",
    )
    content = b"\x89PNG\r\n\x1a\ncanonical-placeholder"
    ref = CoreArtifactRefV01(
        artifact_id="core-artifact-1",
        attachment_id="core-attachment-1",
        kind="image",
        media_type="image/png",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        owner_scope=CoreArtifactOwnerScopeV01(
            provider="discord",
            subject_id=str(facts.user_id),
            conversation_id=discord_core_conversation_id(facts),
        ),
        backend="yonerai-files",
        retention="session",
        provenance="core-result",
    )
    artifact = artifact_reference_from_core_v01(ref)
    gateway = ScriptedGateway(
        (
            RunEvent(
                kind="artifact",
                artifact=artifact,
            ),
            RunEvent(
                kind="final",
                text="",
                payload={
                    "model": "core-selected",
                    "provider": "yonerai-internal-run-v0.1",
                    # 同じrefをterminal payloadでも返しても一度だけ配送する。
                    "artifacts": (artifact,),
                },
            ),
        )
    )

    reply = await execute_ai_run(
        gateway,
        AIRequest(prompt="生成して", guild_id=1, channel_id=3, user_id=2),
        idempotency_key=facts.request_id,
        conversation_key=discord_core_conversation_id(facts),
        discord_core_facts=facts,
        accept_core_artifact_references=True,
    )

    assert reply.text == "成果物を生成しました。"
    assert reply.artifact_references == (artifact,)
    assert "core-artifact" not in reply.text
    assert "core-artifact" not in repr(reply)
    assert gateway.cancelled == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "text", "model", "sources"),
    (
        ("unexpected-provider", "生成しました。", "core-selected", ()),
        ("yonerai-internal-run-v0.1", "core-artifact-1 を生成しました。", "core-selected", ()),
        ("yonerai-internal-run-v0.1", "CORE-ATTACHMENT-1 を生成しました。", "core-selected", ()),
        ("yonerai-internal-run-v0.1", "__sha256__", "core-selected", ()),
        ("yonerai-internal-run-v0.1", "生成しました。", "core-artifact-1", ()),
        (
            "yonerai-internal-run-v0.1",
            "生成しました。",
            "core-selected",
            ({"title": "core-attachment-1", "url": "https://example.com/source"},),
        ),
        (
            "yonerai-internal-run-v0.1",
            "生成しました。",
            "core-selected",
            ({"title": "source", "url": "https://example.com/core-artifact-1"},),
        ),
    ),
)
async def test_execute_ai_run_rejects_core_artifact_provider_or_public_identity_bypass(
    provider: str,
    text: str,
    model: str,
    sources: tuple[dict[str, str], ...],
) -> None:
    facts = DiscordCoreFacts(
        user_id=2,
        guild_id=1,
        channel_id=3,
        message_id=40,
        request_id="discord-message:core-artifact-bypass",
        route_mode="standard",
    )
    content = b"\x89PNG\r\n\x1a\ncanonical-placeholder"
    content_sha256 = hashlib.sha256(content).hexdigest()
    if text == "__sha256__":
        text = f"{content_sha256.upper()} を生成しました。"
    artifact = artifact_reference_from_core_v01(
        CoreArtifactRefV01(
            artifact_id="core-artifact-1",
            attachment_id="core-attachment-1",
            kind="image",
            media_type="image/png",
            size_bytes=len(content),
            sha256=content_sha256,
            owner_scope=CoreArtifactOwnerScopeV01(
                provider="discord",
                subject_id=str(facts.user_id),
                conversation_id=discord_core_conversation_id(facts),
            ),
            backend="yonerai-files",
            retention="session",
            provenance="core-result",
        )
    )
    gateway = ScriptedGateway(
        (
            RunEvent(
                kind="final",
                text=text,
                payload={
                    "model": model,
                    "provider": provider,
                    "artifacts": (artifact,),
                    "sources": sources,
                },
            ),
        )
    )

    with pytest.raises(AIUnavailableError, match="artifact delivery"):
        await execute_ai_run(
            gateway,
            AIRequest(prompt="生成して", guild_id=1, channel_id=3, user_id=2),
            idempotency_key=facts.request_id,
            conversation_key=discord_core_conversation_id(facts),
            discord_core_facts=facts,
            accept_core_artifact_references=True,
        )
    assert gateway.cancelled == ["run-1"]


@pytest.mark.asyncio
async def test_execute_ai_run_rejects_foreign_or_unbound_core_refs_before_delivery() -> None:
    facts = DiscordCoreFacts(
        user_id=2,
        guild_id=1,
        channel_id=3,
        message_id=40,
        request_id="discord-message:core-artifact",
        route_mode="standard",
    )
    content = b"\x89PNG\r\n\x1a\ncanonical-placeholder"
    foreign = artifact_reference_from_core_v01(
        CoreArtifactRefV01(
            artifact_id="core-artifact-foreign",
            attachment_id="core-attachment-foreign",
            kind="image",
            media_type="image/png",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            owner_scope=CoreArtifactOwnerScopeV01(
                provider="discord",
                subject_id="999",
                conversation_id="guild:1:channel:3:user:999",
            ),
            backend="yonerai-files",
            retention="session",
            provenance="core-result",
        )
    )
    gateway = ScriptedGateway(
        (
            RunEvent(
                kind="final",
                text="生成しました。",
                payload={
                    "model": "core-selected",
                    "provider": "yonerai-internal-run-v0.1",
                    "artifacts": (foreign,),
                },
            ),
        )
    )

    with pytest.raises(AIUnavailableError, match="artifact delivery"):
        await execute_ai_run(
            gateway,
            AIRequest(prompt="生成して", guild_id=1, channel_id=3, user_id=2),
            idempotency_key=facts.request_id,
            conversation_key=discord_core_conversation_id(facts),
            discord_core_facts=facts,
            accept_core_artifact_references=True,
        )

    assert gateway.cancelled == ["run-1"]


@pytest.mark.asyncio
async def test_execute_ai_run_suppresses_reused_discord_event() -> None:
    gateway = ScriptedGateway((), reused=True)

    with pytest.raises(DuplicateDiscordRun):
        await execute_ai_run(
            gateway,
            AIRequest(prompt="一度だけ", guild_id=1, user_id=2),
            idempotency_key="discord-message:40",
            conversation_key="session:abc",
        )

    assert len(gateway.started) == 1


@pytest.mark.asyncio
async def test_execute_ai_run_preserves_privacy_boundary_failure_type() -> None:
    gateway = ScriptedGateway(
        (
            RunEvent(
                kind="error",
                text="execution failed",
                payload={"code": "execution_failed", "error_type": "PrivacyBoundaryError"},
            ),
        )
    )

    with pytest.raises(PrivacyBoundaryError):
        await execute_ai_run(
            gateway,
            AIRequest(prompt="送信", guild_id=1, user_id=2),
            idempotency_key="discord-message:40",
            conversation_key="session:abc",
        )


@pytest.mark.asyncio
async def test_execute_ai_run_cancels_gateway_when_discord_task_is_cancelled() -> None:
    started = asyncio.Event()

    class BlockingGateway(ScriptedGateway):
        async def events(self, run_id: str):
            assert run_id == "run-1"
            started.set()
            await asyncio.Event().wait()
            if False:
                yield RunEvent(kind="final")

    gateway = BlockingGateway(())
    task = asyncio.create_task(
        execute_ai_run(
            gateway,
            AIRequest(prompt="待機", guild_id=1, user_id=2),
            idempotency_key="discord-message:40",
            conversation_key="session:abc",
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert gateway.cancelled == ["run-1"]


@pytest.mark.asyncio
async def test_execute_ai_run_ignores_event_callback_error_without_logging_event_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secrets = (
        "secret-event-text",
        "secret-tool-name",
        "secret-tool-args",
        "secret-tool-output",
        "secret-reasoning",
        "https://private.example/artifact?token=secret-uri",
        "secret-artifact-name",
        "secret-run-id",
        "secret-callback-message",
    )
    gateway = ScriptedGateway(
        (
            RunEvent(
                kind="status",
                text=secrets[0],
                artifact=ArtifactReference(
                    artifact_id="private-progress",
                    kind="file",
                    name=secrets[6],
                    uri=secrets[5],
                ),
                payload={
                    "tool": secrets[1],
                    "args": secrets[2],
                    "output": secrets[3],
                    "reasoning": secrets[4],
                },
                extensions={"private": secrets[3]},
                run_id=secrets[7],
            ),
            RunEvent(
                kind="final",
                text="完了しました。",
                payload={"model": "logical-balanced", "provider": "local-test"},
            ),
        )
    )
    received_kinds: list[str] = []

    async def fail_on_status(event: RunEvent) -> None:
        received_kinds.append(event.kind)
        if event.kind == "status":
            raise RuntimeError(secrets[8])

    with caplog.at_level(logging.WARNING, logger="yonerai_discord.modules.ai.gateway_execution"):
        reply = await execute_ai_run(
            gateway,
            AIRequest(prompt="進捗付き", guild_id=1, user_id=2),
            idempotency_key="discord-message:callback-error",
            conversation_key="session:callback-error",
            on_event=fail_on_status,
        )

    assert reply.text == "完了しました。"
    assert received_kinds == ["status", "final"]
    assert gateway.cancelled == []
    records = [record for record in caplog.records if record.message == "ai_gateway_event_callback_failed"]
    assert len(records) == 1
    assert getattr(records[0], "error_type") == "RuntimeError"
    logged = caplog.text + repr(records[0].__dict__)
    assert all(secret not in logged for secret in secrets)


@pytest.mark.asyncio
async def test_execute_ai_run_event_callback_cancel_cancels_gateway_and_is_reraised() -> None:
    gateway = ScriptedGateway(
        (
            RunEvent(kind="status", payload={"opaque": True}),
            RunEvent(
                kind="final",
                text="到達してはいけない",
                payload={"model": "logical-balanced", "provider": "local-test"},
            ),
        )
    )
    received_kinds: list[str] = []

    async def cancel_on_event(event: RunEvent) -> None:
        received_kinds.append(event.kind)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await execute_ai_run(
            gateway,
            AIRequest(prompt="キャンセル", guild_id=1, user_id=2),
            idempotency_key="discord-message:callback-cancel",
            conversation_key="session:callback-cancel",
            on_event=cancel_on_event,
        )

    assert received_kinds == ["status"]
    assert gateway.cancelled == ["run-1"]


@pytest.mark.asyncio
async def test_execute_ai_run_cancels_when_surface_start_callback_fails() -> None:
    gateway = ScriptedGateway(())

    async def fail_to_start_surface() -> None:
        raise RuntimeError("renderer unavailable")

    with pytest.raises(RuntimeError, match="renderer unavailable"):
        await execute_ai_run(
            gateway,
            AIRequest(prompt="開始", guild_id=1, user_id=2),
            idempotency_key="discord-message:40",
            conversation_key="session:abc",
            on_started=fail_to_start_surface,
        )

    assert gateway.cancelled == ["run-1"]
