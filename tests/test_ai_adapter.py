from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from PIL import Image

from yonerai_discord.ai_control import RiskLevel, TaskComplexity, TaskKind
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.execution_gateway.core_contract import (
    DiscordCoreFacts,
    discord_core_conversation_id,
)
from yonerai_discord.execution_gateway.core_files import (
    CoreArtifactOwnerScopeV01,
    CoreArtifactRefV01,
    CoreFileReadReceiptV01,
    CoreFileReadRequestV01,
    artifact_reference_from_core_v01,
)
from yonerai_discord.execution_gateway.models import RunEvent, RunReference
from yonerai_discord.modules.ai.adapter import AIGroup
from yonerai_discord.modules.ai.admission import AIAdmissionController
from yonerai_discord.modules.ai.bounded_tools import StaticCapabilityMetadata, StaticCapabilitySnapshot
from yonerai_discord.modules.ai.conversation import ConversationStore
from yonerai_discord.modules.ai.core_artifact_delivery import CoreArtifactDeliveryPreparer
from yonerai_discord.modules.ai.display_preferences import DisplayMode, DisplayPreferenceStore
from yonerai_discord.modules.ai.models import AIReply, AIRequest, AISource
from yonerai_discord.modules.ai.provider import _extract_web_sources
from yonerai_discord.modules.ai.remote_consent import RemoteConsentStore
from yonerai_discord.modules.ai.service import PrivacyBoundaryError
from yonerai_discord.modules.ai.state_repository import AIStateRepository
from yonerai_discord.modules.ai.task_progress import DiscordAITaskProgressRenderer, ProgressEditPolicy
from yonerai_discord.modules.ai.task_routing import UNKNOWN_OPERATION_REPLY
from yonerai_discord.modules.web_runtime.search import WebSearchSource
from yonerai_discord.capability_metadata_contract import capability_metadata_content_revision
from yonerai_discord.modules.media_pipeline.artifacts import canonicalize_image
from yonerai_discord.search_fabric.contracts import (
    SearchCorroborationState,
    SearchEvidenceV1,
    SearchFetchState,
    SearchIntent,
    SearchResultV1,
    SearchSourceClass,
    query_digest,
)
from yonerai_discord.search_fabric.orchestrator import (
    SearchOrchestratorOutcome,
    SearchVerificationState,
)
from yonerai_discord.search_fabric.receipts import SearchReceiptV1
from yonerai_discord.v0_contracts import MemoryVisibility, Scope
from yonerai_discord.v0_runtime.memory_repository import V0ExplicitMemoryRepository


class _Response:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)

    async def defer(self, **_kwargs: object) -> None:
        self.deferred = True

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))


class _Service:
    available = True
    provider_locality = True

    async def ask(self, _request: object) -> object:
        raise AssertionError("secret-like input must not reach the AI service")


class _Followup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.sent_messages: list[_FollowupMessage] = []
        self._next_id = 1_000
        self.guild: object | None = None
        self.channel: object | None = None

    async def send(self, content: str | None = None, **kwargs: object) -> object:
        self.messages.append((content or "", kwargs))
        message = _FollowupMessage(self._next_id, guild=self.guild, channel=self.channel)
        self._next_id += 1
        self.sent_messages.append(message)
        return message


class _FollowupMessage:
    def __init__(self, message_id: int, *, guild: object | None = None, channel: object | None = None) -> None:
        self.id = message_id
        self.guild = guild
        self.channel = channel
        self.edits: list[dict[str, object]] = []
        self.deleted = False

    async def edit(self, **kwargs: object) -> object:
        self.edits.append(kwargs)
        return self

    async def delete(self) -> None:
        self.deleted = True


class _FollowupChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self._messages: dict[int, _FollowupMessage] = {}

    def bind(self, message: _FollowupMessage) -> None:
        self._messages[message.id] = message

    def get_partial_message(self, message_id: int) -> object:
        original = self._messages[message_id]

        class _PartialMessage:
            id = original.id
            channel = self

            async def edit(self, **kwargs: object) -> object:
                return await original.edit(**kwargs)

            async def delete(self) -> None:
                await original.delete()

        return _PartialMessage()


class _DiscordAttachment:
    def __init__(self, payload: bytes, *, filename: str = "notes.txt", content_type: str = "text/plain") -> None:
        self.id = 9_001
        self.filename = filename
        self.content_type = content_type
        self.size = len(payload)
        self._payload = payload
        self.reads = 0

    async def read(self, *, use_cached: bool) -> bytes:
        assert use_cached is True
        self.reads += 1
        return self._payload


class _BlockingDiscordAttachment(_DiscordAttachment):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def read(self, *, use_cached: bool) -> bytes:
        self.started.set()
        await self.release.wait()
        return await super().read(use_cached=use_cached)


class _RecordingService:
    available = True
    provider_locality = True

    def __init__(self, *, text: str = "完了") -> None:
        self.requests: list[AIRequest] = []
        self.text = text

    async def ask(self, request: AIRequest, *, provider_call_allowed=None) -> AIReply:
        assert callable(provider_call_allowed)
        if provider_call_allowed() is not True:
            raise PrivacyBoundaryError("provider call no longer allowed")
        self.requests.append(request)
        return AIReply(text=self.text, model="gpt-5.6-sol", provider="fake")


class _RemoteRecordingService(_RecordingService):
    provider_locality = False


class _WebRecordingService(_RecordingService):
    async def ask(
        self,
        request: AIRequest,
        *,
        provider_call_allowed=None,
        tool_capability_allowed=None,
    ) -> AIReply:
        assert request.web_search is False
        assert request.uses_tools is False
        assert request.allowed_model_tools == ()
        assert request.max_tool_calls == 0
        assert tool_capability_allowed is None
        reply = await super().ask(request, provider_call_allowed=provider_call_allowed)
        return AIReply(
            text=reply.text,
            model=reply.model,
            provider=reply.provider,
            sources=(AISource(title="モデル生成の偽出典", url="https://model.invalid/source"),),
        )


