from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from .models import CapabilityResult, RunEvent, RunInput, RunReference


class ExecutionGatewayError(RuntimeError):
    pass


class UnknownRunError(ExecutionGatewayError, KeyError):
    pass


class RunTerminalError(ExecutionGatewayError):
    pass


class IdempotencyConflictError(ExecutionGatewayError):
    pass


_CANCELLED = object()
LocalExecutor = Callable[..., Awaitable[object] | AsyncIterable[RunEvent] | object]
ResultAdapter = Callable[[object], RunEvent | Iterable[RunEvent]]


@dataclass(slots=True)
class _RunState:
    reference: RunReference
    fingerprint: str
    request: RunInput | None = field(repr=False)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    result_queue: asyncio.Queue[CapabilityResult | object] = field(default_factory=asyncio.Queue)
    events: list[RunEvent] = field(default_factory=list)
    submitted_results: dict[str, CapabilityResult] = field(default_factory=dict)
    terminal: bool = False
    task: asyncio.Task[None] | None = None


class LocalExecutionContext:
    """Local executorがevent発行とtool continuationに使うrun内helper。

    これは外部adapter境界ではなく、LocalExecutionGateway内部の実行contextである。
    既存の単発AI serviceはこのhelperを使わず、そのまま包める。
    """

    __slots__ = ("_gateway", "_state")

    def __init__(self, gateway: LocalExecutionGateway, state: _RunState) -> None:
        self._gateway = gateway
        self._state = state

    @property
    def run_id(self) -> str:
        return self._state.reference.run_id

    @property
    def cancelled(self) -> bool:
        return self._state.terminal and bool(
            self._state.events
            and self._state.events[-1].kind == "error"
            and self._state.events[-1].payload.get("code") == "cancelled"
        )

    async def emit(self, event: RunEvent) -> RunEvent:
        return await self._gateway._emit(self._state, event)

    async def next_result(self) -> CapabilityResult:
        if self._state.terminal:
            if self.cancelled:
                raise asyncio.CancelledError
            raise RunTerminalError(f"run {self.run_id} is already terminal")
        value = await self._state.result_queue.get()
        if value is _CANCELLED:
            raise asyncio.CancelledError
        if not isinstance(value, CapabilityResult):
            raise RuntimeError("local result queue contains an invalid value")
        return value


