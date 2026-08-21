from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey


JOB_SCHEMA_VERSION = "yonerai.exec-sandbox.signed-job.v1"
RECEIPT_SCHEMA_VERSION = "yonerai.exec-sandbox.signed-receipt.v1"
MAX_ENVELOPE_BYTES = 65_536
MAX_CLOCK_SKEW_SECONDS = 60
MAX_JOB_LIFETIME_SECONDS = 600

_JOB_DOMAIN = b"yonerai.exec-sandbox.signed-job.v1\0"
_RECEIPT_DOMAIN = b"yonerai.exec-sandbox.signed-receipt.v1\0"
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_NONCE = re.compile(r"[a-f0-9]{64}\Z")
_JOB_ID = re.compile(r"job_[a-z0-9]{16,64}\Z")
_KEY_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9._:-]{0,127}\Z")
_IDEMPOTENCY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,255}@sha256:[a-f0-9]{64}\Z")
_ARTIFACT_REF = re.compile(r"artifact_[a-z0-9]{16,64}\Z")
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{86}\Z")

_JOB_FIELDS = {
    "schema_version",
    "broker_key_id",
    "job_id",
    "nonce",
    "issued_at",
    "expires_at",
    "scope_digest",
    "capability_id",
    "runtime_id",
    "input_sha256",
    "limits",
    "output_policy",
    "idempotency_key",
    "worker_image",
    "worker_version",
    "policy_revision",
}
_RECEIPT_FIELDS = (_JOB_FIELDS - {"issued_at", "expires_at"}) | {
    "worker_key_id",
    "started_at",
    "finished_at",
    "terminal_state",
    "exit_code",
    "resource_use",
    "stdout_sha256",
    "stderr_sha256",
    "output_sha256",
    "output_bytes",
    "artifacts",
    "guest_evidence",
}
_LIMIT_FIELDS = {"wall_time_ms", "cpu_time_ms", "memory_mib"}
_OUTPUT_FIELDS = {"max_output_bytes", "max_stdout_bytes", "max_stderr_bytes", "max_artifacts", "max_artifact_bytes"}
_USE_FIELDS = {
    "cpu_time_ms",
    "wall_time_ms",
    "peak_memory_mib",
    "stdout_bytes",
    "stderr_bytes",
    "artifact_bytes",
}


class SigningProtocolError(ValueError):
    pass


class TerminalState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class JobVerificationContext:
    broker_key_id: str
    scope_digest: str
    capability_id: str
    runtime_id: str
    input_sha256: str
    worker_image: str
    worker_version: str
    policy_revision: str

    def __post_init__(self) -> None:
        _match(self.broker_key_id, _KEY_ID, "key_id_invalid")
        _match(self.scope_digest, _DIGEST, "binding_invalid")
        _match(self.capability_id, _IDENTIFIER, "binding_invalid")
        _match(self.runtime_id, _IDENTIFIER, "binding_invalid")
        _match(self.input_sha256, _DIGEST, "binding_invalid")
        _match(self.worker_image, _IMAGE, "worker_image_invalid")
        _match(self.worker_version, _REVISION, "worker_version_invalid")
        _match(self.policy_revision, _REVISION, "policy_revision_invalid")


@dataclass(frozen=True, slots=True)
class JobPayload:
    broker_key_id: str
    job_id: str
    nonce: str = field(repr=False)
    issued_at: int
    expires_at: int
    scope_digest: str
    capability_id: str
    runtime_id: str
    input_sha256: str
    limits: Mapping[str, int]
    output_policy: Mapping[str, int]
    idempotency_key: str = field(repr=False)
    worker_image: str
    worker_version: str
    policy_revision: str
    schema_version: str = JOB_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != JOB_SCHEMA_VERSION:
            raise SigningProtocolError("job_schema_invalid")
        _match(self.broker_key_id, _KEY_ID, "key_id_invalid")
        _match(self.job_id, _JOB_ID, "job_id_invalid")
        _match(self.nonce, _NONCE, "nonce_invalid")
        _time(self.issued_at, "job_time_invalid")
        _time(self.expires_at, "job_time_invalid")
        if not 0 < self.expires_at - self.issued_at <= MAX_JOB_LIFETIME_SECONDS:
            raise SigningProtocolError("job_time_invalid")
        _binding(self)
        object.__setattr__(self, "limits", _limits(self.limits))
        object.__setattr__(self, "output_policy", _output_policy(self.output_policy))

    def to_mapping(self) -> dict[str, object]:
        return {name: _plain(getattr(self, name)) for name in _JOB_FIELDS}


