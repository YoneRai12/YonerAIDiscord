from __future__ import annotations

import hashlib
import struct
import zlib
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.image_editing.domain import ImageEditSource
from yonerai_discord.modules.image_editing.source_claims import ImageEditSourceClaimIssuer
from yonerai_discord.modules.image_generation.domain import (
    ImageGenerationAuthorizationError,
    ImageGenerationRequest,
    image_artifact_request_binding,
)
from yonerai_discord.modules.image_generation.plugin import ImageGenerationPlugin
from yonerai_discord.modules.image_generation.service import ImageGenerationService
from yonerai_discord.modules.image_generation.artifacts import ImageArtifactStore
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
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


PNG = b"\x89PNG\r\n\x1a\nbridge-test"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _canonical_png() -> bytes:
    header = struct.pack(">IIBBBBB", 64, 64, 8, 6, 0, 0, 0)
    raw = b"".join(b"\0" + bytes([3]) * (64 * 4) for _ in range(64))
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


class _Store:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.bindings: dict[str, str] = {}
        self.read_calls = 0

    def put_png(self, data: bytes, *, request_binding: str, artifact_id: str | None = None) -> ArtifactRef:
        artifact_id = artifact_id or f"image-{len(self.values) + 1}"
        self.values[artifact_id] = data
        self.bindings[artifact_id] = request_binding
        return ArtifactRef(artifact_id, ArtifactKind.IMAGE, "image/png", len(data), hashlib.sha256(data).hexdigest())

    def protect_png(self, ref: ArtifactRef, *, request_binding: str):
        if self.bindings.get(ref.artifact_id) != request_binding:
            raise RuntimeError("binding mismatch")
        return nullcontext()

    def read_png(self, ref: ArtifactRef, *, request_binding: str, read_allowed=None) -> bytes:
        self.read_calls += 1
        if read_allowed is not None and read_allowed() is not True:
            raise RuntimeError("denied")
        if self.bindings.get(ref.artifact_id) != request_binding:
            raise RuntimeError("binding mismatch")
        return self.values[ref.artifact_id]


class _Provider:
    provider_id = "bridge-image"
    adapter_id = "test.bridge-image"

    def __init__(self, store: _Store) -> None:
        self.store = store

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status=HealthStatus.READY,
            checked_at=datetime.now(UTC),
            probed_model_aliases=("image.balanced",),
        )

    async def execute(
        self, request: ProviderRequest, invocation: ProviderInvocation, *, execution_allowed=None
    ) -> ProviderResult:
        await require_execution_allowed(execution_allowed)
        binding = image_artifact_request_binding(
            request,
            provider_id=invocation.provider_id,
            provider_model=invocation.provider_model,
            model_alias=invocation.model_alias,
            quality_tier=invocation.quality_tier,
        )
        return ProviderResult(
            request.request_id,
            self.provider_id,
            invocation.provider_model,
            artifacts=(
                self.store.put_png(
                    _canonical_png() if hasattr(self.store, "root") else PNG,
                    request_binding=binding,
                ),
            ),
        )

    async def close(self) -> None:
        return None


class _Audit:
    async def append(self, _record) -> None:
        return None


def _manifest() -> ProviderCatalogManifest:
    return ProviderCatalogManifest(
        schema_version=1,
        module_id="test.bridge",
        capabilities=(CapabilityPolicy(LogicalCapability.IMAGE_GENERATION, True, RbacLevel.TRUSTED, RiskLevel.HIGH),),
        providers=(
            ProviderManifest(
                provider_id=_Provider.provider_id,
                kind=ProviderKind.LOCAL,
                adapter_id=_Provider.adapter_id,
                capabilities=(LogicalCapability.IMAGE_GENERATION,),
                resources=ResourceProfile(target=ResourceTarget.CPU, max_concurrency=1, system_ram_budget_mb=1024),
                enabled=True,
                models=(ModelBinding("image.balanced", "bridge-model", probe_required=False),),
            ),
        ),
        routes=(
            CapabilityRoute(
                LogicalCapability.IMAGE_GENERATION,
                tuple(TierRoute(tier, (_Provider.provider_id,), "image.balanced") for tier in QualityTier),
            ),
        ),
    )


async def _ready_service(issuer=None) -> tuple[ImageGenerationService, _Store, ImageGenerationRequest, object]:
    store = _Store()
    registry = ProviderRegistry(_manifest(), audit_sink=_Audit())
    registry.register_adapter(_Provider(store))
    await registry.refresh_health(_Provider.provider_id)
    service = ImageGenerationService(registry, store, image_edit_source_claim_issuer=issuer)
    request = ImageGenerationRequest("generated-bridge", 10, 20, 30, "a safe bridge")
    generated = await service.generate(request, authorization_current=lambda: True)
    return service, store, request, generated


