from __future__ import annotations

import asyncio
import json
import threading

import pytest

from yonerai_discord.capability_forge import named_pipe_exchange as exchange


def _frame(value: object) -> bytes:
    body = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    return len(body).to_bytes(4, "big") + body


class _Api:
    def __init__(self, response: bytes) -> None:
        self.response = bytearray(response)
        self.opened: list[tuple[str, int]] = []
        self.written = bytearray()
        self.closed: list[int] = []
        self.cancelled: list[int] = []
        self.identity = (
            42,
            str(exchange._BROKER_IMAGE_PATH),
            "S-1-5-18",
        )

    def open(self, name: str, timeout_ms: int) -> int:
        self.opened.append((name, timeout_ms))
        return 7

    def server_identity(self, handle: int) -> tuple[int, str, str]:
        assert handle == 7
        return self.identity

    def write(self, handle: int, value: bytes, *, deadline: float | None = None) -> int:
        assert handle == 7
        assert deadline is not None
        size = min(3, len(value))
        self.written.extend(value[:size])
        return size

    def read(self, handle: int, maximum: int, *, deadline: float | None = None) -> bytes:
        assert handle == 7
        assert deadline is not None
        size = min(2, maximum, len(self.response))
        result = bytes(self.response[:size])
        del self.response[:size]
        return result

    def cancel(self, handle: int) -> None:
        self.cancelled.append(handle)

    def close(self, handle: int) -> None:
        self.closed.append(handle)


@pytest.mark.asyncio
async def test_exchange_uses_only_fixed_pipe_system_server_and_bounded_frames() -> None:
    request = _frame({"schema": "yonerai.exec-sandbox.broker-run.v1"})
    response = _frame({"schema": "yonerai.exec-sandbox.broker-receipt.v2"})
    api = _Api(response)
    client = exchange.FixedNamedPipeExchange(api=api, readiness_probe=lambda: True)

    result = await client.exchange(request)

    assert result == response
    assert bytes(api.written) == exchange.AUTH_PREAMBLE + request
    assert api.opened == [(r"\\.\pipe\YonerAI-ForgeSandbox-Broker-v1", 1_000)]
    assert api.closed == [7]
    assert api.cancelled == []
    assert client.ready is client.verified_current is True
    assert exchange._BROKER_IMAGE_PATH == exchange._BROKER_PACK / "python.exe"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    (
        (0, str(exchange._BROKER_IMAGE_PATH), "S-1-5-18"),
        (42, "untrusted.exe", "S-1-5-18"),
        (42, str(exchange._BROKER_IMAGE_PATH), "S-1-5-21-1"),
    ),
)
async def test_exchange_rejects_non_system_or_wrong_image_server(identity: tuple[int, str, str]) -> None:
    api = _Api(_frame({"schema": "yonerai.exec-sandbox.broker-receipt.v2"}))
    api.identity = identity
    client = exchange.FixedNamedPipeExchange(api=api, readiness_probe=lambda: True)

    with pytest.raises(exchange.NamedPipeExchangeError, match="failed safely"):
        await client.exchange(_frame({"schema": "yonerai.exec-sandbox.broker-run.v1"}))

    assert api.closed == [7]


@pytest.mark.asyncio
async def test_exchange_fails_before_pipe_when_readiness_is_not_current() -> None:
    api = _Api(_frame({"schema": "yonerai.exec-sandbox.broker-receipt.v2"}))
    client = exchange.FixedNamedPipeExchange(api=api, readiness_probe=lambda: False)

    with pytest.raises(exchange.NamedPipeExchangeError, match="failed safely"):
        await client.exchange(_frame({"schema": "yonerai.exec-sandbox.broker-run.v1"}))

    assert client.ready is client.verified_current is False
    assert api.opened == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    (
        b"",
        b"\x00\x00\x00\x02{}extra",
        b"\x00\x00\x00\x01x",
    ),
)
async def test_exchange_rejects_invalid_request_or_response_frames(frame: bytes) -> None:
    api = _Api(frame)
    client = exchange.FixedNamedPipeExchange(api=api, readiness_probe=lambda: True)
    request = frame if frame != b"\x00\x00\x00\x01x" else _frame({"schema": "run"})

    with pytest.raises(exchange.NamedPipeExchangeError, match="failed safely"):
        await client.exchange(request)


def test_close_withdraws_readiness_and_repr_is_content_free() -> None:
    api = _Api(_frame({"schema": "receipt"}))
    client = exchange.FixedNamedPipeExchange(api=api, readiness_probe=lambda: True)

    client.begin_close()

    assert client.ready is client.verified_current is False
    assert "ProgramData" not in repr(client)
    assert "YonerAI-ForgeSandbox" not in repr(client)
    assert exchange.OPERATION_TIMEOUT_SECONDS < 195


