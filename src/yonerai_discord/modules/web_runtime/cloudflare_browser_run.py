from __future__ import annotations

import asyncio
import importlib.util
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Protocol
from urllib.parse import quote, urlsplit

from yonerai_discord.browser_sandbox.models import (
    BrowserAction,
    BrowserOutput,
    BrowserOutputKind,
    BrowserSessionRequest,
    BrowserSessionResult,
    Click,
    CssSelector,
    ExtractText,
    Navigate,
    Screenshot,
    ScreenshotFormat,
    Scroll,
    SelectOption,
    TypeText,
    Wait,
)
from yonerai_discord.browser_sandbox.policy import BrowserNetworkGuard, BrowserSandboxPolicy

from .browser import (
    BrowserPlanSegment,
    BrowserRunCleanupUnconfirmedError,
    BrowserRunContractError,
    BrowserRunPlan,
    BrowserRunScope,
    BrowserRunTransientError,
)
from .search import WebBackendAvailability, WebBackendBlockerCode


_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_CDP_ENDPOINT_TEMPLATE = "wss://api.cloudflare.com/client/v4/accounts/{account_id}/browser-rendering/devtools/browser"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
_MAX_TEXT_BYTES = 512 * 1024
_MAX_QUERY_CHARS = 200
_RESOURCE_CLEANUP_TIMEOUT_SECONDS = 2.0
_YOUTUBE_SCREENSHOT_ORIGIN = "https://www.youtube.com"

BROWSER_RUN_POLICY_BOUNDARY = (
    "Cloudflare Browser Run with application-level Playwright request filtering; "
    "this does not prove strict Sandbox containment or network=0"
)


class BrowserRunConnection(Protocol):
    async def new_context(self, *, accept_downloads: bool, service_workers: str) -> object: ...

    async def close(self) -> None: ...


class BrowserRunConnector(Protocol):
    async def connect(self, *, endpoint: str, headers: Mapping[str, str]) -> BrowserRunConnection: ...


@dataclass(frozen=True, slots=True)
class CloudflareBrowserRunConfig:
    account_id: str = field(repr=False)
    api_token: str = field(repr=False)
    enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.account_id, str) or (self.account_id and _ACCOUNT_ID.fullmatch(self.account_id) is None):
            raise ValueError("Cloudflare account identifier is invalid")
        if (
            not isinstance(self.api_token, str)
            or (self.api_token and self.api_token != self.api_token.strip())
            or len(self.api_token) > 2_048
            or any(ord(character) < 32 or ord(character) == 127 for character in self.api_token)
        ):
            raise ValueError("Cloudflare API credential is invalid")
        if type(self.enabled) is not bool:
            raise TypeError("enabled must be a boolean")

    @property
    def endpoint(self) -> str:
        if not self.account_id:
            raise ValueError("Cloudflare account identifier is not configured")
        return _CDP_ENDPOINT_TEMPLATE.format(account_id=self.account_id)


class PlaywrightCdpConnector:
    """Lazy real connector. It exposes no CDP or Playwright object to action callers."""

    async def connect(self, *, endpoint: str, headers: Mapping[str, str]) -> BrowserRunConnection:
        from playwright.async_api import async_playwright

        manager = await async_playwright().start()
        try:
            browser = await manager.chromium.connect_over_cdp(endpoint, headers=dict(headers))
        except BaseException:
            await manager.stop()
            raise
        return _ManagedPlaywrightConnection(browser=browser, manager=manager)


class _ManagedPlaywrightConnection:
    def __init__(self, *, browser: object, manager: object) -> None:
        self._browser = browser
        self._manager = manager

    async def new_context(self, *, accept_downloads: bool, service_workers: str) -> object:
        return await self._browser.new_context(
            accept_downloads=accept_downloads,
            service_workers=service_workers,
        )

    async def close(self) -> None:
        failure = False
        try:
            await self._browser.close()
        except BaseException:
            failure = True
        try:
            await self._manager.stop()
        except BaseException:
            failure = True
        if failure:
            raise RuntimeError("remote browser connection cleanup failed")


DependencyProbe = Callable[[], bool]


def _playwright_dependency_available() -> bool:
    try:
        return importlib.util.find_spec("playwright.async_api") is not None
    except ModuleNotFoundError:
        return False


