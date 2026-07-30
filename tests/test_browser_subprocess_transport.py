from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from yonerai_discord.browser_sandbox import (
    BROWSER_WORKER_PROTOCOL_REVISION,
    BROWSER_WORKER_SUPPORTED_ACTIONS,
    BrowserWorkerExecuteRequest,
    BrowserWorkerHandshakeRequest,
    BrowserWorkerSubprocessError,
    BrowserWorkerSubprocessState,
    BrowserWorkerSubprocessTransport,
    BrowserWorkerTerminalReason,
    BrowserWorkerTerminateRequest,
    Navigate,
)


_CHILD = r"""
import json
import os
import struct
import sys
import time

mode = sys.argv[1]

def read_frame():
    header = sys.stdin.buffer.read(4)
    if len(header) != 4:
        raise SystemExit(20)
    size = struct.unpack(">I", header)[0]
    body = sys.stdin.buffer.read(size)
    if len(body) != size:
        raise SystemExit(21)
    return json.loads(body)

def write_document(message_type, payload):
    body = json.dumps(
        {
            "message_type": message_type,
            "payload": payload,
            "protocol_revision": "yonerai.browser-worker.v2",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
    sys.stdout.buffer.flush()

if mode == "hang":
    time.sleep(60)
if mode == "eof":
    raise SystemExit(0)
if mode == "zero":
    sys.stdout.buffer.write(b"\x00\x00\x00\x00")
    sys.stdout.buffer.flush()
    time.sleep(60)
if mode == "oversize":
    sys.stdout.buffer.write(struct.pack(">I", 65537))
    sys.stdout.buffer.flush()
    time.sleep(60)
if mode == "malformed":
    body = b"{"
    sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
    sys.stdout.buffer.flush()
    time.sleep(60)

handshake = read_frame()
h = handshake["payload"]
handshake_type = "execution_result" if mode == "wrong_type" else "handshake_response"
context_ok = (
    mode != "verify_context"
    or (
        os.getcwd() == sys.argv[2]
        and os.environ.get("TRUSTED_MARKER") == "yes"
        and os.environ.get("SHOULD_NOT_INHERIT") is None
    )
)
write_document(
    handshake_type,
    {
        "clipboard_enabled": False,
        "context_reuse_enabled": False,
        "credential_import_enabled": False,
        "developer_protocol_enabled": False,
        "downloads_enabled": False,
        "ephemeral_profile": True,
        "external_worker_identity": "worker-context-ok" if context_ok else "worker-context-bad",
        "host_mount_enabled": False,
        "isolation_contract_digest": h["isolation_contract_digest"],
        "nonce": h["nonce"],
        "policy_snapshot_digest": h["policy_snapshot_digest"],
        "protocol_revision": h["protocol_revision"],
        "request_id": h["request_id"],
        "script_evaluation_enabled": False,
        "session_id": h["session_id"],
        "supported_actions": [
            "click",
            "extract_text",
            "navigate",
            "screenshot",
            "scroll",
            "select_option",
            "type_text",
            "wait",
        ],
        "uploads_enabled": False,
    },
)

execute = read_frame()
write_document(
    "execution_result",
    {
        "completed_actions": len(execute["payload"]["actions"]),
        "consumed_bytes": 0,
        "external_worker_identity": execute["payload"]["external_worker_identity"],
        "isolation_contract_digest": h["isolation_contract_digest"],
        "network_request_count": 0,
        "nonce": h["nonce"],
        "outputs": [],
        "policy_snapshot_digest": h["policy_snapshot_digest"],
        "redirect_count": 0,
        "request_id": h["request_id"],
        "session_id": h["session_id"],
    },
)

terminate = read_frame()
t = terminate["payload"]
if mode == "wrong_receipt":
    t["request_id"] = "wrong-request"
write_document(
    "termination_receipt",
    {
        **t,
        "profile_destroyed": True,
        "terminal_sequence": 1,
        "worker_terminated": True,
    },
)
if mode == "extra":
    sys.stdout.buffer.write(b"x")
    sys.stdout.buffer.flush()
if mode == "nonzero":
    raise SystemExit(7)
"""


def _handshake() -> BrowserWorkerHandshakeRequest:
    return BrowserWorkerHandshakeRequest(
        protocol_revision=BROWSER_WORKER_PROTOCOL_REVISION,
        request_id="request-1",
        session_id="session-1",
        nonce="a" * 24,
        isolation_contract_digest="b" * 64,
        policy_snapshot_digest="c" * 64,
        deadline_milliseconds=10_000,
        max_total_bytes=64 * 1024,
        max_actions=2,
        max_network_requests=4,
        max_redirects=1,
    )


