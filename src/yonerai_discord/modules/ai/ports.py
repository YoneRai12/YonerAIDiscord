from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol

from .models import AIReply, AIRequest


_SERVICE_SINK_ISSUER = object()


class ProviderAuthorizationError(PermissionError):
    """The remote authorization was withdrawn before the provider HTTP sink."""


class _ServiceSinkVerifier:
    """One-shot in-process permit issued only by ``AIService``."""

    __slots__ = ("_check", "_consumed", "_issuer", "_provider", "_request")

    def __init__(
        self,
        issuer: object,
        *,
        request: AIRequest,
        provider: object,
        check: Callable[[], bool | Awaitable[bool]],
    ) -> None:
        if issuer is not _SERVICE_SINK_ISSUER:
            raise TypeError("service sink verifier issuer is invalid")
        if not callable(check):
            raise TypeError("service sink verifier check must be callable")
        self._issuer = issuer
        self._request = request
        self._provider = provider
        self._check = check
        self._consumed = False

    def _consume(self, *, request: AIRequest, provider: object) -> bool:
        if (
            self._issuer is not _SERVICE_SINK_ISSUER
            or self._consumed
            or request is not self._request
            or provider is not self._provider
        ):
            return False
        self._consumed = True
        try:
            result = self._check()
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                return False
            return result is True
        except Exception:
            return False

    async def _consume_async(self, *, request: AIRequest, provider: object) -> bool:
        if (
            self._issuer is not _SERVICE_SINK_ISSUER
            or self._consumed
            or request is not self._request
            or provider is not self._provider
        ):
            return False
        self._consumed = True
        try:
            result = self._check()
            if inspect.isawaitable(result):
                result = await result
            return result is True
        except Exception:
            return False


def _issue_service_sink_verifier(
    *,
    request: AIRequest,
    provider: object,
    check: Callable[[], bool | Awaitable[bool]],
) -> _ServiceSinkVerifier:
    return _ServiceSinkVerifier(
        _SERVICE_SINK_ISSUER,
        request=request,
        provider=provider,
        check=check,
    )


def _verify_service_sink(
    value: object,
    *,
    request: AIRequest,
    provider: object,
) -> bool:
    return isinstance(value, _ServiceSinkVerifier) and value._consume(
        request=request,
        provider=provider,
    )


async def _verify_service_sink_async(
    value: object,
    *,
    request: AIRequest,
    provider: object,
) -> bool:
    return isinstance(value, _ServiceSinkVerifier) and await value._consume_async(
        request=request,
        provider=provider,
    )


class AIProvider(Protocol):
    @property
    def is_local(self) -> bool: ...

    async def complete(self, request: AIRequest) -> AIReply: ...

    async def complete_authorized(
        self,
        request: AIRequest,
        provider_sink_verifier: object,
    ) -> AIReply: ...
