from __future__ import annotations

import json
import socket
import struct
import threading
import uuid

import pytest

from yonerai_discord.capability_forge import hyperv_socket_transport as transport


class _FakeNative:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.next_handle = 10
        self.accepted_handle = 20
        self.fail_at: str | None = None
        self.failures: dict[str, int] = {}
        self.readable = True
        self.on_poll = None

    def _call(self, name: str, *values: object) -> None:
        self.calls.append((name, *values))
        if self.failures.get(name, 0):
            self.failures[name] -= 1
            raise OSError("private native failure token")
        if self.fail_at == name:
            raise OSError("private native failure token")

    def startup(self) -> None:
        self._call("startup")

    def socket(self, family: int, sock_type: int, protocol: int) -> int:
        self._call("socket", family, sock_type, protocol)
        return self.next_handle

    def bind(self, handle: int, sockaddr: bytes) -> None:
        self._call("bind", handle, sockaddr)

    def listen(self, handle: int, backlog: int) -> None:
        self._call("listen", handle, backlog)

    def accept(self, handle: int) -> int:
        self._call("accept", handle)
        return self.accepted_handle

    def set_timeout(self, handle: int, timeout_seconds: float) -> None:
        self._call("set_timeout", handle, timeout_seconds)

    def wait_readable(self, handle: int, timeout_seconds: float) -> bool:
        self._call("wait_readable", handle, timeout_seconds)
        if self.on_poll is not None:
            self.on_poll(timeout_seconds)
        return self.readable

    def wait_writable(self, handle: int, timeout_seconds: float) -> bool:
        self._call("wait_writable", handle, timeout_seconds)
        return True

    def send(self, handle: int, data: bytes) -> int:
        self._call("send", handle, len(data))
        return len(data)

    def recv(self, handle: int, maximum: int) -> bytes:
        self._call("recv", handle, maximum)
        return b""

    def shutdown(self, handle: int) -> None:
        self._call("shutdown", handle)

    def close(self, handle: int) -> None:
        self._call("close", handle)


class _FakeStream:
    def __init__(self, incoming: bytes = b"", *, send_limit: int = 3, recv_limit: int = 2) -> None:
        self.incoming = bytearray(incoming)
        self.outgoing = bytearray()
        self.send_limit = send_limit
        self.recv_limit = recv_limit
        self.closed = False
        self.close_attempts = 0
        self.close_failures = 0
        self.failure: Exception | None = None
        self.on_io = None
        self.on_wait = None
        self.readable = True

    def wait_readable(self, timeout_seconds: float) -> bool:
        if self.on_wait is not None:
            self.on_wait(timeout_seconds)
        return self.readable

    def wait_writable(self, timeout_seconds: float) -> bool:
        if self.on_wait is not None:
            self.on_wait(timeout_seconds)
        return True

    def send(self, data: bytes) -> int:
        if self.failure is not None:
            raise self.failure
        if self.on_io is not None:
            self.on_io()
        count = min(self.send_limit, len(data))
        self.outgoing.extend(data[:count])
        return count

    def recv(self, maximum: int) -> bytes:
        if self.failure is not None:
            raise self.failure
        if self.on_io is not None:
            self.on_io()
        count = min(self.recv_limit, maximum, len(self.incoming))
        chunk = bytes(self.incoming[:count])
        del self.incoming[:count]
        return chunk

    def close(self) -> None:
        self.close_attempts += 1
        if self.close_failures:
            self.close_failures -= 1
            raise OSError("private stream close token")
        self.closed = True


