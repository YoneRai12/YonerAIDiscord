from __future__ import annotations

import io
import inspect
import os
import random
import struct
from pathlib import Path

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import yonerai_discord.modules.media_pipeline.artifacts as media_artifacts

from yonerai_discord.modules.media_pipeline import (
    ArtifactKind,
    ArtifactRef,
    ArtifactScope,
    ComposeGridRequest,
    MAX_IMAGE_PIXELS,
    MAX_MARKDOWN_BYTES,
    MAX_PNG_BYTES,
    MAX_QR_PAYLOAD_BYTES,
    MAX_RECIPE_INPUTS,
    MediaArtifactStore,
    MediaAuthorizationError,
    MediaIntegrityError,
    MediaPipelineService,
    MediaValidationError,
    PlaceOnCanvasRequest,
    QrEncodeRequest,
    RgbColor,
    canonicalize_image,
    canonicalize_markdown,
    validate_canonical_markdown,
    validate_canonical_png,
)


_MARKDOWN_RECIPE = "d" * 64


def _scope(
    request_id: str = "request-1",
    *,
    guild_id: int | None = 10,
    channel_id: int = 20,
    user_id: int = 30,
) -> ArtifactScope:
    return ArtifactScope(
        request_id=request_id,
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
    )


def _runtime(tmp_path: Path, name: str = "media") -> tuple[MediaPipelineService, MediaArtifactStore, Path]:
    root = tmp_path / name
    root.mkdir()
    store = MediaArtifactStore(root)
    return MediaPipelineService(store), store, root


def test_store_exposes_only_a_content_free_binding_digest(tmp_path: Path) -> None:
    root = tmp_path / "binding"
    root.mkdir()
    store = MediaArtifactStore(root)
    try:
        assert len(store.binding_digest) == 64
        assert all(character in "0123456789abcdef" for character in store.binding_digest)
        assert str(root) not in store.binding_digest
        reopened = MediaArtifactStore(root)
        try:
            assert store.binding_digest == reopened.binding_digest
        finally:
            reopened.close()
    finally:
        store.close()


def _qr(
    service: MediaPipelineService,
    scope: ArtifactScope,
    payload: str = "https://example.invalid/yonerai",
    *,
    scale: int = 8,
):
    return service.qr_encode(
        QrEncodeRequest(scope=scope, payload=payload, scale=scale),
        commit_check=lambda: True,
    ).artifact


def _open(store: MediaArtifactStore, ref: ArtifactRef, scope: ArtifactScope) -> Image.Image:
    data = store.read_png(ref, scope=scope)
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def test_markdown_document_round_trips_across_store_restart(tmp_path: Path) -> None:
    root = tmp_path / "documents"
    root.mkdir()
    scope = _scope("document-request")
    store = MediaArtifactStore(root)
    ref = store.commit_markdown(
        "# 比較表\r\n\r\n| 項目 | 値 |\r\n|---|---|\r\n| API | JSON |",
        scope=scope,
        recipe_digest=_MARKDOWN_RECIPE,
        commit_check=lambda: True,
    )
    expected = "# 比較表\n\n| 項目 | 値 |\n|---|---|\n| API | JSON |\n".encode()

    assert ref.kind is ArtifactKind.DOCUMENT
    assert (ref.width, ref.height) == (0, 0)
    assert store.read_markdown(ref, scope=scope) == expected
    assert ref.artifact_id not in repr(ref)
    assert "比較表" not in repr(ref)
    store.close()

    reopened = MediaArtifactStore(root)
    try:
        assert reopened.read_markdown(ref, scope=scope) == expected
        with pytest.raises(MediaAuthorizationError):
            reopened.read_markdown(ref, scope=_scope("other-request"))
        with pytest.raises(MediaValidationError):
            reopened.read_png(ref, scope=scope)
    finally:
        reopened.close()


def test_markdown_canonical_contract_rejects_unsafe_or_oversized_text() -> None:
    canonical = canonicalize_markdown("Cafe\u0301")
    assert canonical.data == "Café\n".encode()
    assert validate_canonical_markdown(canonical.data) == canonical

    for value in ("", "\x00", "x" * (MAX_MARKDOWN_BYTES + 1)):
        with pytest.raises(MediaValidationError):
            canonicalize_markdown(value)
    with pytest.raises(MediaIntegrityError):
        validate_canonical_markdown(b"\xef\xbb\xbf# heading\n")


def _png_chunk_types(data: bytes) -> tuple[bytes, ...]:
    cursor = 8
    kinds: list[bytes] = []
    while cursor < len(data):
        length = struct.unpack_from(">I", data, cursor)[0]
        kinds.append(data[cursor + 4 : cursor + 8])
        cursor += 12 + length
    return tuple(kinds)


