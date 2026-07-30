from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from yonerai_discord.modules.video_generation.artifacts import (
    MAX_MP4_BOXES,
    VideoArtifactAuthorizationError,
    VideoArtifactError,
    VideoArtifactStore,
    VideoArtifactValidationError,
    validate_mp4,
)
from yonerai_discord.provider_registry import ArtifactKind


def _box(kind: bytes, payload: bytes = b"", *, extended: bool = False) -> bytes:
    size = len(payload) + (16 if extended else 8)
    return (struct.pack(">I4sQ", 1, kind, size) if extended else struct.pack(">I4s", size, kind)) + payload


def _mp4(*boxes: bytes) -> bytes:
    return _box(b"ftyp", b"isom\0\0\0\0isommp42") + b"".join(boxes or (_box(b"moov"), _box(b"mdat", b"video")))


def _store(tmp_path: Path, **kwargs: object) -> VideoArtifactStore:
    root = tmp_path / "videos"
    root.mkdir()
    return VideoArtifactStore(root, **kwargs)


def test_validate_accepts_bounded_mp4_with_32_and_64_bit_boxes() -> None:
    data = _mp4(_box(b"moov", _box(b"trak", _box(b"mdia"))), _box(b"mdat", b"video", extended=True))
    assert validate_mp4(data).data == data


@pytest.mark.parametrize(
    "data,match",
    [
        (_box(b"ftyp", b"zzzz\0\0\0\0zzzz") + _box(b"moov") + _box(b"mdat"), "compatible"),
        (_mp4(_box(b"moov") + _box(b"moov") + _box(b"mdat")), "exactly one"),
        (_mp4(_box(b"moov")), "mdat"),
        (_mp4(_box(b"moov", _box(b"udta")) + _box(b"mdat", b"video")), "metadata"),
        (_mp4(_box(b"trak", _box(b"moov")), _box(b"mdat", b"video")), "top-level"),
        (_mp4(_box(b"moov"), _box(b"mdat")), "must not be empty"),
    ],
)
def test_validate_rejects_required_structure_and_privacy_boxes(data: bytes, match: str) -> None:
    with pytest.raises(VideoArtifactValidationError, match=match):
        validate_mp4(data)


def test_validate_rejects_zero_size_overflow_trailing_and_box_flood() -> None:
    zero = _box(b"ftyp", b"isom\0\0\0\0isom") + struct.pack(">I4s", 0, b"mdat")
    overflow = _box(b"ftyp", b"isom\0\0\0\0isom") + struct.pack(">I4s", 99, b"moov")
    flood = _mp4(*(_box(b"free") for _ in range(MAX_MP4_BOXES + 1)))
    for data in (zero, overflow, _mp4() + b"x", flood):
        with pytest.raises(VideoArtifactValidationError):
            validate_mp4(data)


@pytest.mark.parametrize(
    "kind",
    [
        b"skip",
        b"uuid",
        b"wide",
        b"Xtra",
        b"meta",
        b"ilst",
        b"loci",
        b"\xa9nam",
        b"\xa9ART",
        b"\xa9alb",
        b"\xa9day",
        b"\xa9gen",
        b"\xa9wrt",
        b"\xa9com",
        b"cprt",
        b"desc",
        b"name",
        b"url ",
        b"urn ",
    ],
)
def test_validate_rejects_opaque_or_metadata_boxes_at_every_depth(kind: bytes) -> None:
    data = _mp4(_box(b"moov", _box(b"trak", _box(kind, b"private"))), _box(b"mdat", b"video"))

    with pytest.raises(VideoArtifactValidationError, match="opaque or metadata"):
        validate_mp4(data)


def test_validate_rejects_duplicate_ftyp_and_non_media_top_level_boxes() -> None:
    duplicate_ftyp = _mp4(_box(b"ftyp", b"isom\0\0\0\0isom"), _box(b"moov"), _box(b"mdat", b"video"))
    unsupported = _mp4(_box(b"moov"), _box(b"junk", b"private"), _box(b"mdat", b"video"))

    with pytest.raises(VideoArtifactValidationError, match="exactly one top-level ftyp"):
        validate_mp4(duplicate_ftyp)
    with pytest.raises(VideoArtifactValidationError, match="unsupported top-level"):
        validate_mp4(unsupported)


def test_validate_accepts_only_bounded_self_contained_data_reference() -> None:
    self_contained_url = _box(b"url ", b"\0\0\0\1")
    accepted = _mp4(
        _box(b"moov", _box(b"dinf", _box(b"dref", b"\0\0\0\0\0\0\0\1" + self_contained_url))),
        _box(b"mdat", b"video"),
    )
    assert validate_mp4(accepted).data == accepted

    external_url = _box(b"url ", b"\0\0\0\0https://example.invalid/video")
    variants = (
        _box(b"dref", b"\0\0\0\0\0\0\0\1" + external_url),
        _box(b"dref", b"\0\0\0\0\0\0\0\2" + self_contained_url + self_contained_url),
        _box(b"dref", b"\0\0\0\0\0\0\0\1" + self_contained_url + b"x"),
    )
    for dref in variants:
        with pytest.raises(VideoArtifactValidationError, match="dref"):
            validate_mp4(_mp4(_box(b"moov", _box(b"dinf", dref)), _box(b"mdat", b"video")))