@dataclass(frozen=True, slots=True)
class ReceiptPayload:
    broker_key_id: str
    worker_key_id: str
    job_id: str
    nonce: str = field(repr=False)
    scope_digest: str
    capability_id: str
    runtime_id: str
    input_sha256: str
    limits: Mapping[str, int]
    output_policy: Mapping[str, int]
    idempotency_key: str = field(repr=False)
    worker_image: str
    worker_version: str
    policy_revision: str
    started_at: int
    finished_at: int
    terminal_state: TerminalState
    exit_code: int | None
    resource_use: Mapping[str, int]
    stdout_sha256: str
    stderr_sha256: str
    output_sha256: str
    output_bytes: int
    artifacts: tuple[Mapping[str, object], ...] = field(repr=False)
    guest_evidence: Mapping[str, int | bool]
    schema_version: str = RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RECEIPT_SCHEMA_VERSION:
            raise SigningProtocolError("receipt_schema_invalid")
        _match(self.broker_key_id, _KEY_ID, "key_id_invalid")
        _match(self.worker_key_id, _KEY_ID, "key_id_invalid")
        _match(self.job_id, _JOB_ID, "job_id_invalid")
        _match(self.nonce, _NONCE, "nonce_invalid")
        _binding(self)
        object.__setattr__(self, "limits", _limits(self.limits))
        object.__setattr__(self, "output_policy", _output_policy(self.output_policy))
        _time(self.started_at, "receipt_time_invalid")
        _time(self.finished_at, "receipt_time_invalid")
        if self.finished_at < self.started_at:
            raise SigningProtocolError("receipt_time_invalid")
        if not isinstance(self.terminal_state, TerminalState):
            raise SigningProtocolError("terminal_state_invalid")
        if self.exit_code is not None and (type(self.exit_code) is not int or not -(2**31) <= self.exit_code < 2**31):
            raise SigningProtocolError("exit_code_invalid")
        if self.terminal_state is TerminalState.SUCCEEDED and self.exit_code != 0:
            raise SigningProtocolError("exit_code_invalid")
        object.__setattr__(self, "resource_use", _resource_use(self.resource_use))
        _match(self.stdout_sha256, _DIGEST, "stream_digest_invalid")
        _match(self.stderr_sha256, _DIGEST, "stream_digest_invalid")
        _match(self.output_sha256, _DIGEST, "output_invalid")
        if type(self.output_bytes) is not int or not 0 <= self.output_bytes <= 1_048_576:
            raise SigningProtocolError("output_invalid")
        object.__setattr__(self, "artifacts", _artifacts(self.artifacts, self.resource_use["artifact_bytes"]))
        guest = _exact_map(
            self.guest_evidence,
            {
                "workspace_clean",
                "child_process_count",
                "network_interface_count",
                "network_connection_count",
                "network_route_count",
            },
            "guest_evidence_invalid",
        )
        if guest["workspace_clean"] is not True or any(
            type(guest[key]) is not int or guest[key] != 0 for key in guest if key != "workspace_clean"
        ):
            raise SigningProtocolError("guest_evidence_invalid")
        object.__setattr__(self, "guest_evidence", guest)

    def to_mapping(self) -> dict[str, object]:
        return {name: _plain(getattr(self, name)) for name in _RECEIPT_FIELDS}


@dataclass(frozen=True, slots=True)
class SignedJobEnvelope:
    payload: JobPayload
    signature: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _envelope_parts_valid(self.payload, JobPayload, self.signature, "job_envelope_invalid")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical({"payload": self.payload.to_mapping(), "signature": _signature_text(self.signature)})


@dataclass(frozen=True, slots=True)
class SignedReceiptEnvelope:
    payload: ReceiptPayload
    signature: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _envelope_parts_valid(self.payload, ReceiptPayload, self.signature, "receipt_envelope_invalid")

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical({"payload": self.payload.to_mapping(), "signature": _signature_text(self.signature)})


class ReplayLedger(Protocol):
    def accept(
        self,
        *,
        key_id: str,
        job_id: str,
        nonce: str,
        expires_at: int,
        now: int,
        max_clock_skew_seconds: int,
    ) -> bool: ...


def sign_job_envelope(payload: JobPayload, signing_key: SigningKey) -> SignedJobEnvelope:
    _key_object(signing_key, SigningKey, "signing_key_invalid")
    if type(payload) is not JobPayload:
        raise SigningProtocolError("job_payload_invalid")
    return SignedJobEnvelope(payload, signing_key.sign(_JOB_DOMAIN + _canonical(payload.to_mapping())).signature)


