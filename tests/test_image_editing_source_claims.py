from __future__ import annotations

import binascii
import hashlib
import struct
import zlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.image_editing import (
    IMAGE_EDITING_CAPABILITY_ID,
    ImageEditSource,
    ImageEditSourceClaimIssuer,
    ImageEditingAuthorizationError,
    ImageEditingContractError,
    ImageEditingPlugin,
    ImageEditingRequest,
    ImageEditingService,
    image_edit_output_binding,
)
from yonerai_discord.modules.image_generation.artifacts import ImageArtifactStore
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
from yonerai_discord.runtime_readiness import refresh_runtime_readiness


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(fill: int = 0) -> bytes:
    width = height = 64
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\0" + bytes([fill]) * (width * 4) for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


class _Store:
    def __init__(self, root: Path) -> None:
        root.mkdir()
        self.inner = ImageArtifactStore(root)
        self.read_calls = 0

    def put_png(self, data: bytes, *, request_binding: str) -> ArtifactRef:
        return self.inner.put_png(data, request_binding=request_binding)

    def discard_png(self, ref: ArtifactRef, *, request_binding: str) -> bool:
        return self.inner.discard_png(ref, request_binding=request_binding)

    def protect_png(self, ref: ArtifactRef, *, request_binding: str):
        return self.inner.protect_png(ref, request_binding=request_binding)

    def read_png(self, ref: ArtifactRef, *, request_binding: str, read_allowed=None) -> bytes:
        self.read_calls += 1
        return self.inner.read_png(ref, request_binding=request_binding, read_allowed=read_allowed)


@dataclass
class _Audit:
    records: list[AuditRecord] = field(default_factory=list)

    async def append(self, record: AuditRecord) -> None:
        self.records.append(record)


class _Provider:
    provider_id = "source-claim-edit"
    adapter_id = "test.source-claim-edit"

    def __init__(self, store: _Store) -> None:
        self.store = store
        self.calls = 0

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status=HealthStatus.READY,
            checked_at=datetime.now(UTC),
            probed_model_aliases=("image.edit.balanced",),
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
        binding = image_edit_output_binding(
            request,
            provider_id=invocation.provider_id,
            provider_model=invocation.provider_model,
            model_alias=invocation.model_alias,
            quality_tier=invocation.quality_tier,
        )
        artifact = self.store.put_png(_png(7), request_binding=binding)
        return ProviderResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            provider_model=invocation.provider_model,
            artifacts=(artifact,),
        )

    async def close(self) -> None:
        return None


async def _registry(
    store: _Store,
    *,
    refresh_health: bool = True,
    provider_kind: ProviderKind = ProviderKind.LOCAL,
) -> tuple[ProviderRegistry, _Provider]:
    provider_id = _Provider.provider_id
    alias = "image.edit.balanced"
    manifest = ProviderCatalogManifest(
        schema_version=1,
        module_id="test.source-claim",
        capabilities=(
            CapabilityPolicy(
                LogicalCapability.IMAGE_EDITING,
                True,
                RbacLevel.TRUSTED,
                RiskLevel.HIGH,
                audit_required=True,
            ),
        ),
        providers=(
            ProviderManifest(
                provider_id=provider_id,
                kind=provider_kind,
                adapter_id=_Provider.adapter_id,
                capabilities=(LogicalCapability.IMAGE_EDITING,),
                resources=(
                    ResourceProfile.remote(max_concurrency=1)
                    if provider_kind is ProviderKind.API
                    else ResourceProfile(
                        target=ResourceTarget.CPU,
                        max_concurrency=1,
                        system_ram_budget_mb=1_024,
                    )
                ),
                enabled=True,
                models=(ModelBinding(alias, "source-claim-model", probe_required=False),),
            ),
        ),
        routes=(
            CapabilityRoute(
                LogicalCapability.IMAGE_EDITING,
                tuple(TierRoute(tier, (provider_id,), alias) for tier in QualityTier),
            ),
        ),
    )
    registry = ProviderRegistry(manifest, audit_sink=_Audit())
    provider = _Provider(store)
    registry.register_adapter(provider)
    if refresh_health:
        await registry.refresh_health(provider_id)
    return registry, provider


