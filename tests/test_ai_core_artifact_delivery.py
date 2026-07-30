from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import replace
from typing import Any

import pytest
from PIL import Image

import yonerai_discord.modules.ai.core_artifact_delivery as delivery
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
from yonerai_discord.modules.ai.core_artifact_delivery import (
    CoreArtifactDeliveryError,
    CoreArtifactDeliveryPreparer,
)
from yonerai_discord.modules.media_pipeline.artifacts import canonicalize_image
from yonerai_discord.modules.media_pipeline.domain import ArtifactKind, MAX_PNG_BYTES


def _facts(request_id: str = "request-1") -> DiscordCoreFacts:
    return DiscordCoreFacts(
        user_id=300,
        guild_id=100,
        channel_id=200,
        message_id=400,
        request_id=request_id,
        route_mode="conversation",
        visibility="guild_channel",
    )


def _png(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    with Image.new("RGB", (2, 3), color) as image:
        return canonicalize_image(image).data


def _scope(facts: DiscordCoreFacts) -> CoreArtifactOwnerScopeV01:
    return CoreArtifactOwnerScopeV01(
        provider="discord",
        subject_id=str(facts.user_id),
        conversation_id=discord_core_conversation_id(facts),
    )


def _ref(
    facts: DiscordCoreFacts,
    data: bytes,
    *,
    suffix: str = "1",
    kind: str = "image",
    media_type: str = "image/png",
    size_bytes: int | None = None,
) -> CoreArtifactRefV01:
    return CoreArtifactRefV01(
        artifact_id=f"core-artifact-{suffix}",
        attachment_id=f"core-attachment-{suffix}",
        kind=kind,
        media_type=media_type,
        size_bytes=len(data) if size_bytes is None else size_bytes,
        sha256=hashlib.sha256(data).hexdigest(),
        owner_scope=_scope(facts),
        backend="yonerai-files",
        retention="session",
        provenance="core-output",
    )


class _ReadPort:
    def __init__(self, content_by_id: dict[str, bytes]) -> None:
        self.content_by_id = content_by_id
        self.requests: list[CoreFileReadRequestV01] = []
        self.receipt_factory: Any = None
        self.on_read: Any = None

    async def read_for_delivery(self, request: CoreFileReadRequestV01) -> CoreFileReadReceiptV01:
        self.requests.append(request)
        if self.on_read is not None:
            self.on_read()
        content = self.content_by_id[request.ref.artifact_id]
        if self.receipt_factory is not None:
            return self.receipt_factory(request, content)
        return CoreFileReadReceiptV01(
            delivery_id=request.delivery_id,
            ref=request.ref,
            owner_scope=request.owner_scope,
            content=content,
        )


@pytest.mark.asyncio
async def test_prepare_returns_code_owned_canonical_pngs_with_exact_request_binding() -> None:
    facts = _facts()
    first_data = _png()
    second_data = _png((40, 50, 60))
    first = _ref(facts, first_data, suffix="1")
    second = _ref(facts, second_data, suffix="2")
    port = _ReadPort({first.artifact_id: first_data, second.artifact_id: second_data})
    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: port)
    checks = 0

    async def authorization_current() -> bool:
        nonlocal checks
        checks += 1
        return True

    prepared = await preparer.prepare(
        (artifact_reference_from_core_v01(first), artifact_reference_from_core_v01(second)),
        facts=facts,
        authorization_current=authorization_current,
    )

    assert [item.filename for item in prepared] == ["media-01.png", "media-02.png"]
    assert [item.data for item in prepared] == [first_data, second_data]
    assert all(item.kind is ArtifactKind.IMAGE and item.media_type == "image/png" for item in prepared)
    assert [request.delivery_id for request in port.requests] == [
        "request-1:msg:400:media:01",
        "request-1:msg:400:media:02",
    ]
    assert all(request.owner_scope == _scope(facts) for request in port.requests)
    assert checks == 8  # start + 3 per read + final
    assert first.artifact_id not in repr(prepared)


@pytest.mark.asyncio
@pytest.mark.parametrize("artifacts", [(), ("not-an-artifact",)])
async def test_prepare_rejects_invalid_count_or_type_before_read(artifacts: tuple[object, ...]) -> None:
    port = _ReadPort({})
    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: port)

    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare(artifacts, facts=_facts(), authorization_current=lambda: True)  # type: ignore[arg-type]

    assert port.requests == []


