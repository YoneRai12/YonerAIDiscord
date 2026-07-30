from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest

from yonerai_discord.ai_control import RiskLevel, TaskComplexity, TaskKind
from yonerai_discord.capability_metadata_contract import (
    canonical_capability_metadata_json,
    capability_metadata_content_revision,
)
from yonerai_discord.modules.ai import (
    PROVIDER_METADATA_ALLOWED_KEYS,
    AIReply,
    AIRequest,
    AIService,
    AIUnavailableError,
    DataBoundary,
    PrivacyBoundaryError,
    ProviderAuthorizationError,
)
from yonerai_discord.modules.ai.bounded_tools import (
    BoundedToolSet,
    EMPTY_CAPABILITY_SNAPSHOT,
    StaticCapabilityMetadata,
    StaticCapabilitySnapshot,
    ToolScopeBinding,
    canonical_revision,
    capability_metadata_transport,
)
from yonerai_discord.modules.ai.ports import (
    _issue_service_sink_verifier,
    _verify_service_sink,
    _verify_service_sink_async,
)
from yonerai_discord.modules.ai.provider import OpenAICompatibleProvider, ProviderConfigurationError
from yonerai_discord.modules.ai.models import provider_facing_envelope_digest
from yonerai_discord.v0_contracts import (
    ContextBuildInput,
    FORMAL_PROVIDER_INPUT_DIRECTIVE,
    MemoryVisibility,
    Scope,
)
from yonerai_discord.v0_runtime.context_builder import RuntimeContextBuilder


class FakeProvider:
    def __init__(self, *, local: bool) -> None:
        self._local = local
        self.calls = 0

    @property
    def is_local(self) -> bool:
        return self._local

    async def complete(self, request: AIRequest) -> AIReply:
        self.calls += 1
        return AIReply(text="ok", model="fake", provider="fake")


def _foreign_capability_metadata() -> str:
    content = {
        "bindings": [],
        "capability_id": "cap-test-foreign",
        "intent_tags": ["conversation"],
        "minimum_rbac": "everyone",
        "module_id": "intelligence.ai-runtime",
        "name": "Foreign candidate",
        "primary_intent": "conversation",
        "risk": "low",
        "source_provenance": "runtime_manifest",
        "surface_bindings": ["command:ai.ask"],
    }
    return canonical_capability_metadata_json(
        {
            **content,
            "content_revision": capability_metadata_content_revision(content),
        }
    )


def _static_conversation_candidate(capability_id: str, name: str) -> StaticCapabilityMetadata:
    content = {
        "bindings": [],
        "capability_id": capability_id,
        "intent_tags": ["conversation"],
        "minimum_rbac": "everyone",
        "module_id": "intelligence.ai-runtime",
        "name": name,
        "primary_intent": "conversation",
        "risk": "low",
        "source_provenance": "runtime_manifest",
        "surface_bindings": ["command:ai.ask"],
    }
    return StaticCapabilityMetadata(
        capability_id=capability_id,
        module_id="intelligence.ai-runtime",
        name=name,
        primary_intent="conversation",
        intent_tags=("conversation",),
        risk="low",
        minimum_rbac="everyone",
        source_provenance="runtime_manifest",
        content_revision=capability_metadata_content_revision(content),
        surface_bindings=("command:ai.ask",),
    )


class BlockingProvider(FakeProvider):
    def __init__(self, *, local: bool = True) -> None:
        super().__init__(local=local)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: AIRequest) -> AIReply:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return AIReply(text="ok", model="fake", provider="fake")

    async def complete_authorized(
        self,
        request: AIRequest,
        provider_sink_verifier: object,
    ) -> AIReply:
        if not _verify_service_sink(
            provider_sink_verifier,
            request=request,
            provider=self,
        ):
            raise ProviderAuthorizationError("authorization expired at fake provider sink")
        return await self.complete(request)


