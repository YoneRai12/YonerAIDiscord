from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import (
    ChallengeBinding,
    ChallengeIntent,
    ChallengeRecord,
    ClaimedChallenge,
    ChallengePurpose,
)


class ChallengeRepository(Protocol):
    """永続化adapterが満たす、競合安全なchallenge操作。"""

    def add(self, record: ChallengeRecord) -> None: ...

    def eligible(
        self,
        token_digest: str,
        purpose: ChallengePurpose,
        intent: ChallengeIntent,
        now: datetime,
    ) -> bool:
        """未完了・期限内・未claimのtokenだけを副作用なしで確認する。"""
        ...

    def claim(
        self,
        token_digest: str,
        purpose: ChallengePurpose,
        binding: ChallengeBinding,
        claim_id: str,
        now: datetime,
    ) -> ClaimedChallenge | None:
        """未使用かつ未claimなら一度だけclaimし、binding不一致ならNone。"""
        ...

    def claim_for_intent(
        self,
        token_digest: str,
        purpose: ChallengePurpose,
        intent: ChallengeIntent,
        claim_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedChallenge | None:
        """URL token自身に保存済みbindingを結び付け、外部入力のIDを信頼せずclaimする。"""
        ...

    def finalize(self, claim_id: str, now: datetime) -> bool:
        """claim済みchallengeを一度だけ完了状態へ遷移する。"""
        ...

    def release(self, claim_id: str) -> bool:
        """外部処理失敗時に、未finalizeのclaimだけを解放する。"""
        ...

    def purge_expired(self, now: datetime) -> int: ...

    def revoke_unclaimed(self, binding: ChallengeBinding, now: datetime) -> int:
        """同じ用途の未claim URLを失効させ、常に最新の1本だけを残す。"""
        ...


class TurnstileVerifier(Protocol):
    async def verify(self, response_token: str, remote_ip: str) -> bool:
        """秘密値やresponse本文を記録せずCloudflare応答を検証する。"""
        ...


class VerificationRoleGranter(Protocol):
    async def grant(self, claimed: ClaimedChallenge) -> bool:
        """claimに保存されたguild/userだけを対象にroleを付与する。"""
        ...
