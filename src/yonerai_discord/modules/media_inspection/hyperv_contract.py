from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass

from .domain import MAX_INSPECTION_OUTPUT_CHARS, MediaInspectionResponseError


HYPERV_MEDIA_SCHEMA = "yonerai.media-inspection.hyperv.v2"
HYPERV_MEDIA_WORKER_VERSION = "0.2.3"
HYPERV_MEDIA_POLICY_REVISION = "yonerai.media-inspection.hyperv-forced-command.v2"
HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION = "yonerai.media-inspection.hyperv-egress.v1"
HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST = "28e41a000630da23d7659f98b6bf4f0f870c9a75696e9de7c565321c069da31c"
HYPERV_MEDIA_REMOTE_ADDRESS = "172.30.240.2"
HYPERV_MEDIA_REMOTE_USER = "yonerai-media"
MAX_HYPERV_MEDIA_FRAME_BYTES = 64 * 1024
_FRAME_HEADER_BYTES = 4

_IDENTITY_DOCUMENT = {
    "policy_revision": HYPERV_MEDIA_POLICY_REVISION,
    "remote_address": HYPERV_MEDIA_REMOTE_ADDRESS,
    "remote_user": HYPERV_MEDIA_REMOTE_USER,
    "schema": HYPERV_MEDIA_SCHEMA,
    "worker_version": HYPERV_MEDIA_WORKER_VERSION,
}
HYPERV_MEDIA_IDENTITY_DIGEST = hashlib.sha256(
    json.dumps(_IDENTITY_DOCUMENT, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


@dataclass(frozen=True, slots=True)
class HyperVMediaProbeResult:
    ready: bool
    cleanup_confirmed: bool
    identity_digest: str
    effective_policy_revision: str
    effective_policy_digest: str


@dataclass(frozen=True, slots=True)
class HyperVMediaExecutionResult:
    text: str
    cleanup_confirmed: bool
    identity_digest: str
    effective_policy_revision: str
    effective_policy_digest: str


def encode_probe_request() -> bytes:
    return _frame({"operation": "probe", "schema": HYPERV_MEDIA_SCHEMA})


def encode_inspect_request(*, url: str, instruction: str) -> bytes:
    return _frame(
        {
            "instruction": instruction,
            "operation": "inspect",
            "schema": HYPERV_MEDIA_SCHEMA,
            "url": url,
        }
    )


def decode_probe_result(raw: bytes) -> HyperVMediaProbeResult:
    document = _response_document(raw)
    if set(document) != _COMMON_FIELDS | {"ready"}:
        raise MediaInspectionResponseError("Hyper-V probe response shape is invalid")
    ready = document["ready"]
    if not isinstance(ready, bool) or document["cleanup_confirmed"] is not True:
        raise MediaInspectionResponseError("Hyper-V probe was not cleanup-confirmed")
    return HyperVMediaProbeResult(
        ready=ready,
        cleanup_confirmed=True,
        identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
        effective_policy_revision=HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
        effective_policy_digest=HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    )


def decode_execution_result(raw: bytes) -> HyperVMediaExecutionResult:
    document = _response_document(raw)
    if set(document) != _COMMON_FIELDS | {"status", "text"}:
        raise MediaInspectionResponseError("Hyper-V execution response shape is invalid")
    text = document["text"]
    if (
        document["status"] != "completed"
        or document["cleanup_confirmed"] is not True
        or not isinstance(text, str)
        or not text
        or len(text) > MAX_INSPECTION_OUTPUT_CHARS
        or "\x00" in text
    ):
        raise MediaInspectionResponseError("Hyper-V execution response is invalid")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MediaInspectionResponseError("Hyper-V execution text is invalid") from exc
    return HyperVMediaExecutionResult(
        text=text,
        cleanup_confirmed=True,
        identity_digest=HYPERV_MEDIA_IDENTITY_DIGEST,
        effective_policy_revision=HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
        effective_policy_digest=HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    )


def frame_payload(raw: bytes) -> bytes:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_HYPERV_MEDIA_FRAME_BYTES:
        raise MediaInspectionResponseError("Hyper-V frame is invalid")
    return struct.pack(">I", len(raw)) + raw


def _frame(value: Mapping[str, object]) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return frame_payload(raw)


_COMMON_FIELDS = {
    "schema",
    "worker_version",
    "policy_revision",
    "identity_digest",
    "effective_policy_revision",
    "effective_policy_digest",
    "cleanup_confirmed",
}


def _response_document(raw: bytes) -> Mapping[str, object]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_HYPERV_MEDIA_FRAME_BYTES:
        raise MediaInspectionResponseError("Hyper-V response size is invalid")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaInspectionResponseError("Hyper-V response JSON is invalid") from exc
    if not isinstance(document, Mapping):
        raise MediaInspectionResponseError("Hyper-V response must be an object")
    if (
        document.get("schema") != HYPERV_MEDIA_SCHEMA
        or document.get("worker_version") != HYPERV_MEDIA_WORKER_VERSION
        or document.get("policy_revision") != HYPERV_MEDIA_POLICY_REVISION
        or document.get("identity_digest") != HYPERV_MEDIA_IDENTITY_DIGEST
        or document.get("effective_policy_revision") != HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION
        or document.get("effective_policy_digest") != HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST
    ):
        raise MediaInspectionResponseError("Hyper-V worker identity is invalid")
    return document


__all__ = [
    "HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST",
    "HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION",
    "HYPERV_MEDIA_IDENTITY_DIGEST",
    "HYPERV_MEDIA_POLICY_REVISION",
    "HYPERV_MEDIA_REMOTE_ADDRESS",
    "HYPERV_MEDIA_REMOTE_USER",
    "HYPERV_MEDIA_SCHEMA",
    "HYPERV_MEDIA_WORKER_VERSION",
    "HyperVMediaExecutionResult",
    "HyperVMediaProbeResult",
    "MAX_HYPERV_MEDIA_FRAME_BYTES",
    "decode_execution_result",
    "decode_probe_result",
    "encode_inspect_request",
    "encode_probe_request",
    "frame_payload",
]