def _formal_provider_envelope_digest(
    *,
    prompt: str = "current input",
    boundary: DataBoundary = DataBoundary.LOCAL_ONLY,
    complexity: TaskComplexity = TaskComplexity.STANDARD,
    metadata: dict[str, str] | None = None,
    task_kind: TaskKind = TaskKind.GENERAL,
    risk: RiskLevel = RiskLevel.NORMAL,
    uses_tools: bool = False,
    web_search: bool = False,
    has_side_effects: bool = False,
    required_model_alias: str | None = None,
    required_model_id: str | None = None,
) -> str:
    return provider_facing_envelope_digest(
        prompt=prompt,
        provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
        history=(),
        attachments=(),
        metadata={} if metadata is None else metadata,
        task_kind=task_kind,
        complexity=complexity,
        risk=risk,
        uses_tools=uses_tools,
        web_search=web_search,
        has_side_effects=has_side_effects,
        boundary=boundary,
        required_model_alias=required_model_alias,
        required_model_id=required_model_id,
    )


@pytest.mark.asyncio
async def test_local_ai_is_allowed_by_default() -> None:
    provider = FakeProvider(local=True)
    reply = await AIService(provider).ask(AIRequest(prompt="こんにちは", guild_id=1, user_id=2))
    assert reply.text == "ok"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_remote_ai_requires_explicit_opt_in() -> None:
    provider = FakeProvider(local=False)
    service = AIService(provider)
    with pytest.raises(PrivacyBoundaryError):
        await service.ask(AIRequest(prompt="秘密", guild_id=1, user_id=2))
    await service.ask(AIRequest(prompt="送信可", guild_id=1, user_id=2, boundary=DataBoundary.REMOTE_OPT_IN))
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["missing_token", "changed_prompt"])
async def test_prepared_context_requires_matching_context_builder_authorization(tamper: str) -> None:
    provider = FakeProvider(local=True)
    service = AIService(provider, require_prepared_context=True)
    context = RuntimeContextBuilder().build(
        ContextBuildInput(
            Scope(1, 2, channel_id=3, visibility=MemoryVisibility.USER_PRIVATE),
            "current input",
            (),
            request_channel_id=3,
        )
    )
    request = AIRequest(
        prompt="current input",
        guild_id=1,
        channel_id=3,
        user_id=2,
        provider_input="Respond only to the current input section.",
        system_prompt=context.prompt,
        context_authorization=context.context_authorization,
    )
    if tamper == "missing_token":
        request = AIRequest(
            prompt=request.prompt,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            user_id=request.user_id,
            provider_input=request.provider_input,
            system_prompt=request.system_prompt,
        )
    else:
        with pytest.raises(ValueError, match="final provider envelope"):
            AIRequest(
                prompt=request.prompt,
                guild_id=request.guild_id,
                channel_id=request.channel_id,
                user_id=request.user_id,
                provider_input=request.provider_input,
                system_prompt=f"{request.system_prompt}\nforged",
                context_authorization=request.context_authorization,
            )
        assert provider.calls == 0
        return

    with pytest.raises(AIUnavailableError, match="canonical ContextBuilder"):
        await service.ask(request)
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_nonformal_prepared_context_without_provider_envelope_claim_remains_compatible() -> None:
    provider = FakeProvider(local=True)
    service = AIService(provider, require_prepared_context=True)
    context = RuntimeContextBuilder().build(
        ContextBuildInput(
            Scope(1, 2, channel_id=3, visibility=MemoryVisibility.USER_PRIVATE),
            "current input",
            (),
            request_channel_id=3,
        )
    )
    request = AIRequest(
        prompt="current input",
        guild_id=1,
        channel_id=3,
        user_id=2,
        provider_input="Respond only to the current input section.",
        system_prompt=context.prompt,
        context_authorization=context.context_authorization,
    )

    assert request.context_authorization is not None
    assert request.context_authorization.provider_envelope_sha256 is None
    assert request.context_authorization_current()
    reply = await service.ask(request)

    assert reply.text == "ok"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_nonformal_request_cannot_reuse_or_tamper_with_a_formal_envelope_claim() -> None:
    provider = FakeProvider(local=True)
    service = AIService(provider, require_prepared_context=True)
    provider_revision = service.provider_catalog_revision
    toolset = BoundedToolSet.issue(
        scope=ToolScopeBinding(1, 3, 2),
        intent="conversation",
        complexity="standard",
        snapshot=EMPTY_CAPABILITY_SNAPSHOT,
        provider_catalog_revision=provider_revision,
        web_search=False,
        issued_at=100.0,
    )
    context = RuntimeContextBuilder().build(
        ContextBuildInput(
            Scope(1, 2, channel_id=3, visibility=MemoryVisibility.USER_PRIVATE),
            "current input",
            (),
            request_channel_id=3,
            intent=toolset.intent,
            complexity=toolset.complexity,
            bounded_toolset_digest=toolset.digest,
            capability_catalog_revision=toolset.capability_catalog_revision,
            provider_catalog_revision=toolset.provider_catalog_revision,
            provider_envelope_sha256=_formal_provider_envelope_digest(),
        )
    )
    request = AIRequest(
        prompt="current input",
        guild_id=1,
        channel_id=3,
        user_id=2,
        provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
        system_prompt=context.prompt,
        context_authorization=context.context_authorization,
        bounded_toolset=toolset,
    )
    object.__setattr__(request, "bounded_toolset", None)
    object.__setattr__(request, "provider_input", "FORGED")

    assert not request.context_authorization_current()
    with pytest.raises(AIUnavailableError, match="canonical ContextBuilder"):
        await service.ask(request)
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["legacy_token", "toolset_revision"])
async def test_formal_prepared_context_rejects_legacy_or_changed_toolset_before_provider_call(
    tamper: str,
) -> None:
    class AuthorizedProvider(FakeProvider):
        async def complete_authorized(
            self,
            request: AIRequest,
            provider_sink_verifier: object,
        ) -> AIReply:
            if not _verify_service_sink(
                provider_sink_verifier,
                request=request,
                provider=self,
            ):
                raise ProviderAuthorizationError("authorization expired")
            return await self.complete(request)

    provider = AuthorizedProvider(local=True)
    service = AIService(
        provider,
        require_prepared_context=True,
        require_authorization=True,
        clock=lambda: 100.5,
    )
    toolset = BoundedToolSet.issue(
        scope=ToolScopeBinding(1, 3, 2),
        intent="conversation",
        complexity="standard",
        snapshot=EMPTY_CAPABILITY_SNAPSHOT,
        provider_catalog_revision=service.provider_catalog_revision,
        web_search=False,
        issued_at=100.0,
    )
    context = RuntimeContextBuilder().build(
        ContextBuildInput(
            Scope(1, 2, channel_id=3, visibility=MemoryVisibility.USER_PRIVATE),
            "current input",
            (),
            request_channel_id=3,
            intent="conversation",
            complexity="standard",
            bounded_toolset_digest=toolset.digest,
            capability_catalog_revision=toolset.capability_catalog_revision,
            provider_catalog_revision=toolset.provider_catalog_revision,
            provider_envelope_sha256=_formal_provider_envelope_digest(),
        )
    )
    request = AIRequest(
        prompt="current input",
        guild_id=1,
        channel_id=3,
        user_id=2,
        provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
        system_prompt=context.prompt,
        context_authorization=context.context_authorization,
        bounded_toolset=toolset,
    )
    if tamper == "legacy_token":
        assert request.context_authorization is not None
        object.__setattr__(
            request,
            "context_authorization",
            replace(
                request.context_authorization,
                contract_version="yonerai-context-seven-section-v1",
                bounded_context_sha256=None,
                provider_envelope_sha256=None,
            ),
        )
    else:
        object.__setattr__(
            request,
            "bounded_toolset",
            replace(
                toolset,
                capability_catalog_revision=canonical_revision({"changed": True}),
                digest="",
            ),
        )

    with pytest.raises(AIUnavailableError, match="provider envelope"):
        await service.ask(
            request,
            provider_call_allowed=lambda: True,
        )

    assert provider.calls == 0