async def _issue(
    issuer: ImageEditSourceClaimIssuer,
    artifact: ArtifactRef,
    binding: str,
    *,
    request_id: str = "edit-source-1",
    authorization_current=lambda: True,
) -> ImageEditSource:
    return await issuer.issue(
        artifact,
        source_binding=binding,
        edit_request_id=request_id,
        guild_id=10,
        channel_id=20,
        actor_id=30,
        authorization_current=authorization_current,
    )


async def test_issued_canonical_png_reaches_fake_provider_edit(tmp_path: Path) -> None:
    store = _Store(tmp_path / "images")
    binding = hashlib.sha256(b"generated-image-binding").hexdigest()
    source = store.put_png(_png(2), request_binding=binding)
    issuer = ImageEditSourceClaimIssuer(store)
    claim = await _issue(issuer, source, binding)
    request = ImageEditingRequest(
        request_id=claim.edit_request_id,
        guild_id=claim.guild_id,
        channel_id=claim.channel_id,
        actor_id=claim.actor_id,
        instruction="背景を青にする",
        source=claim,
    )
    registry, provider = await _registry(store)
    service = ImageEditingService(registry, store, source_artifact_current=issuer.current)

    edited = await service.edit(request, authorization_current=lambda: True)

    assert provider.calls == 1
    assert edited.artifact.artifact_id != source.artifact_id
    assert issuer.current(claim) is True


async def test_invalid_scope_and_request_mismatch_fail_before_artifact_read(tmp_path: Path) -> None:
    store = _Store(tmp_path / "images")
    binding = hashlib.sha256(b"scope-binding").hexdigest()
    source = store.put_png(_png(), request_binding=binding)
    issuer = ImageEditSourceClaimIssuer(store)

    with pytest.raises(ImageEditingContractError):
        await issuer.issue(
            source,
            source_binding=binding,
            edit_request_id="edit-scope",
            guild_id=0,
            channel_id=20,
            actor_id=30,
            authorization_current=lambda: True,
        )
    assert store.read_calls == 0

    claim = await _issue(issuer, source, binding, request_id="edit-scope")
    reads_after_issue = store.read_calls
    with pytest.raises(ValueError, match="scope"):
        ImageEditingRequest(
            request_id="edit-other",
            guild_id=claim.guild_id,
            channel_id=claim.channel_id,
            actor_id=claim.actor_id,
            instruction="変更",
            source=claim,
        )
    assert store.read_calls == reads_after_issue


@pytest.mark.parametrize("case", ["kind", "media", "hash", "size", "binding", "tamper"])
async def test_wrong_artifact_identity_or_binding_never_issues(tmp_path: Path, case: str) -> None:
    store = _Store(tmp_path / "images")
    binding = hashlib.sha256(b"identity-binding").hexdigest()
    source = store.put_png(_png(), request_binding=binding)
    candidate = source
    supplied_binding = binding
    if case == "kind":
        candidate = ArtifactRef(source.artifact_id, ArtifactKind.AUDIO, "audio/wav", source.size_bytes, source.sha256)
    elif case == "media":
        candidate = replace(source, media_type="image/jpeg")
    elif case == "hash":
        candidate = replace(source, sha256="f" * 64)
    elif case == "size":
        candidate = replace(source, size_bytes=(source.size_bytes or 0) + 1)
    elif case == "binding":
        supplied_binding = hashlib.sha256(b"wrong-binding").hexdigest()
    else:
        (store.inner.root / f"{source.artifact_id}.png").write_bytes(_png(8))

    issuer = ImageEditSourceClaimIssuer(store)
    with pytest.raises((ImageEditingAuthorizationError, ImageEditingContractError)):
        await _issue(issuer, candidate, supplied_binding)
    assert (
        issuer.current(
            ImageEditSource(
                source,
                edit_request_id="edit-source-1",
                guild_id=10,
                channel_id=20,
                actor_id=30,
                source_binding=binding,
            )
        )
        is False
    )


