from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest

import yonerai_discord.modules.music_generation.plugin as music_plugin_module
from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.music_generation.adapter import DiscordMusicGenerationAdapter, MusicGenGroup
from yonerai_discord.modules.music_generation.domain import (
    GENERATED_MUSIC_FILENAME,
    MUSIC_GENERATION_CAPABILITY_ID,
    MUSIC_GENERATION_MODULE_ID,
    GeneratedMusic,
    MusicGenerationAuthorizationError,
    MusicGenerationContractError,
    MusicGenerationPolicyError,
    MusicGenerationRequest,
    MusicGenerationUnavailableError,
    music_artifact_request_binding,
)
from yonerai_discord.modules.music_generation.plugin import MusicGenerationPlugin
from yonerai_discord.modules.music_generation.service import MusicGenerationService
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    AuditRecord,
    CapabilityPolicy,
    CapabilityRoute,
    HealthStatus,
    LogicalCapability,
    MediaGenerationInput,
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


def _wav(*, seconds: int = 1, rate: int = 44_100, channels: int = 1) -> bytes:
    block_align = channels * 2
    pcm = b"\0" * (seconds * rate * block_align)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block_align, block_align, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


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

    def put_wav(self, data: bytes, *, request_binding: str) -> ArtifactRef:
        self.put_calls += 1
        artifact_id = f"aud-test-{self.put_calls}"
        self.values[artifact_id] = bytes(data)
        self.bindings[artifact_id] = request_binding
        return ArtifactRef(
            artifact_id,
            ArtifactKind.AUDIO,
            "audio/wav",
            len(data),
            hashlib.sha256(data).hexdigest(),
        )

    def read_wav(self, ref: ArtifactRef, *, request_binding: str, read_allowed=None) -> bytes:
        self.read_calls += 1
        if read_allowed is not None and read_allowed() is not True:
            raise RuntimeError("read denied")
        if self.bindings.get(ref.artifact_id) != request_binding:
            raise RuntimeError("binding mismatch")
        if self.on_read is not None:
            self.on_read()
        return self.values[ref.artifact_id]


class _Adapter:
    provider_id = "static-music"
    adapter_id = "test.static-music"

    def __init__(
        self,
        store: _Store,
        *,
        result: str = "valid",
        duration_override: int | None = None,
    ) -> None:
        self.store = store
        self.result = result
        self.duration_override = duration_override
        self.calls = 0
        self.first_artifact: ArtifactRef | None = None
        self.before_artifact_commit = None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status=HealthStatus.READY,
            checked_at=datetime.now(UTC),
            probed_model_aliases=("music.fast", "music.balanced", "music.quality"),
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
        payload = request.payload
        assert isinstance(payload, MediaGenerationInput)
        duration = int(payload.duration_seconds or 0)
        if self.result == "reuse" and self.first_artifact is not None:
            artifact = self.first_artifact
        else:
            binding = music_artifact_request_binding(
                request,
                provider_id=invocation.provider_id,
                provider_model=invocation.provider_model,
                model_alias=invocation.model_alias,
                quality_tier=invocation.quality_tier,
            )
            artifact = self.store.put_wav(
                _wav(seconds=self.duration_override or duration),
                request_binding=binding,
            )
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
                    artifact.size_bytes,
                    artifact.sha256,
                ),
            )
        return ProviderResult(
            request.request_id,
            self.provider_id,
            invocation.provider_model,
            text=text,
            artifacts=artifacts,
        )

    async def close(self) -> None:
        return None


def _manifest(*, kind: ProviderKind = ProviderKind.LOCAL) -> ProviderCatalogManifest:
    aliases = {
        QualityTier.FAST: "music.fast",
        QualityTier.BALANCED: "music.balanced",
        QualityTier.QUALITY: "music.quality",
    }
    resources = (
        ResourceProfile.remote(max_concurrency=1)
        if kind is ProviderKind.API
        else ResourceProfile(target=ResourceTarget.CPU, max_concurrency=1, system_ram_budget_mb=1_024)
    )
    return ProviderCatalogManifest(
        schema_version=1,
        module_id="test.music-generation",
        capabilities=(
            CapabilityPolicy(
                LogicalCapability.MUSIC_GENERATION,
                True,
                RbacLevel.TRUSTED,
                RiskLevel.HIGH,
                requires_consent=kind is ProviderKind.API,
                audit_required=True,
            ),
        ),
        providers=(
            ProviderManifest(
                "static-music",
                kind,
                "test.static-music",
                (LogicalCapability.MUSIC_GENERATION,),
                resources,
                enabled=True,
                models=tuple(ModelBinding(alias, "static-model", probe_required=False) for alias in aliases.values()),
            ),
        ),
        routes=(
            CapabilityRoute(
                LogicalCapability.MUSIC_GENERATION,
                tuple(TierRoute(tier, ("static-music",), alias) for tier, alias in aliases.items()),
            ),
        ),
    )


