from __future__ import annotations

import asyncio
import hashlib
import json
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.modules.speech_synthesis.domain import (
    speech_artifact_request_binding,
)
from yonerai_discord.modules.speech_synthesis.provider_voicevox import (
    VOICEVOX_PROVIDER_MODEL,
    VoicevoxSpeechSynthesisProviderAdapter,
    VoicevoxSynthesisRequest,
)
from yonerai_discord.modules.speech_transcription.provider_openai import (
    OPENAI_STT_MODEL,
    AiohttpOpenAITranscriptionTransport,
    OpenAITranscriptionProviderAdapter,
)
from yonerai_discord.modules.voice.voicevox import VoicevoxClient
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    LogicalCapability,
    ProviderExecutionDeniedError,
    ProviderInvocation,
    ProviderRequest,
    QualityTier,
    ResourceProfile,
    SpeechSynthesisInput,
    SpeechTranscriptionInput,
)


def _wav(*, rate: int = 24_000, seconds: int = 1, channels: int = 1) -> bytes:
    block_align = channels * 2
    pcm = b"\0" * (rate * seconds * block_align)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block_align, block_align, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def _invocation(provider_id: str, model: str) -> ProviderInvocation:
    return ProviderInvocation(
        provider_id=provider_id,
        quality_tier=QualityTier.BALANCED,
        model_alias="speech.balanced",
        provider_model=model,
        timeout_seconds=30,
        resources=ResourceProfile.remote(max_concurrency=1),
    )


def _stt_request(audio: bytes | None = None) -> ProviderRequest:
    payload = audio or _wav(rate=44_100)
    ref = ArtifactRef(
        "stt-input-audio",
        ArtifactKind.AUDIO,
        "audio/wav",
        len(payload),
        hashlib.sha256(payload).hexdigest(),
    )
    return ProviderRequest(
        "stt-provider-request",
        "trace-stt-provider",
        LogicalCapability.SPEECH_STT,
        "discord-user-123",
        SpeechTranscriptionInput(language_code="ja-JP", prompt="固有名詞"),
        quality_tier=QualityTier.BALANCED,
        input_artifacts=(ref,),
    )


def _tts_request() -> ProviderRequest:
    return ProviderRequest(
        "tts-provider-request",
        "trace-tts-provider",
        LogicalCapability.SPEECH_TTS,
        "discord-user-123",
        SpeechSynthesisInput("安全な読み上げ", voice_alias="standard", language_code="ja-JP"),
        quality_tier=QualityTier.BALANCED,
    )


class _AudioResolver:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls: list[tuple[ProviderRequest, ArtifactRef]] = []

    def read_audio(self, request: ProviderRequest, ref: ArtifactRef) -> bytes:
        self.calls.append((request, ref))
        return self.data


class _SttTransport:
    def __init__(self, text: str = "安全な文字起こし") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []
        self.probes: list[tuple[str, float]] = []
        self.closed = False

    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
        self.probes.append((model, timeout_seconds))
        return True

    async def transcribe(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.text

    async def close(self) -> None:
        self.closed = True


class _VoicevoxClient:
    def __init__(self, wav: bytes | None = None) -> None:
        self.wav = wav or _wav()
        self.calls: list[VoicevoxSynthesisRequest] = []
        self.probes: list[float] = []
        self.closed = False

    async def probe_version(self, *, timeout_seconds: float) -> bool:
        self.probes.append(timeout_seconds)
        return True

    async def synthesize(self, request: VoicevoxSynthesisRequest) -> Any:
        self.calls.append(request)
        return SimpleNamespace(wav=self.wav)

    async def close(self) -> None:
        self.closed = True


class _ArtifactStore:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []

    def put_wav(self, data: bytes, *, request_binding: str) -> ArtifactRef:
        self.calls.append((data, request_binding))
        return ArtifactRef(
            f"speech-output-{len(self.calls)}",
            ArtifactKind.AUDIO,
            "audio/wav",
            len(data),
            hashlib.sha256(data).hexdigest(),
        )


class _ResponseContent:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self.payload), size):
            yield self.payload[offset : offset + size]


