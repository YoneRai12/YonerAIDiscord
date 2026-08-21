"""Fixed, bounded client for the local execution-sandbox broker pipe."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Protocol


PIPE_NAME = r"\\.\pipe\YonerAI-ForgeSandbox-Broker-v1"
PROTOCOL_REVISION = "2026-08-08.1"
MAX_REQUEST_FRAME_BYTES = 196_608
MAX_RECEIPT_FRAME_BYTES = 65_536
OPERATION_TIMEOUT_SECONDS = 185.0
IO_CANCEL_JOIN_SECONDS = 3.0
OVERLAPPED_CANCEL_REAP_SECONDS = 3.0
AUTH_PREAMBLE = b"\x59"

_PROTECTED_ROOT = PureWindowsPath("C" + ":/", "ProgramData", "YonerAI", "ExecSandbox")
_OWNER_PACK = _PROTECTED_ROOT / "owner-pack"
_BROKER_PACK = _PROTECTED_ROOT / "broker-pack"
_VERSION_PATH = _OWNER_PACK / "VERSION.lock"
_OWNER_CONFIG_PATH = _OWNER_PACK / "owner-pack.config.json"
_BROKER_READINESS_PATH = _PROTECTED_ROOT / "readiness" / "broker-ready.json"
_BROKER_IMAGE_PATH = _BROKER_PACK / "python.exe"
_SYSTEM_SID = "S-1-5-18"
_FIXED_ERROR = "execution sandbox pipe exchange failed safely"


class NamedPipeExchangeError(RuntimeError):
    """Content-free failure at the low-privilege broker boundary."""

    def __init__(self) -> None:
        super().__init__(_FIXED_ERROR)


class NamedPipeApi(Protocol):
    def open(self, name: str, timeout_ms: int) -> int: ...

    def server_identity(self, handle: int) -> tuple[int, str, str]: ...

    def write(self, handle: int, value: bytes, *, deadline: float | None = None) -> int: ...

    def read(self, handle: int, maximum: int, *, deadline: float | None = None) -> bytes: ...

    def cancel(self, handle: int) -> None: ...

    def close(self, handle: int) -> None: ...


class _CtypesNamedPipeApi:
    _FILE_FLAG_OVERLAPPED = 0x40000000
    _ERROR_IO_PENDING = 997
    _ERROR_OPERATION_ABORTED = 995
    _ERROR_NOT_FOUND = 1168
    _INFINITE = 0xFFFFFFFF
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 258

    class _Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", ctypes.c_uint32),
            ("OffsetHigh", ctypes.c_uint32),
            ("hEvent", ctypes.c_void_p),
        )

    def __init__(self) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise NamedPipeExchangeError()
        try:
            kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
            advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)
            handle_type = ctypes.c_void_p
            kernel32.WaitNamedPipeW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
            kernel32.WaitNamedPipeW.restype = ctypes.c_int
            kernel32.CreateFileW.argtypes = (
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                handle_type,
            )
            kernel32.CreateFileW.restype = handle_type
            kernel32.GetNamedPipeServerProcessId.argtypes = (handle_type, ctypes.POINTER(ctypes.c_uint32))
            kernel32.GetNamedPipeServerProcessId.restype = ctypes.c_int
            kernel32.ReadFile.argtypes = (
                handle_type,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            )
            kernel32.ReadFile.restype = ctypes.c_int
            kernel32.WriteFile.argtypes = kernel32.ReadFile.argtypes
            kernel32.WriteFile.restype = ctypes.c_int
            kernel32.CancelIoEx.argtypes = (handle_type, ctypes.c_void_p)
            kernel32.CancelIoEx.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = (handle_type,)
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
            kernel32.CreateEventW.restype = handle_type
            kernel32.WaitForSingleObject.argtypes = (handle_type, ctypes.c_uint32)
            kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            kernel32.GetOverlappedResult.argtypes = (
                handle_type,
                ctypes.POINTER(_CtypesNamedPipeApi._Overlapped),
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_int,
            )
            kernel32.GetOverlappedResult.restype = ctypes.c_int
            kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
            kernel32.OpenProcess.restype = handle_type
            kernel32.QueryFullProcessImageNameW.argtypes = (
                handle_type,
                ctypes.c_uint32,
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_uint32),
            )
            kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
            kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
            kernel32.LocalFree.restype = ctypes.c_void_p
            advapi32.OpenProcessToken.argtypes = (handle_type, ctypes.c_uint32, ctypes.POINTER(handle_type))
            advapi32.OpenProcessToken.restype = ctypes.c_int
            advapi32.GetTokenInformation.argtypes = (
                handle_type,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            )
            advapi32.GetTokenInformation.restype = ctypes.c_int
            advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
            advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
        except Exception:
            raise NamedPipeExchangeError() from None
        self._kernel32 = kernel32
        self._advapi32 = advapi32
        self._invalid = ctypes.c_void_p(-1).value

    def open(self, name: str, timeout_ms: int) -> int:
        if self._kernel32.WaitNamedPipeW(name, timeout_ms) == 0:
            raise NamedPipeExchangeError()
        handle = self._kernel32.CreateFileW(
            name,
            0xC0000000,
            0,
            None,
            3,
            self._FILE_FLAG_OVERLAPPED,
            None,
        )
        value = int(handle or 0)
        if value in (0, self._invalid):
            raise NamedPipeExchangeError()
        return value

    def server_identity(self, handle: int) -> tuple[int, str, str]:
        pid = ctypes.c_uint32()
        if self._kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)) == 0 or pid.value == 0:
            raise NamedPipeExchangeError()
        process = self._kernel32.OpenProcess(0x1000, 0, pid.value)
        if not process:
            raise NamedPipeExchangeError()
        token = ctypes.c_void_p()
        try:
            size = ctypes.c_uint32(32_768)
            image = ctypes.create_unicode_buffer(size.value)
            if self._kernel32.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)) == 0:
                raise NamedPipeExchangeError()
            if self._advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)) == 0:
                raise NamedPipeExchangeError()
            needed = ctypes.c_uint32()
            self._advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if needed.value == 0 or needed.value > 65_536:
                raise NamedPipeExchangeError()
            raw = ctypes.create_string_buffer(needed.value)
            if self._advapi32.GetTokenInformation(token, 1, raw, needed.value, ctypes.byref(needed)) == 0:
                raise NamedPipeExchangeError()
            sid_pointer = ctypes.c_void_p.from_buffer(raw).value
            sid_text = ctypes.c_wchar_p()
            if not sid_pointer or self._advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)) == 0:
                raise NamedPipeExchangeError()
            try:
                sid = str(sid_text.value)
            finally:
                self._kernel32.LocalFree(sid_text)
            return int(pid.value), image.value, sid
        finally:
            if token.value:
                self._kernel32.CloseHandle(token)
            self._kernel32.CloseHandle(process)

    def write(self, handle: int, value: bytes, *, deadline: float | None = None) -> int:
        if not value:
            raise NamedPipeExchangeError()
        raw = ctypes.create_string_buffer(value)
        return self._io(self._kernel32.WriteFile, handle, raw, len(value), deadline)

    def read(self, handle: int, maximum: int, *, deadline: float | None = None) -> bytes:
        if type(maximum) is not int or not 0 < maximum <= MAX_RECEIPT_FRAME_BYTES:
            raise NamedPipeExchangeError()
        raw = ctypes.create_string_buffer(maximum)
        count = self._io(self._kernel32.ReadFile, handle, raw, maximum, deadline)
        return raw.raw[:count]

    def _io(
        self,
        operation: object,
        handle: int,
        raw: object,
        size: int,
        deadline: float | None,
    ) -> int:
        end = deadline if deadline is not None else time.monotonic() + OPERATION_TIMEOUT_SECONDS
        event = self._kernel32.CreateEventW(None, 1, 0, None)
        if not event:
            raise NamedPipeExchangeError()
        overlapped = self._Overlapped()
        overlapped.hEvent = event
        try:
            immediate = operation(handle, raw, size, None, ctypes.byref(overlapped))  # type: ignore[operator]
            if not immediate and ctypes.get_last_error() != self._ERROR_IO_PENDING:
                raise NamedPipeExchangeError()
            remaining = end - time.monotonic()
            if remaining <= 0:
                self._cancel_one(handle, overlapped)
                raise NamedPipeExchangeError()
            waited = self._kernel32.WaitForSingleObject(
                event,
                max(1, min(0xFFFFFFFE, int(remaining * 1000 + 0.999))),
            )
            if waited != self._WAIT_OBJECT_0:
                self._cancel_one(handle, overlapped)
                raise NamedPipeExchangeError()
            transferred = ctypes.c_uint32()
            if (
                self._kernel32.GetOverlappedResult(
                    handle,
                    ctypes.byref(overlapped),
                    ctypes.byref(transferred),
                    0,
                )
                == 0
            ):
                raise NamedPipeExchangeError()
            return int(transferred.value)
        finally:
            self._kernel32.CloseHandle(event)

    def _cancel_one(self, handle: int, overlapped: _Overlapped) -> None:
        if self._kernel32.CancelIoEx(handle, ctypes.byref(overlapped)) == 0:
            error = ctypes.get_last_error()
            if error not in (0, self._ERROR_OPERATION_ABORTED, self._ERROR_NOT_FOUND):
                raise NamedPipeExchangeError()
        wait_ms = math.ceil(OVERLAPPED_CANCEL_REAP_SECONDS * 1000)
        if self._kernel32.WaitForSingleObject(overlapped.hEvent, wait_ms) != self._WAIT_OBJECT_0:
            os._exit(70)
        transferred = ctypes.c_uint32()
        if (
            self._kernel32.GetOverlappedResult(
                handle,
                ctypes.byref(overlapped),
                ctypes.byref(transferred),
                0,
            )
            == 0
        ):
            error = ctypes.get_last_error()
            if error not in (self._ERROR_OPERATION_ABORTED, self._ERROR_NOT_FOUND):
                raise NamedPipeExchangeError()

    def cancel(self, handle: int) -> None:
        if self._kernel32.CancelIoEx(handle, None) == 0:
            error = ctypes.get_last_error()
            if error not in (0, self._ERROR_OPERATION_ABORTED, self._ERROR_NOT_FOUND):
                raise NamedPipeExchangeError()

    def close(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)


class FixedNamedPipeExchange:
    """One request per handle, fixed endpoint, SYSTEM server, bounded response."""

    pipe_name = PIPE_NAME

    def __init__(
        self,
        *,
        api: NamedPipeApi | None = None,
        readiness_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._api = api
        self._readiness_probe = readiness_probe or _protected_readiness_current
        self._handles: dict[int, NamedPipeApi] = {}
        self._handles_lock = threading.Lock()
        self._closing = False

    @property
    def ready(self) -> bool:
        if self._closing:
            return False
        try:
            return self._readiness_probe() is True
        except Exception:
            return False

    @property
    def verified_current(self) -> bool:
        return self.ready

    async def exchange(self, frame: bytes) -> bytes:
        _validate_request_frame(frame)
        if not self.ready:
            raise NamedPipeExchangeError()
        result: list[bytes] = []
        errors: list[BaseException] = []
        done = threading.Event()
        cancelled = threading.Event()

        def invoke() -> None:
            try:
                result.append(self._exchange_sync(frame, cancelled=cancelled))
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=invoke, name="yonerai-pipe-client", daemon=True)
        worker.start()
        deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
        try:
            while not done.is_set():
                if time.monotonic() >= deadline:
                    raise NamedPipeExchangeError()
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            cancelled.set()
            self._cancel_active()
            await asyncio.to_thread(done.wait, IO_CANCEL_JOIN_SECONDS)
            raise
        except Exception:
            cancelled.set()
            self._cancel_active()
            await asyncio.to_thread(done.wait, IO_CANCEL_JOIN_SECONDS)
            raise NamedPipeExchangeError() from None
        worker.join(0)
        if errors or len(result) != 1:
            raise NamedPipeExchangeError()
        return result[0]

    def begin_close(self) -> None:
        self._closing = True
        self._cancel_active()

    def _exchange_sync(self, frame: bytes, *, cancelled: threading.Event) -> bytes:
        api = self._api or _CtypesNamedPipeApi()
        handle: int | None = None
        deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
        try:
            if not self.ready:
                raise NamedPipeExchangeError()
            handle = api.open(PIPE_NAME, 1_000)
            if type(handle) is not int or handle <= 0:
                raise NamedPipeExchangeError()
            with self._handles_lock:
                if self._closing or cancelled.is_set():
                    raise NamedPipeExchangeError()
                self._handles[handle] = api
            if cancelled.is_set():
                raise NamedPipeExchangeError()
            _validate_server_identity(api.server_identity(handle))
            offset = 0
            payload = AUTH_PREAMBLE + frame
            while offset < len(payload):
                if cancelled.is_set():
                    raise NamedPipeExchangeError()
                written = api.write(handle, payload[offset:], deadline=deadline)
                if type(written) is not int or not 0 < written <= len(payload) - offset:
                    raise NamedPipeExchangeError()
                offset += written
            header = self._read_exact(api, handle, 4, deadline=deadline, cancelled=cancelled)
            length = int.from_bytes(header, "big")
            if not 0 < length <= MAX_RECEIPT_FRAME_BYTES - 4:
                raise NamedPipeExchangeError()
            result = header + self._read_exact(api, handle, length, deadline=deadline, cancelled=cancelled)
            _validate_receipt_frame(result)
            if cancelled.is_set() or not self.ready:
                raise NamedPipeExchangeError()
            return result
        except NamedPipeExchangeError:
            raise
        except Exception:
            raise NamedPipeExchangeError() from None
        finally:
            if handle is not None:
                with self._handles_lock:
                    self._handles.pop(handle, None)
                    try:
                        api.close(handle)
                    except Exception:
                        pass

    @staticmethod
    def _read_exact(
        api: NamedPipeApi,
        handle: int,
        length: int,
        *,
        deadline: float,
        cancelled: threading.Event,
    ) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            if cancelled.is_set():
                raise NamedPipeExchangeError()
            chunk = api.read(handle, remaining, deadline=deadline)
            if not isinstance(chunk, bytes) or not chunk or len(chunk) > remaining:
                raise NamedPipeExchangeError()
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _cancel_active(self) -> None:
        with self._handles_lock:
            for handle, api in tuple(self._handles.items()):
                try:
                    api.cancel(handle)
                except Exception:
                    pass

    def __repr__(self) -> str:
        return f"FixedNamedPipeExchange(state={'closing' if self._closing else 'ready' if self.ready else 'blocked'})"


def _protected_readiness_current() -> bool:
    try:
        version = _read_exact_json(_VERSION_PATH)
        config_path = Path(_OWNER_CONFIG_PATH)
        if config_path.is_symlink() or not config_path.is_file():
            return False
        config_digest = "sha256:" + hashlib.sha256(config_path.read_bytes()).hexdigest()
        broker = _read_exact_json(_BROKER_READINESS_PATH)
        return (
            set(version)
            == {
                "base_image_sha256",
                "policy_digest",
                "policy_revision",
                "protocol_revision",
                "schema",
                "state",
            }
            and version["schema"] == "yonerai.exec-sandbox.version-lock.v1"
            and version["protocol_revision"] == PROTOCOL_REVISION
            and version["state"] == "ready"
            and set(broker)
            == {
                "base_digest",
                "config_digest",
                "policy_digest",
                "policy_revision",
                "protocol_revision",
                "schema",
                "state",
            }
            and broker["schema"] == "yonerai.exec-sandbox.broker-readiness.v1"
            and broker["state"] == "ready"
            and broker["protocol_revision"] == PROTOCOL_REVISION
            and broker["policy_revision"] == version["policy_revision"]
            and broker["policy_digest"] == version["policy_digest"]
            and broker["base_digest"] == version["base_image_sha256"]
            and broker["config_digest"] == config_digest
        )
    except Exception:
        return False


def _read_exact_json(path: PureWindowsPath) -> dict[str, object]:
    local = Path(path)
    if local.is_symlink() or not local.is_file():
        raise NamedPipeExchangeError()
    raw = local.read_bytes()
    if not raw or len(raw) > 65_536:
        raise NamedPipeExchangeError()
    value = json.loads(raw.decode("utf-8", "strict"))
    if type(value) is not dict:
        raise NamedPipeExchangeError()
    return value


def _validate_server_identity(value: object) -> None:
    if type(value) is not tuple or len(value) != 3:
        raise NamedPipeExchangeError()
    pid, image, sid = value
    expected = os.path.normcase(str(_BROKER_IMAGE_PATH))
    if (
        type(pid) is not int
        or pid <= 0
        or type(image) is not str
        or os.path.normcase(image) != expected
        or sid != _SYSTEM_SID
    ):
        raise NamedPipeExchangeError()


def _validate_request_frame(frame: object) -> None:
    if not isinstance(frame, bytes) or not 5 <= len(frame) <= MAX_REQUEST_FRAME_BYTES + 4:
        raise NamedPipeExchangeError()
    if int.from_bytes(frame[:4], "big") != len(frame) - 4:
        raise NamedPipeExchangeError()


def _validate_receipt_frame(frame: bytes) -> None:
    if not 5 <= len(frame) <= MAX_RECEIPT_FRAME_BYTES or int.from_bytes(frame[:4], "big") != len(frame) - 4:
        raise NamedPipeExchangeError()
    try:
        value = json.loads(frame[4:].decode("utf-8", "strict"))
    except Exception:
        raise NamedPipeExchangeError() from None
    if type(value) is not dict:
        raise NamedPipeExchangeError()


__all__ = [
    "FixedNamedPipeExchange",
    "MAX_RECEIPT_FRAME_BYTES",
    "MAX_REQUEST_FRAME_BYTES",
    "NamedPipeApi",
    "NamedPipeExchangeError",
    "PIPE_NAME",
]
