from __future__ import annotations

import hashlib
import json

import pytest
from nacl.signing import SigningKey

from yonerai_discord.capability_forge.execution_sandbox_signing import (
    MAX_CLOCK_SKEW_SECONDS,
    MAX_ENVELOPE_BYTES,
    JobPayload,
    JobVerificationContext,
    ReceiptPayload,
    SigningProtocolError,
    TerminalState,
    parse_job_envelope,
    parse_receipt_envelope,
    sign_job_envelope,
    sign_receipt_envelope,
    verify_job_envelope,
    verify_receipt_envelope,
)


NOW = 2_000_000_000
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64
IMAGE = "ghcr.io/yonerai/execution-worker@sha256:" + "e" * 64


class Ledger:
    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls: list[dict[str, object]] = []

    def accept(
        self,
        *,
        key_id: str,
        job_id: str,
        nonce: str,
        expires_at: int,
        now: int,
        max_clock_skew_seconds: int,
    ) -> bool:
        self.calls.append(
            {
                "key_id": key_id,
                "job_id": job_id,
                "nonce": nonce,
                "expires_at": expires_at,
                "now": now,
                "max_clock_skew_seconds": max_clock_skew_seconds,
            }
        )
        return self.accepted


def _key(seed: int = 1) -> SigningKey:
    return SigningKey(bytes([seed]) * 32)


def _limits() -> dict[str, int]:
    return {"wall_time_ms": 20_000, "cpu_time_ms": 10_000, "memory_mib": 512}


def _output_policy() -> dict[str, int]:
    return {
        "max_output_bytes": 4_096,
        "max_stdout_bytes": 2_048,
        "max_stderr_bytes": 2_048,
        "max_artifacts": 2,
        "max_artifact_bytes": 8_192,
    }


def _job(**changes: object) -> JobPayload:
    values: dict[str, object] = {
        "broker_key_id": "broker-2026-08",
        "job_id": "job_1234567890abcdef",
        "nonce": "1" * 64,
        "issued_at": NOW - 5,
        "expires_at": NOW + 120,
        "scope_digest": DIGEST_A,
        "capability_id": "forge.python-pure",
        "runtime_id": "hyperv-disposable:v1",
        "input_sha256": DIGEST_B,
        "limits": _limits(),
        "output_policy": _output_policy(),
        "idempotency_key": "discord:request:1234",
        "worker_image": IMAGE,
        "worker_version": "1.2.3",
        "policy_revision": "2026-08-09.1",
    }
    values.update(changes)
    return JobPayload(**values)


def _expected(job: JobPayload | None = None, **changes: object) -> JobVerificationContext:
    job = job or _job()
    values: dict[str, object] = {
        "broker_key_id": job.broker_key_id,
        "scope_digest": job.scope_digest,
        "capability_id": job.capability_id,
        "runtime_id": job.runtime_id,
        "input_sha256": job.input_sha256,
        "worker_image": job.worker_image,
        "worker_version": job.worker_version,
        "policy_revision": job.policy_revision,
    }
    values.update(changes)
    return JobVerificationContext(**values)


def _receipt(job: JobPayload | None = None, **changes: object) -> ReceiptPayload:
    job = job or _job()
    artifact = {"artifact_ref": "artifact_1234567890abcdef", "sha256": DIGEST_C, "size_bytes": 128}
    values: dict[str, object] = {
        "broker_key_id": job.broker_key_id,
        "worker_key_id": "worker-2026-08",
        "job_id": job.job_id,
        "nonce": job.nonce,
        "scope_digest": job.scope_digest,
        "capability_id": job.capability_id,
        "runtime_id": job.runtime_id,
        "input_sha256": job.input_sha256,
        "limits": job.limits,
        "output_policy": job.output_policy,
        "idempotency_key": job.idempotency_key,
        "worker_image": job.worker_image,
        "worker_version": job.worker_version,
        "policy_revision": job.policy_revision,
        "started_at": NOW,
        "finished_at": NOW + 2,
        "terminal_state": TerminalState.SUCCEEDED,
        "exit_code": 0,
        "resource_use": {
            "cpu_time_ms": 200,
            "wall_time_ms": 300,
            "peak_memory_mib": 40,
            "stdout_bytes": 10,
            "stderr_bytes": 0,
            "artifact_bytes": 128,
        },
        "stdout_sha256": DIGEST_D,
        "stderr_sha256": DIGEST_A,
        "output_sha256": hashlib.sha256(b'{"ok":true}').hexdigest(),
        "output_bytes": len(b'{"ok":true}'),
        "artifacts": (artifact,),
        "guest_evidence": {
            "workspace_clean": True,
            "child_process_count": 0,
            "network_interface_count": 0,
            "network_connection_count": 0,
            "network_route_count": 0,
        },
    }
    values.update(changes)
    return ReceiptPayload(**values)


