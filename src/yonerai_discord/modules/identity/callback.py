from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from .http import GuardResult, IdentityHttpGuard, RequestMetadata, security_headers
from .models import CallbackResult, ChallengeIntent, ChallengePurpose
from .ports import VerificationRoleGranter
from .service import IdentityService


class IdentityCallbackService:
    """Framework非依存の固定callback契約。任意redirectや対象ID入力を持たない。"""

    def __init__(
        self,
        identity: IdentityService,
        http_guard: IdentityHttpGuard,
        role_granter: VerificationRoleGranter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._identity = identity
        self._http_guard = http_guard
        self._role_granter = role_granter
        self._clock = clock or (lambda: datetime.now(UTC))

    async def complete_member_verification(
        self,
        *,
        token: str,
        captcha_response: str,
        remote_ip: str,
        now: datetime | None = None,
    ) -> CallbackResult:
        checked_at = now or self._clock()
        if not self._identity.eligible_for_intent(
            token,
            ChallengePurpose.VERIFICATION,
            ChallengeIntent.VERIFY_MEMBER,
            now=checked_at,
        ):
            return CallbackResult(False, 400, "invalid_or_consumed")
        token_key = self._identity.digest_token(token)
        guard = await self._http_guard.authorize(
            RequestMetadata(
                method="POST",
                remote_ip=remote_ip,
                rate_limit_key=token_key,
            ),
            captcha_response=captcha_response,
        )
        if not guard.allowed:
            return CallbackResult(False, guard.status, guard.reason)

        claim_at = now or self._clock()
        claimed = self._identity.claim_for_intent(
            token,
            ChallengePurpose.VERIFICATION,
            ChallengeIntent.VERIFY_MEMBER,
            now=claim_at,
        )
        if claimed is None:
            # 存在・期限切れ・使用済みを区別せず、tokenのoracle化を避ける。
            return CallbackResult(False, 400, "invalid_or_consumed")

        try:
            granted = await self._role_granter.grant(claimed)
        except Exception:
            granted = False
        if not granted:
            self._identity.release(claimed)
            return CallbackResult(False, 503, "role_grant_failed")

        finalized_at = now or self._clock()
        if self._identity.finalize(claimed, now=finalized_at):
            return CallbackResult(True, 200, "verified")

        # Role付与は冪等なので、永続化失敗時は同じchallengeを再試行可能にする。
        self._identity.release(claimed)
        return CallbackResult(False, 503, "finalization_failed")

    async def authorize_page(self, *, token: str, remote_ip: str) -> GuardResult:
        """GET pageにもPOSTと同じIP rate-limitとpeer検証を適用する。"""

        if not self._identity.eligible_for_intent(
            token,
            ChallengePurpose.VERIFICATION,
            ChallengeIntent.VERIFY_MEMBER,
            now=self._clock(),
        ):
            return GuardResult(False, 400, security_headers(), "invalid_or_consumed")

        return await self._http_guard.authorize(
            RequestMetadata(
                method="GET",
                remote_ip=remote_ip,
                rate_limit_key=self._identity.digest_token(token),
            )
        )
