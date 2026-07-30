"""Private content-addressed PCM WAV storage for explicit music imports."""

from __future__ import annotations

import os
import secrets
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _WINDOWS_DELETE = 0x00010000
    _WINDOWS_FILE_READ_ATTRIBUTES = 0x00000080
    _WINDOWS_FILE_SHARE_READ = 0x00000001
    _WINDOWS_FILE_SHARE_WRITE = 0x00000002
    _WINDOWS_FILE_SHARE_DELETE = 0x00000004
    _WINDOWS_OPEN_EXISTING = 3
    _WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _WINDOWS_FILE_DISPOSITION_INFO = 4
    _WINDOWS_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _WindowsByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _WindowsFileDispositionInformation(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    _windows_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _windows_create_file = _windows_kernel32.CreateFileW
    _windows_create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _windows_create_file.restype = wintypes.HANDLE
    _windows_get_file_information = _windows_kernel32.GetFileInformationByHandle
    _windows_get_file_information.argtypes = (wintypes.HANDLE, ctypes.POINTER(_WindowsByHandleFileInformation))
    _windows_get_file_information.restype = wintypes.BOOL
    _windows_set_file_information = _windows_kernel32.SetFileInformationByHandle
    _windows_set_file_information.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _windows_set_file_information.restype = wintypes.BOOL
    _windows_close_handle = _windows_kernel32.CloseHandle
    _windows_close_handle.argtypes = (wintypes.HANDLE,)
    _windows_close_handle.restype = wintypes.BOOL

from yonerai_discord.modules.speech_transcription.artifacts import (
    SpeechAudioArtifactError,
    ValidatedWav,
    validate_pcm_wav,
)


_AUDIO_NAME = "audio.wav"
_MAX_STORED_BYTES = 8 * 1024 * 1024


class MusicImportStoreError(RuntimeError):
    """A private imported WAV could not be stored or cleaned safely."""


@dataclass(frozen=True, slots=True)
class ImportedMusicWav:
    """Opaque receipt for one store-owned PCM WAV file."""

    size_bytes: int
    channels: int
    sample_rate: int
    duration_seconds: float
    content_sha256: str = field(repr=False)
    path: Path = field(repr=False)
    _store_token: object = field(repr=False, compare=False)
    _directory_identity: tuple[int, ...] = field(repr=False, compare=False)
    _file_identity: tuple[int, ...] = field(repr=False, compare=False)


class MusicImportStore:
    """Keep verified WAV imports below one pre-created, identity-pinned root.

    This store intentionally owns no reference count.  The durable queue/rights
    layer supplies that count when it asks for a safe cleanup attempt.
    """

    def __init__(self, root: Path | str) -> None:
        raw_root = Path(root).expanduser().absolute()
        _assert_no_symlink_ancestor(raw_root)
        if not raw_root.is_dir() or raw_root.is_symlink():
            raise MusicImportStoreError("music import root is unavailable")
        try:
            canonical_root = raw_root.resolve(strict=True)
            root_identity = _identity(raw_root)
        except OSError:
            raise MusicImportStoreError("music import root is unavailable") from None
        self.root = canonical_root
        self._root_path = raw_root
        self._root_identity = root_identity
        self._store_token = object()
        self._lock = threading.RLock()

    def put_wav(self, data: bytes) -> tuple[ImportedMusicWav, bool]:
        """Store one exact PCM WAV and return ``(receipt, created)``.

        Repeating the same bytes returns the canonical pre-existing receipt and
        does not mutate the file or directory.
        """

        validated = _validated(data)
        digest = validated.sha256
        with self._lock:
            self._assert_current_root()
            directory, _directory_created, directory_identity = self._directory_for(digest)
            target = directory / _AUDIO_NAME

            def directory_current() -> None:
                self._assert_directory_current(directory, directory_identity)

            try:
                if _path_lexists(target):
                    directory_current()
                    existing = self._validated_existing(target, digest)
                    directory_current()
                    return self._receipt(existing, target, directory_identity), False
                temporary = directory / f".import-{secrets.token_hex(16)}.tmp"
                temporary_identity: list[tuple[int, ...]] = []
                committed = False
                try:
                    _write_atomic_wav(temporary, target, data, directory_current, temporary_identity)
                    committed = True
                finally:
                    if not committed and temporary_identity:
                        _delete_pinned_file(temporary, directory_identity, temporary_identity[0])
                return self._receipt(validated, target, directory_identity), True
            except MusicImportStoreError:
                raise
            except OSError:
                raise MusicImportStoreError("music import storage failed") from None

    def discard_if_unreferenced(self, receipt: ImportedMusicWav, reference_count: int) -> bool:
        """Delete only a current, unreferenced canonical file without recursion."""

        if isinstance(reference_count, bool) or not isinstance(reference_count, int) or reference_count < 0:
            raise ValueError("reference_count must be a non-negative integer")
        if not isinstance(receipt, ImportedMusicWav) or receipt._store_token is not self._store_token:
            raise MusicImportStoreError("music import receipt is not current")
        if reference_count != 0:
            return False
        with self._lock:
            self._assert_current_root()
            digest = receipt.content_sha256
            if not _is_sha256(digest):
                raise MusicImportStoreError("music import receipt is invalid")
            directory = self.root / digest
            target = directory / _AUDIO_NAME
            if receipt.path != target:
                raise MusicImportStoreError("music import receipt is not current")
            if not _path_lexists(directory):
                return False
            directory_identity = _pin_safe_directory(directory, self.root)

            def directory_current() -> None:
                self._assert_directory_current(directory, directory_identity)

            directory_current()
            if tuple(item.name for item in directory.iterdir()) != (_AUDIO_NAME,):
                return False
            directory_current()
            existing = self._validated_existing(target, digest)
            directory_current()
            if not _same_receipt_shape(receipt, existing):
                raise MusicImportStoreError("music import receipt is not current")
            if directory_identity != receipt._directory_identity or _file_identity(target) != receipt._file_identity:
                return False
            return _delete_pinned_file(target, directory_identity, receipt._file_identity)

    def current_receipt(self, content_sha256: str) -> ImportedMusicWav | None:
        """Resolve one canonical store receipt without exposing arbitrary paths."""

        if not _is_sha256(content_sha256):
            raise MusicImportStoreError("music import identifier is invalid")
        with self._lock:
            self._assert_current_root()
            directory = self.root / content_sha256
            target = directory / _AUDIO_NAME
            if not _path_lexists(directory):
                return None
            directory_identity = _pin_safe_directory(directory, self.root)
            self._assert_directory_current(directory, directory_identity)
            validated = self._validated_existing(target, content_sha256)
            self._assert_directory_current(directory, directory_identity)
            return self._receipt(validated, target, directory_identity)

    def _assert_current_root(self) -> None:
        _assert_no_symlink_ancestor(self._root_path)
        try:
            if (
                not self._root_path.is_dir()
                or self._root_path.is_symlink()
                or _identity(self._root_path) != self._root_identity
                or self._root_path.resolve(strict=True) != self.root
            ):
                raise MusicImportStoreError("music import root is unavailable")
        except OSError:
            raise MusicImportStoreError("music import root is unavailable") from None

    def _directory_for(self, digest: str) -> tuple[Path, bool, tuple[int, int]]:
        directory = self.root / digest
        if _path_lexists(directory):
            return directory, False, _pin_safe_directory(directory, self.root)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            return directory, False, _pin_safe_directory(directory, self.root)
        except OSError:
            raise MusicImportStoreError("music import storage failed") from None
        try:
            directory_identity = _pin_safe_directory(directory, self.root)
        except Exception:
            raise
        return directory, True, directory_identity

    def _assert_directory_current(self, directory: Path, identity: tuple[int, int]) -> None:
        self._assert_current_root()
        if _pin_safe_directory(directory, self.root) != identity:
            raise MusicImportStoreError("music import storage is unavailable")

    def _validated_existing(self, target: Path, digest: str) -> ValidatedWav:
        try:
            data = _read_regular_file(target)
        except OSError:
            raise MusicImportStoreError("music import is unavailable") from None
        validated = _validated(data)
        if validated.sha256 != digest:
            raise MusicImportStoreError("music import integrity check failed")
        return validated

    def _receipt(
        self,
        validated: ValidatedWav,
        target: Path,
        directory_identity: tuple[int, ...],
    ) -> ImportedMusicWav:
        return ImportedMusicWav(
            size_bytes=validated.size_bytes,
            channels=validated.channels,
            sample_rate=validated.sample_rate,
            duration_seconds=validated.duration_seconds,
            content_sha256=validated.sha256,
            path=target,
            _store_token=self._store_token,
            _directory_identity=directory_identity,
            _file_identity=_file_identity(target),
        )


def _validated(data: bytes) -> ValidatedWav:
    try:
        validated = validate_pcm_wav(data)
    except (SpeechAudioArtifactError, TypeError):
        raise MusicImportStoreError("music import WAV is invalid") from None
    if validated.size_bytes > _MAX_STORED_BYTES:
        raise MusicImportStoreError("music import WAV is invalid")
    return validated


def _write_atomic_wav(
    temporary: Path,
    target: Path,
    data: bytes,
    directory_current: Callable[[], None],
    temporary_identity: list[tuple[int, ...]],
) -> None:
    directory_current()
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    directory_current()
    temporary_identity.append(_file_identity(temporary))
    directory_current()
    os.replace(temporary, target)
    directory_current()
    os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    directory_current()


def _read_regular_file(path: Path) -> bytes:
    if path.is_symlink():
        raise OSError("not a regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError("not a regular file")
            data = handle.read(_MAX_STORED_BYTES + 1)
    except Exception:
        raise
    if len(data) > _MAX_STORED_BYTES:
        raise OSError("file is too large")
    return data


def _pin_safe_directory(directory: Path, root: Path) -> tuple[int, ...]:
    if directory.is_symlink() or not directory.is_dir() or directory.parent != root:
        raise MusicImportStoreError("music import storage is unavailable")
    try:
        if directory.resolve(strict=True).parent != root:
            raise MusicImportStoreError("music import storage is unavailable")
        return _identity(directory)
    except OSError:
        raise MusicImportStoreError("music import storage is unavailable") from None


def _assert_no_symlink_ancestor(path: Path) -> None:
    for candidate in (path, *path.parents):
        if _path_lexists(candidate) and candidate.is_symlink():
            raise MusicImportStoreError("music import root is unavailable")


def _identity(path: Path) -> tuple[int, ...]:
    stat_result = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(stat_result.st_mode):
        raise OSError("not a directory")
    return stat_result.st_dev, stat_result.st_ino


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _same_receipt_shape(receipt: ImportedMusicWav, validated: ValidatedWav) -> bool:
    return (
        receipt.content_sha256 == validated.sha256
        and receipt.size_bytes == validated.size_bytes
        and receipt.channels == validated.channels
        and receipt.sample_rate == validated.sample_rate
        and receipt.duration_seconds == validated.duration_seconds
    )


def _file_identity(path: Path) -> tuple[int, ...]:
    if os.name == "nt":
        opened = _open_windows_regular_file(path, _WINDOWS_FILE_READ_ATTRIBUTES)
        if opened is None:
            raise MusicImportStoreError("music import is unavailable")
        handle, identity = opened
        try:
            return identity
        finally:
            _close_windows_handle(handle)
    try:
        stat_result = path.stat(follow_symlinks=False)
    except OSError:
        raise MusicImportStoreError("music import is unavailable") from None
    if not stat.S_ISREG(stat_result.st_mode):
        raise MusicImportStoreError("music import is unavailable")
    return stat_result.st_dev, stat_result.st_ino


def _delete_pinned_file(
    path: Path,
    directory_identity: tuple[int, ...],
    file_identity: tuple[int, ...],
) -> bool:
    if os.name == "nt":
        return _delete_pinned_windows_file(path, file_identity)
    return _delete_pinned_posix_file(path, directory_identity, file_identity)


def _delete_pinned_posix_file(
    path: Path,
    directory_identity: tuple[int, ...],
    file_identity: tuple[int, ...],
) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(path.parent, flags)
    except OSError:
        return False
    try:
        directory_stat = os.fstat(directory_fd)
        if (directory_stat.st_dev, directory_stat.st_ino) != directory_identity:
            return False
        file_fd = os.open(_AUDIO_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode) or (file_stat.st_dev, file_stat.st_ino) != file_identity:
                return False
        finally:
            os.close(file_fd)
        os.unlink(_AUDIO_NAME, dir_fd=directory_fd)
        return True
    except OSError:
        return False
    finally:
        os.close(directory_fd)


def _delete_pinned_windows_file(path: Path, file_identity: tuple[int, ...]) -> bool:
    opened = _open_windows_regular_file(path, _WINDOWS_DELETE | _WINDOWS_FILE_READ_ATTRIBUTES)
    if opened is None:
        return False
    handle, opened_identity = opened
    try:
        if opened_identity != file_identity:
            return False
        disposition = _WindowsFileDispositionInformation(True)
        return bool(
            _windows_set_file_information(
                handle,
                _WINDOWS_FILE_DISPOSITION_INFO,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            )
        )
    finally:
        _close_windows_handle(handle)


def _open_windows_regular_file(path: Path, desired_access: int) -> tuple[object, tuple[int, ...]] | None:
    handle = _windows_create_file(
        str(path),
        desired_access,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE | _WINDOWS_FILE_SHARE_DELETE,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _WINDOWS_INVALID_HANDLE_VALUE:
        return None
    information = _WindowsByHandleFileInformation()
    if not _windows_get_file_information(handle, ctypes.byref(information)):
        _close_windows_handle(handle)
        return None
    attributes = int(information.dwFileAttributes)
    if attributes & (_WINDOWS_FILE_ATTRIBUTE_DIRECTORY | _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT):
        _close_windows_handle(handle)
        return None
    return handle, (
        int(information.dwVolumeSerialNumber),
        int(information.nFileIndexHigh),
        int(information.nFileIndexLow),
    )


def _close_windows_handle(handle: object) -> None:
    _windows_close_handle(handle)


__all__ = [
    "ImportedMusicWav",
    "MusicImportStore",
    "MusicImportStoreError",
]