@pytest.mark.parametrize("actual_candidates", ["empty", "different"])
def test_ai_request_rejects_capability_metadata_not_issued_by_actual_toolset(
    actual_candidates: str,
) -> None:
    provider_revision = canonical_revision({"provider": "test"})
    snapshot = (
        EMPTY_CAPABILITY_SNAPSHOT
        if actual_candidates == "empty"
        else StaticCapabilitySnapshot((_static_conversation_candidate("cap-test-actual", "Actual candidate"),))
    )
    toolset = BoundedToolSet.issue(
        scope=ToolScopeBinding(1, 3, 2),
        intent="conversation",
        complexity="standard",
        snapshot=snapshot,
        provider_catalog_revision=provider_revision,
        web_search=False,
        issued_at=100.0,
    )
    context = RuntimeContextBuilder().build(
        ContextBuildInput(
            Scope(1, 2, channel_id=3, visibility=MemoryVisibility.USER_PRIVATE),
            "current input",
            (),
            request_channel_id=3,
            intent="conversation",
            capability_metadata=(_foreign_capability_metadata(),),
            complexity="standard",
            bounded_toolset_digest=toolset.digest,
            capability_catalog_revision=toolset.capability_catalog_revision,
            provider_catalog_revision=toolset.provider_catalog_revision,
            provider_envelope_sha256=_formal_provider_envelope_digest(),
        )
    )
    provider = FakeProvider(local=True)

    with pytest.raises(ValueError, match="context_authorization"):
        AIRequest(
            prompt="current input",
            guild_id=1,
            channel_id=3,
            user_id=2,
            provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
            system_prompt=context.prompt,
            context_authorization=context.context_authorization,
            bounded_toolset=toolset,
        )

    assert provider.calls == 0


