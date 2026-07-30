from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from yonerai_discord.modules.identity import (
    ChallengeBinding,
    ChallengeIntent,
    ChallengePurpose,
    FeatureDisabledError,
    IdentityFeatures,
    IdentityPolicy,
    IdentityService,
    InMemoryChallengeRepository,
    InvalidIdentityConfiguration,
)


NOW = datetime(2026, 7, 20, tzinfo=UTC)


def policy(**overrides) -> IdentityPolicy:
    values = {
        "public_base_url": "https://identity.example.test",
        "features": IdentityFeatures(member_verification=True, account_linking=True),
        "captcha_configured": True,
    }
    values.update(overrides)
    return IdentityPolicy(**values)


def test_public_url_requires_captcha() -> None:
    with pytest.raises(InvalidIdentityConfiguration, match="captcha is required"):
        IdentityService(
            InMemoryChallengeRepository(),
            policy(captcha_configured=False),
        )


def test_localhost_requires_explicit_development_opt_out() -> None:
    with pytest.raises(InvalidIdentityConfiguration):
        IdentityService(
            InMemoryChallengeRepository(),
            policy(public_base_url="http://localhost:8080", captcha_configured=False),
        )
    IdentityService(
        InMemoryChallengeRepository(),
        policy(
            public_base_url="http://127.0.0.1:8080",
            captcha_configured=False,
            allow_insecure_localhost=True,
        ),
    )


def test_feature_flags_are_per_intent() -> None:
    service = IdentityService(
        InMemoryChallengeRepository(),
        policy(features=IdentityFeatures(member_verification=True, account_linking=False)),
    )
    service.issue(
        ChallengePurpose.VERIFICATION,
        ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER),
        now=NOW,
    )
    with pytest.raises(FeatureDisabledError):
        service.issue(
            ChallengePurpose.STATE,
            ChallengeBinding(1, 2, ChallengeIntent.LINK_ACCOUNT),
            now=NOW,
        )


def test_raw_token_is_high_entropy_redacted_and_never_stored() -> None:
    repository = InMemoryChallengeRepository()
    service = IdentityService(repository, policy())
    issued = service.issue(
        ChallengePurpose.VERIFICATION,
        ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER),
        now=NOW,
    )
    assert len(issued.token) >= 43
    assert issued.token not in repr(issued)
    assert "[REDACTED]" in repr(issued)
    record = repository.snapshot()[0]
    assert issued.token != record.token_digest
    assert record.token_digest == service.digest_token(issued.token)
    assert len(record.token_digest) == 64


def test_claim_is_bound_to_user_guild_intent_and_purpose() -> None:
    repository = InMemoryChallengeRepository()
    service = IdentityService(repository, policy())
    binding = ChallengeBinding(10, 20, ChallengeIntent.LINK_ACCOUNT)
    issued = service.issue(ChallengePurpose.STATE, binding, now=NOW)

    assert (
        service.claim(
            issued.token,
            ChallengePurpose.STATE,
            ChallengeBinding(10, 21, ChallengeIntent.LINK_ACCOUNT),
            now=NOW,
        )
        is None
    )
    assert (
        service.claim(
            issued.token,
            ChallengePurpose.STATE,
            ChallengeBinding(11, 20, ChallengeIntent.LINK_ACCOUNT),
            now=NOW,
        )
        is None
    )
    assert (
        service.claim(
            issued.token,
            ChallengePurpose.STATE,
            ChallengeBinding(10, 20, ChallengeIntent.VERIFY_MEMBER),
            now=NOW,
        )
        is None
    )
    assert service.claim(issued.token, ChallengePurpose.VERIFICATION, binding, now=NOW) is None
    assert service.claim(issued.token, ChallengePurpose.STATE, binding, now=NOW) is not None


def test_expired_challenge_cannot_be_claimed_and_is_purged() -> None:
    repository = InMemoryChallengeRepository()
    service = IdentityService(repository, policy(verification_ttl_seconds=10))
    binding = ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER)
    issued = service.issue(ChallengePurpose.VERIFICATION, binding, now=NOW)
    assert (
        service.claim(
            issued.token,
            ChallengePurpose.VERIFICATION,
            binding,
            now=NOW + timedelta(seconds=10),
        )
        is None
    )
    assert repository.purge_expired(NOW + timedelta(seconds=10)) == 1


def test_atomic_claim_allows_exactly_one_competitor() -> None:
    repository = InMemoryChallengeRepository()
    service = IdentityService(repository, policy())
    binding = ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER)
    issued = service.issue(ChallengePurpose.VERIFICATION, binding, now=NOW)

    def attempt():
        return service.claim(issued.token, ChallengePurpose.VERIFICATION, binding, now=NOW)

    with ThreadPoolExecutor(max_workers=12) as executor:
        claims = list(executor.map(lambda _: attempt(), range(40)))
    assert sum(claim is not None for claim in claims) == 1


def test_release_allows_retry_finalize_prevents_reuse() -> None:
    repository = InMemoryChallengeRepository()
    service = IdentityService(repository, policy())
    binding = ChallengeBinding(1, 2, ChallengeIntent.VERIFY_MEMBER)
    issued = service.issue(ChallengePurpose.VERIFICATION, binding, now=NOW)
    first = service.claim(issued.token, ChallengePurpose.VERIFICATION, binding, now=NOW)
    assert first is not None and service.release(first)
    second = service.claim(issued.token, ChallengePurpose.VERIFICATION, binding, now=NOW)
    assert second is not None and service.finalize(second, now=NOW)
    assert not service.finalize(second, now=NOW)
    assert service.claim(issued.token, ChallengePurpose.VERIFICATION, binding, now=NOW) is None
