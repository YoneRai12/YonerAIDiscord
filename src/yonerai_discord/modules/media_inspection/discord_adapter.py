"""Discord message scope向けmedia URL inspection境界。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .domain import MediaInspectionResult


AuthorizationCurrent = Callable[[], bool | Awaitable[bool]]


class MediaInspectionProvider(Protocol):
    async def inspect(self, url: str, instruction: str) -> MediaInspectionResult: ...


class DiscordMediaInspectionAdapter:
    """remote送信前後にfresh認可を再確認し、安全なtextだけを返す。"""

    def __init__(self, provider: MediaInspectionProvider) -> None:
        try:
            provider_inspect = getattr(provider, "inspect", None)
        except Exception as exc:
            raise TypeError("provider must implement inspect()") from exc
        if not callable(provider_inspect):
            raise TypeError("provider must implement inspect()")
        self._provider = provider
        self._requires_external_ai_consent = _requires_external_ai_consent(provider)
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def requires_external_ai_consent(self) -> bool:
        return self._requires_external_ai_consent

    def begin_close(self) -> None:
        self._closing = True

    async def inspect_for_message(
        self,
        message: Any,
        url: str,
        instruction: str,
        authorization_current: AuthorizationCurrent,
    ) -> str | None:
        if self._closing or not callable(authorization_current) or not _valid_message_scope(message):
            return None
        try:
            if not await self._authorized(authorization_current):
                return None
            result = await self._provider.inspect(url, instruction)
            if not await self._authorized(authorization_current):
                return None
            return result.text
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _authorized(self, check: AuthorizationCurrent) -> bool:
        if self._closing:
            return False
        try:
            result = check()
            if inspect.isawaitable(result):
                result = await result
            return not self._closing and result is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False


def _valid_message_scope(message: object) -> bool:
    values = (
        getattr(getattr(message, "guild", None), "id", None),
        getattr(getattr(message, "channel", None), "id", None),
        getattr(getattr(message, "author", None), "id", None),
        getattr(message, "id", None),
    )
    return all(not isinstance(value, bool) and isinstance(value, int) and value > 0 for value in values)


def _requires_external_ai_consent(provider: object) -> bool:
    try:
        value = getattr(provider, "requires_external_ai_consent")
    except Exception:
        return True
    return value is not False


__all__ = ["AuthorizationCurrent", "DiscordMediaInspectionAdapter", "MediaInspectionProvider"]