def test_service_sink_verifier_is_one_shot_and_bound_to_exact_request_and_provider() -> None:
    request = AIRequest(prompt="first", guild_id=1, user_id=2)
    other_request = AIRequest(prompt="second", guild_id=1, user_id=2)
    provider = object()
    other_provider = object()
    verifier = _issue_service_sink_verifier(
        request=request,
        provider=provider,
        check=lambda: True,
    )

    assert not callable(verifier)
    assert not _verify_service_sink(verifier, request=other_request, provider=provider)
    assert not _verify_service_sink(verifier, request=request, provider=other_provider)
    assert _verify_service_sink(verifier, request=request, provider=provider)
    assert not _verify_service_sink(verifier, request=request, provider=provider)
    failing = _issue_service_sink_verifier(
        request=request,
        provider=provider,
        check=lambda: (_ for _ in ()).throw(RuntimeError("do-not-leak")),
    )
    assert not _verify_service_sink(failing, request=request, provider=provider)


def test_provider_rejects_remote_endpoint_without_opt_in() -> None:
    with pytest.raises(ProviderConfigurationError):
        OpenAICompatibleProvider(base_url="https://example.com/v1", openai_api_key="key")


def test_ai_request_repr_does_not_embed_provider_secret() -> None:
    request = AIRequest(prompt="hello", guild_id=1, user_id=2)
    assert "api_key" not in repr(request)
    assert "hello" not in repr(request)


def test_ai_reply_repr_does_not_embed_response_body() -> None:
    reply = AIReply(text="private response", model="fake", provider="fake")
    assert "private response" not in repr(reply)


def test_provider_metadata_allowlist_is_the_single_exported_contract() -> None:
    assert PROVIDER_METADATA_ALLOWED_KEYS == frozenset(
        {
            "discord_trigger",
            "model_alias",
            "request_id",
            "source",
            "surface",
            "trace_id",
            "trigger",
        }
    )
    request = AIRequest(
        prompt="safe",
        guild_id=1,
        user_id=2,
        metadata={"source": "Discordからの入力", "trigger": "direct_mention"},
    )
    assert dict(request.metadata) == {
        "source": "Discordからの入力",
        "trigger": "direct_mention",
    }


@pytest.mark.parametrize(
    "key",
    (
        "api_key",
        "API-Key",
        "api key",
        "client_secret",
        "Access-Token",
        "password",
        "discord_token",
        "Authorization",
        "Proxy-Authorization",
        "Cookie",
        "Set-Cookie",
        "private_key",
        "credentials",
        "ａｐｉ＿ｋｅｙ",
        "ｐａｓｓｗｏｒｄ",
        "Ａuthorization",
        "custom_field",
    ),
)
def test_provider_metadata_rejects_sensitive_confusable_or_unknown_keys(key: str) -> None:
    provider = FakeProvider(local=True)

    with pytest.raises(ValueError, match="metadata"):
        AIRequest(
            prompt="safe",
            guild_id=1,
            user_id=2,
            metadata={key: "notobvioussecretvalue12345"},
        )

    assert provider.calls == 0


