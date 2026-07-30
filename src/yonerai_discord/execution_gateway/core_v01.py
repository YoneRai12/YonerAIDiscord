from __future__ import annotations

import asyncio
import hashlib
import json
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace

from .core_contract import (
    CoreCancelDispositionV01,
    CoreCancelOutcomeV01,
    CoreMessageRequestV01,
    CoreRunReferenceV01,
    CoreToolResultV01,
    YonerAIInternalRunPortV01,
    project_core_message_v01,
)
from .core_files import (
    CoreArtifactOwnerScopeV01,
    CoreFilesContractError,
    artifact_reference_from_core_v01,
    core_artifact_ref_from_mapping_v01,
    core_ref_from_artifact_v01,
)
from .local import (
    IdempotencyConflictError,
    LocalExecutionContext,
    LocalExecutionGateway,
    RunTerminalError,
    UnknownRunError,
)
from .models import CapabilityResult, RunEvent, RunInput, RunReference
from yonerai_discord.secret_detection import contains_secret_like


_STATUS_EVENTS = frozenset({"meta", "progress", "trace", "reasoning_summary", "tool_result_submit"})


class CoreV01AdapterError(RuntimeError):
    """v0.1 portまたはevent/result DTOが契約を満たさない。"""


class CoreV01AuthorizationError(CoreV01AdapterError):
    """remote sink直前のlocal認可が拒否した。"""


@dataclass(slots=True)
class _RunBinding:
    local: LocalExecutionGateway
    request_fingerprint: str
    owner_scope: CoreArtifactOwnerScopeV01
    preferred_model: str | None
    remote_run_id: str | None = None
    remote_ready: asyncio.Event = field(default_factory=asyncio.Event)
    result_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tool_calls: dict[str, tuple[str, str]] = field(default_factory=dict)
    submitted_results: dict[str, str] = field(default_factory=dict)
    terminal: bool = False
    cancel_outcome: CoreCancelOutcomeV01 | None = None
    fresh_authorization_check: Callable[[], bool | Awaitable[bool]] | None = field(
        default=None,
        repr=False,
    )
    capability_authorization_check: Callable[[str], bool] | None = field(
        default=None,
        repr=False,
    )