def sign_receipt_envelope(payload: ReceiptPayload, signing_key: SigningKey) -> SignedReceiptEnvelope:
    _key_object(signing_key, SigningKey, "signing_key_invalid")
    if type(payload) is not ReceiptPayload:
        raise SigningProtocolError("receipt_payload_invalid")
    return SignedReceiptEnvelope(
        payload, signing_key.sign(_RECEIPT_DOMAIN + _canonical(payload.to_mapping())).signature
    )


def parse_job_envelope(raw: bytes | str) -> SignedJobEnvelope:
    value, encoded = _decode(raw)
    payload, signature = _split_envelope(value)
    _exact_fields(payload, _JOB_FIELDS, "job_fields_invalid")
    signed = SignedJobEnvelope(JobPayload(**payload), signature)
    if signed.canonical_bytes != encoded:
        raise SigningProtocolError("json_not_canonical")
    return signed


def parse_receipt_envelope(raw: bytes | str) -> SignedReceiptEnvelope:
    value, encoded = _decode(raw)
    payload, signature = _split_envelope(value)
    _exact_fields(payload, _RECEIPT_FIELDS, "receipt_fields_invalid")
    try:
        payload["terminal_state"] = TerminalState(payload["terminal_state"])
        payload["artifacts"] = tuple(payload["artifacts"])
        signed = SignedReceiptEnvelope(ReceiptPayload(**payload), signature)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, SigningProtocolError):
            raise
        raise SigningProtocolError("receipt_payload_invalid") from None
    if signed.canonical_bytes != encoded:
        raise SigningProtocolError("json_not_canonical")
    return signed


