from __future__ import annotations

import json
import struct

import pytest

from yonerai_discord.modules.media_inspection.domain import MediaInspectionResponseError
from yonerai_discord.modules.media_inspection.hyperv_contract import (
    HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
    HYPERV_MEDIA_IDENTITY_DIGEST,
    MAX_HYPERV_MEDIA_FRAME_BYTES,
    HYPERV_MEDIA_POLICY_REVISION,
    HYPERV_MEDIA_SCHEMA,
    HYPERV_MEDIA_WORKER_VERSION,
    decode_execution_result,
    decode_probe_result,
    encode_inspect_request,
    encode_probe_request,
)


def _response(**extra: object) -> bytes:
    return json.dumps(
        {
            "schema": HYPERV_MEDIA_SCHEMA,
            "worker_version": HYPERV_MEDIA_WORKER_VERSION,
            "policy_revision": HYPERV_MEDIA_POLICY_REVISION,
            "identity_digest": HYPERV_MEDIA_IDENTITY_DIGEST,
            "effective_policy_revision": HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
            "effective_policy_digest": HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
            "cleanup_confirmed": True,
            **extra,
        },
        ensure_ascii=False,
    ).encode()


def _unframe(value: bytes) -> dict[str, object]:
    length = struct.unpack(">I", value[:4])[0]
    assert length == len(value) - 4
    document = json.loads(value[4:])
    assert isinstance(document, dict)
    return document


def test_requests_use_v2_exact_shapes_with_operation_inside_frame() -> None:
    assert _unframe(encode_probe_request()) == {
        "operation": "probe",
        "schema": HYPERV_MEDIA_SCHEMA,
    }
    assert _unframe(
        encode_inspect_request(
            url="https://www.youtube.com/watch?v=ABCDEFGHIJK",
            instruction="内容を説明して",
        )
    ) == {
        "instruction": "内容を説明して",
        "operation": "inspect",
        "schema": HYPERV_MEDIA_SCHEMA,
        "url": "https://www.youtube.com/watch?v=ABCDEFGHIJK",
    }


def test_strict_results_require_exact_identity_cleanup_and_shape() -> None:
    assert decode_probe_result(_response(ready=True)).ready is True
    assert decode_execution_result(_response(status="completed", text="解析結果")).text == "解析結果"

    invalid_documents = (
        _response(status="completed", text="解析結果", cleanup_confirmed=False),
        _response(status="completed", text="解析結果", identity_digest="0" * 64),
        _response(status="completed", text="解析結果", unexpected=True),
    )
    for document in invalid_documents:
        with pytest.raises(MediaInspectionResponseError):
            decode_execution_result(document)


def test_probe_attestation_rejects_missing_mismatch_unknown_nonbool_and_oversize() -> None:
    valid = json.loads(_response(ready=True))
    assert isinstance(valid, dict)
    invalid_documents = [
        {key: value for key, value in valid.items() if key != "effective_policy_digest"},
        valid | {"effective_policy_revision": "replaced"},
        valid | {"effective_policy_digest": "0" * 64},
        valid | {"identity_digest": "0" * 64},
        valid | {"unknown": True},
        valid | {"ready": 1},
    ]
    for document in invalid_documents:
        with pytest.raises(MediaInspectionResponseError):
            decode_probe_result(json.dumps(document).encode())

    with pytest.raises(MediaInspectionResponseError):
        decode_probe_result(b" " * (MAX_HYPERV_MEDIA_FRAME_BYTES + 1))


def test_identity_digest_is_a_fixed_lowercase_sha256() -> None:
    assert len(HYPERV_MEDIA_IDENTITY_DIGEST) == 64
    assert HYPERV_MEDIA_IDENTITY_DIGEST == HYPERV_MEDIA_IDENTITY_DIGEST.casefold()
    assert all(character in "0123456789abcdef" for character in HYPERV_MEDIA_IDENTITY_DIGEST)
