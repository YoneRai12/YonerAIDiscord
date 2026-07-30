from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from html import escape
import ipaddress
import re
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiohttp import web

from .callback import IdentityCallbackService
from .http import GuardResult, security_headers


_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
_MAX_BODY_BYTES = 8_192
_READ_TIMEOUT_SECONDS = 5.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_RESULT_TEXT = MappingProxyType(
    {
        "success": "本人確認が完了しました。このページを閉じてDiscordへ戻ってください。",
        "failure": "本人確認を完了できませんでした。Discordから新しい認証URLを発行してください。",
        "busy": "現在本人確認を完了できません。少し待ってから新しい認証URLで再試行してください。",
    }
)


class IdentityWebConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IdentityWebConfig:
    bind_host: str = "127.0.0.1"
    bind_port: int = 8_765
    public_base_url: str = ""
    turnstile_site_key: str = field(default="", repr=False)
    captcha_required: bool = True

    def __post_init__(self) -> None:
        host = self.bind_host.strip().lower()
        if host not in _LOOPBACK_HOSTS:
            raise IdentityWebConfigurationError("identity HTTP listener must bind to loopback")
        try:
            address = ipaddress.ip_address(host) if host != "localhost" else None
        except ValueError as exc:
            raise IdentityWebConfigurationError("identity HTTP listener host is invalid") from exc
        if address is not None and not address.is_loopback:
            raise IdentityWebConfigurationError("identity HTTP listener must bind to loopback")
        if not isinstance(self.bind_port, int) or not 0 <= self.bind_port <= 65_535:
            raise IdentityWebConfigurationError("identity HTTP listener port is invalid")
        parsed = urlparse(self.public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise IdentityWebConfigurationError("identity public URL is invalid")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise IdentityWebConfigurationError("identity public URL contains forbidden parts")
        if parsed.path not in {"", "/"}:
            raise IdentityWebConfigurationError("built-in identity HTTP requires an origin-only public URL")
        if self.captcha_required and not self.turnstile_site_key:
            raise IdentityWebConfigurationError("Turnstile site key is required")
        if len(self.turnstile_site_key) > 2_048:
            raise IdentityWebConfigurationError("Turnstile site key is too long")
        object.__setattr__(self, "bind_host", host)
        object.__setattr__(self, "public_base_url", self.public_base_url.rstrip("/"))

    @property
    def public_origin(self) -> str:
        parsed = urlparse(self.public_base_url)
        return f"{parsed.scheme}://{parsed.netloc}"


class IdentityWebServer:
    """localhost限定の固定本人確認route。access logとproxy headerを使用しない。"""

    def __init__(self, callback: IdentityCallbackService, config: IdentityWebConfig) -> None:
        self._callback = callback
        self.config = config
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def create_application(self) -> web.Application:
        application = web.Application(client_max_size=_MAX_BODY_BYTES)
        application.router.add_get("/v1/identity/verify/{token}", self._get_verify)
        application.router.add_post("/v1/identity/verify/{token}", self._post_verify)
        return application

    async def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("identity HTTP server is already running")
        runner = web.AppRunner(self.create_application(), access_log=None, handle_signals=False)
        try:
            await runner.setup()
            site = web.TCPSite(
                runner,
                host=self.config.bind_host,
                port=self.config.bind_port,
                shutdown_timeout=5.0,
            )
            await site.start()
        except Exception:
            await runner.cleanup()
            raise
        self._runner = runner
        self._site = site

    async def stop(self) -> None:
        runner = self._runner
        self._site = None
        self._runner = None
        if runner is not None:
            await runner.cleanup()

    async def _get_verify(self, request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        peer = _peer_ip(request)
        if not _TOKEN_PATTERN.fullmatch(token):
            return _html_response(404, _RESULT_TEXT["failure"])
        guard: GuardResult = await self._callback.authorize_page(token=token, remote_ip=peer)
        if not guard.allowed:
            return _html_response(guard.status, _RESULT_TEXT["failure"], extra_headers=guard.headers)
        return _page_response(self.config)

    async def _post_verify(self, request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        if not _TOKEN_PATTERN.fullmatch(token):
            return _html_response(400, _RESULT_TEXT["failure"])
        origin = request.headers.get("Origin")
        if origin and origin != self.config.public_origin:
            return _html_response(403, _RESULT_TEXT["failure"])
        content_type = request.content_type.lower()
        if content_type != "application/x-www-form-urlencoded":
            return _html_response(415, _RESULT_TEXT["failure"])
        try:
            async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                raw_body = await request.read()
        except (TimeoutError, web.HTTPRequestEntityTooLarge):
            return _html_response(413, _RESULT_TEXT["failure"])
        if len(raw_body) > _MAX_BODY_BYTES:
            return _html_response(413, _RESULT_TEXT["failure"])
        try:
            decoded = raw_body.decode("utf-8", errors="strict")
            values = parse_qs(decoded, keep_blank_values=True, strict_parsing=False)
        except (UnicodeError, ValueError):
            return _html_response(400, _RESULT_TEXT["failure"])
        if set(values) - {"cf-turnstile-response"}:
            return _html_response(400, _RESULT_TEXT["failure"])
        captcha_values = values.get("cf-turnstile-response", [""])
        if len(captcha_values) != 1 or len(captcha_values[0]) > 4_096:
            return _html_response(400, _RESULT_TEXT["failure"])
        result = await self._callback.complete_member_verification(
            token=token,
            captcha_response=captcha_values[0],
            remote_ip=_peer_ip(request),
        )
        if result.ok:
            return _html_response(200, _RESULT_TEXT["success"])
        if result.status == 429:
            return _html_response(429, _RESULT_TEXT["busy"])
        if result.status >= 500:
            return _html_response(503, _RESULT_TEXT["busy"])
        return _html_response(400, _RESULT_TEXT["failure"])


def _peer_ip(request: web.Request) -> str:
    transport = request.transport
    peer: Any = transport.get_extra_info("peername") if transport is not None else None
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return ""


def _page_response(config: IdentityWebConfig) -> web.Response:
    if config.captcha_required:
        widget = (
            '<div class="cf-turnstile" data-sitekey="'
            + escape(config.turnstile_site_key, quote=True)
            + '" data-action="identity_verify"></div>'
        )
        script = '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>'
    else:
        widget = ""
        script = ""
    body = (
        "<!doctype html><html lang=ja><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Discord本人確認</title>"
        "<style>body{font-family:sans-serif;max-width:36rem;margin:4rem auto;padding:1rem}"
        "button{padding:.8rem 1.2rem}</style></head><body>"
        "<h1>Discord本人確認</h1><p>このURLを発行したDiscordアカウントへ認証roleを付与します。</p>"
        f"<form method=post>{widget}<button type=submit>本人確認を完了</button></form>{script}"
        "</body></html>"
    )
    return web.Response(text=body, status=200, content_type="text/html", headers=dict(security_headers()))


def _html_response(
    status: int,
    message: str,
    *,
    extra_headers: Any | None = None,
) -> web.Response:
    headers = dict(security_headers())
    if extra_headers:
        headers.update({str(key): str(value) for key, value in extra_headers.items()})
    body = (
        "<!doctype html><html lang=ja><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Discord本人確認</title></head><body><p>" + escape(message) + "</p></body></html>"
    )
    return web.Response(text=body, status=status, content_type="text/html", headers=headers)


__all__ = [
    "IdentityWebConfig",
    "IdentityWebConfigurationError",
    "IdentityWebServer",
]