def verify_job_envelope(
    envelope: SignedJobEnvelope | bytes | str,
    verify_key: VerifyKey,
    *,
    expected: JobVerificationContext,
    now: int,
    replay_ledger: ReplayLedger,
    max_clock_skew_seconds: int = MAX_CLOCK_SKEW_SECONDS,
) -> JobPayload:
    _key_object(verify_key, VerifyKey, "verify_key_invalid")
    if type(expected) is not JobVerificationContext:
        raise SigningProtocolError("expected_binding_invalid")
    _clock(now, max_clock_skew_seconds)
    signed = envelope if type(envelope) is SignedJobEnvelope else parse_job_envelope(envelope)
    job = signed.payload
    _verify(verify_key, _JOB_DOMAIN, job.to_mapping(), signed.signature)
    if tuple(getattr(job, name) for name in expected.__dataclass_fields__) != tuple(
        getattr(expected, name) for name in expected.__dataclass_fields__
    ):
        raise SigningProtocolError("job_binding_mismatch")
    if now < job.issued_at - max_clock_skew_seconds:
        raise SigningProtocolError("job_not_yet_valid")
    if now > job.expires_at + max_clock_skew_seconds:
        raise SigningProtocolError("job_expired")
    try:
        accepted = replay_ledger.accept(
            key_id=job.broker_key_id,
            job_id=job.job_id,
            nonce=job.nonce,
            expires_at=job.expires_at,
            now=now,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
    except Exception:
        raise SigningProtocolError("replay_ledger_failed") from None
    if accepted is not True:
        raise SigningProtocolError("job_replayed")
    return job


def verify_receipt_envelope(
    envelope: SignedReceiptEnvelope | bytes | str,
    verify_key: VerifyKey,
    *,
    expected_job: JobPayload,
    expected_worker_key_id: str,
    observed_output: bytes,
    artifact_observations: Mapping[str, Mapping[str, object]],
    now: int,
    max_clock_skew_seconds: int = MAX_CLOCK_SKEW_SECONDS,
) -> ReceiptPayload:
    _key_object(verify_key, VerifyKey, "verify_key_invalid")
    if type(expected_job) is not JobPayload:
        raise SigningProtocolError("expected_job_invalid")
    if type(observed_output) is not bytes:
        raise SigningProtocolError("output_mismatch")
    _match(expected_worker_key_id, _KEY_ID, "key_id_invalid")
    _clock(now, max_clock_skew_seconds)
    signed = envelope if type(envelope) is SignedReceiptEnvelope else parse_receipt_envelope(envelope)
    receipt = signed.payload
    _verify(verify_key, _RECEIPT_DOMAIN, receipt.to_mapping(), signed.signature)
    if receipt.worker_key_id != expected_worker_key_id:
        raise SigningProtocolError("worker_key_mismatch")
    binding = tuple(
        _plain(getattr(receipt, name)) for name in _JOB_FIELDS - {"issued_at", "expires_at", "schema_version"}
    )
    expected = tuple(
        _plain(getattr(expected_job, name)) for name in _JOB_FIELDS - {"issued_at", "expires_at", "schema_version"}
    )
    if binding != expected:
        raise SigningProtocolError("receipt_binding_mismatch")
    if (
        receipt.started_at < expected_job.issued_at - max_clock_skew_seconds
        or receipt.finished_at > expected_job.expires_at + max_clock_skew_seconds
        or receipt.finished_at > now + max_clock_skew_seconds
        or now > expected_job.expires_at + max_clock_skew_seconds
    ):
        raise SigningProtocolError("receipt_time_invalid")
    use, limits, policy = receipt.resource_use, receipt.limits, receipt.output_policy
    if (
        use["cpu_time_ms"] > limits["cpu_time_ms"]
        or use["wall_time_ms"] > limits["wall_time_ms"]
        or use["peak_memory_mib"] > limits["memory_mib"]
        or use["stdout_bytes"] > policy["max_stdout_bytes"]
        or use["stderr_bytes"] > policy["max_stderr_bytes"]
        or receipt.output_bytes > policy["max_output_bytes"]
        or len(receipt.artifacts) > policy["max_artifacts"]
        or use["artifact_bytes"] > policy["max_artifact_bytes"]
    ):
        raise SigningProtocolError("receipt_limit_exceeded")
    if (
        len(observed_output) != receipt.output_bytes
        or hashlib.sha256(observed_output).hexdigest() != receipt.output_sha256
    ):
        raise SigningProtocolError("output_mismatch")
    _observed_artifacts(receipt.artifacts, artifact_observations)
    return receipt


def _binding(value: JobPayload | ReceiptPayload) -> None:
    _match(value.scope_digest, _DIGEST, "binding_invalid")
    _match(value.capability_id, _IDENTIFIER, "binding_invalid")
    _match(value.runtime_id, _IDENTIFIER, "binding_invalid")
    _match(value.input_sha256, _DIGEST, "binding_invalid")
    _match(value.idempotency_key, _IDEMPOTENCY, "idempotency_key_invalid")
    _match(value.worker_image, _IMAGE, "worker_image_invalid")
    _match(value.worker_version, _REVISION, "worker_version_invalid")
    _match(value.policy_revision, _REVISION, "policy_revision_invalid")


def _limits(value: object) -> Mapping[str, int]:
    result = _exact_map(value, _LIMIT_FIELDS, "limits_invalid")
    bounds = {"wall_time_ms": 300_000, "cpu_time_ms": 300_000, "memory_mib": 4_096}
    if any(type(result[key]) is not int or not 1 <= result[key] <= maximum for key, maximum in bounds.items()):
        raise SigningProtocolError("limits_invalid")
    if result["cpu_time_ms"] > result["wall_time_ms"]:
        raise SigningProtocolError("limits_invalid")
    return result


def _output_policy(value: object) -> Mapping[str, int]:
    result = _exact_map(value, _OUTPUT_FIELDS, "output_policy_invalid")
    bounds = {
        "max_output_bytes": 1_048_576,
        "max_stdout_bytes": 1_048_576,
        "max_stderr_bytes": 1_048_576,
        "max_artifacts": 16,
        "max_artifact_bytes": 16_777_216,
    }
    if any(type(result[key]) is not int or not 0 <= result[key] <= maximum for key, maximum in bounds.items()):
        raise SigningProtocolError("output_policy_invalid")
    if (result["max_artifacts"] == 0) != (result["max_artifact_bytes"] == 0):
        raise SigningProtocolError("output_policy_invalid")
    return result


def _resource_use(value: object) -> Mapping[str, int]:
    result = _exact_map(value, _USE_FIELDS, "resource_use_invalid")
    bounds = {
        "cpu_time_ms": 300_000,
        "wall_time_ms": 300_000,
        "peak_memory_mib": 4_096,
        "stdout_bytes": 1_048_576,
        "stderr_bytes": 1_048_576,
        "artifact_bytes": 16_777_216,
    }
    if any(type(result[key]) is not int or not 0 <= result[key] <= maximum for key, maximum in bounds.items()):
        raise SigningProtocolError("resource_use_invalid")
    return result


def _artifacts(value: object, observed_bytes: int) -> tuple[Mapping[str, object], ...]:
    if type(value) is not tuple or len(value) > 16:
        raise SigningProtocolError("artifact_invalid")
    result: list[Mapping[str, object]] = []
    for item in value:
        frozen = _exact_map(item, {"artifact_ref", "sha256", "size_bytes"}, "artifact_invalid")
        _match(frozen["artifact_ref"], _ARTIFACT_REF, "artifact_invalid")
        _match(frozen["sha256"], _DIGEST, "artifact_invalid")
        if type(frozen["size_bytes"]) is not int or not 1 <= frozen["size_bytes"] <= 16_777_216:
            raise SigningProtocolError("artifact_invalid")
        result.append(frozen)
    refs = [item["artifact_ref"] for item in result]
    if (
        refs != sorted(refs)
        or len(refs) != len(set(refs))
        or sum(item["size_bytes"] for item in result) != observed_bytes
    ):
        raise SigningProtocolError("artifact_invalid")
    return tuple(result)


def _observed_artifacts(expected: tuple[Mapping[str, object], ...], observed: object) -> None:
    if not isinstance(observed, Mapping) or set(observed) != {item["artifact_ref"] for item in expected}:
        raise SigningProtocolError("artifact_mismatch")
    for item in expected:
        actual = _exact_map(observed[item["artifact_ref"]], {"sha256", "size_bytes"}, "artifact_mismatch")
        if actual["sha256"] != item["sha256"] or actual["size_bytes"] != item["size_bytes"]:
            raise SigningProtocolError("artifact_mismatch")


def _decode(raw: bytes | str) -> tuple[dict[str, Any], bytes]:
    try:
        encoded = raw.encode("utf-8", "strict") if isinstance(raw, str) else raw
    except UnicodeEncodeError as exc:
        raise SigningProtocolError("json_utf8_invalid") from exc
    if type(encoded) is not bytes:
        raise SigningProtocolError("envelope_type_invalid")
    if not encoded or len(encoded) > MAX_ENVELOPE_BYTES:
        raise SigningProtocolError("envelope_size_invalid")
    try:
        value = json.loads(encoded.decode("utf-8", "strict"), object_pairs_hook=_unique, parse_constant=_nonfinite)
    except SigningProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise SigningProtocolError("json_invalid") from None
    if not isinstance(value, dict):
        raise SigningProtocolError("envelope_fields_invalid")
    return value, encoded


def _split_envelope(value: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    _exact_fields(value, {"payload", "signature"}, "envelope_fields_invalid")
    if not isinstance(value["payload"], dict):
        raise SigningProtocolError("payload_invalid")
    signature = value["signature"]
    if not isinstance(signature, str) or _SIGNATURE.fullmatch(signature) is None:
        raise SigningProtocolError("signature_encoding_invalid")
    try:
        decoded = base64.b64decode(signature + "==", altchars=b"-_", validate=True)
    except (TypeError, ValueError):
        raise SigningProtocolError("signature_encoding_invalid") from None
    if len(decoded) != 64 or _signature_text(decoded) != signature:
        raise SigningProtocolError("signature_encoding_invalid")
    return value["payload"], decoded


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            _plain(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise SigningProtocolError("canonical_json_invalid") from None


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def _verify(key: VerifyKey, domain: bytes, payload: object, signature: bytes) -> None:
    try:
        key.verify(domain + _canonical(payload), signature)
    except (BadSignatureError, TypeError, ValueError):
        raise SigningProtocolError("signature_invalid") from None


def _exact_map(value: object, fields: set[str], code: str) -> MappingProxyType:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SigningProtocolError(code)
    return MappingProxyType(dict(value))


def _exact_fields(value: Mapping[str, object], fields: set[str], code: str) -> None:
    if set(value) != fields:
        raise SigningProtocolError(code)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SigningProtocolError("json_duplicate_key")
        result[key] = value
    return result


def _nonfinite(_: str) -> None:
    raise SigningProtocolError("json_non_finite")


def _signature_text(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _envelope_parts_valid(payload: object, kind: type, signature: object, code: str) -> None:
    if type(payload) is not kind or type(signature) is not bytes or len(signature) != 64:
        raise SigningProtocolError(code)


def _key_object(value: object, kind: type, code: str) -> None:
    if not isinstance(value, kind):
        raise SigningProtocolError(code)


def _match(value: object, pattern: re.Pattern[str], code: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SigningProtocolError(code)


def _time(value: object, code: str) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise SigningProtocolError(code)


def _clock(now: object, skew: object) -> None:
    _time(now, "clock_invalid")
    if type(skew) is not int or not 0 <= skew <= MAX_CLOCK_SKEW_SECONDS:
        raise SigningProtocolError("clock_skew_invalid")
