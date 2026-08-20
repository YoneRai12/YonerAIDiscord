"""Narrow Hyper-V Socket/VSOCK transport for one disposable-VM job.

The public boundary owns every address constant.  Callers provide only the
exact VM UUID on the Windows host; they cannot select a family, service,
protocol, wildcard, CID, port, or raw sockaddr.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import socket as _socket
import struct
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable


AF_HYPERV = 34
HV_PROTOCOL_RAW = 1
GUEST_PORT = 40_509
SERVICE_ID = uuid.UUID(f"{GUEST_PORT:08x}-facb-11e6-bd58-64006a7986d3")
PROVISIONING_PORT = 40_508
PROVISIONING_SERVICE_ID = uuid.UUID(f"{PROVISIONING_PORT:08x}-facb-11e6-bd58-64006a7986d3")
MAX_JSON_FRAME_BYTES = 131_072
MAX_JSON_DEPTH = 64
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 300.0

_SOCKADDR_HV_BYTES = 36
_SOCKET_ERROR = -1
_INVALID_SOCKET = ctypes.c_size_t(-1).value
_SOL_SOCKET = 0xFFFF
_SO_RCVTIMEO = 0x1006
_SO_SNDTIMEO = 0x1005
_SD_BOTH = 2
_POLL_READ = 0x0300
_POLL_WRITE = 0x0010
_POLL_TERMINAL = 0x0007
_IO_SLICE_SECONDS = 0.1
_FIXED_ERROR = "Hyper-V socket transport failed safely"
_FIXED_CANCELLED = "Hyper-V socket transport was cancelled safely"


class HyperVSocketTransportError(RuntimeError):
    """Content-free failure at the sealed transport boundary."""

    def __init__(self) -> None:
        super().__init__(_FIXED_ERROR)


class HyperVSocketTransportCancelled(HyperVSocketTransportError):
    """Content-free cancellation after the owned socket was closed."""

    def __init__(self) -> None:
        RuntimeError.__init__(self, _FIXED_CANCELLED)


class _IoSliceTimeout(Exception):
    pass


@runtime_checkable
class WindowsWinsockApi(Protocol):
    """Injectable seam for offline tests; not a general socket API."""

    def startup(self) -> None: ...

    def socket(self, family: int, sock_type: int, protocol: int) -> int: ...

    def bind(self, handle: int, sockaddr: bytes) -> None: ...

    def listen(self, handle: int, backlog: int) -> None: ...

    def accept(self, handle: int) -> int: ...

    def set_timeout(self, handle: int, timeout_seconds: float) -> None: ...

    def wait_readable(self, handle: int, timeout_seconds: float) -> bool: ...

    def wait_writable(self, handle: int, timeout_seconds: float) -> bool: ...

    def send(self, handle: int, data: bytes) -> int: ...

    def recv(self, handle: int, maximum: int) -> bytes: ...

    def shutdown(self, handle: int) -> None: ...

    def close(self, handle: int) -> None: ...


class _Stream(Protocol):
    def wait_readable(self, timeout_seconds: float) -> bool: ...

    def wait_writable(self, timeout_seconds: float) -> bool: ...

    def send(self, data: bytes) -> int: ...

    def recv(self, maximum: int) -> bytes: ...

    def close(self) -> None: ...


class _CtypesWinsock:
    """Minimal ctypes binding used because standard Python has no AF_HYPERV."""

    def __init__(self) -> None:
        if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
            raise HyperVSocketTransportError()
        try:
            dll = ctypes.WinDLL("Ws2_32.dll", use_last_error=True)
            socket_type = ctypes.c_size_t
            dll.WSAStartup.argtypes = (ctypes.c_ushort, ctypes.c_void_p)
            dll.WSAStartup.restype = ctypes.c_int
            dll.WSAGetLastError.argtypes = ()
            dll.WSAGetLastError.restype = ctypes.c_int
            dll.socket.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
            dll.socket.restype = socket_type
            dll.bind.argtypes = (socket_type, ctypes.c_void_p, ctypes.c_int)
            dll.bind.restype = ctypes.c_int
            dll.listen.argtypes = (socket_type, ctypes.c_int)
            dll.listen.restype = ctypes.c_int
            dll.accept.argtypes = (socket_type, ctypes.c_void_p, ctypes.c_void_p)
            dll.accept.restype = socket_type
            dll.setsockopt.argtypes = (
                socket_type,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_int,
            )
            dll.setsockopt.restype = ctypes.c_int
            dll.WSAPoll.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int)
            dll.WSAPoll.restype = ctypes.c_int
            dll.send.argtypes = (socket_type, ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
            dll.send.restype = ctypes.c_int
            dll.recv.argtypes = (socket_type, ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
            dll.recv.restype = ctypes.c_int
            dll.shutdown.argtypes = (socket_type, ctypes.c_int)
            dll.shutdown.restype = ctypes.c_int
            dll.closesocket.argtypes = (socket_type,)
            dll.closesocket.restype = ctypes.c_int
        except Exception:
            raise HyperVSocketTransportError() from None
        self._dll = dll
        self._started = False

    def startup(self) -> None:
        if self._started:
            return
        data = ctypes.create_string_buffer(512)
        if self._dll.WSAStartup(0x0202, ctypes.byref(data)) != 0:
            raise HyperVSocketTransportError()
        self._started = True

    def socket(self, family: int, sock_type: int, protocol: int) -> int:
        handle = int(self._dll.socket(family, sock_type, protocol))
        if handle == _INVALID_SOCKET:
            raise HyperVSocketTransportError()
        return handle

    def bind(self, handle: int, sockaddr: bytes) -> None:
        raw = ctypes.create_string_buffer(sockaddr)
        if self._dll.bind(handle, ctypes.byref(raw), len(sockaddr)) == _SOCKET_ERROR:
            raise HyperVSocketTransportError()

    def listen(self, handle: int, backlog: int) -> None:
        if self._dll.listen(handle, backlog) == _SOCKET_ERROR:
            raise HyperVSocketTransportError()

    def accept(self, handle: int) -> int:
        accepted = int(self._dll.accept(handle, None, None))
        if accepted == _INVALID_SOCKET:
            self._raise_socket_error()
        return accepted

    def set_timeout(self, handle: int, timeout_seconds: float) -> None:
        milliseconds = ctypes.c_uint32(max(1, math.ceil(timeout_seconds * 1000)))
        for option in (_SO_RCVTIMEO, _SO_SNDTIMEO):
            if (
                self._dll.setsockopt(
                    handle,
                    _SOL_SOCKET,
                    option,
                    ctypes.byref(milliseconds),
                    ctypes.sizeof(milliseconds),
                )
                == _SOCKET_ERROR
            ):
                raise HyperVSocketTransportError()

    def wait_readable(self, handle: int, timeout_seconds: float) -> bool:
        return self._poll(handle, _POLL_READ, timeout_seconds)

    def wait_writable(self, handle: int, timeout_seconds: float) -> bool:
        return self._poll(handle, _POLL_WRITE, timeout_seconds)

    def _poll(self, handle: int, events: int, timeout_seconds: float) -> bool:
        class _PollFd(ctypes.Structure):
            _fields_ = (("fd", ctypes.c_size_t), ("events", ctypes.c_short), ("revents", ctypes.c_short))

        poll_fd = _PollFd(handle, events, 0)
        milliseconds = max(1, math.ceil(min(timeout_seconds, _IO_SLICE_SECONDS) * 1000))
        result = int(self._dll.WSAPoll(ctypes.byref(poll_fd), 1, milliseconds))
        if result == _SOCKET_ERROR:
            raise HyperVSocketTransportError()
        if result == 0:
            return False
        return bool(poll_fd.revents & (events | _POLL_TERMINAL))

    def send(self, handle: int, data: bytes) -> int:
        if not data:
            return 0
        raw = ctypes.create_string_buffer(data)
        sent = int(self._dll.send(handle, ctypes.byref(raw), len(data), 0))
        if sent == _SOCKET_ERROR:
            self._raise_socket_error()
        return sent

    def recv(self, handle: int, maximum: int) -> bytes:
        raw = ctypes.create_string_buffer(maximum)
        received = int(self._dll.recv(handle, ctypes.byref(raw), maximum, 0))
        if received == _SOCKET_ERROR:
            self._raise_socket_error()
        return raw.raw[:received]

    def _raise_socket_error(self) -> None:
        if int(self._dll.WSAGetLastError()) in (10035, 10060):
            raise _IoSliceTimeout()
        raise HyperVSocketTransportError()

    def shutdown(self, handle: int) -> None:
        if self._dll.shutdown(handle, _SD_BOTH) == _SOCKET_ERROR:
            raise HyperVSocketTransportError()

    def close(self, handle: int) -> None:
        if self._dll.closesocket(handle) == _SOCKET_ERROR:
            raise HyperVSocketTransportError()


class _NativeStream:
    def __init__(self, native: WindowsWinsockApi, handle: int) -> None:
        self._native = native
        self._handle = handle
        self._closed = False
        self._close_lock = threading.Lock()

    def wait_readable(self, timeout_seconds: float) -> bool:
        return self._native.wait_readable(self._handle, timeout_seconds)

    def wait_writable(self, timeout_seconds: float) -> bool:
        return self._native.wait_writable(self._handle, timeout_seconds)

    def send(self, data: bytes) -> int:
        return self._native.send(self._handle, data)

    def recv(self, maximum: int) -> bytes:
        return self._native.recv(self._handle, maximum)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            try:
                self._native.shutdown(self._handle)
            except Exception:
                pass
            try:
                self._native.close(self._handle)
            except Exception:
                raise HyperVSocketTransportError() from None
            self._closed = True


class _PythonSocketStream:
    def __init__(self, sock: object) -> None:
        self._socket = sock
        self._closed = False
        self._close_lock = threading.Lock()

    def wait_readable(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return True

    def wait_writable(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return True

    def send(self, data: bytes) -> int:
        return int(self._socket.send(data))  # type: ignore[attr-defined]

    def recv(self, maximum: int) -> bytes:
        return bytes(self._socket.recv(maximum))  # type: ignore[attr-defined]

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            try:
                self._socket.shutdown(_socket.SHUT_RDWR)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                self._socket.close()  # type: ignore[attr-defined]
            except Exception:
                raise HyperVSocketTransportError() from None
            self._closed = True


class JsonFrameConnection:
    """One owned stream carrying bounded, canonical JSON frames."""

    def __init__(
        self,
        stream: _Stream,
        *,
        cancelled: Callable[[], bool] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream = stream
        self._cancelled = cancelled or _never_cancelled
        self._timeout = _validate_timeout(timeout_seconds)
        self._clock = clock
        self._closed = False
        self._close_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self._close_lock:
            return self._closed

    def send_json(self, value: object) -> None:
        try:
            self._check_active()
            body = _canonical_json(value)
            if not body or len(body) > MAX_JSON_FRAME_BYTES:
                raise HyperVSocketTransportError()
            self._send_all(struct.pack("!I", len(body)) + body, self._deadline())
        except HyperVSocketTransportCancelled:
            self.close()
            raise
        except Exception:
            self.close()
            raise HyperVSocketTransportError() from None

    def receive_json(self) -> object:
        try:
            self._check_active()
            deadline = self._deadline()
            declared = struct.unpack("!I", self._receive_exact(4, deadline))[0]
            if declared == 0 or declared > MAX_JSON_FRAME_BYTES:
                raise HyperVSocketTransportError()
            body = self._receive_exact(declared, deadline)
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if _canonical_json(value) != body:
                raise HyperVSocketTransportError()
            return value
        except HyperVSocketTransportCancelled:
            self.close()
            raise
        except Exception:
            self.close()
            raise HyperVSocketTransportError() from None

    def exchange_json(self, value: object) -> object:
        self.send_json(value)
        return self.receive_json()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            try:
                self._stream.close()
            except Exception:
                raise HyperVSocketTransportError() from None
            self._closed = True

    def _check_active(self) -> None:
        if self._closed:
            raise HyperVSocketTransportError()
        try:
            cancelled = self._cancelled()
        except Exception:
            raise HyperVSocketTransportError() from None
        if cancelled is not False:
            raise HyperVSocketTransportCancelled()

    def _deadline(self) -> float:
        return self._clock() + self._timeout

    def _slice(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise HyperVSocketTransportError()
        return min(_IO_SLICE_SECONDS, remaining)

    def _send_all(self, data: bytes, deadline: float) -> None:
        offset = 0
        while offset < len(data):
            self._check_active()
            if not self._stream.wait_writable(self._slice(deadline)):
                continue
            self._check_active()
            try:
                sent = self._stream.send(data[offset:])
            except (TimeoutError, _IoSliceTimeout):
                continue
            if type(sent) is not int or sent <= 0 or sent > len(data) - offset:
                raise HyperVSocketTransportError()
            offset += sent
        self._check_active()

    def _receive_exact(self, size: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            self._check_active()
            if not self._stream.wait_readable(self._slice(deadline)):
                continue
            self._check_active()
            try:
                chunk = self._stream.recv(remaining)
            except (TimeoutError, _IoSliceTimeout):
                continue
            if not isinstance(chunk, bytes) or not chunk or len(chunk) > remaining:
                raise HyperVSocketTransportError()
            chunks.append(chunk)
            remaining -= len(chunk)
        self._check_active()
        return b"".join(chunks)


def _accept_windows_host_service(
    vm_id: uuid.UUID,
    *,
    service_id: uuid.UUID,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    native: WindowsWinsockApi | None = None,
    clock: Callable[[], float] = time.monotonic,
    listening: Callable[[], None] | None = None,
) -> JsonFrameConnection:
    """Bind the fixed service to one exact VM and accept exactly once."""
    listener: int | None = None
    accepted: int | None = None
    api = native
    try:
        _validate_vm_id(vm_id)
        timeout = _validate_timeout(timeout_seconds)
        probe = cancelled or _never_cancelled
        _require_not_cancelled(probe)
        api = api or _CtypesWinsock()
        api.startup()
        listener = api.socket(AF_HYPERV, _socket.SOCK_STREAM, HV_PROTOCOL_RAW)
        _validate_handle(listener)
        api.set_timeout(listener, min(timeout, _IO_SLICE_SECONDS))
        api.bind(listener, _sockaddr_hv(vm_id, service_id))
        api.listen(listener, 1)
        if listening is not None:
            listening()
        deadline = clock() + timeout
        while accepted is None:
            _require_not_cancelled(probe)
            remaining = deadline - clock()
            if remaining <= 0:
                raise HyperVSocketTransportError()
            if not api.wait_readable(listener, min(_IO_SLICE_SECONDS, remaining)):
                continue
            try:
                accepted = api.accept(listener)
            except (TimeoutError, _IoSliceTimeout):
                continue
        _validate_handle(accepted)
        api.close(listener)
        listener = None
        _require_not_cancelled(probe)
        api.set_timeout(accepted, min(timeout, _IO_SLICE_SECONDS))
        connection = JsonFrameConnection(
            _NativeStream(api, accepted),
            cancelled=probe,
            timeout_seconds=timeout,
            clock=clock,
        )
        accepted = None
        return connection
    except HyperVSocketTransportCancelled:
        if api is not None:
            _close_native(api, accepted)
            _close_native(api, listener, shutdown=False)
        raise
    except Exception:
        if api is not None:
            _close_native(api, accepted)
            _close_native(api, listener, shutdown=False)
        raise HyperVSocketTransportError() from None


def accept_windows_host(
    vm_id: uuid.UUID,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    native: WindowsWinsockApi | None = None,
    clock: Callable[[], float] = time.monotonic,
    listening: Callable[[], None] | None = None,
) -> JsonFrameConnection:
    """Bind the execution service to one exact VM and accept exactly once."""
    return _accept_windows_host_service(
        vm_id,
        service_id=SERVICE_ID,
        timeout_seconds=timeout_seconds,
        cancelled=cancelled,
        native=native,
        clock=clock,
        listening=listening,
    )


def accept_windows_provisioning_host(
    vm_id: uuid.UUID,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    native: WindowsWinsockApi | None = None,
    clock: Callable[[], float] = time.monotonic,
    listening: Callable[[], None] | None = None,
) -> JsonFrameConnection:
    """Bind the one-time provisioning service to one exact VM."""
    return _accept_windows_host_service(
        vm_id,
        service_id=PROVISIONING_SERVICE_ID,
        timeout_seconds=timeout_seconds,
        cancelled=cancelled,
        native=native,
        clock=clock,
        listening=listening,
    )


def _connect_linux_guest_service(
    *,
    guest_port: int,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    socket_module: object | None = None,
    platform_name: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> JsonFrameConnection:
    """Connect a Linux guest to CID 2 using only standard named constants."""
    module = socket_module or _socket
    sock: object | None = None
    probe = cancelled or _never_cancelled
    try:
        if (platform_name or sys.platform) != "linux":
            raise HyperVSocketTransportError()
        family = getattr(module, "AF_VSOCK")
        host_cid = getattr(module, "VMADDR_CID_HOST")
        sock_type = getattr(module, "SOCK_STREAM")
        if type(host_cid) is not int or host_cid != 2:
            raise HyperVSocketTransportError()
        timeout = _validate_timeout(timeout_seconds)
        deadline = clock() + timeout
        while sock is None:
            _require_not_cancelled(probe)
            remaining = deadline - clock()
            if remaining <= 0:
                raise HyperVSocketTransportError()
            candidate = module.socket(family, sock_type)  # type: ignore[attr-defined]
            try:
                candidate.settimeout(min(remaining, _IO_SLICE_SECONDS))
                candidate.connect((host_cid, guest_port))
            except (OSError, TimeoutError):
                _close_python_socket(candidate)
                remaining = deadline - clock()
                if remaining <= 0:
                    raise HyperVSocketTransportError() from None
                sleeper(min(_IO_SLICE_SECONDS, remaining))
                continue
            sock = candidate
        _require_not_cancelled(probe)
        stream = _PythonSocketStream(sock)
        sock = None
        return JsonFrameConnection(stream, cancelled=probe, timeout_seconds=timeout)
    except HyperVSocketTransportCancelled:
        _close_python_socket(sock)
        raise
    except Exception:
        _close_python_socket(sock)
        raise HyperVSocketTransportError() from None


def connect_linux_guest(
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    socket_module: object | None = None,
    platform_name: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> JsonFrameConnection:
    """Connect the execution worker to the fixed host service."""
    return _connect_linux_guest_service(
        guest_port=GUEST_PORT,
        timeout_seconds=timeout_seconds,
        cancelled=cancelled,
        socket_module=socket_module,
        platform_name=platform_name,
        clock=clock,
        sleeper=sleeper,
    )


def connect_linux_provisioning_guest(
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    socket_module: object | None = None,
    platform_name: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> JsonFrameConnection:
    """Connect the key-free guest to the fixed one-time provisioning service."""
    return _connect_linux_guest_service(
        guest_port=PROVISIONING_PORT,
        timeout_seconds=timeout_seconds,
        cancelled=cancelled,
        socket_module=socket_module,
        platform_name=platform_name,
        clock=clock,
        sleeper=sleeper,
    )


def _sockaddr_hv(vm_id: uuid.UUID, service_id: uuid.UUID = SERVICE_ID) -> bytes:
    if not isinstance(service_id, uuid.UUID) or service_id.int == 0:
        raise HyperVSocketTransportError()
    raw = struct.pack("<HH", AF_HYPERV, 0) + vm_id.bytes_le + service_id.bytes_le
    if len(raw) != _SOCKADDR_HV_BYTES:
        raise HyperVSocketTransportError()
    return raw


def _canonical_json(value: object) -> bytes:
    _validate_json(value, depth=0)
    rendered = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return rendered.encode("ascii")


def _validate_json(value: object, *, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        raise HyperVSocketTransportError()
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise HyperVSocketTransportError()
        return
    if type(value) is list:
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise HyperVSocketTransportError()
            _validate_json(item, depth=depth + 1)
        return
    raise HyperVSocketTransportError()


def _unique_object(pairs: list[tuple[str, object]]) -> Mapping[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise HyperVSocketTransportError()
        value[key] = item
    return value


def _reject_constant(_value: str) -> object:
    raise HyperVSocketTransportError()


def _validate_vm_id(vm_id: object) -> None:
    if type(vm_id) is not uuid.UUID or vm_id.int == 0:
        raise HyperVSocketTransportError()


def _validate_timeout(value: object) -> float:
    if type(value) not in (int, float):
        raise HyperVSocketTransportError()
    timeout = float(value)
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise HyperVSocketTransportError()
    return timeout


def _validate_handle(handle: object) -> None:
    if type(handle) is not int or handle < 0 or handle == _INVALID_SOCKET:
        raise HyperVSocketTransportError()


def _require_not_cancelled(probe: Callable[[], bool]) -> None:
    try:
        cancelled = probe()
    except Exception:
        raise HyperVSocketTransportError() from None
    if cancelled is not False:
        raise HyperVSocketTransportCancelled()


def _never_cancelled() -> bool:
    return False


def _close_native(
    api: WindowsWinsockApi,
    handle: int | None,
    *,
    shutdown: bool = True,
) -> None:
    if handle is None:
        return
    if shutdown:
        try:
            api.shutdown(handle)
        except Exception:
            pass
    try:
        api.close(handle)
    except Exception:
        pass


def _close_python_socket(sock: object | None) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(_socket.SHUT_RDWR)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        sock.close()  # type: ignore[attr-defined]
    except Exception:
        pass


__all__ = [
    "AF_HYPERV",
    "DEFAULT_TIMEOUT_SECONDS",
    "GUEST_PORT",
    "PROVISIONING_PORT",
    "PROVISIONING_SERVICE_ID",
    "HV_PROTOCOL_RAW",
    "HyperVSocketTransportCancelled",
    "HyperVSocketTransportError",
    "JsonFrameConnection",
    "MAX_JSON_FRAME_BYTES",
    "SERVICE_ID",
    "WindowsWinsockApi",
    "accept_windows_host",
    "accept_windows_provisioning_host",
    "connect_linux_guest",
    "connect_linux_provisioning_guest",
]
