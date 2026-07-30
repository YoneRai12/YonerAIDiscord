from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from yonerai_discord.modules.media_inspection.domain import MediaInspectionUnavailableError
from yonerai_discord.modules.media_inspection.hyperv_contract import (
    HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
    HYPERV_MEDIA_IDENTITY_DIGEST,
    HYPERV_MEDIA_POLICY_REVISION,
    HYPERV_MEDIA_REMOTE_ADDRESS,
    HYPERV_MEDIA_REMOTE_USER,
    HYPERV_MEDIA_SCHEMA,
    HYPERV_MEDIA_WORKER_VERSION,
    frame_payload,
)
from yonerai_discord.modules.media_inspection.hyperv_transport import (
    HYPERV_SSH_EXECUTABLE,
    HyperVMediaInspectionTransport,
    HyperVRuntimePaths,
    _ssh_argv,
)


class _Input:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        self.data.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _Output:
    def __init__(self, data: bytes, *, block: bool = False) -> None:
        self.data = bytearray(data)
        self.block = block

    async def readexactly(self, size: int) -> bytes:
        if self.block:
            await asyncio.Event().wait()
        if len(self.data) < size:
            raise asyncio.IncompleteReadError(bytes(self.data), size)
        value = bytes(self.data[:size])
        del self.data[:size]
        return value

    async def read(self, size: int) -> bytes:
        value = bytes(self.data[:size])
        del self.data[:size]
        return value


class _Process:
    def __init__(self, output: bytes = b"", *, block: bool = False) -> None:
        self.stdin = _Input()
        self.stdout = _Output(output, block=block)
        self.returncode: int | None = None
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _runtime_root(tmp_path: Path) -> Path:
    runtime = tmp_path / ".runtime" / "hyperv-media-inspection"
    runtime.mkdir(parents=True)
    (runtime / "id_ed25519").write_text("fixed-private-key-placeholder", encoding="utf-8")
    (runtime / "known_hosts").write_text(
        "172.30.240.2 ssh-ed25519 fixed-host-key-placeholder\n",
        encoding="utf-8",
    )
    return runtime


def _execution_frame() -> bytes:
    return frame_payload(
        json.dumps(
            {
                "schema": HYPERV_MEDIA_SCHEMA,
                "worker_version": HYPERV_MEDIA_WORKER_VERSION,
                "policy_revision": HYPERV_MEDIA_POLICY_REVISION,
                "identity_digest": HYPERV_MEDIA_IDENTITY_DIGEST,
                "effective_policy_revision": HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
                "effective_policy_digest": HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
                "cleanup_confirmed": True,
                "status": "completed",
                "text": "解析結果",
            }
        ).encode()
    )


def test_ssh_argv_is_fixed_and_paths_are_derived_from_project_root(tmp_path: Path) -> None:
    runtime = _runtime_root(tmp_path)
    paths = HyperVRuntimePaths.from_project_root(tmp_path)

    argv = _ssh_argv(paths, 30)

    assert argv == (
        HYPERV_SSH_EXECUTABLE,
        "-F",
        "NUL",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={runtime / 'known_hosts'}",
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
        "ConnectTimeout=30",
        "-i",
        str(runtime / "id_ed25519"),
        "-l",
        HYPERV_MEDIA_REMOTE_USER,
        HYPERV_MEDIA_REMOTE_ADDRESS,
    )


@pytest.mark.asyncio
async def test_transport_sends_operation_url_and_instruction_only_in_framed_stdin(
    tmp_path: Path,
) -> None:
    _runtime_root(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    worker = _Process(_execution_frame())

    async def factory(*argv: str, **kwargs: Any) -> _Process:
        calls.append((argv, kwargs))
        return worker

    transport = HyperVMediaInspectionTransport(
        project_root=tmp_path,
        timeout_seconds=30,
        process_factory=factory,
    )
    url = "https://www.youtube.com/watch?v=ABCDEFGHIJK"
    instruction = "内容を説明して"

    result = await transport.inspect(url=url, instruction=instruction)

    assert result.text == "解析結果"
    argv = calls[0][0]
    assert url not in argv and instruction not in argv
    assert b'"operation":"inspect"' in worker.stdin.data
    assert url.encode() in worker.stdin.data
    assert instruction.encode() in worker.stdin.data


@pytest.mark.asyncio
async def test_timeout_and_cancellation_kill_and_reap_ssh_without_stopping_vm(
    tmp_path: Path,
) -> None:
    _runtime_root(tmp_path)
    timeout_process = _Process(block=True)
    cancelled_process = _Process(block=True)
    pending_processes = [timeout_process, cancelled_process]
    calls: list[tuple[str, ...]] = []

    async def factory(*argv: str, **_kwargs: Any) -> _Process:
        calls.append(argv)
        return pending_processes.pop(0)

    timeout_transport = HyperVMediaInspectionTransport(
        project_root=tmp_path,
        timeout_seconds=1,
        process_factory=factory,
    )
    with pytest.raises(MediaInspectionUnavailableError):
        await timeout_transport.probe()

    cancel_transport = HyperVMediaInspectionTransport(
        project_root=tmp_path,
        timeout_seconds=30,
        process_factory=factory,
    )
    task = asyncio.create_task(cancel_transport.probe())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await timeout_transport.close()
    await cancel_transport.close()

    assert timeout_process.killed and timeout_process.waited
    assert cancelled_process.killed and cancelled_process.waited
    assert len(calls) == 2
    assert all(argv[0] == HYPERV_SSH_EXECUTABLE for argv in calls)
    assert not any(any("stop-vm" in value.casefold() for value in argv) for argv in calls)


@pytest.mark.asyncio
async def test_missing_code_owned_runtime_files_fail_before_process_spawn(tmp_path: Path) -> None:
    calls = 0

    async def factory(*_argv: str, **_kwargs: Any) -> _Process:
        nonlocal calls
        calls += 1
        return _Process()

    transport = HyperVMediaInspectionTransport(
        project_root=tmp_path,
        timeout_seconds=30,
        process_factory=factory,
    )

    with pytest.raises(MediaInspectionUnavailableError):
        await transport.probe()
    assert calls == 0