def test_validate_accepts_only_empty_top_level_free_and_bitexact_ffmpeg_metadata_shape() -> None:
    self_contained_url = _box(b"url ", b"\0\0\0\1")
    hdlr = _box(
        b"hdlr",
        b"\0\0\0\0" + b"\0\0\0\0" + b"mdir" + b"appl" + (b"\0" * 8) + b"\0",
    )
    bitexact_udta = _box(
        b"udta",
        _box(b"meta", b"\0\0\0\0" + hdlr + _box(b"ilst")),
    )
    stripped_moov = _box(
        b"moov",
        _box(b"mvhd")
        + _box(
            b"trak",
            _box(
                b"mdia",
                _box(
                    b"minf",
                    _box(
                        b"dinf",
                        _box(b"dref", b"\0\0\0\0\0\0\0\1" + self_contained_url),
                    ),
                ),
            ),
        )
        + bitexact_udta,
    )
    normal_order = _mp4(_box(b"free"), _box(b"mdat", b"video"), stripped_moov)
    faststart_order = _mp4(stripped_moov, _box(b"free"), _box(b"mdat", b"video"))
    for accepted in (normal_order, faststart_order):
        assert validate_mp4(accepted).data == accepted

    normal_metadata_udta = _box(
        b"udta",
        _box(
            b"meta",
            b"\0\0\0\0"
            + hdlr
            + _box(
                b"ilst",
                _box(b"\xa9too", _box(b"data", b"Lavf private metadata")),
            ),
        ),
    )
    for rejected in (
        _mp4(_box(b"free", b"\0"), _box(b"moov"), _box(b"mdat", b"video")),
        _mp4(_box(b"free"), _box(b"free"), _box(b"moov"), _box(b"mdat", b"video")),
        _mp4(_box(b"moov", _box(b"free")), _box(b"mdat", b"video")),
        _mp4(_box(b"moov", normal_metadata_udta), _box(b"mdat", b"video")),
    ):
        with pytest.raises(VideoArtifactValidationError):
            validate_mp4(rejected)


def test_store_roundtrips_opaque_bound_ref_and_cleans_orphans(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    (root / "vid-stale.mp4").write_bytes(_mp4())
    (root / ".video-0123456789abcdef.tmp").write_bytes(b"stale")
    store = VideoArtifactStore(root)
    assert not tuple(root.iterdir())
    ref = store.put_mp4(_mp4(), request_binding="request-1", artifact_id="vid-fixed")
    assert ref.kind is ArtifactKind.VIDEO
    assert ref.media_type == "video/mp4"
    assert store.read_mp4(ref, request_binding="request-1") == _mp4()
    assert (root / "vid-fixed.mp4").exists()


def test_store_rejects_path_escape_missing_root_and_cross_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(VideoArtifactError, match="root is unavailable"):
        VideoArtifactStore(tmp_path / "missing")
    store = _store(tmp_path)
    with pytest.raises(VideoArtifactValidationError, match="path segment"):
        store.put_mp4(_mp4(), request_binding="request-1", artifact_id="../escape")
    ref = store.put_mp4(_mp4(), request_binding="request-1")
    original_open = os.open
    calls = 0

    def counted_open(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", counted_open)
    with pytest.raises(VideoArtifactAuthorizationError, match="binding"):
        store.read_mp4(ref, request_binding="request-2")
    assert calls == 0


def test_read_checks_integrity_authorization_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    store = VideoArtifactStore(root)
    ref = store.put_mp4(_mp4(), request_binding="request-1", artifact_id="vid-safe")
    decisions = iter((True, False))
    with pytest.raises(VideoArtifactAuthorizationError):
        store.read_mp4(ref, request_binding="request-1", read_allowed=lambda: next(decisions))
    (root / "vid-safe.mp4").write_bytes(_mp4(_box(b"moov", _box(b"loci")), _box(b"mdat")))
    with pytest.raises(VideoArtifactValidationError, match="integrity"):
        store.read_mp4(ref, request_binding="request-1")


def test_eviction_updates_state_only_after_successful_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    store = VideoArtifactStore(root, max_artifacts=1)
    first = store.put_mp4(_mp4(), request_binding="request-1", artifact_id="vid-first")
    original_unlink = Path.unlink

    def fail_unlink(path: Path, *args, **kwargs):
        if path.name == "vid-first.mp4":
            raise PermissionError("locked")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(PermissionError, match="locked"):
        store.put_mp4(_mp4(), request_binding="request-2", artifact_id="vid-second")
    assert store.read_mp4(first, request_binding="request-1")
    assert not (root / "vid-second.mp4").exists()
