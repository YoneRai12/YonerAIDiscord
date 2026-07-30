from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

import yonerai_discord.modules.music.imports as music_imports
from yonerai_discord.modules.music.imports import (
    ImportedMusicWav,
    MusicImportStore,
    MusicImportStoreError,
)


def _wav(*, seconds: int = 1) -> bytes:
    sample_rate = 44_100
    pcm = b"\0" * (sample_rate * seconds * 2)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def _store(tmp_path: Path) -> tuple[MusicImportStore, Path]:
    root = tmp_path / "imports"
    root.mkdir(parents=True)
    return MusicImportStore(root), root


def test_put_wav_is_content_addressed_deduplicated_and_receipt_is_opaque(tmp_path: Path) -> None:
    store, root = _store(tmp_path)

    first, created = store.put_wav(_wav())
    repeated, repeated_created = store.put_wav(_wav())

    assert created is True
    assert repeated_created is False
    assert repeated.path == first.path == root / first.content_sha256 / "audio.wav"
    assert first.path.read_bytes() == _wav()
    rendered = repr(first)
    assert str(root) not in rendered
    assert first.content_sha256 not in rendered
    assert "RIFF" not in rendered


def test_precreated_root_and_invalid_pcm_are_required_without_residue(tmp_path: Path) -> None:
    with pytest.raises(MusicImportStoreError, match="root"):
        MusicImportStore(tmp_path / "missing")

    store, root = _store(tmp_path)
    with pytest.raises(MusicImportStoreError, match="invalid"):
        store.put_wav(b"not-a-wav")
    assert tuple(root.iterdir()) == ()


def test_store_rejects_symlinked_content_directory_without_reading_target(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    receipt, _ = store.put_wav(_wav())
    receipt.path.unlink()
    receipt.path.parent.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, root / receipt.content_sha256, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(MusicImportStoreError, match="storage"):
        store.put_wav(_wav())


def test_store_detects_replaced_root_identity(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    moved = tmp_path / "moved"
    root.rename(moved)
    root.mkdir()

    with pytest.raises(MusicImportStoreError, match="root"):
        store.put_wav(_wav())


def test_failed_atomic_write_removes_only_new_temp_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root = _store(tmp_path)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("blocked")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(MusicImportStoreError, match="storage"):
        store.put_wav(_wav())

    directories = tuple(root.iterdir())
    assert len(directories) == 1 and directories[0].is_dir()
    assert tuple(directories[0].iterdir()) == ()


def test_put_fails_closed_without_touching_replacement_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root = _store(tmp_path)
    original_replace = os.replace
    replacement_sentinel: list[Path] = []

    def swap_directory(source: object, destination: object) -> None:
        directory = Path(destination).parent
        parked = tmp_path / "parked-import"
        directory.rename(parked)
        directory.mkdir()
        sentinel = directory / "sentinel"
        sentinel.write_bytes(b"keep")
        replacement_sentinel.append(sentinel)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", swap_directory)
    with pytest.raises(MusicImportStoreError, match="storage"):
        store.put_wav(_wav())

    assert replacement_sentinel[0].read_bytes() == b"keep"
    assert not (replacement_sentinel[0].parent / "audio.wav").exists()


def test_existing_digest_is_not_replaced_when_corrupt(tmp_path: Path) -> None:
    store, _root = _store(tmp_path)
    receipt, _ = store.put_wav(_wav())
    receipt.path.write_bytes(b"corrupt")

    with pytest.raises(MusicImportStoreError, match="invalid|integrity"):
        store.put_wav(_wav())
    assert receipt.path.read_bytes() == b"corrupt"


def test_cleanup_only_removes_current_unreferenced_receipt(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    receipt, _ = store.put_wav(_wav())

    assert store.discard_if_unreferenced(receipt, 1) is False
    assert receipt.path.is_file()
    assert store.discard_if_unreferenced(receipt, 0) is True
    assert receipt.path.parent.is_dir()
    assert not receipt.path.exists()
    assert store.discard_if_unreferenced(receipt, 0) is False


def test_cleanup_fails_closed_before_opening_swapped_parent_symlink(tmp_path: Path) -> None:
    store, _root = _store(tmp_path)
    receipt, _ = store.put_wav(_wav())
    outside = tmp_path / "outside-cleanup"
    outside.mkdir()
    external_audio = outside / "audio.wav"
    external_audio.write_bytes(_wav())
    directory = receipt.path.parent
    parked = tmp_path / "parked-before-cleanup"
    directory.rename(parked)
    try:
        os.symlink(outside, directory, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(MusicImportStoreError, match="receipt|storage"):
        store.discard_if_unreferenced(receipt, 0)
    assert external_audio.read_bytes() == _wav()
    assert (parked / "audio.wav").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle deletion is the production transport")
def test_windows_handle_delete_keeps_external_symlink_target_intact_on_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root = _store(tmp_path)
    receipt, _ = store.put_wav(_wav())
    outside = tmp_path / "outside"
    outside.mkdir()
    external_audio = outside / "audio.wav"
    external_audio.write_bytes(_wav())
    original_set = music_imports._windows_set_file_information

    def swap_parent_then_delete(*args: object) -> object:
        directory = receipt.path.parent
        parked = tmp_path / "parked-handle-delete"
        try:
            directory.rename(parked)
        except PermissionError:
            # The DELETE-capable file handle itself blocks this parent swap.
            return original_set(*args)
        try:
            os.symlink(outside, directory, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        return original_set(*args)

    monkeypatch.setattr(music_imports, "_windows_set_file_information", swap_parent_then_delete)

    assert store.discard_if_unreferenced(receipt, 0) is True
    assert external_audio.read_bytes() == _wav()
    assert not (tmp_path / "parked-handle-delete" / "audio.wav").exists()


def test_cleanup_rejects_foreign_or_tampered_receipts(tmp_path: Path) -> None:
    store, _root = _store(tmp_path)
    other, _other_root = _store(tmp_path / "other-parent")
    receipt, _ = store.put_wav(_wav())
    foreign, _ = other.put_wav(_wav())

    with pytest.raises(MusicImportStoreError, match="receipt"):
        store.discard_if_unreferenced(foreign, 0)
    with pytest.raises(ValueError, match="reference_count"):
        store.discard_if_unreferenced(receipt, True)
    assert isinstance(receipt, ImportedMusicWav)
