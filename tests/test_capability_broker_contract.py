from __future__ import annotations

from dataclasses import fields

import pytest

from yonerai_discord.capability_broker import (
    ApprovalClass,
    ArtifactDescriptor,
    ArtifactKind,
    CapabilityArtifact,
    CapabilityAuthorizationError,
    CapabilityBinding,
    CapabilityContractError,
    CapabilityKind,
    CapabilityRequest,
    MediaCapabilityInput,
    NetworkPolicy,
    PermissionClass,
    ResourceLimits,
)


def _binding(*, actor_id: int = 10) -> CapabilityBinding:
    return CapabilityBinding(actor_id=actor_id, guild_id=20, conversation_id=30)


def _request(
    capability: CapabilityKind = CapabilityKind.MEDIA_INSPECTION,
    *,
    instruction: str | None = "秘密を含みうる利用者指示",
) -> CapabilityRequest:
    if capability is not CapabilityKind.MEDIA_INSPECTION:
        instruction = None
    return CapabilityRequest(
        request_id="request-1",
        idempotency_key="guild:20:message:40",
        binding=_binding(),
        capability=capability,
        payload=MediaCapabilityInput(
            "https://youtu.be/ABCDEFGHIJK?feature=share",
            instruction,
        ),
    )


def test_typed_request_binds_identity_policy_and_hides_input_from_repr() -> None:
    request = _request()

    rendered = repr(request)
    assert "秘密を含みうる利用者指示" not in rendered
    assert "youtu.be" not in rendered
    assert request.permission is PermissionClass.READ_PUBLIC_MEDIA
    assert request.approval is ApprovalClass.FRESH_INVOCATION
    assert len(request.policy_digest) == 64
    assert len(request.request_digest) == 64
    assert {"command", "shell", "argv", "path", "secret"}.isdisjoint(field.name for field in fields(CapabilityRequest))


def test_subtitle_and_ocr_are_fixed_intents_without_arbitrary_prompt_or_command() -> None:
    assert _request(CapabilityKind.SUBTITLE_EXTRACTION).payload.instruction is None
    assert _request(CapabilityKind.THUMBNAIL_OCR).payload.instruction is None

    with pytest.raises(CapabilityContractError):
        CapabilityRequest(
            request_id="request-2",
            idempotency_key="message:41",
            binding=_binding(),
            capability=CapabilityKind.SUBTITLE_EXTRACTION,
            payload=MediaCapabilityInput(
                "https://www.youtube.com/watch?v=ABCDEFGHIJK",
                "任意の命令",
            ),
        )
    with pytest.raises(TypeError):
        MediaCapabilityInput(  # type: ignore[call-arg]
            source_url="https://www.youtube.com/watch?v=ABCDEFGHIJK",
            command="whoami",
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MediaCapabilityInput("http://www.youtube.com/watch?v=ABCDEFGHIJK", "調査"),
        lambda: MediaCapabilityInput("https://example.com/watch?v=ABCDEFGHIJK", "調査"),
        lambda: NetworkPolicy(("www.youtube.com", "example.com")),
        lambda: ResourceLimits(max_concurrency=2),
        lambda: ResourceLimits(secret_access=True),
        lambda: ResourceLimits(host_mount=True),
        lambda: ResourceLimits(max_output_bytes=32 * 1024 + 1),
        lambda: CapabilityRequest(
            request_id="request-large",
            idempotency_key="message:large",
            binding=_binding(),
            capability=CapabilityKind.MEDIA_INSPECTION,
            payload=MediaCapabilityInput(
                "https://www.youtube.com/watch?v=ABCDEFGHIJK&padding=" + ("a" * 512),
                "調査",
            ),
            resources=ResourceLimits(max_input_bytes=64),
        ),
    ],
)
def test_network_and_resource_contracts_fail_closed(factory) -> None:
    with pytest.raises(CapabilityContractError):
        factory()


def test_artifact_hash_and_owner_binding_are_enforced() -> None:
    text = "検査結果"
    encoded = text.encode()
    import hashlib

    descriptor = ArtifactDescriptor(
        artifact_id="artifact:abc",
        kind=ArtifactKind.INSPECTION_TEXT,
        media_type="text/plain; charset=utf-8",
        size_bytes=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        owner=_binding(),
    )
    artifact = CapabilityArtifact(descriptor, text)

    assert artifact.read_text(binding=_binding()) == text
    assert text not in repr(artifact)
    with pytest.raises(CapabilityAuthorizationError):
        artifact.read_text(binding=_binding(actor_id=11))
    with pytest.raises(CapabilityContractError):
        CapabilityArtifact(descriptor, "改ざん")
