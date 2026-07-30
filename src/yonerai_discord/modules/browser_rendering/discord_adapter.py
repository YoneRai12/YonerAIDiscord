"""Discord delivery boundary for the narrow remote screenshot capability.

This adapter intentionally does not use ``BrowserSandboxService``.  The
Cloudflare backend executes remotely, therefore this module only exposes an
owner-authorized ``Navigate -> Screenshot`` operation and makes no browser
isolation claim.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from yonerai_discord.browser_sandbox.discord_sink import DiscordBrowserScreenshotSink
from yonerai_discord.browser_sandbox.models import BrowserSessionRequest, Navigate, Screenshot
from yonerai_discord.browser_sandbox.remote_service import RemoteBrowserScreenshotService


_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "accesstoken",
        "apikey",
        "accesskey",
        "accesskeyid",
        "privatekey",
        "secret",
        "signature",
        "sig",
        "auth",
        "authorization",
        "code",
        "oauthcode",
        "authorizationcode",
        "password",
        "session",
        "sessionid",
        "sessiontoken",
        "securitytoken",
        "cookie",
        "jwt",
        "credential",
    }
)
_IDEMPOTENCY_CACHE_LIMIT = 256


class RemoteBrowserScreenshotInputError(ValueError):
    """Unsafe input was rejected before any remote request or audit write."""


@dataclass(frozen=True, slots=True)
class RemoteBrowserScreenshotRequest:
    """Minimal typed Discord binding; the target URL is deliberately redacted."""

    guild_id: int
    channel_id: int
    actor_id: int
    message_id: int
    session: BrowserSessionRequest

    @classmethod
    def from_message(cls, message: Any, url: str) -> "RemoteBrowserScreenshotRequest":
        _validate_target_url(url)
        values = (
            getattr(getattr(message, "guild", None), "id", None),
            getattr(getattr(message, "channel", None), "id", None),
            getattr(getattr(message, "author", None), "id", None),
            getattr(message, "id", None),
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise RemoteBrowserScreenshotInputError("Discord scope is unavailable")
        guild_id, channel_id, actor_id, message_id = values
        return cls(
            guild_id=guild_id,
            channel_id=channel_id,
            actor_id=actor_id,
            message_id=message_id,
            session=BrowserSessionRequest((Navigate(url), Screenshot())),
        )


AuthorizationCurrent = Callable[[], bool | Awaitable[bool]]


class DiscordRemoteBrowserScreenshotAdapter:
    """Fail closed before remote egress and again before Discord delivery."""

    def __init__(
        self,
        service: RemoteBrowserScreenshotService,
        sink: DiscordBrowserScreenshotSink | Any | None = None,
    ) -> None:
        if not isinstance(service, RemoteBrowserScreenshotService):
            raise TypeError("service must be a RemoteBrowserScreenshotService")
        if sink is not None and not callable(getattr(sink, "send_screenshot", None)):
            raise TypeError("sink must provide send_screenshot")
        self._service = service
        self._sink = sink or DiscordBrowserScreenshotSink()
        self._closing = False
        self._idempotency_lock = asyncio.Lock()
        self._inflight: dict[tuple[int, str, bytes], asyncio.Task[bool]] = {}
        self._terminal: OrderedDict[tuple[int, str, bytes], bool] = OrderedDict()

    def begin_close(self) -> None:
        self._closing = True

    @property
    def closing(self) -> bool:
        return self._closing

    async def capture_for_message(
        self,
        message: Any,
        *,
        url: str,
        authorization_current: AuthorizationCurrent,
        target_kind: str = "url",
    ) -> bool:
        """Capture and reply once; URL/query/body are never audited or displayed."""
        if self._closing or not callable(authorization_current):
            return False
        try:
            request = RemoteBrowserScreenshotRequest.from_message(message, url)
            if not _target_kind(target_kind):
                return False
            key = (request.message_id, target_kind, _target_fingerprint(url))
            task = await self._idempotent_task(
                key,
                message=message,
                request=request,
                authorization_current=authorization_current,
                target_kind=target_kind,
            )
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _idempotent_task(
        self,
        key: tuple[int, str, bytes],
        *,
        message: Any,
        request: RemoteBrowserScreenshotRequest,
        authorization_current: AuthorizationCurrent,
        target_kind: str,
    ) -> asyncio.Task[bool]:
        async with self._idempotency_lock:
            terminal = self._terminal.get(key)
            if terminal is not None:
                return _completed_bool_task(terminal)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._capture_once(
                        message,
                        request=request,
                        authorization_current=authorization_current,
                        target_kind=target_kind,
                    )
                )
                self._inflight[key] = task
                task.add_done_callback(lambda completed: asyncio.create_task(self._remember_completion(key, completed)))
            return task

    async def _remember_completion(self, key: tuple[int, str, bytes], task: asyncio.Task[bool]) -> None:
        async with self._idempotency_lock:
            if self._inflight.get(key) is not task:
                return
            self._inflight.pop(key, None)
            if task.cancelled():
                return
            try:
                completed = task.result()
            except Exception:
                completed = False
            self._terminal[key] = completed is True
            self._terminal.move_to_end(key)
            while len(self._terminal) > _IDEMPOTENCY_CACHE_LIMIT:
                self._terminal.popitem(last=False)

    async def _capture_once(
        self,
        message: Any,
        *,
        request: RemoteBrowserScreenshotRequest,
        authorization_current: AuthorizationCurrent,
        target_kind: str,
    ) -> bool:
        async def currently_authorized() -> bool:
            return not self._closing and await _authorized(authorization_current)

        if not await currently_authorized():
            return False
        if not await self._append_audit(message, request, event="browser_rendering.requested", target_kind=target_kind):
            return False
        result = await self._service.capture_screenshot(
            request.session,
            authorization_current=currently_authorized,
        )
        if not await currently_authorized():
            return False
        if not await self._append_audit(message, request, event="browser_rendering.completed", target_kind=target_kind):
            return False
        if not await currently_authorized():
            return False
        await self._sink.send_screenshot(message, result.outputs[0])
        return True

    async def _append_audit(
        self,
        message: Any,
        request: RemoteBrowserScreenshotRequest,
        *,
        event: str,
        target_kind: str,
    ) -> bool:
        bot = getattr(self, "_bot", None)
        append = getattr(getattr(bot, "database", None), "append_audit", None)
        if not callable(append):
            return False
        # Only stable Discord identifiers and a fixed target kind are durable.
        details: Mapping[str, object] = {
            "channel_id": request.channel_id,
            "message_id": request.message_id,
            "target_kind": target_kind,
        }
        try:
            await asyncio.to_thread(
                append,
                event,
                actor_id=request.actor_id,
                guild_id=request.guild_id,
                plugin="browser_rendering",
                details=details,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return not self._closing

    def bind_bot(self, bot: Any) -> None:
        """Plugin-only binding.  Kept out of request construction and audit data."""
        self._bot = bot


async def _authorized(check: AuthorizationCurrent) -> bool:
    try:
        allowed = check()
        if inspect.isawaitable(allowed):
            allowed = await allowed
        return allowed is True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


def _validate_target_url(url: object) -> None:
    if not isinstance(url, str) or not url or url != url.strip():
        raise RemoteBrowserScreenshotInputError("URL is invalid")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise RemoteBrowserScreenshotInputError("URL is invalid") from exc
    if parsed.fragment or parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise RemoteBrowserScreenshotInputError("URL is invalid")
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:
        raise RemoteBrowserScreenshotInputError("URL is invalid") from exc
    if any(_is_sensitive_query_key(key) for key, _value in pairs):
        raise RemoteBrowserScreenshotInputError("URL contains a credential-like query parameter")


def _is_sensitive_query_key(value: str) -> bool:
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    return normalized in _SENSITIVE_QUERY_KEYS or any(
        normalized.endswith(suffix) for suffix in ("token", "secret", "password", "credential", "signature")
    )


def _target_kind(value: object) -> bool:
    return isinstance(value, str) and value in {"url", "youtube_search"}


def _target_fingerprint(url: str) -> bytes:
    """Process-local dedupe identity without retaining the raw target URL."""

    return hashlib.sha256(url.encode("utf-8")).digest()


def _completed_bool_task(value: bool) -> asyncio.Task[bool]:
    async def completed() -> bool:
        return value

    return asyncio.create_task(completed())


__all__ = [
    "DiscordRemoteBrowserScreenshotAdapter",
    "RemoteBrowserScreenshotInputError",
    "RemoteBrowserScreenshotRequest",
]