class YonerAIInternalRunGatewayV01:
    """neutral ExecutionGatewayをv0.1 Internal Run API portへ適合する。

    LocalExecutionGatewayはevent replay、terminal-once、run内result eventを担い、
    userごとにinstanceを分けることでidempotency namespaceをuser scopeへ閉じる。
    """

    def __init__(
        self,
        port: YonerAIInternalRunPortV01,
        *,
        max_runs_per_user: int = 4_096,
        max_users: int = 4_096,
    ) -> None:
        for method in ("start", "events", "submit_result"):
            if not callable(getattr(port, method, None)):
                raise TypeError(f"port must expose a callable {method} method")
        if isinstance(max_users, bool) or not isinstance(max_users, int) or max_users <= 0:
            raise ValueError("max_users must be a positive integer")
        if isinstance(max_runs_per_user, bool) or not isinstance(max_runs_per_user, int) or max_runs_per_user <= 0:
            raise ValueError("max_runs_per_user must be a positive integer")
        self._port = port
        self._max_runs_per_user = max_runs_per_user
        self._max_users = max_users
        self._lock = asyncio.Lock()
        self._locals: dict[str, LocalExecutionGateway] = {}
        self._runs: dict[str, _RunBinding] = {}

    def __repr__(self) -> str:
        return "YonerAIInternalRunGatewayV01()"

    async def start(self, request: RunInput) -> RunReference:
        if not isinstance(request, RunInput):
            raise TypeError("request must be a RunInput")
        _require_sync_authorization(request.authorization_check)
        remote_request = replace(
            request,
            local_payload=None,
            authorization_check=None,
            fresh_authorization_check=None,
            capability_authorization_check=None,
        )
        projected = project_core_message_v01(remote_request)
        user_scope = _identity_scope(projected)
        request_fingerprint = _fingerprint_request(projected)
        owner_scope = _owner_scope(projected)
        async with self._lock:
            local = self._locals.get(user_scope)
            if local is None:
                if len(self._locals) >= self._max_users:
                    raise CoreV01AdapterError("Core v0.1 user scope capacity is exhausted")

                async def execute(
                    run_request: RunInput,
                    context: LocalExecutionContext,
                    *,
                    expected_user_scope: str = user_scope,
                ) -> None:
                    await self._execute(expected_user_scope, run_request, context)

                local = LocalExecutionGateway(
                    execute,
                    with_context=True,
                    max_runs=self._max_runs_per_user,
                )
                self._locals[user_scope] = local
            reference = await local.start(request)
            if not reference.reused:
                self._prune_terminal_bindings(local)
            binding = self._runs.get(reference.run_id)
            if binding is None:
                self._runs[reference.run_id] = _RunBinding(
                    local=local,
                    request_fingerprint=request_fingerprint,
                    owner_scope=owner_scope,
                    preferred_model=projected.preferred_model,
                    fresh_authorization_check=request.fresh_authorization_check,
                    capability_authorization_check=request.capability_authorization_check,
                )
            elif binding.local is not local or binding.request_fingerprint != request_fingerprint:
                raise IdempotencyConflictError("local run reference collided across Core identity scopes")
            elif not reference.reused:
                binding.fresh_authorization_check = request.fresh_authorization_check
                binding.capability_authorization_check = request.capability_authorization_check
            return reference

    def events(self, run_id: str) -> AsyncIterator[RunEvent]:
        binding = self._binding_now(run_id)
        return binding.local.events(run_id)

    async def submit_result(self, run_id: str, result: CapabilityResult) -> None:
        if not isinstance(result, CapabilityResult):
            raise TypeError("result must be a CapabilityResult")
        binding = self._binding_now(run_id)
        await binding.remote_ready.wait()
        if binding.remote_run_id is None:
            raise CoreV01AdapterError("Core run did not start")
        wire = _project_tool_result(result, binding.owner_scope)
        fingerprint = _fingerprint_result(wire)
        async with binding.result_lock:
            if binding.terminal:
                raise RunTerminalError(f"run {run_id} is already terminal")
            expected_call = binding.tool_calls.get(result.result_id)
            if expected_call is None:
                raise CoreV01AdapterError("tool_call_id was not requested by this Core run")
            if expected_call[0] != result.capability:
                raise CoreV01AdapterError("tool result does not match the requested tool")
            previous = binding.submitted_results.get(result.result_id)
            if previous is not None:
                if previous != fingerprint:
                    raise IdempotencyConflictError("tool_call_id was already submitted with different content")
                return
            capability_check = binding.capability_authorization_check
            _require_capability_authorization(capability_check, result.capability)
            await _require_fresh_authorization(binding)
            await self._port.submit_result(binding.remote_run_id, wire)
            binding.submitted_results[result.result_id] = fingerprint
            await binding.local.submit_result(run_id, result)

    async def cancel(self, run_id: str) -> CoreCancelOutcomeV01:
        binding = self._binding_now(run_id)
        async with binding.result_lock:
            if binding.cancel_outcome is not None:
                return binding.cancel_outcome
            if binding.terminal:
                outcome = CoreCancelOutcomeV01.already_terminal()
                binding.cancel_outcome = outcome
                return outcome
            binding.terminal = True
            _release_authorization_callbacks(binding)
            remote_outcome = CoreCancelOutcomeV01.unsupported()
            cancel_remote = getattr(self._port, "cancel", None)
            if binding.remote_run_id is not None and callable(cancel_remote):
                try:
                    candidate = await cancel_remote(binding.remote_run_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    candidate = CoreCancelOutcomeV01.unsupported()
                if isinstance(candidate, CoreCancelOutcomeV01):
                    remote_outcome = candidate
            await binding.local.cancel(run_id)
            if remote_outcome.disposition is CoreCancelDispositionV01.CONFIRMED:
                outcome = remote_outcome
            else:
                outcome = CoreCancelOutcomeV01.local_detached_outcome()
            binding.cancel_outcome = outcome
            return outcome

    async def _execute(
        self,
        user_scope: str,
        request: RunInput,
        context: LocalExecutionContext,
    ) -> None:
        remote_request = replace(
            request,
            local_payload=None,
            authorization_check=None,
            fresh_authorization_check=None,
            capability_authorization_check=None,
        )
        projected = project_core_message_v01(remote_request)
        if _identity_scope(projected) != user_scope:
            raise CoreV01AdapterError("Core run identity scope changed before execution")
        binding = await self._ensure_binding(
            context.run_id,
            projected,
            user_scope,
            fresh_authorization_check=request.fresh_authorization_check,
            capability_authorization_check=request.capability_authorization_check,
        )
        try:
            await _require_fresh_authorization(binding)
            remote = await self._port.start(projected)
            if not isinstance(remote, CoreRunReferenceV01):
                raise CoreV01AdapterError("Core start returned an invalid run reference")
            binding.remote_run_id = remote.run_id
            binding.remote_ready.set()
            stream = self._port.events(remote.run_id)
            if not hasattr(stream, "__aiter__"):
                raise CoreV01AdapterError("Core v0.1 events must return an async iterator")
            async for raw in stream:
                event = normalize_core_event_v01(raw)
                if event is None:
                    continue
                if event.kind == "final":
                    try:
                        for artifact in event.payload.get("artifacts", ()):
                            core_ref_from_artifact_v01(
                                artifact,
                                owner_scope=binding.owner_scope,
                            )
                    except CoreFilesContractError:
                        raise CoreV01AdapterError("Core v0.1 final artifact does not belong to this run") from None
                    await _require_fresh_authorization(binding)
                    event = replace(
                        event,
                        payload={
                            **event.payload,
                            "model": binding.preferred_model or "core-selected",
                            "provider": "yonerai-internal-run-v0.1",
                        },
                    )
                if event.kind == "action_required":
                    tool_call_id = event.payload["tool_call_id"]
                    tool = event.payload["tool"]
                    call_fingerprint = _fingerprint_tool_call(event)
                    capability_check = binding.capability_authorization_check
                    async with binding.result_lock:
                        if binding.terminal:
                            return
                        existing = binding.tool_calls.get(tool_call_id)
                        if existing is not None:
                            if existing != (tool, call_fingerprint):
                                raise CoreV01AdapterError("tool_call_id was reused with different content")
                            continue
                    _require_capability_authorization(capability_check, tool)
                    await _require_fresh_authorization(binding)
                    async with binding.result_lock:
                        if binding.terminal:
                            return
                        _require_capability_authorization(
                            binding.capability_authorization_check,
                            tool,
                        )
                        existing = binding.tool_calls.get(tool_call_id)
                        if existing is not None:
                            if existing != (tool, call_fingerprint):
                                raise CoreV01AdapterError("tool_call_id was reused with different content")
                            continue
                        binding.tool_calls[tool_call_id] = (tool, call_fingerprint)
                        await context.emit(event)
                    continue
                if event.terminal:
                    async with binding.result_lock:
                        if binding.terminal:
                            return
                        binding.terminal = True
                        _release_authorization_callbacks(binding)
                await context.emit(event)
                if event.terminal:
                    return
            raise CoreV01AdapterError("Core v0.1 event stream ended without a terminal event")
        finally:
            async with binding.result_lock:
                if not binding.terminal:
                    binding.terminal = True
                _release_authorization_callbacks(binding)
            binding.remote_ready.set()

    async def _ensure_binding(
        self,
        run_id: str,
        projected: CoreMessageRequestV01,
        user_scope: str,
        *,
        fresh_authorization_check: Callable[[], bool | Awaitable[bool]] | None,
        capability_authorization_check: Callable[[str], bool] | None,
    ) -> _RunBinding:
        request_fingerprint = _fingerprint_request(projected)
        async with self._lock:
            binding = self._runs.get(run_id)
            local = self._locals.get(user_scope)
            if local is None:
                raise CoreV01AdapterError("Core v0.1 local user scope is unavailable")
            if binding is None:
                binding = _RunBinding(
                    local=local,
                    request_fingerprint=request_fingerprint,
                    owner_scope=_owner_scope(projected),
                    preferred_model=projected.preferred_model,
                    fresh_authorization_check=fresh_authorization_check,
                    capability_authorization_check=capability_authorization_check,
                )
                self._runs[run_id] = binding
            elif binding.local is not local or binding.request_fingerprint != request_fingerprint:
                raise CoreV01AdapterError("Core v0.1 run binding changed")
            return binding

    def _binding_now(self, run_id: str) -> _RunBinding:
        if not isinstance(run_id, str):
            raise TypeError("run_id must be a string")
        binding = self._runs.get(run_id)
        if binding is None:
            raise UnknownRunError(run_id)
        return binding

    def _prune_terminal_bindings(self, local: LocalExecutionGateway) -> None:
        while sum(binding.local is local for binding in self._runs.values()) >= self._max_runs_per_user:
            terminal_run_id = next(
                (run_id for run_id, binding in self._runs.items() if binding.local is local and binding.terminal),
                None,
            )
            if terminal_run_id is None:
                return
            self._runs.pop(terminal_run_id)


def normalize_core_event_v01(raw: Mapping[str, object]) -> RunEvent | None:
    """v0.1 `event`+`data`だけをstrictに正規化し、未知eventは無視する。"""

    if not isinstance(raw, Mapping):
        raise CoreV01AdapterError("Core v0.1 event must be a mapping")
    copied = dict(raw)
    if set(copied) != {"event", "data"} or not isinstance(copied["event"], str):
        raise CoreV01AdapterError("Core v0.1 event envelope is invalid")
    kind = copied["event"].strip()
    if not kind:
        raise CoreV01AdapterError("Core v0.1 event kind is invalid")
    if kind not in _STATUS_EVENTS | {"delta", "tool_start", "final", "error"}:
        return None
    data = copied["data"]
    if not isinstance(data, Mapping) or _contains_raw_bytes(data):
        raise CoreV01AdapterError("Core v0.1 event data is invalid")
    normalized = dict(data)
    _require_json_value(normalized, depth=0)

    if kind in _STATUS_EVENTS:
        return RunEvent(kind="status", payload={**normalized, "core_event": kind})
    if kind == "delta":
        if set(normalized) != {"text"} or not isinstance(normalized["text"], str) or not normalized["text"]:
            raise CoreV01AdapterError("Core v0.1 delta event is invalid")
        return RunEvent(kind="text_delta", text=normalized["text"])
    if kind == "tool_start":
        if set(normalized) not in (
            {"tool", "tool_call_id"},
            {"tool", "tool_call_id", "arguments"},
        ):
            raise CoreV01AdapterError("Core v0.1 tool_start event is invalid")
        tool = _event_identifier(normalized["tool"], label="tool")
        tool_call_id = _event_identifier(normalized["tool_call_id"], label="tool_call_id")
        arguments = normalized.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise CoreV01AdapterError("Core v0.1 tool arguments are invalid")
        return RunEvent(
            kind="action_required",
            payload={
                "tool": tool,
                "tool_call_id": tool_call_id,
                "arguments": dict(arguments),
            },
        )
    if kind == "final":
        if not set(normalized).issubset({"text", "artifacts"}):
            raise CoreV01AdapterError("Core v0.1 final event is invalid")
        text = normalized.get("text")
        if text is not None and not isinstance(text, str):
            raise CoreV01AdapterError("Core v0.1 final text is invalid")
        artifacts = normalized.get("artifacts", ())
        if not isinstance(artifacts, (list, tuple)) or len(artifacts) > 32:
            raise CoreV01AdapterError("Core v0.1 final artifacts are invalid")
        try:
            references = tuple(
                artifact_reference_from_core_v01(core_artifact_ref_from_mapping_v01(item)) for item in artifacts
            )
        except CoreFilesContractError:
            raise CoreV01AdapterError("Core v0.1 final artifact ref is invalid") from None
        payload = {} if not references else {"artifacts": references}
        return RunEvent(kind="final", text=text, payload=payload)
    if not set(normalized).issubset({"code", "error_type"}):
        raise CoreV01AdapterError("Core v0.1 error event is invalid")
    return RunEvent(
        kind="error",
        text="execution failed",
        payload=_safe_error_payload(normalized),
    )


def _project_tool_result(
    result: CapabilityResult,
    owner_scope: CoreArtifactOwnerScopeV01,
) -> CoreToolResultV01:
    if result.metadata or result.extensions:
        raise CoreV01AdapterError("neutral result metadata/extensions cannot cross the Core v0.1 boundary")
    if _contains_secret_like_value(result.output):
        raise CoreV01AdapterError("secret-like tool result cannot cross the Core v0.1 boundary")
    artifacts = [
        core_ref_from_artifact_v01(artifact, owner_scope=owner_scope).to_mapping() for artifact in result.artifacts
    ]
    wire_result: object = {
        "output": result.output,
        "is_error": result.is_error,
        "artifacts": artifacts,
    }
    return CoreToolResultV01(
        tool=result.capability,
        result=wire_result,
        tool_call_id=result.result_id,
    )


def _identity_scope(request: CoreMessageRequestV01) -> str:
    return f"{request.user_identity['provider']}:{request.user_identity['id']}"


def _owner_scope(request: CoreMessageRequestV01) -> CoreArtifactOwnerScopeV01:
    return CoreArtifactOwnerScopeV01(
        provider="discord",
        subject_id=request.user_identity["id"],
        conversation_id=request.conversation_id,
    )


def _fingerprint_request(request: CoreMessageRequestV01) -> str:
    try:
        rendered = json.dumps(
            request.to_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise CoreV01AdapterError("Core v0.1 request cannot be fingerprinted") from None
    return hashlib.sha256(rendered).hexdigest()


def _fingerprint_result(result: CoreToolResultV01) -> str:
    try:
        rendered = json.dumps(
            result.to_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise CoreV01AdapterError("Core v0.1 result cannot be fingerprinted") from None
    return hashlib.sha256(rendered).hexdigest()


def _fingerprint_tool_call(event: RunEvent) -> str:
    return _fingerprint_json_value(
        {
            "tool": event.payload["tool"],
            "arguments": event.payload["arguments"],
        },
        label="Core v0.1 tool call",
    )


def _fingerprint_json_value(value: object, *, label: str) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise CoreV01AdapterError(f"{label} cannot be fingerprinted") from None
    return hashlib.sha256(rendered).hexdigest()


def _event_identifier(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CoreV01AdapterError(f"Core v0.1 {label} is invalid")
    return value


def _safe_error_payload(payload: Mapping[str, object]) -> dict[str, str]:
    safe = {"code": "core_error"}
    for key in ("code", "error_type"):
        value = payload.get(key)
        if isinstance(value, str) and value and len(value) <= 128 and value.isascii():
            safe[key] = value
    return safe


async def _require_fresh_authorization(binding: _RunBinding) -> None:
    check = binding.fresh_authorization_check
    if check is None:
        return
    try:
        candidate = check()
        allowed = await candidate if inspect.isawaitable(candidate) else candidate
    except asyncio.CancelledError:
        raise
    except Exception:
        raise CoreV01AuthorizationError("Core v0.1 fresh authorization check failed") from None
    if allowed is not True:
        raise CoreV01AuthorizationError("Core v0.1 remote execution is not freshly authorized")


def _require_sync_authorization(check: Callable[[], bool] | None) -> None:
    if check is None:
        return
    try:
        allowed = check()
    except Exception:
        raise CoreV01AuthorizationError("Core v0.1 authorization check failed") from None
    if allowed is not True:
        raise CoreV01AuthorizationError("Core v0.1 remote execution is not authorized")


def _require_capability_authorization(
    check: Callable[[str], bool] | None,
    capability: str,
) -> None:
    if check is None:
        return
    try:
        allowed = check(capability)
    except Exception:
        raise CoreV01AuthorizationError("Core v0.1 capability authorization check failed") from None
    if allowed is not True:
        raise CoreV01AuthorizationError("Core v0.1 capability is not authorized")


def _release_authorization_callbacks(binding: _RunBinding) -> None:
    binding.fresh_authorization_check = None
    binding.capability_authorization_check = None


def _contains_raw_bytes(value: object) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, Mapping):
        return any(_contains_raw_bytes(key) or _contains_raw_bytes(item) for key, item in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(_contains_raw_bytes(item) for item in value)
    return False


def _contains_secret_like_value(value: object) -> bool:
    if isinstance(value, str):
        return contains_secret_like(value)
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and isinstance(item, str) and contains_secret_like(f"{key}={item}"):
                return True
            if _contains_secret_like_value(key) or _contains_secret_like_value(item):
                return True
        return False
    if isinstance(value, (tuple, list)):
        return any(_contains_secret_like_value(item) for item in value)
    return False


def _require_json_value(value: object, *, depth: int) -> None:
    if depth > 16:
        raise CoreV01AdapterError("Core v0.1 event data is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise CoreV01AdapterError("Core v0.1 event data contains a non-finite number")
        return
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise CoreV01AdapterError("Core v0.1 event data mapping is too large")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise CoreV01AdapterError("Core v0.1 event data key is invalid")
            _require_json_value(item, depth=depth + 1)
        return
    if isinstance(value, (tuple, list)):
        if len(value) > 256:
            raise CoreV01AdapterError("Core v0.1 event data list is too large")
        for item in value:
            _require_json_value(item, depth=depth + 1)
        return
    raise CoreV01AdapterError("Core v0.1 event data is not JSON-compatible")


__all__ = [
    "CoreV01AuthorizationError",
    "CoreV01AdapterError",
    "YonerAIInternalRunGatewayV01",
    "normalize_core_event_v01",
]