class _SearchFabricGateway:
    def __init__(
        self,
        *,
        title: str = "公式資料",
        url: str = "https://example.com/docs",
        snippet: str = "Search Fabricが直接取得した証拠です。",
        verification_state: SearchVerificationState = SearchVerificationState.VERIFIED,
    ) -> None:
        self.title = title
        self.url = url
        self.snippet = snippet
        self.verification_state = verification_state
        self.calls: list[object] = []
        self.request_ids: list[str] = []

    async def probe(self) -> bool:
        return True

    async def search(
        self,
        query: str,
        *,
        request_id: str,
        intent: SearchIntent,
        language: str,
        high_stakes: bool,
        authorization_current: object,
    ) -> SearchOrchestratorOutcome:
        assert high_stakes is False
        assert callable(authorization_current)
        assert await authorization_current() is True
        self.calls.append(query)
        self.request_ids.append(request_id)
        result = SearchResultV1(
            request_id=request_id,
            query_digest=query_digest(query),
            intent=intent,
            language=language,
            evidence=(
                SearchEvidenceV1(
                    source=WebSearchSource(
                        title=self.title,
                        url=self.url,
                        snippet=self.snippet,
                        source_id="src_verified",
                    ),
                    source_class=SearchSourceClass.PRIMARY_OFFICIAL,
                    fetch_state=SearchFetchState.FETCHED,
                    published=None,
                    retrieved="2026-07-29T00:00:00Z",
                    content_hash="sha256:" + "a" * 64,
                    corroboration=SearchCorroborationState.INDEPENDENT,
                    verification_reasons=("official_domain", "direct_fetch"),
                ),
            ),
            backend_ids=("searxng.local",),
            engine_errors=(),
            candidate_count=1,
            cache_hits=0,
            latency_ms=1,
        )
        return SearchOrchestratorOutcome(
            result=result,
            receipt=SearchReceiptV1.from_result(result),
            verification_state=self.verification_state,
            evidence_text=(f"[src_verified] primary_official; direct_fetch; {self.title}; {self.snippet}"),
        )


class _UnknownLocalityService:
    available = True

    def __init__(self) -> None:
        self.requests: list[AIRequest] = []

    async def ask(self, request: AIRequest, *, provider_call_allowed=None) -> AIReply:
        self.requests.append(request)
        return AIReply(text="must not run", model="unknown", provider="unknown")


def _interaction(*, guild_id: int = 1, channel_id: int = 3, user_id: int = 2, interaction_id: int = 100) -> object:
    class PolicyGuard:
        def __init__(self) -> None:
            self.allowed = True
            self.attachment_allowed = True
            self.denied_capability_ids: set[str] = set()
            self.calls: list[dict[str, object]] = []
            self.registry = SimpleNamespace(
                capability=lambda capability_id: SimpleNamespace(capability_id=capability_id)
            )
            self.policy = SimpleNamespace(
                evaluate=lambda capability_id, actor: SimpleNamespace(
                    allowed=(
                        self.attachment_allowed
                        if capability_id == "cap-run-ai-attachment-understand"
                        else self.allowed and capability_id not in self.denied_capability_ids
                    ),
                    actor_level=actor.level,
                )
            )

        def currently_allowed(self, capability_id: str, **kwargs: object) -> bool:
            self.calls.append({"capability_id": capability_id, **kwargs})
            if capability_id in self.denied_capability_ids:
                return False
            return self.attachment_allowed if capability_id == "cap-run-ai-attachment-understand" else self.allowed

        async def evaluate_fresh_member(self, capability_id: str, *, guild: object, member: object) -> object:
            del guild
            attribute = (
                "fresh_attachment_allowed"
                if capability_id == "cap-run-ai-attachment-understand"
                else "fresh_ai_allowed"
            )
            allowed = getattr(
                member,
                attribute,
                self.attachment_allowed if attribute == "fresh_attachment_allowed" else self.allowed,
            )
            return SimpleNamespace(allowed=allowed, actor_level=RbacLevel.EVERYONE)

    permissions = SimpleNamespace(
        administrator=False,
        manage_guild=False,
        moderate_members=False,
        manage_messages=False,
        kick_members=False,
        ban_members=False,
    )
    user = SimpleNamespace(
        id=user_id,
        roles=(),
        guild_permissions=permissions,
        fresh_ai_allowed=True,
        fresh_attachment_allowed=True,
    )
    guild = SimpleNamespace(id=guild_id, owner_id=999)

    async def fetch_member(user_id: int) -> object:
        assert user_id == user.id
        return guild.fresh_member

    guild.fetch_member = fetch_member
    guild.fresh_member = user
    client = SimpleNamespace(
        is_closing=False,
        capability_guard=PolicyGuard(),
        settings=SimpleNamespace(
            bot_owner_ids=frozenset(),
            moderator_role_ids=frozenset(),
            trusted_role_ids=frozenset(),
            ai_attachments_enabled=False,
            ai_attachment_max_file_bytes=1024 * 1024,
            ai_attachment_max_total_bytes=2 * 1024 * 1024,
            ai_timeout_seconds=30,
        ),
    )

    async def is_owner(_member: object) -> bool:
        return False

    client.is_owner = is_owner
    client.capability_guard.bot = client
    client.capability_guard.settings = client.settings
    followup = _Followup()
    channel = _FollowupChannel(channel_id)
    channel.guild = guild
    channel.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True,
        read_message_history=True,
    )
    followup.guild = guild
    followup.channel = channel

    original_send = followup.send

    async def send_and_bind(content: str | None = None, **kwargs: object) -> object:
        message = await original_send(content, **kwargs)
        assert isinstance(message, _FollowupMessage)
        channel.bind(message)
        return message

    followup.send = send_and_bind  # type: ignore[method-assign]
    return SimpleNamespace(
        id=interaction_id,
        guild_id=guild_id,
        channel_id=channel_id,
        guild=guild,
        channel=channel,
        user=user,
        client=client,
        response=_Response(),
        followup=followup,
    )


