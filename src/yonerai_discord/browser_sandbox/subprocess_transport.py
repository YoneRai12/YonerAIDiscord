from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from .worker_contract import (
    BrowserWorkerContractError,
    BrowserWorkerExecuteRequest,
    BrowserWorkerExecutionResult,
    BrowserWorkerHandshakeRequest,
    BrowserWorkerHandshakeResponse,
    BrowserWorkerTerminateRequest,
    BrowserWorkerTerminationReceipt,
    validate_worker_termination,
)
from .worker_wire import (
    MAX_BROWSER_WORKER_WIRE_BYTES,
    decode_worker_execution_result,
    decode_worker_handshake_response,
    decode_worker_termination_receipt,
    encode_worker_execute_request,
    encode_worker_handshake_request,
    encode_worker_terminate_request,
)


_MAX_CONTROL_FRAME_BYTES = 64 * 1024
_MAX_ARGV_ITEMS = 32
_MAX_ENV_ITEMS = 32
_FRAME_HEADER_BYTES = 4
_T = TypeVar("_T")


class BrowserWorkerSubprocessError(BrowserWorkerContractError):
    """The direct child failed safely; this does not prove descendant-process cleanup."""


class BrowserWorkerSubprocessState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    HANDSHAKEN = "handshaken"
    EXECUTED = "executed"
    CLOSING = "closing"
    TERMINATING = "terminating"
    CLOSED = "closed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class BrowserWorkerSubprocessTransport:
    """One direct child and one protocol session over a bounded length-prefixed pipe.

    This transport is intentionally not bound to BrowserSandboxService. It does not
    contain or reap descendants, so a worker that launches a browser process requires
    a container, Windows Job Object, or equivalent process-tree boundary first.
    """

    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        cwd: str | Path,
        env: Mapping[str, str],
        operation_timeout_seconds: float = 30.0,
        cleanup_timeout_seconds: float = 2.0,
    ) -> None:
        self._argv = _validated_argv(argv)
        self._cwd = _validated_cwd(cwd)
        self._env = _validated_env(env)
        self._timeout = _validated_timeout(operation_timeout_seconds, "operation_timeout_seconds")
        self._cleanup_timeout = _validated_timeout(cleanup_timeout_seconds, "cleanup_timeout_seconds")
        self._lock = asyncio.Lock()
        self._state = BrowserWorkerSubprocessState.NEW
        self._process: asyncio.subprocess.Process | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> BrowserWorkerSubprocessState:
        return self._state

    async def handshake(self, request: BrowserWorkerHandshakeRequest) -> BrowserWorkerHandshakeResponse:
        async with self._lock:
            self._require_state(BrowserWorkerSubprocessState.NEW)
            self._state = BrowserWorkerSubprocessState.STARTING
            try:
                async with asyncio.timeout(self._timeout):
                    await self._spawn()
                    response = await self._exchange(
                        encode_worker_handshake_request(request),
                        decode_worker_handshake_response,
                        _MAX_CONTROL_FRAME_BYTES,
                    )
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise BrowserWorkerSubprocessError("browser worker subprocess handshake failed safely") from None
            self._state = BrowserWorkerSubprocessState.HANDSHAKEN
            return response

    async def execute(self, request: BrowserWorkerExecuteRequest) -> BrowserWorkerExecutionResult:
        async with self._lock:
            self._require_state(BrowserWorkerSubprocessState.HANDSHAKEN)
            try:
                async with asyncio.timeout(self._timeout):
                    result = await self._exchange(
                        encode_worker_execute_request(request),
                        decode_worker_execution_result,
                        MAX_BROWSER_WORKER_WIRE_BYTES,
                    )
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise BrowserWorkerSubprocessError("browser worker subprocess execution failed safely") from None
            self._state = BrowserWorkerSubprocessState.EXECUTED
            return result

    async def close(self, request: BrowserWorkerTerminateRequest) -> BrowserWorkerTerminationReceipt:
        async with self._lock:
            self._require_state(BrowserWorkerSubprocessState.EXECUTED)
            self._state = BrowserWorkerSubprocessState.CLOSING
            return await self._terminate_exchange(request)

    async def terminate(self, request: BrowserWorkerTerminateRequest) -> BrowserWorkerTerminationReceipt:
        async with self._lock:
            if self._state not in {
                BrowserWorkerSubprocessState.HANDSHAKEN,
                BrowserWorkerSubprocessState.EXECUTED,
            }:
                raise BrowserWorkerSubprocessError("browser worker subprocess termination was not confirmed")
            self._state = BrowserWorkerSubprocessState.TERMINATING
            return await self._terminate_exchange(request)

    async def _terminate_exchange(
        self,
        request: BrowserWorkerTerminateRequest,
    ) -> BrowserWorkerTerminationReceipt:
        try:
            async with asyncio.timeout(self._timeout):
                receipt = await self._exchange(
                    encode_worker_terminate_request(request),
                    decode_worker_termination_receipt,
                    _MAX_CONTROL_FRAME_BYTES,
                )
                validate_worker_termination(request, receipt)
                await self._close_input()
                await self._require_clean_exit()
        except asyncio.CancelledError:
            await self._fail_closed()
            raise
        except Exception:
            await self._fail_closed()
            raise BrowserWorkerSubprocessError("browser worker subprocess termination was not confirmed") from None
        self._state = BrowserWorkerSubprocessState.CLOSED
        return receipt

    async def _spawn(self) -> None:
        if self._process is not None:
            raise BrowserWorkerSubprocessError("browser worker subprocess is one-shot")
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            cwd=self._cwd,
            env=self._env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if self._process.stdin is None or self._process.stdout is None:
            raise BrowserWorkerSubprocessError("browser worker subprocess pipes were unavailable")

    async def _exchange(
        self,
        payload: bytes,
        decoder: Callable[[bytes], _T],
        response_limit: int,
    ) -> _T:
        process = self._require_live_process()
        stdin = process.stdin
        stdout = process.stdout
        if stdin is None or stdout is None:
            raise BrowserWorkerSubprocessError("browser worker subprocess pipes were unavailable")
        if not payload or len(payload) > 0xFFFFFFFF:
            raise BrowserWorkerSubprocessError("browser worker subprocess request frame was invalid")
        stdin.write(struct.pack(">I", len(payload)))
        stdin.write(payload)
        await stdin.drain()
        header = await stdout.readexactly(_FRAME_HEADER_BYTES)
        frame_length = struct.unpack(">I", header)[0]
        if not 1 <= frame_length <= response_limit:
            raise BrowserWorkerSubprocessError("browser worker subprocess response frame was invalid")
        return decoder(await stdout.readexactly(frame_length))

    async def _close_input(self) -> None:
        process = self._require_process()
        stdin = process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
            await stdin.wait_closed()

    async def _require_clean_exit(self) -> None:
        process = self._require_process()
        stdout = process.stdout
        if stdout is None:
            raise BrowserWorkerSubprocessError("browser worker subprocess pipes were unavailable")
        if await stdout.read(1):
            raise BrowserWorkerSubprocessError("browser worker subprocess emitted trailing data")
        return_code = await process.wait()
        if return_code != 0:
            raise BrowserWorkerSubprocessError("browser worker subprocess did not exit cleanly")

    async def _fail_closed(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._kill_and_reap())
        while not self._cleanup_task.done():
            try:
                await asyncio.shield(self._cleanup_task)
            except asyncio.CancelledError:
                continue
        try:
            self._cleanup_task.result()
        except asyncio.CancelledError:
            self._state = BrowserWorkerSubprocessState.UNKNOWN
            raise BrowserWorkerSubprocessError("browser worker subprocess cleanup was not confirmed") from None

    async def _kill_and_reap(self) -> None:
        process = self._process
        self._state = BrowserWorkerSubprocessState.FAILED
        if process is None:
            return
        try:
            stdin = process.stdin
            if stdin is not None and not stdin.is_closing():
                stdin.close()
            if process.returncode is None:
                process.kill()
            async with asyncio.timeout(self._cleanup_timeout):
                await process.wait()
        except ProcessLookupError:
            try:
                async with asyncio.timeout(self._cleanup_timeout):
                    await process.wait()
            except Exception:
                self._state = BrowserWorkerSubprocessState.UNKNOWN
                raise BrowserWorkerSubprocessError("browser worker subprocess cleanup was not confirmed") from None
        except Exception:
            self._state = BrowserWorkerSubprocessState.UNKNOWN
            raise BrowserWorkerSubprocessError("browser worker subprocess cleanup was not confirmed") from None

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise BrowserWorkerSubprocessError("browser worker subprocess was not started")
        return self._process

    def _require_live_process(self) -> asyncio.subprocess.Process:
        process = self._require_process()
        if process.returncode is not None:
            raise BrowserWorkerSubprocessError("browser worker subprocess was not available")
        return process

    def _require_state(self, expected: BrowserWorkerSubprocessState) -> None:
        if self._state is not expected:
            raise BrowserWorkerSubprocessError("browser worker subprocess protocol order was rejected")


def _validated_argv(value: object) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or not 1 <= len(value) <= _MAX_ARGV_ITEMS
        or any(type(item) is not str or not item or len(item) > 4096 or "\0" in item for item in value)
    ):
        raise ValueError("argv must be a fixed non-empty tuple of bounded strings")
    executable = Path(value[0])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("argv executable must be an existing absolute file")
    return value


def _validated_cwd(value: object) -> str:
    if not isinstance(value, (str, Path)):
        raise TypeError("cwd must be an absolute directory path")
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    return str(path)


def _validated_env(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > _MAX_ENV_ITEMS:
        raise ValueError("env must be a bounded explicit mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if (
            type(key) is not str
            or type(item) is not str
            or not key
            or len(key) > 128
            or len(item) > 4096
            or "=" in key
            or "\0" in key
            or "\0" in item
        ):
            raise ValueError("env must contain only bounded string entries")
        result[key] = item
    return result


def _validated_timeout(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.01 <= float(value) <= 300.0:
        raise ValueError(f"{label} is outside the allowed range")
    return float(value)


__all__ = [
    "BrowserWorkerSubprocessError",
    "BrowserWorkerSubprocessState",
    "BrowserWorkerSubprocessTransport",
]
