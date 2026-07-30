from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock

from yonerai_discord.modules.image_generation.artifacts import (
    ImageArtifactAuthorizationError,
    ImageArtifactError,
    ImageArtifactValidationError,
    canonicalize_png,
)
from yonerai_discord.provider_registry import ArtifactRef

from .domain import (
    ImageEditSource,
    ImageEditingAuthorizationError,
    ImageEditingContractError,
)
from .ports import ImageArtifactStore


SourceClaimAuthorizationCheck = Callable[[], bool | Awaitable[bool]]
DEFAULT_MAX_SOURCE_CLAIMS = 64
MAX_SOURCE_CLAIMS = 1_024


@dataclass(frozen=True, slots=True)
class _IssuedClaim:
    claim: ImageEditSource
    digest: str


class ImageEditSourceClaimIssuer:
    """既存store内のcanonical PNGへ、process-localな編集source claimを発行する。"""

    def __init__(
        self,
        artifact_store: ImageArtifactStore,
        *,
        max_claims: int = DEFAULT_MAX_SOURCE_CLAIMS,
    ) -> None:
        if not callable(getattr(artifact_store, "protect_png", None)) or not callable(
            getattr(artifact_store, "read_png", None)
        ):
            raise TypeError("artifact_store must provide protect_png and read_png")
        if isinstance(max_claims, bool) or not isinstance(max_claims, int) or not 1 <= max_claims <= MAX_SOURCE_CLAIMS:
            raise ValueError("max_claims is outside the allowed range")
        self.artifact_store = artifact_store
        self.max_claims = max_claims
        self._claims: dict[str, _IssuedClaim] = {}
        self._lock = RLock()
        self._closing = False

    async def issue(
        self,
        artifact: ArtifactRef,
        *,
        source_binding: str,
        edit_request_id: str,
        guild_id: int,
        channel_id: int,
        actor_id: int,
        authorization_current: SourceClaimAuthorizationCheck,
    ) -> ImageEditSource:
        """scopeとartifact identityへ束縛したclaimを、fresh認可後にだけ発行する。"""

        if not callable(authorization_current):
            raise TypeError("authorization_current is required")
        await self._require_authorized(authorization_current)
        try:
            claim = ImageEditSource(
                artifact,
                edit_request_id=edit_request_id,
                guild_id=guild_id,
                channel_id=channel_id,
                actor_id=actor_id,
                source_binding=source_binding,
            )
        except (TypeError, ValueError):
            raise ImageEditingContractError("image edit source claim input is invalid") from None

        loop = asyncio.get_running_loop()

        def read_allowed() -> bool:
            with self._lock:
                if self._closing:
                    return False
            future: Future[bool] = asyncio.run_coroutine_threadsafe(
                self._authorization_value(authorization_current),
                loop,
            )
            try:
                allowed = future.result()
            except Exception:
                future.cancel()
                return False
            with self._lock:
                return not self._closing and allowed is True

        try:
            with self.artifact_store.protect_png(
                artifact,
                request_binding=source_binding,
            ):
                await self._require_authorized(authorization_current)
                png = await asyncio.to_thread(
                    self.artifact_store.read_png,
                    artifact,
                    request_binding=source_binding,
                    read_allowed=read_allowed,
                )
                await self._require_authorized(authorization_current)
        except asyncio.CancelledError:
            raise
        except ImageArtifactAuthorizationError:
            raise ImageEditingAuthorizationError("image edit source authorization changed") from None
        except (ImageArtifactValidationError, ImageArtifactError, TypeError, ValueError):
            raise ImageEditingContractError("image edit source artifact could not be verified") from None
        except ImageEditingAuthorizationError:
            raise
        except Exception:
            raise ImageEditingContractError("image edit source artifact could not be verified") from None

        try:
            canonical = canonicalize_png(png)
        except (ImageArtifactValidationError, TypeError, ValueError):
            raise ImageEditingContractError("image edit source artifact could not be verified") from None
        if (
            canonical.data != png
            or artifact.size_bytes != len(png)
            or artifact.sha256 != hashlib.sha256(png).hexdigest()
        ):
            raise ImageEditingContractError("image edit source artifact integrity mismatch")

        await self._require_authorized(authorization_current)
        entry = _IssuedClaim(claim=claim, digest=claim.claim_digest)
        with self._lock:
            if self._closing:
                raise ImageEditingAuthorizationError("image edit source issuer is closing")
            self._claims.pop(claim.edit_request_id, None)
            self._claims[claim.edit_request_id] = entry
            while len(self._claims) > self.max_claims:
                self._claims.pop(next(iter(self._claims)))
        return claim

    def current(self, claim: ImageEditSource) -> bool:
        """このissuerが現在保持する同一claim objectだけを許可する。"""

        if not isinstance(claim, ImageEditSource):
            return False
        with self._lock:
            entry = self._claims.get(claim.edit_request_id)
            return (
                not self._closing and entry is not None and entry.claim is claim and entry.digest == claim.claim_digest
            )

    def revoke(self, claim: ImageEditSource) -> bool:
        if not isinstance(claim, ImageEditSource):
            return False
        with self._lock:
            entry = self._claims.get(claim.edit_request_id)
            if entry is None or entry.claim is not claim:
                return False
            self._claims.pop(claim.edit_request_id)
            return True

    def begin_close(self) -> None:
        with self._lock:
            self._closing = True
            self._claims.clear()

    async def _require_authorized(self, check: SourceClaimAuthorizationCheck) -> None:
        if await self._authorization_value(check) is not True:
            raise ImageEditingAuthorizationError("image edit source authorization changed")

    async def _authorization_value(self, check: SourceClaimAuthorizationCheck) -> bool:
        with self._lock:
            if self._closing:
                return False
        try:
            allowed = check()
            allowed = await allowed if inspect.isawaitable(allowed) else allowed
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        with self._lock:
            return not self._closing and allowed is True


__all__ = [
    "DEFAULT_MAX_SOURCE_CLAIMS",
    "MAX_SOURCE_CLAIMS",
    "ImageEditSourceClaimIssuer",
    "SourceClaimAuthorizationCheck",
]