def _core_png_artifact_for_interaction(interaction: object) -> tuple[bytes, object]:
    with Image.new("RGB", (2, 3), (10, 20, 30)) as image:
        data = canonicalize_image(image).data
    facts = DiscordCoreFacts(
        user_id=interaction.user.id,
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        message_id=interaction.id,
        request_id=f"discord-interaction:{interaction.id}",
        route_mode="general",
        trigger="slash",
        visibility="guild_channel",
    )
    scope = CoreArtifactOwnerScopeV01(
        provider="discord",
        subject_id=str(facts.user_id),
        conversation_id=discord_core_conversation_id(facts),
    )
    ref = CoreArtifactRefV01(
        artifact_id="core-secret-artifact",
        attachment_id="core-secret-attachment",
        kind="image",
        media_type="image/png",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        owner_scope=scope,
        backend="yonerai-files",
        retention="session",
        provenance="core-output",
    )
    return data, artifact_reference_from_core_v01(ref)


class _CoreArtifactGateway:
    def __init__(self, artifact: object) -> None:
        self.artifact = artifact
        self.starts: list[object] = []
        self.cancels: list[str] = []

    async def start(self, request: object) -> RunReference:
        self.starts.append(request)
        return RunReference("core-run", request.idempotency_key)

    async def events(self, run_id: str):
        assert run_id == "core-run"
        yield RunEvent(kind="artifact", artifact=self.artifact)
        yield RunEvent(
            kind="final",
            text="Core result",
            payload={"model": "core-model", "provider": "yonerai-internal-run-v0.1"},
        )

    async def submit_result(self, run_id: str, result: object) -> None:
        raise AssertionError((run_id, result))

    async def cancel(self, run_id: str) -> None:
        self.cancels.append(run_id)


class _CoreReadPort:
    def __init__(self, data: bytes, *, on_read=None) -> None:
        self.data = data
        self.on_read = on_read
        self.requests: list[CoreFileReadRequestV01] = []

    async def read_for_delivery(self, request: CoreFileReadRequestV01) -> CoreFileReadReceiptV01:
        self.requests.append(request)
        if self.on_read is not None:
            self.on_read()
        return CoreFileReadReceiptV01(
            delivery_id=request.delivery_id,
            ref=request.ref,
            owner_scope=request.owner_scope,
            content=self.data,
        )


class _HangingCoreReadPort(_CoreReadPort):
    async def read_for_delivery(self, request: CoreFileReadRequestV01) -> CoreFileReadReceiptV01:
        self.requests.append(request)
        await asyncio.Event().wait()
        raise AssertionError("hung read must be cancelled")


class _CoreRecordingRenderer:
    def __init__(self, *, before_fresh=None) -> None:
        self.contents: list[str] = []
        self.media: tuple[object, ...] = ()
        self.sends = 0
        self.before_fresh = before_fresh

    async def reply(self, _source: object, content: str, **kwargs: object) -> object:
        self.contents.append(content)
        self.media = kwargs["media_attachments"]  # type: ignore[assignment]
        if self.before_fresh is not None:
            self.before_fresh()
        fresh = kwargs["fresh_send_allowed"]  # type: ignore[index]
        if await fresh() is True:
            self.sends += 1
            primary_message = SimpleNamespace(id=9_999)
        else:
            primary_message = None
        return SimpleNamespace(primary_message=primary_message, full_text=content, reused_message=True)


class _BlockingAuthorizedService:
    available = True
    provider_locality = True

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[AIRequest] = []

    async def ask(
        self,
        request: AIRequest,
        *,
        provider_call_allowed=None,
        fresh_provider_call_allowed=None,
    ) -> AIReply:
        assert callable(provider_call_allowed)
        self.started.set()
        await self.release.wait()
        if fresh_provider_call_allowed is not None:
            fresh = fresh_provider_call_allowed()
            if asyncio.iscoroutine(fresh):
                fresh = await fresh
            if fresh is not True:
                raise PrivacyBoundaryError("fresh provider authorization no longer allowed")
        if provider_call_allowed() is not True:
            raise PrivacyBoundaryError("provider call no longer allowed")
        self.requests.append(request)
        return AIReply(text="must not be sent", model="gpt-5.6-terra", provider="fake")


def _web_capability_snapshot() -> StaticCapabilitySnapshot:
    content = {
        "bindings": [],
        "capability_id": "cap-can-0153",
        "intent_tags": ["web_research"],
        "minimum_rbac": "everyone",
        "module_id": "intelligence.ai-runtime",
        "name": "Web search",
        "primary_intent": "web_research",
        "risk": "medium",
        "source_provenance": "runtime_manifest",
        "surface_bindings": ["command:ai.search"],
    }
    return StaticCapabilitySnapshot(
        (
            StaticCapabilityMetadata(
                capability_id="cap-can-0153",
                module_id="intelligence.ai-runtime",
                name="Web search",
                primary_intent="web_research",
                intent_tags=("web_research",),
                risk="medium",
                minimum_rbac="everyone",
                source_provenance="runtime_manifest",
                content_revision=capability_metadata_content_revision(content),
                surface_bindings=("command:ai.search",),
            ),
        )
    )


def _code_capability_snapshot() -> StaticCapabilitySnapshot:
    content = {
        "bindings": [],
        "capability_id": "cap-test-code-candidate",
        "intent_tags": ["code"],
        "minimum_rbac": "everyone",
        "module_id": "intelligence.ai-runtime",
        "name": "Code generation candidate",
        "primary_intent": "code",
        "risk": "medium",
        "source_provenance": "runtime_manifest",
        "surface_bindings": ["command:ai.ask"],
    }
    return StaticCapabilitySnapshot(
        (
            StaticCapabilityMetadata(
                capability_id=content["capability_id"],
                module_id=content["module_id"],
                name=content["name"],
                primary_intent="code",
                intent_tags=("code",),
                risk="medium",
                minimum_rbac="everyone",
                source_provenance="runtime_manifest",
                content_revision=capability_metadata_content_revision(content),
                surface_bindings=("command:ai.ask",),
            ),
        )
    )


