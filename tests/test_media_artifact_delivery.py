from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import yonerai_discord.modules.media_pipeline.delivery as delivery

from yonerai_discord.modules.ai.orchestration import PlanArtifactOutput
from yonerai_discord.modules.media_pipeline import (
    ArtifactKind,
    ArtifactScope,
    MediaArtifactStore,
    MediaPipelineService,
    QrEncodeRequest,
)
from yonerai_discord.modules.media_pipeline.delivery import (
    MediaArtifactDeliveryPreparer,
    MediaDeliveryError,
    PreparedMediaAttachment,
)


def _scope(request_id: str = "request-1") -> ArtifactScope:
    return ArtifactScope(request_id=request_id, guild_id=10, channel_id=20, user_id=30)


def _store(tmp_path: Path, name: str = "artifacts") -> MediaArtifactStore:
    root = tmp_path / name
    root.mkdir()
    return MediaArtifactStore(root)


def _output(store: MediaArtifactStore, scope: ArtifactScope, step_id: str = "step-1") -> PlanArtifactOutput:
    artifact = (
        MediaPipelineService(store)
        .qr_encode(
            QrEncodeRequest(scope=scope, payload=f"https://example.invalid/{step_id}"),
            commit_check=lambda: True,
        )
        .artifact
    )
    return PlanArtifactOutput(step_id=step_id, action_id="qr.encode", artifact=artifact)


