from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from types import MappingProxyType
import threading
import time
import ipaddress
from typing import Mapping

from .models import IdentityPolicy
from .ports import TurnstileVerifier
from .service import validate_policy


_SECURITY_HEADERS = MappingProxyType(
    {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "script-src https://challenges.cloudflare.com; "
            "frame-src https://challenges.cloudflare.com; base-uri 'none'"
        ),
        "Cross-Origin-Opener-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
)


def security_headers() -> Mapping[str, str]:
    return _SECURITY_HEADERS


@dataclass(frozen=True, slots=True)
class RequestMetadata:
    method: str
    remote_ip: str
    rate_limit_key: str | None = None


@dataclass(frozen=True, slots=True)
class GuardResult:
    allowed: bool
    status: int
    headers: Mapping[str, str]
    reason: str


class _IpRateLimiter:
    def __init__(self, get_limit: int, post_limit: int, window_seconds: float = 60.0) -> None:
        self._limits = {"GET": get_limit, "POST": post_limit}
        self._window = window_seconds
        self._entries: dict[str, OrderedDict[str, deque[float]]] = {
            "GET": OrderedDict(),
            "POST": OrderedDict(),
        }
        self._max_peers = 10_000
        self._lock = threading.Lock()

    def allow(self, method: str, remote_ip: str, now: float | None = None) -> tuple[bool, int]:
        normalized = method.upper()
        limit = self._limits.get(normalized, self._limits["POST"])
        current = time.monotonic() if now is None else now
        key = remote_ip or "unknown"
        with self._lock:
            method_entries = self._entries.setdefault(normalized, OrderedDict())
            entries = method_entries.get(key)
            if entries is None:
                if len(method_entries) >= self._max_peers:
                    method_entries.popitem(last=False)
                entries = deque()
                method_entries[key] = entries
            else:
                method_entries.move_to_end(key)
            cutoff = current - self._window
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= limit:
                retry_after = max(1, int(entries[0] + self._window - current) + 1)
                return False, retry_after
            entries.append(current)
            return True, 0


class IdentityHttpGuard:
    """Web framework adapterの手前でrate limit・captcha・headersを統一する。"""

    def __init__(self, policy: IdentityPolicy, turnstile: TurnstileVerifier | None) -> None:
        validate_policy(policy)
        self._policy = policy
        self._turnstile = turnstile
        if policy.captcha_configured and turnstile is None:
            raise ValueError("configured captcha requires a TurnstileVerifier")
        self._limiter = _IpRateLimiter(
            policy.get_requests_per_minute,
            policy.post_requests_per_minute,
        )

    async def authorize(
        self,
        request: RequestMetadata,
        *,
        captcha_response: str = "",
    ) -> GuardResult:
        headers = dict(security_headers())
        if request.method.upper() not in {"GET", "POST"}:
            return GuardResult(False, 405, headers, "method_not_allowed")
        if not request.remote_ip or len(request.remote_ip) > 64:
            return GuardResult(False, 400, headers, "invalid_peer")
        try:
            remote_ip = str(ipaddress.ip_address(request.remote_ip))
        except ValueError:
            return GuardResult(False, 400, headers, "invalid_peer")
        rate_limit_key = request.rate_limit_key
        if rate_limit_key is not None and (
            not isinstance(rate_limit_key, str) or not rate_limit_key or len(rate_limit_key) > 128
        ):
            return GuardResult(False, 400, headers, "invalid_rate_limit_key")
        subject = f"token:{rate_limit_key}" if rate_limit_key is not None else remote_ip
        allowed, retry_after = self._limiter.allow(request.method, subject)
        if not allowed:
            headers["Retry-After"] = str(retry_after)
            return GuardResult(False, 429, headers, "rate_limited")
        if request.method.upper() == "POST" and self._policy.captcha_configured:
            if not captcha_response or self._turnstile is None:
                return GuardResult(False, 400, headers, "captcha_failed")
            try:
                verified = await self._turnstile.verify(captcha_response, remote_ip)
            except Exception:
                # Fail closed. Do not attach exception/body/token to the result or logs.
                verified = False
            if not verified:
                return GuardResult(False, 400, headers, "captcha_failed")
        return GuardResult(True, 200, headers, "ok")