async def _ready_runtime(
    *,
    kind: ProviderKind = ProviderKind.LOCAL,
    result: str = "valid",
    audit: bool = True,
    duration_override: int | None = None,
) -> tuple[ProviderRegistry, _Adapter, _Store]:
    store = _Store()
    registry = ProviderRegistry(_manifest(kind=kind), audit_sink=_Audit() if audit else None)
    adapter = _Adapter(store, result=result, duration_override=duration_override)
    registry.register_adapter(adapter)
    await registry.refresh_health(adapter.provider_id)
    return registry, adapter, store


def _request(
    *,
    request_id: str = "music-100",
    prompt: str = "warm ambient synth",
    duration_seconds: int = 1,
    guild_id: int = 10,
    channel_id: int = 20,
    actor_id: int = 30,
) -> MusicGenerationRequest:
    return MusicGenerationRequest(
        request_id,
        guild_id,
        channel_id,
        actor_id,
        prompt,
        duration_seconds=duration_seconds,
        rights_confirmed=True,
    )


def test_request_defaults_rights_markers_and_scope_binding() -> None:
    default = MusicGenerationRequest("music-default", 10, 20, 30, "calm piano", rights_confirmed=True)
    assert default.duration_seconds == 15
    assert MUSIC_GENERATION_MODULE_ID == "media.music-generation"
    assert MUSIC_GENERATION_CAPABILITY_ID == "cap-run-music-generate"
    assert "calm piano" not in repr(default)
    with pytest.raises(MusicGenerationPolicyError, match="rights_confirmed"):
        MusicGenerationRequest("music-1", 10, 20, 30, "calm piano")
    for prompt in (
        "with lyrics",
        "歌詞付き",
        "歌声サンプル",
        "女性ボイス入りの曲",
        "女性が歌っている曲",
        "声クローン",
        "カバー曲",
        "リミックス",
        "既存曲を編曲した音楽",
        "参照音声",
    ):
        with pytest.raises(MusicGenerationPolicyError, match="original instrumental"):
            MusicGenerationRequest("music-1", 10, 20, 30, prompt, rights_confirmed=True)
    assert (
        _request().fingerprint
        != MusicGenerationRequest(
            "music-100",
            11,
            20,
            30,
            "warm ambient synth",
            duration_seconds=1,
            rights_confirmed=True,
        ).fingerprint
    )


@pytest.mark.parametrize("mode", ["unbound", "health", "audit", "consent"])
async def test_unready_runtime_fails_before_provider_and_artifact(mode: str) -> None:
    if mode == "unbound":
        store = _Store()
        service = MusicGenerationService(None, store)
        adapter = None
    else:
        kind = ProviderKind.API if mode == "consent" else ProviderKind.LOCAL
        store = _Store()
        registry = ProviderRegistry(_manifest(kind=kind), audit_sink=None if mode == "audit" else _Audit())
        adapter = _Adapter(store)
        registry.register_adapter(adapter)
        if mode != "health":
            await registry.refresh_health(adapter.provider_id)
        service = MusicGenerationService(registry, store)

    with pytest.raises(MusicGenerationUnavailableError):
        await service.generate(_request(), authorization_current=lambda: True)
    assert store.put_calls == 0
    assert adapter is None or adapter.calls == 0


async def test_local_generation_is_bound_exact_duration_and_idempotent() -> None:
    registry, adapter, store = await _ready_runtime()
    service = MusicGenerationService(registry, store)

    first = await service.generate(_request(), authorization_current=lambda: True)
    second = await service.generate(_request(), authorization_current=lambda: True)

    assert isinstance(first, GeneratedMusic)
    assert first == second
    assert adapter.calls == 1
    assert store.put_calls == 1
    assert store.read_calls == 2
    assert len(first.wav) == 44 + 44_100 * 2

    scoped = await service.generate(
        _request(request_id="music-scope", guild_id=11, channel_id=21),
        authorization_current=lambda: True,
    )
    assert scoped.wav
    assert adapter.calls == 2
    assert store.put_calls == 2

    mismatch_registry, mismatch_adapter, mismatch_store = await _ready_runtime(duration_override=2)
    with pytest.raises(MusicGenerationContractError, match="duration"):
        await MusicGenerationService(mismatch_registry, mismatch_store).generate(
            _request(),
            authorization_current=lambda: True,
        )
    assert mismatch_adapter.calls == 1