class CloudflareBrowserRunSessionFactory:
    """Creates one fresh remote Browser Run context per bounded segment."""

    def __init__(
        self,
        *,
        config: CloudflareBrowserRunConfig,
        connector: BrowserRunConnector | None = None,
        dependency_probe: DependencyProbe = _playwright_dependency_available,
    ) -> None:
        if not isinstance(config, CloudflareBrowserRunConfig):
            raise TypeError("config must be CloudflareBrowserRunConfig")
        if connector is not None and not callable(getattr(connector, "connect", None)):
            raise TypeError("connector must implement connect")
        if not callable(dependency_probe):
            raise TypeError("dependency_probe must be callable")
        self._config = config
        self._connector = PlaywrightCdpConnector() if connector is None else connector
        self._dependency_probe = dependency_probe

    @property
    def availability(self) -> WebBackendAvailability:
        if self._config.enabled is not True:
            return WebBackendAvailability(False, WebBackendBlockerCode.DISABLED, "Cloudflare Browser Run is disabled")
        if not self._config.account_id or not self._config.api_token:
            return WebBackendAvailability(
                False,
                WebBackendBlockerCode.UNCONFIGURED,
                "Cloudflare Browser Run credentials are not configured",
            )
        try:
            dependency_available = self._dependency_probe() is True
        except BaseException:
            dependency_available = False
        if not dependency_available:
            return WebBackendAvailability(
                False,
                WebBackendBlockerCode.DEPENDENCY_MISSING,
                "Python Playwright dependency is not installed",
            )
        return WebBackendAvailability(
            True,
            None,
            "Browser Run configuration and Playwright dependency are present; live health is unverified",
        )

    @property
    def configured(self) -> bool:
        return self.availability.available is True

    def create(self, *, request_id: str, policy: BrowserSandboxPolicy) -> CloudflareBrowserRunSession:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id is invalid")
        if not isinstance(policy, BrowserSandboxPolicy):
            raise TypeError("policy must be BrowserSandboxPolicy")
        if self.configured is not True:
            raise BrowserRunContractError("remote browser backend is unavailable")
        return CloudflareBrowserRunSession(
            request_id=request_id,
            policy=policy,
            config=self._config,
            connector=self._connector,
        )


class CloudflareBrowserRunSession:
    """One-shot Playwright adapter with a fresh context and application-level egress gate."""

    def __init__(
        self,
        *,
        request_id: str,
        policy: BrowserSandboxPolicy,
        config: CloudflareBrowserRunConfig,
        connector: BrowserRunConnector,
    ) -> None:
        self._request_id = request_id
        self._policy = policy
        self._config = config
        self._connector = connector
        self._cleanup_confirmed = False
        self._used = False
        self._network_failure = False

    @property
    def cleanup_confirmed(self) -> bool:
        return self._cleanup_confirmed

    async def execute(self, request: BrowserSessionRequest) -> BrowserSessionResult:
        if not isinstance(request, BrowserSessionRequest):
            raise TypeError("request must be BrowserSessionRequest")
        if self._used:
            raise BrowserRunContractError("remote browser session is one-shot")
        self._used = True
        self._policy.validate_session(request)
        if any(
            isinstance(action, Screenshot) and action.image_format is not ScreenshotFormat.PNG
            for action in request.actions
        ):
            raise BrowserRunContractError("remote browser screenshots must be PNG")

        connection: BrowserRunConnection | None = None
        context: object | None = None
        page: object | None = None
        result: BrowserSessionResult | None = None
        error: BaseException | None = None
        try:
            connection = await self._connector.connect(
                endpoint=self._config.endpoint,
                headers={"Authorization": f"Bearer {self._config.api_token}"},
            )
            context = await connection.new_context(
                accept_downloads=False,
                service_workers="block",
            )
            network_guard = BrowserNetworkGuard(self._policy)
            route = getattr(context, "route", None)
            route_web_socket = getattr(context, "route_web_socket", None)
            if not callable(route) or not callable(route_web_socket):
                raise BrowserRunContractError("remote browser context interception is unavailable")
            await route(
                "**/*",
                _route_handler(
                    network_guard,
                    self,
                    max_total_bytes=self._policy.limits.max_total_bytes,
                ),
            )
            await route_web_socket("**/*", _websocket_route_handler(self))
            page = await context.new_page()
            outputs: list[BrowserOutput] = []
            expected_screenshot_origin: str | None = None
            for index, action in enumerate(request.actions):
                if isinstance(action, Navigate):
                    expected_screenshot_origin = _exact_https_origin(action.url)
                output = await _execute_action(
                    page,
                    action,
                    index=index,
                    expected_screenshot_origin=expected_screenshot_origin,
                )
                self._raise_network_failure()
                if output is not None:
                    outputs.append(output)
            result = BrowserSessionResult(tuple(outputs))
        except asyncio.CancelledError as exc:
            error = exc
        except BrowserRunContractError:
            error = BrowserRunContractError("remote browser execution failed")
        except BaseException as exc:
            if _is_transient_browser_error(exc):
                error = BrowserRunTransientError("remote browser execution failed transiently")
            else:
                error = BrowserRunContractError("remote browser execution failed")

        self._cleanup_confirmed = await _close_all(page=page, context=context, connection=connection)
        if self._cleanup_confirmed is not True:
            raise BrowserRunCleanupUnconfirmedError("remote browser cleanup was not confirmed") from None
        if isinstance(error, asyncio.CancelledError):
            raise error
        if error is not None:
            raise error from None
        assert result is not None
        return result

    def _reject_network(self) -> None:
        self._network_failure = True

    def _raise_network_failure(self) -> None:
        if self._network_failure:
            raise BrowserRunContractError("remote browser network request was rejected")