@pytest.mark.asyncio
async def test_ai_search_is_explicitly_bounded_and_ask_remains_tool_free() -> None:
    service = _WebRecordingService()
    gateway = _SearchFabricGateway()
    store = ConversationStore()
    session = await store.start(guild_id=1, channel_id=3, user_id=2)
    await store.append_exchange(
        session_id=session.session_id,
        guild_id=1,
        channel_id=3,
        user_id=2,
        user_text="private history sentinel",
        assistant_text="private answer sentinel",
    )
    group = AIGroup(
        service,  # type: ignore[arg-type]
        web_search_available=True,
        capability_snapshot=_web_capability_snapshot(),
        search_gateway=gateway,
        search_gateway_current=lambda: gateway,
        conversation_store=store,
    )
    search_interaction = _interaction(interaction_id=801)

    await group.search.callback(group, search_interaction, "今日の宇宙ニュース")

    assert len(service.requests) == 1
    assert len(gateway.calls) == 1
    assert gateway.request_ids[0].startswith("search-")
    assert len(gateway.request_ids[0]) == len("search-") + 64
    assert set(gateway.request_ids[0].removeprefix("search-")) <= set("0123456789abcdef")
    assert not gateway.request_ids[0].startswith("discord-")
    search_request = service.requests[0]
    assert search_request.uses_tools is False
    assert search_request.web_search is False
    assert search_request.has_side_effects is False
    assert search_request.allowed_model_tools == ()
    assert search_request.max_tool_calls == 0
    assert search_request.bounded_toolset is not None
    assert search_request.bounded_toolset.effective_tools == ()
    assert search_request.history == ()
    assert search_request.attachments == ()
    assert "private history sentinel" not in search_request.prompt
    assert "private history sentinel" not in search_request.system_prompt
    assert "Search Fabricが直接取得した証拠です。" in search_request.prompt
    assert "https://example.com/docs" not in search_request.prompt
    assert search_interaction.followup.sent_messages
    rendered = search_interaction.followup.messages[-1][1]["embed"].description
    assert "https://example.com/docs" in rendered
    assert "https://model.invalid/source" not in rendered

    ask_interaction = _interaction(interaction_id=802)
    await group.ask.callback(group, ask_interaction, "今日は元気？", "normal", False)
    ask_request = service.requests[-1]
    assert ask_request.uses_tools is False
    assert ask_request.web_search is False
    assert ask_request.allowed_model_tools == ()
    assert ask_request.max_tool_calls == 0


@pytest.mark.asyncio
async def test_slash_core_artifact_is_prepared_for_renderer_without_exposing_core_identity() -> None:
    interaction = _interaction(interaction_id=8_101)
    data, artifact = _core_png_artifact_for_interaction(interaction)
    gateway = _CoreArtifactGateway(artifact)
    port = _CoreReadPort(data)
    current_port = [port]
    renderer = _CoreRecordingRenderer()
    service = _RecordingService()
    interaction.client.ai_service = service
    interaction.client.ai_execution_gateway = gateway
    group = AIGroup(
        service,  # type: ignore[arg-type]
        response_renderer=renderer,  # type: ignore[arg-type]
        execution_gateway=gateway,  # type: ignore[arg-type]
        core_artifact_delivery=CoreArtifactDeliveryPreparer(
            port,
            port_current=lambda: current_port[0],
        ),
    )

    await group.ask.callback(group, interaction, "今日の天気は？", "normal", False)

    assert len(gateway.starts) == 1
    assert len(port.requests) == 1
    assert renderer.sends == 1
    assert [item.filename for item in renderer.media] == ["media-01.png"]
    assert renderer.media[0].data == data
    rendered = "\n".join(renderer.contents)
    assert "Core result" in rendered
    assert "core-secret-artifact" not in rendered
    assert "core-secret-attachment" not in rendered
    assert interaction.client.capability_guard.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("read_revoke", "final_port_swap", "final_gateway_swap"))
async def test_slash_core_artifact_revocation_or_final_port_swap_never_sends_media(change: str) -> None:
    interaction_id = {
        "read_revoke": 8_102,
        "final_port_swap": 8_103,
        "final_gateway_swap": 8_104,
    }[change]
    interaction = _interaction(interaction_id=interaction_id)
    data, artifact = _core_png_artifact_for_interaction(interaction)
    gateway = _CoreArtifactGateway(artifact)
    replacement = _CoreReadPort(data)
    current_port: list[object] = []

    def revoke_after_read() -> None:
        if change == "read_revoke":
            interaction.client.capability_guard.allowed = False

    port = _CoreReadPort(data, on_read=revoke_after_read)
    current_port.append(port)

    def swap_before_final_send() -> None:
        if change == "final_port_swap":
            current_port[0] = replacement
        elif change == "final_gateway_swap":
            interaction.client.ai_execution_gateway = object()

    renderer = _CoreRecordingRenderer(before_fresh=swap_before_final_send)
    service = _RecordingService()
    interaction.client.ai_service = service
    interaction.client.ai_execution_gateway = gateway
    group = AIGroup(
        service,  # type: ignore[arg-type]
        response_renderer=renderer,  # type: ignore[arg-type]
        execution_gateway=gateway,  # type: ignore[arg-type]
        core_artifact_delivery=CoreArtifactDeliveryPreparer(
            port,
            port_current=lambda: current_port[0],  # type: ignore[return-value]
        ),
    )

    await group.ask.callback(group, interaction, "今日の天気は？", "normal", False)

    assert renderer.sends == 0
    if change == "read_revoke":
        assert renderer.media == ()
        assert interaction.followup.messages
    else:
        assert len(renderer.media) == 1