def test_preparer_rejects_missing_read_port_without_exposing_details() -> None:
    with pytest.raises(CoreArtifactDeliveryError, match="unavailable"):
        CoreArtifactDeliveryPreparer(None, port_current=lambda: None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_prepare_rejects_duplicate_ids_wrong_scope_and_non_png_before_read() -> None:
    facts = _facts()
    data = _png()
    baseline = _ref(facts, data)
    port = _ReadPort({})
    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: port)
    duplicate_attachment = replace(baseline, artifact_id="core-artifact-2")
    wrong_scope_artifact = artifact_reference_from_core_v01(_ref(replace(facts, user_id=301), data))
    non_png = _ref(facts, b"%PDF-bytes", kind="file", media_type="application/pdf")

    cases = (
        (
            artifact_reference_from_core_v01(baseline),
            artifact_reference_from_core_v01(duplicate_attachment),
        ),
        (wrong_scope_artifact,),
        (artifact_reference_from_core_v01(non_png),),
    )
    for artifacts in cases:
        with pytest.raises(CoreArtifactDeliveryError):
            await preparer.prepare(artifacts, facts=facts, authorization_current=lambda: True)

    five = tuple(artifact_reference_from_core_v01(_ref(facts, data, suffix=str(index))) for index in range(1, 6))
    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare(five, facts=facts, authorization_current=lambda: True)
    assert port.requests == []


@pytest.mark.asyncio
async def test_prepare_rejects_per_item_and_total_declared_limits_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _facts()
    data = _png()
    port = _ReadPort({})
    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: port)
    oversized = _ref(facts, data, size_bytes=MAX_PNG_BYTES + 1)

    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare(
            (artifact_reference_from_core_v01(oversized),),
            facts=facts,
            authorization_current=lambda: True,
        )

    monkeypatch.setattr(delivery, "MAX_PREPARED_MEDIA_BYTES", len(data) - 1)
    valid = _ref(facts, data)
    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare(
            (artifact_reference_from_core_v01(valid),),
            facts=facts,
            authorization_current=lambda: True,
        )
    assert port.requests == []


@pytest.mark.asyncio
async def test_prepare_fails_closed_on_authorization_or_port_identity_change() -> None:
    facts = _facts()
    data = _png()
    ref = _ref(facts, data)
    artifact = artifact_reference_from_core_v01(ref)
    port = _ReadPort({ref.artifact_id: data})
    replacement = _ReadPort({})
    current_port = [port]
    port.on_read = lambda: current_port.__setitem__(0, replacement)
    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: current_port[0])

    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare((artifact,), facts=facts, authorization_current=lambda: True)
    assert len(port.requests) == 1

    port.requests.clear()
    port.on_read = None
    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: port)
    checks = iter((True, False))
    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare((artifact,), facts=facts, authorization_current=lambda: next(checks))
    assert port.requests == []

    assert await preparer.currently_available(lambda: True)
    assert not await CoreArtifactDeliveryPreparer(
        port,
        port_current=lambda: replacement,
    ).currently_available(lambda: True)


@pytest.mark.asyncio
async def test_prepare_rechecks_port_identity_after_awaited_authorization_before_read() -> None:
    facts = _facts()
    data = _png()
    ref = _ref(facts, data)
    artifact = artifact_reference_from_core_v01(ref)
    port = _ReadPort({ref.artifact_id: data})
    replacement = _ReadPort({})
    current_port = [port]

    async def authorization_current() -> bool:
        current_port[0] = replacement
        return True

    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: current_port[0])
    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare(
            (artifact,),
            facts=facts,
            authorization_current=authorization_current,
        )
    assert port.requests == []


@pytest.mark.asyncio
async def test_prepare_times_out_hung_authorization_and_read_without_later_reads() -> None:
    facts = _facts()
    first_data = _png()
    second_data = _png((40, 50, 60))
    first = _ref(facts, first_data, suffix="1")
    second = _ref(facts, second_data, suffix="2")
    artifacts = (
        artifact_reference_from_core_v01(first),
        artifact_reference_from_core_v01(second),
    )
    port = _ReadPort({first.artifact_id: first_data, second.artifact_id: second_data})
    preparer = CoreArtifactDeliveryPreparer(
        port,
        port_current=lambda: port,
        timeout_seconds=0.1,
    )

    async def hung_authorization() -> bool:
        await asyncio.Event().wait()
        return True

    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare(
            artifacts,
            facts=facts,
            authorization_current=hung_authorization,
        )
    assert port.requests == []

    class HungReadPort(_ReadPort):
        async def read_for_delivery(self, request: CoreFileReadRequestV01) -> CoreFileReadReceiptV01:
            self.requests.append(request)
            await asyncio.Event().wait()
            raise AssertionError("hung read must be cancelled")

    hung_port = HungReadPort({first.artifact_id: first_data, second.artifact_id: second_data})
    hung_preparer = CoreArtifactDeliveryPreparer(
        hung_port,
        port_current=lambda: hung_port,
        timeout_seconds=0.1,
    )
    with pytest.raises(CoreArtifactDeliveryError):
        await hung_preparer.prepare(
            artifacts,
            facts=facts,
            authorization_current=lambda: True,
        )
    assert len(hung_port.requests) == 1