class _BlockingCloseStream(_FakeStream):
    def __init__(self) -> None:
        super().__init__()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.second_close = threading.Event()

    def close(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            self.close_entered.set()
            assert self.close_release.wait(1)
        else:
            self.second_close.set()
        self.closed = True


def _frame(body: bytes) -> bytes:
    return struct.pack("!I", len(body)) + body


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_code_owned_address_constants_are_exact() -> None:
    assert transport.AF_HYPERV == 34
    assert transport.HV_PROTOCOL_RAW == 1
    assert transport.GUEST_PORT == 40509
    assert transport.SERVICE_ID == uuid.UUID("00009e3d-facb-11e6-bd58-64006a7986d3")


def test_windows_host_binds_exact_vm_and_service_then_accepts_once() -> None:
    native = _FakeNative()
    vm_id = uuid.UUID("12345678-1234-5678-9abc-def012345678")
    connection = transport.accept_windows_host(vm_id, timeout_seconds=4.25, native=native)

    assert [call[0] for call in native.calls] == [
        "startup",
        "socket",
        "set_timeout",
        "bind",
        "listen",
        "wait_readable",
        "accept",
        "close",
        "set_timeout",
    ]
    assert native.calls[1] == ("socket", 34, socket.SOCK_STREAM, 1)
    raw = native.calls[3][2]
    assert isinstance(raw, bytes) and len(raw) == 36
    assert raw[:4] == struct.pack("<HH", 34, 0)
    assert raw[4:20] == vm_id.bytes_le
    assert raw[20:] == transport.SERVICE_ID.bytes_le
    assert native.calls[2] == ("set_timeout", 10, 0.1)
    assert native.calls[4] == ("listen", 10, 1)
    assert native.calls[7] == ("close", 10)
    assert native.calls[8] == ("set_timeout", 20, 0.1)

    connection.close()
    connection.close()
    assert native.calls[-2:] == [("shutdown", 20), ("close", 20)]


@pytest.mark.parametrize("vm_id", (uuid.UUID(int=0), "12345678-1234-5678-9abc-def012345678", None))
def test_windows_host_rejects_wildcard_and_untyped_vm_id_before_native_calls(vm_id: object) -> None:
    native = _FakeNative()
    with pytest.raises(transport.HyperVSocketTransportError) as raised:
        transport.accept_windows_host(vm_id, native=native)  # type: ignore[arg-type]
    assert str(raised.value) == "Hyper-V socket transport failed safely"
    assert native.calls == []


def test_windows_accept_never_readable_times_out_and_closes_listener_once() -> None:
    native = _FakeNative()
    native.readable = False
    clock = _Clock()
    native.on_poll = clock.advance
    with pytest.raises(transport.HyperVSocketTransportError):
        transport.accept_windows_host(
            uuid.UUID("12345678-1234-5678-9abc-def012345678"),
            timeout_seconds=0.25,
            native=native,
            clock=clock,
        )
    assert sum(call == ("close", 10) for call in native.calls) == 1
    assert not any(call[0] == "accept" for call in native.calls)


def test_windows_accept_poll_cancellation_closes_listener_once() -> None:
    native = _FakeNative()
    native.readable = False
    state = {"cancelled": False}
    native.on_poll = lambda _seconds: state.__setitem__("cancelled", True)
    with pytest.raises(transport.HyperVSocketTransportCancelled):
        transport.accept_windows_host(
            uuid.UUID("12345678-1234-5678-9abc-def012345678"),
            native=native,
            cancelled=lambda: state["cancelled"],
        )
    assert sum(call == ("close", 10) for call in native.calls) == 1


def test_windows_accept_handle_zero_is_valid_and_closed_once() -> None:
    native = _FakeNative()
    native.accepted_handle = 0
    connection = transport.accept_windows_host(
        uuid.UUID("12345678-1234-5678-9abc-def012345678"),
        native=native,
    )
    connection.close()
    assert sum(call == ("close", 0) for call in native.calls) == 1


def test_windows_stream_close_failure_is_content_free_and_retryable() -> None:
    native = _FakeNative()
    connection = transport.accept_windows_host(
        uuid.UUID("12345678-1234-5678-9abc-def012345678"),
        native=native,
    )
    native.failures["close"] = 1

    with pytest.raises(transport.HyperVSocketTransportError) as raised:
        connection.close()
    assert "private" not in repr(raised.value) and connection.closed is False

    connection.close()
    assert connection.closed is True
    assert sum(call == ("close", 20) for call in native.calls) == 2


@pytest.mark.parametrize("failure", ("startup", "socket", "bind", "listen", "accept", "close", "set_timeout"))
def test_windows_native_failures_are_content_free_and_close_owned_handles(failure: str) -> None:
    native = _FakeNative()
    native.fail_at = failure
    vm_id = uuid.UUID("12345678-1234-5678-9abc-def012345678")
    with pytest.raises(transport.HyperVSocketTransportError) as raised:
        transport.accept_windows_host(vm_id, native=native)
    visible = repr(raised.value) + str(raised.value)
    assert "private" not in visible and "token" not in visible
    if failure not in ("startup", "socket"):
        assert any(call[0] == "close" for call in native.calls)


def test_canonical_json_write_is_big_endian_and_handles_partial_send() -> None:
    stream = _FakeStream(send_limit=2)
    connection = transport.JsonFrameConnection(stream)
    value = {"z": "文字", "a": [True, 1]}
    connection.send_json(value)

    body = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    assert bytes(stream.outgoing) == struct.pack("!I", len(body)) + body
    assert connection.closed is False


def test_canonical_json_read_handles_partial_receive() -> None:
    body = b'{"a":[true,1],"z":"\\u6587\\u5b57"}'
    connection = transport.JsonFrameConnection(_FakeStream(_frame(body), recv_limit=1))
    assert connection.receive_json() == {"a": [True, 1], "z": "文字"}


@pytest.mark.parametrize(
    "wire",
    (
        b"\x00\x00",
        struct.pack("!I", transport.MAX_JSON_FRAME_BYTES + 1),
        _frame(b'{"b":1,"a":2}'),
        _frame(b'{"a":1,"a":2}'),
        _frame(b"NaN"),
        _frame(b'{"a":'),
    ),
)
def test_eof_oversize_and_noncanonical_input_fail_closed(wire: bytes) -> None:
    stream = _FakeStream(wire)
    connection = transport.JsonFrameConnection(stream)
    with pytest.raises(transport.HyperVSocketTransportError):
        connection.receive_json()
    assert connection.closed is True and stream.closed is True


@pytest.mark.parametrize("bad", ({1: "coerced"}, ("tuple",), float("nan")))
def test_noncanonical_outgoing_values_fail_closed(bad: object) -> None:
    stream = _FakeStream()
    connection = transport.JsonFrameConnection(stream)
    with pytest.raises(transport.HyperVSocketTransportError):
        connection.send_json(bad)
    assert stream.closed is True and stream.outgoing == b""


def test_timeout_like_io_error_is_sanitized_and_closes() -> None:
    stream = _FakeStream()
    stream.failure = TimeoutError("private endpoint timeout")
    clock = _Clock()
    stream.on_wait = clock.advance
    connection = transport.JsonFrameConnection(stream, timeout_seconds=0.25, clock=clock)
    with pytest.raises(transport.HyperVSocketTransportError) as raised:
        connection.send_json({"a": 1})
    assert str(raised.value) == "Hyper-V socket transport failed safely"
    assert "endpoint" not in repr(raised.value) and stream.closed is True


def test_cancellation_during_partial_io_closes_with_fixed_exception() -> None:
    state = {"cancelled": False}
    stream = _FakeStream(send_limit=1)
    stream.on_io = lambda: state.__setitem__("cancelled", True)
    connection = transport.JsonFrameConnection(stream, cancelled=lambda: state["cancelled"])
    with pytest.raises(transport.HyperVSocketTransportCancelled) as raised:
        connection.send_json({"a": 1})
    assert str(raised.value) == "Hyper-V socket transport was cancelled safely"
    assert stream.closed is True


def test_frame_close_failure_is_content_free_and_retryable() -> None:
    stream = _FakeStream()
    stream.close_failures = 1
    connection = transport.JsonFrameConnection(stream)

    with pytest.raises(transport.HyperVSocketTransportError) as raised:
        connection.close()
    assert "private" not in repr(raised.value)
    assert connection.closed is False and stream.close_attempts == 1

    connection.close()
    assert connection.closed is True and stream.closed is True
    assert stream.close_attempts == 2


def test_concurrent_frame_close_is_serialized_without_double_close() -> None:
    stream = _BlockingCloseStream()
    connection = transport.JsonFrameConnection(stream)
    errors: list[Exception] = []

    def close_connection() -> None:
        try:
            connection.close()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=close_connection)
    first.start()
    assert stream.close_entered.wait(1)
    second = threading.Thread(target=close_connection)
    second.start()
    assert not stream.second_close.wait(0.05)
    stream.close_release.set()
    first.join(1)
    second.join(1)

    assert not first.is_alive() and not second.is_alive()
    assert errors == [] and connection.closed is True
    assert stream.close_attempts == 1