def _observations(receipt: ReceiptPayload | None = None) -> dict[str, dict[str, object]]:
    receipt = receipt or _receipt()
    return {
        str(item["artifact_ref"]): {"sha256": item["sha256"], "size_bytes": item["size_bytes"]}
        for item in receipt.artifacts
    }


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()


def test_job_round_trip_is_canonical_signed_and_replay_is_delegated() -> None:
    key = _key()
    job = _job()
    ledger = Ledger()
    signed = sign_job_envelope(job, key)

    assert signed.canonical_bytes == _canonical(json.loads(signed.canonical_bytes))
    parsed = parse_job_envelope(signed.canonical_bytes)
    verified = verify_job_envelope(
        parsed,
        key.verify_key,
        expected=_expected(job),
        now=NOW,
        replay_ledger=ledger,
    )

    assert verified == job
    assert ledger.calls == [
        {
            "key_id": job.broker_key_id,
            "job_id": job.job_id,
            "nonce": job.nonce,
            "expires_at": job.expires_at,
            "now": NOW,
            "max_clock_skew_seconds": MAX_CLOCK_SKEW_SECONDS,
        }
    ]
    visible = repr(signed)
    assert job.nonce not in visible
    assert job.idempotency_key not in visible
    assert signed.signature.hex() not in visible


@pytest.mark.parametrize(
    "raw,error",
    (
        (b'{"payload":{},"payload":{},"signature":"x"}', "json_duplicate_key"),
        (b'{"payload":NaN,"signature":"x"}', "json_non_finite"),
        (b"{}" + b" " * MAX_ENVELOPE_BYTES, "envelope_size_invalid"),
    ),
    ids=("duplicate-key", "non-finite", "oversize"),
)
def test_job_parser_rejects_duplicate_non_finite_and_oversize(raw: bytes, error: str) -> None:
    with pytest.raises(SigningProtocolError, match=error):
        parse_job_envelope(raw)


def test_parser_requires_exact_fields_and_exact_canonical_encoding() -> None:
    signed = sign_job_envelope(_job(), _key())
    value = json.loads(signed.canonical_bytes)
    value["unexpected"] = True
    with pytest.raises(SigningProtocolError, match="envelope_fields_invalid"):
        parse_job_envelope(_canonical(value))
    with pytest.raises(SigningProtocolError, match="json_not_canonical"):
        parse_job_envelope(b" " + signed.canonical_bytes)


@pytest.mark.parametrize(
    ("expected_change", "error"),
    (
        ({"broker_key_id": "other-key"}, "job_binding_mismatch"),
        ({"scope_digest": DIGEST_C}, "job_binding_mismatch"),
        ({"capability_id": "forge.other"}, "job_binding_mismatch"),
        ({"runtime_id": "other-runtime:v1"}, "job_binding_mismatch"),
        ({"input_sha256": DIGEST_D}, "job_binding_mismatch"),
        ({"policy_revision": "2026-08-09.2"}, "job_binding_mismatch"),
    ),
)
def test_job_verification_rejects_external_binding_mismatch(expected_change: dict[str, object], error: str) -> None:
    key = _key()
    signed = sign_job_envelope(_job(), key)
    with pytest.raises(SigningProtocolError, match=error):
        verify_job_envelope(
            signed,
            key.verify_key,
            expected=_expected(**expected_change),
            now=NOW,
            replay_ledger=Ledger(),
        )


