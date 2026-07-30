from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from yonerai_discord.provider_registry import ArtifactRef


AuthorizationCheck = Callable[[], bool | Awaitable[bool]]
RemoteConsentCheck = Callable[[int], bool | Awaitable[bool]]


class VideoArtifactStore(Protocol):
    """MP4実体を固定rootへ保存し、opaqueなArtifactRefだけを返すport。"""

    def put_mp4(
        self,
        data: bytes,
        *,
        request_binding: str,
        artifact_id: str | None = None,
    ) -> ArtifactRef: ...

    def read_mp4(
        self,
        ref: ArtifactRef,
        *,
        request_binding: str,
        read_allowed: Callable[[], bool] | None = None,
    ) -> bytes: ...


__all__ = ["AuthorizationCheck", "RemoteConsentCheck", "VideoArtifactStore"]
