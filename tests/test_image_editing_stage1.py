from __future__ import annotations

import binascii
import hashlib
import struct
import zlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.image_editing import (
    EDITED_IMAGE_FILENAME,
    IMAGE_EDITING_CAPABILITY_ID,
    IMAGE_EDITING_MODULE_ID,
    IMAGE_EDITING_PLUGIN_NAME,
    EditedImage,
    ImageEditSource,
    ImageEditingAuthorizationError,
    ImageEditingContractError,
    ImageEditingDelivery,
    ImageEditingPlugin,
    ImageEditingRequest,
    ImageEditingService,
    ImageEditingUnavailableError,
    image_edit_output_binding,
    setup,
)
from yonerai_discord.modules.image_generation.artifacts import (
    ImageArtifactStore,
    canonicalize_png,
)
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    AuditRecord,
    CapabilityPolicy,
    CapabilityRoute,
    HealthStatus,
    ImageEditingInput,
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


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(
    *,
    fill: int = 0,
    ancillary: tuple[tuple[bytes, bytes], ...] = (),
) -> bytes:
    width = height = 64
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\0" + bytes([fill]) * (width * 4) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + b"".join(_chunk(kind, payload) for kind, payload in ancillary)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _chunk_types(data: bytes) -> tuple[bytes, ...]:
    cursor = 8
    result: list[bytes] = []
    while cursor < len(data):
        length = struct.unpack_from(">I", data, cursor)[0]
        result.append(data[cursor + 4 : cursor + 8])
        cursor += length + 12
    return tuple(result)


@dataclass
class _Audit:
    records: list[AuditRecord] = field(default_factory=list)

    async def append(self, record: AuditRecord) -> None:
        self.records.append(record)


class _Store:
    def __init__(self, root: Path, *, max_artifacts: int = 64) -> None:
        root.mkdir(parents=True)
        self.inner = ImageArtifactStore(root, max_artifacts=max_artifacts)
        self.put_calls = 0
        self.read_calls = 0
        self.on_read = None

    def put_png(self, data: bytes, *, request_binding: str, artifact_id: str | None = None) -> ArtifactRef:
        self.put_calls += 1
        return self.inner.put_png(
            data,
            request_binding=request_binding,
            artifact_id=artifact_id,
        )

    def read_png(self, ref: ArtifactRef, *, request_binding: str, read_allowed=None) -> bytes:
        self.read_calls += 1
        data = self.inner.read_png(
            ref,
            request_binding=request_binding,
            read_allowed=read_allowed,
        )
        if self.on_read is not None:
            self.on_read(self.read_calls)
        return data

    def protect_png(self, ref: ArtifactRef, *, request_binding: str):
        return self.inner.protect_png(ref, request_binding=request_binding)


class _Provider:
    provider_id = "static-image-edit"
    adapter_id = "test.static-image-edit"

    def __init__(
        self,
        store: _Store,
        *,
        mode: str = "valid",
        before_commit=None,
    ) -> None:
        self.store = store
        self.mode = mode
        self.before_commit = before_commit
        self.calls = 0

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status=HealthStatus.READY,
            checked_at=datetime.now(UTC),
            probed_model_aliases=(
                "image.edit.fast",
                "image.edit.balanced",
                "image.edit.quality",
            ),
        )

    async def execute(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        execution_allowed=None,
    ) -> ProviderResult:
        self.calls += 1
        assert request.capability is LogicalCapability.IMAGE_EDITING
        assert isinstance(request.payload, ImageEditingInput)
        assert len(request.input_artifacts) == 1
        if self.before_commit is not None:
            self.before_commit()
        await require_execution_allowed(execution_allowed)
        if self.mode == "same-id":
            artifact = request.input_artifacts[0]
        elif self.mode == "wrong-kind":
            artifact = ArtifactRef(
                "edited-audio",
                ArtifactKind.AUDIO,
                "audio/wav",
                size_bytes=8,
                sha256="b" * 64,
            )
        elif self.mode == "missing":
            artifact = ArtifactRef(
                "edited-missing",
                ArtifactKind.IMAGE,
                "image/png",
                size_bytes=8,
                sha256="b" * 64,
            )
        else:
            binding = image_edit_output_binding(
                request,
                provider_id=invocation.provider_id,
                provider_model=invocation.provider_model,
                model_alias=invocation.model_alias,
                quality_tier=invocation.quality_tier,
            )
            artifact = self.store.put_png(
                _png(
                    fill=1,
                    ancillary=(
                        (b"tEXt", b"instruction\x00private"),
                        (b"eXIf", b"private"),
                    ),
                ),
                request_binding=binding,
            )
        artifacts = (artifact,)
        text = ""
        if self.mode == "text":
            text = "must not be returned"
        elif self.mode == "extra":
            artifacts = (artifact, artifact)
        return ProviderResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            provider_model=invocation.provider_model,
            text=text,
            artifacts=artifacts,
        )

    async def close(self) -> None:
        return None


