from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from yonerai_discord.modules.voice.process import (
    VoicevoxOwnedProcessLifecycle,
    VoicevoxProcessError,
)


class FakeProcess:
    def __init__(self, *, ignore_terminate: bool = False) -> None:
        self.returncode: int | None = None
        self.ignore_terminate = ignore_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self.ignore_terminate:
            self.returncode = -15
            self._exited.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


class FakeProcessFactory:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.spawned = asyncio.Event()

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeProcess:
        self.calls.append((argv, kwargs))
        self.spawned.set()
        return self.process


class DelayedReturnProcessFactory(FakeProcessFactory):
    def __init__(self, process: FakeProcess) -> None:
        super().__init__(process)
        self.release = asyncio.Event()

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeProcess:
        self.calls.append((argv, kwargs))
        self.spawned.set()
        await self.release.wait()
        return self.process


def _run_executable(tmp_path: Path) -> Path:
    executable = tmp_path / "run.exe"
    executable.write_bytes(b"fixed-test-executable")
    return executable


def _lifecycle(
    tmp_path: Path,
    factory: FakeProcessFactory,
    *,
    managed: bool = True,
    endpoint: str = "http://127.0.0.1:50021",
    startup: float = 0.2,
    shutdown: float = 0.05,
    executable: Path | None = None,
) -> VoicevoxOwnedProcessLifecycle:
    return VoicevoxOwnedProcessLifecycle(
        managed_enabled=managed,
        endpoint=endpoint,
        executable=executable if executable is not None else _run_executable(tmp_path),
        startup_timeout_seconds=startup,
        shutdown_timeout_seconds=shutdown,
        process_factory=factory,
    )


@pytest.mark.asyncio
async def test_disabled_lifecycle_probes_but_never_spawns(tmp_path: Path) -> None:
    factory = FakeProcessFactory(FakeProcess())
    lifecycle = _lifecycle(tmp_path, factory, managed=False, executable=None)

    assert await lifecycle.ensure_ready(lambda _timeout: _ready(False)) is False
    await lifecycle.stop()

    assert factory.calls == []
    assert lifecycle.owns_process is False


@pytest.mark.asyncio
async def test_preexisting_engine_is_ready_but_never_owned_or_stopped(tmp_path: Path) -> None:
    process = FakeProcess()
    factory = FakeProcessFactory(process)
    lifecycle = _lifecycle(tmp_path, factory)

    assert await lifecycle.ensure_ready(lambda _timeout: _ready(True)) is True
    await lifecycle.stop()

    assert factory.calls == []
    assert process.terminate_calls == 0
    assert process.kill_calls == 0


@pytest.mark.asyncio
async def test_managed_engine_uses_fixed_argv_and_stops_exact_owned_handle(tmp_path: Path) -> None:
    executable = _run_executable(tmp_path)
    process = FakeProcess()
    factory = FakeProcessFactory(process)
    lifecycle = _lifecycle(tmp_path, factory, executable=executable)
    probe_calls = 0

    async def probe(_timeout: float) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return probe_calls >= 2

    assert await lifecycle.ensure_ready(probe) is True
    assert lifecycle.owns_process is True
    assert len(factory.calls) == 1
    argv, kwargs = factory.calls[0]
    assert argv == (str(executable.resolve()), "--host", "127.0.0.1", "--port", "50021")
    assert kwargs["cwd"] == str(executable.parent.resolve())
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
    assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
    assert set(kwargs["env"]) <= {"PATH", "LANG", "LC_ALL", "SystemRoot", "WINDIR"}

    await lifecycle.stop()

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert lifecycle.owns_process is False


@pytest.mark.asyncio
async def test_startup_timeout_terminates_then_kills_and_reaps_owned_process(tmp_path: Path) -> None:
    process = FakeProcess(ignore_terminate=True)
    factory = FakeProcessFactory(process)
    lifecycle = _lifecycle(tmp_path, factory, startup=0.05, shutdown=0.05)

    with pytest.raises(VoicevoxProcessError, match="voicevox_process_start_failed"):
        await lifecycle.ensure_ready(lambda _timeout: _ready(False))

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.returncode == -9
    assert lifecycle.owns_process is False


