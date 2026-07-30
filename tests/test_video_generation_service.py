from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest

import yonerai_discord.modules.video_generation.plugin as video_plugin_module
from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.video_generation.adapter import DiscordVideoGenerationAdapter, VideoGroup
from yonerai_discord.modules.video_generation.domain import (
    GENERATED_VIDEO_FILENAME,
    VIDEO_GENERATION_CAPABILITY_ID,
    GeneratedVideo,
    VideoGenerationAuthorizationError,
    VideoGenerationContractError,
    VideoGenerationRequest,
    VideoGenerationUnavailableError,
    video_artifact_request_binding,
)
from yonerai_discord.modules.video_generation.plugin import VideoGenerationPlugin
from yonerai_discord.modules.video_generation.service import VideoGenerationService
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
    TierRoute,
    require_execution_allowed,
)


MP4 = b"validated-mp4-fixture"


@dataclass
class _Audit:
    records: list[AuditRecord] = field(default_factory=list)

    async def append(self, record: AuditRecord) -> None:
        self.records.append(record)


class _Store:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.bindings: dict[str, str] = {}
        self.put_calls = 0
        self.read_calls = 0
        self.on_read = None

    def put_mp4(
        self,
        data: bytes,
        *,
        request_binding: str,
        artifact_id: str | None = None,
    ) -> ArtifactRef:
        self.put_calls += 1
        key = artifact_id or f"video-{self.put_calls}"
        self.values[key] = bytes(data)
        self.bindings[key] = request_binding
        return ArtifactRef(
            artifact_id=key,
            kind=ArtifactKind.VIDEO,
            media_type="video/mp4",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def read_mp4(self, ref: ArtifactRef, *, request_binding: str, read_allowed=None) -> bytes:
        self.read_calls += 1
        if read_allowed is not None and read_allowed() is not True:
            raise RuntimeError("read denied")
        if self.bindings.get(ref.artifact_id) != request_binding:
            raise RuntimeError("binding mismatch")
        if self.on_read is not None:
            self.on_read()
        return self.values[ref.artifact_id]


class _Adapter:
    provider_id = "static-video"
    adapter_id = "test.static-video"

    def __init__(self, store: _Store, *, result: str = "valid") -> None:
        self.store = store
        self.result = result
        self.calls = 0
        self.first_artifact: ArtifactRef | None = None
        self.before_artifact_commit = None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status=HealthStatus.READY,
            checked_at=datetime.now(UTC),
            probed_model_aliases=("video.fast", "video.balanced", "video.quality"),
        )

    async def execute(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        execution_allowed=None,
    ) -> ProviderResult:
        self.calls += 1
        if self.before_artifact_commit is not None:
            self.before_artifact_commit()
        await require_execution_allowed(execution_allowed)
        if self.result == "reuse" and self.first_artifact is not None:
            artifact = self.first_artifact
        else:
            binding = video_artifact_request_binding(
                request,
                provider_id=invocation.provider_id,
                provider_model=invocation.provider_model,
                model_alias=invocation.model_alias,
                quality_tier=invocation.quality_tier,
            )
            artifact = self.store.put_mp4(MP4, request_binding=binding)
            self.first_artifact = artifact
        artifacts = (artifact,)
        text = ""
        if self.result == "text":
            text = "must be rejected"
        elif self.result == "extra":
            artifacts = (artifact, artifact)
        elif self.result == "image":
            artifacts = (
                ArtifactRef(
                    "image-1",
                    ArtifactKind.IMAGE,
                    "image/png",
                    size_bytes=len(MP4),
                    sha256=hashlib.sha256(MP4).hexdigest(),
                ),
            )
        return ProviderResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            provider_model=invocation.provider_model,
            text=text,
            artifacts=artifacts,
        )

    async def close(self) -> None:
        return None