class LocalExecutionGateway:
    """既存Local Runtimeをneutral run/event契約で包むin-process gateway。"""

    def __init__(
        self,
        executor: LocalExecutor,
        *,
        with_context: bool = False,
        result_adapter: ResultAdapter | None = None,
        max_runs: int = 4_096,
    ) -> None:
        if not callable(executor):
            raise TypeError("executor must be callable")
        if type(with_context) is not bool:
            raise TypeError("with_context must be a boolean")
        if result_adapter is not None and not callable(result_adapter):
            raise TypeError("result_adapter must be callable or None")
        if isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs <= 0:
            raise ValueError("max_runs must be a positive integer")
        self._executor = executor
        self._with_context = with_context
        self._result_adapter = result_adapter or _default_result_adapter
        self._max_runs = max_runs
        self._lock = asyncio.Lock()
        self._runs: dict[str, _RunState] = {}
        self._idempotency: dict[str, str] = {}

    @classmethod
    def from_ai_service(cls, service: object) -> LocalExecutionGateway:
        """既存``AIService.ask``を移動・書換えせずに包む。

        ``RunInput.local_payload`` へ既存AIRequestを渡す。request単位の
        ``authorization_check`` は既存serviceのprovider sink再認可へ接続する。
        """

        ask = getattr(service, "ask", None)
        if not callable(ask):
            raise TypeError("service must expose a callable ask method")
        try:
            parameters = inspect.signature(ask).parameters.values()
            supports_fresh_authorization = any(
                parameter.name == "fresh_provider_call_allowed" or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_fresh_authorization = False

        async def execute(request: RunInput) -> object:
            if request.local_payload is None:
                raise TypeError("AI service execution requires RunInput.local_payload")
            keyword_arguments: dict[str, object] = {}
            if request.authorization_check is not None:
                keyword_arguments["provider_call_allowed"] = request.authorization_check
            if request.fresh_authorization_check is not None and supports_fresh_authorization:
                keyword_arguments["fresh_provider_call_allowed"] = request.fresh_authorization_check
            if request.capability_authorization_check is not None:
                keyword_arguments["tool_capability_allowed"] = request.capability_authorization_check
            if not keyword_arguments:
                return await ask(request.local_payload)
            return await ask(request.local_payload, **keyword_arguments)

        return cls(execute)

    async def start(self, request: RunInput) -> RunReference:
        if not isinstance(request, RunInput):
            raise TypeError("request must be a RunInput")
        async with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id is not None:
                state = self._runs[existing_id]
                if state.fingerprint != _neutral_fingerprint(request):
                    raise IdempotencyConflictError("idempotency key was already used for a different input")
                return replace(state.reference, reused=True)

            self._prune_terminal_runs()
            if len(self._runs) >= self._max_runs:
                raise ExecutionGatewayError("local execution capacity is exhausted")
            run_id = uuid4().hex
            reference = RunReference(run_id, request.idempotency_key)
            state = _RunState(
                reference=reference,
                fingerprint=_neutral_fingerprint(request),
                request=request,
            )
            self._runs[run_id] = state
            self._idempotency[request.idempotency_key] = run_id
            task = asyncio.create_task(self._run(state), name=f"local-execution-{run_id[:12]}")
            state.task = task
            task.add_done_callback(_consume_task_result)
            return reference

    async def events(self, run_id: str) -> AsyncIterator[RunEvent]:
        state = await self._state(run_id)
        index = 0
        while True:
            async with state.condition:
                await state.condition.wait_for(lambda: index < len(state.events) or state.terminal)
                if index >= len(state.events):
                    return
                batch = tuple(state.events[index:])
                index += len(batch)
            for event in batch:
                yield event
            if state.terminal and index >= len(state.events):
                return

    async def submit_result(self, run_id: str, result: CapabilityResult) -> None:
        if not isinstance(result, CapabilityResult):
            raise TypeError("result must be a CapabilityResult")
        state = await self._state(run_id)
        async with state.condition:
            if state.terminal:
                raise RunTerminalError(f"run {run_id} is already terminal")
            previous = state.submitted_results.get(result.result_id)
            if previous is not None:
                if previous != result:
                    raise IdempotencyConflictError("result_id was already submitted with different content")
                return
            state.submitted_results[result.result_id] = result
            event = _bound_event(
                state,
                RunEvent(
                    kind="tool_result",
                    payload={
                        "result_id": result.result_id,
                        "capability": result.capability,
                        "output": result.output,
                        "is_error": result.is_error,
                        "artifacts": result.artifacts,
                    },
                    extensions=result.extensions,
                ),
            )
            state.events.append(event)
            state.result_queue.put_nowait(result)
            state.condition.notify_all()

    async def cancel(self, run_id: str) -> None:
        state = await self._state(run_id)
        async with state.condition:
            if state.terminal:
                return
            event = _bound_event(
                state,
                RunEvent(
                    kind="error",
                    text="cancelled",
                    payload={"code": "cancelled"},
                ),
            )
            state.events.append(event)
            state.terminal = True
            state.result_queue.put_nowait(_CANCELLED)
            task = state.task
            state.request = None
            state.condition.notify_all()
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _run(self, state: _RunState) -> None:
        context = LocalExecutionContext(self, state)
        try:
            request = state.request
            if request is None:
                raise RuntimeError("run request is unavailable")
            raw = self._executor(request, context) if self._with_context else self._executor(request)
            outcome = await raw if inspect.isawaitable(raw) else raw
            if not state.terminal:
                await self._consume_outcome(state, outcome)
            if not state.terminal:
                await self._emit(state, RunEvent(kind="final"))
        except asyncio.CancelledError:
            if not state.terminal:
                with suppress(RunTerminalError):
                    await self._emit(
                        state,
                        RunEvent(kind="error", text="cancelled", payload={"code": "cancelled"}),
                    )
            raise
        except RunTerminalError:
            if not state.terminal:
                await self._fail(state, RuntimeError("terminal state changed during execution"))
        except Exception as exc:
            await self._fail(state, exc)
        finally:
            state.request = None

    async def _consume_outcome(self, state: _RunState, outcome: object) -> None:
        if isinstance(outcome, AsyncIterable):
            async for event in outcome:
                if not isinstance(event, RunEvent):
                    raise TypeError("async executor streams must contain RunEvent instances")
                await self._emit(state, event)
            return

        events = self._result_adapter(outcome)
        if isinstance(events, RunEvent):
            await self._emit(state, events)
            return
        if isinstance(events, (str, bytes, bytearray, Mapping)):
            raise TypeError("result_adapter must return a RunEvent or an iterable of RunEvent")
        for event in events:
            if not isinstance(event, RunEvent):
                raise TypeError("result_adapter iterable must contain RunEvent instances")
            await self._emit(state, event)

    async def _emit(self, state: _RunState, event: RunEvent) -> RunEvent:
        if not isinstance(event, RunEvent):
            raise TypeError("event must be a RunEvent")
        async with state.condition:
            if state.terminal:
                raise RunTerminalError(f"run {state.reference.run_id} is already terminal")
            bound = _bound_event(state, event)
            state.events.append(bound)
            if bound.terminal:
                state.terminal = True
            state.condition.notify_all()
            return bound

    async def _fail(self, state: _RunState, error: Exception) -> None:
        if state.terminal:
            return
        with suppress(RunTerminalError):
            await self._emit(
                state,
                RunEvent(
                    kind="error",
                    text="execution failed",
                    payload={
                        "code": "execution_failed",
                        "error_type": type(error).__name__,
                    },
                ),
            )

    async def _state(self, run_id: str) -> _RunState:
        if not isinstance(run_id, str):
            raise TypeError("run_id must be a string")
        async with self._lock:
            state = self._runs.get(run_id)
        if state is None:
            raise UnknownRunError(run_id)
        return state

    def _prune_terminal_runs(self) -> None:
        while len(self._runs) >= self._max_runs:
            terminal_id = next((run_id for run_id, state in self._runs.items() if state.terminal), None)
            if terminal_id is None:
                return
            state = self._runs.pop(terminal_id)
            self._idempotency.pop(state.reference.idempotency_key, None)


def _neutral_fingerprint(request: RunInput) -> str:
    """本文やlocal payloadを保持せず、neutral入力の同一性だけを残す。"""

    artifacts = [
        {
            "artifact_id": artifact.artifact_id,
            "kind": artifact.kind,
            "uri": artifact.uri,
            "name": artifact.name,
            "media_type": artifact.media_type,
            "size_bytes": artifact.size_bytes,
            "metadata": dict(artifact.metadata),
            "extensions": dict(artifact.extensions),
        }
        for artifact in request.artifacts
    ]
    payload = {
        "input_text": request.input_text,
        "conversation_key": request.conversation_key,
        "artifacts": artifacts,
        "metadata": dict(request.metadata),
        "extensions": dict(request.extensions),
    }
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_fingerprint_default,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("run input cannot be fingerprinted") from exc
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _fingerprint_default(value: object) -> object:
    try:
        representation = repr(value)
    except Exception:
        representation = "<unrepresentable>"
    return {
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
        "repr": representation,
    }


def _bound_event(state: _RunState, event: RunEvent) -> RunEvent:
    return replace(
        event,
        run_id=state.reference.run_id,
        sequence=len(state.events) + 1,
    )


def _default_result_adapter(result: object) -> RunEvent | Iterable[RunEvent]:
    if isinstance(result, RunEvent):
        return result
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes, bytearray, Mapping)):
        return result
    if result is None:
        return RunEvent(kind="final")
    if isinstance(result, str):
        return RunEvent(kind="final", text=result)

    text = getattr(result, "text", None)
    if isinstance(text, str):
        payload: dict[str, Any] = {}
        model = getattr(result, "model", None)
        provider = getattr(result, "provider", None)
        if isinstance(model, str) and model:
            payload["model"] = model
        if isinstance(provider, str) and provider:
            payload["provider"] = provider
        sources = getattr(result, "sources", None)
        if sources:
            payload["sources"] = tuple(
                {"title": str(getattr(source, "title", "")), "url": str(getattr(source, "url", ""))}
                for source in sources
            )
        return RunEvent(kind="final", text=text, payload=payload)
    raise TypeError("executor returned an unsupported result")


def _consume_task_result(task: asyncio.Task[None]) -> None:
    with suppress(asyncio.CancelledError, Exception):
        task.result()


__all__ = [
    "ExecutionGatewayError",
    "IdempotencyConflictError",
    "LocalExecutionContext",
    "LocalExecutionGateway",
    "RunTerminalError",
    "UnknownRunError",
]
