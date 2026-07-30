from __future__ import annotations

import asyncio
import hashlib
import inspect
from dataclasses import dataclass

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.music_generation.artifacts import validate_wav
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    LogicalCapability,
    ProviderExecutionDeniedError,
    ProviderKind,
    ProviderRegistry,
    ProviderRequest,
    ProviderResolution,
    ProviderResult,
    ProviderUnavailableError,
    SpeechSynthesisInput,
)

from .domain import (
    SpeechSynthesisAuthorizationError,
    SpeechSynthesisContractError,
    SpeechSynthesisIdempotencyError,
    SpeechSynthesisRequest,
    SpeechSynthesisUnavailableError,
    SynthesizedAudio,
    speech_artifact_request_binding,
)
from .ports import AuthorizationCheck, RemoteConsentCheck, SpeechArtifactStore


@dataclass(frozen=True, slots=True)
class _CachedSpeech:
    fingerprint: str
    synthesized: SynthesizedAudio
    request_binding: str


class SpeechSynthesisService:
    """Standard voice aliasだけをrequest-bound PCM WAVへ変換する。"""

    def __init__(
        self,
        registry: ProviderRegistry | None,
        artifact_store: SpeechArtifactStore | None,
        *,
        remote_consent_active: RemoteConsentCheck | None = None,
        max_idempotency_entries: int = 256,
    ) -> None:
        if registry is not None and not isinstance(registry, ProviderRegistry):
            raise TypeError("registry must be a ProviderRegistry or None")
        if artifact_store is not None and not callable(getattr(artifact_store, "read_wav", None)):
            raise TypeError("artifact_store must provide read_wav")
        if (
            isinstance(max_idempotency_entries, bool)
            or not isinstance(max_idempotency_entries, int)
            or not 1 <= max_idempotency_entries <= 4_096
        ):
            raise ValueError("max_idempotency_entries is outside the allowed range")
        self.registry = registry
        self.artifact_store = artifact_store
        self.remote_consent_active = remote_consent_active
        self.max_idempotency_entries = max_idempotency_entries
        self._lock = asyncio.Lock()
        self._completed: dict[str, _CachedSpeech] = {}
        self._delivered: set[str] = set()
        self._closing = False

    def begin_close(self) -> None:
        self._closing = True

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
        *,
        authorization_current: AuthorizationCheck,
    ) -> SynthesizedAudio:
        if not isinstance(request, SpeechSynthesisRequest):
            raise TypeError("request must be a SpeechSynthesisRequest")
        if not callable(authorization_current):
            raise TypeError("authorization_current is required")
        async with self._lock:
            selection, consent = await self._state(request, authorization_current)
            store = self.artifact_store
            if store is None:
                raise SpeechSynthesisUnavailableError("speech artifact store is not configured")
            provider_request = self._provider_request(request)
            request_binding = self._binding(provider_request, selection)
            cached = self._completed.get(request.request_id)
            if cached is not None:
                if cached.fingerprint != request.fingerprint:
                    raise SpeechSynthesisIdempotencyError("request_id was reused for a different request")
                if cached.request_binding != request_binding:
                    raise SpeechSynthesisAuthorizationError("speech synthesis route changed")
                wav = await self._read_output(
                    request,
                    cached.synthesized.artifact,
                    request_binding=request_binding,
                    authorization_current=authorization_current,
                    expected_selection=selection,
                    expected_consent=consent,
                )
                if wav != cached.synthesized.wav:
                    raise SpeechSynthesisContractError("speech artifact changed")
                return cached.synthesized

            registry = self.registry
            if registry is None:
                raise SpeechSynthesisUnavailableError("speech synthesis provider is not configured")

            async def execution_allowed() -> bool:
                try:
                    current, now_consent = await self._state(request, authorization_current)
                    return now_consent == consent and self._same_route(current, selection)
                except (
                    SpeechSynthesisAuthorizationError,
                    SpeechSynthesisUnavailableError,
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
                raise SpeechSynthesisAuthorizationError("speech synthesis authorization changed") from None
            except ProviderUnavailableError:
                raise SpeechSynthesisUnavailableError("speech synthesis provider is unavailable") from None
            except Exception:
                raise SpeechSynthesisUnavailableError("speech synthesis failed safely") from None

            current, now_consent = await self._state(request, authorization_current)
            if now_consent != consent or not self._same_route(current, selection):
                raise SpeechSynthesisAuthorizationError("speech synthesis route changed")
            artifact = self._validate_result(provider_request, result)
            wav = await self._read_output(
                request,
                artifact,
                request_binding=request_binding,
                authorization_current=authorization_current,
                expected_selection=selection,
                expected_consent=consent,
            )
            synthesized = SynthesizedAudio(artifact, wav, request_binding)
            self._completed[request.request_id] = _CachedSpeech(
                request.fingerprint,
                synthesized,
                request_binding,
            )
            while len(self._completed) > self.max_idempotency_entries:
                evicted = next(iter(self._completed))
                self._completed.pop(evicted)
                self._delivered.discard(evicted)
            return synthesized

    async def claim_delivery(
        self,
        request: SpeechSynthesisRequest,
        synthesized: SynthesizedAudio,
        *,
        authorization_current: AuthorizationCheck,
    ) -> bool:
        if not isinstance(request, SpeechSynthesisRequest) or not isinstance(synthesized, SynthesizedAudio):
            return False
        try:
            async with self._lock:
                selection, consent = await self._state(request, authorization_current)
                cached = self._completed.get(request.request_id)
                if (
                    cached is None
                    or cached.fingerprint != request.fingerprint
                    or cached.synthesized is not synthesized
                    or request.request_id in self._delivered
                    or cached.request_binding != self._binding(self._provider_request(request), selection)
                ):
                    return False
                wav = await self._read_output(
                    request,
                    synthesized.artifact,
                    request_binding=cached.request_binding,
                    authorization_current=authorization_current,
                    expected_selection=selection,
                    expected_consent=consent,
                )
                if wav != synthesized.wav:
                    return False
                self._delivered.add(request.request_id)
                return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def delivery_current(
        self,
        request: SpeechSynthesisRequest,
        synthesized: SynthesizedAudio,
        *,
        authorization_current: AuthorizationCheck,
    ) -> bool:
        if not isinstance(request, SpeechSynthesisRequest) or not isinstance(synthesized, SynthesizedAudio):
            return False
        try:
            async with self._lock:
                selection, consent = await self._state(request, authorization_current)
                cached = self._completed.get(request.request_id)
                if (
                    cached is None
                    or cached.fingerprint != request.fingerprint
                    or cached.synthesized is not synthesized
                    or request.request_id not in self._delivered
                    or cached.request_binding != self._binding(self._provider_request(request), selection)
                ):
                    return False
                wav = await self._read_output(
                    request,
                    synthesized.artifact,
                    request_binding=cached.request_binding,
                    authorization_current=authorization_current,
                    expected_selection=selection,
                    expected_consent=consent,
                )
                return wav == synthesized.wav
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _read_output(
        self,
        request: SpeechSynthesisRequest,
        artifact: ArtifactRef,
        *,
        request_binding: str,
        authorization_current: AuthorizationCheck,
        expected_selection: ProviderResolution,
        expected_consent: bool,
    ) -> bytes:
        before, consent = await self._state(request, authorization_current)
        if consent != expected_consent or not self._same_route(before, expected_selection):
            raise SpeechSynthesisAuthorizationError("speech synthesis route changed")
        store = self.artifact_store
        if store is None:
            raise SpeechSynthesisUnavailableError("speech artifact store is not configured")
        try:
            wav = store.read_wav(
                artifact,
                request_binding=request_binding,
                read_allowed=lambda: not self._closing,
            )
        except Exception:
            raise SpeechSynthesisContractError("speech artifact could not be verified") from None
        after, consent = await self._state(request, authorization_current)
        if consent != expected_consent or not self._same_route(after, expected_selection):
            raise SpeechSynthesisAuthorizationError("speech synthesis route changed")
        if (
            not isinstance(wav, bytes)
            or artifact.size_bytes != len(wav)
            or artifact.sha256 != hashlib.sha256(wav).hexdigest()
        ):
            raise SpeechSynthesisContractError("speech artifact integrity mismatch")
        try:
            validate_wav(wav)
        except Exception:
            raise SpeechSynthesisContractError("speech artifact WAV is invalid") from None
        return wav

    async def _state(
        self,
        request: SpeechSynthesisRequest,
        authorization_current: AuthorizationCheck,
    ) -> tuple[ProviderResolution, bool]:
        await self._require_authorized(authorization_current)
        registry = self.registry
        if registry is None:
            raise SpeechSynthesisUnavailableError("speech synthesis provider is not configured")
        route = registry.manifest.route(LogicalCapability.SPEECH_TTS)
        if route is None:
            raise SpeechSynthesisUnavailableError("speech synthesis provider is unavailable")
        tier_route = route.tier_route(request.tier)
        route_kinds = {
            provider.kind
            for provider_id in tier_route.provider_ids
            if (provider := registry.manifest.provider(provider_id)) is not None
        }
        if len(route_kinds) > 1:
            raise SpeechSynthesisUnavailableError("mixed local and remote fallback is not allowed")
        resolution = registry.resolve(
            LogicalCapability.SPEECH_TTS,
            actor_level=RbacLevel.TRUSTED,
            quality_tier=request.tier,
            consent_verified=True,
        )
        if not resolution.ready or resolution.provider_id is None:
            raise SpeechSynthesisUnavailableError("speech synthesis provider is unavailable")
        provider = registry.manifest.provider(resolution.provider_id)
        if provider is None:
            raise SpeechSynthesisUnavailableError("speech synthesis provider disappeared")
        if route_kinds and provider.kind not in route_kinds:
            raise SpeechSynthesisUnavailableError("speech synthesis route kind changed")
        consent = True
        if provider.kind is ProviderKind.API:
            consent = await self._remote_consent(request.actor_id)
            if not consent:
                raise SpeechSynthesisUnavailableError("remote speech synthesis consent is required")
        return resolution, consent

    async def _require_authorized(self, check: AuthorizationCheck) -> None:
        if self._closing:
            raise SpeechSynthesisAuthorizationError("speech synthesis is closing")
        try:
            value = check()
            value = await value if inspect.isawaitable(value) else value
        except asyncio.CancelledError:
            raise
        except Exception:
            value = False
        if self._closing or value is not True:
            raise SpeechSynthesisAuthorizationError("speech synthesis authorization changed")

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
    def _provider_request(request: SpeechSynthesisRequest) -> ProviderRequest:
        return ProviderRequest(
            request_id=request.provider_request_id,
            trace_id=request.trace_id,
            capability=LogicalCapability.SPEECH_TTS,
            actor_ref=request.actor_ref,
            payload=SpeechSynthesisInput(
                request.text,
                voice_alias=request.voice_alias,
                language_code=request.language_code,
            ),
            quality_tier=request.tier,
            input_artifacts=(),
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
    def _binding(
        provider_request: ProviderRequest,
        resolution: ProviderResolution,
    ) -> str:
        if resolution.provider_id is None:
            raise SpeechSynthesisUnavailableError("speech synthesis provider is unavailable")
        return speech_artifact_request_binding(
            provider_request,
            provider_id=resolution.provider_id,
            provider_model=resolution.provider_model,
            model_alias=resolution.model_alias,
            quality_tier=resolution.quality_tier,
        )

    @staticmethod
    def _validate_result(
        request: ProviderRequest,
        result: ProviderResult,
    ) -> ArtifactRef:
        if (
            not isinstance(result, ProviderResult)
            or result.request_id != request.request_id
            or result.text
            or len(result.artifacts) != 1
        ):
            raise SpeechSynthesisContractError("provider returned an invalid speech synthesis result")
        artifact = result.artifacts[0]
        if (
            artifact.kind is not ArtifactKind.AUDIO
            or artifact.media_type != "audio/wav"
            or artifact.size_bytes is None
            or artifact.size_bytes <= 0
            or artifact.sha256 is None
        ):
            raise SpeechSynthesisContractError("provider returned an invalid WAV artifact")
        return artifact


__all__ = ["SpeechSynthesisService"]
