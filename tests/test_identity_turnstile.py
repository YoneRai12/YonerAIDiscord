from __future__ import annotations

import json

from yonerai_discord.modules.identity import (
    CloudflareTurnstileVerifier,
    IdentityRuntimeConfig,
    TURNSTILE_SITEVERIFY_URL,
)


class Content:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    async def read(self, size: int) -> bytes:
        return self.raw[:size]


class Response:
    def __init__(self, payload: object, *, status: int = 200, content_length: int | None = None) -> None:
        raw = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
        self.status = status
        self.content_length = len(raw) if content_length is None else content_length
        self.content = Content(raw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class Session:
    def __init__(self, response: Response, calls: list[tuple]) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def post(self, url: str, *, data: dict[str, str], allow_redirects: bool):
        self.calls.append((url, data, allow_redirects))
        return self.response


class Factory:
    def __init__(self, payload: object, **response_options) -> None:
        self.response = Response(payload, **response_options)
        self.calls: list[tuple] = []

    def __call__(self, **kwargs):
        return Session(self.response, self.calls)


async def test_turnstile_uses_only_fixed_endpoint_and_binds_host_action() -> None:
    factory = Factory({"success": True, "hostname": "identity.example.test", "action": "identity_verify"})
    verifier = CloudflareTurnstileVerifier(
        "top-secret",
        expected_hostname="identity.example.test",
        session_factory=factory,
    )
    assert await verifier.verify("opaque-response", "203.0.113.1")
    assert "top-secret" not in repr(verifier)
    assert factory.calls == [
        (
            TURNSTILE_SITEVERIFY_URL,
            {
                "secret": "top-secret",
                "response": "opaque-response",
                "remoteip": "203.0.113.1",
            },
            False,
        )
    ]


async def test_turnstile_fails_closed_on_binding_mismatch_invalid_peer_and_large_body() -> None:
    mismatch = Factory({"success": True, "hostname": "evil.example", "action": "identity_verify"})
    verifier = CloudflareTurnstileVerifier(
        "secret",
        expected_hostname="identity.example.test",
        session_factory=mismatch,
    )
    assert not await verifier.verify("opaque", "198.51.100.2")
    assert not await verifier.verify("opaque", "not-an-ip")
    assert len(mismatch.calls) == 1

    oversized = Factory(b"x" * 65_537, content_length=65_537)
    verifier = CloudflareTurnstileVerifier(
        "secret",
        expected_hostname="identity.example.test",
        session_factory=oversized,
    )
    assert not await verifier.verify("opaque", "198.51.100.2")


def test_runtime_configuration_is_off_by_default_and_redacts_secret() -> None:
    config = IdentityRuntimeConfig.load(object(), {})
    assert not config.enabled and not config.callback_configured
    configured = IdentityRuntimeConfig.load(
        object(),
        {
            "IDENTITY_ENABLED": "true",
            "IDENTITY_PUBLIC_BASE_URL": "https://identity.example.test",
            "IDENTITY_TURNSTILE_SECRET": "secret-value",
        },
    )
    assert configured.enabled and configured.callback_configured
    assert "secret-value" not in repr(configured)