def _fake_ref(
    scope: ArtifactScope,
    *,
    width: int = 64,
    height: int = 64,
    marker: str = "1",
) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"mp-{marker * 64}",
        scope_digest=scope.digest,
        recipe_digest="2" * 64,
        content_digest="3" * 64,
        kind=ArtifactKind.IMAGE,
        width=width,
        height=height,
        byte_size=1,
    )


def test_qr_is_canonical_square_png_with_preserved_quiet_zone(tmp_path: Path) -> None:
    service, store, _ = _runtime(tmp_path)
    scope = _scope()
    ref = _qr(service, scope)
    data = store.read_png(ref, scope=scope)

    assert ref.kind is ArtifactKind.QR_CODE
    assert ref.width == ref.height
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert _png_chunk_types(data) == (b"IHDR", b"IDAT", b"IEND")
    with _open(store, ref, scope) as image:
        quiet_zone_pixels = 4 * 8
        assert image.mode == "RGB"
        assert image.crop((0, 0, image.width, quiet_zone_pixels)).getextrema() == (
            (255, 255),
            (255, 255),
            (255, 255),
        )
        assert image.crop((0, 0, quiet_zone_pixels, image.height)).getextrema() == (
            (255, 255),
            (255, 255),
            (255, 255),
        )
        assert image.getextrema() == ((0, 255), (0, 255), (0, 255))


def test_canonical_png_strips_input_metadata_and_rejects_metadata_bearing_png() -> None:
    source = Image.new("RGB", (64, 64), (12, 34, 56))
    source.info["icc_profile"] = b"not-a-real-profile"
    metadata = PngInfo()
    metadata.add_text("Comment", "must not survive canonicalization")
    try:
        canonical = canonicalize_image(source)
        metadata_buffer = io.BytesIO()
        source.save(
            metadata_buffer,
            format="PNG",
            optimize=False,
            compress_level=9,
            pnginfo=metadata,
        )
    finally:
        source.close()

    assert _png_chunk_types(canonical.data) == (b"IHDR", b"IDAT", b"IEND")
    metadata_png = metadata_buffer.getvalue()
    assert b"iCCP" in _png_chunk_types(metadata_png)
    assert b"tEXt" in _png_chunk_types(metadata_png)
    with pytest.raises(MediaIntegrityError, match="non-canonical"):
        validate_canonical_png(metadata_png)


def test_same_scope_recipe_is_deterministic_and_other_scope_gets_a_different_ref(tmp_path: Path) -> None:
    service, store, root = _runtime(tmp_path)
    first_scope = _scope()
    checks = 0

    def current() -> bool:
        nonlocal checks
        checks += 1
        return True

    request = QrEncodeRequest(scope=first_scope, payload="deterministic")
    first = service.qr_encode(request, commit_check=current).artifact
    second = service.qr_encode(request, commit_check=current).artifact
    other = service.qr_encode(
        QrEncodeRequest(scope=_scope("request-2"), payload="deterministic"),
        commit_check=current,
    ).artifact

    assert first == second
    assert first.content_digest == other.content_digest
    assert first.artifact_id != other.artifact_id
    assert checks == 3
    assert len(tuple(root.glob("*.png"))) == 2
    assert store.read_png(first, scope=first_scope) == store.read_png(second, scope=first_scope)


def test_4096_square_canvas_is_allowed_without_resizing_source(tmp_path: Path) -> None:
    service, store, _ = _runtime(tmp_path)
    scope = _scope()
    source = _qr(service, scope, scale=4)
    result = service.image_place_on_canvas(
        PlaceOnCanvasRequest(
            scope=scope,
            source=source,
            canvas_width=4096,
            canvas_height=4096,
            background=RgbColor(255, 255, 255),
        ),
        commit_check=lambda: True,
    )

    assert (result.artifact.width, result.artifact.height) == (4096, 4096)
    assert len(store.read_png(result.artifact, scope=scope)) <= MAX_PNG_BYTES


def test_3840x2160_black_canvas_keeps_qr_white_panel_and_quiet_zone(tmp_path: Path) -> None:
    service, store, _ = _runtime(tmp_path)
    scope = _scope()
    source = _qr(service, scope, scale=4)
    result = service.image_place_on_canvas(
        PlaceOnCanvasRequest(
            scope=scope,
            source=source,
            canvas_width=3840,
            canvas_height=2160,
            background=RgbColor(0, 0, 0),
        ),
        commit_check=lambda: True,
    )
    x = (3840 - source.width) // 2
    y = (2160 - source.height) // 2

    with _open(store, result.artifact, scope) as image:
        assert image.getpixel((0, 0)) == (0, 0, 0)
        assert image.getpixel((x, y)) == (255, 255, 255)
        assert image.getpixel((x + (4 * 4) - 1, y + (4 * 4) - 1)) == (255, 255, 255)


