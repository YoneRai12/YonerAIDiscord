from __future__ import annotations

import asyncio
import hashlib
import inspect
from dataclasses import dataclass

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.provider_registry import (
    LogicalCapability,
    ProviderExecutionDeniedError,
    ProviderKind,
    ProviderRegistry,
    ProviderRequest,
    ProviderResolution,
    ProviderResult,
    ProviderUnavailableError,
    SpeechTranscriptionInput,
)

from .domain import (
    MAX_TRANSCRIPT_CHARS,
    SpeechTranscript,
    SpeechTranscriptionAuthorizationError,
    SpeechTranscriptionContractError,
    SpeechTranscriptionIdempotencyError,
    SpeechTranscriptionRequest,
    SpeechTranscriptionUnavailableError,
    normalize_transcript_text,
)
from .ports import AudioArtifactCheck, AuthorizationCheck, RemoteConsentCheck


@dataclass(frozen=True, slots=True)
class _CachedTranscript:
    fingerprint: str
    transcript: SpeechTranscript
    route_binding: str


class SpeechTranscriptionService:
    """検証済みAUDIO ArtifactRefだけをprovider-neutral STTへ渡す。"""

    def __init__(
        self,
        registry: ProviderRegistry | None,
        *,
        audio_artifact_current: AudioArtifactCheck | None = None,
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
        self.audio_artifact_current = audio_artifact_current
        self.remote_consent_active = remote_consent_active
        self.max_idempotency_entries = max_idempotency_entries
        self._lock = asyncio.Lock()
        self._completed: dict[str, _CachedTranscript] = {}
        self._delivered: set[str] = set()
        self._closing = False

    def begin_close(self) -> None:
        self._closing = True

    async def transcribe(
        self,
        request: SpeechTranscriptionRequest,
        *,
        authorization_current: AuthorizationCheck,
    ) -> SpeechTranscript:
        if not isinstance(request, SpeechTranscriptionRequest):
            raise TypeError("request must be a SpeechTranscriptionRequest")
        if not callable(authorization_current):
            raise TypeError("authorization_current is required")
        async with self._lock:
            selection, consent = await self._state(request, authorization_current)
            route_binding = self._route_binding(request, selection)
            cached = self._completed.get(request.request_id)
            if cached is not None:
                if cached.fingerprint != request.fingerprint:
                    raise SpeechTranscriptionIdempotencyError("request_id was reused for a different request")
                if cached.route_binding != route_binding:
                    raise SpeechTranscriptionAuthorizationError("speech transcription route changed")
                return cached.transcript

            registry = self.registry
            if registry is None:
                raise SpeechTranscriptionUnavailableError("speech transcription provider is not configured")
            provider_request = self._provider_request(request)

            async def allowed() -> bool:
                try:
                    current, now_consent = await self._state(request, authorization_current)
                    return now_consent == consent and self._same_route(current, selection)
                except (
                    SpeechTranscriptionAuthorizationError,
                    SpeechTranscriptionUnavailableError,
                ):
                    return False

            try:
                result = await registry.execute(
                    provider_request,
                    actor_level=RbacLevel.TRUSTED,
                    consent_verified=consent,
                    execution_allowed=allowed,
                )
            except ProviderExecutionDeniedError:
                raise SpeechTranscriptionAuthorizationError("speech transcription authorization changed") from None
            except ProviderUnavailableError:
                raise SpeechTranscriptionUnavailableError("speech transcription provider is unavailable") from None
            except Exception:
                raise SpeechTranscriptionUnavailableError("speech transcription failed safely") from None

            current, now_consent = await self._state(request, authorization_current)
            if now_consent != consent or not self._same_route(current, selection):
                raise SpeechTranscriptionAuthorizationError("speech transcription route changed")
            text = self._validate_result(provider_request, result)
            transcript = SpeechTranscript(
                request.request_id,
                text,
                route_binding,
            )
            self._completed[request.request_id] = _CachedTranscript(
                request.fingerprint,
                transcript,
                route_binding,
            )
            while len(self._completed) > self.max_idempotency_entries:
                evicted = next(iter(self._completed))
                self._completed.pop(evicted)
                self._delivered.discard(evicted)
            return transcript

    async def claim_delivery(
        self,
        request: SpeechTranscriptionRequest,
        transcript: SpeechTranscript,
        *,
        authorization_current: AuthorizationCheck,
    ) -> bool:
        if not isinstance(request, SpeechTranscriptionRequest) or not isinstance(transcript, SpeechTranscript):
            return False
        try:
            async with self._lock:
                selection, _ = await self._state(request, authorization_current)
                cached = self._completed.get(request.request_id)
                if (
                    cached is None
                    or cached.fingerprint != request.fingerprint
                    or cached.transcript is not transcript
                    or cached.route_binding != self._route_binding(request, selection)
                    or request.request_id in self._delivered
                ):
                    return False
                self._delivered.add(request.request_id)
                return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def delivery_current(
        self,
        request: SpeechTranscriptionRequest,
        transcript: SpeechTranscript,
        *,
        authorization_current: AuthorizationCheck,
    ) -> bool:
        """送信直前にscope、artifact、route、health、consentをまとめて再照合する。"""

        if not isinstance(request, SpeechTranscriptionRequest) or not isinstance(transcript, SpeechTranscript):
            return False
        try:
            async with self._lock:
                selection, _ = await self._state(request, authorization_current)
                cached = self._completed.get(request.request_id)
                return (
                    cached is not None
                    and cached.fingerprint == request.fingerprint
                    and cached.transcript is transcript
                    and cached.route_binding == self._route_binding(request, selection)
                    and request.request_id in self._delivered
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _state(
        self,
        request: SpeechTranscriptionRequest,
        authorization: AuthorizationCheck,
    ) -> tuple[ProviderResolution, bool]:
        await self._require_authorized(authorization)
        await self._require_audio(request)
        registry = self.registry
        if registry is None:
            raise SpeechTranscriptionUnavailableError("speech transcription provider is not configured")
        resolution = registry.resolve(
            LogicalCapability.SPEECH_STT,
            actor_level=RbacLevel.TRUSTED,
            quality_tier=request.tier,
            consent_verified=True,
        )
        if not resolution.ready or resolution.provider_id is None:
            raise SpeechTranscriptionUnavailableError("speech transcription provider is unavailable")
        provider = registry.manifest.provider(resolution.provider_id)
        if provider is None:
            raise SpeechTranscriptionUnavailableError("speech transcription provider disappeared")
        consent = True
        if provider.kind is ProviderKind.API:
            consent = await self._remote_consent(request.actor_id)
            if not consent:
                raise SpeechTranscriptionUnavailableError("remote speech transcription consent is required")
        return resolution, consent

    async def _require_audio(self, request: SpeechTranscriptionRequest) -> None:
        check = self.audio_artifact_current
        if check is None:
            raise SpeechTranscriptionUnavailableError("audio artifact authorization is not configured")
        try:
            value = check(request.audio, request.audio_binding)
            value = await value if inspect.isawaitable(value) else value
        except asyncio.CancelledError:
            raise
        except Exception:
            value = False
        if value is not True:
            raise SpeechTranscriptionAuthorizationError("audio artifact authorization changed")

    async def _require_authorized(self, check: AuthorizationCheck) -> None:
        if self._closing:
            raise SpeechTranscriptionAuthorizationError("speech transcription is closing")
        try:
            value = check()
            value = await value if inspect.isawaitable(value) else value
        except asyncio.CancelledError:
            raise
        except Exception:
            value = False
        if self._closing or value is not True:
            raise SpeechTranscriptionAuthorizationError("speech transcription authorization changed")

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
    def _provider_request(request: SpeechTranscriptionRequest) -> ProviderRequest:
        return ProviderRequest(
            request_id=request.provider_request_id,
            trace_id=request.trace_id,
            capability=LogicalCapability.SPEECH_STT,
            actor_ref=request.actor_ref,
            payload=SpeechTranscriptionInput(
                language_code=request.language_code,
                prompt=request.prompt,
            ),
            quality_tier=request.tier,
            input_artifacts=(request.audio,),
        )

    @staticmethod
    def _same_route(
        left: ProviderResolution,
        right: ProviderResolution,
    ) -> bool:
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
    def _route_binding(
        request: SpeechTranscriptionRequest,
        resolution: ProviderResolution,
    ) -> str:
        if resolution.provider_id is None:
            raise SpeechTranscriptionUnavailableError("speech transcription provider is unavailable")
        return hashlib.sha256(
            "\0".join(
                (
                    request.fingerprint,
                    resolution.provider_id,
                    resolution.provider_model or "",
                    resolution.model_alias or "",
                    resolution.quality_tier.value,
                )
            ).encode()
        ).hexdigest()

    @staticmethod
    def _validate_result(
        request: ProviderRequest,
        result: ProviderResult,
    ) -> str:
        if (
            not isinstance(result, ProviderResult)
            or result.request_id != request.request_id
            or result.artifacts
            or not result.text
        ):
            raise SpeechTranscriptionContractError("provider returned an invalid transcription result")
        try:
            text = normalize_transcript_text(result.text)
        except (TypeError, ValueError):
            raise SpeechTranscriptionContractError("provider returned an invalid transcription result") from None
        if len(text) > MAX_TRANSCRIPT_CHARS:
            raise SpeechTranscriptionContractError("provider returned an invalid transcription result")
        return text