def _manifest(*, kind: ProviderKind = ProviderKind.LOCAL) -> ProviderCatalogManifest:
    aliases = {
        QualityTier.FAST: "video.fast",
        QualityTier.BALANCED: "video.balanced",
        QualityTier.QUALITY: "video.quality",
    }
    resources = (
        ResourceProfile.remote(max_concurrency=1)
        if kind is ProviderKind.API
        else ResourceProfile(target=ResourceTarget.CPU, max_concurrency=1, system_ram_budget_mb=1_024)
    )
    return ProviderCatalogManifest(
        schema_version=1,
        module_id="test.video-generation",
        capabilities=(
            CapabilityPolicy(
                LogicalCapability.VIDEO_GENERATION,
                True,
                RbacLevel.TRUSTED,
                RiskLevel.HIGH,
                requires_consent=kind is ProviderKind.API,
                audit_required=True,
            ),
        ),
        providers=(
            ProviderManifest(
                provider_id="static-video",
                kind=kind,
                adapter_id="test.static-video",
                capabilities=(LogicalCapability.VIDEO_GENERATION,),
                resources=resources,
                enabled=True,
                models=tuple(ModelBinding(alias, "static-model", probe_required=False) for alias in aliases.values()),
            ),
        ),
        routes=(
            CapabilityRoute(
                LogicalCapability.VIDEO_GENERATION,
                tuple(TierRoute(tier, ("static-video",), alias) for tier, alias in aliases.items()),
            ),
        ),
    )


async def _ready_runtime(
    *,
    kind: ProviderKind = ProviderKind.LOCAL,
    result: str = "valid",
    audit: bool = True,
) -> tuple[ProviderRegistry, _Adapter, _Store]:
    store = _Store()
    registry = ProviderRegistry(_manifest(kind=kind), audit_sink=_Audit() if audit else None)
    adapter = _Adapter(store, result=result)
    registry.register_adapter(adapter)
    await registry.refresh_health(adapter.provider_id)
    return registry, adapter, store


def _request(*, request_id: str = "video-100", prompt: str = "青い海") -> VideoGenerationRequest:
    return VideoGenerationRequest(
        request_id=request_id,
        guild_id=10,
        channel_id=20,
        actor_id=30,
        prompt=prompt,
    )


@pytest.mark.parametrize("mode", ["unbound", "health", "audit", "consent"])
async def test_unready_runtime_fails_before_provider_and_artifact(mode: str) -> None:
    if mode == "unbound":
        store = _Store()
        service = VideoGenerationService(None, store)
        adapter = None
    else:
        kind = ProviderKind.API if mode == "consent" else ProviderKind.LOCAL
        store = _Store()
        registry = ProviderRegistry(_manifest(kind=kind), audit_sink=None if mode == "audit" else _Audit())
        adapter = _Adapter(store)
        registry.register_adapter(adapter)
        if mode != "health":
            await registry.refresh_health(adapter.provider_id)
        service = VideoGenerationService(registry, store)

    with pytest.raises(VideoGenerationUnavailableError):
        await service.generate(_request(), authorization_current=lambda: True)
    assert store.put_calls == 0
    assert adapter is None or adapter.calls == 0


async def test_local_generation_is_bound_reread_and_idempotent() -> None:
    registry, adapter, store = await _ready_runtime()
    service = VideoGenerationService(registry, store)

    first = await service.generate(_request(), authorization_current=lambda: True)
    second = await service.generate(_request(), authorization_current=lambda: True)

    assert isinstance(first, GeneratedVideo)
    assert first == second
    assert first.mp4 == MP4
    assert adapter.calls == 1
    assert store.put_calls == 1
    assert store.read_calls == 2


async def test_provider_cannot_reuse_artifact_from_another_request() -> None:
    registry, adapter, store = await _ready_runtime(result="reuse")
    service = VideoGenerationService(registry, store)
    await service.generate(_request(), authorization_current=lambda: True)

    with pytest.raises(VideoGenerationContractError):
        await service.generate(
            _request(request_id="video-101", prompt="赤い空"),
            authorization_current=lambda: True,
        )

    assert adapter.calls == 2
    assert store.put_calls == 1


@pytest.mark.parametrize("result", ["text", "extra", "image"])
async def test_provider_result_must_be_exactly_one_mp4_artifact(result: str) -> None:
    registry, adapter, store = await _ready_runtime(result=result)

    with pytest.raises(VideoGenerationContractError):
        await VideoGenerationService(registry, store).generate(
            _request(),
            authorization_current=lambda: True,
        )

    assert adapter.calls == 1
    assert store.read_calls == 0