async def test_cross_request_replay_and_invalid_provider_results_are_rejected() -> None:
    registry, adapter, store = await _ready_runtime(result="reuse")
    service = MusicGenerationService(registry, store)
    await service.generate(_request(), authorization_current=lambda: True)
    with pytest.raises(MusicGenerationContractError):
        await service.generate(
            _request(request_id="music-101", prompt="other original instrumental"),
            authorization_current=lambda: True,
        )
    assert adapter.calls == 2
    assert store.put_calls == 1

    for result in ("text", "extra", "image"):
        registry, adapter, store = await _ready_runtime(result=result)
        with pytest.raises(MusicGenerationContractError):
            await MusicGenerationService(registry, store).generate(
                _request(request_id=f"music-{result}"),
                authorization_current=lambda: True,
            )
        assert adapter.calls == 1
        assert store.read_calls == 0


async def test_adapter_commit_and_artifact_read_recheck_authorization() -> None:
    registry, adapter, store = await _ready_runtime()
    allowed = True

    def revoke_before_commit() -> None:
        nonlocal allowed
        allowed = False

    adapter.before_artifact_commit = revoke_before_commit
    with pytest.raises(MusicGenerationUnavailableError):
        await MusicGenerationService(registry, store).generate(
            _request(),
            authorization_current=lambda: allowed,
        )
    assert store.put_calls == 0

    registry, adapter, store = await _ready_runtime()
    allowed = True

    def revoke_during_read() -> None:
        nonlocal allowed
        allowed = False

    store.on_read = revoke_during_read
    with pytest.raises(MusicGenerationAuthorizationError):
        await MusicGenerationService(registry, store).generate(
            _request(),
            authorization_current=lambda: allowed,
        )
    assert adapter.calls == 1


async def test_api_requires_user_global_consent_and_rechecks_before_adapter() -> None:
    registry, adapter, store = await _ready_runtime(kind=ProviderKind.API)
    consent = False
    service = MusicGenerationService(registry, store, remote_consent_active=lambda _actor: consent)
    with pytest.raises(MusicGenerationUnavailableError):
        await service.generate(_request(), authorization_current=lambda: True)
    assert adapter.calls == 0

    consent = True
    assert (await service.generate(_request(), authorization_current=lambda: True)).wav
    assert adapter.calls == 1

    registry, adapter, store = await _ready_runtime(kind=ProviderKind.API)
    checks = 0

    def one_check(_actor: int) -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    with pytest.raises(MusicGenerationUnavailableError):
        await MusicGenerationService(
            registry,
            store,
            remote_consent_active=one_check,
        ).generate(_request(), authorization_current=lambda: True)
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


async def test_discord_adapter_sends_fixed_private_wav_without_prompt() -> None:
    registry, _, store = await _ready_runtime()
    adapter = DiscordMusicGenerationAdapter(
        MusicGenerationService(registry, store),
        capability_check=lambda *_: True,
    )
    interaction = _Interaction()

    await adapter.generate(
        interaction,
        "secret-looking original instrumental",
        duration_seconds=1,
        rights_confirmed=True,
    )

    assert interaction.response.deferred
    assert len(interaction.followup.messages) == 1
    sent = interaction.followup.messages[0]
    assert isinstance(sent["file"], discord.File)
    assert sent["file"].filename == GENERATED_MUSIC_FILENAME
    assert sent["ephemeral"] is True
    assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert "構造検証済みPCM16 WAV" in sent["embed"].description
    assert "secret-looking" not in repr(sent)


async def test_mention_adapter_reuses_service_with_confirmed_rights_and_replies_with_fixed_wav() -> None:
    registry, provider, store = await _ready_runtime()
    adapter = DiscordMusicGenerationAdapter(
        MusicGenerationService(registry, store),
        capability_check=lambda *_: True,
    )
    message = _Message()

    delivered = await adapter.generate_for_message(
        message,
        prompt="secret-looking original instrumental",
        authorization_current=lambda: True,
    )

    assert delivered is True
    assert provider.calls == 1
    assert len(message.replies) == 1
    sent = message.replies[0]
    assert isinstance(sent["file"], discord.File)
    assert sent["file"].filename == GENERATED_MUSIC_FILENAME
    assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert "secret-looking" not in repr(sent)


