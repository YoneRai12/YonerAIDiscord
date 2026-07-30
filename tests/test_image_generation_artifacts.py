from __future__ import annotations

import asyncio
import binascii
import hashlib
import os
import struct
import zlib
from pathlib import Path

import pytest

from yonerai_discord.modules.image_generation import artifacts as artifacts_module
from yonerai_discord.modules.image_generation.artifacts import (
    MAX_PNG_CHUNKS,
    MAX_PNG_BYTES,
    ImageArtifactAuthorizationError,
    ImageArtifactError,
    ImageArtifactStore,
    ImageArtifactValidationError,
    canonicalize_png,
)
from yonerai_discord.provider_registry import ArtifactKind, ArtifactRef


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(
    *,
    width: int = 64,
    height: int = 64,
    interlace: int = 0,
    ancillary: tuple[tuple[bytes, bytes], ...] = (),
    raw: bytes | None = None,
) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, interlace)
    scanlines = raw if raw is not None else b"".join(b"\0" + bytes(width * 4) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + b"".join(_chunk(kind, payload) for kind, payload in ancillary)
        + _chunk(b"IDAT", zlib.compress(scanlines))
        + _chunk(b"IEND", b"")
    )


def _chunk_types(data: bytes) -> tuple[bytes, ...]:
    cursor = 8
    result: list[bytes] = []
    while cursor < len(data):
        length = struct.unpack_from(">I", data, cursor)[0]
        kind = data[cursor + 4 : cursor + 8]
        result.append(kind)
        cursor += 12 + length
    return tuple(result)


def _store(tmp_path: Path, name: str = "images") -> ImageArtifactStore:
    root = tmp_path / name
    root.mkdir()
    return ImageArtifactStore(root)


def test_png_is_canonical_and_metadata_is_removed() -> None:
    source = _png(ancillary=((b"tEXt", b"prompt\x00private"), (b"eXIf", b"metadata")))

    first = canonicalize_png(source)
    second = canonicalize_png(first.data)

    assert first.width == 64
    assert first.height == 64
    assert first.data == second.data
    assert first.sha256 == hashlib.sha256(first.data).hexdigest()
    assert _chunk_types(first.data) == (b"IHDR", b"IDAT", b"IEND")


@pytest.mark.parametrize(
    "source,match",
    [
        (_png(width=63), "width"),
        (_png(height=2049), "height"),
        (_png(interlace=1), "non-interlaced"),
        (_png(raw=b"\0"), "length"),
        (_png(raw=b"\x05" + bytes(64 * 4) + b"".join(b"\0" + bytes(64 * 4) for _ in range(63))), "filter"),
    ],
)
def test_png_dimensions_encoding_and_expansion_are_bounded(source: bytes, match: str) -> None:
    with pytest.raises(ImageArtifactValidationError, match=match):
        canonicalize_png(source)


def test_png_rejects_crc_trailing_polyglot_and_oversized_input() -> None:
    valid = _png()
    broken_crc = bytearray(valid)
    broken_crc[-1] ^= 1

    for source in (bytes(broken_crc), valid + b"polyglot", valid + bytes(MAX_PNG_BYTES)):
        with pytest.raises(ImageArtifactValidationError):
            canonicalize_png(source)


def test_png_rejects_an_unbounded_number_of_chunks() -> None:
    source = _png(ancillary=tuple((b"aaAA", b"") for _ in range(MAX_PNG_CHUNKS + 1)))

    with pytest.raises(ImageArtifactValidationError, match="too many chunks"):
        canonicalize_png(source)


def test_store_returns_opaque_ref_and_roundtrips_only_canonical_png(tmp_path: Path) -> None:
    root = tmp_path / "private-images"
    store = _store(tmp_path, "private-images")
    ref = store.put_png(
        _png(ancillary=((b"tEXt", b"ignored"),)),
        request_binding="request-1",
        artifact_id="img-fixed-test",
    )

    assert ref.kind is ArtifactKind.IMAGE
    assert ref.media_type == "image/png"
    assert ref.size_bytes is not None
    assert ref.sha256 is not None
    assert store.read_png(ref, request_binding="request-1") == canonicalize_png(_png()).data
    paths = tuple(root.rglob("*"))
    assert root / f"{ref.artifact_id}.png" in paths
    assert all("prompt" not in path.name and "provider" not in path.name for path in paths)
    if os.name != "nt":
        assert (root / f"{ref.artifact_id}.png").stat().st_mode & 0o777 == 0o600


def test_store_requires_a_precreated_private_root(tmp_path: Path) -> None:
    with pytest.raises(ImageArtifactError, match="root is unavailable"):
        ImageArtifactStore(tmp_path / "missing")


