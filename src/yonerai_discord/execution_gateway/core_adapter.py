from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from .core_contract import CoreRunPort, project_core_request
from .local import LocalExecutionContext, LocalExecutionGateway
from .models import CapabilityResult, RunEvent, RunInput, RunReference


_CORE_EVENT_KINDS = {
    "meta": "status",
    "progress": "status",
    "status": "status",
    "delta": "text_delta",
    "text_delta": "text_delta",
    "final": "final",
    "error": "error",
}


class CoreAdapterError(RuntimeError):
    """Injected CoreRunPortがneutral gateway契約を完了できなかった。"""


class CoreExecutionGateway:
    """CoreRunPortを既存LocalExecutionGatewayのidempotency/event契約へ適合する。"""

    def __init__(self, port: CoreRunPort, *, max_runs: int = 4_096) -> None:
        if not callable(getattr(port, "run", None)):
            raise TypeError("port must expose a callable run method")
        self._port = port
        self._local = LocalExecutionGateway(
            self._execute,
            with_context=True,
            max_runs=max_runs,
        )

    async def start(self, request: RunInput) -> RunReference:
        project_core_request(request)
        return await self._local.start(request)

    def events(self, run_id: str) -> AsyncIterator[RunEvent]:
        return self._local.events(run_id)

    async def submit_result(self, run_id: str, result: CapabilityResult) -> None:
        del run_id, result
        raise CoreAdapterError("Core tool result submission is not wired")

    async def cancel(self, run_id: str) -> None:
        await self._local.cancel(run_id)

    async def _execute(self, request: RunInput, context: LocalExecutionContext) -> None:
        projected = project_core_request(request)
        stream = self._port.run(projected)
        if not hasattr(stream, "__aiter__"):
            raise CoreAdapterError("CoreRunPort.run must return an async iterator")

        async for raw in stream:
            event = normalize_core_event(raw)
            if event is None:
                continue
            await context.emit(event)
            if event.terminal:
                return
        raise CoreAdapterError("Core stream ended without a terminal event")


def normalize_core_event(raw: Mapping[str, object]) -> RunEvent | None:
    """data-onlyとevent/dataの2形を既存RunEventへ正規化する。"""

    if not isinstance(raw, Mapping):
        raise CoreAdapterError("Core event must be a mapping")
    copied = dict(raw)
    if "event" in copied:
        raw_kind = copied["event"]
        raw_data = copied.get("data")
    else:
        raw_kind = copied.get("kind")
        raw_data = copied
    if not isinstance(raw_kind, str):
        raise CoreAdapterError("Core event kind must be a string")
    neutral_kind = _CORE_EVENT_KINDS.get(raw_kind.strip())
    if neutral_kind is None:
        return None
    if "event" in copied:
        if not isinstance(raw_data, Mapping):
            raise CoreAdapterError("Core event data must be a mapping")
        normalized = dict(raw_data)
        if set(copied) != {"event", "data"} or not set(normalized).issubset({"text", "payload"}):
            raise CoreAdapterError("Core event data schema is invalid")
    else:
        if not set(copied).issubset({"kind", "text", "payload"}):
            raise CoreAdapterError("Core data-only event schema is invalid")
        normalized = {key: value for key, value in raw_data.items() if key != "kind"}

    text = normalized.get("text")
    if text is not None and not isinstance(text, str):
        raise CoreAdapterError("Core event text must be a string or None")
    payload = normalized.get("payload", {})
    if not isinstance(payload, Mapping) or _contains_raw_bytes(payload):
        raise CoreAdapterError("Core event payload must be a bytes-free mapping")
    if neutral_kind == "text_delta" and (not isinstance(text, str) or not text):
        raise CoreAdapterError("Core delta event requires text")
    if neutral_kind == "error":
        return RunEvent(
            kind="error",
            text="execution failed",
            payload=_safe_error_payload(payload),
        )
    return RunEvent(
        kind=neutral_kind,
        text=text,
        payload=dict(payload),
    )


def _safe_error_payload(payload: Mapping[str, object]) -> dict[str, str]:
    safe = {"code": "core_error"}
    code = payload.get("code")
    error_type = payload.get("error_type")
    if isinstance(code, str) and code and len(code) <= 128 and code.isascii():
        safe["code"] = code
    if isinstance(error_type, str) and error_type and len(error_type) <= 128 and error_type.isascii():
        safe["error_type"] = error_type
    return safe


def _contains_raw_bytes(value: object) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, Mapping):
        return any(_contains_raw_bytes(key) or _contains_raw_bytes(item) for key, item in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(_contains_raw_bytes(item) for item in value)
    return False


__all__ = [
    "CoreAdapterError",
    "CoreExecutionGateway",
    "normalize_core_event",
]