def test_qr_variants_compose_into_a_bounded_grid(tmp_path: Path) -> None:
    service, store, _ = _runtime(tmp_path)
    scope = _scope()
    variants = tuple(_qr(service, scope, f"variant-{index}", scale=4) for index in range(4))
    result = service.image_compose_grid(
        ComposeGridRequest(
            scope=scope,
            sources=variants,
            canvas_width=1024,
            canvas_height=1024,
            columns=2,
            background=RgbColor(32, 32, 32),
            padding=24,
            gap=16,
        ),
        commit_check=lambda: True,
    )

    assert result.inputs == variants
    assert (result.artifact.width, result.artifact.height) == (1024, 1024)
    with _open(store, result.artifact, scope) as image:
        assert image.getpixel((0, 0)) == (32, 32, 32)
        assert image.getextrema() == ((0, 255), (0, 255), (0, 255))


def test_grid_contain_preserves_rectangular_input_aspect_ratio(tmp_path: Path) -> None:
    service, store, _ = _runtime(tmp_path)
    scope = _scope()
    qr = _qr(service, scope, scale=2)
    rectangular = service.image_place_on_canvas(
        PlaceOnCanvasRequest(
            scope=scope,
            source=qr,
            canvas_width=300,
            canvas_height=150,
            background=RgbColor(0, 0, 0),
        ),
        commit_check=lambda: True,
    ).artifact
    grid = service.image_compose_grid(
        ComposeGridRequest(
            scope=scope,
            sources=(rectangular,),
            canvas_width=600,
            canvas_height=600,
            columns=1,
            background=RgbColor(0, 0, 255),
            padding=0,
            gap=0,
        ),
        commit_check=lambda: True,
    ).artifact

    with _open(store, grid, scope) as image:
        assert image.getpixel((300, 50)) == (0, 0, 255)
        assert image.getpixel((10, 300)) == (0, 0, 0)


@pytest.mark.parametrize(
    "builder",
    [
        lambda scope: QrEncodeRequest(scope=scope, payload="x", border=3),
        lambda scope: QrEncodeRequest(scope=scope, payload="あ" * (MAX_QR_PAYLOAD_BYTES + 1)),
        lambda scope: PlaceOnCanvasRequest(
            scope=scope,
            source=_fake_ref(scope),
            canvas_width=4097,
            canvas_height=1,
        ),
        lambda scope: PlaceOnCanvasRequest(
            scope=scope,
            source=_fake_ref(scope, width=128, height=128),
            canvas_width=64,
            canvas_height=64,
        ),
    ],
)
def test_payload_dimension_and_layout_limits_fail_before_execution(builder) -> None:
    with pytest.raises(MediaValidationError):
        builder(_scope())


def test_png_byte_limit_rejects_high_entropy_output() -> None:
    width = height = 1700
    source = random.Random(0).randbytes(width * height * 3)
    image = Image.frombytes("RGB", (width, height), source)
    try:
        with pytest.raises(MediaValidationError, match="byte"):
            canonicalize_image(image)
    finally:
        image.close()


def test_recipe_input_count_and_total_input_pixels_are_bounded() -> None:
    scope = _scope()
    small = _fake_ref(scope)
    with pytest.raises(MediaValidationError, match="input count"):
        ComposeGridRequest(
            scope=scope,
            sources=tuple(small for _ in range(MAX_RECIPE_INPUTS + 1)),
            canvas_width=1024,
            canvas_height=1024,
            columns=3,
        )
    consumed = 0

    def unbounded_sources():
        nonlocal consumed
        while True:
            consumed += 1
            yield small

    with pytest.raises(MediaValidationError, match="input count"):
        ComposeGridRequest(
            scope=scope,
            sources=unbounded_sources(),  # type: ignore[arg-type]
            canvas_width=1024,
            canvas_height=1024,
            columns=3,
        )
    assert consumed == MAX_RECIPE_INPUTS + 1

    large = _fake_ref(scope, width=4096, height=4096)
    assert large.width * large.height == MAX_IMAGE_PIXELS
    with pytest.raises(MediaValidationError, match="input pixels"):
        ComposeGridRequest(
            scope=scope,
            sources=(large, large),
            canvas_width=1024,
            canvas_height=1024,
            columns=2,
        )
    with pytest.raises(MediaValidationError, match="byte_size"):
        ArtifactRef(
            artifact_id="mp-" + ("4" * 64),
            scope_digest=scope.digest,
            recipe_digest="5" * 64,
            content_digest="6" * 64,
            kind=ArtifactKind.IMAGE,
            width=64,
            height=64,
            byte_size=MAX_PNG_BYTES + 1,
        )