def test_job_verification_rejects_forgery_wrong_key_expiry_skew_and_replay() -> None:
    key = _key()
    signed = sign_job_envelope(_job(), key)
    raw = bytearray(signed.canonical_bytes)
    raw[raw.index(b"forge.python-pure")] = ord("x")
    with pytest.raises(SigningProtocolError, match="signature_invalid"):
        verify_job_envelope(
            bytes(raw),
            key.verify_key,
            expected=_expected(),
            now=NOW,
            replay_ledger=Ledger(),
        )
    with pytest.raises(SigningProtocolError, match="signature_invalid"):
        verify_job_envelope(
            signed,
            _key(2).verify_key,
            expected=_expected(),
            now=NOW,
            replay_ledger=Ledger(),
        )
    with pytest.raises(SigningProtocolError, match="job_expired"):
        verify_job_envelope(
            signed,
            key.verify_key,
            expected=_expected(),
            now=_job().expires_at + 61,
            replay_ledger=Ledger(),
        )
    future = _job(issued_at=NOW + 61, expires_at=NOW + 120)
    with pytest.raises(SigningProtocolError, match="job_not_yet_valid"):
        verify_job_envelope(
            sign_job_envelope(future, key),
            key.verify_key,
            expected=_expected(future),
            now=NOW,
            replay_ledger=Ledger(),
        )
    with pytest.raises(SigningProtocolError, match="job_replayed"):
        verify_job_envelope(
            signed,
            key.verify_key,
            expected=_expected(),
            now=NOW,
            replay_ledger=Ledger(accepted=False),
        )


def test_job_expiry_skew_boundary_is_delegated_exactly_to_replay_ledger() -> None:
    key = _key()
    job = _job(expires_at=NOW)
    ledger = Ledger()

    assert (
        verify_job_envelope(
            sign_job_envelope(job, key),
            key.verify_key,
            expected=_expected(job),
            now=NOW + 7,
            replay_ledger=ledger,
            max_clock_skew_seconds=7,
        )
        == job
    )
    assert ledger.calls[0]["now"] == NOW + 7
    assert ledger.calls[0]["max_clock_skew_seconds"] == 7


def test_only_injected_pynacl_key_objects_are_accepted() -> None:
    key = _key()
    with pytest.raises(SigningProtocolError, match="signing_key_invalid"):
        sign_job_envelope(_job(), bytes(key))  # type: ignore[arg-type]
    with pytest.raises(SigningProtocolError, match="verify_key_invalid"):
        verify_job_envelope(
            sign_job_envelope(_job(), key),
            bytes(key.verify_key),  # type: ignore[arg-type]
            expected=_expected(),
            now=NOW,
            replay_ledger=Ledger(),
        )


def test_receipt_round_trip_binds_job_resources_artifacts_and_containment_evidence() -> None:
    worker_key = _key(2)
    job = _job()
    receipt = _receipt(job)
    signed = sign_receipt_envelope(receipt, worker_key)

    parsed = parse_receipt_envelope(signed.canonical_bytes)
    verified = verify_receipt_envelope(
        parsed,
        worker_key.verify_key,
        expected_job=job,
        expected_worker_key_id=receipt.worker_key_id,
        observed_output=b'{"ok":true}',
        artifact_observations=_observations(receipt),
        now=NOW + 2,
    )

    assert verified == receipt
    visible = repr(signed)
    assert job.nonce not in visible
    assert job.idempotency_key not in visible
    assert receipt.artifacts[0]["artifact_ref"] not in visible


@pytest.mark.parametrize(
    "changed",
    (
        {"scope_digest": DIGEST_C},
        {"capability_id": "forge.other"},
        {"runtime_id": "other-runtime:v1"},
        {"input_sha256": DIGEST_D},
        {"policy_revision": "2026-08-09.2"},
    ),
)
def test_receipt_rejects_job_binding_swaps(changed: dict[str, object]) -> None:
    key = _key()
    job = _job()
    receipt = _receipt(job, **changed)
    with pytest.raises(SigningProtocolError, match="receipt_binding_mismatch"):
        verify_receipt_envelope(
            sign_receipt_envelope(receipt, key),
            key.verify_key,
            expected_job=job,
            expected_worker_key_id=receipt.worker_key_id,
            observed_output=b'{"ok":true}',
            artifact_observations=_observations(receipt),
            now=NOW + 2,
        )


