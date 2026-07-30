from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import secrets
import re
from urllib.parse import quote, urlparse

from .models import (
    ChallengeBinding,
    ChallengeIntent,
    ChallengePurpose,
    ChallengeRecord,
    ClaimedChallenge,
    IdentityPolicy,
    IssuedChallenge,
)
from .ports import ChallengeRepository


class InvalidIdentityConfiguration(ValueError):
    pass


class FeatureDisabledError(RuntimeError):
    pass


def _is_local_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def validate_policy(policy: IdentityPolicy) -> None:
    parsed = urlparse(policy.public_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InvalidIdentityConfiguration("public_base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InvalidIdentityConfiguration("public_base_url must not contain credentials, query or fragment")
    if parsed.scheme != "https" and not _is_local_url(policy.public_base_url):
        raise InvalidIdentityConfiguration("public identity URL must use HTTPS")
    if not policy.captcha_configured and not (
        _is_local_url(policy.public_base_url) and policy.allow_insecure_localhost
    ):
        raise InvalidIdentityConfiguration(
            "captcha is required; only localhost can opt out with explicit development mode"
        )
    for name, value in (
        ("state_ttl_seconds", policy.state_ttl_seconds),
        ("verification_ttl_seconds", policy.verification_ttl_seconds),
        ("get_requests_per_minute", policy.get_requests_per_minute),
        ("post_requests_per_minute", policy.post_requests_per_minute),
        ("claim_lease_seconds", policy.claim_lease_seconds),
    ):
        if value <= 0:
            raise InvalidIdentityConfiguration(f"{name} must be positive")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


class IdentityService:
    def __init__(self, repository: ChallengeRepository, policy: IdentityPolicy) -> None:
        validate_policy(policy)
        self._repository = repository
        self.policy = policy

    @staticmethod
    def digest_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _valid_token(token: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token))

    def issue(
        self,
        purpose: ChallengePurpose,
        binding: ChallengeBinding,
        *,
        now: datetime | None = None,
    ) -> IssuedChallenge:
        if not self.policy.features.enabled(binding.intent):
            raise FeatureDisabledError(f"identity intent is disabled: {binding.intent.value}")
        issued_at = now or datetime.now(UTC)
        _require_aware(issued_at)
        ttl = (
            self.policy.state_ttl_seconds if purpose is ChallengePurpose.STATE else self.policy.verification_ttl_seconds
        )
        # 32 random bytes = 256 bits of entropy. Only its digest crosses into storage.
        token = secrets.token_urlsafe(32)
        expires_at = issued_at + timedelta(seconds=ttl)
        self._repository.purge_expired(issued_at)
        self._repository.revoke_unclaimed(binding, issued_at)
        self._repository.add(
            ChallengeRecord(
                token_digest=self.digest_token(token),
                purpose=purpose,
                binding=binding,
                expires_at=expires_at,
                created_at=issued_at,
            )
        )
        return IssuedChallenge(token, purpose, binding, expires_at)

    def claim(
        self,
        token: str,
        purpose: ChallengePurpose,
        binding: ChallengeBinding,
        *,
        now: datetime | None = None,
    ) -> ClaimedChallenge | None:
        if not self._valid_token(token) or not self.policy.features.enabled(binding.intent):
            return None
        claimed_at = now or datetime.now(UTC)
        _require_aware(claimed_at)
        return self._repository.claim(
            self.digest_token(token),
            purpose,
            binding,
            secrets.token_urlsafe(24),
            claimed_at,
        )

    def claim_for_intent(
        self,
        token: str,
        purpose: ChallengePurpose,
        intent: ChallengeIntent,
        *,
        now: datetime | None = None,
    ) -> ClaimedChallenge | None:
        """URLからguild/userを受け取らず、保存済みbindingを唯一の対象にする。"""

        if not self._valid_token(token) or not self.policy.features.enabled(intent):
            return None
        claimed_at = now or datetime.now(UTC)
        _require_aware(claimed_at)
        return self._repository.claim_for_intent(
            self.digest_token(token),
            purpose,
            intent,
            secrets.token_urlsafe(24),
            claimed_at,
            self.policy.claim_lease_seconds,
        )

    def eligible_for_intent(
        self,
        token: str,
        purpose: ChallengePurpose,
        intent: ChallengeIntent,
        *,
        now: datetime | None = None,
    ) -> bool:
        """偽tokenでcaptcha・共有rate-limitを消費する前の副作用なし検査。"""

        if not self._valid_token(token) or not self.policy.features.enabled(intent):
            return False
        checked_at = now or datetime.now(UTC)
        _require_aware(checked_at)
        return self._repository.eligible(
            self.digest_token(token),
            purpose,
            intent,
            checked_at,
        )

    def verification_url(self, issued: IssuedChallenge) -> str:
        if (
            issued.purpose is not ChallengePurpose.VERIFICATION
            or issued.binding.intent is not ChallengeIntent.VERIFY_MEMBER
            or not self._valid_token(issued.token)
        ):
            raise ValueError("issued challenge is not a member verification challenge")
        # routeとtoken以外を受け取らず、redirect先を外部入力へ委ねない。
        base = self.policy.public_base_url.rstrip("/")
        return f"{base}/v1/identity/verify/{quote(issued.token, safe='')}"

    def finalize(self, claimed: ClaimedChallenge, *, now: datetime | None = None) -> bool:
        finalized_at = now or datetime.now(UTC)
        _require_aware(finalized_at)
        return self._repository.finalize(claimed.claim_id, finalized_at)

    def release(self, claimed: ClaimedChallenge) -> bool:
        return self._repository.release(claimed.claim_id)
