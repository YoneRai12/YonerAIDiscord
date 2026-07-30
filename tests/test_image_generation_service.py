from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.image_generation.adapter import DiscordImageGenerationAdapter
from yonerai_discord.modules.image_generation.domain import (
    GENERATED_IMAGE_FILENAME,
    GeneratedImage,
    ImageGenerationAuthorizationError,
    ImageGenerationContractError,
    ImageGenerationRequest,
    ImageGenerationUnavailableError,
    image_artifact_request_binding,
)
from yonerai_discord.modules.image_generation.plugin import ImageGenerationPlugin
from yonerai_discord.modules.image_generation.service import ImageGenerationService
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


PNG = b"\x89PNG\r\n\x1a\nvalidated-png-fixture"


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

    def put_png(
        self,
        data: bytes,
        *,
        request_binding: str,
        artifact_id: str | None = None,
    ) -> ArtifactRef:
        self.put_calls += 1
        key = artifact_id or f"image-{self.put_calls}"
        self.values[key] = bytes(data)
        self.bindings[key] = request_binding
        return ArtifactRef(
            artifact_id=key,
            kind=ArtifactKind.IMAGE,
            media_type="image/png",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def read_png(self, ref: ArtifactRef, *, request_binding: str, read_allowed=None) -> bytes:
        self.read_calls += 1
        if read_allowed is not None and read_allowed() is not True:
            raise RuntimeError("read denied")
        if self.bindings.get(ref.artifact_id) != request_binding:
            raise RuntimeError("binding mismatch")
        if self.on_read is not None:
            self.on_read()
        return self.values[ref.artifact_id]


class _Adapter:
    provider_id = "static-image"
    adapter_id = "test.static-image"

    def __init__(self, store: _Store, *, result: str = "valid") -> None:
        self.store = store
        self.result = result
        self.calls = 0
        self.first_artifact: ArtifactRef | None = None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status=HealthStatus.READY,
            checked_at=datetime.now(UTC),
            probed_model_aliases=("image.fast", "image.balanced", "image.quality"),
        )

    async def execute(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        execution_allowed=None,
    ) -> ProviderResult:
        self.calls += 1
        await require_execution_allowed(execution_allowed)
        if self.result == "reuse" and self.first_artifact is not None:
            artifact = self.first_artifact
        else:
            binding = image_artifact_request_binding(
                request,
                provider_id=invocation.provider_id,
                provider_model=invocation.provider_model,
                model_alias=invocation.model_alias,
                quality_tier=invocation.quality_tier,
            )
            artifact = self.store.put_png(PNG, request_binding=binding)
            self.first_artifact = artifact
        artifacts = (artifact,)
        text = ""
        if self.result == "text":
            text = "must be rejected"
        elif self.result == "extra":
            artifacts = (artifact, artifact)
        elif self.result == "audio":
            artifacts = (
                ArtifactRef(
                    "audio-1",
                    ArtifactKind.AUDIO,
                    "audio/wav",
                    size_bytes=len(PNG),
                    sha256=hashlib.sha256(PNG).hexdigest(),
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
    provider_id = "static-image"
    aliases = {
        QualityTier.FAST: "image.fast",
        QualityTier.BALANCED: "image.balanced",
        QualityTier.QUALITY: "image.quality",
    }
    resources = (
        ResourceProfile.remote(max_concurrency=1)
        if kind is ProviderKind.API
        else ResourceProfile(target=ResourceTarget.CPU, max_concurrency=1, system_ram_budget_mb=1_024)
    )
    return ProviderCatalogManifest(
        schema_version=1,
        module_id="test.image-generation",
        capabilities=(
            CapabilityPolicy(
                LogicalCapability.IMAGE_GENERATION,
                True,
                RbacLevel.TRUSTED,
                RiskLevel.HIGH,
                requires_consent=kind is ProviderKind.API,
                audit_required=True,
            ),
        ),
        providers=(
            ProviderManifest(
                provider_id=provider_id,
                kind=kind,
                adapter_id="test.static-image",
                capabilities=(LogicalCapability.IMAGE_GENERATION,),
                resources=resources,
                enabled=True,
                models=tuple(ModelBinding(alias, "static-model", probe_required=False) for alias in aliases.values()),
            ),
        ),
        routes=(
            CapabilityRoute(
                LogicalCapability.IMAGE_GENERATION,
                tuple(TierRoute(tier, (provider_id,), alias) for tier, alias in aliases.items()),
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


def _request(*, request_id: str = "image-100", prompt: str = "青い猫") -> ImageGenerationRequest:
    return ImageGenerationRequest(
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
        service = ImageGenerationService(None, store)
        adapter = None
    else:
        kind = ProviderKind.API if mode == "consent" else ProviderKind.LOCAL
        store = _Store()
        registry = ProviderRegistry(_manifest(kind=kind), audit_sink=None if mode == "audit" else _Audit())
        adapter = _Adapter(store)
        registry.register_adapter(adapter)
        if mode != "health":
            await registry.refresh_health(adapter.provider_id)
        service = ImageGenerationService(registry, store)

    with pytest.raises(ImageGenerationUnavailableError):
        await service.generate(_request(), authorization_current=lambda: True)
    assert store.put_calls == 0
    assert adapter is None or adapter.calls == 0


async def test_local_generation_validates_ref_rereads_bytes_and_is_idempotent() -> None:
    registry, adapter, store = await _ready_runtime()
    service = ImageGenerationService(registry, store)

    first = await service.generate(_request(), authorization_current=lambda: True)
    second = await service.generate(_request(), authorization_current=lambda: True)

    assert isinstance(first, GeneratedImage)
    assert first == second
    assert first.png == PNG
    assert first.artifact.sha256 == hashlib.sha256(PNG).hexdigest()
    assert adapter.calls == 1
    assert store.put_calls == 1
    assert store.read_calls == 2


async def test_provider_cannot_reuse_an_artifact_from_a_different_request() -> None:
    registry, adapter, store = await _ready_runtime(result="reuse")
    service = ImageGenerationService(registry, store)
    await service.generate(_request(request_id="image-100"), authorization_current=lambda: True)

    with pytest.raises(ImageGenerationContractError):
        await service.generate(
            _request(request_id="image-101", prompt="赤い犬"),
            authorization_current=lambda: True,
        )

    assert adapter.calls == 2
    assert store.put_calls == 1


async def test_authorization_revoked_during_artifact_read_rejects_generated_bytes() -> None:
    registry, adapter, store = await _ready_runtime()
    allowed = True

    def revoke() -> None:
        nonlocal allowed
        allowed = False

    store.on_read = revoke
    service = ImageGenerationService(registry, store)

    with pytest.raises(ImageGenerationAuthorizationError):
        await service.generate(_request(), authorization_current=lambda: allowed)

    assert adapter.calls == 1
    assert store.read_calls == 1


async def test_api_generation_requires_user_global_consent_and_rechecks_it() -> None:
    registry, adapter, store = await _ready_runtime(kind=ProviderKind.API)
    consent = True
    service = ImageGenerationService(registry, store, remote_consent_active=lambda _actor: consent)

    generated = await service.generate(_request(), authorization_current=lambda: True)
    assert generated.png == PNG
    assert adapter.calls == 1


@pytest.mark.parametrize("result", ["text", "extra", "audio"])
async def test_provider_result_must_be_exactly_one_png_artifact(result: str) -> None:
    registry, adapter, store = await _ready_runtime(result=result)
    service = ImageGenerationService(registry, store)

    with pytest.raises(ImageGenerationContractError):
        await service.generate(_request(), authorization_current=lambda: True)
    assert adapter.calls == 1
    assert store.read_calls == 0


async def test_authorization_revoked_at_registry_commit_boundary_calls_no_adapter() -> None:
    registry, adapter, store = await _ready_runtime()
    checks = 0

    async def current() -> bool:
        nonlocal checks
        checks += 1
        return checks < 3

    service = ImageGenerationService(registry, store)
    with pytest.raises(ImageGenerationUnavailableError):
        await service.generate(_request(), authorization_current=current)
    assert adapter.calls == 0
    assert store.put_calls == 0


async def test_closing_service_rejects_before_provider_execution() -> None:
    registry, adapter, store = await _ready_runtime()
    service = ImageGenerationService(registry, store)
    service.begin_close()

    with pytest.raises(ImageGenerationAuthorizationError):
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


async def test_discord_adapter_sends_fixed_png_attachment_without_prompt_or_mentions() -> None:
    registry, _, store = await _ready_runtime()
    service = ImageGenerationService(registry, store)
    adapter = DiscordImageGenerationAdapter(service, capability_check=lambda *_: True)
    interaction = _Interaction()

    await adapter.generate(interaction, "secret-looking prompt", "balanced")

    assert interaction.response.deferred
    assert len(interaction.followup.messages) == 1
    sent = interaction.followup.messages[0]
    assert isinstance(sent["file"], discord.File)
    assert sent["file"].filename == GENERATED_IMAGE_FILENAME
    assert sent["embed"].image.url == f"attachment://{GENERATED_IMAGE_FILENAME}"
    assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert "secret-looking prompt" not in repr(sent)


async def test_mention_adapter_reuses_service_and_replies_with_a_fixed_png() -> None:
    registry, provider, store = await _ready_runtime()
    adapter = DiscordImageGenerationAdapter(
        ImageGenerationService(registry, store),
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
    assert sent["file"].filename == GENERATED_IMAGE_FILENAME
    assert sent["embed"].image.url == f"attachment://{GENERATED_IMAGE_FILENAME}"
    assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert "secret-looking prompt" not in repr(sent)


@pytest.mark.parametrize("revocation", ["health", "consent", "capability"])
async def test_discord_send_is_suppressed_when_provider_or_consent_changes(
    revocation: str,
) -> None:
    kind = ProviderKind.API if revocation == "consent" else ProviderKind.LOCAL
    registry, adapter_provider, store = await _ready_runtime(kind=kind)
    consent = True
    capability_allowed = True
    service = ImageGenerationService(
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
            registry._health[adapter_provider.provider_id] = ProviderHealth(
                provider_id=adapter_provider.provider_id,
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
    adapter = DiscordImageGenerationAdapter(
        service,
        capability_check=lambda *_: capability_allowed,
    )
    interaction = _Interaction()

    await adapter.generate(interaction, "猫")

    assert adapter_provider.calls == 1
    assert interaction.followup.messages == []


async def test_discord_adapter_is_guild_only_and_send_denial_has_no_side_effect() -> None:
    registry, adapter_provider, store = await _ready_runtime()
    adapter = DiscordImageGenerationAdapter(
        ImageGenerationService(registry, store),
        capability_check=lambda *_: True,
    )
    interaction = _Interaction(guild_id=None)

    await adapter.generate(interaction, "猫")

    assert len(interaction.response.messages) == 1
    assert adapter_provider.calls == 0
    assert store.put_calls == 0


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def add_command(self, command: object) -> None:
        self.commands[getattr(command, "name")] = command

    def remove_command(self, name: str) -> None:
        self.commands.pop(name, None)


async def test_plugin_without_explicit_provider_binding_starts_but_generation_is_unavailable() -> None:
    bot = SimpleNamespace(tree=_Tree(), is_closing=False)
    plugin = ImageGenerationPlugin()

    await plugin.start(bot)
    assert "image" in bot.tree.commands
    assert plugin.service is not None
    with pytest.raises(ImageGenerationUnavailableError):
        await plugin.service.generate(_request(), authorization_current=lambda: True)
    await plugin.stop()
    assert "image" not in bot.tree.commands


async def test_plugin_capability_check_requires_a_fresh_trusted_member() -> None:
    member = SimpleNamespace(id=30)

    class _Guild:
        id = 10

        async def fetch_member(self, user_id: int):
            assert user_id == member.id
            return member

    class _Guard:
        actor_level = RbacLevel.EVERYONE
        allowed: object = True
        current: object = True

        async def evaluate_fresh_member(self, *_args, **_kwargs):
            return SimpleNamespace(allowed=self.allowed, actor_level=self.actor_level)

        def currently_allowed(self, *_args, **_kwargs):
            return self.current

    guard = _Guard()
    bot = SimpleNamespace(is_closing=False, capability_guard=guard)
    interaction = SimpleNamespace(
        user=member,
        guild_id=10,
        guild=_Guild(),
    )
    check = ImageGenerationPlugin._capability_check(bot)

    assert await check("cap-run-image-generate", interaction) is False
    guard.actor_level = RbacLevel.TRUSTED
    assert await check("cap-run-image-generate", interaction) is True
    guard.allowed = 1
    assert await check("cap-run-image-generate", interaction) is False
    guard.allowed = True
    guard.current = 1
    assert await check("cap-run-image-generate", interaction) is False
    guard.current = True
    interaction.guild_id = True
    assert await check("cap-run-image-generate", interaction) is False
    interaction.guild_id = 11
    assert await check("cap-run-image-generate", interaction) is False