@pytest.mark.parametrize(
    ("deny_at", "expected_reads"),
    ((2, 0), (3, 1), (6, 1)),
)
async def test_authorization_rechecked_before_during_and_immediately_before_issue(
    tmp_path: Path,
    deny_at: int,
    expected_reads: int,
) -> None:
    store = _Store(tmp_path / "images")
    binding = hashlib.sha256(b"fresh-auth-binding").hexdigest()
    source = store.put_png(_png(), request_binding=binding)
    issuer = ImageEditSourceClaimIssuer(store)
    calls = 0

    async def authorization_current() -> bool:
        nonlocal calls
        calls += 1
        return calls != deny_at

    with pytest.raises(ImageEditingAuthorizationError):
        await _issue(issuer, source, binding, authorization_current=authorization_current)
    assert store.read_calls == expected_reads


async def test_clone_revoke_eviction_and_close_make_claim_non_current(tmp_path: Path) -> None:
    store = _Store(tmp_path / "images")
    first_binding = hashlib.sha256(b"first-binding").hexdigest()
    second_binding = hashlib.sha256(b"second-binding").hexdigest()
    first_ref = store.put_png(_png(1), request_binding=first_binding)
    second_ref = store.put_png(_png(2), request_binding=second_binding)
    issuer = ImageEditSourceClaimIssuer(store, max_claims=1)
    first = await _issue(issuer, first_ref, first_binding, request_id="edit-first")

    clone = replace(first)
    assert issuer.current(first) is True
    assert issuer.current(clone) is False
    assert issuer.revoke(clone) is False

    second = await _issue(issuer, second_ref, second_binding, request_id="edit-second")
    assert issuer.current(first) is False
    assert issuer.current(second) is True
    assert issuer.revoke(second) is True
    assert issuer.current(second) is False

    third = await _issue(issuer, first_ref, first_binding, request_id="edit-third")
    issuer.begin_close()
    assert issuer.current(third) is False
    with pytest.raises(ImageEditingAuthorizationError):
        await _issue(issuer, first_ref, first_binding, request_id="edit-fourth")

    recency = ImageEditSourceClaimIssuer(store, max_claims=2)
    old_first = await _issue(recency, first_ref, first_binding, request_id="edit-recency-a")
    recency_b = await _issue(recency, second_ref, second_binding, request_id="edit-recency-b")
    refreshed_first = await _issue(recency, first_ref, first_binding, request_id="edit-recency-a")
    recency_c = await _issue(recency, first_ref, first_binding, request_id="edit-recency-c")
    assert recency.current(old_first) is False
    assert recency.current(recency_b) is False
    assert recency.current(refreshed_first) is True
    assert recency.current(recency_c) is True


async def test_plugin_connects_issuer_only_when_store_is_configured(tmp_path: Path) -> None:
    unconfigured_bot = SimpleNamespace(is_closing=False)
    unconfigured = ImageEditingPlugin()
    await unconfigured.start(unconfigured_bot)
    assert unconfigured.source_claim_issuer is None
    assert unconfigured.service is not None
    assert unconfigured.service.source_artifact_current is None
    assert unconfigured_bot.runtime_capability_readiness[IMAGE_EDITING_CAPABILITY_ID] is False
    assert not hasattr(unconfigured_bot, "image_editing_source_claim_issuer")
    await unconfigured.stop()

    store = _Store(tmp_path / "images")
    configured_bot = SimpleNamespace(is_closing=False)
    configured = ImageEditingPlugin(artifact_store=store)
    await configured.start(configured_bot)
    assert configured.source_claim_issuer is not None
    assert configured.service is not None
    assert configured.service.source_artifact_current == configured.source_claim_issuer.current
    assert configured_bot.image_editing_source_claim_issuer is configured.source_claim_issuer
    assert configured_bot.runtime_capability_readiness[IMAGE_EDITING_CAPABILITY_ID] is False
    issuer = configured.source_claim_issuer
    await configured.stop()
    assert issuer is not None
    assert not hasattr(configured_bot, "image_editing_source_claim_issuer")