async def test_registry_boundary_and_artifact_read_recheck_authorization() -> None:
    registry, adapter, store = await _ready_runtime()
    checks = 0

    async def denied_at_adapter() -> bool:
        nonlocal checks
        checks += 1
        return checks < 3

    with pytest.raises(VideoGenerationUnavailableError):
        await VideoGenerationService(registry, store).generate(
            _request(),
            authorization_current=denied_at_adapter,
        )
    assert adapter.calls == 0
    assert store.put_calls == 0

    registry, adapter, store = await _ready_runtime()
    allowed = True

    def revoke() -> None:
        nonlocal allowed
        allowed = False

    store.on_read = revoke
    with pytest.raises(VideoGenerationAuthorizationError):
        await VideoGenerationService(registry, store).generate(
            _request(),
            authorization_current=lambda: allowed,
        )
    assert adapter.calls == 1


async def test_adapter_rechecks_authorization_immediately_before_artifact_commit() -> None:
    registry, adapter, store = await _ready_runtime()
    allowed = True

    def revoke() -> None:
        nonlocal allowed
        allowed = False

    adapter.before_artifact_commit = revoke
    with pytest.raises(VideoGenerationUnavailableError):
        await VideoGenerationService(registry, store).generate(
            _request(),
            authorization_current=lambda: allowed,
        )

    assert adapter.calls == 1
    assert store.put_calls == 0


async def test_api_generation_requires_user_global_consent() -> None:
    registry, adapter, store = await _ready_runtime(kind=ProviderKind.API)
    consent = False
    service = VideoGenerationService(
        registry,
        store,
        remote_consent_active=lambda _actor: consent,
    )

    with pytest.raises(VideoGenerationUnavailableError):
        await service.generate(_request(), authorization_current=lambda: True)
    assert adapter.calls == 0
    assert store.put_calls == 0

    consent = True
    generated = await service.generate(_request(), authorization_current=lambda: True)
    assert generated.mp4 == MP4
    assert adapter.calls == 1


async def test_api_consent_revoked_before_adapter_call_has_no_side_effect() -> None:
    registry, adapter, store = await _ready_runtime(kind=ProviderKind.API)
    checks = 0

    def consent(_actor: int) -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    service = VideoGenerationService(
        registry,
        store,
        remote_consent_active=consent,
    )

    with pytest.raises(VideoGenerationUnavailableError):
        await service.generate(_request(), authorization_current=lambda: True)

    assert adapter.calls == 0
    assert store.put_calls == 0


class _Response:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.deferred = False

    async def send_message(self, *args, **kwargs) -> None:
        self.messages.append({"args": args, **kwargs})

    async def defer(self, **_kwargs) -> None:
        self.deferred = True


class _Followup:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, *args, **kwargs) -> None:
        self.messages.append({"args": args, **kwargs})


class _Interaction:
    def __init__(self, *, guild_id: int | None = 10) -> None:
        self.id = 777
        self.guild_id = guild_id
        self.channel_id = 20
        self.user = SimpleNamespace(id=30)
        self.response = _Response()
        self.followup = _Followup()


class _Message:
    def __init__(self) -> None:
        self.id = 778
        self.guild = SimpleNamespace(id=10)
        self.channel = SimpleNamespace(id=20)
        self.author = SimpleNamespace(id=30)
        self.replies: list[dict[str, Any]] = []

    async def reply(self, *args: Any, **kwargs: Any) -> None:
        self.replies.append({"args": args, **kwargs})


async def test_discord_adapter_sends_fixed_private_attachment_without_prompt() -> None:
    registry, _, store = await _ready_runtime()
    adapter = DiscordVideoGenerationAdapter(
        VideoGenerationService(registry, store),
        capability_check=lambda *_: True,
    )
    interaction = _Interaction()

    await adapter.generate(interaction, "secret-looking prompt")

    assert interaction.response.deferred
    assert len(interaction.followup.messages) == 1
    sent = interaction.followup.messages[0]
    assert isinstance(sent["file"], discord.File)
    assert sent["file"].filename == GENERATED_VIDEO_FILENAME
    assert sent["embed"].description == (f"[構造検証済みMP4を添付しました。](attachment://{GENERATED_VIDEO_FILENAME})")
    assert sent["ephemeral"] is True
    assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert "secret-looking prompt" not in repr(sent)