def _manifest(kind: ProviderKind = ProviderKind.LOCAL) -> ProviderCatalogManifest:
    provider_id = "static-image-edit"
    aliases = {
        QualityTier.FAST: "image.edit.fast",
        QualityTier.BALANCED: "image.edit.balanced",
        QualityTier.QUALITY: "image.edit.quality",
    }
    resources = (
        ResourceProfile.remote(max_concurrency=1)
        if kind is ProviderKind.API
        else ResourceProfile(
            target=ResourceTarget.CPU,
            max_concurrency=1,
            system_ram_budget_mb=1_024,
        )
    )
    return ProviderCatalogManifest(
        schema_version=1,
        module_id="test.image-editing",
        capabilities=(
            CapabilityPolicy(
                LogicalCapability.IMAGE_EDITING,
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
                adapter_id="test.static-image-edit",
                capabilities=(LogicalCapability.IMAGE_EDITING,),
                resources=resources,
                enabled=True,
                models=tuple(
                    ModelBinding(alias, "static-edit-model", probe_required=False) for alias in aliases.values()
                ),
            ),
        ),
        routes=(
            CapabilityRoute(
                LogicalCapability.IMAGE_EDITING,
                tuple(TierRoute(tier, (provider_id,), alias) for tier, alias in aliases.items()),
            ),
        ),
    )


async def _runtime(
    tmp_path: Path,
    *,
    kind: ProviderKind = ProviderKind.LOCAL,
    mode: str = "valid",
    audit: bool = True,
    health: bool = True,
    before_commit=None,
) -> tuple[ProviderRegistry, _Provider, _Store]:
    store = _Store(tmp_path / "images")
    registry = ProviderRegistry(
        _manifest(kind),
        audit_sink=_Audit() if audit else None,
    )
    provider = _Provider(store, mode=mode, before_commit=before_commit)
    registry.register_adapter(provider)
    if health:
        await registry.refresh_health(provider.provider_id)
    return registry, provider, store


def _request(
    store: _Store,
    *,
    request_id: str = "edit-100",
    guild_id: int = 10,
    channel_id: int = 20,
    actor_id: int = 30,
    instruction: str = "背景を青にする",
) -> ImageEditingRequest:
    source_binding = hashlib.sha256(f"source:{request_id}:{guild_id}:{channel_id}:{actor_id}".encode()).hexdigest()
    source = store.put_png(
        _png(fill=2, ancillary=((b"tEXt", b"source\x00private"),)),
        request_binding=source_binding,
    )
    claim = ImageEditSource(
        source,
        edit_request_id=request_id,
        guild_id=guild_id,
        channel_id=channel_id,
        actor_id=actor_id,
        source_binding=source_binding,
    )
    return ImageEditingRequest(
        request_id=request_id,
        guild_id=guild_id,
        channel_id=channel_id,
        actor_id=actor_id,
        instruction=instruction,
        source=claim,
    )


def _source_current(request: ImageEditingRequest):
    def current(claim: ImageEditSource) -> bool:
        return claim is request.source and claim.claim_digest == request.source.claim_digest

    return current


