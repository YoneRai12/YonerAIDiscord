from __future__ import annotations
from collections.abc import Awaitable, Callable
from typing import Protocol

from yonerai_discord.provider_registry import (
    ArtifactRef,
    ProviderRequest,
    ProviderResolution,
)

from .domain import MusicGenerationRequest

AuthorizationCheck = Callable[[], bool | Awaitable[bool]]
RemoteConsentCheck = Callable[[int], bool | Awaitable[bool]]


class MusicArtifactStore(Protocol):
    def read_wav(
        self, ref: ArtifactRef, *, request_binding: str, read_allowed: Callable[[], bool] | None = None
    ) -> bytes: ...


class MusicExecutionProofIssuer(Protocol):
    """registry.executeの間だけ有効な、service発行rights proof。"""

    def issue(
        self,
        request: MusicGenerationRequest,
        provider_request: ProviderRequest,
        resolution: ProviderResolution,
    ) -> object: ...

    def revoke(self, token: object) -> None: ...