async def test_mention_adapter_reuses_service_and_replies_with_a_fixed_mp4() -> None:
    registry, provider, store = await _ready_runtime()
    adapter = DiscordVideoGenerationAdapter(
        VideoGenerationService(registry, store),
        capability_check=lambda *_: True,
    )
    message = _Message()

    delivered = await adapter.generate_for_message(
        message,
        prompt="secret-looking prompt",
        authorization_current=lambda: True,
    )

    assert delivered is True
    assert provider.calls == 1
    assert len(message.replies) == 1
    sent = message.replies[0]
    assert isinstance(sent["file"], discord.File)
    assert sent["file"].filename == GENERATED_VIDEO_FILENAME
    assert sent["embed"].description == (f"[構造検証済みMP4を添付しました。](attachment://{GENERATED_VIDEO_FILENAME})")
    assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert "secret-looking prompt" not in repr(sent)


@pytest.mark.parametrize("revocation", ["health", "consent", "capability"])
async def test_discord_send_is_suppressed_when_current_state_changes(revocation: str) -> None:
    kind = ProviderKind.API if revocation == "consent" else ProviderKind.LOCAL
    registry, provider, store = await _ready_runtime(kind=kind)
    consent = True
    capability_allowed = True
    service = VideoGenerationService(
        registry,
        store,
        remote_consent_active=lambda _actor: consent,
    )
    original_delivery_current = service.delivery_current

    async def revoke_before_delivery(request, generated, *, authorization_current):
        nonlocal capability_allowed, consent
        if revocation == "consent":
            consent = False
        elif revocation == "capability":
            capability_allowed = False
        else:
            registry._health[provider.provider_id] = ProviderHealth(
                provider_id=provider.provider_id,
                status=HealthStatus.UNAVAILABLE,
                checked_at=datetime.now(UTC),
                detail_code="revoked",
            )
        return await original_delivery_current(
            request,
            generated,
            authorization_current=authorization_current,
        )

    service.delivery_current = revoke_before_delivery
    adapter = DiscordVideoGenerationAdapter(
        service,
        capability_check=lambda *_: capability_allowed,
    )
    interaction = _Interaction()

    await adapter.generate(interaction, "海")

    assert provider.calls == 1
    assert interaction.followup.messages == []


async def test_discord_adapter_is_guild_only() -> None:
    registry, provider, store = await _ready_runtime()
    adapter = DiscordVideoGenerationAdapter(
        VideoGenerationService(registry, store),
        capability_check=lambda *_: True,
    )
    interaction = _Interaction(guild_id=None)

    await adapter.generate(interaction, "海")

    assert len(interaction.response.messages) == 1
    assert provider.calls == 0
    assert store.put_calls == 0
    group = VideoGroup(adapter)
    assert group.guild_only is True
    assert group.get_command("generate").parent is group


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def add_command(self, command: object) -> None:
        self.commands[getattr(command, "name")] = command

    def remove_command(self, name: str) -> None:
        self.commands.pop(name, None)


async def test_plugin_unbound_is_safe_and_fresh_trusted_member_is_required() -> None:
    bot = SimpleNamespace(tree=_Tree(), is_closing=False)
    plugin = VideoGenerationPlugin()
    await plugin.start(bot)
    assert "video" in bot.tree.commands
    assert bot.runtime_capability_readiness[VIDEO_GENERATION_CAPABILITY_ID] is False
    with pytest.raises(VideoGenerationUnavailableError):
        await plugin.service.generate(_request(), authorization_current=lambda: True)
    await plugin.stop()
    assert "video" not in bot.tree.commands
    assert VIDEO_GENERATION_CAPABILITY_ID not in bot.runtime_capability_readiness

    member = SimpleNamespace(id=30)

    class _Guild:
        id = 10

        async def fetch_member(self, user_id: int):
            assert user_id == member.id
            return member

    class _Guard:
        actor_level = RbacLevel.EVERYONE
        allowed: object = True
        current_value: object = True

        async def evaluate_fresh_member(self, *_args, **_kwargs):
            return SimpleNamespace(allowed=self.allowed, actor_level=self.actor_level)

        def currently_allowed(self, *_args, **_kwargs) -> object:
            return self.current_value

    guard = _Guard()
    check = VideoGenerationPlugin._capability_check(SimpleNamespace(is_closing=False, capability_guard=guard))
    interaction = SimpleNamespace(user=member, guild_id=10, guild=_Guild())
    assert await check("cap-run-video-generate", interaction) is False
    guard.actor_level = RbacLevel.TRUSTED
    assert await check("cap-run-video-generate", interaction) is True
    guard.allowed = 1
    assert await check("cap-run-video-generate", interaction) is False
    guard.allowed = True
    guard.current_value = 1
    assert await check("cap-run-video-generate", interaction) is False
    guard.current_value = True
    assert (
        await check(
            "cap-run-video-generate",
            SimpleNamespace(user=SimpleNamespace(id=True), guild_id=10, guild=_Guild()),
        )
        is False
    )
    assert (
        await check(
            "cap-run-video-generate",
            SimpleNamespace(user=member, guild_id=11, guild=_Guild()),
        )
        is False
    )


