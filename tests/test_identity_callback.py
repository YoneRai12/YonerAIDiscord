from __future__ import annotations

from datetime import UTC, datetime, timedelta

from yonerai_discord.modules.identity import (
    ChallengeBinding,
    ChallengeIntent,
    ChallengePurpose,
    IdentityCallbackService,
    IdentityFeatures,
    IdentityHttpGuard,
    IdentityPolicy,
    IdentityService,
    InMemoryChallengeRepository,
)


NOW = datetime(2026, 7, 21, tzinfo=UTC)


class Captcha:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls = 0

    async def verify(self, response_token: str, remote_ip: str) -> bool:
        self.calls += 1
        return self.allowed


class Granter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.claims = []

    async def grant(self, claimed) -> bool:
        self.claims.append(claimed)
        return self.allowed


def _parts(granter: Granter, *, captcha: bool = True):
    repository = InMemoryChallengeRepository()
    policy = IdentityPolicy(
        public_base_url="https://identity.example.test/base",
        features=IdentityFeatures(member_verification=True),
        captcha_configured=True,
    )
    identity = IdentityService(repository, policy)
    callback = IdentityCallbackService(identity, IdentityHttpGuard(policy, Captcha(captcha)), granter)
    return repository, identity, callback


async def test_success_uses_repository_binding_and_consumes_token() -> None:
    granter = Granter()
    _, identity, callback = _parts(granter)
    binding = ChallengeBinding(123, 456, ChallengeIntent.VERIFY_MEMBER)
    issued = identity.issue(ChallengePurpose.VERIFICATION, binding, now=NOW)
    result = await callback.complete_member_verification(
        token=issued.token,
        captcha_response="opaque-captcha",
        remote_ip="203.0.113.10",
        now=NOW,
    )
    assert result.ok and result.reason == "verified"
    assert len(granter.claims) == 1
    assert granter.claims[0].binding == binding
    assert issued.token not in repr(result)

    reused = await callback.complete_member_verification(
        token=issued.token,
        captcha_response="opaque-captcha-2",
        remote_ip="203.0.113.10",
        now=NOW,
    )
    assert reused.status == 400
    assert len(granter.claims) == 1


async def test_role_failure_releases_claim_for_safe_retry() -> None:
    granter = Granter(allowed=False)
    _, identity, callback = _parts(granter)
    issued = identity.issue(
        ChallengePurpose.VERIFICATION,
        ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER),
        now=NOW,
    )
    failed = await callback.complete_member_verification(
        token=issued.token,
        captcha_response="captcha",
        remote_ip="198.51.100.1",
        now=NOW,
    )
    assert failed.status == 503 and failed.reason == "role_grant_failed"
    granter.allowed = True
    retried = await callback.complete_member_verification(
        token=issued.token,
        captcha_response="captcha-2",
        remote_ip="198.51.100.1",
        now=NOW,
    )
    assert retried.ok


async def test_captcha_failure_never_claims_or_reveals_token() -> None:
    granter = Granter()
    _, identity, callback = _parts(granter, captcha=False)
    issued = identity.issue(
        ChallengePurpose.VERIFICATION,
        ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER),
        now=NOW,
    )
    result = await callback.complete_member_verification(
        token=issued.token,
        captcha_response="rejected-secret",
        remote_ip="192.0.2.1",
        now=NOW,
    )
    assert result.status == 400 and result.reason == "captcha_failed"
    assert granter.claims == []
    assert issued.token not in repr(result)


async def test_unknown_token_is_rejected_before_captcha_and_does_not_consume_real_token() -> None:
    repository = InMemoryChallengeRepository()
    policy = IdentityPolicy(
        public_base_url="https://identity.example.test",
        features=IdentityFeatures(member_verification=True),
        captcha_configured=True,
        post_requests_per_minute=1,
    )
    captcha = Captcha()
    identity = IdentityService(repository, policy)
    callback = IdentityCallbackService(identity, IdentityHttpGuard(policy, captcha), Granter())
    issued = identity.issue(
        ChallengePurpose.VERIFICATION,
        ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER),
        now=NOW,
    )

    fake = await callback.complete_member_verification(
        token="Z" * 43,
        captcha_response="fake-captcha",
        remote_ip="127.0.0.1",
        now=NOW,
    )
    real = await callback.complete_member_verification(
        token=issued.token,
        captcha_response="real-captcha",
        remote_ip="127.0.0.1",
        now=NOW,
    )

    assert fake.status == 400
    assert real.ok
    assert captcha.calls == 1


async def test_token_expiring_during_captcha_is_rechecked_before_claim() -> None:
    repository = InMemoryChallengeRepository()
    policy = IdentityPolicy(
        public_base_url="https://identity.example.test",
        features=IdentityFeatures(member_verification=True),
        captcha_configured=True,
        verification_ttl_seconds=1,
    )
    identity = IdentityService(repository, policy)
    granter = Granter()
    times = iter((NOW, NOW + timedelta(seconds=2)))
    callback = IdentityCallbackService(
        identity,
        IdentityHttpGuard(policy, Captcha()),
        granter,
        clock=lambda: next(times),
    )
    issued = identity.issue(
        ChallengePurpose.VERIFICATION,
        ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER),
        now=NOW,
    )

    result = await callback.complete_member_verification(
        token=issued.token,
        captcha_response="captcha",
        remote_ip="127.0.0.1",
    )

    assert result.status == 400
    assert result.reason == "invalid_or_consumed"
    assert granter.claims == []


def test_verification_url_is_fixed_and_contains_no_binding_or_redirect() -> None:
    granter = Granter()
    _, identity, _ = _parts(granter)
    issued = identity.issue(
        ChallengePurpose.VERIFICATION,
        ChallengeBinding(123456, 654321, ChallengeIntent.VERIFY_MEMBER),
        now=NOW,
    )
    url = identity.verification_url(issued)
    assert url == f"https://identity.example.test/base/v1/identity/verify/{issued.token}"
    assert "123456" not in url and "654321" not in url
    assert "redirect" not in url and "?" not in url
