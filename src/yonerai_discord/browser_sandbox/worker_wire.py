from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from typing import TypeVar

from .models import (
    BrowserAction,
    BrowserOutput,
    BrowserOutputKind,
    Click,
    CssSelector,
    ExtractText,
    Navigate,
    Screenshot,
    ScreenshotFormat,
    Scroll,
    SelectOption,
    TypeText,
    Wait,
)
from .worker_contract import (
    BROWSER_WORKER_PROTOCOL_REVISION,
    BROWSER_WORKER_SUPPORTED_ACTIONS,
    BrowserWorkerContractError,
    BrowserWorkerExecuteRequest,
    BrowserWorkerExecutionResult,
    BrowserWorkerHandshakeRequest,
    BrowserWorkerHandshakeResponse,
    BrowserWorkerTerminateRequest,
    BrowserWorkerTerminationReceipt,
)


MAX_BROWSER_WORKER_OUTPUT_BYTES = 100 * 1024 * 1024
MAX_BROWSER_WORKER_WIRE_BYTES = ((MAX_BROWSER_WORKER_OUTPUT_BYTES + 2) // 3) * 4 + 1024 * 1024
_MAX_CONTROL_WIRE_BYTES = 64 * 1024
# 200 actions × (8,000 text chars at six JSON bytes + a 512-char selector) plus envelope overhead.
_MAX_EXECUTE_REQUEST_WIRE_BYTES = 12 * 1024 * 1024
_MAX_ACTIONS = 200
_T = TypeVar("_T")


class BrowserWorkerWireError(BrowserWorkerContractError):
    """Canonical worker JSON could not be decoded without weakening the contract."""


def encode_worker_handshake_request(value: BrowserWorkerHandshakeRequest) -> bytes:
    _require_exact_type(value, BrowserWorkerHandshakeRequest)
    return _encode_message("handshake_request", _handshake_request_payload(value))


def decode_worker_handshake_request(data: bytes) -> BrowserWorkerHandshakeRequest:
    return _decode_message(data, "handshake_request", _parse_handshake_request)


def encode_worker_handshake_response(value: BrowserWorkerHandshakeResponse) -> bytes:
    _require_exact_type(value, BrowserWorkerHandshakeResponse)
    return _encode_message(
        "handshake_response",
        {
            "clipboard_enabled": value.clipboard_enabled,
            "context_reuse_enabled": value.context_reuse_enabled,
            "credential_import_enabled": value.credential_import_enabled,
            "developer_protocol_enabled": value.developer_protocol_enabled,
            "downloads_enabled": value.downloads_enabled,
            "ephemeral_profile": value.ephemeral_profile,
            "external_worker_identity": value.external_worker_identity,
            "host_mount_enabled": value.host_mount_enabled,
            "isolation_contract_digest": value.isolation_contract_digest,
            "nonce": value.nonce,
            "policy_snapshot_digest": value.policy_snapshot_digest,
            "protocol_revision": value.protocol_revision,
            "request_id": value.request_id,
            "script_evaluation_enabled": value.script_evaluation_enabled,
            "session_id": value.session_id,
            "supported_actions": list(value.supported_actions),
            "uploads_enabled": value.uploads_enabled,
        },
    )


def decode_worker_handshake_response(data: bytes) -> BrowserWorkerHandshakeResponse:
    return _decode_message(data, "handshake_response", _parse_handshake_response)


def encode_worker_execute_request(value: BrowserWorkerExecuteRequest) -> bytes:
    _require_exact_type(value, BrowserWorkerExecuteRequest)
    return _encode_message(
        "execute_request",
        {
            "actions": [_action_payload(action) for action in value.actions],
            "external_worker_identity": value.external_worker_identity,
            "handshake": _handshake_request_payload(value.handshake),
        },
    )


def decode_worker_execute_request(data: bytes) -> BrowserWorkerExecuteRequest:
    return _decode_message(data, "execute_request", _parse_execute_request)


def encode_worker_execution_result(value: BrowserWorkerExecutionResult) -> bytes:
    _require_exact_type(value, BrowserWorkerExecutionResult)
    _require_output_budget(value.outputs)
    return _encode_message(
        "execution_result",
        {
            "completed_actions": value.completed_actions,
            "consumed_bytes": value.consumed_bytes,
            "external_worker_identity": value.external_worker_identity,
            "isolation_contract_digest": value.isolation_contract_digest,
            "network_request_count": value.network_request_count,
            "nonce": value.nonce,
            "outputs": [_output_payload(output) for output in value.outputs],
            "policy_snapshot_digest": value.policy_snapshot_digest,
            "redirect_count": value.redirect_count,
            "request_id": value.request_id,
            "session_id": value.session_id,
        },
    )


def decode_worker_execution_result(data: bytes) -> BrowserWorkerExecutionResult:
    return _decode_message(data, "execution_result", _parse_execution_result)


def encode_worker_terminate_request(value: BrowserWorkerTerminateRequest) -> bytes:
    _require_exact_type(value, BrowserWorkerTerminateRequest)
    return _encode_message("terminate_request", _terminate_request_payload(value))


def decode_worker_terminate_request(data: bytes) -> BrowserWorkerTerminateRequest:
    return _decode_message(data, "terminate_request", _parse_terminate_request)


def encode_worker_termination_receipt(value: BrowserWorkerTerminationReceipt) -> bytes:
    _require_exact_type(value, BrowserWorkerTerminationReceipt)
    return _encode_message(
        "termination_receipt",
        {
            **_terminate_binding_payload(value),
            "profile_destroyed": value.profile_destroyed,
            "terminal_sequence": value.terminal_sequence,
            "worker_terminated": value.worker_terminated,
        },
    )


def decode_worker_termination_receipt(data: bytes) -> BrowserWorkerTerminationReceipt:
    return _decode_message(data, "termination_receipt", _parse_termination_receipt)


def _encode_message(message_type: str, payload: dict[str, object]) -> bytes:
    try:
        rendered = json.dumps(
            {
                "message_type": message_type,
                "payload": payload,
                "protocol_revision": BROWSER_WORKER_PROTOCOL_REVISION,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BrowserWorkerWireError("browser worker message could not be encoded") from exc
    if len(rendered) > _message_wire_limit(message_type):
        raise BrowserWorkerWireError("browser worker message exceeded the wire byte limit")
    return rendered


def _decode_message(data: bytes, message_type: str, parser: Callable[[dict[str, object]], _T]) -> _T:
    if type(data) is not bytes:
        raise BrowserWorkerWireError("browser worker message must be exact bytes")
    if not data or len(data) > _message_wire_limit(message_type):
        raise BrowserWorkerWireError("browser worker message size is outside the wire limit")
    if data.startswith(b"\xef\xbb\xbf"):
        raise BrowserWorkerWireError("browser worker message must not contain a UTF-8 BOM")
    try:
        decoded = data.decode("utf-8", errors="strict")
        document = json.loads(
            decoded,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        _reject_surrogates(document)
        envelope = _exact_object(
            document,
            {"message_type", "payload", "protocol_revision"},
            "worker envelope",
        )
        if envelope["protocol_revision"] != BROWSER_WORKER_PROTOCOL_REVISION:
            raise BrowserWorkerWireError("unsupported browser worker wire revision")
        if envelope["message_type"] != message_type:
            raise BrowserWorkerWireError("unexpected browser worker message type")
        payload = _require_object(envelope["payload"], "worker payload")
        return parser(payload)
    except BrowserWorkerWireError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        binascii.Error,
        RecursionError,
    ) as exc:
        raise BrowserWorkerWireError("browser worker message failed strict decoding") from exc


def _handshake_request_payload(value: BrowserWorkerHandshakeRequest) -> dict[str, object]:
    return {
        "deadline_milliseconds": value.deadline_milliseconds,
        "isolation_contract_digest": value.isolation_contract_digest,
        "max_actions": value.max_actions,
        "max_network_requests": value.max_network_requests,
        "max_redirects": value.max_redirects,
        "max_total_bytes": value.max_total_bytes,
        "nonce": value.nonce,
        "policy_snapshot_digest": value.policy_snapshot_digest,
        "protocol_revision": value.protocol_revision,
        "request_id": value.request_id,
        "session_id": value.session_id,
    }


def _parse_handshake_request(value: dict[str, object]) -> BrowserWorkerHandshakeRequest:
    payload = _exact_object(
        value,
        {
            "deadline_milliseconds",
            "isolation_contract_digest",
            "max_actions",
            "max_network_requests",
            "max_redirects",
            "max_total_bytes",
            "nonce",
            "policy_snapshot_digest",
            "protocol_revision",
            "request_id",
            "session_id",
        },
        "handshake request",
    )
    return BrowserWorkerHandshakeRequest(**payload)


def _parse_handshake_response(value: dict[str, object]) -> BrowserWorkerHandshakeResponse:
    payload = _exact_object(
        value,
        {
            "clipboard_enabled",
            "context_reuse_enabled",
            "credential_import_enabled",
            "developer_protocol_enabled",
            "downloads_enabled",
            "ephemeral_profile",
            "external_worker_identity",
            "host_mount_enabled",
            "isolation_contract_digest",
            "nonce",
            "policy_snapshot_digest",
            "protocol_revision",
            "request_id",
            "script_evaluation_enabled",
            "session_id",
            "supported_actions",
            "uploads_enabled",
        },
        "handshake response",
    )
    supported_actions = _require_list(payload.pop("supported_actions"), "supported_actions")
    if supported_actions != list(BROWSER_WORKER_SUPPORTED_ACTIONS):
        raise BrowserWorkerWireError("worker supported actions did not exactly match the wire contract")
    payload["supported_actions"] = tuple(supported_actions)
    return BrowserWorkerHandshakeResponse(**payload)


def _parse_execute_request(value: dict[str, object]) -> BrowserWorkerExecuteRequest:
    payload = _exact_object(value, {"actions", "external_worker_identity", "handshake"}, "execute request")
    actions = _require_list(payload["actions"], "actions")
    if not 1 <= len(actions) <= _MAX_ACTIONS:
        raise BrowserWorkerWireError("worker action count is outside the wire limit")
    return BrowserWorkerExecuteRequest(
        handshake=_parse_handshake_request(_require_object(payload["handshake"], "handshake")),
        external_worker_identity=payload["external_worker_identity"],
        actions=tuple(_parse_action(action) for action in actions),
    )


def _parse_execution_result(value: dict[str, object]) -> BrowserWorkerExecutionResult:
    payload = _exact_object(
        value,
        {
            "completed_actions",
            "consumed_bytes",
            "external_worker_identity",
            "isolation_contract_digest",
            "network_request_count",
            "nonce",
            "outputs",
            "policy_snapshot_digest",
            "redirect_count",
            "request_id",
            "session_id",
        },
        "execution result",
    )
    outputs = _parse_outputs(payload.pop("outputs"))
    payload["outputs"] = outputs
    return BrowserWorkerExecutionResult(**payload)


def _terminate_binding_payload(
    value: BrowserWorkerTerminateRequest | BrowserWorkerTerminationReceipt,
) -> dict[str, object]:
    return {
        "external_worker_identity": value.external_worker_identity,
        "isolation_contract_digest": value.isolation_contract_digest,
        "nonce": value.nonce,
        "policy_snapshot_digest": value.policy_snapshot_digest,
        "protocol_revision": value.protocol_revision,
        "reason": value.reason.value,
        "request_id": value.request_id,
        "session_id": value.session_id,
    }


def _terminate_request_payload(value: BrowserWorkerTerminateRequest) -> dict[str, object]:
    return _terminate_binding_payload(value)


def _parse_terminate_request(value: dict[str, object]) -> BrowserWorkerTerminateRequest:
    payload = _exact_object(value, _TERMINATE_BINDING_FIELDS, "terminate request")
    return BrowserWorkerTerminateRequest(**payload)


def _parse_termination_receipt(value: dict[str, object]) -> BrowserWorkerTerminationReceipt:
    payload = _exact_object(
        value,
        _TERMINATE_BINDING_FIELDS | {"profile_destroyed", "terminal_sequence", "worker_terminated"},
        "termination receipt",
    )
    return BrowserWorkerTerminationReceipt(**payload)


_TERMINATE_BINDING_FIELDS = {
    "external_worker_identity",
    "isolation_contract_digest",
    "nonce",
    "policy_snapshot_digest",
    "protocol_revision",
    "reason",
    "request_id",
    "session_id",
}


def _action_payload(action: BrowserAction) -> dict[str, object]:
    if type(action) is Navigate:
        return {"action": "navigate", "url": action.url}
    if type(action) is Click:
        return {"action": "click", "selector": action.selector.value}
    if type(action) is TypeText:
        return {
            "action": "type_text",
            "clear_first": action.clear_first,
            "selector": action.selector.value,
            "text": action.text,
        }
    if type(action) is SelectOption:
        return {"action": "select_option", "selector": action.selector.value, "value": action.value}
    if type(action) is Scroll:
        return {"action": "scroll", "delta_x": action.delta_x, "delta_y": action.delta_y}
    if type(action) is Wait:
        return {"action": "wait", "milliseconds": action.milliseconds}
    if type(action) is Screenshot:
        return {
            "action": "screenshot",
            "full_page": action.full_page,
            "image_format": action.image_format.value,
        }
    if type(action) is ExtractText:
        return {
            "action": "extract_text",
            "selector": None if action.selector is None else action.selector.value,
        }
    raise BrowserWorkerWireError("worker action type is not supported by the wire contract")


def _parse_action(value: object) -> BrowserAction:
    payload = _require_object(value, "action")
    action_type = payload.get("action")
    if action_type == "navigate":
        exact = _exact_object(payload, {"action", "url"}, "navigate action")
        return Navigate(url=exact["url"])
    if action_type == "click":
        exact = _exact_object(payload, {"action", "selector"}, "click action")
        return Click(selector=CssSelector(exact["selector"]))
    if action_type == "type_text":
        exact = _exact_object(payload, {"action", "clear_first", "selector", "text"}, "type_text action")
        return TypeText(
            selector=CssSelector(exact["selector"]),
            text=exact["text"],
            clear_first=exact["clear_first"],
        )
    if action_type == "select_option":
        exact = _exact_object(payload, {"action", "selector", "value"}, "select_option action")
        return SelectOption(selector=CssSelector(exact["selector"]), value=exact["value"])
    if action_type == "scroll":
        exact = _exact_object(payload, {"action", "delta_x", "delta_y"}, "scroll action")
        return Scroll(delta_x=exact["delta_x"], delta_y=exact["delta_y"])
    if action_type == "wait":
        exact = _exact_object(payload, {"action", "milliseconds"}, "wait action")
        return Wait(milliseconds=exact["milliseconds"])
    if action_type == "screenshot":
        exact = _exact_object(payload, {"action", "full_page", "image_format"}, "screenshot action")
        return Screenshot(
            full_page=exact["full_page"],
            image_format=ScreenshotFormat(exact["image_format"]),
        )
    if action_type == "extract_text":
        exact = _exact_object(payload, {"action", "selector"}, "extract_text action")
        selector = exact["selector"]
        return ExtractText(selector=None if selector is None else CssSelector(selector))
    raise BrowserWorkerWireError("worker action name is not supported by the wire contract")


def _output_payload(output: BrowserOutput) -> dict[str, object]:
    _require_exact_type(output, BrowserOutput)
    if output.byte_length > MAX_BROWSER_WORKER_OUTPUT_BYTES:
        raise BrowserWorkerWireError("worker output exceeded the output byte limit")
    return {
        "data_base64": base64.b64encode(output.data).decode("ascii"),
        "kind": output.kind.value,
        "media_type": output.media_type,
        "step_index": output.step_index,
    }


def _parse_outputs(value: object) -> tuple[BrowserOutput, ...]:
    raw_outputs = _require_list(value, "outputs")
    if len(raw_outputs) > _MAX_ACTIONS:
        raise BrowserWorkerWireError("worker output count is outside the wire limit")
    outputs: list[BrowserOutput] = []
    total_bytes = 0
    for raw_output in raw_outputs:
        payload = _exact_object(
            raw_output,
            {"data_base64", "kind", "media_type", "step_index"},
            "worker output",
        )
        data = _decode_base64(payload["data_base64"])
        total_bytes += len(data)
        if total_bytes > MAX_BROWSER_WORKER_OUTPUT_BYTES:
            raise BrowserWorkerWireError("worker outputs exceeded the output byte limit")
        outputs.append(
            BrowserOutput(
                step_index=payload["step_index"],
                kind=BrowserOutputKind(payload["kind"]),
                data=data,
                media_type=payload["media_type"],
            )
        )
    return tuple(outputs)


def _decode_base64(value: object) -> bytes:
    if not isinstance(value, str):
        raise BrowserWorkerWireError("worker output base64 must be a string")
    max_encoded = ((MAX_BROWSER_WORKER_OUTPUT_BYTES + 2) // 3) * 4
    if not value or len(value) > max_encoded or not value.isascii():
        raise BrowserWorkerWireError("worker output base64 size is outside the limit")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise BrowserWorkerWireError("worker output base64 is invalid") from exc
    if len(decoded) > MAX_BROWSER_WORKER_OUTPUT_BYTES or base64.b64encode(decoded) != encoded:
        raise BrowserWorkerWireError("worker output base64 is not canonical")
    return decoded


def _require_output_budget(outputs: tuple[BrowserOutput, ...]) -> None:
    if len(outputs) > _MAX_ACTIONS:
        raise BrowserWorkerWireError("worker output count is outside the wire limit")
    if sum(output.byte_length for output in outputs) > MAX_BROWSER_WORKER_OUTPUT_BYTES:
        raise BrowserWorkerWireError("worker outputs exceeded the output byte limit")


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BrowserWorkerWireError("browser worker message contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise BrowserWorkerWireError(f"browser worker message contains a forbidden numeric constant: {value}")


def _reject_surrogates(value: object) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise BrowserWorkerWireError("browser worker message contains an invalid Unicode scalar")
        return
    if isinstance(value, list):
        for item in value:
            _reject_surrogates(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_surrogates(key)
            _reject_surrogates(item)


def _exact_object(value: object, fields: set[str], label: str) -> dict[str, object]:
    payload = _require_object(value, label)
    if set(payload) != fields:
        raise BrowserWorkerWireError(f"{label} fields did not exactly match the wire contract")
    return dict(payload)


def _require_object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise BrowserWorkerWireError(f"{label} must be a JSON object")
    return value


def _require_list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise BrowserWorkerWireError(f"{label} must be a JSON array")
    return value


def _require_exact_type(value: object, expected: type[object]) -> None:
    if type(value) is not expected:
        raise BrowserWorkerWireError(f"wire encoder requires exact {expected.__name__}")


def _message_wire_limit(message_type: str) -> int:
    if message_type == "execute_request":
        return _MAX_EXECUTE_REQUEST_WIRE_BYTES
    if message_type == "execution_result":
        return MAX_BROWSER_WORKER_WIRE_BYTES
    if message_type in {
        "handshake_request",
        "handshake_response",
        "terminate_request",
        "termination_receipt",
    }:
        return _MAX_CONTROL_WIRE_BYTES
    raise BrowserWorkerWireError("unsupported browser worker message type")


__all__ = [
    "MAX_BROWSER_WORKER_OUTPUT_BYTES",
    "MAX_BROWSER_WORKER_WIRE_BYTES",
    "BrowserWorkerWireError",
    "decode_worker_execute_request",
    "decode_worker_execution_result",
    "decode_worker_handshake_request",
    "decode_worker_handshake_response",
    "decode_worker_terminate_request",
    "decode_worker_termination_receipt",
    "encode_worker_execute_request",
    "encode_worker_execution_result",
    "encode_worker_handshake_request",
    "encode_worker_handshake_response",
    "encode_worker_terminate_request",
    "encode_worker_termination_receipt",
]