def test_scope_mismatch_is_rejected_before_file_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, _ = _runtime(tmp_path)
    first_scope = _scope()
    ref = _qr(service, first_scope)
    open_calls = 0
    original_open = os.open

    def counted_open(*args, **kwargs):
        nonlocal open_calls
        open_calls += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", counted_open)
    changed_scopes = (
        _scope("request-2"),
        _scope(guild_id=11),
        _scope(channel_id=21),
        _scope(user_id=31),
    )
    for changed_scope in changed_scopes:
        with pytest.raises(MediaAuthorizationError, match="scope"):
            store.read_png(ref, scope=changed_scope)
    assert open_calls == 0


def test_tamper_is_rejected_by_digest_before_decode(tmp_path: Path) -> None:
    service, store, root = _runtime(tmp_path)
    scope = _scope()
    ref = _qr(service, scope)
    (root / f"{ref.artifact_id}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"tampered")

    with pytest.raises(MediaIntegrityError, match="digest"):
        store.read_png(ref, scope=scope)


def test_traversal_symlink_network_drive_and_path_or_delete_apis_are_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(MediaValidationError, match="opaque"):
        ArtifactRef(
            artifact_id="../outside.png",
            scope_digest="1" * 64,
            recipe_digest="2" * 64,
            content_digest="3" * 64,
            kind=ArtifactKind.IMAGE,
            width=64,
            height=64,
            byte_size=1,
        )
    with pytest.raises(MediaValidationError, match="absolute"):
        MediaArtifactStore(Path("relative-artifacts"))

    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        os.symlink(target, linked, target_is_directory=True)
    except OSError:
        pass
    else:
        with pytest.raises(MediaIntegrityError, match="symlink|redirected"):
            MediaArtifactStore(linked)

    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setattr(media_artifacts, "_windows_drive_type", lambda path: 4)
    with pytest.raises(MediaValidationError, match="mapped network"):
        MediaArtifactStore(root)
    monkeypatch.setattr(media_artifacts, "_windows_drive_type", lambda path: None)
    store = MediaArtifactStore(root)
    assert all(not hasattr(store, name) for name in ("delete", "move", "path_for", "read_path", "open_path"))
    assert "path" not in inspect.signature(store.read_png).parameters


def test_commit_check_runs_immediately_before_publish_and_failure_leaves_no_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, root = _runtime(tmp_path)
    scope = _scope()
    with pytest.raises(MediaAuthorizationError, match="authorization"):
        service.qr_encode(
            QrEncodeRequest(scope=scope, payload="denied"),
            commit_check=lambda: False,
        )
    assert tuple(root.iterdir()) == ()

    allowed = False
    original_link = os.link

    def checked_link(source, target, *args, **kwargs):
        assert allowed is True
        return original_link(source, target, *args, **kwargs)

    def current() -> bool:
        nonlocal allowed
        allowed = True
        return True

    monkeypatch.setattr(os, "link", checked_link)
    result = service.qr_encode(
        QrEncodeRequest(scope=scope, payload="allowed"),
        commit_check=current,
    )
    assert result.artifact.artifact_id.startswith("mp-")
    assert tuple(root.glob(".*.tmp")) == ()


def test_commit_time_temp_replacement_fails_closed_without_deleting_replacement(tmp_path: Path) -> None:
    service, _, root = _runtime(tmp_path)
    scope = _scope()

    def replace_owned_temp() -> bool:
        temporary = next(root.glob(".mp-*.tmp"))
        temporary.unlink()
        temporary.write_bytes(b"replacement-must-remain")
        return True

    with pytest.raises(MediaIntegrityError, match="identity"):
        service.qr_encode(
            QrEncodeRequest(scope=scope, payload="replace-temp"),
            commit_check=replace_owned_temp,
        )

    targets = tuple(root.glob("*.png"))
    assert len(targets) == 1
    assert targets[0].read_bytes() == b"replacement-must-remain"
    suspicious = tuple(root.glob(".mp-*.tmp"))
    assert len(suspicious) == 1
    assert suspicious[0].read_bytes() == b"replacement-must-remain"


def test_publish_time_target_replacement_fails_closed_without_deleting_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, root = _runtime(tmp_path)
    scope = _scope()
    original_link = os.link

    def replace_target_after_link(source, target, *args, **kwargs):
        original_link(source, target, *args, **kwargs)
        Path(target).unlink()
        Path(target).write_bytes(b"foreign-target-must-remain")

    monkeypatch.setattr(os, "link", replace_target_after_link)
    with pytest.raises(MediaIntegrityError, match="identity"):
        service.qr_encode(
            QrEncodeRequest(scope=scope, payload="replace-target"),
            commit_check=lambda: True,
        )

    targets = tuple(root.glob("*.png"))
    assert len(targets) == 1
    assert targets[0].read_bytes() == b"foreign-target-must-remain"
    assert tuple(root.glob(".mp-*.tmp")) == ()