@dataclass
class _Issuer:
    artifact_store: object
    calls: list[dict] = None

    def __post_init__(self) -> None:
        self.calls = []

    async def issue(self, artifact, **kwargs):
        self.calls.append({"artifact": artifact, **kwargs})
        return ImageEditSource(
            artifact,
            edit_request_id=kwargs["edit_request_id"],
            guild_id=kwargs["guild_id"],
            channel_id=kwargs["channel_id"],
            actor_id=kwargs["actor_id"],
            source_binding=kwargs["source_binding"],
        )


async def test_bridge_rechecks_canonical_store_and_scopes_claim() -> None:
    placeholder = _Issuer(None)
    service, store, request, generated = await _ready_service(placeholder)
    placeholder.artifact_store = store
    service.image_edit_source_claim_issuer = placeholder

    source = await service.issue_edit_source(
        request, generated, edit_request_id="edit-bridge", authorization_current=lambda: True
    )

    assert source.edit_request_id == "edit-bridge"
    assert store.read_calls >= 2
    assert placeholder.calls == [
        {
            "artifact": generated.artifact,
            "source_binding": service._completed[request.request_id].request_binding,
            "edit_request_id": "edit-bridge",
            "guild_id": 10,
            "channel_id": 20,
            "actor_id": 30,
            "authorization_current": placeholder.calls[0]["authorization_current"],
        }
    ]


@pytest.mark.parametrize("case", ["unconfigured", "other_store", "missing_issue"])
async def test_bridge_fails_closed_without_same_store_issuer(case: str) -> None:
    issuer = None if case == "unconfigured" else _Issuer(_Store())
    if case == "missing_issue":
        issuer = SimpleNamespace(artifact_store=_Store())
    service, _store, request, generated = await _ready_service(issuer)
    with pytest.raises(Exception, match="source claims are unavailable"):
        await service.issue_edit_source(
            request, generated, edit_request_id="edit-denied", authorization_current=lambda: True
        )


async def test_bridge_rejects_non_cached_or_wrong_generated_artifact() -> None:
    issuer = _Issuer(None)
    service, store, request, generated = await _ready_service(issuer)
    issuer.artifact_store = store
    service.image_edit_source_claim_issuer = issuer
    wrong = SimpleNamespace(artifact=ArtifactRef("other", ArtifactKind.IMAGE, "image/png", 1, "0" * 64), png=b"x")
    with pytest.raises(TypeError):
        await service.issue_edit_source(
            request, wrong, edit_request_id="edit-wrong", authorization_current=lambda: True
        )
    assert issuer.calls == []


async def test_bridge_fresh_authorization_blocks_before_issuer() -> None:
    issuer = _Issuer(None)
    service, store, request, generated = await _ready_service(issuer)
    issuer.artifact_store = store
    service.image_edit_source_claim_issuer = issuer
    with pytest.raises(ImageGenerationAuthorizationError):
        await service.issue_edit_source(
            request, generated, edit_request_id="edit-auth", authorization_current=lambda: False
        )
    assert issuer.calls == []


async def test_plugin_injects_only_same_store_issuer_and_clears_reference() -> None:
    store = _Store()
    matching = _Issuer(store)
    bot = SimpleNamespace(
        image_artifact_store=store,
        image_editing_source_claim_issuer=matching,
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
    )
    plugin = ImageGenerationPlugin()
    await plugin.start(bot)
    assert plugin.service is not None
    assert plugin.service.image_edit_source_claim_issuer is matching
    await plugin.stop()
    assert plugin.image_edit_source_claim_issuer is None

    mismatched = ImageGenerationPlugin(artifact_store=store)
    bot.image_editing_source_claim_issuer = _Issuer(_Store())
    await mismatched.start(bot)
    assert mismatched.service is not None
    assert mismatched.service.image_edit_source_claim_issuer is None
    await mismatched.stop()


async def test_bridge_uses_existing_real_issuer_for_same_canonical_store(tmp_path) -> None:
    root = tmp_path / "canonical"
    root.mkdir()
    store = ImageArtifactStore(root)
    registry = ProviderRegistry(_manifest(), audit_sink=_Audit())
    registry.register_adapter(_Provider(store))
    await registry.refresh_health(_Provider.provider_id)
    issuer = ImageEditSourceClaimIssuer(store)
    service = ImageGenerationService(registry, store, image_edit_source_claim_issuer=issuer)
    request = ImageGenerationRequest("generated-real", 10, 20, 30, "a canonical source")
    generated = await service.generate(request, authorization_current=lambda: True)

    claim = await service.issue_edit_source(
        request, generated, edit_request_id="edit-real", authorization_current=lambda: True
    )

    assert isinstance(claim, ImageEditSource)
    assert issuer.current(claim) is True