@pytest.mark.asyncio
async def test_prepare_runs_duplicate_canonical_validation_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _facts()
    data = _png()
    ref = _ref(facts, data)
    port = _ReadPort({ref.artifact_id: data})
    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: port)
    main_thread = threading.get_ident()
    validation_threads: list[int] = []
    validate = delivery.validate_canonical_png

    def tracked_validate(payload: bytes):
        validation_threads.append(threading.get_ident())
        return validate(payload)

    monkeypatch.setattr(delivery, "validate_canonical_png", tracked_validate)
    await preparer.prepare(
        (artifact_reference_from_core_v01(ref),),
        facts=facts,
        authorization_current=lambda: True,
    )

    assert validation_threads
    assert all(thread_id != main_thread for thread_id in validation_threads)


@pytest.mark.asyncio
async def test_prepare_rejects_receipt_mismatch_hash_change_and_noncanonical_png() -> None:
    facts = _facts()
    data = _png()
    ref = _ref(facts, data)
    artifact = artifact_reference_from_core_v01(ref)
    port = _ReadPort({ref.artifact_id: data})
    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: port)

    port.receipt_factory = lambda request, content: CoreFileReadReceiptV01(
        delivery_id="different-delivery",
        ref=request.ref,
        owner_scope=request.owner_scope,
        content=content,
    )
    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare((artifact,), facts=facts, authorization_current=lambda: True)

    changed = bytearray(data)
    changed[-1] ^= 1
    port.content_by_id[ref.artifact_id] = bytes(changed)
    port.receipt_factory = None
    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare((artifact,), facts=facts, authorization_current=lambda: True)

    noncanonical = b"\x89PNG\r\n\x1a\n" + b"x" * (len(data) - 8)
    noncanonical_ref = _ref(facts, noncanonical, suffix="2")
    port.content_by_id = {noncanonical_ref.artifact_id: noncanonical}
    with pytest.raises(CoreArtifactDeliveryError):
        await preparer.prepare(
            (artifact_reference_from_core_v01(noncanonical_ref),),
            facts=facts,
            authorization_current=lambda: True,
        )


@pytest.mark.asyncio
async def test_prepare_errors_and_repr_do_not_expose_core_identity_or_content() -> None:
    facts = _facts()
    data = _png()
    ref = _ref(facts, data, suffix="secret-ref")
    artifact = artifact_reference_from_core_v01(ref)
    port = _ReadPort({ref.artifact_id: b"unavailable"})
    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: port)

    with pytest.raises(CoreArtifactDeliveryError) as captured:
        await preparer.prepare((artifact,), facts=facts, authorization_current=lambda: True)

    rendered = f"{captured.value!s} {preparer!r}"
    assert ref.artifact_id not in rendered
    assert ref.attachment_id not in rendered
    assert ref.sha256 not in rendered
    assert data.hex() not in rendered


@pytest.mark.asyncio
async def test_prepare_wraps_port_and_async_authorization_exceptions_without_leakage() -> None:
    facts = _facts()
    data = _png()
    ref = _ref(facts, data)
    artifact = artifact_reference_from_core_v01(ref)

    class FailingPort:
        async def read_for_delivery(self, request: CoreFileReadRequestV01) -> CoreFileReadReceiptV01:
            raise RuntimeError(f"secret:{request.ref.artifact_id}")

    port = FailingPort()
    preparer = CoreArtifactDeliveryPreparer(port, port_current=lambda: port)
    with pytest.raises(CoreArtifactDeliveryError) as port_failure:
        await preparer.prepare((artifact,), facts=facts, authorization_current=lambda: True)
    assert ref.artifact_id not in str(port_failure.value)

    async def failed_authorization() -> bool:
        raise RuntimeError(f"secret:{ref.attachment_id}")

    with pytest.raises(CoreArtifactDeliveryError) as authorization_failure:
        await preparer.prepare((artifact,), facts=facts, authorization_current=failed_authorization)
    assert ref.attachment_id not in str(authorization_failure.value)