@pytest.mark.asyncio
async def test_slash_core_artifact_read_timeout_never_reaches_renderer() -> None:
    interaction = _interaction(interaction_id=8_105)
    data, artifact = _core_png_artifact_for_interaction(interaction)
    gateway = _CoreArtifactGateway(artifact)
    port = _HangingCoreReadPort(data)
    renderer = _CoreRecordingRenderer()
    service = _RecordingService()
    interaction.client.ai_service = service
    interaction.client.ai_execution_gateway = gateway
    group = AIGroup(
        service,  # type: ignore[arg-type]
        response_renderer=renderer,  # type: ignore[arg-type]
        execution_gateway=gateway,  # type: ignore[arg-type]
        core_artifact_delivery=CoreArtifactDeliveryPreparer(
            port,
            port_current=lambda: port,
            timeout_seconds=0.1,
        ),
    )

    await group.ask.callback(group, interaction, "今日の天気は？", "normal", False)

    assert renderer.sends == 0
    assert renderer.media == ()
    assert len(port.requests) == 1


def test_web_sources_accept_only_web_search_call_items() -> None:
    response = {
        "output": [
            {
                "type": "message",
                "action": {"sources": [{"title": "偽", "url": "https://example.invalid/forged"}]},
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [
                            {"type": "url_citation", "title": "偽", "url": "https://example.invalid/citation"}
                        ],
                    }
                ],
            },
            {
                "type": "web_search_call",
                "action": {"sources": [{"title": "公式", "url": "https://example.com/docs"}]},
            },
        ]
    }

    assert _extract_web_sources(response) == (AISource(title="公式", url="https://example.com/docs"),)


@pytest.mark.asyncio
async def test_ai_ask_rejects_secret_like_prompt_before_remote_provider() -> None:
    response = _Response()
    interaction = SimpleNamespace(
        guild_id=1,
        user=SimpleNamespace(id=2),
        response=response,
    )
    group = AIGroup(_Service())  # type: ignore[arg-type]

    await group.ask.callback(
        group,
        interaction,
        "OPENAI_API_KEY=sk-" + "proj-this-is-a-secret-value-123456",
        "normal",
        True,
    )

    assert response.deferred is False
    assert len(response.messages) == 1
    assert "秘密情報" in response.messages[0][0]
    assert response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_ai_ask_recognized_unknown_stops_before_service_with_clarification() -> None:
    service = _RecordingService()
    interaction = _interaction()
    group = AIGroup(service)  # type: ignore[arg-type]

    await group.ask.callback(group, interaction, "PCを操作して", "normal", False)

    assert service.requests == []
    assert interaction.response.deferred is False
    assert len(interaction.response.messages) == 1
    content, kwargs = interaction.response.messages[0]
    assert content == UNKNOWN_OPERATION_REPLY
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].everyone is False
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_ai_ask_normal_mode_keeps_code_route_without_exposing_tools_or_side_effects() -> None:
    service = _RecordingService()
    interaction = _interaction()
    group = AIGroup(service)  # type: ignore[arg-type]

    await group.ask.callback(group, interaction, "Discord BOTのコードを書いて実装して", "normal", False)

    request = service.requests[0]
    assert request.task_kind is TaskKind.CODE_GENERATION
    assert request.complexity is TaskComplexity.COMPLEX
    assert request.risk is RiskLevel.NORMAL
    assert request.uses_tools is False
    assert request.web_search is False
    assert request.has_side_effects is False
    assert request.bounded_toolset is not None
    assert request.bounded_toolset.effective_tools == ()
    assert request.allowed_model_tools == ()
    assert request.max_tool_calls == 0
    assert interaction.followup.messages[0][1]["embed"].description == "完了"


@pytest.mark.asyncio
async def test_ai_ask_complex_mode_only_escalates_classified_route() -> None:
    service = _RecordingService()
    interaction = _interaction()
    group = AIGroup(service)  # type: ignore[arg-type]

    await group.ask.callback(group, interaction, "今日は元気？", "complex", True)

    request = service.requests[0]
    assert request.task_kind is TaskKind.GENERAL
    assert request.complexity is TaskComplexity.COMPLEX
    assert request.risk is RiskLevel.NORMAL
    assert request.max_tool_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_complexity"),
    (("normal", TaskComplexity.STANDARD), ("complex", TaskComplexity.COMPLEX)),
)
async def test_ai_ask_context_authorization_tracks_effective_mode_complexity(
    mode: str,
    expected_complexity: TaskComplexity,
) -> None:
    service = _RecordingService()
    interaction = _interaction()
    group = AIGroup(service)  # type: ignore[arg-type]

    await group.ask.callback(group, interaction, "今日は元気？", mode, False)

    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.complexity is expected_complexity
    assert request.context_authorization is not None
    assert request.bounded_toolset is not None
    assert request.context_authorization_current()


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_remote", [False, True])
async def test_ai_ask_persistent_consent_is_authority_regardless_of_compatibility_flag(
    allow_remote: bool,
) -> None:
    service = _RemoteRecordingService()
    interaction = _interaction()
    group = AIGroup(
        service,  # type: ignore[arg-type]
        remote_consent_active=lambda user_id: user_id == 2,
    )

    await group.ask.callback(group, interaction, "永続同意済みの質問", "normal", allow_remote)

    assert len(service.requests) == 1
    assert service.requests[0].boundary.value == "remote_opt_in"


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_remote", [False, True])
async def test_ai_ask_compatibility_flag_never_replaces_missing_persistent_consent(
    allow_remote: bool,
) -> None:
    service = _RemoteRecordingService()
    interaction = _interaction()
    group = AIGroup(
        service,  # type: ignore[arg-type]
        remote_consent_active=lambda _user_id: False,
    )

    await group.ask.callback(group, interaction, "未同意の質問", "normal", allow_remote)

    assert service.requests == []
    assert interaction.response.deferred is False
    assert "初回同意が未完了" in interaction.response.messages[0][0]


@pytest.mark.asyncio
async def test_ai_ask_unknown_provider_locality_is_not_assumed_local() -> None:
    service = _UnknownLocalityService()
    interaction = _interaction()
    group = AIGroup(service)  # type: ignore[arg-type]

    await group.ask.callback(group, interaction, "provider locality不明", "normal", True)

    assert service.requests == []
    assert "初回同意が未完了" in interaction.response.messages[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("policy", "shutdown"))