def _execute(handshake: BrowserWorkerHandshakeRequest, identity: str) -> BrowserWorkerExecuteRequest:
    return BrowserWorkerExecuteRequest(
        handshake=handshake,
        external_worker_identity=identity,
        actions=(Navigate("https://example.com/"),),
    )


def _terminate(handshake: BrowserWorkerHandshakeRequest, identity: str) -> BrowserWorkerTerminateRequest:
    return BrowserWorkerTerminateRequest(
        protocol_revision=handshake.protocol_revision,
        request_id=handshake.request_id,
        session_id=handshake.session_id,
        nonce=handshake.nonce,
        isolation_contract_digest=handshake.isolation_contract_digest,
        policy_snapshot_digest=handshake.policy_snapshot_digest,
        external_worker_identity=identity,
        reason=BrowserWorkerTerminalReason.COMPLETED,
    )


def _transport(
    tmp_path: Path,
    mode: str,
    *,
    timeout: float = 2.0,
    cleanup_timeout: float = 2.0,
    env: dict[str, str] | None = None,
    extra_argv: tuple[str, ...] = (),
) -> BrowserWorkerSubprocessTransport:
    return BrowserWorkerSubprocessTransport(
        argv=(sys.executable, "-I", "-c", _CHILD, mode, *extra_argv),
        cwd=tmp_path,
        env={} if env is None else env,
        operation_timeout_seconds=timeout,
        cleanup_timeout_seconds=cleanup_timeout,
    )


async def _advance_to_close(
    transport: BrowserWorkerSubprocessTransport,
) -> tuple[BrowserWorkerHandshakeRequest, str]:
    handshake = _handshake()
    response = await transport.handshake(handshake)
    await transport.execute(_execute(handshake, response.external_worker_identity))
    return handshake, response.external_worker_identity


class _ControlledStdin:
    def __init__(self) -> None:
        self.close_calls = 0

    def is_closing(self) -> bool:
        return self.close_calls > 0

    def close(self) -> None:
        self.close_calls += 1


class _ControlledProcess:
    def __init__(self) -> None:
        self.stdin = _ControlledStdin()
        self.stdout = None
        self.returncode: int | None = None
        self.kill_calls = 0
        self.wait_calls = 0
        self.wait_started = asyncio.Event()
        self.release_wait = asyncio.Event()

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        self.wait_started.set()
        await self.release_wait.wait()
        self.returncode = -9
        return self.returncode


@pytest.mark.asyncio
async def test_success_uses_one_child_one_session_and_strict_order(tmp_path: Path) -> None:
    transport = _transport(tmp_path, "success")
    handshake, identity = await _advance_to_close(transport)
    receipt = await transport.close(_terminate(handshake, identity))

    assert receipt.worker_terminated is True
    assert receipt.profile_destroyed is True
    assert transport.state is BrowserWorkerSubprocessState.CLOSED
    with pytest.raises(BrowserWorkerSubprocessError, match="protocol order"):
        await transport.handshake(handshake)


@pytest.mark.asyncio
async def test_fixed_argv_explicit_env_and_dedicated_cwd_are_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOULD_NOT_INHERIT", "secret-parent-value")
    transport = _transport(
        tmp_path,
        "verify_context",
        env={"TRUSTED_MARKER": "yes"},
        extra_argv=(str(tmp_path),),
    )
    handshake = _handshake()
    response = await transport.handshake(handshake)
    assert response.external_worker_identity == "worker-context-ok"
    await transport.execute(_execute(handshake, response.external_worker_identity))
    await transport.close(_terminate(handshake, response.external_worker_identity))


@pytest.mark.parametrize("mode", ["zero", "oversize", "eof"])
@pytest.mark.asyncio
async def test_invalid_or_missing_control_frame_fails_closed_and_reaps(tmp_path: Path, mode: str) -> None:
    transport = _transport(tmp_path, mode)
    with pytest.raises(BrowserWorkerSubprocessError, match="failed safely"):
        await transport.handshake(_handshake())
    assert transport.state is BrowserWorkerSubprocessState.FAILED
    assert transport._process is not None and transport._process.returncode is not None