@pytest.mark.asyncio
async def test_cancelling_one_exchange_cancels_its_real_handle_without_closing_client() -> None:
    class BlockingApi(_Api):
        def __init__(self) -> None:
            super().__init__(_frame({"schema": "receipt"}))
            self.read_started = threading.Event()
            self.release = threading.Event()

        def read(self, handle: int, maximum: int, *, deadline: float | None = None) -> bytes:
            self.read_started.set()
            self.release.wait(2)
            return b""

        def cancel(self, handle: int) -> None:
            super().cancel(handle)
            self.release.set()

    api = BlockingApi()
    client = exchange.FixedNamedPipeExchange(api=api, readiness_probe=lambda: True)
    task = asyncio.create_task(client.exchange(_frame({"schema": "run"})))
    assert await asyncio.to_thread(api.read_started.wait, 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert api.cancelled == [7]
    assert client.ready is client.verified_current is True
    assert not any(thread.name == "yonerai-pipe-client" for thread in threading.enumerate())


@pytest.mark.asyncio
async def test_cancel_during_open_never_publishes_or_writes_the_late_handle() -> None:
    class DelayedOpenApi(_Api):
        def __init__(self) -> None:
            super().__init__(_frame({"schema": "receipt"}))
            self.open_started = threading.Event()
            self.release_open = threading.Event()

        def open(self, name: str, timeout_ms: int) -> int:
            self.open_started.set()
            assert self.release_open.wait(2)
            return super().open(name, timeout_ms)

    api = DelayedOpenApi()
    client = exchange.FixedNamedPipeExchange(api=api, readiness_probe=lambda: True)
    task = asyncio.create_task(client.exchange(_frame({"schema": "run"})))
    assert await asyncio.to_thread(api.open_started.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    api.release_open.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert api.written == b""
    assert api.cancelled == []
    assert api.closed == [7]
    assert not any(thread.name == "yonerai-pipe-client" for thread in threading.enumerate())


def test_ctypes_pipe_uses_overlapped_absolute_deadline_and_bounded_cancel_join() -> None:
    source = exchange.Path(exchange.__file__).read_text(encoding="utf-8")
    assert "_FILE_FLAG_OVERLAPPED" in source
    assert "GetOverlappedResult" in source
    assert "CancelIoEx" in source
    assert "_ERROR_NOT_FOUND" in source
    assert "deadline=deadline" in source
    assert exchange.IO_CANCEL_JOIN_SECONDS <= 3


def test_cancelled_client_overlapped_io_is_reaped_before_storage_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Kernel:
        def CancelIoEx(self, _handle: int, _overlapped: object) -> int:
            calls.append("cancel")
            return 1

        def WaitForSingleObject(self, _event: object, _timeout: int) -> int:
            calls.append("wait")
            return exchange._CtypesNamedPipeApi._WAIT_OBJECT_0

        def GetOverlappedResult(
            self,
            _handle: int,
            _overlapped: object,
            _transferred: object,
            _wait: int,
        ) -> int:
            calls.append("result")
            return 0

    api = object.__new__(exchange._CtypesNamedPipeApi)
    api._kernel32 = Kernel()
    monkeypatch.setattr(
        exchange.ctypes,
        "get_last_error",
        lambda: exchange._CtypesNamedPipeApi._ERROR_OPERATION_ABORTED,
        raising=False,
    )
    overlapped = exchange._CtypesNamedPipeApi._Overlapped()
    overlapped.hEvent = 99

    api._cancel_one(7, overlapped)

    assert calls == ["cancel", "wait", "result"]


def test_client_fails_fast_instead_of_freeing_a_still_pending_overlapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Kernel:
        def CancelIoEx(self, _handle: int, _overlapped: object) -> int:
            return 1

        def WaitForSingleObject(self, _event: object, timeout: int) -> int:
            assert timeout <= 3_000
            return exchange._CtypesNamedPipeApi._WAIT_TIMEOUT

    api = object.__new__(exchange._CtypesNamedPipeApi)
    api._kernel32 = Kernel()
    exits: list[int] = []

    def fail_fast(code: int) -> None:
        exits.append(code)
        raise SystemExit

    monkeypatch.setattr(exchange.os, "_exit", fail_fast)
    overlapped = exchange._CtypesNamedPipeApi._Overlapped()
    overlapped.hEvent = 99

    with pytest.raises(SystemExit):
        api._cancel_one(7, overlapped)
    assert exits == [70]


def test_cancel_and_close_are_serialized_under_handle_ownership_lock() -> None:
    source = exchange.Path(exchange.__file__).read_text(encoding="utf-8")
    cancel_block = source[source.index("    def _cancel_active") : source.index("    def __repr__")]
    finally_block = source[
        source.index("        finally:", source.index("    def _exchange_sync")) : source.index(
            "    @staticmethod", source.index("    def _exchange_sync")
        )
    ]

    assert cancel_block.index("with self._handles_lock:") < cancel_block.index("api.cancel(handle)")
    assert finally_block.index("with self._handles_lock:") < finally_block.index("api.close(handle)")
