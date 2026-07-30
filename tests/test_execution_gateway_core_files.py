from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from yonerai_discord.execution_gateway.core_contract import (
    CORE_FACTS_EXTENSION,
    DiscordCoreFacts,
    project_core_message_v01,
)
from yonerai_discord.execution_gateway.core_files import (
    CORE_ARTIFACT_REF_EXTENSION_V01,
    CoreArtifactOwnerScopeV01,
    CoreArtifactRefV01,
    CoreFileReadReceiptV01,
    CoreFileReadRequestV01,
    CoreFileRegistrationV01,
    CoreFilesContractError,
    read_core_file_v01,
    register_core_file_v01,
)
from yonerai_discord.execution_gateway.models import ArtifactReference, RunInput


def _scope() -> CoreArtifactOwnerScopeV01:
    return CoreArtifactOwnerScopeV01(
        provider="discord",
        subject_id="300",
        conversation_id="guild:100:channel:200:user:300",
    )


class _FilesPort:
    def __init__(self, ref: CoreArtifactRefV01) -> None:
        self.ref = ref
        self.requests: list[CoreFileRegistrationV01] = []

    async def register(self, request: CoreFileRegistrationV01) -> CoreArtifactRefV01:
        self.requests.append(request)
        return self.ref


class _FilesReadPort:
    def __init__(self, receipt: object) -> None:
        self.receipt = receipt
        self.requests: list[CoreFileReadRequestV01] = []

    async def read_for_delivery(self, request: CoreFileReadRequestV01) -> object:
        self.requests.append(request)
        return self.receipt


def _registration() -> CoreFileRegistrationV01:
    return CoreFileRegistrationV01(
        local_artifact_id="local-image-1",
        kind="image",
        media_type="image/png",
        owner_scope=_scope(),
        retention="session",
        provenance="discord-attachment",
        content=b"\x89PNG\r\n\x1a\nvalidated-payload",
    )


def _read_ref(
    content: bytes = b"\x89PNG\r\n\x1a\nvalidated-payload",
    *,
    artifact_id: str = "core-artifact-read-1",
    attachment_id: str = "core-attachment-read-1",
    kind: str = "image",
    media_type: str = "image/png",
    owner_scope: CoreArtifactOwnerScopeV01 | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
) -> CoreArtifactRefV01:
    return CoreArtifactRefV01(
        artifact_id=artifact_id,
        attachment_id=attachment_id,
        kind=kind,
        media_type=media_type,
        size_bytes=len(content) if size_bytes is None else size_bytes,
        sha256=hashlib.sha256(content).hexdigest() if sha256 is None else sha256,
        owner_scope=owner_scope or _scope(),
        backend="yonerai-files",
        retention="session",
        provenance="core-result",
    )


@pytest.mark.asyncio
async def test_validated_file_registration_returns_typed_core_ref_not_local_id() -> None:
    request = _registration()
    ref = CoreArtifactRefV01(
        artifact_id="core-artifact-1",
        attachment_id="core-attachment-1",
        kind=request.kind,
        media_type=request.media_type,
        size_bytes=request.size_bytes,
        sha256=request.sha256,
        owner_scope=request.owner_scope,
        backend="yonerai-files",
        retention=request.retention,
        provenance=request.provenance,
    )
    port = _FilesPort(ref)

    artifact = await register_core_file_v01(request, port)

    assert port.requests == [request]
    assert artifact.artifact_id == "core-artifact-1"
    assert artifact.uri is None
    assert artifact.metadata["sha256"] == request.sha256
    assert artifact.metadata["backend"] == "yonerai-files"
    assert artifact.extensions[CORE_ARTIFACT_REF_EXTENSION_V01] is ref


