"""Core Filesのscope-bound参照をDiscord配送用canonical PNGへ準備する。"""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable

from yonerai_discord.execution_gateway.core_contract import (
    DiscordCoreFacts,
    discord_core_conversation_id,
)
from yonerai_discord.execution_gateway.core_files import (
    CoreArtifactRefV01,
    CoreArtifactOwnerScopeV01,
    CoreFileReadRequestV01,
    CoreFilesContractError,
    CoreFilesReadPortV01,
    core_ref_from_artifact_v01,
    read_core_file_v01,
)
from yonerai_discord.execution_gateway.models import ArtifactReference
from yonerai_discord.modules.media_pipeline.artifacts import PNG_MEDIA_TYPE, validate_canonical_png
from yonerai_discord.modules.media_pipeline.delivery import (
    MAX_PREPARED_ATTACHMENTS,
    MAX_PREPARED_MEDIA_BYTES,
    PreparedMediaAttachment,
)
from yonerai_discord.modules.media_pipeline.domain import (
    ArtifactKind,
    MAX_PNG_BYTES,
    MediaIntegrityError,
    MediaValidationError,
)


AuthorizationCurrent = Callable[[], bool | Awaitable[bool]]
PortCurrent = Callable[[], CoreFilesReadPortV01 | None]
DEFAULT_CORE_ARTIFACT_DELIVERY_TIMEOUT_SECONDS = 30.0


class CoreArtifactDeliveryError(RuntimeError):
    """Core artifactを安全にDiscord配送へ移せなかった。"""


class CoreArtifactDeliveryPreparer:
    """注入されたCore Files portから検証済みPNG bytesだけを取り出す。"""

    def __init__(
        self,
        port: CoreFilesReadPortV01,
        *,
        port_current: PortCurrent,
        timeout_seconds: float = DEFAULT_CORE_ARTIFACT_DELIVERY_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(getattr(port, "read_for_delivery", None)) or not callable(port_current):
            raise CoreArtifactDeliveryError("Core artifact delivery is unavailable")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.1 <= float(timeout_seconds) <= 60.0
        ):
            raise CoreArtifactDeliveryError("Core artifact delivery is unavailable")
        self._port = port
        self._port_current = port_current
        self._timeout_seconds = float(timeout_seconds)

    async def prepare(
        self,
        artifacts: tuple[ArtifactReference, ...],
        *,
        facts: DiscordCoreFacts,
        authorization_current: AuthorizationCurrent,
    ) -> tuple[PreparedMediaAttachment, ...]:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await self._prepare(
                    artifacts,
                    facts=facts,
                    authorization_current=authorization_current,
                )
        except TimeoutError:
            raise CoreArtifactDeliveryError("Core artifact delivery is unavailable") from None

    async def _prepare(
        self,
        artifacts: tuple[ArtifactReference, ...],
        *,
        facts: DiscordCoreFacts,
        authorization_current: AuthorizationCurrent,
    ) -> tuple[PreparedMediaAttachment, ...]:
        if (
            not isinstance(artifacts, tuple)
            or not 1 <= len(artifacts) <= MAX_PREPARED_ATTACHMENTS
            or any(not isinstance(artifact, ArtifactReference) for artifact in artifacts)
            or not isinstance(facts, DiscordCoreFacts)
            or not callable(authorization_current)
        ):
            raise CoreArtifactDeliveryError("Core artifact delivery is unavailable")

        try:
            owner_scope = CoreArtifactOwnerScopeV01(
                provider="discord",
                subject_id=str(facts.user_id),
                conversation_id=discord_core_conversation_id(facts),
            )
            refs = tuple(core_ref_from_artifact_v01(artifact, owner_scope=owner_scope) for artifact in artifacts)
        except (CoreFilesContractError, TypeError, ValueError):
            raise CoreArtifactDeliveryError("Core artifact delivery is unavailable") from None

        if (
            len({ref.artifact_id for ref in refs}) != len(refs)
            or len({ref.attachment_id for ref in refs}) != len(refs)
            or any(
                ref.kind != "image" or ref.media_type != PNG_MEDIA_TYPE or not 1 <= ref.size_bytes <= MAX_PNG_BYTES
                for ref in refs
            )
            or sum(ref.size_bytes for ref in refs) > MAX_PREPARED_MEDIA_BYTES
        ):
            raise CoreArtifactDeliveryError("Core artifact delivery is unavailable")

        await self._require_current(authorization_current)
        prepared: list[PreparedMediaAttachment] = []
        total_bytes = 0
        for index, ref in enumerate(refs, start=1):
            await self._require_current(authorization_current)
            request = CoreFileReadRequestV01(
                delivery_id=f"{facts.request_id}:msg:{facts.message_id}:media:{index:02d}",
                ref=ref,
                owner_scope=owner_scope,
            )
            try:
                data = await read_core_file_v01(request, self._port)
            except Exception:
                raise CoreArtifactDeliveryError("Core artifact delivery is unavailable") from None
            await self._require_current(authorization_current)

            try:
                prepared_item = await asyncio.to_thread(
                    _prepare_canonical_png,
                    data,
                    ref,
                    index,
                )
            except (MediaIntegrityError, MediaValidationError, TypeError, ValueError):
                raise CoreArtifactDeliveryError("Core artifact delivery is unavailable") from None
            await self._require_current(authorization_current)
            total_bytes += len(data)
            if total_bytes > MAX_PREPARED_MEDIA_BYTES:
                raise CoreArtifactDeliveryError("Core artifact delivery is unavailable")
            prepared.append(prepared_item)

        await self._require_current(authorization_current)
        return tuple(prepared)

    async def currently_available(self, authorization_current: AuthorizationCurrent) -> bool:
        """read後からDiscord送信までのport identityと認可を再確認する。"""

        if not callable(authorization_current):
            return False
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._require_current(authorization_current)
        except (CoreArtifactDeliveryError, TimeoutError):
            return False
        return True

    async def _require_current(self, authorization_current: AuthorizationCurrent) -> None:
        try:
            if self._port_current() is not self._port:
                raise CoreArtifactDeliveryError("Core artifact delivery is unavailable")
            value = authorization_current()
            allowed = await value if inspect.isawaitable(value) else value
            if self._port_current() is not self._port:
                raise CoreArtifactDeliveryError("Core artifact delivery is unavailable")
        except CoreArtifactDeliveryError:
            raise
        except Exception:
            raise CoreArtifactDeliveryError("Core artifact delivery is unavailable") from None
        if allowed is not True:
            raise CoreArtifactDeliveryError("Core artifact delivery is unavailable")


def _prepare_canonical_png(
    data: bytes,
    ref: CoreArtifactRefV01,
    index: int,
) -> PreparedMediaAttachment:
    canonical = validate_canonical_png(data)
    if canonical.data != data or len(data) != ref.size_bytes or canonical.content_digest != ref.sha256:
        raise ValueError("Core artifact delivery is unavailable")
    return PreparedMediaAttachment(
        filename=f"media-{index:02d}.png",
        data=data,
        media_type=PNG_MEDIA_TYPE,
        kind=ArtifactKind.IMAGE,
        width=canonical.width,
        height=canonical.height,
    )


__all__ = [
    "DEFAULT_CORE_ARTIFACT_DELIVERY_TIMEOUT_SECONDS",
    "CoreArtifactDeliveryError",
    "CoreArtifactDeliveryPreparer",
]