@pytest.mark.parametrize(
    "secret_like",
    (
        "Authorization: " + "Bearer " + "eyJ" + ("x" * 24),
        "ａｐｉ＿ｋｅｙ＝notobvioussecretvalue12345",
        "Ａuthorization： Ｂearer " + ("x" * 24),
        "ｐａｓｓｗｏｒｄ＝notobvioussecretvalue12345",
    ),
)
def test_provider_metadata_rejects_secret_like_value_without_logging_it(
    secret_like: str,
) -> None:
    with pytest.raises(ValueError, match="metadata") as caught:
        AIRequest(
            prompt="safe",
            guild_id=1,
            user_id=2,
            metadata={"source": secret_like},
        )

    assert secret_like not in str(caught.value)


@pytest.mark.parametrize(
    ("value", "accepted"),
    (
        ("v" * 512, True),
        ("v" * 513, False),
        (("あ" * 170) + "ab", True),
        (("あ" * 170) + "abc", False),
    ),
)
def test_provider_metadata_value_utf8_byte_limit_is_exact(
    value: str,
    accepted: bool,
) -> None:
    if accepted:
        request = AIRequest(
            prompt="safe",
            guild_id=1,
            user_id=2,
            metadata={"source": value},
        )
        assert request.metadata["source"] == value
        return

    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        AIRequest(
            prompt="safe",
            guild_id=1,
            user_id=2,
            metadata={"source": value},
        )


@pytest.mark.asyncio
async def test_ai_service_logs_neither_request_nor_response_body(caplog: pytest.LogCaptureFixture) -> None:
    provider = FakeProvider(local=True)
    with caplog.at_level(logging.INFO, logger="yonerai_discord.modules.ai.service"):
        await AIService(provider).ask(
            AIRequest(
                prompt="private request body",
                guild_id=123456789012345678,
                user_id=987654321098765432,
            )
        )
    assert "private request body" not in caplog.text
    assert "ok" not in caplog.text
    assert "123456789012345678" not in caplog.text
    assert "987654321098765432" not in caplog.text