@pytest.mark.parametrize("artifact_id", ["../escape", "a/b", ".", "画像", "img.with-dot"])
def test_artifact_id_is_one_safe_segment(tmp_path: Path, artifact_id: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(ImageArtifactValidationError, match="path segment"):
        store.put_png(
            _png(),
            request_binding="request-1",
            artifact_id=artifact_id,
        )


def test_read_checks_authorization_before_and_after_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = store.put_png(_png(), request_binding="request-1")
    decisions = iter((True, False))

    with pytest.raises(ImageArtifactAuthorizationError, match="authorization"):
        store.read_png(
            ref,
            request_binding="request-1",
            read_allowed=lambda: next(decisions),
        )
    with pytest.raises(ImageArtifactAuthorizationError, match="authorization"):
        store.read_png(ref, request_binding="request-1", read_allowed=lambda: False)


def test_request_binding_rejects_cross_request_ref_without_reading_bytes(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    ref = store.put_png(_png(), request_binding="request-1")
    open_calls = 0
    original_open = os.open

    def counted_open(*args, **kwargs):
        nonlocal open_calls
        open_calls += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", counted_open)
    with pytest.raises(ImageArtifactAuthorizationError, match="binding"):
        store.read_png(ref, request_binding="request-2")
    assert open_calls == 0


def test_flat_root_publish_cannot_be_redirected_by_old_parent_directory_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "images"
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"unchanged")
    root.mkdir()
    real_replace = os.replace

    def swap_old_parent_then_replace(source, target) -> None:
        old_parent = root / "img-race"
        old_parent.mkdir()
        (old_parent / "generated-image.png").write_bytes(b"attacker-controlled")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", swap_old_parent_then_replace)
    store = ImageArtifactStore(root)
    ref = store.put_png(_png(), request_binding="request-1", artifact_id="img-race")

    assert store.read_png(ref, request_binding="request-1") == canonicalize_png(_png()).data
    assert (root / "img-race" / "generated-image.png").read_bytes() == b"attacker-controlled"
    assert outside.read_bytes() == b"unchanged"


def test_binding_is_not_overwritten_and_is_not_recovered_after_restart(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    store = ImageArtifactStore(root)
    ref = store.put_png(_png(), request_binding="request-1", artifact_id="img-binding")

    with pytest.raises(ImageArtifactError, match="collision"):
        store.put_png(_png(), request_binding="request-2", artifact_id="img-binding")
    assert store.read_png(ref, request_binding="request-1")
    restarted = ImageArtifactStore(root)
    assert not (root / f"{ref.artifact_id}.png").exists()
    with pytest.raises(ImageArtifactAuthorizationError, match="binding"):
        restarted.read_png(ref, request_binding="request-1")


def test_store_evicts_the_oldest_artifact_to_keep_file_count_bounded(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    store = ImageArtifactStore(root, max_artifacts=2)
    first = store.put_png(_png(), request_binding="request-1", artifact_id="img-first")
    second = store.put_png(_png(), request_binding="request-2", artifact_id="img-second")
    third = store.put_png(_png(), request_binding="request-3", artifact_id="img-third")

    assert tuple(sorted(path.name for path in root.glob("*.png"))) == ("img-second.png", "img-third.png")
    with pytest.raises(ImageArtifactAuthorizationError, match="binding"):
        store.read_png(first, request_binding="request-1")
    assert store.read_png(second, request_binding="request-2")
    assert store.read_png(third, request_binding="request-3")


def test_failed_eviction_keeps_the_existing_binding_and_quota_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "images"
    root.mkdir()
    store = ImageArtifactStore(root, max_artifacts=1)
    first = store.put_png(_png(), request_binding="request-1", artifact_id="img-first")
    original_unlink = Path.unlink

    def fail_first_unlink(path: Path, *args, **kwargs) -> None:
        if path.name == "img-first.png":
            raise PermissionError("locked")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_unlink)
    with pytest.raises(PermissionError, match="locked"):
        store.put_png(_png(), request_binding="request-2", artifact_id="img-second")

    assert store.read_png(first, request_binding="request-1")
    assert (root / "img-first.png").exists()
    assert not (root / "img-second.png").exists()


def test_expired_artifact_is_rejected_and_reclaims_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr(artifacts_module.time, "monotonic", lambda: now)
    root = tmp_path / "images"
    root.mkdir()
    store = ImageArtifactStore(root, max_artifacts=1, ttl_seconds=1)
    first = store.put_png(_png(), request_binding="request-1", artifact_id="img-expired")

    def expire_after_bytes() -> bool:
        nonlocal now
        now += 2.0
        return True

    with pytest.raises(ImageArtifactError, match="expired"):
        store.read_png(first, request_binding="request-1", read_allowed=expire_after_bytes)
    second = store.put_png(_png(), request_binding="request-2", artifact_id="img-reclaimed")

    assert not (root / "img-expired.png").exists()
    assert store.read_png(second, request_binding="request-2")


def test_expired_artifact_unlink_failure_preserves_binding_and_quota_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(artifacts_module.time, "monotonic", lambda: now)
    root = tmp_path / "images"
    root.mkdir()
    store = ImageArtifactStore(root, max_artifacts=1, ttl_seconds=1)
    first = store.put_png(_png(), request_binding="request-1", artifact_id="img-expired")
    now += 2.0
    original_unlink = Path.unlink

    def fail_expired_unlink(path: Path, *args, **kwargs) -> None:
        if path.name == "img-expired.png":
            raise PermissionError("locked")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_expired_unlink)
    with pytest.raises(PermissionError, match="locked"):
        store.read_png(first, request_binding="request-1")

    assert tuple(store._request_bindings) == ("img-expired",)
    assert store._total_bytes == first.size_bytes
    assert (root / "img-expired.png").exists()


def test_expired_unlink_failure_does_not_block_put_with_spare_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(artifacts_module.time, "monotonic", lambda: now)
    root = tmp_path / "images"
    root.mkdir()
    store = ImageArtifactStore(root, max_artifacts=2, ttl_seconds=1)
    first = store.put_png(_png(), request_binding="request-1", artifact_id="img-expired")
    now += 2.0
    original_unlink = Path.unlink

    def fail_expired_unlink(path: Path, *args, **kwargs) -> None:
        if path.name == "img-expired.png":
            raise PermissionError("locked")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_expired_unlink)
    second = store.put_png(_png(), request_binding="request-2", artifact_id="img-current")

    assert tuple(store._request_bindings) == ("img-expired", "img-current")
    assert store._total_bytes == first.size_bytes + second.size_bytes
    assert (root / "img-current.png").exists()