@pytest.mark.asyncio
async def test_startup_cancellation_still_reaps_only_owned_process(tmp_path: Path) -> None:
    process = FakeProcess()
    factory = FakeProcessFactory(process)
    lifecycle = _lifecycle(tmp_path, factory, startup=2.0)

    async def unavailable(_timeout: float) -> bool:
        return False

    task = asyncio.create_task(lifecycle.ensure_ready(unavailable))
    await asyncio.wait_for(factory.spawned.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert lifecycle.owns_process is False


@pytest.mark.asyncio
async def test_cancel_after_spawn_before_factory_return_drains_and_reaps_exact_handle(
    tmp_path: Path,
) -> None:
    process = FakeProcess()
    factory = DelayedReturnProcessFactory(process)
    lifecycle = _lifecycle(tmp_path, factory, startup=0.2)

    task = asyncio.create_task(lifecycle.ensure_ready(lambda _timeout: _ready(False)))
    await asyncio.wait_for(factory.spawned.wait(), timeout=1.0)
    task.cancel()
    factory.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert lifecycle.pending_spawn is False
    assert lifecycle.owned_process_present is False


@pytest.mark.asyncio
async def test_pending_spawn_cleanup_timeout_is_retained_and_stop_retries_exact_handle(
    tmp_path: Path,
) -> None:
    process = FakeProcess()
    factory = DelayedReturnProcessFactory(process)
    lifecycle = _lifecycle(tmp_path, factory, startup=0.05)

    task = asyncio.create_task(lifecycle.ensure_ready(lambda _timeout: _ready(False)))
    await asyncio.wait_for(factory.spawned.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(VoicevoxProcessError, match="voicevox_process_cleanup_unconfirmed"):
        await task
    assert lifecycle.pending_spawn is True
    assert process.terminate_calls == 0

    factory.release.set()
    await lifecycle.stop()

    assert lifecycle.pending_spawn is False
    assert lifecycle.owned_process_present is False
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


@pytest.mark.asyncio
async def test_factory_hang_is_bounded_and_pending_spawn_is_retried_by_stop(
    tmp_path: Path,
) -> None:
    process = FakeProcess()
    factory = DelayedReturnProcessFactory(process)
    lifecycle = _lifecycle(tmp_path, factory, startup=0.05)

    with pytest.raises(VoicevoxProcessError, match="voicevox_process_cleanup_unconfirmed"):
        await lifecycle.ensure_ready(lambda _timeout: _ready(False))

    assert lifecycle.pending_spawn is True
    assert process.terminate_calls == 0
    factory.release.set()

    await lifecycle.stop()

    assert lifecycle.pending_spawn is False
    assert lifecycle.owned_process_present is False
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    (
        "http://localhost:50021",
        "https://127.0.0.1:50021",
        "http://192.168.1.10:50021",
        "http://127.0.0.1:50021/path",
        "http://127.0.0.1:50021?token=value",
    ),
)
async def test_managed_engine_rejects_nonexact_loopback_endpoint(tmp_path: Path, endpoint: str) -> None:
    factory = FakeProcessFactory(FakeProcess())
    lifecycle = _lifecycle(tmp_path, factory, endpoint=endpoint)

    with pytest.raises(VoicevoxProcessError, match="configuration_invalid"):
        await lifecycle.ensure_ready(lambda _timeout: _ready(False))

    assert factory.calls == []


@pytest.mark.asyncio
async def test_managed_engine_rejects_missing_wrong_or_symlink_executable(tmp_path: Path) -> None:
    wrong = tmp_path / "other.exe"
    wrong.write_bytes(b"wrong")
    missing = tmp_path / "run.exe"
    candidates = [wrong, missing]
    symlink = tmp_path / "link" / "run.exe"
    try:
        symlink.parent.mkdir()
        symlink.symlink_to(wrong)
    except OSError:
        pass
    else:
        candidates.append(symlink)

    for candidate in candidates:
        factory = FakeProcessFactory(FakeProcess())
        lifecycle = _lifecycle(tmp_path, factory, executable=candidate)
        with pytest.raises(VoicevoxProcessError, match="configuration_invalid"):
            await lifecycle.ensure_ready(lambda _timeout: _ready(False))
        assert factory.calls == []


async def _ready(value: bool) -> bool:
    return value
