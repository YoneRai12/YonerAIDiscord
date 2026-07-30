from __future__ import annotations

import binascii
import struct
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import discord

from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.image_editing import (
    EDITED_IMAGE_FILENAME,
    ImageEditSourceClaimIssuer,
    ImageEditingDelivery,
    ImageEditingService,
    image_edit_output_binding,
)
from yonerai_discord.modules.image_generation.artifacts import ImageArtifactStore
from yonerai_discord.provider_registry import (
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


def _png(fill: int = 0) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
        )

    width = height = 64
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\0" + bytes([fill]) * (width * 4) for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class _Store:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True)
        self.inner = ImageArtifactStore(root)
        self.put_calls = 0
        self.discarded = []

    def put_png(self, data: bytes, *, request_binding: str):
        self.put_calls += 1
        return self.inner.put_png(data, request_binding=request_binding)

    def discard_png(self, ref, *, request_binding: str) -> bool:
        self.discarded.append(ref)
        return self.inner.discard_png(ref, request_binding=request_binding)

    def protect_png(self, ref, *, request_binding: str):
        return self.inner.protect_png(ref, request_binding=request_binding)

    def read_png(self, ref, *, request_binding: str, read_allowed=None) -> bytes:
        return self.inner.read_png(ref, request_binding=request_binding, read_allowed=read_allowed)


@dataclass
class _Audit:
    records: list[AuditRecord] = field(default_factory=list)

    async def append(self, record: AuditRecord) -> None:
        self.records.append(record)


class _Provider:
    provider_id = "mention-adapter-edit"
    adapter_id = "test.mention-adapter-edit"

    def __init__(self, store: _Store, *, fail: bool = False) -> None:
        self.store, self.fail, self.calls = store, fail, 0

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.provider_id,
            status=HealthStatus.READY,
            checked_at=datetime.now(UTC),
            probed_model_aliases=("image.edit.balanced",),
        )

    async def execute(
        self, request: ProviderRequest, invocation: ProviderInvocation, *, execution_allowed=None
    ) -> ProviderResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
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


async def _delivery(tmp_path: Path, *, fail_provider: bool = False, revoke_on_issue: list[bool] | None = None):
    store = _Store(tmp_path / "images")
    manifest = ProviderCatalogManifest(
        schema_version=1,
        module_id="test.mention-adapter",
        capabilities=(
            CapabilityPolicy(
                LogicalCapability.IMAGE_EDITING, True, RbacLevel.TRUSTED, RiskLevel.HIGH, audit_required=True
            ),
        ),
        providers=(
            ProviderManifest(
                provider_id=_Provider.provider_id,
                kind=ProviderKind.LOCAL,
                adapter_id=_Provider.adapter_id,
                capabilities=(LogicalCapability.IMAGE_EDITING,),
                resources=ResourceProfile(target=ResourceTarget.CPU, max_concurrency=1, system_ram_budget_mb=1024),
                enabled=True,
                models=(ModelBinding("image.edit.balanced", "mention-adapter-model", probe_required=False),),
            ),
        ),
        routes=(
            CapabilityRoute(
                LogicalCapability.IMAGE_EDITING,
                tuple(TierRoute(tier, (_Provider.provider_id,), "image.edit.balanced") for tier in QualityTier),
            ),
        ),
    )
    registry = ProviderRegistry(manifest, audit_sink=_Audit())
    provider = _Provider(store, fail=fail_provider)
    registry.register_adapter(provider)
    await registry.refresh_health(provider.provider_id)
    if revoke_on_issue is None:
        claim_issuer = ImageEditSourceClaimIssuer(store)
    else:

        class _RevokingIssuer(ImageEditSourceClaimIssuer):
            async def issue(self, *args, **kwargs):
                revoke_on_issue[0] = True
                return await super().issue(*args, **kwargs)

        claim_issuer = _RevokingIssuer(store)
    service = ImageEditingService(registry, store, source_artifact_current=claim_issuer.current)
    return (
        ImageEditingDelivery(service, None, capability_check=lambda *_: True, source_claim_issuer=claim_issuer),
        provider,
        store,
    )


class _Attachment:
    def __init__(self, data: bytes, *, filename: str = "source.png", content_type: str = "image/png") -> None:
        self.data, self.size, self.filename, self.content_type = data, len(data), filename, content_type

    async def read(self, *, use_cached: bool) -> bytes:
        assert use_cached is True
        return self.data


def _message(*, message_id: int = 99, attachments=(), reply_raises: bool = False):
    guild, channel, author = SimpleNamespace(id=10), SimpleNamespace(id=20), SimpleNamespace(id=30, bot=False)
    replies = []

    async def reply(**kwargs) -> None:
        if reply_raises:
            raise RuntimeError("Discord reply failed")
        replies.append(kwargs)

    return SimpleNamespace(
        id=message_id,
        guild=guild,
        channel=channel,
        author=author,
        attachments=attachments,
        reference=None,
        reply=reply,
        replies=replies,
    )