def test_protection_cleanup_does_not_mask_cancellation_after_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(artifacts_module.time, "monotonic", lambda: now)
    root = tmp_path / "images"
    root.mkdir()
    store = ImageArtifactStore(root, ttl_seconds=1)
    ref = store.put_png(_png(), request_binding="request-1", artifact_id="img-protected")

    def fail_unlink(*args, **kwargs) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(asyncio.CancelledError):
        with store.protect_png(ref, request_binding="request-1"):
            now += 2.0
            raise asyncio.CancelledError

    assert not store._protected_artifacts
    assert tuple(store._request_bindings) == ("img-protected",)


def test_expired_cleanup_revalidates_root_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(artifacts_module.time, "monotonic", lambda: now)
    root = tmp_path / "images"
    root.mkdir()
    store = ImageArtifactStore(root, ttl_seconds=1)
    ref = store.put_png(_png(), request_binding="request-1", artifact_id="img-expired")
    now += 2.0

    def reject_swapped_root(candidate: Path) -> None:
        assert candidate == root
        raise ImageArtifactError("image artifact path cannot contain symlinks")

    def unexpected_unlink(*args, **kwargs) -> None:
        pytest.fail("expiry cleanup unlinked before root revalidation")

    monkeypatch.setattr(artifacts_module, "_assert_no_symlink_ancestor", reject_swapped_root)
    monkeypatch.setattr(Path, "unlink", unexpected_unlink)
    with pytest.raises(ImageArtifactError, match="symlink"):
        store.read_png(ref, request_binding="request-1")

    assert tuple(store._request_bindings) == ("img-expired",)


def test_read_rejects_tamper_and_incomplete_or_wrong_ref(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    store = ImageArtifactStore(root)
    ref = store.put_png(_png(), request_binding="request-1", artifact_id="img-integrity")
    target = root / f"{ref.artifact_id}.png"
    target.write_bytes(_png(ancillary=((b"tEXt", b"changed"),)))

    with pytest.raises(ImageArtifactValidationError, match="integrity"):
        store.read_png(ref, request_binding="request-1")
    wrong_kind = ArtifactRef(
        artifact_id=ref.artifact_id,
        kind=ArtifactKind.DOCUMENT,
        media_type="application/pdf",
    )
    with pytest.raises(ImageArtifactValidationError, match="complete PNG"):
        store.read_png(wrong_kind, request_binding="request-1")


def test_symlink_root_and_artifact_file_are_rejected(tmp_path: Path) -> None:
    target_root = tmp_path / "target"
    target_root.mkdir()
    linked_root = tmp_path / "linked"
    try:
        os.symlink(target_root, linked_root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    with pytest.raises(ImageArtifactError, match="symlink"):
        ImageArtifactStore(linked_root).put_png(_png(), request_binding="request-1")

    root = tmp_path / "real"
    root.mkdir()
    store = ImageArtifactStore(root)
    ref = store.put_png(_png(), request_binding="request-1", artifact_id="img-symlink")
    stored = root / f"{ref.artifact_id}.png"
    replacement = tmp_path / "replacement.png"
    replacement.write_bytes(_png())
    stored.unlink()
    os.symlink(replacement, stored)
    with pytest.raises(ImageArtifactError, match="symlink"):
        store.read_png(ref, request_binding="request-1")
    with pytest.raises(ImageArtifactError, match="symlink"):
        store.discard_png(ref, request_binding="request-1")


def test_discard_png_requires_matching_binding_and_removes_unprotected_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = store.put_png(_png(), request_binding="request-1")

    assert store.discard_png(ref, request_binding="wrong") is False
    assert store.read_png(ref, request_binding="request-1")
    with store.protect_png(ref, request_binding="request-1"):
        assert store.discard_png(ref, request_binding="request-1") is False
    assert store.discard_png(ref, request_binding="request-1") is True
    assert store.discard_png(ref, request_binding="request-1") is False