@pytest.mark.asyncio
async def test_ai_service_bounds_pending_tasks_and_queue_wait() -> None:
    provider = BlockingProvider()
    service = AIService(provider, concurrency=1, max_pending=1, queue_timeout_seconds=0.05)
    request = AIRequest(prompt="hello", guild_id=1, user_id=2)

    active = asyncio.create_task(service.ask(request))
    await provider.started.wait()
    queued = asyncio.create_task(service.ask(request))
    await asyncio.sleep(0)

    with pytest.raises(AIUnavailableError, match="queue is full"):
        await service.ask(request)
    with pytest.raises(AIUnavailableError, match="queue wait expired"):
        await queued
    assert provider.calls == 1

    provider.release.set()
    assert (await active).text == "ok"
    assert (await service.ask(request)).text == "ok"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_remote_authorization_is_rechecked_after_queue_wait_before_provider_call() -> None:
    provider = BlockingProvider(local=False)
    service = AIService(provider, concurrency=1, max_pending=1, queue_timeout_seconds=1.0)
    request = AIRequest(prompt="private", guild_id=1, user_id=2, boundary=DataBoundary.REMOTE_OPT_IN)
    allowed = True
    queued_checks = 0

    def queued_authorization() -> bool:
        nonlocal queued_checks
        queued_checks += 1
        return allowed

    active = asyncio.create_task(service.ask(request, provider_call_allowed=lambda: True))
    await asyncio.wait_for(provider.started.wait(), timeout=1.0)
    queued = asyncio.create_task(service.ask(request, provider_call_allowed=queued_authorization))
    await asyncio.sleep(0)
    assert queued_checks == 0

    allowed = False
    provider.release.set()
    assert (await active).text == "ok"
    with pytest.raises(PrivacyBoundaryError, match="authorization expired"):
        await queued

    assert queued_checks == 1
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_remote_provider_sink_awaits_fresh_authorization_before_side_effect() -> None:
    class AwaitingSinkProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__(local=False)
            self.at_sink = asyncio.Event()
            self.release_sink = asyncio.Event()

        async def complete_authorized(
            self,
            request: AIRequest,
            provider_sink_verifier: object,
        ) -> AIReply:
            self.at_sink.set()
            await self.release_sink.wait()
            if not await _verify_service_sink_async(
                provider_sink_verifier,
                request=request,
                provider=self,
            ):
                raise ProviderAuthorizationError("fresh authorization expired at fake provider sink")
            self.calls += 1
            return AIReply(text="ok", model="fake", provider="fake")

    provider = AwaitingSinkProvider()
    service = AIService(provider)
    request = AIRequest(prompt="private", guild_id=1, user_id=2, boundary=DataBoundary.REMOTE_OPT_IN)
    member_current = True

    async def fresh_member_authorization() -> bool:
        return member_current

    task = asyncio.create_task(
        service.ask(
            request,
            provider_call_allowed=lambda: True,
            fresh_provider_call_allowed=fresh_member_authorization,
        )
    )
    try:
        await asyncio.wait_for(provider.at_sink.wait(), timeout=1.0)
        member_current = False
        provider.release_sink.set()
        with pytest.raises(PrivacyBoundaryError, match="provider boundary"):
            await task
    finally:
        provider.release_sink.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_remote_provider_without_sink_authorization_support_fails_closed() -> None:
    provider = FakeProvider(local=False)
    request = AIRequest(
        prompt="private",
        guild_id=1,
        user_id=2,
        boundary=DataBoundary.REMOTE_OPT_IN,
    )

    with pytest.raises(PrivacyBoundaryError, match="provider boundary"):
        await AIService(provider).ask(request, provider_call_allowed=lambda: True)

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_provider_sink_authorization_error_remains_a_privacy_error() -> None:
    class RejectingProvider(FakeProvider):
        async def complete_authorized(
            self,
            request: AIRequest,
            provider_sink_verifier: object,
        ) -> AIReply:
            del request, provider_sink_verifier
            raise ProviderAuthorizationError("authorization expired at provider sink")

    provider = RejectingProvider(local=False)
    request = AIRequest(
        prompt="private",
        guild_id=1,
        user_id=2,
        boundary=DataBoundary.REMOTE_OPT_IN,
    )

    with pytest.raises(PrivacyBoundaryError, match="provider boundary"):
        await AIService(provider).ask(request, provider_call_allowed=lambda: True)

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_local_http_compatible_sink_rechecks_authorization_after_async_boundary() -> None:
    class LocalSinkProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__(local=True)
            self.at_sink = asyncio.Event()
            self.release_sink = asyncio.Event()

        async def complete_authorized(
            self,
            request: AIRequest,
            provider_sink_verifier: object,
        ) -> AIReply:
            self.at_sink.set()
            await self.release_sink.wait()
            if not _verify_service_sink(
                provider_sink_verifier,
                request=request,
                provider=self,
            ):
                raise ProviderAuthorizationError("authorization expired at local HTTP sink")
            return await self.complete(request)

    provider = LocalSinkProvider()
    service = AIService(provider, require_authorization=True, clock=lambda: 100.5)
    toolset = BoundedToolSet.issue(
        scope=ToolScopeBinding(1, None, 2),
        intent="conversation",
        complexity="standard",
        snapshot=EMPTY_CAPABILITY_SNAPSHOT,
        provider_catalog_revision=service.provider_catalog_revision,
        web_search=False,
        issued_at=100.0,
    )
    allowed = True
    provider_envelope_sha256 = _formal_provider_envelope_digest(
        prompt="local request",
    )
    context = RuntimeContextBuilder().build(
        ContextBuildInput(
            Scope(1, 2, visibility=MemoryVisibility.USER_PRIVATE),
            "local request",
            (),
            allowed_typed_tools=toolset.effective_tools,
            intent=toolset.intent,
            capability_metadata=capability_metadata_transport(toolset),
            complexity=toolset.complexity,
            bounded_toolset_digest=toolset.digest,
            capability_catalog_revision=toolset.capability_catalog_revision,
            provider_catalog_revision=toolset.provider_catalog_revision,
            provider_envelope_sha256=provider_envelope_sha256,
        )
    )
    task = asyncio.create_task(
        service.ask(
            AIRequest(
                prompt="local request",
                guild_id=1,
                user_id=2,
                provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
                system_prompt=context.prompt,
                context_authorization=context.context_authorization,
                bounded_toolset=toolset,
            ),
            provider_call_allowed=lambda: allowed,
        )
    )
    await asyncio.wait_for(provider.at_sink.wait(), timeout=1.0)
    allowed = False
    provider.release_sink.set()

    with pytest.raises(AIUnavailableError, match="provider boundary"):
        await task
    assert provider.calls == 0