@pytest.mark.parametrize("revocation", ["health", "consent", "capability"])
async def test_discord_send_is_suppressed_when_current_state_changes(revocation: str) -> None:
    kind = ProviderKind.API if revocation == "consent" else ProviderKind.LOCAL
    registry, provider, store = await _ready_runtime(kind=kind)
    consent = True
    capability_allowed = True
    service = MusicGenerationService(registry, store, remote_consent_active=lambda _actor: consent)
    original = service.delivery_current

    async def revoke_before_delivery(request, generated, *, authorization_current):
        nonlocal capability_allowed, consent
        if revocation == "consent":
            consent = False
        elif revocation == "capability":
            capability_allowed = False
        else:
            registry._health[provider.provider_id] = ProviderHealth(
                provider.provider_id,
                HealthStatus.UNAVAILABLE,
                datetime.now(UTC),
                detail_code="revoked",
            )
        return await original(request, generated, authorization_current=authorization_current)

    service.delivery_current = revoke_before_delivery
    adapter = DiscordMusicGenerationAdapter(service, capability_check=lambda *_: capability_allowed)
    interaction = _Interaction()
    await adapter.generate(interaction, "original instrumental", True, duration_seconds=1)

    assert provider.calls == 1
    assert interaction.followup.messages == []


async def test_discord_checks_route_and_consent_after_the_last_capability_await() -> None:
    registry, provider, store = await _ready_runtime(kind=ProviderKind.API)
    capability_allowed = True
    consent = True
    generation_done = False
    service = MusicGenerationService(registry, store, remote_consent_active=lambda _actor: consent)
    original_generate = service.generate
    original_delivery = service.delivery_current

    async def mark_generation_done(*args, **kwargs):
        nonlocal generation_done
        generated = await original_generate(*args, **kwargs)
        generation_done = True
        return generated

    async def capability_check(*_args) -> bool:
        nonlocal consent
        if generation_done:
            consent = False
        return capability_allowed

    async def current_delivery(*args, **kwargs) -> bool:
        return await original_delivery(*args, **kwargs)

    service.generate = mark_generation_done
    service.delivery_current = current_delivery
    adapter = DiscordMusicGenerationAdapter(service, capability_check=capability_check)
    interaction = _Interaction()

    await adapter.generate(interaction, "original instrumental", True, duration_seconds=1)

    assert provider.calls == 1
    assert consent is False
    assert interaction.followup.messages == []


async def test_group_is_guild_only_and_rights_required_with_default_15_seconds() -> None:
    registry, provider, store = await _ready_runtime()
    adapter = DiscordMusicGenerationAdapter(
        MusicGenerationService(registry, store),
        capability_check=lambda *_: True,
    )
    interaction = _Interaction(guild_id=None)
    await adapter.generate(interaction, "original instrumental", rights_confirmed=True)

    assert interaction.response.messages
    assert provider.calls == 0
    assert store.put_calls == 0
    group = MusicGenGroup(adapter)
    command = group.get_command("generate")
    assert group.guild_only is True
    assert command is not None and command.parent is group
    parameters = {parameter.name: parameter for parameter in command.parameters}
    assert parameters["rights_confirmed"].required is True
    assert parameters["duration_seconds"].default == 15


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def add_command(self, command: object) -> None:
        self.commands[getattr(command, "name")] = command

    def remove_command(self, name: str) -> None:
        self.commands.pop(name, None)