async def test_plugin_start_failure_removes_command_bindings_and_readiness(monkeypatch) -> None:
    bot = SimpleNamespace(tree=_Tree(), is_closing=False)
    plugin = VideoGenerationPlugin()

    def fail_readiness(target: object, capability_id: str, _probe: object) -> None:
        target.runtime_capability_readiness = {capability_id: False}
        raise RuntimeError("readiness failed")

    monkeypatch.setattr(video_plugin_module, "publish_runtime_readiness_probe", fail_readiness)
    with pytest.raises(RuntimeError, match="readiness failed"):
        await plugin.start(bot)

    assert bot.tree.commands == {}
    assert bot.runtime_capability_readiness == {}
    assert not hasattr(bot, "video_generation_service")
    assert not hasattr(bot, "video_generation_adapter")
    assert plugin.service is None
    assert plugin.adapter is None
    assert plugin._bot is None


async def test_plugin_stop_failure_still_withdraws_runtime_and_bindings(monkeypatch) -> None:
    bot = SimpleNamespace(tree=_Tree(), is_closing=False)
    plugin = VideoGenerationPlugin()
    await plugin.start(bot)
    service = plugin.service

    def fail_close(_adapter: DiscordVideoGenerationAdapter) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(DiscordVideoGenerationAdapter, "begin_close", fail_close)
    with pytest.raises(RuntimeError, match="close failed"):
        await plugin.stop()

    assert bot.tree.commands == {}
    assert bot.runtime_capability_readiness == {}
    assert not hasattr(bot, "video_generation_service")
    assert not hasattr(bot, "video_generation_adapter")
    assert plugin.service is None
    assert plugin.adapter is None
    assert plugin._bot is None
    assert service is not None and service._closing is True


async def test_plugin_failed_duplicate_install_preserves_existing_command() -> None:
    existing = object()

    class _RejectingTree(_Tree):
        def add_command(self, command: object) -> None:
            name = getattr(command, "name")
            if name in self.commands:
                raise RuntimeError("already registered")
            super().add_command(command)

    tree = _RejectingTree()
    tree.commands["video"] = existing
    bot = SimpleNamespace(tree=tree, is_closing=False)
    plugin = VideoGenerationPlugin()

    with pytest.raises(RuntimeError, match="already registered"):
        await plugin.start(bot)

    assert tree.commands == {"video": existing}
    assert plugin.service is None
    assert plugin.adapter is None
    assert plugin._bot is None


async def test_plugin_adapter_construction_failure_closes_service(monkeypatch) -> None:
    closed: list[VideoGenerationService] = []
    original_close = VideoGenerationService.begin_close

    def record_close(service: VideoGenerationService) -> None:
        original_close(service)
        closed.append(service)

    def fail_adapter(*_args, **_kwargs):
        raise RuntimeError("adapter failed")

    monkeypatch.setattr(VideoGenerationService, "begin_close", record_close)
    monkeypatch.setattr(video_plugin_module, "DiscordVideoGenerationAdapter", fail_adapter)
    plugin = VideoGenerationPlugin()

    with pytest.raises(RuntimeError, match="adapter failed"):
        await plugin.start(SimpleNamespace(tree=_Tree(), is_closing=False))

    assert len(closed) == 1
    assert closed[0]._closing is True
    assert plugin.service is None
    assert plugin.adapter is None
    assert plugin._bot is None