def test_request_binds_instruction_source_and_scope_without_repr_leak(tmp_path: Path) -> None:
    store = _Store(tmp_path / "domain")
    request = _request(store, instruction="秘密に見える編集指示")
    changed = _request(
        store,
        request_id="edit-101",
        channel_id=request.channel_id + 1,
    )

    assert request.fingerprint != changed.fingerprint
    assert request.source.claim_digest not in repr(request)
    assert request.instruction not in repr(request)
    with pytest.raises(ValueError, match="scope"):
        ImageEditingRequest(
            request_id=request.request_id,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            actor_id=request.actor_id,
            instruction="背景を青にする",
            source=replace(request.source, guild_id=request.guild_id + 1),
        )


async def test_real_store_edit_returns_new_canonical_id_and_preserves_source(tmp_path: Path) -> None:
    registry, provider, store = await _runtime(tmp_path)
    request = _request(store)
    source_before = store.read_png(
        request.source.artifact,
        request_binding=request.source.source_binding,
    )
    service = ImageEditingService(
        registry,
        store,
        source_artifact_current=_source_current(request),
    )

    first = await service.edit(request, authorization_current=lambda: True)
    second = await service.edit(request, authorization_current=lambda: True)

    assert first is second
    assert first.artifact.artifact_id != request.source.artifact.artifact_id
    assert first.png == canonicalize_png(first.png).data
    assert _chunk_types(first.png) == (b"IHDR", b"IDAT", b"IEND")
    assert (
        store.read_png(
            request.source.artifact,
            request_binding=request.source.source_binding,
        )
        == source_before
    )
    assert provider.calls == 1


async def test_output_quota_never_evicts_the_source_image(tmp_path: Path) -> None:
    store = _Store(tmp_path / "quota", max_artifacts=1)
    registry = ProviderRegistry(_manifest(), audit_sink=_Audit())
    provider = _Provider(store)
    registry.register_adapter(provider)
    await registry.refresh_health(provider.provider_id)
    request = _request(store)
    source_before = store.read_png(
        request.source.artifact,
        request_binding=request.source.source_binding,
    )
    service = ImageEditingService(
        registry,
        store,
        source_artifact_current=_source_current(request),
    )

    with pytest.raises(ImageEditingUnavailableError):
        await service.edit(request, authorization_current=lambda: True)

    assert (
        store.read_png(
            request.source.artifact,
            request_binding=request.source.source_binding,
        )
        == source_before
    )
    assert provider.calls == 1


@pytest.mark.parametrize("mode", ["same-id", "wrong-kind", "missing", "text", "extra"])
async def test_provider_result_must_be_one_new_complete_png(
    tmp_path: Path,
    mode: str,
) -> None:
    registry, provider, store = await _runtime(tmp_path, mode=mode)
    request = _request(store)
    service = ImageEditingService(
        registry,
        store,
        source_artifact_current=_source_current(request),
    )

    with pytest.raises(ImageEditingContractError):
        await service.edit(request, authorization_current=lambda: True)
    assert provider.calls == 1


@pytest.mark.parametrize(
    "mode",
    ["registry", "store", "source", "health", "audit", "consent"],
)
async def test_missing_runtime_boundary_fails_before_provider_output(
    tmp_path: Path,
    mode: str,
) -> None:
    kind = ProviderKind.API if mode == "consent" else ProviderKind.LOCAL
    registry, provider, store = await _runtime(
        tmp_path,
        kind=kind,
        audit=mode != "audit",
        health=mode != "health",
    )
    request = _request(store)
    baseline_puts = store.put_calls
    service = ImageEditingService(
        None if mode == "registry" else registry,
        None if mode == "store" else store,
        source_artifact_current=None if mode == "source" else _source_current(request),
        remote_consent_active=None,
    )

    with pytest.raises((ImageEditingUnavailableError, ImageEditingAuthorizationError)):
        await service.edit(request, authorization_current=lambda: True)
    assert provider.calls == 0
    assert store.put_calls == baseline_puts