def test_no_data_receive_poll_cancellation_is_bounded_and_closes() -> None:
    state = {"cancelled": False}
    stream = _FakeStream()
    stream.readable = False
    stream.on_wait = lambda _seconds: state.__setitem__("cancelled", True)
    connection = transport.JsonFrameConnection(stream, cancelled=lambda: state["cancelled"])
    with pytest.raises(transport.HyperVSocketTransportCancelled):
        connection.receive_json()
    assert stream.closed is True


class _GuestSocket:
    def __init__(self, *, fail_connect: bool = False) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.fail_connect = fail_connect
        self.close_failures = 0

    def settimeout(self, value: float) -> None:
        self.calls.append(("settimeout", value))

    def connect(self, address: tuple[int, int]) -> None:
        self.calls.append(("connect", address))
        if self.fail_connect:
            raise OSError("private guest path")

    def send(self, data: bytes) -> int:
        self.calls.append(("send", data))
        return len(data)

    def recv(self, maximum: int) -> bytes:
        self.calls.append(("recv", maximum))
        return b""

    def shutdown(self, how: int) -> None:
        self.calls.append(("shutdown", how))

    def close(self) -> None:
        self.calls.append(("close",))
        if self.close_failures:
            self.close_failures -= 1
            raise OSError("private guest close token")


