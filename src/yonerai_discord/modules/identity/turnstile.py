from __future__ import annotations

from collections.abc import Callable
import ipaddress
import json
from typing import Any

import aiohttp


TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class CloudflareTurnstileVerifier:
    """固定Cloudflare endpointだけを利用し、secret/responseを保持・記録しない。"""

    __slots__ = ("_expected_action", "_expected_hostname", "_secret", "_session_factory", "_timeout")

    def __init__(
        self,
        secret: str,
        *,
        expected_hostname: str,
        expected_action: str = "identity_verify",
        timeout_seconds: float = 5.0,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
    ) -> None:
        if not secret or len(secret) > 2_048:
            raise ValueError("Turnstile secret is missing or invalid")
        hostname = expected_hostname.strip().lower().rstrip(".")
        if not hostname or "/" in hostname or len(hostname) > 253:
            raise ValueError("expected hostname is invalid")
        if not expected_action or len(expected_action) > 64:
            raise ValueError("expected action is invalid")
        if not 0.25 <= timeout_seconds <= 15:
            raise ValueError("timeout_seconds is outside the safe range")
        self._secret = secret
        self._expected_hostname = hostname
        self._expected_action = expected_action
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session_factory = session_factory

    def __repr__(self) -> str:
        return "CloudflareTurnstileVerifier(secret='[REDACTED]')"

    async def verify(self, response_token: str, remote_ip: str) -> bool:
        if not response_token or len(response_token) > 2_048:
            return False
        try:
            normalized_ip = str(ipaddress.ip_address(remote_ip))
        except ValueError:
            return False
        form = {
            "secret": self._secret,
            "response": response_token,
            "remoteip": normalized_ip,
        }
        try:
            async with self._session_factory(timeout=self._timeout) as session:
                async with session.post(
                    TURNSTILE_SITEVERIFY_URL,
                    data=form,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        return False
                    if response.content_length is not None and response.content_length > 65_536:
                        return False
                    raw = await response.content.read(65_537)
                    if len(raw) > 65_536:
                        return False
                    payload = json.loads(raw)
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
            return False
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return False
        hostname = str(payload.get("hostname", "")).strip().lower().rstrip(".")
        action = str(payload.get("action", ""))
        return hostname == self._expected_hostname and action == self._expected_action