def test_prepare_returns_only_code_owned_canonical_png_attachments(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = _scope()
    outputs = (_output(store, scope, "step-1"), _output(store, scope, "step-2"))
    preparer = MediaArtifactDeliveryPreparer(store, store_current=lambda: store)
    checks = 0

    def authorization_current() -> bool:
        nonlocal checks
        checks += 1
        return True

    prepared = preparer.prepare(outputs, scope=scope, authorization_current=authorization_current)

    assert [item.filename for item in prepared] == ["media-01.png", "media-02.png"]
    assert all(item.media_type == "image/png" for item in prepared)
    assert all(item.kind is ArtifactKind.QR_CODE for item in prepared)
    assert all(item.data.startswith(b"\x89PNG\r\n\x1a\n") for item in prepared)
    assert "mp-" not in repr(prepared)
    assert checks == 5  # read前・read後を各artifact、最後の返却直前。


def test_prepare_supports_bounded_markdown_with_code_owned_filename(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = _scope("markdown-delivery")
    document = store.commit_markdown(
        "# 比較\n\n| A | B |\n|---|---|\n| 1 | 2 |",
        scope=scope,
        recipe_digest=hashlib.sha256(b"comparison-table-v1").hexdigest(),
        commit_check=lambda: True,
    )
    output = PlanArtifactOutput(step_id="compare", action_id="artifact.table.create", artifact=document)
    prepared = MediaArtifactDeliveryPreparer(store, store_current=lambda: store).prepare(
        (output,),
        scope=scope,
        authorization_current=lambda: True,
    )

    assert len(prepared) == 1
    assert prepared[0].filename == "media-01.md"
    assert prepared[0].media_type == "text/markdown; charset=utf-8"
    assert prepared[0].kind is ArtifactKind.DOCUMENT
    assert (prepared[0].width, prepared[0].height) == (0, 0)
    assert prepared[0].data.decode() == "# 比較\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    assert document.artifact_id not in repr(prepared)
    assert document.content_digest not in repr(prepared)


@pytest.mark.parametrize("outputs", [(), ("not-output",)])
def test_prepare_rejects_invalid_output_contract_before_reads(tmp_path: Path, outputs: tuple[object, ...]) -> None:
    store = _store(tmp_path)
    preparer = MediaArtifactDeliveryPreparer(store, store_current=lambda: store)

    with pytest.raises(MediaDeliveryError):
        preparer.prepare(outputs, scope=_scope(), authorization_current=lambda: True)  # type: ignore[arg-type]


def test_prepare_rejects_duplicate_and_cross_scope_refs_before_reads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = _scope()
    first = _output(store, scope)
    duplicate = PlanArtifactOutput(step_id="step-2", action_id="qr.encode", artifact=first.artifact)
    preparer = MediaArtifactDeliveryPreparer(store, store_current=lambda: store)

    with pytest.raises(MediaDeliveryError):
        preparer.prepare((first, duplicate), scope=scope, authorization_current=lambda: True)
    with pytest.raises(MediaDeliveryError):
        preparer.prepare((first,), scope=_scope("request-2"), authorization_current=lambda: True)


def test_prepare_fails_closed_when_authorization_changes_after_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = _scope()
    checks = iter((True, False))
    preparer = MediaArtifactDeliveryPreparer(store, store_current=lambda: store)

    with pytest.raises(MediaDeliveryError):
        preparer.prepare((_output(store, scope),), scope=scope, authorization_current=lambda: next(checks))


def test_prepare_fails_closed_when_store_identity_changes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    replacement = _store(tmp_path, "replacement")
    scope = _scope()
    preparer = MediaArtifactDeliveryPreparer(store, store_current=lambda: replacement)

    with pytest.raises(MediaDeliveryError):
        preparer.prepare((_output(store, scope),), scope=scope, authorization_current=lambda: True)


def test_prepare_fails_closed_when_store_identity_changes_after_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    replacement = _store(tmp_path, "replacement")
    scope = _scope()
    stores = iter((store, replacement))
    preparer = MediaArtifactDeliveryPreparer(store, store_current=lambda: next(stores))

    with pytest.raises(MediaDeliveryError):
        preparer.prepare((_output(store, scope),), scope=scope, authorization_current=lambda: True)


def test_prepare_revalidates_returned_content_before_exposure(tmp_path: Path) -> None:
    class CorruptReadStore(MediaArtifactStore):
        def read_png(self, ref, *, scope):  # type: ignore[no-untyped-def]
            return b"not a canonical PNG"

    original = _store(tmp_path, "corrupt")
    scope = _scope()
    output = _output(original, scope)
    store = CorruptReadStore(tmp_path / "corrupt")
    preparer = MediaArtifactDeliveryPreparer(store, store_current=lambda: store)

    with pytest.raises(MediaDeliveryError):
        preparer.prepare((output,), scope=scope, authorization_current=lambda: True)


def test_prepare_enforces_total_byte_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    scope = _scope()
    output = _output(store, scope)
    preparer = MediaArtifactDeliveryPreparer(store, store_current=lambda: store)
    monkeypatch.setattr(delivery, "MAX_PREPARED_MEDIA_BYTES", 1)

    with pytest.raises(MediaDeliveryError):
        preparer.prepare((output,), scope=scope, authorization_current=lambda: True)


def test_prepared_attachment_direct_construction_keeps_transport_contract(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = _scope()
    artifact = _output(store, scope).artifact
    data = store.read_png(artifact, scope=scope)

    prepared = PreparedMediaAttachment(
        filename="media-01.png",
        data=data,
        media_type="image/png",
        kind=artifact.kind,
        width=artifact.width,
        height=artifact.height,
    )

    assert prepared.filename == "media-01.png"
    with pytest.raises(ValueError):
        PreparedMediaAttachment(
            filename=f"media-{artifact.artifact_id}.png",
            data=data,
            media_type="image/png",
            kind=artifact.kind,
            width=artifact.width,
            height=artifact.height,
        )
    with pytest.raises(ValueError):
        PreparedMediaAttachment(
            filename="media-02.png",
            data=b"not a PNG",
            media_type="image/png",
            kind=artifact.kind,
            width=artifact.width,
            height=artifact.height,
        )
    with pytest.raises(ValueError):
        PreparedMediaAttachment(
            filename="media-03.png",
            data=data,
            media_type="image/png",
            kind=artifact.kind,
            width=artifact.width + 1,
            height=artifact.height,
        )


def test_prepare_does_not_expose_artifact_identity_in_fail_closed_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    scope = _scope()
    output = _output(store, scope)
    preparer = MediaArtifactDeliveryPreparer(store, store_current=lambda: store)

    with pytest.raises(MediaDeliveryError) as captured:
        preparer.prepare((output,), scope=_scope("request-2"), authorization_current=lambda: True)

    assert output.artifact.artifact_id not in str(captured.value)
    assert output.artifact.content_digest not in str(captured.value)
