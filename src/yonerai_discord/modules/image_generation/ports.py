from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from yonerai_discord.provider_registry import ArtifactRef

if TYPE_CHECKING:
    from yonerai_discord.modules.image_editing.domain import ImageEditSource


AuthorizationCheck = Callable[[], bool | Awaitable[bool]]
RemoteConsentCheck = Callable[[int], bool | Awaitable[bool]]


class ImageArtifactStore(Protocol):
    """PNG実体を固定rootへ保存し、opaqueなArtifactRefだけを返すport。"""

    def put_png(
        self,
        data: bytes,
        *,
        request_binding: str,
        artifact_id: str | None = None,
    ) -> ArtifactRef: ...

    def read_png(
        self,
        ref: ArtifactRef,
        *,
        request_binding: str,
        read_allowed: Callable[[], bool] | None = None,
    ) -> bytes: ...

    def protect_png(
        self,
        ref: ArtifactRef,
        *,
        request_binding: str,
    ) -> AbstractContextManager[None]: ...


@runtime_checkable
class ImageEditSourceClaimIssuerPort(Protocol):
    """生成側が依存してよい、process-local な画像編集source claim発行口。"""

    artifact_store: ImageArtifactStore

    async def issue(
        self,
        artifact: ArtifactRef,
        *,
        source_binding: str,
        edit_request_id: str,
        guild_id: int,
        channel_id: int,
        actor_id: int,
        authorization_current: AuthorizationCheck,
    ) -> ImageEditSource: ...


__all__ = [
    "AuthorizationCheck",
    "ImageArtifactStore",
    "ImageEditSourceClaimIssuerPort",
    "RemoteConsentCheck",
]
