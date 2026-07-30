from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.image_generation.artifacts import canonicalize_png
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    ImageEditingInput,
    LogicalCapability,
    ProviderExecutionDeniedError,
    ProviderKind,
    ProviderRegistry,
    ProviderRequest,
    ProviderResolution,
    ProviderResult,
    ProviderUnavailableError,
)

from .domain import (
    EditedImage,
    ImageEditSource,
    ImageEditingAuthorizationError,
    ImageEditingContractError,
    ImageEditingError,
    ImageEditingIdempotencyError,
    ImageEditingRequest,
    ImageEditingUnavailableError,
    image_edit_output_binding,
)
from .ports import (
    AuthorizationCheck,
    ImageArtifactStore,
    RemoteConsentCheck,
    SourceArtifactCheck,
)


@dataclass(frozen=True, slots=True)
class _CachedEdit:
    fingerprint: str
    edited: EditedImage
    output_binding: str


class ImageEditingService:
    """検証済みPNG 1件を、新IDのcanonical PNG 1件へ編集する境界。"""

    def __init__(
        self,
        registry: ProviderRegistry | None,
        artifact_store: ImageArtifactStore | None,
        *,
        source_artifact_current: SourceArtifactCheck | None = None,
        remote_consent_active: RemoteConsentCheck | None = None,
        max_idempotency_entries: int = 256,
    ) -> None:
        if registry is not None and not isinstance(registry, ProviderRegistry):
            raise TypeError("registry must be a ProviderRegistry or None")
        if (
            isinstance(max_idempotency_entries, bool)
            or not isinstance(max_idempotency_entries, int)
            or not 1 <= max_idempotency_entries <= 4_096
        ):
            raise ValueError("max_idempotency_entries is outside the allowed range")
        self.registry = registry
        self.artifact_store = artifact_store
        self.source_artifact_current = source_artifact_current
        self.remote_consent_active = remote_consent_active
        self.max_idempotency_entries = max_idempotency_entries
        self._lock = asyncio.Lock()
        self._completed: dict[str, _CachedEdit] = {}
        self._delivered: set[str] = set()
        self._closing = False

    def begin_close(self) -> None:
        self._closing = True

    async def edit(
        self,
        request: ImageEditingRequest,
        *,
        authorization_current: AuthorizationCheck,
    ) -> EditedImage:
        if not isinstance(request, ImageEditingRequest):
            raise TypeError("request must be an ImageEditingRequest")
        if not callable(authorization_current):
            raise TypeError("authorization_current is required")
        async with self._lock:
            selection, consent = await self._state(request, authorization_current)
            with self._protect_source(request):
                await self._read_source(
                    request,
                    authorization_current=authorization_current,
                    expected_selection=selection,
                    expected_consent=consent,
                )
                cached = self._completed.get(request.request_id)
                if cached is not None:
                    if cached.fingerprint != request.fingerprint:
                        raise ImageEditingIdempotencyError("request_id was reused for a different request")
                    png = await self._read_output(
                        request,
                        cached.edited.artifact,
                        output_binding=cached.output_binding,
                        authorization_current=authorization_current,
                        expected_selection=selection,
                        expected_consent=consent,
                    )
                    if png != cached.edited.png:
                        raise ImageEditingContractError("edited image bytes changed")
                    return cached.edited

                edited, output_binding = await self._edit_once(
                    request,
                    authorization_current=authorization_current,
                    selection=selection,
                    consent=consent,
                )
                self._completed[request.request_id] = _CachedEdit(
                    request.fingerprint,
                    edited,
                    output_binding,
                )
                while len(self._completed) > self.max_idempotency_entries:
                    evicted = next(iter(self._completed))
                    self._completed.pop(evicted)
                    self._delivered.discard(evicted)
                return edited

    @contextmanager
    def _protect_source(self, request: ImageEditingRequest) -> Iterator[None]:
        store = self.artifact_store
        protect = getattr(store, "protect_png", None)
        if store is None or not callable(protect):
            raise ImageEditingUnavailableError("image artifact source protection is not configured")
        try:
            with protect(
                request.source.artifact,
                request_binding=request.source.source_binding,
            ):
                yield
        except ImageEditingError:
            raise
        except Exception:
            raise ImageEditingAuthorizationError("source image authorization changed") from None

    async def claim_delivery(
        self,
        request: ImageEditingRequest,
        edited: EditedImage,
        *,
        authorization_current: AuthorizationCheck,
    ) -> bool:
        if not isinstance(request, ImageEditingRequest) or not isinstance(edited, EditedImage):
            return False
        try:
            async with self._lock:
                selection, consent = await self._state(request, authorization_current)
                cached = self._completed.get(request.request_id)
                if (
                    cached is None
                    or cached.fingerprint != request.fingerprint
                    or cached.edited is not edited
                    or request.request_id in self._delivered
                    or cached.output_binding != self._binding(self._provider_request(request), selection)
                ):
                    return False
                png = await self._read_output(
                    request,
                    edited.artifact,
                    output_binding=cached.output_binding,
                    authorization_current=authorization_current,
                    expected_selection=selection,
                    expected_consent=consent,
                )
                if png != edited.png:
                    return False
                self._delivered.add(request.request_id)
                return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def delivery_current(
        self,
        request: ImageEditingRequest,
        edited: EditedImage,
        *,
        authorization_current: AuthorizationCheck,
    ) -> bool:
        if not isinstance(request, ImageEditingRequest) or not isinstance(edited, EditedImage):
            return False
        try:
            async with self._lock:
                selection, consent = await self._state(request, authorization_current)
                cached = self._completed.get(request.request_id)
                if (
                    cached is None
                    or cached.fingerprint != request.fingerprint
                    or cached.edited is not edited
                    or request.request_id not in self._delivered
                    or cached.output_binding != self._binding(self._provider_request(request), selection)
                ):
                    return False
                png = await self._read_output(
                    request,
                    edited.artifact,
                    output_binding=cached.output_binding,
                    authorization_current=authorization_current,
                    expected_selection=selection,
                    expected_consent=consent,
                )
                return png == edited.png
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _edit_once(
        self,
        request: ImageEditingRequest,
        *,
        authorization_current: AuthorizationCheck,
        selection: ProviderResolution,
        consent: bool,
    ) -> tuple[EditedImage, str]:
        registry = self.registry
        if registry is None:
            raise ImageEditingUnavailableError("image editing provider is not configured")
        provider_request = self._provider_request(request)
        output_binding = self._binding(provider_request, selection)

        async def execution_allowed() -> bool:
            try:
                current, now_consent = await self._state(request, authorization_current)
                return now_consent == consent and self._same_route(current, selection)
            except (
                ImageEditingAuthorizationError,
                ImageEditingUnavailableError,
            ):
                return False

        try:
            result = await registry.execute(
                provider_request,
                actor_level=RbacLevel.TRUSTED,
                consent_verified=consent,
                execution_allowed=execution_allowed,
            )
        except ProviderExecutionDeniedError:
            raise ImageEditingAuthorizationError("image editing authorization changed") from None
        except ProviderUnavailableError:
            raise ImageEditingUnavailableError("image editing provider is unavailable") from None
        except Exception:
            raise ImageEditingUnavailableError("image editing failed safely") from None

        current, now_consent = await self._state(request, authorization_current)
        if now_consent != consent or not self._same_route(current, selection):
            raise ImageEditingAuthorizationError("image editing route changed")
        artifact = self._validate_result(provider_request, result, request.source.artifact)
        png = await self._read_output(
            request,
            artifact,
            output_binding=output_binding,
            authorization_current=authorization_current,
            expected_selection=selection,
            expected_consent=consent,
        )
        return EditedImage(artifact, png), output_binding

    async def _read_source(
        self,
        request: ImageEditingRequest,
        *,
        authorization_current: AuthorizationCheck,
        expected_selection: ProviderResolution,
        expected_consent: bool,
    ) -> bytes:
        store = self.artifact_store
        if store is None:
            raise ImageEditingUnavailableError("image artifact store is not configured")
        before, consent = await self._state(request, authorization_current)
        if consent != expected_consent or not self._same_route(before, expected_selection):
            raise ImageEditingAuthorizationError("image editing route changed")
        try:
            png = store.read_png(
                request.source.artifact,
                request_binding=request.source.source_binding,
                read_allowed=lambda: not self._closing,
            )
        except Exception:
            raise ImageEditingAuthorizationError("source image authorization changed") from None
        after, consent = await self._state(request, authorization_current)
        if consent != expected_consent or not self._same_route(after, expected_selection):
            raise ImageEditingAuthorizationError("image editing route changed")
        canonical = canonicalize_png(png)
        if (
            canonical.data != png
            or request.source.artifact.size_bytes != len(png)
            or request.source.artifact.sha256 != canonical.sha256
        ):
            raise ImageEditingContractError("source image integrity mismatch")
        return png

    async def _read_output(
        self,
        request: ImageEditingRequest,
        artifact: ArtifactRef,
        *,
        output_binding: str,
        authorization_current: AuthorizationCheck,
        expected_selection: ProviderResolution,
        expected_consent: bool,
    ) -> bytes:
        store = self.artifact_store
        if store is None:
            raise ImageEditingUnavailableError("image artifact store is not configured")
        before, consent = await self._state(request, authorization_current)
        if (
            consent != expected_consent
            or not self._same_route(before, expected_selection)
            or output_binding != self._binding(self._provider_request(request), before)
        ):
            raise ImageEditingAuthorizationError("image editing route changed")
        try:
            png = store.read_png(
                artifact,
                request_binding=output_binding,
                read_allowed=lambda: not self._closing,
            )
        except Exception:
            raise ImageEditingContractError("edited image could not be verified") from None
        after, consent = await self._state(request, authorization_current)
        if (
            consent != expected_consent
            or not self._same_route(after, expected_selection)
            or output_binding != self._binding(self._provider_request(request), after)
        ):
            raise ImageEditingAuthorizationError("image editing route changed")
        canonical = canonicalize_png(png)
        if canonical.data != png or artifact.size_bytes != len(png) or artifact.sha256 != canonical.sha256:
            raise ImageEditingContractError("edited image integrity mismatch")
        return png

    async def _state(
        self,
        request: ImageEditingRequest,
        authorization_current: AuthorizationCheck,
    ) -> tuple[ProviderResolution, bool]:
        await self._require_authorized(authorization_current)
        await self._require_source_current(request.source)
        registry = self.registry
        if registry is None:
            raise ImageEditingUnavailableError("image editing provider is not configured")
        resolution = registry.resolve(
            LogicalCapability.IMAGE_EDITING,
            actor_level=RbacLevel.TRUSTED,
            quality_tier=request.tier,
            consent_verified=True,
        )
        if not resolution.ready or resolution.provider_id is None:
            raise ImageEditingUnavailableError("image editing provider is unavailable")
        provider = registry.manifest.provider(resolution.provider_id)
        if provider is None:
            raise ImageEditingUnavailableError("image editing provider disappeared")
        consent = True
        if provider.kind is ProviderKind.API:
            consent = await self._remote_consent(request.actor_id)
            if not consent:
                raise ImageEditingUnavailableError("remote image editing consent is required")
        return resolution, consent

    async def _require_source_current(self, source: ImageEditSource) -> None:
        check = self.source_artifact_current
        if check is None:
            raise ImageEditingUnavailableError("source image authorization is not configured")
        try:
            value = check(source)
            value = await value if inspect.isawaitable(value) else value
        except asyncio.CancelledError:
            raise
        except Exception:
            value = False
        if value is not True:
            raise ImageEditingAuthorizationError("source image authorization changed")

    async def _require_authorized(self, check: AuthorizationCheck) -> None:
        if self._closing:
            raise ImageEditingAuthorizationError("image editing is closing")
        try:
            value = check()
            value = await value if inspect.isawaitable(value) else value
        except asyncio.CancelledError:
            raise
        except Exception:
            value = False
        if self._closing or value is not True:
            raise ImageEditingAuthorizationError("image editing authorization changed")

    async def _remote_consent(self, actor_id: int) -> bool:
        check = self.remote_consent_active
        if check is None:
            return False
        try:
            value = check(actor_id)
            value = await value if inspect.isawaitable(value) else value
            return value is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    @staticmethod
    def _provider_request(request: ImageEditingRequest) -> ProviderRequest:
        return ProviderRequest(
            request_id=request.provider_request_id,
            trace_id=request.trace_id,
            capability=LogicalCapability.IMAGE_EDITING,
            actor_ref=request.actor_ref,
            payload=ImageEditingInput(
                request.instruction,
                source_binding_digest=request.source.source_binding,
            ),
            quality_tier=request.tier,
            input_artifacts=(request.source.artifact,),
        )

    @staticmethod
    def _binding(request: ProviderRequest, resolution: ProviderResolution) -> str:
        if resolution.provider_id is None:
            raise ImageEditingUnavailableError("image editing provider is unavailable")
        return image_edit_output_binding(
            request,
            provider_id=resolution.provider_id,
            provider_model=resolution.provider_model,
            model_alias=resolution.model_alias,
            quality_tier=resolution.quality_tier,
        )

    @staticmethod
    def _same_route(left: ProviderResolution, right: ProviderResolution) -> bool:
        return (
            left.provider_id,
            left.provider_model,
            left.model_alias,
            left.quality_tier,
        ) == (
            right.provider_id,
            right.provider_model,
            right.model_alias,
            right.quality_tier,
        )

    @staticmethod
    def _validate_result(
        request: ProviderRequest,
        result: ProviderResult,
        source: ArtifactRef,
    ) -> ArtifactRef:
        if (
            not isinstance(result, ProviderResult)
            or result.request_id != request.request_id
            or result.text
            or len(result.artifacts) != 1
        ):
            raise ImageEditingContractError("provider returned an invalid image edit result")
        artifact = result.artifacts[0]
        if (
            artifact.artifact_id == source.artifact_id
            or artifact.kind is not ArtifactKind.IMAGE
            or artifact.media_type != "image/png"
            or artifact.size_bytes is None
            or artifact.size_bytes <= 0
            or artifact.sha256 is None
        ):
            raise ImageEditingContractError("provider returned an invalid image edit artifact")
        return artifact


__all__ = ["ImageEditingService"]
