from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from yonerai_discord.modules.music_generation.artifacts import (
    MusicArtifactAuthorizationError,
    MusicArtifactError,
    MusicArtifactStore,
    MusicArtifactValidationError,
    validate_wav,
)
from yonerai_discord.provider_registry import ArtifactKind


def _wav(*, channels: int = 1, rate: int = 44_100, seconds: int = 1) -> bytes:
    block_align = channels * 2
    pcm = b"\0" * (rate * seconds * block_align)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block_align, block_align, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def _store(tmp_path: Path, **kwargs: object) -> MusicArtifactStore:
    root = tmp_path / "music"
    root.mkdir()
    return MusicArtifactStore(root, **kwargs)


def test_validate_accepts_exact_pcm_wav_and_reports_duration() -> None:
    valid = _wav(channels=2, rate=48_000, seconds=2)
    parsed = validate_wav(valid)
    assert parsed.data == valid
    assert parsed.duration_seconds == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: b"RF64" + value[4:],
        lambda value: value[:4] + struct.pack("<I", len(value)) + value[8:],
        lambda value: value[:12] + b"data" + value[16:],
        lambda value: value[:36] + b"JUNK" + value[40:],
        lambda value: value + b"x",
    ],
)
def test_validate_rejects_noncanonical_chunk_layout_and_trailing_bytes(mutate) -> None:
    with pytest.raises(MusicArtifactValidationError):
        validate_wav(mutate(_wav()))


@pytest.mark.parametrize("offset,value", [(20, 3), (22, 3), (24, 22_050), (34, 32)])
def test_validate_rejects_non_pcm_formats(offset: int, value: int) -> None:
    data = bytearray(_wav())
    struct.pack_into("<H" if offset in {20, 22, 34} else "<I", data, offset, value)
    with pytest.raises(MusicArtifactValidationError):
        validate_wav(bytes(data))


def test_validate_rejects_bad_rate_alignment_frame_and_duration() -> None:
    bad_rate = bytearray(_wav())
    struct.pack_into("<I", bad_rate, 28, 1)
    bad_frame = _wav()[:-1]
    too_short = _wav()[:44] + b"\0\0"
    for data in (bytes(bad_rate), bad_frame, too_short, _wav(seconds=31)):
        with pytest.raises(MusicArtifactValidationError):
            validate_wav(data)


def test_store_roundtrips_bound_opaque_ref_acl_and_orphan_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    (root / "aud-old.wav").write_bytes(_wav())
    (root / ".audio-0123456789abcdef.tmp").write_bytes(b"old")
    store = MusicArtifactStore(root)
    assert not tuple(root.iterdir())
    ref = store.put_wav(_wav(), request_binding="request-1", artifact_id="aud-fixed")
    assert ref.kind is ArtifactKind.AUDIO and ref.media_type == "audio/wav"
    assert store.read_wav(ref, request_binding="request-1") == _wav()


def test_store_rejects_escape_cross_request_and_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    with pytest.raises(MusicArtifactValidationError, match="path segment"):
        store.put_wav(_wav(), request_binding="one", artifact_id="../escape")
    ref = store.put_wav(_wav(), request_binding="one", artifact_id="aud-one")
    original_open = os.open
    monkeypatch.setattr(os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("opened")))
    with pytest.raises(MusicArtifactAuthorizationError, match="binding"):
        store.read_wav(ref, request_binding="two")
    monkeypatch.setattr(os, "open", original_open)
    (tmp_path / "music" / "aud-one.wav").unlink()
    try:
        os.symlink(tmp_path / "outside.wav", tmp_path / "music" / "aud-one.wav")
    except OSError as exc:
        pytest.skip(f"symlink creation is not available: {exc}")
    with pytest.raises(MusicArtifactError, match="symlink"):
        store.read_wav(ref, request_binding="one")


def test_eviction_only_updates_state_after_successful_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path, max_artifacts=1)
    first = store.put_wav(_wav(), request_binding="one", artifact_id="aud-first")
    original_unlink = Path.unlink

    def fail_unlink(path: Path, *args, **kwargs):
        if path.name == "aud-first.wav":
            raise PermissionError("locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(PermissionError, match="locked"):
        store.put_wav(_wav(), request_binding="two", artifact_id="aud-second")
    assert store.read_wav(first, request_binding="one")
