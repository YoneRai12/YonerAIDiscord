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

from .artifacts import validate_wav
from .domain import (
    GeneratedMusic,
    MusicGenerationAuthorizationError,
    MusicGenerationContractError,
    MusicGenerationIdempotencyError,
    MusicGenerationRequest,
    MusicGenerationUnavailableError,
    music_artifact_request_binding,
)
from .ports import (
    AuthorizationCheck,
    MusicArtifactStore,
    MusicExecutionProofIssuer,
    RemoteConsentCheck,
)


@dataclass(frozen=True, slots=True)
class _CachedMusic:
    fingerprint: str
    artifact: ArtifactRef
    request_binding: str


class MusicGenerationService:
    """原創・インストゥルメンタル短尺previewだけを扱う、provider-neutralな境界。"""

    def __init__(
        self,
        registry: ProviderRegistry | None,
        artifact_store: MusicArtifactStore | None,
        *,
        remote_consent_active: RemoteConsentCheck | None = None,
        execution_proofs: MusicExecutionProofIssuer | None = None,
        max_idempotency_entries: int = 256,
    ) -> None:
        if registry is not None and not isinstance(registry, ProviderRegistry):
            raise TypeError("registry must be a ProviderRegistry or None")
        if (
            isinstance(max_idempotency_entries, bool)
            or not isinstance(max_idempotency_entries, int)
            or not 1 <= max_idempotency_entries <= 4096
        ):
            raise ValueError("max_idempotency_entries is outside the allowed range")
        if execution_proofs is not None and (
            not callable(getattr(execution_proofs, "issue", None))
            or not callable(getattr(execution_proofs, "revoke", None))
        ):
            raise TypeError("execution_proofs must provide issue and revoke")
        self.registry, self.artifact_store, self.remote_consent_active = registry, artifact_store, remote_consent_active
        self.execution_proofs = execution_proofs
        self.max_idempotency_entries, self._lock, self._completed, self._closing = (
            max_idempotency_entries,
            asyncio.Lock(),
            {},
            False,
        )

    def begin_close(self) -> None:
        self._closing = True

    async def generate(
        self, request: MusicGenerationRequest, *, authorization_current: AuthorizationCheck
    ) -> GeneratedMusic:
        if not isinstance(request, MusicGenerationRequest):
            raise TypeError("request must be a MusicGenerationRequest")
        if not callable(authorization_current):
            raise TypeError("authorization_current is required")
        async with self._lock:
            await self._require_authorized(authorization_current)
            cached = self._completed.get(request.request_id)
            if cached:
                if cached.fingerprint != request.fingerprint:
                    raise MusicGenerationIdempotencyError("request_id was reused for a different request")
                return await self._read(cached.artifact, cached.request_binding, request, authorization_current)
            registry, store = self.registry, self.artifact_store
            if registry is None or store is None:
                raise MusicGenerationUnavailableError("music generation provider or store is not configured")
            selection, consent = await self._selection(request, authorization_current)
            provider_request = self._provider_request(request)
            binding = self._binding(provider_request, selection, request)

            async def allowed() -> bool:
                try:
                    current, now_consent = await self._selection(request, authorization_current)
                    return now_consent == consent and self._same_route(current, selection)
                except (MusicGenerationAuthorizationError, MusicGenerationUnavailableError):
                    return False

            proof_token: object | None = None
            try:
                proofs = self.execution_proofs
                if proofs is not None:
                    proof_token = proofs.issue(request, provider_request, selection)
                result = await registry.execute(
                    provider_request,
                    actor_level=RbacLevel.TRUSTED,
                    consent_verified=consent,
                    execution_allowed=allowed,
                )
            except ProviderUnavailableError:
                raise MusicGenerationUnavailableError("music generation provider is unavailable") from None
            except MusicGenerationAuthorizationError:
                raise
            except Exception:
                raise MusicGenerationUnavailableError("music generation failed safely") from None
            finally:
                if proof_token is not None:
                    try:
                        assert self.execution_proofs is not None
                        self.execution_proofs.revoke(proof_token)
                    except Exception:
                        raise MusicGenerationAuthorizationError("music execution proof cleanup failed") from None
            await self._require_authorized(authorization_current)
            if not await allowed():
                raise MusicGenerationAuthorizationError("authorization changed after provider execution")
            artifact = self._validate_result(provider_request, result)
            self._completed[request.request_id] = _CachedMusic(request.fingerprint, artifact, binding)
            while len(self._completed) > self.max_idempotency_entries:
                self._completed.pop(next(iter(self._completed)))
            return await self._read(artifact, binding, request, authorization_current)

    async def delivery_current(
        self, request: MusicGenerationRequest, generated: GeneratedMusic, *, authorization_current: AuthorizationCheck
    ) -> bool:
        try:
            cached = self._completed.get(request.request_id)
            if cached is None or cached.artifact != generated.artifact:
                return False
            current, _ = await self._selection(request, authorization_current)
            return (
                cached.request_binding == self._binding(self._provider_request(request), current, request)
                and generated.artifact.size_bytes == len(generated.wav)
                and generated.artifact.sha256 == hashlib.sha256(generated.wav).hexdigest()
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _selection(
        self, request: MusicGenerationRequest, authorization: AuthorizationCheck
    ) -> tuple[ProviderResolution, bool]:
        await self._require_authorized(authorization)
        if self.registry is None:
            raise MusicGenerationUnavailableError("music generation provider is not configured")
        resolution = self.registry.resolve(
            LogicalCapability.MUSIC_GENERATION,
            actor_level=RbacLevel.TRUSTED,
            quality_tier=request.tier,
            consent_verified=True,
        )
        if not resolution.ready or resolution.provider_id is None:
            raise MusicGenerationUnavailableError("music generation provider is unavailable")
        provider = self.registry.manifest.provider(resolution.provider_id)
        if provider is None:
            raise MusicGenerationUnavailableError("music generation provider disappeared")
        consent = True
        if provider.kind is ProviderKind.API:
            consent = await self._remote_consent(request.actor_id)
            if not consent:
                raise MusicGenerationUnavailableError("remote music generation consent is required")
        return resolution, consent

    async def _read(
        self, artifact: ArtifactRef, binding: str, request: MusicGenerationRequest, authorization: AuthorizationCheck
    ) -> GeneratedMusic:
        selection, _ = await self._selection(request, authorization)
        if binding != self._binding(self._provider_request(request), selection, request):
            raise MusicGenerationAuthorizationError("music provider route changed")
        if self.artifact_store is None:
            raise MusicGenerationUnavailableError("music artifact store is not configured")
        try:
            wav = self.artifact_store.read_wav(
                artifact, request_binding=binding, read_allowed=lambda: not self._closing
            )
        except Exception:
            raise MusicGenerationContractError("music artifact could not be verified") from None
        post, _ = await self._selection(request, authorization)
        if binding != self._binding(self._provider_request(request), post, request):
            raise MusicGenerationAuthorizationError("music provider route changed")
        if (
            not isinstance(wav, bytes)
            or artifact.size_bytes != len(wav)
            or artifact.sha256 != hashlib.sha256(wav).hexdigest()
        ):
            raise MusicGenerationContractError("music artifact integrity mismatch")
        if validate_wav(wav).duration_seconds != request.duration_seconds:
            raise MusicGenerationContractError("music artifact duration does not match the request")
        return GeneratedMusic(artifact, wav)

    @staticmethod
    def _provider_request(request: MusicGenerationRequest) -> ProviderRequest:
        return ProviderRequest(
            request_id=request.provider_request_id,
            trace_id=request.trace_id,
            capability=LogicalCapability.MUSIC_GENERATION,
            actor_ref=request.actor_ref,
            payload=MediaGenerationInput(prompt=request.prompt, duration_seconds=request.duration_seconds),
            quality_tier=request.tier,
        )

    @staticmethod
    def _same_route(left: ProviderResolution, right: ProviderResolution) -> bool:
        return (left.provider_id, left.provider_model, left.model_alias, left.quality_tier) == (
            right.provider_id,
            right.provider_model,
            right.model_alias,
            right.quality_tier,
        )

    @staticmethod
    def _binding(
        provider_request: ProviderRequest, resolution: ProviderResolution, request: MusicGenerationRequest
    ) -> str:
        if resolution.provider_id is None:
            raise MusicGenerationUnavailableError("music generation provider is unavailable")
        return music_artifact_request_binding(
            provider_request,
            provider_id=resolution.provider_id,
            provider_model=resolution.provider_model,
            model_alias=resolution.model_alias,
            quality_tier=resolution.quality_tier,
        )

    async def _require_authorized(self, check: AuthorizationCheck) -> None:
        if self._closing:
            raise MusicGenerationAuthorizationError("music generation is closing")
        try:
            value = check()
            value = await value if inspect.isawaitable(value) else value
        except asyncio.CancelledError:
            raise
        except Exception:
            value = False
        if self._closing or value is not True:
            raise MusicGenerationAuthorizationError("music generation authorization changed")

    async def _remote_consent(self, actor_id: int) -> bool:
        if self.remote_consent_active is None:
            return False
        try:
            value = self.remote_consent_active(actor_id)
            value = await value if inspect.isawaitable(value) else value
            return value is True
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
            raise MusicGenerationContractError("provider returned an invalid music result")
        artifact = result.artifacts[0]
        if (
            artifact.kind is not ArtifactKind.AUDIO
            or artifact.media_type != "audio/wav"
            or artifact.size_bytes is None
            or artifact.size_bytes <= 0
            or artifact.sha256 is None
        ):
            raise MusicGenerationContractError("provider returned an invalid WAV artifact")
        return artifact