async def test_source_owner_denial_happens_before_store_read(tmp_path: Path) -> None:
    registry, provider, store = await _runtime(tmp_path)
    request = _request(store)
    service = ImageEditingService(
        registry,
        store,
        source_artifact_current=lambda _claim: False,
    )

    with pytest.raises(ImageEditingAuthorizationError):
        await service.edit(request, authorization_current=lambda: True)
    assert store.read_calls == 0
    assert provider.calls == 0


async def test_registry_commit_and_output_read_revocation_fail_closed(tmp_path: Path) -> None:
    allowed = True

    def revoke() -> None:
        nonlocal allowed
        allowed = False

    registry, provider, store = await _runtime(tmp_path, before_commit=revoke)
    request = _request(store)
    service = ImageEditingService(
        registry,
        store,
        source_artifact_current=_source_current(request),
    )
    with pytest.raises(ImageEditingAuthorizationError):
        await service.edit(request, authorization_current=lambda: allowed)
    assert provider.calls == 1
    assert store.put_calls == 1

    allowed = True
    registry, provider, store = await _runtime(tmp_path / "read")
    request = _request(store, request_id="edit-output-read")

    def revoke_after_output_read(read_calls: int) -> None:
        nonlocal allowed
        if read_calls == 2:
            allowed = False

    store.on_read = revoke_after_output_read
    service = ImageEditingService(
        registry,
        store,
        source_artifact_current=_source_current(request),
    )
    with pytest.raises(ImageEditingAuthorizationError):
        await service.edit(request, authorization_current=lambda: allowed)
    assert provider.calls == 1


async def test_api_provider_requires_and_rechecks_user_consent(tmp_path: Path) -> None:
    registry, provider, store = await _runtime(tmp_path, kind=ProviderKind.API)
    request = _request(store)
    consent = False
    service = ImageEditingService(
        registry,
        store,
        source_artifact_current=_source_current(request),
        remote_consent_active=lambda _actor: consent,
    )
    with pytest.raises(ImageEditingUnavailableError):
        await service.edit(request, authorization_current=lambda: True)
    assert provider.calls == 0

    consent = True
    edited = await service.edit(request, authorization_current=lambda: True)
    assert edited.artifact.kind is ArtifactKind.IMAGE
    assert provider.calls == 1


class _Sink:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_png(self, interaction, png, **kwargs) -> None:
        self.messages.append(
            {
                "interaction": interaction,
                "png": png,
                **kwargs,
            }
        )


class _Interaction:
    def __init__(
        self,
        *,
        guild_id: int = 10,
        channel_id: int = 20,
        actor_id: int = 30,
    ) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user = SimpleNamespace(id=actor_id)


async def test_delivery_is_one_shot_scope_bound_and_uses_safe_flags(tmp_path: Path) -> None:
    registry, provider, store = await _runtime(tmp_path)
    request = _request(store)
    service = ImageEditingService(
        registry,
        store,
        source_artifact_current=_source_current(request),
    )
    sink = _Sink()
    delivery = ImageEditingDelivery(
        service,
        sink,
        capability_check=lambda *_: True,
    )

    assert await delivery.deliver(_Interaction(), request) is True
    assert await delivery.deliver(_Interaction(), request) is False
    assert await delivery.deliver(_Interaction(channel_id=999), request) is False
    assert provider.calls == 1
    assert len(sink.messages) == 1
    sent = sink.messages[0]
    assert sent["filename"] == EDITED_IMAGE_FILENAME
    assert sent["ephemeral"] is True
    assert sent["mentions_allowed"] is False
    assert request.instruction not in repr(sent)