class _HttpResponse:
    def __init__(
        self,
        status: int,
        payload: bytes,
        *,
        content_length: int | None = None,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.content_length = len(payload) if content_length is None else content_length
        self.headers = {"Content-Type": content_type}
        self.content = _ResponseContent(payload)

    async def __aenter__(self) -> _HttpResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _HttpSession:
    def __init__(self, response: _HttpResponse) -> None:
        self.response = response
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _HttpResponse:
        self.calls.append((url, kwargs))
        return self.response

    def get(self, url: str, **kwargs: Any) -> _HttpResponse:
        self.calls.append((url, kwargs))
        return self.response

    async def close(self) -> None:
        self.closed = True


async def test_openai_stt_resolves_one_bound_audio_and_rechecks_authorization() -> None:
    audio = _wav(rate=44_100)
    request = _stt_request(audio)
    resolver = _AudioResolver(audio)
    transport = _SttTransport()
    adapter = OpenAITranscriptionProviderAdapter(
        transport,
        audio_resolver=resolver,
        readiness_current=lambda: True,
    )
    authorization_calls = 0

    def allowed() -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        return True

    result = await adapter.execute(
        request,
        _invocation("openai-api", OPENAI_STT_MODEL),
        execution_allowed=allowed,
    )

    assert authorization_calls == 3
    assert resolver.calls == [(request, request.input_artifacts[0])]
    assert len(transport.calls) == 1
    assert transport.calls[0]["filename"] == "input-audio.wav"
    assert transport.calls[0]["media_type"] == "audio/wav"
    assert transport.calls[0]["model"] == OPENAI_STT_MODEL
    assert transport.calls[0]["language_code"] == "ja-jp"
    assert result.text == "安全な文字起こし"
    assert result.artifacts == ()


@pytest.mark.parametrize("allowed_results", [(False,), (True, False)])
async def test_openai_stt_revoke_prevents_read_or_remote_provider(
    allowed_results: tuple[bool, ...],
) -> None:
    audio = _wav(rate=44_100)
    request = _stt_request(audio)
    resolver = _AudioResolver(audio)
    transport = _SttTransport()
    adapter = OpenAITranscriptionProviderAdapter(
        transport,
        audio_resolver=resolver,
        readiness_current=lambda: True,
    )
    decisions = iter(allowed_results)

    with pytest.raises(ProviderExecutionDeniedError):
        await adapter.execute(
            request,
            _invocation("openai-api", OPENAI_STT_MODEL),
            execution_allowed=lambda: next(decisions),
        )

    assert len(resolver.calls) == (0 if allowed_results == (False,) else 1)
    assert transport.calls == []


async def test_openai_stt_rejects_audio_integrity_and_model_before_remote_call() -> None:
    audio = _wav(rate=44_100)
    request = _stt_request(audio)
    resolver = _AudioResolver(audio + b"changed")
    transport = _SttTransport()
    adapter = OpenAITranscriptionProviderAdapter(
        transport,
        audio_resolver=resolver,
        readiness_current=lambda: True,
    )

    with pytest.raises(RuntimeError, match="verified"):
        await adapter.execute(
            request,
            _invocation("openai-api", OPENAI_STT_MODEL),
            execution_allowed=lambda: True,
        )
    with pytest.raises(RuntimeError, match="contract mismatch"):
        await adapter.execute(
            request,
            _invocation("openai-api", "gpt-4o-transcribe"),
            execution_allowed=lambda: True,
        )
    assert transport.calls == []


async def test_openai_http_transport_uses_fixed_endpoint_and_hides_secret() -> None:
    payload = json.dumps({"text": "文字起こし", "usage": {}}).encode()
    session = _HttpSession(_HttpResponse(200, payload))
    transport = AiohttpOpenAITranscriptionTransport(api_key="sk-test-secret")
    transport._session = session

    text = await transport.transcribe(
        audio=_wav(rate=44_100),
        filename="input-audio.wav",
        media_type="audio/wav",
        model=OPENAI_STT_MODEL,
        language_code="ja-jp",
        prompt="",
        timeout_seconds=9,
    )

    assert text == "文字起こし"
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == "https://api.openai.com/v1/audio/transcriptions"
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"].total == 9
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test-secret"
    assert "sk-test-secret" not in repr(transport)
    await transport.close()
    assert session.closed is True
    with pytest.raises(RuntimeError, match="transport is closed"):
        await transport.transcribe(
            audio=_wav(rate=44_100),
            filename="input-audio.wav",
            media_type="audio/wav",
            model=OPENAI_STT_MODEL,
            language_code=None,
            prompt="",
            timeout_seconds=9,
        )
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (_HttpResponse(302, b"redirect"), "rejected"),
        (_HttpResponse(200, b'{"text":"one","text":"two"}'), "invalid JSON"),
        (_HttpResponse(200, b'{"text":"ok"}', content_length=40_000), "exceeds"),
        (_HttpResponse(200, b'{"text":"ok","unexpected":true}'), "invalid result"),
    ],
)
async def test_openai_http_transport_rejects_untrusted_responses(
    response: _HttpResponse,
    match: str,
) -> None:
    transport = AiohttpOpenAITranscriptionTransport(api_key="sk-not-logged")
    transport._session = _HttpSession(response)

    with pytest.raises(RuntimeError, match=match):
        await transport.transcribe(
            audio=_wav(rate=44_100),
            filename="input-audio.wav",
            media_type="audio/wav",
            model=OPENAI_STT_MODEL,
            language_code=None,
            prompt="",
            timeout_seconds=9,
        )


