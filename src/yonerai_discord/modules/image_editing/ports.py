from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from yonerai_discord.modules.image_generation.ports import ImageArtifactStore

from .domain import ImageEditSource


AuthorizationCheck = Callable[[], bool | Awaitable[bool]]
RemoteConsentCheck = Callable[[int], bool | Awaitable[bool]]
SourceArtifactCheck = Callable[[ImageEditSource], bool | Awaitable[bool]]


class ImageEditingSink(Protocol):
    async def send_png(
        self,
        interaction: Any,
        png: bytes,
        *,
        filename: str,
        ephemeral: bool,
        mentions_allowed: bool,
    ) -> None: ...


__all__ = [
    "AuthorizationCheck",
    "ImageArtifactStore",
    "ImageEditingSink",
    "RemoteConsentCheck",
    "SourceArtifactCheck",
]