async def test_plugin_readiness_probe_tracks_health_and_requires_api_consent_store(tmp_path: Path) -> None:
    local_store = _Store(tmp_path / "local-images")
    local_registry, local_provider = await _registry(local_store, refresh_health=False)
    local_bot = SimpleNamespace(is_closing=False)
    local_plugin = ImageEditingPlugin(registry=local_registry, artifact_store=local_store)
    await local_plugin.start(local_bot)
    assert local_bot.runtime_capability_readiness[IMAGE_EDITING_CAPABILITY_ID] is False

    await local_registry.refresh_health(local_provider.provider_id)
    assert refresh_runtime_readiness(local_bot, IMAGE_EDITING_CAPABILITY_ID) is True
    assert local_bot.runtime_capability_readiness[IMAGE_EDITING_CAPABILITY_ID] is True
    await local_plugin.stop()
    assert not hasattr(local_bot, "runtime_capability_readiness_probes")

    api_store = _Store(tmp_path / "api-images")
    api_registry, _ = await _registry(api_store, provider_kind=ProviderKind.API)
    api_bot = SimpleNamespace(is_closing=False)
    api_plugin = ImageEditingPlugin(registry=api_registry, artifact_store=api_store)
    await api_plugin.start(api_bot)
    assert api_bot.runtime_capability_readiness[IMAGE_EDITING_CAPABILITY_ID] is False
    await api_plugin.stop()


async def test_plugin_stop_cleans_state_when_close_hook_raises(tmp_path: Path) -> None:
    store = _Store(tmp_path / "images")
    bot = SimpleNamespace(is_closing=False)
    plugin = ImageEditingPlugin(artifact_store=store)
    await plugin.start(bot)
    service = plugin.service
    issuer = plugin.source_claim_issuer
    assert service is not None
    assert issuer is not None

    class _BrokenAdapter:
        def begin_close(self) -> None:
            raise RuntimeError("close failed")

    adapter = _BrokenAdapter()
    plugin.adapter = adapter
    bot.image_editing_adapter = adapter

    with pytest.raises(RuntimeError, match="close failed"):
        await plugin.stop()
    assert plugin.service is None
    assert plugin.adapter is None
    assert plugin.source_claim_issuer is None
    assert service._closing is True
    assert issuer._closing is True
    assert IMAGE_EDITING_CAPABILITY_ID not in bot.runtime_capability_readiness
    assert not hasattr(bot, "image_editing_service")
    assert not hasattr(bot, "image_editing_adapter")
    assert not hasattr(bot, "image_editing_source_claim_issuer")


async def test_repr_and_failures_do_not_expose_png_binding_or_instruction(tmp_path: Path) -> None:
    store = _Store(tmp_path / "images")
    binding = hashlib.sha256(b"private-source-binding").hexdigest()
    png = _png(9)
    source = store.put_png(png, request_binding=binding)
    issuer = ImageEditSourceClaimIssuer(store)
    claim = await _issue(issuer, source, binding)
    instruction = "秘密の編集指示"
    request = ImageEditingRequest(
        request_id=claim.edit_request_id,
        guild_id=claim.guild_id,
        channel_id=claim.channel_id,
        actor_id=claim.actor_id,
        instruction=instruction,
        source=claim,
    )

    rendered = "\n".join((repr(issuer), repr(claim), repr(request)))
    assert binding not in rendered
    assert source.artifact_id not in rendered
    assert instruction not in rendered
    with pytest.raises(ImageEditingAuthorizationError) as captured:
        await _issue(issuer, source, "0" * 64, request_id="edit-private")
    message = str(captured.value)
    assert binding not in message
    assert png.hex() not in message
    assert instruction not in message