class _GuestSocketModule:
    AF_VSOCK = object()
    VMADDR_CID_HOST = 2
    SOCK_STREAM = object()

    def __init__(self, guest: _GuestSocket) -> None:
        self.guest = guest
        self.calls: list[tuple[object, ...]] = []

    def socket(self, family: object, sock_type: object) -> _GuestSocket:
        self.calls.append(("socket", family, sock_type))
        return self.guest


class _GuestSocketSequenceModule(_GuestSocketModule):
    def __init__(self, guests: list[_GuestSocket]) -> None:
        super().__init__(guests[-1])
        self.guests = list(guests)

    def socket(self, family: object, sock_type: object) -> _GuestSocket:
        self.calls.append(("socket", family, sock_type))
        return self.guests.pop(0)


def test_linux_guest_uses_named_af_vsock_and_exact_host_cid_two() -> None:
    guest = _GuestSocket()
    module = _GuestSocketModule(guest)
    connection = transport.connect_linux_guest(
        timeout_seconds=5,
        socket_module=module,
        platform_name="linux",
    )
    assert module.calls == [("socket", module.AF_VSOCK, module.SOCK_STREAM)]
    assert guest.calls[:2] == [("settimeout", 0.1), ("connect", (2, 40509))]
    connection.close()
    assert guest.calls[-2:] == [("shutdown", socket.SHUT_RDWR), ("close",)]


def test_linux_guest_retries_fresh_sockets_until_host_listener_is_ready() -> None:
    first = _GuestSocket(fail_connect=True)
    second = _GuestSocket()
    module = _GuestSocketSequenceModule([first, second])
    now = {"value": 0.0}

    connection = transport.connect_linux_guest(
        timeout_seconds=1,
        socket_module=module,
        platform_name="linux",
        clock=lambda: now["value"],
        sleeper=lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    assert len(module.calls) == 2
    assert first.calls[-2:] == [("shutdown", socket.SHUT_RDWR), ("close",)]
    assert second.calls[:2] == [("settimeout", 0.1), ("connect", (2, 40509))]
    connection.close()


def test_linux_guest_close_failure_is_content_free_and_retryable() -> None:
    guest = _GuestSocket()
    connection = transport.connect_linux_guest(
        socket_module=_GuestSocketModule(guest),
        platform_name="linux",
    )
    guest.close_failures = 1

    with pytest.raises(transport.HyperVSocketTransportError) as raised:
        connection.close()
    assert "private" not in repr(raised.value) and connection.closed is False

    connection.close()
    assert connection.closed is True
    assert sum(call == ("close",) for call in guest.calls) == 2


@pytest.mark.parametrize("cid", (None, 0, 1, 3, "2"))
def test_linux_guest_has_no_numeric_cid_fallback(cid: object) -> None:
    guest = _GuestSocket()
    module = _GuestSocketModule(guest)
    module.VMADDR_CID_HOST = cid
    with pytest.raises(transport.HyperVSocketTransportError):
        transport.connect_linux_guest(socket_module=module, platform_name="linux")
    assert module.calls == [] and guest.calls == []


def test_linux_guest_requires_standard_named_constants_and_linux_platform() -> None:
    class MissingConstants:
        def socket(self, *_args: object) -> object:
            raise AssertionError("must not open a socket")

    with pytest.raises(transport.HyperVSocketTransportError):
        transport.connect_linux_guest(socket_module=MissingConstants(), platform_name="linux")
    with pytest.raises(transport.HyperVSocketTransportError):
        transport.connect_linux_guest(socket_module=MissingConstants(), platform_name="win32")


def test_linux_connect_failure_closes_and_sanitizes() -> None:
    guest = _GuestSocket(fail_connect=True)
    module = _GuestSocketModule(guest)
    with pytest.raises(transport.HyperVSocketTransportError) as raised:
        transport.connect_linux_guest(
            timeout_seconds=0.1,
            socket_module=module,
            platform_name="linux",
            clock=lambda: 0.0 if len(module.calls) == 0 else 0.1,
            sleeper=lambda _seconds: None,
        )
    assert "private" not in repr(raised.value)
    assert guest.calls[-2:] == [("shutdown", socket.SHUT_RDWR), ("close",)]


@pytest.mark.parametrize("timeout", (0, -1, 301, float("inf"), True, "1"))
def test_timeout_is_fixed_type_and_bounded(timeout: object) -> None:
    native = _FakeNative()
    with pytest.raises(transport.HyperVSocketTransportError):
        transport.accept_windows_host(
            uuid.UUID("12345678-1234-5678-9abc-def012345678"),
            timeout_seconds=timeout,  # type: ignore[arg-type]
            native=native,
        )
    assert native.calls == []