def _is_transient_browser_error(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return True
    error_type = type(error)
    return error_type.__module__.startswith("playwright.") and error_type.__name__ in {
        "TargetClosedError",
        "TimeoutError",
    }


def _route_handler(
    network_guard: BrowserNetworkGuard,
    session: CloudflareBrowserRunSession,
    *,
    max_total_bytes: int,
) -> Callable[[object], Awaitable[None]]:
    async def handle(route: object) -> None:
        try:
            request = route.request
            method = request.method
            url = request.url
            if method not in {"GET", "HEAD"}:
                raise BrowserRunContractError("remote browser method is not allowed")
            if request.redirected_from is not None:
                raise BrowserRunContractError("remote browser redirects are not allowed")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise BrowserRunContractError("remote browser URL is not HTTPS")
            network_guard.authorize_request(url, redirect=False)
            response = await route.fetch(max_redirects=0)
            declared_bytes = _response_content_length(response)
            remaining_bytes = max_total_bytes - network_guard.consumed_bytes
            if declared_bytes > remaining_bytes:
                raise BrowserRunContractError("remote browser response exceeded its byte budget")
            body = await response.body()
            if type(body) is not bytes:
                raise BrowserRunContractError("remote browser response body is invalid")
            if len(body) > declared_bytes or len(body) > remaining_bytes:
                raise BrowserRunContractError("remote browser response exceeded its declared size")
            network_guard.consume_bytes(len(body))
            await route.fulfill(response=response, body=body)
        except asyncio.CancelledError:
            raise
        except BaseException:
            session._reject_network()
            try:
                await route.abort("blockedbyclient")
            except BaseException:
                pass

    return handle


def _response_content_length(response: object) -> int:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        raise BrowserRunContractError("remote browser response headers are invalid")
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise BrowserRunContractError("remote browser response headers are invalid")
        lowered = name.lower()
        if lowered in normalized:
            raise BrowserRunContractError("remote browser response headers are ambiguous")
        normalized[lowered] = value
    content_length = normalized.get("content-length")
    if content_length is None or re.fullmatch(r"[0-9]+", content_length) is None:
        raise BrowserRunContractError("remote browser response length is invalid")
    return int(content_length)


def _websocket_route_handler(
    session: CloudflareBrowserRunSession,
) -> Callable[[object], Awaitable[None]]:
    async def handle(route: object) -> None:
        session._reject_network()
        close = getattr(route, "close", None)
        if not callable(close):
            return
        try:
            await close(code=1008, reason="blocked")
        except asyncio.CancelledError:
            raise
        except BaseException:
            pass

    return handle


async def _execute_action(
    page: object,
    action: BrowserAction,
    *,
    index: int,
    expected_screenshot_origin: str | None,
) -> BrowserOutput | None:
    if isinstance(action, Navigate):
        await page.goto(action.url, wait_until="domcontentloaded")
        return None
    if isinstance(action, Click):
        await page.click(action.selector.value)
        return None
    if isinstance(action, TypeText):
        if action.clear_first:
            await page.fill(action.selector.value, action.text)
        else:
            await page.type(action.selector.value, action.text)
        return None
    if isinstance(action, SelectOption):
        await page.select_option(action.selector.value, action.value)
        return None
    if isinstance(action, Scroll):
        await page.mouse.wheel(action.delta_x, action.delta_y)
        return None
    if isinstance(action, Wait):
        await page.wait_for_timeout(action.milliseconds)
        return None
    if isinstance(action, Screenshot):
        if expected_screenshot_origin != _YOUTUBE_SCREENSHOT_ORIGIN:
            raise BrowserRunContractError("remote browser screenshot origin is not allowed")
        actual_origin = _page_origin(page)
        if actual_origin != expected_screenshot_origin:
            raise BrowserRunContractError("remote browser page origin changed before screenshot")
        data = await page.screenshot(full_page=action.full_page, type="png")
        if _page_origin(page) != actual_origin:
            raise BrowserRunContractError("remote browser page origin changed during screenshot")
        if type(data) is not bytes or not data.startswith(_PNG_SIGNATURE) or len(data) > _MAX_SCREENSHOT_BYTES:
            raise BrowserRunContractError("remote browser screenshot violated the PNG contract")
        return BrowserOutput(
            step_index=index,
            kind=BrowserOutputKind.SCREENSHOT,
            data=data,
            media_type="image/png",
        )
    if isinstance(action, ExtractText):
        selector = "body" if action.selector is None else action.selector.value
        value = await page.text_content(selector)
        if not isinstance(value, str) or not value:
            raise BrowserRunContractError("remote browser text output is empty")
        data = value.encode("utf-8")
        if len(data) > _MAX_TEXT_BYTES:
            raise BrowserRunContractError("remote browser text output exceeded its limit")
        return BrowserOutput(
            step_index=index,
            kind=BrowserOutputKind.TEXT,
            data=data,
            media_type="text/plain; charset=utf-8",
        )
    raise BrowserRunContractError("unsupported remote browser action")


def _page_origin(page: object) -> str:
    return _exact_https_origin(getattr(page, "url", None))


def _exact_https_origin(value: object) -> str:
    if not isinstance(value, str):
        raise BrowserRunContractError("remote browser page origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BrowserRunContractError("remote browser page origin is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise BrowserRunContractError("remote browser page origin is invalid")
    return f"https://{parsed.hostname.lower()}"


async def _close_all(*, page: object | None, context: object | None, connection: BrowserRunConnection | None) -> bool:
    confirmed = connection is not None
    for resource in (page, context, connection):
        if resource is None:
            continue
        try:
            await asyncio.wait_for(resource.close(), timeout=_RESOURCE_CLEANUP_TIMEOUT_SECONDS)
        except BaseException:
            confirmed = False
    return confirmed


def youtube_playback_evidence_plan(
    *,
    plan_id: str,
    scope: BrowserRunScope,
    query: str,
) -> BrowserRunPlan:
    """Code-owned fixed recipe; callers provide only a bounded search query and scope."""

    if not isinstance(query, str) or not query or query != query.strip() or len(query) > _MAX_QUERY_CHARS:
        raise ValueError("YouTube search query is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in query):
        raise ValueError("YouTube search query is invalid")
    return BrowserRunPlan(
        plan_id=plan_id,
        scope=scope,
        segments=(
            BrowserPlanSegment(
                segment_id="youtube-search-evidence",
                actions=(
                    Navigate("https://www.youtube.com/"),
                    TypeText(CssSelector("input#search"), query),
                    Click(CssSelector("button#search-icon-legacy")),
                    Wait(1_000),
                    Screenshot(),
                ),
            ),
            BrowserPlanSegment(
                segment_id="youtube-playback-evidence",
                actions=(
                    Navigate("https://www.youtube.com/results?search_query=" + quote(query, safe="")),
                    Click(CssSelector("ytd-video-renderer a#video-title")),
                    Wait(1_000),
                    Click(CssSelector("button.ytp-play-button")),
                    Wait(1_000),
                    Screenshot(),
                ),
            ),
        ),
    )


__all__ = [
    "BROWSER_RUN_POLICY_BOUNDARY",
    "BrowserRunConnection",
    "BrowserRunConnector",
    "CloudflareBrowserRunConfig",
    "CloudflareBrowserRunSession",
    "CloudflareBrowserRunSessionFactory",
    "PlaywrightCdpConnector",
    "youtube_playback_evidence_plan",
]