@pytest.mark.asyncio
async def test_scope_bound_core_file_read_returns_validated_bytes() -> None:
    content = b"\x89PNG\r\n\x1a\nvalidated-payload"
    ref = _read_ref(content)
    request = CoreFileReadRequestV01(
        delivery_id="discord-delivery-400",
        ref=ref,
        owner_scope=ref.owner_scope,
    )
    receipt = CoreFileReadReceiptV01(
        delivery_id=request.delivery_id,
        ref=ref,
        owner_scope=request.owner_scope,
        content=content,
    )
    port = _FilesReadPort(receipt)

    assert await read_core_file_v01(request, port) == content
    assert port.requests == [request]


@pytest.mark.asyncio
async def test_core_file_read_rejects_invalid_port_receipt_and_request_binding() -> None:
    content = b"\x89PNG\r\n\x1a\nvalidated-payload"
    ref = _read_ref(content)
    request = CoreFileReadRequestV01(
        delivery_id="discord-delivery-400",
        ref=ref,
        owner_scope=ref.owner_scope,
    )
    baseline = CoreFileReadReceiptV01(
        delivery_id=request.delivery_id,
        ref=ref,
        owner_scope=request.owner_scope,
        content=content,
    )
    other_scope = CoreArtifactOwnerScopeV01(
        provider="discord",
        subject_id="301",
        conversation_id="guild:100:channel:200:user:301",
    )
    other_ref = _read_ref(
        content,
        artifact_id="core-artifact-read-2",
        attachment_id="core-attachment-read-2",
    )

    with pytest.raises(TypeError, match="CoreFileReadRequestV01"):
        await read_core_file_v01(object(), _FilesReadPort(baseline))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="read_for_delivery"):
        await read_core_file_v01(request, object())  # type: ignore[arg-type]
    with pytest.raises(CoreFilesContractError, match="invalid receipt"):
        await read_core_file_v01(request, _FilesReadPort(object()))
    for receipt in (
        replace(baseline, delivery_id="discord-delivery-401"),
        replace(baseline, ref=other_ref),
        replace(baseline, owner_scope=other_scope),
    ):
        with pytest.raises(CoreFilesContractError, match="does not match"):
            await read_core_file_v01(request, _FilesReadPort(receipt))
    with pytest.raises(CoreFilesContractError, match="owner_scope does not match"):
        CoreFileReadRequestV01(
            delivery_id=request.delivery_id,
            ref=ref,
            owner_scope=other_scope,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref",
    (
        _read_ref(size_bytes=1),
        _read_ref(sha256="0" * 64),
        _read_ref(b"not-png"),
        _read_ref(media_type="image/jpeg"),
    ),
)
async def test_core_file_read_rejects_size_hash_or_media_magic_mismatch(ref: CoreArtifactRefV01) -> None:
    content = b"\x89PNG\r\n\x1a\nvalidated-payload"
    if ref.sha256 == hashlib.sha256(b"not-png").hexdigest():
        content = b"not-png"
    request = CoreFileReadRequestV01(
        delivery_id="discord-delivery-400",
        ref=ref,
        owner_scope=ref.owner_scope,
    )
    receipt = CoreFileReadReceiptV01(
        delivery_id=request.delivery_id,
        ref=ref,
        owner_scope=request.owner_scope,
        content=content,
    )

    with pytest.raises(CoreFilesContractError):
        await read_core_file_v01(request, _FilesReadPort(receipt))


def test_core_file_read_contract_hides_ref_content_and_hash_from_repr_and_errors() -> None:
    content = b"\x89PNG\r\n\x1a\nsecret-payload-marker"
    ref = _read_ref(content, artifact_id="private-artifact-marker")
    request = CoreFileReadRequestV01(
        delivery_id="discord-delivery-400",
        ref=ref,
        owner_scope=ref.owner_scope,
    )
    receipt = CoreFileReadReceiptV01(
        delivery_id=request.delivery_id,
        ref=ref,
        owner_scope=request.owner_scope,
        content=content,
    )

    rendered = repr(request) + repr(receipt)
    assert "private-artifact-marker" not in rendered
    assert "secret-payload-marker" not in rendered
    assert ref.sha256 not in rendered
    with pytest.raises(CoreFilesContractError) as error:
        replace(receipt, content=b"")
    message = str(error.value)
    assert "private-artifact-marker" not in message
    assert "secret-payload-marker" not in message
    assert ref.sha256 not in message


