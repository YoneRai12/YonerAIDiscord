from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ChallengeIntent(StrEnum):
    VERIFY_MEMBER = "verify_member"
    LINK_ACCOUNT = "link_account"


class ChallengePurpose(StrEnum):
    STATE = "state"
    VERIFICATION = "verification"


@dataclass(frozen=True, slots=True)
class ChallengeBinding:
    guild_id: int
    user_id: int
    intent: ChallengeIntent

    def __post_init__(self) -> None:
        if not 0 < self.guild_id < 2**63 or not 0 < self.user_id < 2**63:
            raise ValueError("guild_id and user_id must be positive")


@dataclass(frozen=True, slots=True)
class ChallengeRecord:
    token_digest: str
    purpose: ChallengePurpose
    binding: ChallengeBinding
    expires_at: datetime
    created_at: datetime
    claim_id: str | None = None
    claimed_at: datetime | None = None
    claim_expires_at: datetime | None = None
    finalized_at: datetime | None = None


@dataclass(frozen=True, slots=True, repr=False)
class IssuedChallenge:
    token: str = field(repr=False)
    purpose: ChallengePurpose
    binding: ChallengeBinding
    expires_at: datetime

    def __repr__(self) -> str:
        return (
            "IssuedChallenge(token='[REDACTED]', "
            f"purpose={self.purpose!r}, binding={self.binding!r}, expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, slots=True)
class ClaimedChallenge:
    claim_id: str
    purpose: ChallengePurpose
    binding: ChallengeBinding
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GuildVerificationConfig:
    guild_id: int
    verified_role_id: int
    enabled: bool
    updated_by: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if any(not 0 < value < 2**63 for value in (self.guild_id, self.verified_role_id, self.updated_by)):
            raise ValueError("guild, role and actor IDs must be positive")


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """Web adapterへ返す固定結果。Discord IDやtokenは含めない。"""

    ok: bool
    status: int
    reason: str


@dataclass(frozen=True, slots=True)
class IdentityFeatures:
    member_verification: bool = False
    account_linking: bool = False

    def enabled(self, intent: ChallengeIntent) -> bool:
        if intent is ChallengeIntent.VERIFY_MEMBER:
            return self.member_verification
        if intent is ChallengeIntent.LINK_ACCOUNT:
            return self.account_linking
        return False


@dataclass(frozen=True, slots=True)
class IdentityPolicy:
    public_base_url: str
    features: IdentityFeatures = IdentityFeatures()
    captcha_configured: bool = False
    allow_insecure_localhost: bool = False
    state_ttl_seconds: int = 300
    verification_ttl_seconds: int = 900
    get_requests_per_minute: int = 30
    post_requests_per_minute: int = 10
    claim_lease_seconds: int = 120
