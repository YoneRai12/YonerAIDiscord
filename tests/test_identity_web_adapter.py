from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer
import pytest

from yonerai_discord.modules.identity.http import GuardResult, security_headers
from yonerai_discord.modules.identity.models import CallbackResult
from yonerai_discord.modules.identity.web_adapter import (
    IdentityWebConfig,
    IdentityWebConfigurationError,
    IdentityWebServer,
)


TOKEN = "A" * 43


class FakeCallback:
    def __init__(self) -> None:
        self.page_peers: list[str] = []
        self.completions: list[tuple[str, str, str]] = []

    async def authorize_page(self, *, token: str, remote_ip: str) -> GuardResult:
        assert token == TOKEN
        self.page_peers.append(remote_ip)
        return GuardResult(True, 200, security_headers(), "ok")

    async def complete_member_verification(
        self,
        *,
        token: str,
        captcha_response: str,
        remote_ip: str,
        now: object | None = None,
    ) -> CallbackResult:
        del now
        self.completions.append((token, captcha_response, remote_ip))
        return CallbackResult(True, 200, "verified")


def config(**changes: object) -> IdentityWebConfig:
    values = {
        "bind_host": "127.0.0.1",
        "bind_port": 0,
        "public_base_url": "https://verify.example.test",
        "turnstile_site_key": "site-key",
        "captcha_required": True,
    }
    values.update(changes)
    return IdentityWebConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "verify.example.test"])
def test_listener_rejects_non_loopback_bind(host: str) -> None:
    with pytest.raises(IdentityWebConfigurationError):
        config(bind_host=host)


def test_built_in_server_requires_origin_only_public_url_and_site_key() -> None:
    with pytest.raises(IdentityWebConfigurationError):
        config(public_base_url="https://verify.example.test/base")
    with pytest.raises(IdentityWebConfigurationError):
        config(turnstile_site_key="")


async def test_get_page_uses_socket_peer_not_forwarded_header_and_escapes_site_key() -> None:
    callback = FakeCallback()
    server = IdentityWebServer(
        callback,  # type: ignore[arg-type]
        config(turnstile_site_key='"><script>alert(1)</script>'),
    )
    client = TestClient(TestServer(server.create_application()))
    await client.start_server()
    try:
        response = await client.get(
            f"/v1/identity/verify/{TOKEN}",
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        body = await response.text()
    finally:
        await client.close()

    assert response.status == 200
    assert callback.page_peers and callback.page_peers[0] in {"127.0.0.1", "::1"}
    assert callback.page_peers[0] != "8.8.8.8"
    assert "<script>alert(1)</script>" not in body
    assert "&quot;&gt;&lt;script&gt;" in body
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "script-src https://challenges.cloudflare.com; "
        "frame-src https://challenges.cloudflare.com; base-uri 'none'"
    )


async def test_post_accepts_only_same_public_origin_and_fixed_form_field() -> None:
    callback = FakeCallback()
    server = IdentityWebServer(callback, config())  # type: ignore[arg-type]
    client = TestClient(TestServer(server.create_application()))
    await client.start_server()
    try:
        rejected = await client.post(
            f"/v1/identity/verify/{TOKEN}",
            data={"cf-turnstile-response": "captcha-token"},
            headers={"Origin": "https://attacker.example"},
        )
        accepted = await client.post(
            f"/v1/identity/verify/{TOKEN}",
            data={"cf-turnstile-response": "captcha-token"},
            headers={
                "Origin": "https://verify.example.test",
                "X-Forwarded-For": "8.8.8.8",
            },
        )
    finally:
        await client.close()

    assert rejected.status == 403
    assert accepted.status == 200
    assert len(callback.completions) == 1
    token, captcha, peer = callback.completions[0]
    assert token == TOKEN
    assert captcha == "captcha-token"
    assert peer in {"127.0.0.1", "::1"}


async def test_post_rejects_json_unknown_fields_and_invalid_token_without_callback() -> None:
    callback = FakeCallback()
    server = IdentityWebServer(callback, config())  # type: ignore[arg-type]
    client = TestClient(TestServer(server.create_application()))
    await client.start_server()
    try:
        json_response = await client.post(
            f"/v1/identity/verify/{TOKEN}",
            json={"cf-turnstile-response": "captcha-token"},
        )
        extra_response = await client.post(
            f"/v1/identity/verify/{TOKEN}",
            data={"cf-turnstile-response": "captcha-token", "redirect": "https://example.test"},
        )
        invalid_response = await client.post(
            "/v1/identity/verify/not-a-token",
            data={"cf-turnstile-response": "captcha-token"},
        )
    finally:
        await client.close()

    assert (json_response.status, extra_response.status, invalid_response.status) == (415, 400, 400)
    assert callback.completions == []
