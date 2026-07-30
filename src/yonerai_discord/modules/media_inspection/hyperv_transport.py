from __future__ import annotations

import asyncio
import math
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import MediaInspectionUnavailableError
from .hyperv_contract import (
    HYPERV_MEDIA_REMOTE_ADDRESS,
    HYPERV_MEDIA_REMOTE_USER,
    MAX_HYPERV_MEDIA_FRAME_BYTES,
    HyperVMediaExecutionResult,
    HyperVMediaProbeResult,
    decode_execution_result,
    decode_probe_result,
    encode_inspect_request,
    encode_probe_request,
)


HYPERV_SSH_EXECUTABLE = r"C:\Windows\System32\OpenSSH\ssh.exe"
HYPERV_RUNTIME_DIRECTORY = Path(".runtime") / "hyperv-media-inspection"
HYPERV_IDENTITY_FILENAME = "id_ed25519"
HYPERV_KNOWN_HOSTS_FILENAME = "known_hosts"
_PROCESS_CLEANUP_SECONDS = 3.0
_MAX_IDENTITY_BYTES = 32 * 1024
_MAX_KNOWN_HOSTS_BYTES = 256 * 1024

ProcessFactory = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class HyperVRuntimePaths:
    project_root: Path
    runtime_root: Path
    identity_file: Path
    known_hosts_file: Path

    @classmethod
    def from_project_root(cls, value: str | Path) -> HyperVRuntimePaths:
        if not isinstance(value, (str, Path)):
            raise TypeError("project_root must be a path")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise ValueError("project_root must be absolute")
        try:
            project_root = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError("project_root is unavailable") from exc
        if not project_root.is_dir():
            raise ValueError("project_root must be a directory")
        runtime_root = project_root / HYPERV_RUNTIME_DIRECTORY
        return cls(
            project_root=project_root,
            runtime_root=runtime_root,
            identity_file=runtime_root / HYPERV_IDENTITY_FILENAME,
            known_hosts_file=runtime_root / HYPERV_KNOWN_HOSTS_FILENAME,
        )

    def validate_files(self) -> None:
        _validate_runtime_file(
            self.identity_file,
            parent=self.runtime_root,
            maximum=_MAX_IDENTITY_BYTES,
        )
        _validate_runtime_file(
            self.known_hosts_file,
            parent=self.runtime_root,
            maximum=_MAX_KNOWN_HOSTS_BYTES,
        )


