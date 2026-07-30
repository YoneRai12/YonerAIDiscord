from __future__ import annotations

import asyncio
import ipaddress
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit


class VoicevoxProcessError(RuntimeError):
    """VOICEVOX process lifecycleの公開可能な固定error code。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VoicevoxProcessHandle(Protocol):
    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


ProcessFactory = Callable[..., Awaitable[VoicevoxProcessHandle]]
ReadinessProbe = Callable[[float], Awaitable[bool]]


class VoicevoxOwnedProcessLifecycle:
    """明示設定時だけVOICEVOXを起動し、生成したexact handleだけを回収する。"""

    def __init__(
        self,
        *,
        managed_enabled: bool,
        endpoint: str,
        executable: Path | None,
        startup_timeout_seconds: float,
        shutdown_timeout_seconds: float,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
    ) -> None:
        if type(managed_enabled) is not bool:
            raise VoicevoxProcessError("voicevox_process_configuration_invalid")
        if not _timeout(startup_timeout_seconds) or not _timeout(shutdown_timeout_seconds):
            raise VoicevoxProcessError("voicevox_process_configuration_invalid")
        if not callable(process_factory):
            raise VoicevoxProcessError("voicevox_process_configuration_invalid")
        self._managed_enabled = managed_enabled
        self._endpoint = endpoint
        self._executable = executable
        self._startup_timeout = float(startup_timeout_seconds)
        self._shutdown_timeout = float(shutdown_timeout_seconds)
        self._process_factory = process_factory
        self._owned_process: VoicevoxProcessHandle | None = None
        self._pending_spawn: asyncio.Task[VoicevoxProcessHandle] | None = None
        self._lock = asyncio.Lock()

    @property
    def owns_process(self) -> bool:
        process = self._owned_process
        return process is not None and process.returncode is None

    @property
    def owned_process_present(self) -> bool:
        return self._owned_process is not None

    @property
    def pending_spawn(self) -> bool:
        return self._pending_spawn is not None

    async def ensure_ready(self, probe: ReadinessProbe) -> bool:
        if not callable(probe):
            raise VoicevoxProcessError("voicevox_process_configuration_invalid")
        async with self._lock:
            if await _probe(probe, min(self._startup_timeout, 1.0)):
                return True
            if not self._managed_enabled:
                return False

            host, port = _managed_endpoint(self._endpoint)
            executable = _managed_executable(self._executable)
            if self._owned_process is not None or self._pending_spawn is not None:
                raise VoicevoxProcessError("voicevox_process_state_invalid")
            spawn_task = asyncio.create_task(
                self._process_factory(
                    str(executable),
                    "--host",
                    host,
                    "--port",
                    str(port),
                    cwd=str(executable.parent),
                    env=_minimal_environment(executable),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            )
            self._pending_spawn = spawn_task
            try:
                process = await asyncio.wait_for(
                    asyncio.shield(spawn_task),
                    timeout=self._startup_timeout,
                )
            except asyncio.CancelledError:
                process = await self._drain_pending_spawn()
                await self._cleanup_after_cancel(process)
                raise
            except TimeoutError:
                raise VoicevoxProcessError("voicevox_process_cleanup_unconfirmed") from None
            except Exception:
                self._pending_spawn = None
                raise VoicevoxProcessError("voicevox_process_start_failed") from None
            self._pending_spawn = None
            self._owned_process = process
            if _cancellation_requested():
                await self._cleanup_after_cancel(process)
                raise asyncio.CancelledError

            try:
                async with asyncio.timeout(self._startup_timeout):
                    while process.returncode is None:
                        ready = await _probe(probe, min(1.0, self._startup_timeout))
                        if _cancellation_requested():
                            raise asyncio.CancelledError
                        if ready:
                            return True
                        await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                await self._cleanup_after_cancel(process)
                raise
            except TimeoutError:
                pass
            except Exception:
                pass

            await self._cleanup_owned(process)
            raise VoicevoxProcessError("voicevox_process_start_failed")

    async def stop(self) -> None:
        async with self._lock:
            if self._pending_spawn is not None:
                process = await self._drain_pending_spawn()
                self._owned_process = process
            process = self._owned_process
            if process is None:
                return
            try:
                await self._cleanup_owned(process)
            except asyncio.CancelledError:
                await self._cleanup_after_cancel(process)
                raise

    async def _drain_pending_spawn(self) -> VoicevoxProcessHandle:
        task = self._pending_spawn
        if task is None:
            raise VoicevoxProcessError("voicevox_process_state_invalid")
        try:
            process = await asyncio.wait_for(asyncio.shield(task), timeout=self._startup_timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise VoicevoxProcessError("voicevox_process_cleanup_unconfirmed") from None
        except Exception:
            self._pending_spawn = None
            raise VoicevoxProcessError("voicevox_process_start_failed") from None
        self._pending_spawn = None
        self._owned_process = process
        return process

    async def _cleanup_after_cancel(self, process: VoicevoxProcessHandle) -> None:
        cleanup = asyncio.create_task(self._cleanup_owned(process))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
        except Exception:
            pass

    async def _cleanup_owned(self, process: VoicevoxProcessHandle) -> None:
        if self._owned_process is not process:
            raise VoicevoxProcessError("voicevox_process_ownership_changed")
        cleanup_confirmed = await _terminate_and_reap(process, timeout_seconds=self._shutdown_timeout)
        if cleanup_confirmed:
            self._owned_process = None
            return
        raise VoicevoxProcessError("voicevox_process_cleanup_unconfirmed")


async def _terminate_and_reap(process: VoicevoxProcessHandle, *, timeout_seconds: float) -> bool:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        except Exception:
            return False
    if await _wait_reaped(process, timeout_seconds=timeout_seconds):
        return True
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except Exception:
            return False
    return await _wait_reaped(process, timeout_seconds=timeout_seconds)


async def _wait_reaped(process: VoicevoxProcessHandle, *, timeout_seconds: float) -> bool:
    wait_task = asyncio.create_task(process.wait())
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=timeout_seconds)
    except asyncio.CancelledError:
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)
        raise
    except Exception:
        wait_task.cancel()
        await asyncio.gather(wait_task, return_exceptions=True)
        return False
    return process.returncode is not None


async def _probe(probe: ReadinessProbe, timeout_seconds: float) -> bool:
    try:
        return await asyncio.wait_for(probe(timeout_seconds), timeout=timeout_seconds) is True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


def _managed_endpoint(value: str) -> tuple[str, int]:
    if not isinstance(value, str) or not value:
        raise VoicevoxProcessError("voicevox_process_configuration_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        address = ipaddress.ip_address(parsed.hostname or "")
    except (ValueError, TypeError):
        raise VoicevoxProcessError("voicevox_process_configuration_invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not address.is_loopback
        or port is None
        or not 1 <= port <= 65_535
    ):
        raise VoicevoxProcessError("voicevox_process_configuration_invalid")
    return str(address), port


def _managed_executable(value: Path | None) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or value.name.casefold() != "run.exe":
        raise VoicevoxProcessError("voicevox_process_configuration_invalid")
    try:
        if value.is_symlink() or not value.is_file():
            raise VoicevoxProcessError("voicevox_process_configuration_invalid")
        return value.resolve(strict=True)
    except VoicevoxProcessError:
        raise
    except OSError:
        raise VoicevoxProcessError("voicevox_process_configuration_invalid") from None


def _minimal_environment(executable: Path) -> dict[str, str]:
    environment = {"PATH": str(executable.parent), "LANG": "C", "LC_ALL": "C"}
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        environment["SystemRoot"] = system_root
        environment["WINDIR"] = system_root
    return environment


def _timeout(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and 0.05 <= float(value) <= 120.0


def _cancellation_requested() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


__all__ = [
    "ProcessFactory",
    "ReadinessProbe",
    "VoicevoxOwnedProcessLifecycle",
    "VoicevoxProcessError",
    "VoicevoxProcessHandle",
]