async def test_ai_ask_rechecks_policy_and_shutdown_at_provider_sink(changed: str) -> None:
    service = _BlockingAuthorizedService()
    interaction = _interaction()
    group = AIGroup(service)  # type: ignore[arg-type]

    task = asyncio.create_task(group.ask.callback(group, interaction, "外部AIに送る質問", "normal", True))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    if changed == "policy":
        interaction.client.capability_guard.allowed = False
    else:
        interaction.client.is_closing = True
    service.release.set()
    await task

    assert service.requests == []
    assert "AIへ送信しませんでした" in interaction.followup.messages[0][0]


@pytest.mark.asyncio
async def test_ai_ask_rechecks_exact_capability_candidates_at_provider_sink() -> None:
    service = _BlockingAuthorizedService()
    interaction = _interaction(interaction_id=110)
    group = AIGroup(
        service,  # type: ignore[arg-type]
        capability_snapshot=_code_capability_snapshot(),
    )

    task = asyncio.create_task(
        group.ask.callback(group, interaction, "Discord BOTのコードを書いて実装して", "normal", False)
    )
    try:
        await asyncio.wait_for(service.started.wait(), timeout=1.0)
        interaction.client.capability_guard.denied_capability_ids.add("cap-test-code-candidate")
        service.release.set()
        await task
    finally:
        service.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert service.requests == []
    assert interaction.followup.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("with_attachment", (False, True))
async def test_ai_ask_fresh_sink_recheck_rejects_revoked_member_without_cached_role_fallback(
    with_attachment: bool,
) -> None:
    service = _BlockingAuthorizedService()
    interaction = _interaction(interaction_id=108 if with_attachment else 109)
    attachment = None
    if with_attachment:
        interaction.client.settings.ai_attachments_enabled = True
        attachment = _DiscordAttachment(b"fresh attachment")
    group = AIGroup(
        service,  # type: ignore[arg-type]
        attachments_available=with_attachment,
    )

    task = asyncio.create_task(
        group.ask.callback(group, interaction, "Discord BOTのコードを書いて実装して", "normal", False, attachment)
    )
    try:
        await asyncio.wait_for(service.started.wait(), timeout=1.0)
        # interaction.user は変えず、REST fetch が返す現在memberだけを降格する。
        interaction.guild.fresh_member.fresh_attachment_allowed = not with_attachment
        interaction.guild.fresh_member.fresh_ai_allowed = False if not with_attachment else True
        service.release.set()
        await task
    finally:
        service.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert service.requests == []
    assert "AIへ送信しませんでした" in interaction.followup.messages[0][0]


@pytest.mark.asyncio
async def test_ai_ask_reuses_one_progress_reply_and_appends_only_same_scope_history() -> None:
    store = ConversationStore()
    service = _RecordingService()
    renderer = DiscordAITaskProgressRenderer(
        edit_policy=ProgressEditPolicy(min_edit_interval_seconds=0.25),
    )
    group = AIGroup(service, conversation_store=store, task_progress_renderer=renderer)  # type: ignore[arg-type]

    first = _interaction(interaction_id=101)
    await group.ask.callback(group, first, "最初のDiscordコードを実装して", "normal", False)
    assert first.response.deferred is True
    assert len(first.followup.messages) == 1
    assert first.followup.sent_messages[0].edits

    same_scope = _interaction(interaction_id=102)
    await group.ask.callback(group, same_scope, "続きのDiscordコードを実装して", "normal", False)
    assert [turn.text for turn in service.requests[1].history] == ["最初のDiscordコードを実装して", "完了"]

    other_scope = _interaction(channel_id=4, interaction_id=103)
    await group.ask.callback(group, other_scope, "別チャンネルのDiscordコードを実装して", "normal", False)
    assert service.requests[2].history == ()


@pytest.mark.asyncio
async def test_ai_ask_attachment_uses_existing_admission_without_persisting_binary(tmp_path) -> None:
    state = AIStateRepository(tmp_path / "slash-attachments.sqlite3")
    store = ConversationStore(repository=state)
    service = _RecordingService()
    interaction = _interaction(interaction_id=104)
    interaction.client.settings.ai_attachments_enabled = True
    attachment = _DiscordAttachment(b"safe UTF-8 attachment")
    group = AIGroup(
        service,
        conversation_store=store,
        attachments_available=True,
    )  # type: ignore[arg-type]

    try:
        await group.ask.callback(group, interaction, "この添付を要約して", "normal", False, attachment)

        assert attachment.reads == 1
        assert len(service.requests) == 1
        assert service.requests[0].attachments[0].data == b"safe UTF-8 attachment"
        snapshot = await store.get(guild_id=1, channel_id=3, user_id=2)
        assert snapshot is not None
        assert snapshot.history[0].attachments[0].data == b"safe UTF-8 attachment"
        persisted_values = tuple(
            value for exchange in state.load_conversations()[0].exchanges for value in exchange[:2]
        )
        assert all("safe UTF-8 attachment" not in value for value in persisted_values)
    finally:
        state.close()


