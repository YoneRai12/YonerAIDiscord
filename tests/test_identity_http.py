from __future__ import annotations

import asyncio

from yonerai_discord.modules.identity import (
    IdentityFeatures,
    IdentityHttpGuard,
    IdentityPolicy,
    RequestMetadata,
    security_headers,
)


def policy(**overrides) -> IdentityPolicy:
    values = {
        "public_base_url": "https://identity.example.test",
        "features": IdentityFeatures(member_verification=True),
        "captcha_configured": True,
        "get_requests_per_minute": 2,
        "post_requests_per_minute": 1,
    }
    values.update(overrides)
    return IdentityPolicy(**values)


class Captcha:
    def __init__(self, result: bool = True, raises: bool = False) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    async def verify(self, response_token: str, remote_ip: str) -> bool:
        self.calls.append((response_token, remote_ip))
        if self.raises:
            raise RuntimeError("upstream body containing a secret must not escape")
        return self.result


def test_security_headers_are_immutable_and_defensive() -> None:
    headers = security_headers()
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Frame-Options"] == "DENY"
    assert "challenges.cloudflare.com" in headers["Content-Security-Policy"]
    try:
        headers["unsafe"] = "value"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("security headers must be immutable")


def test_post_requires_valid_captcha_and_never_returns_token_or_body() -> None:
    async def scenario() -> None:
        captcha = Captcha(result=False)
        guard = IdentityHttpGuard(policy(post_requests_per_minute=5), captcha)
        secret_body = "sensitive-turnstile-response"
        result = await guard.authorize(
            RequestMetadata("POST", "203.0.113.4"),
            captcha_response=secret_body,
        )
        assert result.status == 400
        assert result.reason == "captcha_failed"
        assert secret_body not in repr(result)
        assert captcha.calls == [(secret_body, "203.0.113.4")]

    asyncio.run(scenario())


def test_turnstile_exception_fails_closed_without_exception_details() -> None:
    async def scenario() -> None:
        guard = IdentityHttpGuard(policy(), Captcha(raises=True))
        result = await guard.authorize(RequestMetadata("POST", "203.0.113.5"), captcha_response="secret")
        assert not result.allowed
        assert result.reason == "captcha_failed"
        assert "upstream" not in repr(result)

    asyncio.run(scenario())


def test_get_and_post_have_separate_ip_rate_limits() -> None:
    async def scenario() -> None:
        guard = IdentityHttpGuard(policy(), Captcha())
        ip = "198.51.100.2"
        assert (await guard.authorize(RequestMetadata("GET", ip))).allowed
        assert (await guard.authorize(RequestMetadata("GET", ip))).allowed
        get_limited = await guard.authorize(RequestMetadata("GET", ip))
        assert get_limited.status == 429
        assert int(get_limited.headers["Retry-After"]) > 0

        assert (await guard.authorize(RequestMetadata("POST", ip), captcha_response="valid")).allowed
        post_limited = await guard.authorize(RequestMetadata("POST", ip), captcha_response="valid")
        assert post_limited.status == 429

        # Another peer has an independent allowance.
        assert (await guard.authorize(RequestMetadata("GET", "198.51.100.3"))).allowed

        # Reverse proxyでpeerが同じでも、token digestごとに独立したbucketを使える。
        shared_peer = "127.0.0.1"
        assert (
            await guard.authorize(
                RequestMetadata("POST", shared_peer, rate_limit_key="token-a"),
                captcha_response="valid",
            )
        ).allowed
        assert (
            await guard.authorize(
                RequestMetadata("POST", shared_peer, rate_limit_key="token-b"),
                captcha_response="valid",
            )
        ).allowed

    asyncio.run(scenario())


def test_local_development_can_run_without_turnstile_verifier() -> None:
    async def scenario() -> None:
        guard = IdentityHttpGuard(
            policy(
                public_base_url="http://localhost:8080",
                captcha_configured=False,
                allow_insecure_localhost=True,
            ),
            None,
        )
        result = await guard.authorize(RequestMetadata("POST", "127.0.0.1"))
        assert result.allowed

    asyncio.run(scenario())


def test_get_bucket_churn_cannot_evict_a_post_token_bucket() -> None:
    async def scenario() -> None:
        guard = IdentityHttpGuard(policy(), Captcha())
        guard._limiter._max_peers = 2
        peer = "127.0.0.1"
        request = RequestMetadata("POST", peer, rate_limit_key="real-token")
        assert (await guard.authorize(request, captcha_response="valid")).allowed
        assert (await guard.authorize(request, captcha_response="valid")).status == 429

        for index in range(3):
            assert (await guard.authorize(RequestMetadata("GET", peer, rate_limit_key=f"fake-{index}"))).allowed

        assert (await guard.authorize(request, captcha_response="valid")).status == 429

    asyncio.run(scenario())