async def test_delivery_claim_rejects_clone_and_final_revocation(tmp_path: Path) -> None:
    registry, provider, store = await _runtime(tmp_path)
    request = _request(store)
    service = ImageEditingService(
        registry,
        store,
        source_artifact_current=_source_current(request),
    )
    edited = await service.edit(request, authorization_current=lambda: True)
    clone = EditedImage(edited.artifact, edited.png)
    assert (
        await service.claim_delivery(
            request,
            clone,
            authorization_current=lambda: True,
        )
        is False
    )
    assert (
        await service.claim_delivery(
            request,
            edited,
            authorization_current=lambda: True,
        )
        is True
    )
    assert (
        await service.claim_delivery(
            request,
            edited,
            authorization_current=lambda: True,
        )
        is False
    )

    second = _request(store, request_id="edit-final")
    second_service = ImageEditingService(
        registry,
        store,
        source_artifact_current=_source_current(second),
    )
    allowed = True
    original_claim = second_service.claim_delivery

    async def revoke_after_claim(*args, **kwargs):
        nonlocal allowed
        claimed = await original_claim(*args, **kwargs)
        allowed = False
        return claimed

    second_service.claim_delivery = revoke_after_claim
    sink = _Sink()
    delivery = ImageEditingDelivery(
        second_service,
        sink,
        capability_check=lambda *_: allowed,
    )
    assert await delivery.deliver(_Interaction(), second) is False
    assert sink.messages == []


async def test_delivery_rechecks_consent_after_final_capability_check(tmp_path: Path) -> None:
    registry, provider, store = await _runtime(tmp_path, kind=ProviderKind.API)
    request = _request(store, request_id="edit-send-race")
    consent = True
    service = ImageEditingService(
        registry,
        store,
        source_artifact_current=_source_current(request),
        remote_consent_active=lambda _actor: consent,
    )
    after_claim = False
    original_claim = service.claim_delivery

    async def mark_after_claim(*args, **kwargs):
        nonlocal after_claim
        claimed = await original_claim(*args, **kwargs)
        after_claim = claimed
        return claimed

    service.claim_delivery = mark_after_claim

    def revoke_consent_on_final_capability_check(*_args):
        nonlocal consent
        if after_claim:
            consent = False
        return True

    sink = _Sink()
    delivery = ImageEditingDelivery(
        service,
        sink,
        capability_check=revoke_consent_on_final_capability_check,
    )

    assert await delivery.deliver(_Interaction(), request) is False
    assert provider.calls == 1
    assert sink.messages == []


async def test_plugin_is_unready_unregistered_and_fresh_trusted_only() -> None:
    bot = SimpleNamespace(is_closing=False)
    plugin = ImageEditingPlugin()
    await plugin.start(bot)
    assert bot.runtime_capability_readiness[IMAGE_EDITING_CAPABILITY_ID] is False
    assert bot.image_editing_service is plugin.service
    assert plugin.adapter is None
    assert IMAGE_EDITING_MODULE_ID == "media.image-editing"
    assert IMAGE_EDITING_PLUGIN_NAME == "image_editing"

    member = SimpleNamespace(id=30)

    class _Guild:
        id = 10

        async def fetch_member(self, user_id: int):
            assert user_id == member.id
            return member

    class _Guard:
        level = RbacLevel.EVERYONE
        allowed: object = True
        current: object = True

        async def evaluate_fresh_member(self, *_args, **_kwargs):
            return SimpleNamespace(allowed=self.allowed, actor_level=self.level)

        def currently_allowed(self, *_args, **_kwargs):
            return self.current

    guard = _Guard()
    check = ImageEditingPlugin._capability_check(SimpleNamespace(is_closing=False, capability_guard=guard))
    interaction = SimpleNamespace(user=member, guild_id=10, guild=_Guild())
    assert await check(IMAGE_EDITING_CAPABILITY_ID, interaction) is False
    guard.level = RbacLevel.TRUSTED
    guard.allowed = object()
    assert await check(IMAGE_EDITING_CAPABILITY_ID, interaction) is False
    guard.allowed = True
    guard.current = object()
    assert await check(IMAGE_EDITING_CAPABILITY_ID, interaction) is False
    guard.current = True
    assert await check(IMAGE_EDITING_CAPABILITY_ID, interaction) is True

    await plugin.stop()
    assert IMAGE_EDITING_CAPABILITY_ID not in bot.runtime_capability_readiness
    assert not hasattr(bot, "image_editing_service")
    registrations: list[tuple[str, object]] = []
    setup(SimpleNamespace(register_plugin=lambda name, factory: registrations.append((name, factory))))
    assert registrations == [(IMAGE_EDITING_PLUGIN_NAME, ImageEditingPlugin)]