_SETTINGS = SimpleNamespace(
    ai_attachment_max_file_bytes=8 * 1024 * 1024, ai_attachment_max_total_bytes=8 * 1024 * 1024, ai_timeout_seconds=15.0
)


async def test_mention_current_png_replies_with_fixed_filename_and_mentions_disabled(tmp_path: Path) -> None:
    delivery, provider, store = await _delivery(tmp_path)
    message = _message(attachments=(_Attachment(_png()),))
    message.reference = SimpleNamespace(message_id=1, guild_id=999, channel_id=999)

    assert await delivery.edit_for_message(
        message, instruction="edit", authorization_current=lambda: True, settings=_SETTINGS
    )
    assert provider.calls == 1
    sent = message.replies[0]
    assert sent["file"].filename == EDITED_IMAGE_FILENAME
    assert sent["mention_author"] is False
    assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


async def test_mention_uses_same_author_same_scope_replied_png_when_current_has_no_attachment(tmp_path: Path) -> None:
    delivery, provider, store = await _delivery(tmp_path)
    message = _message()
    source = _message(message_id=51, attachments=(_Attachment(_png()),))
    source.guild, source.channel, source.author = message.guild, message.channel, message.author
    message.reference = SimpleNamespace(message_id=51, guild_id=10, channel_id=20, resolved=source)

    assert await delivery.edit_for_message(
        message, instruction="edit", authorization_current=lambda: True, settings=_SETTINGS
    )
    assert provider.calls == 1
    assert len(message.replies) == 1


async def test_mention_rejects_foreign_author_or_channel_reference_before_ingress(tmp_path: Path) -> None:
    delivery, provider, store = await _delivery(tmp_path)
    for foreign_author, foreign_channel in ((True, False), (False, True)):
        message = _message(message_id=100 + int(foreign_author) + int(foreign_channel))
        source = _message(message_id=55, attachments=(_Attachment(_png()),))
        source.guild = message.guild
        source.channel = SimpleNamespace(id=21) if foreign_channel else message.channel
        source.author = SimpleNamespace(id=31, bot=False) if foreign_author else message.author
        message.reference = SimpleNamespace(message_id=55, guild_id=10, channel_id=20, resolved=source)
        assert not await delivery.edit_for_message(
            message, instruction="edit", authorization_current=lambda: True, settings=_SETTINGS
        )
        assert message.replies == []
    assert provider.calls == 0
    assert store.put_calls == 0


async def test_mention_rejects_multiple_or_non_png_attachments_before_provider(tmp_path: Path) -> None:
    delivery, provider, store = await _delivery(tmp_path)
    messages = (
        _message(attachments=(_Attachment(_png()), _Attachment(_png()))),
        _message(attachments=(_Attachment(b"not-png", filename="source.jpg", content_type="image/jpeg"),)),
    )
    for message in messages:
        assert not await delivery.edit_for_message(
            message, instruction="edit", authorization_current=lambda: True, settings=_SETTINGS
        )
        assert message.replies == []
    assert provider.calls == 0
    assert store.put_calls == 0


async def test_mention_discards_ingressed_artifact_when_authorization_is_revoked_before_claim(tmp_path: Path) -> None:
    authorization_revoked = [False]
    delivery, provider, store = await _delivery(tmp_path, revoke_on_issue=authorization_revoked)
    message = _message(attachments=(_Attachment(_png()),))

    assert not await delivery.edit_for_message(
        message,
        instruction="edit",
        authorization_current=lambda: not authorization_revoked[0],
        settings=_SETTINGS,
    )
    assert len(store.discarded) == 1
    assert provider.calls == 0
    assert message.replies == []


async def test_mention_provider_and_reply_fail_closed_and_cache_is_bounded(tmp_path: Path) -> None:
    delivery, provider, _ = await _delivery(tmp_path / "provider", fail_provider=True)
    for message_id in range(1, 66):
        message = _message(message_id=message_id, attachments=(_Attachment(_png(message_id % 8)),))
        assert not await delivery.edit_for_message(
            message, instruction="edit", authorization_current=lambda: True, settings=_SETTINGS
        )
    assert provider.calls == 65
    assert len(delivery._mention_sources) <= 64

    reply_delivery, reply_provider, _ = await _delivery(tmp_path / "reply")
    reply_failure = _message(attachments=(_Attachment(_png()),), reply_raises=True)
    assert not await reply_delivery.edit_for_message(
        reply_failure, instruction="edit", authorization_current=lambda: True, settings=_SETTINGS
    )
    assert reply_provider.calls == 1
