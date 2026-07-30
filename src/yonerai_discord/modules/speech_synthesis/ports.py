from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from yonerai_discord.provider_registry import ArtifactRef


AuthorizationCheck = Callable[[], bool | Awaitable[bool]]
RemoteConsentCheck = Callable[[int], bool | Awaitable[bool]]


class SpeechArtifactStore(Protocol):
    def read_wav(
        self,
        ref: ArtifactRef,
        *,
        request_binding: str,
        read_allowed: Callable[[], bool] | None = None,
    ) -> bytes: ...


class SpeechSynthesisSink(Protocol):
    async def send_wav(
        self,
        interaction: Any,
        wav: bytes,
        *,
        filename: str,
        ephemeral: bool,
        mentions_allowed: bool,
    ) -> None: ...


__all__ = [
    "AuthorizationCheck",
    "RemoteConsentCheck",
    "SpeechArtifactStore",
    "SpeechSynthesisSink",
]