def test_receipt_rejects_artifact_swap_missing_and_extra_artifacts() -> None:
    key = _key()
    job = _job()
    receipt = _receipt(job)
    signed = sign_receipt_envelope(receipt, key)
    ref = str(receipt.artifacts[0]["artifact_ref"])

    for observations in (
        {ref: {"sha256": DIGEST_D, "size_bytes": 128}},
        {},
        {
            ref: {"sha256": DIGEST_C, "size_bytes": 128},
            "artifact_abcdef1234567890": {"sha256": DIGEST_D, "size_bytes": 1},
        },
    ):
        with pytest.raises(SigningProtocolError, match="artifact_mismatch"):
            verify_receipt_envelope(
                signed,
                key.verify_key,
                expected_job=job,
                expected_worker_key_id=receipt.worker_key_id,
                observed_output=b'{"ok":true}',
                artifact_observations=observations,
                now=NOW + 2,
            )


def test_receipt_rejects_resource_overrun_and_mutable_worker_image() -> None:
    key = _key()
    job = _job()
    overrun = _receipt(
        job,
        resource_use={**_receipt(job).resource_use, "cpu_time_ms": job.limits["cpu_time_ms"] + 1},
    )
    with pytest.raises(SigningProtocolError, match="receipt_limit_exceeded"):
        verify_receipt_envelope(
            sign_receipt_envelope(overrun, key),
            key.verify_key,
            expected_job=job,
            expected_worker_key_id=overrun.worker_key_id,
            observed_output=b'{"ok":true}',
            artifact_observations=_observations(overrun),
            now=NOW + 2,
        )
    with pytest.raises(SigningProtocolError, match="worker_image_invalid"):
        _job(worker_image="ghcr.io/yonerai/execution-worker:latest")


def test_receipt_rejects_worker_key_broker_key_and_output_swaps() -> None:
    broker_key, worker_key = _key(), _key(2)
    job = _job()
    receipt = _receipt(job)
    signed = sign_receipt_envelope(receipt, worker_key)
    common = {
        "expected_job": job,
        "expected_worker_key_id": receipt.worker_key_id,
        "artifact_observations": _observations(receipt),
        "now": NOW + 2,
    }
    with pytest.raises(SigningProtocolError, match="signature_invalid"):
        verify_receipt_envelope(signed, broker_key.verify_key, observed_output=b'{"ok":true}', **common)
    swapped_worker = _receipt(job, worker_key_id="worker-other")
    with pytest.raises(SigningProtocolError, match="worker_key_mismatch"):
        verify_receipt_envelope(
            sign_receipt_envelope(swapped_worker, worker_key),
            worker_key.verify_key,
            observed_output=b'{"ok":true}',
            **common,
        )
    swapped_broker = _receipt(job, broker_key_id="broker-other")
    with pytest.raises(SigningProtocolError, match="receipt_binding_mismatch"):
        verify_receipt_envelope(
            sign_receipt_envelope(swapped_broker, worker_key),
            worker_key.verify_key,
            observed_output=b'{"ok":true}',
            **common,
        )
    with pytest.raises(SigningProtocolError, match="output_mismatch"):
        verify_receipt_envelope(signed, worker_key.verify_key, observed_output=b'{"ok":false}', **common)


def test_worker_receipt_only_contains_guest_observable_evidence() -> None:
    payload = _receipt().to_mapping()
    encoded = json.dumps(payload, sort_keys=True)
    assert set(payload["guest_evidence"]) == {
        "workspace_clean",
        "child_process_count",
        "network_interface_count",
        "network_connection_count",
        "network_route_count",
    }
    assert "worker_terminated" not in encoded
    assert "host" not in encoded
    assert "workspace_destroyed" not in encoded


def test_receipt_parser_rejects_unknown_payload_field_before_crypto() -> None:
    signed = sign_receipt_envelope(_receipt(), _key())
    value = json.loads(signed.canonical_bytes)
    value["payload"]["raw_output"] = "must-not-be-accepted"
    with pytest.raises(SigningProtocolError, match="receipt_fields_invalid"):
        parse_receipt_envelope(_canonical(value))