class HyperVMediaInspectionTransport:
    """Fixed OpenSSH transport to a Hyper-V forced-command worker."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        timeout_seconds: float,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1.0 <= float(timeout_seconds) <= 300.0
        ):
            raise ValueError("timeout_seconds is outside the Hyper-V contract")
        if not callable(process_factory):
            raise TypeError("process_factory must be callable")
        self._runtime_paths = HyperVRuntimePaths.from_project_root(project_root)
        self._timeout = float(timeout_seconds)
        self._process_factory = process_factory
        self._active: set[Any] = set()
        self._closing = False
        self._lock = asyncio.Lock()

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def runtime_paths(self) -> HyperVRuntimePaths:
        return self._runtime_paths

    async def probe(self) -> HyperVMediaProbeResult:
        raw = await self._exchange(encode_probe_request())
        return decode_probe_result(raw)

    async def inspect(self, *, url: str, instruction: str) -> HyperVMediaExecutionResult:
        raw = await self._exchange(
            encode_inspect_request(url=url, instruction=instruction),
        )
        return decode_execution_result(raw)

    async def close(self) -> None:
        self._closing = True
        async with self._lock:
            active = tuple(self._active)
        cleanup_error: MediaInspectionUnavailableError | None = None
        for process in active:
            try:
                await self._kill_and_reap(process)
            except MediaInspectionUnavailableError as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error

    async def _exchange(self, payload: bytes) -> bytes:
        if self._closing:
            raise MediaInspectionUnavailableError("Hyper-V transport is closing")
        try:
            self._runtime_paths.validate_files()
        except (OSError, ValueError) as exc:
            raise MediaInspectionUnavailableError("Hyper-V runtime credentials are unavailable") from exc

        process: Any | None = None
        try:
            process = await self._process_factory(
                *_ssh_argv(self._runtime_paths, self._timeout),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with self._lock:
                if self._closing:
                    await self._kill_and_reap(process)
                    raise MediaInspectionUnavailableError("Hyper-V transport is closing")
                self._active.add(process)
            async with asyncio.timeout(self._timeout):
                return await self._exchange_frame(process, payload)
        except asyncio.CancelledError:
            if process is not None:
                await asyncio.shield(self._kill_and_reap(process))
            raise
        except Exception as exc:
            if process is not None:
                await self._kill_and_reap(process)
            if isinstance(exc, MediaInspectionUnavailableError):
                raise
            raise MediaInspectionUnavailableError("Hyper-V worker failed safely") from exc
        finally:
            if process is not None:
                async with self._lock:
                    self._active.discard(process)

    @staticmethod
    async def _exchange_frame(process: Any, payload: bytes) -> bytes:
        stdin = getattr(process, "stdin", None)
        stdout = getattr(process, "stdout", None)
        if stdin is None or stdout is None:
            raise MediaInspectionUnavailableError("Hyper-V worker pipes are unavailable")
        stdin.write(payload)
        await stdin.drain()
        stdin.close()
        wait_closed = getattr(stdin, "wait_closed", None)
        if callable(wait_closed):
            await wait_closed()

        header = await stdout.readexactly(4)
        length = struct.unpack(">I", header)[0]
        if not 1 <= length <= MAX_HYPERV_MEDIA_FRAME_BYTES:
            raise MediaInspectionUnavailableError("Hyper-V worker output is outside the bound")
        raw = await stdout.readexactly(length)
        if await stdout.read(1):
            raise MediaInspectionUnavailableError("Hyper-V worker emitted trailing output")
        return_code = await process.wait()
        if return_code != 0:
            raise MediaInspectionUnavailableError("Hyper-V worker exited unsuccessfully")
        return raw

    @staticmethod
    async def _kill_and_reap(process: Any) -> None:
        try:
            if getattr(process, "returncode", None) is None:
                process.kill()
        except ProcessLookupError:
            pass
        try:
            async with asyncio.timeout(_PROCESS_CLEANUP_SECONDS):
                await process.wait()
        except Exception as exc:
            raise MediaInspectionUnavailableError("Hyper-V SSH process could not be reaped") from exc


def _ssh_argv(paths: HyperVRuntimePaths, timeout_seconds: float) -> tuple[str, ...]:
    connect_timeout = min(30, max(1, math.ceil(timeout_seconds)))
    return (
        HYPERV_SSH_EXECUTABLE,
        "-F",
        "NUL",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={paths.known_hosts_file}",
        "-o",
        "GlobalKnownHostsFile=NUL",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "RequestTTY=no",
        "-o",
        "EscapeChar=none",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "VerifyHostKeyDNS=no",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-i",
        str(paths.identity_file),
        "-l",
        HYPERV_MEDIA_REMOTE_USER,
        HYPERV_MEDIA_REMOTE_ADDRESS,
    )


def _validate_runtime_file(path: Path, *, parent: Path, maximum: int) -> None:
    if not path.is_absolute() or path.is_symlink() or parent.is_symlink():
        raise ValueError("Hyper-V runtime path is unsafe")
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        stat = resolved_path.stat()
    except OSError as exc:
        raise ValueError("Hyper-V runtime file is unavailable") from exc
    if resolved_parent != parent or resolved_path != path or resolved_path.parent != resolved_parent:
        raise ValueError("Hyper-V runtime path escaped the project root")
    if not resolved_path.is_file() or not 1 <= stat.st_size <= maximum:
        raise ValueError("Hyper-V runtime file is invalid")


__all__ = [
    "HYPERV_IDENTITY_FILENAME",
    "HYPERV_KNOWN_HOSTS_FILENAME",
    "HYPERV_RUNTIME_DIRECTORY",
    "HYPERV_SSH_EXECUTABLE",
    "HyperVMediaInspectionTransport",
    "HyperVRuntimePaths",
]