async def test_openai_stt_health_requires_exact_bounded_read_only_model_probe() -> None:
    payload = json.dumps(
        {
            "id": OPENAI_STT_MODEL,
            "object": "model",
            "created": 1,
            "owned_by": "openai",
        }
    ).encode()
    session = _HttpSession(_HttpResponse(200, payload))
    transport = AiohttpOpenAITranscriptionTransport(api_key="sk-probe-secret")
    transport._session = session
    adapter = OpenAITranscriptionProviderAdapter(
        transport,
        audio_resolver=_AudioResolver(_wav(rate=44_100)),
        readiness_current=lambda: True,
    )

    health = await adapter.health()

    assert health.status.value == "ready"
    assert health.probed_model_aliases == ("stt.fast", "stt.balanced", "stt.quality")
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == f"https://api.openai.com/v1/models/{OPENAI_STT_MODEL}"
    assert kwargs["headers"] == {"Authorization": "Bearer sk-probe-secret"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"].total == 5.0
    assert "sk-probe-secret" not in repr(transport)

    mismatch_session = _HttpSession(
        _HttpResponse(
            200,
            payload.replace(OPENAI_STT_MODEL.encode(), b"gpt-4o-transcribe"),
        )
    )
    mismatch_transport = AiohttpOpenAITranscriptionTransport(api_key="sk-mismatch-secret")
    mismatch_transport._session = mismatch_session
    mismatch_adapter = OpenAITranscriptionProviderAdapter(
        mismatch_transport,
        audio_resolver=_AudioResolver(_wav(rate=44_100)),
        readiness_current=lambda: True,
    )
    assert (await mismatch_adapter.health()).status.value == "unavailable"
    assert "sk-mismatch-secret" not in repr(mismatch_adapter)

    oversized_transport = AiohttpOpenAITranscriptionTransport(api_key="sk-oversized-secret")
    oversized_transport._session = _HttpSession(_HttpResponse(200, payload, content_length=9_000))
    oversized = OpenAITranscriptionProviderAdapter(
        oversized_transport,
        audio_resolver=_AudioResolver(_wav(rate=44_100)),
        readiness_current=lambda: True,
    )
    assert (await oversized.health()).status.value == "unavailable"
    assert "sk-oversized-secret" not in repr(oversized_transport)

    wrong_type_transport = AiohttpOpenAITranscriptionTransport(api_key="sk-wrong-type-secret")
    wrong_type_transport._session = _HttpSession(_HttpResponse(200, payload, content_type="text/plain"))
    wrong_type = OpenAITranscriptionProviderAdapter(
        wrong_type_transport,
        audio_resolver=_AudioResolver(_wav(rate=44_100)),
        readiness_current=lambda: True,
    )
    assert (await wrong_type.health()).status.value == "unavailable"
    assert "sk-wrong-type-secret" not in repr(wrong_type_transport)


async def test_speech_probe_timeout_never_claims_ready() -> None:
    class TimedOutStt(_SttTransport):
        async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
            self.probes.append((model, timeout_seconds))
            raise TimeoutError("private provider timeout detail")

    class TimedOutVoicevox(_VoicevoxClient):
        async def probe_version(self, *, timeout_seconds: float) -> bool:
            self.probes.append(timeout_seconds)
            raise TimeoutError("private engine timeout detail")

    stt = OpenAITranscriptionProviderAdapter(
        TimedOutStt(),
        audio_resolver=_AudioResolver(_wav(rate=44_100)),
        readiness_current=lambda: True,
    )
    voicevox = VoicevoxSpeechSynthesisProviderAdapter(
        TimedOutVoicevox(),
        _ArtifactStore(),
        readiness_current=lambda: True,
    )

    assert (await stt.health()).status.value == "unavailable"
    assert (await voicevox.health()).status.value == "unavailable"


async def test_voicevox_client_version_probe_is_get_only_strict_and_bounded() -> None:
    session = _HttpSession(_HttpResponse(200, b'"0.24.1"'))
    client = VoicevoxClient(endpoint="http://127.0.0.1:50021")
    client._session = session

    assert await client.probe_version(timeout_seconds=5.0) is True
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == "http://127.0.0.1:50021/version"
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"].total == 5.0

    invalid_session = _HttpSession(_HttpResponse(200, b'""'))
    invalid = VoicevoxClient(endpoint="http://127.0.0.1:50021")
    invalid._session = invalid_session
    assert await invalid.probe_version(timeout_seconds=5.0) is False

    arbitrary_session = _HttpSession(_HttpResponse(200, b'"not-voicevox"'))
    arbitrary = VoicevoxClient(endpoint="http://127.0.0.1:50021")
    arbitrary._session = arbitrary_session
    assert await arbitrary.probe_version(timeout_seconds=5.0) is False

    oversized_session = _HttpSession(_HttpResponse(200, b'"0.24.1"', content_length=300))
    oversized = VoicevoxClient(endpoint="http://127.0.0.1:50021")
    oversized._session = oversized_session
    assert await oversized.probe_version(timeout_seconds=5.0) is False


async def test_voicevox_tts_commits_canonical_request_bound_new_wav() -> None:
    request = _tts_request()
    invocation = _invocation("voicevox-local", VOICEVOX_PROVIDER_MODEL)
    client = _VoicevoxClient()
    store = _ArtifactStore()
    adapter = VoicevoxSpeechSynthesisProviderAdapter(
        client,
        store,
        readiness_current=lambda: True,
    )
    authorization_calls = 0

    def allowed() -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        return True

    result = await adapter.execute(request, invocation, execution_allowed=allowed)
    expected_binding = speech_artifact_request_binding(
        request,
        provider_id=adapter.provider_id,
        provider_model=invocation.provider_model,
        model_alias=invocation.model_alias,
        quality_tier=invocation.quality_tier,
    )

    assert authorization_calls == 2
    assert len(client.calls) == 1
    assert client.calls[0].request_binding == expected_binding
    assert "安全な読み上げ" not in repr(client.calls[0])
    assert len(store.calls) == 1
    wav, binding = store.calls[0]
    assert binding == expected_binding
    assert struct.unpack_from("<I", wav, 24)[0] == 48_000
    assert result.artifacts[0].artifact_id == "speech-output-1"
    assert result.artifacts[0].sha256 == hashlib.sha256(wav).hexdigest()
    assert result.text == ""


async def test_voicevox_tts_revoke_at_commit_keeps_artifact_store_unchanged() -> None:
    client = _VoicevoxClient()
    store = _ArtifactStore()
    adapter = VoicevoxSpeechSynthesisProviderAdapter(
        client,
        store,
        readiness_current=lambda: True,
    )
    decisions = iter((True, False))

    with pytest.raises(ProviderExecutionDeniedError):
        await adapter.execute(
            _tts_request(),
            _invocation("voicevox-local", VOICEVOX_PROVIDER_MODEL),
            execution_allowed=lambda: next(decisions),
        )

    assert len(client.calls) == 1
    assert store.calls == []


async def test_voicevox_tts_rejects_noncanonical_provider_wav_before_commit() -> None:
    original = _wav()
    extra = b"LIST" + struct.pack("<I", 4) + b"meta"
    malformed = original[:4] + struct.pack("<I", len(original) + len(extra) - 8) + original[8:] + extra
    client = _VoicevoxClient(malformed)
    store = _ArtifactStore()
    adapter = VoicevoxSpeechSynthesisProviderAdapter(
        client,
        store,
        readiness_current=lambda: True,
    )

    with pytest.raises(RuntimeError, match="invalid WAV"):
        await adapter.execute(
            _tts_request(),
            _invocation("voicevox-local", VOICEVOX_PROVIDER_MODEL),
            execution_allowed=lambda: True,
        )

    assert store.calls == []


async def test_provider_close_revokes_health_and_propagates_external_cancel() -> None:
    class CancelledStt(_SttTransport):
        async def transcribe(self, **kwargs: Any) -> str:
            raise asyncio.CancelledError

    audio = _wav(rate=44_100)
    stt = OpenAITranscriptionProviderAdapter(
        CancelledStt(),
        audio_resolver=_AudioResolver(audio),
        readiness_current=lambda: True,
    )
    with pytest.raises(asyncio.CancelledError):
        await stt.execute(
            _stt_request(audio),
            _invocation("openai-api", OPENAI_STT_MODEL),
            execution_allowed=lambda: True,
        )
    await stt.close()
    assert (await stt.health()).status.value == "unavailable"

    voicevox = VoicevoxSpeechSynthesisProviderAdapter(
        _VoicevoxClient(),
        _ArtifactStore(),
        readiness_current=lambda: True,
    )
    await voicevox.close()
    assert (await voicevox.health()).status.value == "unavailable"


async def test_provider_close_is_linearized_with_in_flight_external_calls() -> None:
    class BlockingStt(_SttTransport):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def transcribe(self, **kwargs: Any) -> str:
            self.calls.append(kwargs)
            self.started.set()
            await self.release.wait()
            return self.text

    audio = _wav(rate=44_100)
    stt_transport = BlockingStt()
    stt = OpenAITranscriptionProviderAdapter(
        stt_transport,
        audio_resolver=_AudioResolver(audio),
        readiness_current=lambda: True,
    )
    stt_execution = asyncio.create_task(
        stt.execute(
            _stt_request(audio),
            _invocation("openai-api", OPENAI_STT_MODEL),
            execution_allowed=lambda: True,
        )
    )
    await stt_transport.started.wait()
    stt_close = asyncio.create_task(stt.close())
    await asyncio.sleep(0)
    assert stt_close.done() is False
    stt_transport.release.set()
    assert (await stt_execution).text == "安全な文字起こし"
    await stt_close
    with pytest.raises(RuntimeError, match="contract mismatch"):
        await stt.execute(
            _stt_request(audio),
            _invocation("openai-api", OPENAI_STT_MODEL),
            execution_allowed=lambda: True,
        )
    assert len(stt_transport.calls) == 1

    class BlockingVoicevox(_VoicevoxClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def synthesize(self, request: VoicevoxSynthesisRequest) -> Any:
            self.calls.append(request)
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(wav=self.wav)

    voicevox_client = BlockingVoicevox()
    voicevox_store = _ArtifactStore()
    voicevox = VoicevoxSpeechSynthesisProviderAdapter(
        voicevox_client,
        voicevox_store,
        readiness_current=lambda: True,
    )
    voicevox_execution = asyncio.create_task(
        voicevox.execute(
            _tts_request(),
            _invocation("voicevox-local", VOICEVOX_PROVIDER_MODEL),
            execution_allowed=lambda: True,
        )
    )
    await voicevox_client.started.wait()
    voicevox_close = asyncio.create_task(voicevox.close())
    await asyncio.sleep(0)
    assert voicevox_close.done() is False
    voicevox_client.release.set()
    assert len((await voicevox_execution).artifacts) == 1
    await voicevox_close
    with pytest.raises(RuntimeError, match="contract mismatch"):
        await voicevox.execute(
            _tts_request(),
            _invocation("voicevox-local", VOICEVOX_PROVIDER_MODEL),
            execution_allowed=lambda: True,
        )
    assert len(voicevox_client.calls) == 1