@pytest.mark.asyncio
async def test_ai_ask_shared_admission_rejects_parallel_and_closing_runs() -> None:
    admission = AIAdmissionController(max_global=1, max_waiters=1, wait_timeout_seconds=0.1)
    service = _BlockingAuthorizedService()
    group = AIGroup(service, admission=admission)  # type: ignore[arg-type]
    first = _interaction(interaction_id=1_401)
    second = _interaction(interaction_id=1_402, user_id=4)

    task = asyncio.create_task(group.ask.callback(group, first, "最初の質問", "normal", False))
    try:
        await asyncio.wait_for(service.started.wait(), timeout=1.0)
        await group.ask.callback(group, second, "二番目の質問", "normal", False)
        assert service.requests == []
        assert "混雑" in second.followup.messages[0][0]
        await admission.begin_close()
        closing = _interaction(interaction_id=1_403, user_id=5)
        await group.ask.callback(group, closing, "停止中の質問", "normal", False)
        assert "権限または機能設定" in closing.followup.messages[0][0]
        await group.begin_close()
        after_group_close = _interaction(interaction_id=1_404, user_id=6)
        await group.ask.callback(group, after_group_close, "閉鎖後の質問", "normal", False)
        assert after_group_close.response.deferred is False
        assert "権限または機能設定" in after_group_close.response.messages[0][0]
    finally:
        service.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ai_ask_attachment_discards_payload_when_fresh_member_changes_during_read() -> None:
    service = _RecordingService()
    interaction = _interaction(interaction_id=1_404)
    interaction.client.settings.ai_attachments_enabled = True
    attachment = _BlockingDiscordAttachment(b"discard after revocation")
    group = AIGroup(service, attachments_available=True)  # type: ignore[arg-type]

    task = asyncio.create_task(group.ask.callback(group, interaction, "添付を確認して", "normal", False, attachment))
    try:
        await asyncio.wait_for(attachment.started.wait(), timeout=1.0)
        interaction.guild.fresh_member.fresh_attachment_allowed = False
        attachment.release.set()
        await task
    finally:
        attachment.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert attachment.reads == 1
    assert service.requests == []
    assert "添付の読み取りを中止しました" in interaction.followup.messages[0][0]


@pytest.mark.asyncio
async def test_ai_ask_remote_consent_button_persists_and_resumes_once_then_deletes_prompt(tmp_path) -> None:
    state = AIStateRepository(tmp_path / "slash-consent.sqlite3")
    consent_store = RemoteConsentStore(repository=state)
    service = _RemoteRecordingService()
    interaction = _interaction(interaction_id=105)
    group = AIGroup(
        service,  # type: ignore[arg-type]
        remote_consent_active=consent_store.active_user,
        remote_consent_store=consent_store,
    )

    try:
        await group.ask.callback(group, interaction, "外部AIへの質問", "normal", False)

        assert service.requests == []
        assert interaction.response.deferred is True
        content, kwargs = interaction.followup.messages[0]
        assert "Discord user ID単位" in content
        view = kwargs["view"]
        prompt_message = interaction.followup.sent_messages[0]
        confirmation = _interaction(interaction_id=106)
        confirmation.message = prompt_message
        await view.confirm(confirmation)

        assert consent_store.active_user(2) is True
        assert state.get_consent(2) is not None
        assert len(service.requests) == 1
        assert prompt_message.deleted is True
    finally:
        state.close()


@pytest.mark.asyncio
async def test_ai_ask_uses_configured_gateway_availability_without_local_service_bypass() -> None:
    service = _RemoteRecordingService()
    service.available = False
    interaction = _interaction(interaction_id=1_404_001)
    consent_store = RemoteConsentStore()
    group = AIGroup(  # type: ignore[arg-type]
        service,
        provider_is_local=False,
        provider_available=True,
        remote_consent_active=consent_store.active_user,
        remote_consent_store=consent_store,
    )

    await group.ask.callback(group, interaction, "外部AIへの質問", "normal", False)

    assert service.requests == []
    assert interaction.response.deferred is True
    assert "Discord user ID単位" in interaction.followup.messages[0][0]
    assert "未設定" not in interaction.followup.messages[0][0]


@pytest.mark.asyncio
async def test_ai_ask_remote_consent_pending_views_are_bounded_and_closed_on_shutdown(tmp_path) -> None:
    state = AIStateRepository(tmp_path / "slash-consent-bound.sqlite3")
    consent_store = RemoteConsentStore(repository=state)
    group = AIGroup(
        _RemoteRecordingService(),  # type: ignore[arg-type]
        remote_consent_active=consent_store.active_user,
        remote_consent_store=consent_store,
        max_pending_consent_prompts=1,
    )
    first = _interaction(interaction_id=1_405, user_id=11)
    second = _interaction(interaction_id=1_406, user_id=12)
    try:
        await group.ask.callback(group, first, "最初の外部AI質問", "normal", False)
        first_view = first.followup.messages[0][1]["view"]
        await group.ask.callback(group, second, "次の外部AI質問", "normal", False)

        assert len(group._pending_remote_consents) == 1  # noqa: SLF001 - bounded pending invariant
        assert first_view.consumed is True
        await group.begin_close()
        assert group._pending_remote_consents == {}  # noqa: SLF001 - shutdown closes pending cards
        second_view = second.followup.messages[0][1]["view"]
        assert second_view.consumed is True
        await second_view.confirm(_interaction(interaction_id=1_407, user_id=12))
        assert consent_store.active_user(12) is False
    finally:
        state.close()


@pytest.mark.asyncio
async def test_ai_ask_rechecks_fresh_member_after_conversation_wait() -> None:
    store = ConversationStore()
    service = _RecordingService()
    interaction = _interaction(interaction_id=107)
    group = AIGroup(service, conversation_store=store)  # type: ignore[arg-type]
    original_snapshot = group._conversation_snapshot
    reached_snapshot = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def delayed_snapshot(value: object) -> object:
        snapshot = await original_snapshot(value)  # type: ignore[arg-type]
        reached_snapshot.set()
        await release_snapshot.wait()
        return snapshot

    group._conversation_snapshot = delayed_snapshot  # type: ignore[method-assign]
    task = asyncio.create_task(group.ask.callback(group, interaction, "待機後の質問", "normal", False))
    try:
        await asyncio.wait_for(reached_snapshot.wait(), timeout=1.0)
        interaction.client.capability_guard.allowed = False
        release_snapshot.set()
        await task
    finally:
        release_snapshot.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert service.requests == []
    assert "AIへ送信しませんでした" in interaction.followup.messages[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("forget", "capability"))