@pytest.mark.parametrize("mode", ["malformed", "wrong_type"])
@pytest.mark.asyncio
async def test_malformed_or_wrong_message_type_fails_closed_without_payload_leak(tmp_path: Path, mode: str) -> None:
    transport = _transport(tmp_path, mode)
    with pytest.raises(BrowserWorkerSubprocessError) as raised:
        await transport.handshake(_handshake())
    assert "example.com" not in str(raised.value)
    assert "aaaa" not in str(raised.value)
    assert transport.state is BrowserWorkerSubprocessState.FAILED


@pytest.mark.parametrize("mode", ["wrong_receipt", "extra", "nonzero"])
@pytest.mark.asyncio
async def test_unconfirmed_or_unclean_termination_never_returns_fake_success(tmp_path: Path, mode: str) -> None:
    transport = _transport(tmp_path, mode)
    handshake, identity = await _advance_to_close(transport)
    with pytest.raises(BrowserWorkerSubprocessError, match="not confirmed"):
        await transport.close(_terminate(handshake, identity))
    assert transport.state is BrowserWorkerSubprocessState.FAILED
    assert transport._process is not None and transport._process.returncode is not None


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_without_stderr_or_command_leak(tmp_path: Path) -> None:
    transport = _transport(tmp_path, "hang", timeout=0.05)
    with pytest.raises(BrowserWorkerSubprocessError) as raised:
        await transport.handshake(_handshake())
    assert "-c" not in str(raised.value)
    assert transport.state is BrowserWorkerSubprocessState.FAILED
    assert transport._process is not None and transport._process.returncode is not None


@pytest.mark.asyncio
async def test_cancellation_kills_and_reaps_then_preserves_cancel(tmp_path: Path) -> None:
    transport = _transport(tmp_path, "hang")
    task = asyncio.create_task(transport.handshake(_handshake()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.state is BrowserWorkerSubprocessState.FAILED
    assert transport._process is not None and transport._process.returncode is not None


@pytest.mark.asyncio
async def test_repeated_cancel_during_cleanup_waits_for_one_kill_and_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _transport(tmp_path, "success")
    process = _ControlledProcess()
    operation_started = asyncio.Event()
    operation_blocked = asyncio.Event()

    async def fake_spawn() -> None:
        transport._process = process  # type: ignore[assignment]

    async def blocked_exchange(*_args: object) -> object:
        operation_started.set()
        await operation_blocked.wait()
        raise AssertionError("blocked exchange unexpectedly resumed")

    monkeypatch.setattr(transport, "_spawn", fake_spawn)
    monkeypatch.setattr(transport, "_exchange", blocked_exchange)
    task = asyncio.create_task(transport.handshake(_handshake()))
    await operation_started.wait()
    task.cancel()
    await process.wait_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    process.release_wait.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.kill_calls == 1
    assert process.wait_calls == 1
    assert process.stdin.close_calls == 1
    assert transport.state is BrowserWorkerSubprocessState.FAILED


@pytest.mark.asyncio
async def test_cleanup_wait_hang_is_bounded_unknown_and_not_retried(tmp_path: Path) -> None:
    transport = _transport(tmp_path, "success", cleanup_timeout=0.02)
    process = _ControlledProcess()
    transport._process = process  # type: ignore[assignment]

    with pytest.raises(BrowserWorkerSubprocessError, match="cleanup was not confirmed"):
        await transport._fail_closed()
    with pytest.raises(BrowserWorkerSubprocessError, match="cleanup was not confirmed"):
        await transport._fail_closed()

    assert transport.state is BrowserWorkerSubprocessState.UNKNOWN
    assert process.kill_calls == 1
    assert process.wait_calls == 1
    assert process.stdin.close_calls == 1


def test_constructor_rejects_dynamic_or_unbounded_process_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fixed"):
        BrowserWorkerSubprocessTransport(argv=[sys.executable], cwd=tmp_path, env={})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="existing absolute file"):
        BrowserWorkerSubprocessTransport(argv=("python",), cwd=tmp_path, env={})
    with pytest.raises(ValueError, match="bounded explicit"):
        BrowserWorkerSubprocessTransport(argv=(sys.executable,), cwd=tmp_path, env={str(i): "" for i in range(33)})
    with pytest.raises(ValueError, match="existing absolute"):
        BrowserWorkerSubprocessTransport(argv=(sys.executable,), cwd=Path("relative"), env={})

    assert BROWSER_WORKER_SUPPORTED_ACTIONS == (
        "click",
        "extract_text",
        "navigate",
        "screenshot",
        "scroll",
        "select_option",
        "type_text",
        "wait",
    )