@pytest.mark.asyncio
async def test_registration_rejects_local_id_or_local_backend_masquerading_as_core_ref() -> None:
    request = _registration()
    baseline = CoreArtifactRefV01(
        artifact_id="core-artifact-1",
        attachment_id="core-attachment-1",
        kind=request.kind,
        media_type=request.media_type,
        size_bytes=request.size_bytes,
        sha256=request.sha256,
        owner_scope=request.owner_scope,
        backend="yonerai-files",
        retention=request.retention,
        provenance=request.provenance,
    )
    invalid_refs = (
        replace(baseline, attachment_id=request.local_artifact_id),
        replace(baseline, artifact_id=request.local_artifact_id),
        replace(baseline, sha256="0" * 64),
    )
    for ref in invalid_refs:
        with pytest.raises(CoreFilesContractError, match="does not match"):
            await register_core_file_v01(request, _FilesPort(ref))
    with pytest.raises(CoreFilesContractError, match="must not be local"):
        replace(baseline, backend="local-artifact-store")


@pytest.mark.parametrize(
    ("kind", "media_type", "content"),
    (
        ("image", "image/png", b"not-png"),
        ("image", "application/octet-stream", b"bytes"),
        ("file", "application/pdf", b"not-pdf"),
        ("file", "text/plain", b"\xff"),
    ),
)
def test_registration_validates_declared_kind_media_type_and_actual_bytes(
    kind: str,
    media_type: str,
    content: bytes,
) -> None:
    with pytest.raises(CoreFilesContractError):
        CoreFileRegistrationV01(
            local_artifact_id="local-1",
            kind=kind,
            media_type=media_type,
            owner_scope=_scope(),
            retention="ephemeral",
            provenance="discord-attachment",
            content=content,
        )


def test_v01_projection_rejects_plain_local_artifact_and_accepts_registered_core_ref() -> None:
    facts = DiscordCoreFacts(
        user_id=300,
        guild_id=100,
        channel_id=200,
        message_id=400,
        request_id="request-400",
        route_mode="conversation",
        visibility="guild_channel",
    )

    def run_input(artifact: ArtifactReference) -> RunInput:
        return RunInput(
            input_text="添付を処理して",
            idempotency_key="discord:message_create:400",
            conversation_key="guild:100:channel:200:user:300",
            artifacts=(artifact,),
            metadata={"surface": "discord"},
            extensions={CORE_FACTS_EXTENSION: facts},
        )

    local = ArtifactReference(
        artifact_id="local-image-1",
        kind="image",
        media_type="image/png",
        size_bytes=123,
    )
    with pytest.raises(CoreFilesContractError, match="not a registered Core ref"):
        project_core_message_v01(run_input(local))

    ref = CoreArtifactRefV01(
        artifact_id="core-artifact-1",
        attachment_id="core-attachment-1",
        kind="image",
        media_type="image/png",
        size_bytes=123,
        sha256="a" * 64,
        owner_scope=_scope(),
        backend="yonerai-files",
        retention="session",
        provenance="discord-attachment",
    )
    registered = ArtifactReference(
        artifact_id=ref.artifact_id,
        kind=ref.kind,
        media_type=ref.media_type,
        size_bytes=ref.size_bytes,
        metadata={
            "sha256": ref.sha256,
            "owner_scope": ref.owner_scope.to_mapping(),
            "backend": ref.backend,
            "retention": ref.retention,
            "provenance": ref.provenance,
        },
        extensions={CORE_ARTIFACT_REF_EXTENSION_V01: ref},
    )
    projected = project_core_message_v01(run_input(registered))

    assert projected.to_mapping()["attachments"] == [{"type": "image_ref", "attachment_id": "core-attachment-1"}]