async def test_ai_ask_rechecks_exact_memory_authorization_at_provider_sink(tmp_path, changed: str) -> None:
    state = AIStateRepository(tmp_path / "state.sqlite3")
    repository = V0ExplicitMemoryRepository(state, clock=lambda: 1_000)
    scope = Scope(1, 2, visibility=MemoryVisibility.GUILD_PUBLIC)
    repository.set_privacy(
        guild_id=1,
        user_id=2,
        visibility=MemoryVisibility.GUILD_PUBLIC,
        channel_id=None,
    )
    record = repository.remember(scope, "待機中に再認可する記憶")
    allowed = [True]
    service = _BlockingAuthorizedService()
    interaction = _interaction()
    group = AIGroup(
        service,  # type: ignore[arg-type]
        explicit_memory_repository=repository,
        memory_recall_allowed=lambda _interaction: allowed[0],
    )
    try:
        task = asyncio.create_task(group.ask.callback(group, interaction, "記憶を使って答えて", "normal", True))
        await asyncio.wait_for(service.started.wait(), timeout=1.0)
        if changed == "forget":
            assert repository.forget(scope, record.memory_id) is True
        else:
            allowed[0] = False
        service.release.set()
        await task
    finally:
        state.close()

    assert service.requests == []


@pytest.mark.asyncio
async def test_ai_ask_automatically_recalls_query_relevant_memory_without_id(tmp_path) -> None:
    state = AIStateRepository(tmp_path / "state.sqlite3")
    now = [1_000]

    def clock() -> int:
        value = now[0]
        now[0] += 1
        return value

    repository = V0ExplicitMemoryRepository(state, clock=clock)
    scope = Scope(1, 2, visibility=MemoryVisibility.GUILD_PUBLIC)
    repository.set_privacy(
        guild_id=1,
        user_id=2,
        visibility=MemoryVisibility.GUILD_PUBLIC,
        channel_id=None,
    )
    repository.remember(scope, "project_zeta uses rust")
    for index in range(6):
        repository.remember(scope, f"unrelated recent memory {index}")
    service = _RecordingService()
    interaction = _interaction()
    group = AIGroup(
        service,  # type: ignore[arg-type]
        explicit_memory_repository=repository,
        memory_recall_allowed=lambda _interaction: True,
    )
    try:
        await group.ask.callback(group, interaction, "project_zetaについて教えて", "normal", False)
    finally:
        state.close()

    assert "project_zeta uses rust" in service.requests[0].system_prompt
    assert "unrelated recent memory" not in service.requests[0].system_prompt


@pytest.mark.asyncio
async def test_ai_ask_auto_short_reply_is_plain() -> None:
    service = _RecordingService()
    interaction = _interaction()
    group = AIGroup(service)  # type: ignore[arg-type]

    await group.ask.callback(group, interaction, "今日は元気？", "normal", False)

    content, kwargs = interaction.followup.messages[0]
    assert content == "完了\n\n-# gpt-5.6-sol"
    assert "embed" not in kwargs
    assert "mention_author" not in kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].everyone is False


@pytest.mark.asyncio
async def test_ai_ask_explicit_card_and_plain_override_auto_route() -> None:
    preferences = DisplayPreferenceStore()
    preferences.set(2, DisplayMode.CARD)
    card_interaction = _interaction()
    card_group = AIGroup(_RecordingService(), preferences)  # type: ignore[arg-type]

    await card_group.ask.callback(card_group, card_interaction, "今日は元気？", "normal", False)

    assert card_interaction.followup.messages[0][1]["embed"].description == "完了"

    preferences.set(2, DisplayMode.PLAIN)
    plain_interaction = _interaction()
    plain_service = _RecordingService()
    plain_group = AIGroup(plain_service, preferences)  # type: ignore[arg-type]

    await plain_group.ask.callback(
        plain_group,
        plain_interaction,
        "Discord BOTのコードを書いて実装して",
        "normal",
        False,
    )

    content, kwargs = plain_interaction.followup.messages[0]
    assert content == "完了\n\n-# gpt-5.6-sol"
    assert "embed" not in kwargs
    assert "mention_author" not in kwargs
    assert plain_service.requests[0].uses_tools is False
    assert plain_service.requests[0].max_tool_calls == 0


@pytest.mark.asyncio
async def test_ai_ask_plain_long_output_keeps_full_utf8_attachment_and_discord_limit() -> None:
    answer = "回答😀" * 1_000
    preferences = DisplayPreferenceStore()
    preferences.set(2, DisplayMode.PLAIN)
    interaction = _interaction()
    group = AIGroup(_RecordingService(text=answer), preferences)  # type: ignore[arg-type]

    await group.ask.callback(group, interaction, "長い回答をください", "normal", False)

    content, kwargs = interaction.followup.messages[0]
    assert len(content) <= 2_000
    assert kwargs["ephemeral"] is True
    assert "mention_author" not in kwargs
    assert kwargs["allowed_mentions"].everyone is False
    attachment = next(file for file in kwargs["files"] if file.filename == "yonerai-answer.md")
    assert attachment.fp.read().decode("utf-8") == answer


@pytest.mark.asyncio
async def test_ai_ask_auto_code_output_uses_card_and_preserves_code_attachment() -> None:
    answer = "```python\n" + ("print('安全')\n" * 200) + "```"
    interaction = _interaction()
    group = AIGroup(_RecordingService(text=answer))  # type: ignore[arg-type]

    await group.ask.callback(group, interaction, "Pythonのコードを書いて", "normal", False)

    _content, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is True
    assert "mention_author" not in kwargs
    assert kwargs["embed"].title == "YonerAI Code"
    assert kwargs["embed"].footer.text == "モデル: gpt-5.6-sol"
    assert any(file.filename == "yonerai-code-1.py" for file in kwargs["files"])


@pytest.mark.asyncio
async def test_ai_ask_duplicate_interaction_runs_provider_and_followup_once() -> None:
    service = _RecordingService()
    group = AIGroup(service)  # type: ignore[arg-type]
    first = _interaction()
    duplicate = _interaction()

    await group.ask.callback(group, first, "一度だけ処理", "normal", False)
    await group.ask.callback(group, duplicate, "一度だけ処理", "normal", False)

    assert len(service.requests) == 1
    assert len(first.followup.messages) == 1
    assert duplicate.followup.messages == []
