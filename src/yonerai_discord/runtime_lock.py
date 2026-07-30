from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


_LOCK_FILE_PREFIX = ".yonerai-runtime-"
_WINDOWS_SHARING_ERRORS = frozenset({5, 32, 33})


class RuntimeLockError(RuntimeError):
    """Runtime排他の準備または取得に失敗した。"""


class RuntimeAlreadyRunningError(RuntimeLockError):
    """同じruntime data領域を使うprocessが既に起動している。"""


def runtime_lock_path(database_path: str | os.PathLike[str], *, lock_dir: Path | None = None) -> Path:
    """DBの絶対位置を秘密非含有の固定長IDへ変換する。"""

    resolved = Path(database_path).expanduser().resolve(strict=False)
    normalized = os.path.normcase(os.fspath(resolved))
    digest = hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()[:32]
    directory = lock_dir if lock_dir is not None else resolved.parent
    return Path(directory) / f"{_LOCK_FILE_PREFIX}{digest}.lock"


class RuntimeInstanceLock:
    """OSがprocess終了時に自動解放するcross-process single-instance lock。"""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        lock_dir: Path | None = None,
    ) -> None:
        self.path = runtime_lock_path(database_path, lock_dir=lock_dir)
        self._handle: BinaryIO | int | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeAlreadyRunningError("runtime lock is already acquired")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeLockError("runtime lock could not be prepared") from exc

        if os.name == "nt":
            self._handle = self._acquire_windows_handle()
            return
        try:
            handle = self.path.open("a+b")
        except OSError as exc:
            raise RuntimeLockError("runtime lock could not be prepared") from exc

        try:
            self._lock_unix_handle(handle)
        except OSError as exc:
            handle.close()
            raise RuntimeAlreadyRunningError("runtime lock is unavailable") from exc
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        if isinstance(handle, int):
            self._close_windows_handle(handle)
            return
        try:
            self._unlock_unix_handle(handle)
        finally:
            handle.close()

    def _acquire_windows_handle(self) -> int:
        import ctypes
        from ctypes import wintypes

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(self.path),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0,  # exclusive: no read/write/delete sharing
            None,
            4,  # OPEN_ALWAYS
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            error_code = ctypes.get_last_error()
            if error_code in _WINDOWS_SHARING_ERRORS:
                raise RuntimeAlreadyRunningError("runtime lock is unavailable")
            raise RuntimeLockError("runtime lock could not be acquired")
        return int(handle)

    @staticmethod
    def _close_windows_handle(handle: int) -> None:
        import ctypes
        from ctypes import wintypes

        close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        # CloseHandle失敗時もprocess終了でOSが自動解放する。
        close_handle(wintypes.HANDLE(handle))

    @staticmethod
    def _lock_unix_handle(handle: BinaryIO) -> None:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_unix_handle(handle: BinaryIO) -> None:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> RuntimeInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
