from __future__ import annotations

import asyncio
import hashlib
import inspect
from dataclasses import dataclass

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    LogicalCapability,
    MediaGenerationInput,
    ProviderKind,
    ProviderRegistry,
    ProviderRequest,
    ProviderResolution,
    ProviderResult,
    ProviderUnavailableError,
)

from .domain import (
    GeneratedVideo,
    VideoGenerationAuthorizationError,
    VideoGenerationContractError,
    VideoGenerationIdempotencyError,
    VideoGenerationRequest,
    VideoGenerationUnavailableError,
    video_artifact_request_binding,
)
from .ports import AuthorizationCheck, RemoteConsentCheck, VideoArtifactStore


@dataclass(frozen=True, slots=True)
class _CachedVideo:
    fingerprint: str
    artifact: ArtifactRef
    request_binding: str


class VideoGenerationService:
    """provider-neutralな単発動画生成。promptや動画bytesをDBへ保存しない。"""

    def __init__(
        self,
        registry: ProviderRegistry | None,
        artifact_store: VideoArtifactStore | None,
        *,
        remote_consent_active: RemoteConsentCheck | None = None,
        max_idempotency_entries: int = 256,
    ) -> None:
        if registry is not None and not isinstance(registry, ProviderRegistry):
            raise TypeError("registry must be a ProviderRegistry or None")
        if isinstance(max_idempotency_entries, bool) or not isinstance(max_idempotency_entries, int):
            raise TypeError("max_idempotency_entries must be an integer")
        if not 1 <= max_idempotency_entries <= 4_096:
            raise ValueError("max_idempotency_entries is outside the allowed range")
        self.registry = registry
        self.artifact_store = artifact_store
        self.remote_consent_active = remote_consent_active
        self.max_idempotency_entries = max_idempotency_entries
        self._lock = asyncio.Lock()
        self._completed: dict[str, _CachedVideo] = {}
        self._closing = False

    def begin_close(self) -> None:
        self._closing = True

    async def delivery_current(
        self,
        request: VideoGenerationRequest,
        generated: GeneratedVideo,
        *,
        authorization_current: AuthorizationCheck,
    ) -> bool:
        """Discord送信直前のroute/health/同意/権限とartifact整合を再検査する。"""

        if not isinstance(request, VideoGenerationRequest) or not isinstance(generated, GeneratedVideo):
            return False
        try:
            selection, _ = await self._current_selection(request, authorization_current)
        except asyncio.CancelledError:
            raise
        except (VideoGenerationAuthorizationError, VideoGenerationUnavailableError):
            return False
        except Exception:
            return False
        cached = self._completed.get(request.request_id)
        if cached is None or cached.artifact != generated.artifact:
            return False
        provider_request = self._provider_request(request)
        return (
            cached.request_binding == self._binding(provider_request, selection)
            and generated.artifact.size_bytes == len(generated.mp4)
            and generated.artifact.sha256 == hashlib.sha256(generated.mp4).hexdigest()
        )

    async def generate(
        self,
        request: VideoGenerationRequest,
        *,
        authorization_current: AuthorizationCheck,
    ) -> GeneratedVideo:
        if not isinstance(request, VideoGenerationRequest):
            raise TypeError("request must be a VideoGenerationRequest")
        if not callable(authorization_current):
            raise TypeError("authorization_current is required")
        async with self._lock:
            await self._require_authorized(authorization_current)
            cached = self._completed.get(request.request_id)
            if cached is not None:
                if cached.fingerprint != request.fingerprint:
                    raise VideoGenerationIdempotencyError("request_id was reused for a different request")
                return await self._read_validated(
                    cached.artifact,
                    request_binding=cached.request_binding,
                    request=request,
                    authorization_current=authorization_current,
                )
            artifact, request_binding = await self._generate_once(
                request,
                authorization_current=authorization_current,
            )
            self._completed[request.request_id] = _CachedVideo(
                request.fingerprint,
                artifact,
                request_binding,
            )
            while len(self._completed) > self.max_idempotency_entries:
                self._completed.pop(next(iter(self._completed)))
            return await self._read_validated(
                artifact,
                request_binding=request_binding,
                request=request,
                authorization_current=authorization_current,
            )

    async def _generate_once(
        self,
        request: VideoGenerationRequest,
        *,
        authorization_current: AuthorizationCheck,
    ) -> tuple[ArtifactRef, str]:
        registry = self.registry
        if registry is None or self.artifact_store is None:
            raise VideoGenerationUnavailableError("video generation provider is not configured")
        selection, consent_verified = await self._current_selection(request, authorization_current)
        provider_request = self._provider_request(request)
        request_binding = self._binding(provider_request, selection)

        async def execution_allowed() -> bool:
            try:
                current, current_consent = await self._current_selection(request, authorization_current)
            except (VideoGenerationAuthorizationError, VideoGenerationUnavailableError):
                return False
            return (
                current_consent == consent_verified
                and current.provider_id == selection.provider_id
                and current.provider_model == selection.provider_model
                and current.model_alias == selection.model_alias
                and current.quality_tier is selection.quality_tier
            )

        try:
            result = await registry.execute(
                provider_request,
                actor_level=RbacLevel.TRUSTED,
                consent_verified=consent_verified,
                execution_allowed=execution_allowed,
            )
        except ProviderUnavailableError:
            raise VideoGenerationUnavailableError("video generation provider is unavailable") from None
        except VideoGenerationAuthorizationError:
            raise
        except Exception:
            raise VideoGenerationUnavailableError("video generation failed safely") from None
        await self._require_authorized(authorization_current)
        if not await execution_allowed():
            raise VideoGenerationAuthorizationError("authorization changed after provider execution")
        return self._validate_result(provider_request, result), request_binding

    async def _current_selection(
        self,
        request: VideoGenerationRequest,
        authorization_current: AuthorizationCheck,
    ) -> tuple[ProviderResolution, bool]:
        await self._require_authorized(authorization_current)
        registry = self.registry
        if registry is None:
            raise VideoGenerationUnavailableError("video generation provider is not configured")
        resolution = registry.resolve(
            LogicalCapability.VIDEO_GENERATION,
            actor_level=RbacLevel.TRUSTED,
            quality_tier=request.tier,
            consent_verified=True,
        )
        if not resolution.ready or resolution.provider_id is None:
            raise VideoGenerationUnavailableError("video generation provider is unavailable")
        provider = registry.manifest.provider(resolution.provider_id)
        if provider is None:
            raise VideoGenerationUnavailableError("video generation provider disappeared")
        consent_verified = True
        if provider.kind is ProviderKind.API:
            consent_verified = await self._remote_consent_current(request.actor_id)
            if not consent_verified:
                raise VideoGenerationUnavailableError("remote video generation consent is required")
        return resolution, consent_verified

    async def _read_validated(
        self,
        artifact: ArtifactRef,
        *,
        request_binding: str,
        request: VideoGenerationRequest,
        authorization_current: AuthorizationCheck,
    ) -> GeneratedVideo:
        selection, _ = await self._current_selection(request, authorization_current)
        if request_binding != self._binding(self._provider_request(request), selection):
            raise VideoGenerationAuthorizationError("video provider route changed")
        store = self.artifact_store
        if store is None:
            raise VideoGenerationUnavailableError("video artifact store is not configured")
        try:
            mp4 = store.read_mp4(
                artifact,
                request_binding=request_binding,
                read_allowed=lambda: not self._closing,
            )
        except Exception:
            raise VideoGenerationContractError("video artifact could not be verified") from None
        post_selection, _ = await self._current_selection(request, authorization_current)
        if request_binding != self._binding(self._provider_request(request), post_selection):
            raise VideoGenerationAuthorizationError("video provider route changed")
        if (
            not isinstance(mp4, bytes)
            or artifact.size_bytes != len(mp4)
            or artifact.sha256 != hashlib.sha256(mp4).hexdigest()
        ):
            raise VideoGenerationContractError("video artifact integrity mismatch")
        return GeneratedVideo(artifact=artifact, mp4=mp4)

    @staticmethod
    def _provider_request(request: VideoGenerationRequest) -> ProviderRequest:
        return ProviderRequest(
            request_id=request.provider_request_id,
            trace_id=request.trace_id,
            capability=LogicalCapability.VIDEO_GENERATION,
            actor_ref=request.actor_ref,
            payload=MediaGenerationInput(prompt=request.prompt),
            quality_tier=request.tier,
        )

    @staticmethod
    def _binding(request: ProviderRequest, resolution: ProviderResolution) -> str:
        if resolution.provider_id is None:
            raise VideoGenerationUnavailableError("video generation provider is unavailable")
        return video_artifact_request_binding(
            request,
            provider_id=resolution.provider_id,
            provider_model=resolution.provider_model,
            model_alias=resolution.model_alias,
            quality_tier=resolution.quality_tier,
        )

    async def _require_authorized(self, authorization_current: AuthorizationCheck) -> None:
        if self._closing:
            raise VideoGenerationAuthorizationError("video generation is closing")
        try:
            allowed = authorization_current()
            if inspect.isawaitable(allowed):
                allowed = await allowed
        except asyncio.CancelledError:
            raise
        except Exception:
            allowed = False
        if self._closing or allowed is not True:
            raise VideoGenerationAuthorizationError("video generation authorization changed")

    async def _remote_consent_current(self, actor_id: int) -> bool:
        check = self.remote_consent_active
        if check is None:
            return False
        try:
            allowed = check(actor_id)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            return allowed is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    @staticmethod
    def _validate_result(request: ProviderRequest, result: ProviderResult) -> ArtifactRef:
        if (
            not isinstance(result, ProviderResult)
            or result.request_id != request.request_id
            or result.text
            or len(result.artifacts) != 1
        ):
            raise VideoGenerationContractError("provider returned an invalid video result")
        artifact = result.artifacts[0]
        if (
            artifact.kind is not ArtifactKind.VIDEO
            or artifact.media_type != "video/mp4"
            or artifact.size_bytes is None
            or artifact.size_bytes <= 0
            or artifact.sha256 is None
        ):
            raise VideoGenerationContractError("provider returned an invalid video artifact")
        return artifact


__all__ = ["VideoGenerationService"]