async def test_plugin_unbound_requires_fresh_trusted_member_and_does_not_touch_playback() -> None:
    existing_music = object()
    existing_voice = object()
    bot = SimpleNamespace(
        tree=_Tree(),
        is_closing=False,
        music_service=existing_music,
        voice_service=existing_voice,
    )
    plugin = MusicGenerationPlugin()
    await plugin.start(bot)
    assert "musicgen" in bot.tree.commands
    assert bot.runtime_capability_readiness[MUSIC_GENERATION_CAPABILITY_ID] is False
    assert bot.music_service is existing_music
    assert bot.voice_service is existing_voice
    with pytest.raises(MusicGenerationUnavailableError):
        await plugin.service.generate(_request(), authorization_current=lambda: True)
    await plugin.stop()
    assert "musicgen" not in bot.tree.commands
    assert MUSIC_GENERATION_CAPABILITY_ID not in bot.runtime_capability_readiness
    assert bot.music_service is existing_music
    assert bot.voice_service is existing_voice

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
    check = MusicGenerationPlugin._capability_check(SimpleNamespace(is_closing=False, capability_guard=guard))
    interaction = SimpleNamespace(user=member, guild_id=10, guild=_Guild())
    assert await check(MUSIC_GENERATION_CAPABILITY_ID, interaction) is False
    guard.actor_level = RbacLevel.TRUSTED
    assert await check(MUSIC_GENERATION_CAPABILITY_ID, interaction) is True
    guard.allowed = 1
    assert await check(MUSIC_GENERATION_CAPABILITY_ID, interaction) is False
    guard.allowed = True
    guard.current_value = 1
    assert await check(MUSIC_GENERATION_CAPABILITY_ID, interaction) is False
    guard.current_value = True
    assert (
        await check(
            MUSIC_GENERATION_CAPABILITY_ID,
            SimpleNamespace(user=SimpleNamespace(id=True), guild_id=10, guild=_Guild()),
        )
        is False
    )
    assert (
        await check(
            MUSIC_GENERATION_CAPABILITY_ID,
            SimpleNamespace(user=member, guild_id=11, guild=_Guild()),
        )
        is False
    )


async def test_plugin_start_failure_removes_command_bindings_and_readiness(monkeypatch) -> None:
    bot = SimpleNamespace(tree=_Tree(), is_closing=False)
    plugin = MusicGenerationPlugin()

    def fail_readiness(target: object, capability_id: str, _probe: object) -> None:
        target.runtime_capability_readiness = {capability_id: False}
        raise RuntimeError("readiness failed")

    monkeypatch.setattr(music_plugin_module, "publish_runtime_readiness_probe", fail_readiness)
    with pytest.raises(RuntimeError, match="readiness failed"):
        await plugin.start(bot)

    assert bot.tree.commands == {}
    assert bot.runtime_capability_readiness == {}
    assert not hasattr(bot, "music_generation_service")
    assert not hasattr(bot, "music_generation_adapter")
    assert plugin.service is None
    assert plugin.adapter is None
    assert plugin._bot is None


async def test_plugin_stop_failure_still_withdraws_runtime_and_bindings(monkeypatch) -> None:
    bot = SimpleNamespace(tree=_Tree(), is_closing=False)
    plugin = MusicGenerationPlugin()
    await plugin.start(bot)
    service = plugin.service

    def fail_close(_adapter: DiscordMusicGenerationAdapter) -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(DiscordMusicGenerationAdapter, "begin_close", fail_close)
    with pytest.raises(RuntimeError, match="close failed"):
        await plugin.stop()

    assert bot.tree.commands == {}
    assert bot.runtime_capability_readiness == {}
    assert not hasattr(bot, "music_generation_service")
    assert not hasattr(bot, "music_generation_adapter")
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
    tree.commands["musicgen"] = existing
    bot = SimpleNamespace(tree=tree, is_closing=False)
    plugin = MusicGenerationPlugin()

    with pytest.raises(RuntimeError, match="already registered"):
        await plugin.start(bot)

    assert tree.commands == {"musicgen": existing}
    assert plugin.service is None
    assert plugin.adapter is None
    assert plugin._bot is None


async def test_plugin_adapter_construction_failure_closes_service(monkeypatch) -> None:
    closed: list[MusicGenerationService] = []
    original_close = MusicGenerationService.begin_close

    def record_close(service: MusicGenerationService) -> None:
        original_close(service)
        closed.append(service)

    def fail_adapter(*_args, **_kwargs):
        raise RuntimeError("adapter failed")

    monkeypatch.setattr(MusicGenerationService, "begin_close", record_close)
    monkeypatch.setattr(music_plugin_module, "DiscordMusicGenerationAdapter", fail_adapter)
    plugin = MusicGenerationPlugin()

    with pytest.raises(RuntimeError, match="adapter failed"):
        await plugin.start(SimpleNamespace(tree=_Tree(), is_closing=False))

    assert len(closed) == 1
    assert closed[0]._closing is True
    assert plugin.service is None
    assert plugin.adapter is None
    assert plugin._bot is None
