from __future__ import annotations

import asyncio
import hashlib
import json
import struct
from pathlib import Path

import pytest

import yonerai_discord.modules.music_generation.provider_elevenlabs as music_provider_module
import yonerai_discord.modules.video_generation.provider_gemini_veo as video_provider_module
from yonerai_discord.modules.music_generation.artifacts import MusicArtifactStore
from yonerai_discord.modules.music_generation.domain import music_artifact_request_binding
from yonerai_discord.modules.music_generation.provider_elevenlabs import (
    AiohttpElevenLabsMusicTransport,
    ELEVEN_MUSIC_ORIGIN,
    ElevenLabsMusicProviderAdapter,
    ElevenLabsMusicProviderError,
    ElevenLabsMusicRemoteOutcomeUncertainCancelledError,
    ElevenLabsMusicTimeoutError,
)
from yonerai_discord.modules.speech_synthesis.domain import speech_artifact_request_binding
from yonerai_discord.modules.speech_synthesis.provider_voicevox import (
    VoicevoxSpeechSynthesisProviderAdapter,
)
from yonerai_discord.modules.speech_transcription.provider_openai import (
    OpenAITranscriptionProviderAdapter,
)
from yonerai_discord.modules.video_generation.artifacts import VideoArtifactStore
from yonerai_discord.modules.video_generation.domain import video_artifact_request_binding
from yonerai_discord.modules.video_generation.provider_gemini_veo import (
    AiohttpGeminiVeoTransport,
    GEMINI_VEO_ORIGIN,
    GeminiVeoProviderAdapter,
    GeminiVeoProviderError,
    GeminiVeoRemoteOutcomeUncertainCancelledError,
    GeminiVeoRemoteOutcomeUncertainError,
)
from yonerai_discord.provider_registry import (
    ArtifactKind,
    ArtifactRef,
    LogicalCapability,
    MediaGenerationInput,
    ProviderExecutionDeniedError,
    ProviderInvocation,
    ProviderRequest,
    QualityTier,
    ResourceProfile,
    HealthStatus,
    SpeechSynthesisInput,
    SpeechTranscriptionInput,
)


def _wav(*, rate: int = 44_100, seconds: int = 1) -> bytes:
    pcm = b"\0\0" * (rate * seconds)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def _box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def _mp4() -> bytes:
    return _box(b"ftyp", b"isom\0\0\0\0isommp42") + _box(b"moov") + _box(b"mdat", b"video")


def _invocation(
    provider: str,
    model: str,
    *,
    timeout_seconds: float = 30,
) -> ProviderInvocation:
    return ProviderInvocation(
        provider_id=provider,
        quality_tier=QualityTier.BALANCED,
        model_alias="test.balanced",
        provider_model=model,
        timeout_seconds=timeout_seconds,
        resources=ResourceProfile.remote(max_concurrency=1),
    )


class _Voicevox:
    closed = False

    async def probe_version(self, *, timeout_seconds: float) -> bool:
        assert timeout_seconds == 5.0
        return True

    async def synthesize(self, request):
        assert request.speaker_id == 3
        return type("Speech", (), {"wav": _wav(rate=24_000)})()

    async def close(self) -> None:
        self.closed = True


class _Music:
    closed = False

    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
        assert model == "music_v2"
        assert timeout_seconds == 5.0
        return True

    async def compose_instrumental(self, **kwargs):
        assert kwargs["model"] == "music_v2"
        assert kwargs["duration_seconds"] == 3
        return _wav(seconds=3)

    async def close(self) -> None:
        self.closed = True


class _Video:
    closed = False

    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
        assert model.startswith("veo-3.1-")
        assert timeout_seconds == 5.0
        return True

    async def generate_video(self, **kwargs):
        assert kwargs["model"] == "veo-3.1-fast-generate-preview"
        assert kwargs["duration_seconds"] == 6
        assert kwargs["resolution"] == "720p"
        return _mp4()

    async def close(self) -> None:
        self.closed = True


class _Stt:
    closed = False

    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
        assert model == "gpt-4o-mini-transcribe"
        assert timeout_seconds == 5.0
        return True

    async def transcribe(self, **kwargs):
        assert kwargs["model"] == "gpt-4o-mini-transcribe"
        assert kwargs["filename"] == "input-audio.wav"
        return "安全な文字起こし"

    async def close(self) -> None:
        self.closed = True


class _ChunkStream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def iter_chunked(self, size: int):
        payload, self.payload = self.payload, b""
        for offset in range(0, len(payload), size):
            yield payload[offset : offset + size]


class _HangingChunkStream:
    async def iter_chunked(self, _size: int):
        await asyncio.Event().wait()
        if False:
            yield b""


class _HttpResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = _ChunkStream(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _HttpSession:
    def __init__(self, responses: list[_HttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True
        return False

    def post(self, url: str, **kwargs):
        return self._request("POST", url, kwargs)

    def get(self, url: str, **kwargs):
        return self._request("GET", url, kwargs)

    def _request(self, method: str, url: str, kwargs: dict[str, object]):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


class _HangingVideo:
    async def generate_video(self, **_kwargs):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        return None


class _CancelledVideo:
    async def generate_video(self, **_kwargs):
        raise asyncio.CancelledError

    async def close(self) -> None:
        return None


async def test_injected_transport_alone_never_claims_provider_readiness(tmp_path: Path) -> None:
    root = tmp_path / "not-ready"
    root.mkdir()
    adapter = VoicevoxSpeechSynthesisProviderAdapter(_Voicevox(), MusicArtifactStore(root))

    assert (await adapter.health()).status is HealthStatus.UNAVAILABLE


async def test_voicevox_adapter_canonicalizes_24khz_and_commits_after_authorization(tmp_path: Path) -> None:
    root = tmp_path / "tts"
    root.mkdir()
    store = MusicArtifactStore(root)
    adapter = VoicevoxSpeechSynthesisProviderAdapter(_Voicevox(), store, readiness_current=lambda: True)
    request = ProviderRequest(
        "tts-request-1",
        "trace-tts-1",
        LogicalCapability.SPEECH_TTS,
        "discord-user-1",
        SpeechSynthesisInput("読み上げ", language_code="ja-JP"),
        quality_tier=QualityTier.BALANCED,
    )
    invocation = _invocation("voicevox-local", "voicevox-engine")
    calls = 0

    def allowed() -> bool:
        nonlocal calls
        calls += 1
        return True

    result = await adapter.execute(request, invocation, execution_allowed=allowed)
    binding = speech_artifact_request_binding(
        request,
        provider_id=adapter.provider_id,
        provider_model=invocation.provider_model,
        model_alias=invocation.model_alias,
        quality_tier=invocation.quality_tier,
    )
    wav = store.read_wav(result.artifacts[0], request_binding=binding)
    assert struct.unpack_from("<I", wav, 24)[0] == 48_000
    assert calls == 2


async def test_openai_stt_reads_one_bound_artifact_and_never_returns_it() -> None:
    audio = _wav()
    ref = ArtifactRef(
        "stt-audio-1",
        ArtifactKind.AUDIO,
        "audio/wav",
        len(audio),
        hashlib.sha256(audio).hexdigest(),
    )
    request = ProviderRequest(
        "stt-request-1",
        "trace-stt-1",
        LogicalCapability.SPEECH_STT,
        "discord-user-1",
        SpeechTranscriptionInput(language_code="ja-JP"),
        quality_tier=QualityTier.BALANCED,
        input_artifacts=(ref,),
    )
    reads = 0

    def read_audio(actual_request, actual_ref):
        nonlocal reads
        assert actual_request is request and actual_ref is ref
        reads += 1
        return audio

    adapter = OpenAITranscriptionProviderAdapter(
        _Stt(),
        read_audio=read_audio,
        readiness_current=lambda: True,
    )
    result = await adapter.execute(
        request,
        _invocation("openai-api", "gpt-4o-mini-transcribe"),
        execution_allowed=lambda: True,
    )
    assert reads == 1
    assert result.text == "安全な文字起こし"
    assert result.artifacts == ()


async def test_music_and_video_adapters_commit_exact_request_bound_artifacts(tmp_path: Path) -> None:
    music_root = tmp_path / "music"
    video_root = tmp_path / "video"
    music_root.mkdir()
    video_root.mkdir()
    music_store = MusicArtifactStore(music_root)
    video_store = VideoArtifactStore(video_root)
    policy_checks: list[tuple[ProviderRequest, ProviderInvocation]] = []

    def music_policy_current(
        request: ProviderRequest,
        invocation: ProviderInvocation,
    ) -> bool:
        policy_checks.append((request, invocation))
        return True

    music = ElevenLabsMusicProviderAdapter(
        _Music(),
        music_store,
        readiness_current=lambda: True,
        request_policy_current=music_policy_current,
    )
    video = GeminiVeoProviderAdapter(_Video(), video_store, readiness_current=lambda: True)
    music_request = ProviderRequest(
        "music-request-1",
        "trace-music-1",
        LogicalCapability.MUSIC_GENERATION,
        "discord-user-1",
        MediaGenerationInput("original instrumental", duration_seconds=3),
        quality_tier=QualityTier.BALANCED,
    )
    video_request = ProviderRequest(
        "video-request-1",
        "trace-video-1",
        LogicalCapability.VIDEO_GENERATION,
        "discord-user-1",
        MediaGenerationInput("short animation"),
        quality_tier=QualityTier.BALANCED,
    )
    music_invocation = _invocation("elevenlabs-api", "music_v2")
    video_invocation = _invocation("google-gemini-api", "veo-3.1-fast-generate-preview")
    music_checks = 0
    video_checks = 0

    def music_allowed() -> bool:
        nonlocal music_checks
        music_checks += 1
        return True

    def video_allowed() -> bool:
        nonlocal video_checks
        video_checks += 1
        return True

    music_result = await music.execute(
        music_request,
        music_invocation,
        execution_allowed=music_allowed,
    )
    video_result = await video.execute(
        video_request,
        video_invocation,
        execution_allowed=video_allowed,
    )
    music_binding = music_artifact_request_binding(
        music_request,
        provider_id=music.provider_id,
        provider_model=music_invocation.provider_model,
        model_alias=music_invocation.model_alias,
        quality_tier=music_invocation.quality_tier,
    )
    video_binding = video_artifact_request_binding(
        video_request,
        provider_id=video.provider_id,
        provider_model=video_invocation.provider_model,
        model_alias=video_invocation.model_alias,
        quality_tier=video_invocation.quality_tier,
    )
    assert music_store.read_wav(music_result.artifacts[0], request_binding=music_binding) == _wav(seconds=3)
    assert video_store.read_mp4(video_result.artifacts[0], request_binding=video_binding) == _mp4()
    assert music_checks == 4
    assert video_checks == 2
    assert policy_checks == [(music_request, music_invocation)] * 2


async def test_eleven_music_requires_current_service_issued_rights_proof_before_provider_and_store(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music-policy"
    root.mkdir()
    provider_calls = 0

    class TrackingMusic:
        async def compose_instrumental(self, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            return _wav(seconds=3)

        async def close(self) -> None:
            return None

    request = ProviderRequest(
        "music-request-policy",
        "trace-music-policy",
        LogicalCapability.MUSIC_GENERATION,
        "discord-user-1",
        MediaGenerationInput("original instrumental", duration_seconds=3),
    )
    invocation = _invocation("elevenlabs-api", "music_v2")
    store = MusicArtifactStore(root)
    missing = ElevenLabsMusicProviderAdapter(
        TrackingMusic(),
        store,
        readiness_current=lambda: True,
    )
    assert (await missing.health()).status is HealthStatus.UNAVAILABLE
    with pytest.raises(ElevenLabsMusicProviderError):
        await missing.execute(request, invocation, execution_allowed=lambda: True)
    assert provider_calls == 0

    authorization_current = True

    async def policy_await_with_revoke(*_args) -> bool:
        nonlocal authorization_current
        await asyncio.sleep(0)
        authorization_current = False
        return True

    revoked_during_policy = ElevenLabsMusicProviderAdapter(
        TrackingMusic(),
        store,
        readiness_current=lambda: True,
        request_policy_current=policy_await_with_revoke,
    )
    with pytest.raises(ProviderExecutionDeniedError):
        await revoked_during_policy.execute(
            request,
            invocation,
            execution_allowed=lambda: authorization_current,
        )
    assert provider_calls == 0

    policy_results = iter((True, False))
    revoked = ElevenLabsMusicProviderAdapter(
        TrackingMusic(),
        store,
        readiness_current=lambda: True,
        request_policy_current=lambda *_args: next(policy_results),
    )
    with pytest.raises(ElevenLabsMusicProviderError):
        await revoked.execute(request, invocation, execution_allowed=lambda: True)
    assert provider_calls == 1
    assert not list(root.glob("*.wav"))


@pytest.mark.parametrize("kind", ["tts", "stt", "music", "video"])
async def test_all_adapters_fail_before_provider_or_store_commit_when_reauthorization_revokes(
    tmp_path: Path,
    kind: str,
) -> None:
    calls = 0

    def revoked() -> bool:
        nonlocal calls
        calls += 1
        return False

    if kind == "tts":
        root = tmp_path / "tts-denied"
        root.mkdir()
        adapter = VoicevoxSpeechSynthesisProviderAdapter(
            _Voicevox(),
            MusicArtifactStore(root),
            readiness_current=lambda: True,
        )
        request = ProviderRequest(
            "tts-denied",
            "trace-tts-denied",
            LogicalCapability.SPEECH_TTS,
            "discord-user-1",
            SpeechSynthesisInput("secret", language_code="ja-JP"),
        )
        invocation = _invocation("voicevox-local", "voicevox-engine")
    elif kind == "stt":
        audio = _wav()
        ref = ArtifactRef(
            "stt-denied-audio",
            ArtifactKind.AUDIO,
            "audio/wav",
            len(audio),
            hashlib.sha256(audio).hexdigest(),
        )
        adapter = OpenAITranscriptionProviderAdapter(
            _Stt(),
            read_audio=lambda *_: audio,
            readiness_current=lambda: True,
        )
        request = ProviderRequest(
            "stt-denied",
            "trace-stt-denied",
            LogicalCapability.SPEECH_STT,
            "discord-user-1",
            SpeechTranscriptionInput(),
            input_artifacts=(ref,),
        )
        invocation = _invocation("openai-api", "gpt-4o-mini-transcribe")
    elif kind == "music":
        root = tmp_path / "music-denied"
        root.mkdir()
        adapter = ElevenLabsMusicProviderAdapter(
            _Music(),
            MusicArtifactStore(root),
            readiness_current=lambda: True,
            request_policy_current=lambda *_args: True,
        )
        request = ProviderRequest(
            "music-request-denied",
            "trace-music-denied",
            LogicalCapability.MUSIC_GENERATION,
            "discord-user-1",
            MediaGenerationInput("instrumental", duration_seconds=3),
        )
        invocation = _invocation("elevenlabs-api", "music_v2")
    else:
        root = tmp_path / "video-denied"
        root.mkdir()
        adapter = GeminiVeoProviderAdapter(
            _Video(),
            VideoArtifactStore(root),
            readiness_current=lambda: True,
        )
        request = ProviderRequest(
            "video-request-denied",
            "trace-video-denied",
            LogicalCapability.VIDEO_GENERATION,
            "discord-user-1",
            MediaGenerationInput("animation"),
        )
        invocation = _invocation("google-gemini-api", "veo-3.1-fast-generate-preview")

    with pytest.raises(ProviderExecutionDeniedError):
        await adapter.execute(request, invocation, execution_allowed=revoked)
    assert calls == 1


async def test_eleven_music_http_contract_is_fixed_wav_instrumental_and_sanitized(
    monkeypatch,
) -> None:
    session = _HttpSession([_HttpResponse(_wav(seconds=3), content_type="audio/wav")])
    monkeypatch.setattr(
        music_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    transport = AiohttpElevenLabsMusicTransport(api_key="private-eleven-key")

    audio = await transport.compose_instrumental(
        prompt="original instrumental preview",
        duration_seconds=3,
        model="music_v2",
        timeout_seconds=30,
    )

    assert audio == _wav(seconds=3)
    assert session.closed is True
    assert session.calls == [
        {
            "method": "POST",
            "url": f"{ELEVEN_MUSIC_ORIGIN}/v1/music",
            "params": {"output_format": "wav_44100"},
            "json": {
                "prompt": "original instrumental preview",
                "music_length_ms": 3_000,
                "model_id": "music_v2",
                "force_instrumental": True,
                "store_for_inpainting": False,
            },
            "headers": {
                "xi-api-key": "private-eleven-key",
                "Content-Type": "application/json",
            },
            "allow_redirects": False,
        }
    ]
    assert "private-eleven-key" not in repr(transport)


async def test_eleven_music_cancel_before_submission_is_regular_cancel_and_calls_provider_zero(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music-pre-submit-cancel"
    root.mkdir()
    provider_calls = 0
    authorization_started = asyncio.Event()

    class TrackingMusic(_Music):
        async def compose_instrumental(self, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            return _wav(seconds=3)

    async def blocked_authorization() -> bool:
        authorization_started.set()
        await asyncio.Event().wait()
        return True

    adapter = ElevenLabsMusicProviderAdapter(
        TrackingMusic(),
        MusicArtifactStore(root),
        readiness_current=lambda: True,
        request_policy_current=lambda *_args: True,
    )
    request = ProviderRequest(
        "music-request-pre-submit-cancel",
        "trace-music-pre-submit-cancel",
        LogicalCapability.MUSIC_GENERATION,
        "discord-user-1",
        MediaGenerationInput("private instrumental prompt", duration_seconds=3),
    )
    execution = asyncio.create_task(
        adapter.execute(
            request,
            _invocation("elevenlabs-api", "music_v2"),
            execution_allowed=blocked_authorization,
        )
    )
    await authorization_started.wait()
    execution.cancel()

    with pytest.raises(asyncio.CancelledError) as captured:
        await execution

    assert type(captured.value) is asyncio.CancelledError
    assert provider_calls == 0
    assert not list(root.glob("*.wav"))


async def test_eleven_music_cancel_after_post_is_uncertain_and_never_commits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = asyncio.Event()

    class StartedHangingStream:
        async def iter_chunked(self, _size: int):
            started.set()
            await asyncio.Event().wait()
            if False:
                yield b""

    response = _HttpResponse(b"", content_type="audio/wav")
    response.content = StartedHangingStream()
    session = _HttpSession([response])
    monkeypatch.setattr(
        music_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    root = tmp_path / "music-post-submit-cancel"
    root.mkdir()
    adapter = ElevenLabsMusicProviderAdapter(
        AiohttpElevenLabsMusicTransport(api_key="private-eleven-cancel-key"),
        MusicArtifactStore(root),
        readiness_current=lambda: True,
        request_policy_current=lambda *_args: True,
    )
    request = ProviderRequest(
        "music-request-post-submit-cancel",
        "trace-music-post-submit-cancel",
        LogicalCapability.MUSIC_GENERATION,
        "discord-user-1",
        MediaGenerationInput("private instrumental prompt", duration_seconds=3),
    )
    execution = asyncio.create_task(
        adapter.execute(
            request,
            _invocation("elevenlabs-api", "music_v2"),
            execution_allowed=lambda: True,
        )
    )
    await started.wait()
    execution.cancel()

    with pytest.raises(ElevenLabsMusicRemoteOutcomeUncertainCancelledError) as captured:
        await execution

    assert isinstance(captured.value, asyncio.CancelledError)
    assert len(session.calls) == 1
    assert session.closed is True
    assert "private instrumental prompt" not in str(captured.value)
    assert "private-eleven-cancel-key" not in str(captured.value)
    assert not list(root.glob("*.wav"))


async def test_eleven_music_outer_deadline_is_timeout_not_uncertain_cancel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = asyncio.Event()

    class StartedHangingStream:
        async def iter_chunked(self, _size: int):
            started.set()
            await asyncio.Event().wait()
            if False:
                yield b""

    response = _HttpResponse(b"", content_type="audio/wav")
    response.content = StartedHangingStream()
    session = _HttpSession([response])
    monkeypatch.setattr(
        music_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    monkeypatch.setattr(music_provider_module, "_timeout", lambda _value: 0.01)
    root = tmp_path / "music-outer-deadline"
    root.mkdir()
    adapter = ElevenLabsMusicProviderAdapter(
        AiohttpElevenLabsMusicTransport(api_key="private-eleven-timeout-key"),
        MusicArtifactStore(root),
        readiness_current=lambda: True,
        request_policy_current=lambda *_args: True,
    )
    request = ProviderRequest(
        "music-request-outer-deadline",
        "trace-music-outer-deadline",
        LogicalCapability.MUSIC_GENERATION,
        "discord-user-1",
        MediaGenerationInput("private timeout prompt", duration_seconds=3),
    )

    with pytest.raises(ElevenLabsMusicTimeoutError) as captured:
        await adapter.execute(
            request,
            _invocation("elevenlabs-api", "music_v2"),
            execution_allowed=lambda: True,
        )

    assert started.is_set()
    assert not isinstance(captured.value, asyncio.CancelledError)
    assert len(session.calls) == 1
    assert session.closed is True
    assert "private timeout prompt" not in str(captured.value)
    assert "private-eleven-timeout-key" not in str(captured.value)
    assert not list(root.glob("*.wav"))


async def test_eleven_music_accepts_finite_nine_hundred_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _HttpSession([_HttpResponse(_wav(seconds=3), content_type="audio/wav")])
    session_kwargs: dict[str, object] = {}

    def session_factory(**kwargs: object) -> _HttpSession:
        session_kwargs.update(kwargs)
        return session

    monkeypatch.setattr(music_provider_module.aiohttp, "ClientSession", session_factory)
    transport = AiohttpElevenLabsMusicTransport(api_key="private-eleven-timeout-key")

    audio = await transport.compose_instrumental(
        prompt="original instrumental preview",
        duration_seconds=3,
        model="music_v2",
        timeout_seconds=900,
    )

    timeout = session_kwargs["timeout"]
    assert isinstance(timeout, music_provider_module.aiohttp.ClientTimeout)
    assert timeout.total == 900
    assert audio == _wav(seconds=3)
    with pytest.raises(ElevenLabsMusicProviderError):
        await transport.compose_instrumental(
            prompt="original instrumental preview",
            duration_seconds=3,
            model="music_v2",
            timeout_seconds=900.01,
        )
    assert len(session.calls) == 1


async def test_eleven_health_probe_is_get_only_exact_bounded_and_non_leaking(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = _HttpSession(
        [
            _HttpResponse(
                _json_bytes(
                    [
                        {"model_id": "eleven_flash_v2_5"},
                        {"model_id": "music_v2"},
                    ]
                )
            )
        ]
    )
    monkeypatch.setattr(
        music_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    root = tmp_path / "music-health"
    root.mkdir()
    transport = AiohttpElevenLabsMusicTransport(api_key="private-eleven-probe-key")
    adapter = ElevenLabsMusicProviderAdapter(
        transport,
        MusicArtifactStore(root),
        readiness_current=lambda: True,
        request_policy_current=lambda *_args: True,
    )

    health = await adapter.health()

    assert health.status is HealthStatus.READY
    assert health.probed_model_aliases == ("music.fast", "music.balanced", "music.quality")
    assert session.calls == [
        {
            "method": "GET",
            "url": f"{ELEVEN_MUSIC_ORIGIN}/v1/models",
            "headers": {"xi-api-key": "private-eleven-probe-key"},
            "allow_redirects": False,
        }
    ]
    assert "private-eleven-probe-key" not in repr(transport)

    mismatch_session = _HttpSession([_HttpResponse(_json_bytes([{"model_id": "music_v1"}]))])
    monkeypatch.setattr(
        music_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: mismatch_session,
    )
    mismatch = ElevenLabsMusicProviderAdapter(
        AiohttpElevenLabsMusicTransport(api_key="private-eleven-mismatch-key"),
        MusicArtifactStore(root),
        readiness_current=lambda: True,
        request_policy_current=lambda *_args: True,
    )
    assert (await mismatch.health()).status is HealthStatus.UNAVAILABLE
    assert "private-eleven-mismatch-key" not in repr(mismatch)

    oversized_session = _HttpSession([_HttpResponse(b"x" * (512 * 1024 + 1))])
    monkeypatch.setattr(
        music_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: oversized_session,
    )
    oversized = ElevenLabsMusicProviderAdapter(
        AiohttpElevenLabsMusicTransport(api_key="private-eleven-oversized-key"),
        MusicArtifactStore(root),
        readiness_current=lambda: True,
        request_policy_current=lambda *_args: True,
    )
    assert (await oversized.health()).status is HealthStatus.UNAVAILABLE
    assert "private-eleven-oversized-key" not in repr(oversized)


@pytest.mark.parametrize(
    "response",
    [
        _HttpResponse(b"private-provider-body", status=422, content_type="text/plain"),
        _HttpResponse(_wav(seconds=3), content_type="audio/mpeg"),
        _HttpResponse(b"x" * (8 * 1024 * 1024 + 1), content_type="audio/wav"),
    ],
)
async def test_eleven_music_http_failure_is_bounded_and_non_leaking(
    monkeypatch,
    response: _HttpResponse,
) -> None:
    session = _HttpSession([response])
    monkeypatch.setattr(
        music_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    transport = AiohttpElevenLabsMusicTransport(api_key="private-eleven-key")

    with pytest.raises(ElevenLabsMusicProviderError) as captured:
        await transport.compose_instrumental(
            prompt="original instrumental preview",
            duration_seconds=3,
            model="music_v2",
            timeout_seconds=30,
        )

    assert len(session.calls) == 1
    assert "private-provider-body" not in str(captured.value)
    assert "private-eleven-key" not in str(captured.value)


async def test_gemini_veo_http_contract_polls_once_and_downloads_one_mp4(
    monkeypatch,
) -> None:
    uri = f"{GEMINI_VEO_ORIGIN}/v1beta/files/video-1?alt=media"
    session = _HttpSession(
        [
            _HttpResponse(_json_bytes({"name": "operations/op-1"})),
            _HttpResponse(
                _json_bytes(
                    {
                        "done": True,
                        "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": uri}}]}},
                    }
                )
            ),
            _HttpResponse(_mp4(), content_type="video/mp4"),
        ]
    )
    monkeypatch.setattr(
        video_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    transport = AiohttpGeminiVeoTransport(
        api_key="private-gemini-key",
        poll_seconds=0.05,
    )

    mp4 = await transport.generate_video(
        prompt="short animation",
        model="veo-3.1-generate-preview",
        duration_seconds=8,
        resolution="4k",
        timeout_seconds=30,
    )

    assert mp4 == _mp4()
    assert [call["method"] for call in session.calls] == ["POST", "GET", "GET"]
    assert session.calls[0] == {
        "method": "POST",
        "url": (f"{GEMINI_VEO_ORIGIN}/v1beta/models/veo-3.1-generate-preview:predictLongRunning"),
        "headers": {
            "x-goog-api-key": "private-gemini-key",
            "Content-Type": "application/json",
        },
        "json": {
            "instances": [{"prompt": "short animation"}],
            "parameters": {"durationSeconds": 8, "resolution": "4k"},
        },
        "allow_redirects": False,
    }
    assert session.calls[1]["url"] == f"{GEMINI_VEO_ORIGIN}/v1beta/operations/op-1"
    assert session.calls[2]["url"] == uri
    assert all(call["allow_redirects"] is False for call in session.calls)
    assert "private-gemini-key" not in repr(transport)


async def test_gemini_veo_health_probe_is_exact_get_only_and_non_leaking(
    monkeypatch,
    tmp_path: Path,
) -> None:
    models = sorted(
        {
            "veo-3.1-lite-generate-preview",
            "veo-3.1-fast-generate-preview",
            "veo-3.1-generate-preview",
        }
    )
    session = _HttpSession([_HttpResponse(_json_bytes({"name": f"models/{model}"})) for model in models])
    monkeypatch.setattr(
        video_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    root = tmp_path / "video-health"
    root.mkdir()
    transport = AiohttpGeminiVeoTransport(api_key="private-gemini-probe-key")
    adapter = GeminiVeoProviderAdapter(
        transport,
        VideoArtifactStore(root),
        readiness_current=lambda: True,
    )

    health = await adapter.health()

    assert health.status is HealthStatus.READY
    assert health.probed_model_aliases == ("video.fast", "video.balanced", "video.quality")
    assert session.calls == [
        {
            "method": "GET",
            "url": f"{GEMINI_VEO_ORIGIN}/v1beta/models/{model}",
            "headers": {"x-goog-api-key": "private-gemini-probe-key"},
            "allow_redirects": False,
        }
        for model in models
    ]
    assert all("private-gemini-probe-key" not in str(call["url"]) for call in session.calls)
    assert "private-gemini-probe-key" not in repr(transport)

    mismatch_session = _HttpSession([_HttpResponse(_json_bytes({"name": "models/wrong-model"})) for _model in models])
    monkeypatch.setattr(
        video_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: mismatch_session,
    )
    mismatch = GeminiVeoProviderAdapter(
        AiohttpGeminiVeoTransport(api_key="private-gemini-mismatch-key"),
        VideoArtifactStore(root),
        readiness_current=lambda: True,
    )
    assert (await mismatch.health()).status is HealthStatus.UNAVAILABLE
    assert "private-gemini-mismatch-key" not in repr(mismatch)

    oversized_session = _HttpSession([_HttpResponse(b"x" * (64 * 1024 + 1)) for _model in models])
    monkeypatch.setattr(
        video_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: oversized_session,
    )
    oversized = GeminiVeoProviderAdapter(
        AiohttpGeminiVeoTransport(api_key="private-gemini-oversized-key"),
        VideoArtifactStore(root),
        readiness_current=lambda: True,
    )
    assert (await oversized.health()).status is HealthStatus.UNAVAILABLE
    assert "private-gemini-oversized-key" not in repr(oversized)


async def test_eleven_and_veo_health_probe_timeout_never_claims_ready(tmp_path: Path) -> None:
    class TimedOutMusic(_Music):
        async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
            raise TimeoutError("private provider timeout detail")

    class TimedOutVideo(_Video):
        async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
            raise TimeoutError("private provider timeout detail")

    music_root = tmp_path / "music-probe-timeout"
    music_root.mkdir()
    video_root = tmp_path / "video-probe-timeout"
    video_root.mkdir()
    music = ElevenLabsMusicProviderAdapter(
        TimedOutMusic(),
        MusicArtifactStore(music_root),
        readiness_current=lambda: True,
        request_policy_current=lambda *_args: True,
    )
    video = GeminiVeoProviderAdapter(
        TimedOutVideo(),
        VideoArtifactStore(video_root),
        readiness_current=lambda: True,
    )

    assert (await music.health()).status is HealthStatus.UNAVAILABLE
    assert (await video.health()).status is HealthStatus.UNAVAILABLE


@pytest.mark.parametrize("kind", ["music", "video"])
async def test_eleven_and_veo_close_drain_in_flight_operation(
    tmp_path: Path,
    kind: str,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    if kind == "music":
        root = tmp_path / "music-close-drain"
        root.mkdir()

        class BlockingMusic(_Music):
            async def compose_instrumental(self, **_kwargs):
                started.set()
                await release.wait()
                return _wav(seconds=3)

        transport = BlockingMusic()
        adapter = ElevenLabsMusicProviderAdapter(
            transport,
            MusicArtifactStore(root),
            readiness_current=lambda: True,
            request_policy_current=lambda *_args: True,
        )
        request = ProviderRequest(
            "music-request-close-drain",
            "trace-music-close-drain",
            LogicalCapability.MUSIC_GENERATION,
            "discord-user-1",
            MediaGenerationInput("instrumental", duration_seconds=3),
        )
        invocation = _invocation("elevenlabs-api", "music_v2")
        expected_error = ElevenLabsMusicProviderError
    else:
        root = tmp_path / "video-close-drain"
        root.mkdir()

        class BlockingVideo(_Video):
            async def generate_video(self, **_kwargs):
                started.set()
                await release.wait()
                return _mp4()

        transport = BlockingVideo()
        adapter = GeminiVeoProviderAdapter(
            transport,
            VideoArtifactStore(root),
            readiness_current=lambda: True,
        )
        request = ProviderRequest(
            "video-request-close-drain",
            "trace-video-close-drain",
            LogicalCapability.VIDEO_GENERATION,
            "discord-user-1",
            MediaGenerationInput("animation"),
        )
        invocation = _invocation("google-gemini-api", "veo-3.1-fast-generate-preview")
        expected_error = GeminiVeoProviderError

    execution = asyncio.create_task(adapter.execute(request, invocation, execution_allowed=lambda: True))
    await started.wait()
    closing = asyncio.create_task(adapter.close())
    await asyncio.sleep(0)

    assert closing.done() is False
    assert transport.closed is False
    release.set()
    with pytest.raises(expected_error, match="readiness changed"):
        await execution
    await closing
    assert transport.closed is True
    assert not list(root.iterdir())


@pytest.mark.parametrize(
    "uri,download_response,expected_calls",
    [
        ("https://attacker.invalid/video.mp4", None, 2),
        (
            f"{GEMINI_VEO_ORIGIN}/v1beta/files/video-1?alt=media",
            _HttpResponse(b"private-provider-body", status=302, content_type="text/plain"),
            3,
        ),
    ],
)
async def test_gemini_veo_rejects_foreign_or_redirected_download_without_leak(
    monkeypatch,
    uri: str,
    download_response: _HttpResponse | None,
    expected_calls: int,
) -> None:
    responses = [
        _HttpResponse(_json_bytes({"name": "operations/op-1"})),
        _HttpResponse(
            _json_bytes(
                {
                    "done": True,
                    "response": {"generateVideoResponse": {"generatedSamples": [{"video": {"uri": uri}}]}},
                }
            )
        ),
    ]
    if download_response is not None:
        responses.append(download_response)
    session = _HttpSession(responses)
    monkeypatch.setattr(
        video_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    transport = AiohttpGeminiVeoTransport(
        api_key="private-gemini-key",
        poll_seconds=0.05,
    )

    with pytest.raises(GeminiVeoProviderError) as captured:
        await transport.generate_video(
            prompt="short animation",
            model="veo-3.1-generate-preview",
            duration_seconds=8,
            resolution="4k",
            timeout_seconds=30,
        )

    assert len(session.calls) == expected_calls
    assert "private-provider-body" not in str(captured.value)
    assert "private-gemini-key" not in str(captured.value)


@pytest.mark.parametrize(
    "start_payload",
    [
        b'{"name":"operations/op-1","name":"operations/op-2"}',
        b'{"name":"operations/op-1","done":0}',
    ],
)
async def test_gemini_veo_rejects_invalid_start_json_and_unknown_model_before_download(
    monkeypatch,
    start_payload: bytes,
) -> None:
    session = _HttpSession([_HttpResponse(start_payload)])
    monkeypatch.setattr(
        video_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    transport = AiohttpGeminiVeoTransport(api_key="private-gemini-key")

    with pytest.raises(GeminiVeoProviderError):
        await transport.generate_video(
            prompt="short animation",
            model="veo-3.1-generate-preview",
            duration_seconds=8,
            resolution="4k",
            timeout_seconds=30,
        )
    with pytest.raises(GeminiVeoProviderError):
        await transport.generate_video(
            prompt="short animation",
            model="../veo-private",
            duration_seconds=8,
            resolution="4k",
            timeout_seconds=30,
        )
    with pytest.raises(GeminiVeoProviderError):
        await transport.generate_video(
            prompt="short animation",
            model="veo-3.1-fast-generate-preview",
            duration_seconds=8,
            resolution="4k",
            timeout_seconds=30,
        )

    assert len(session.calls) == 1


async def test_gemini_veo_rejects_tier_model_mismatch_before_provider(
    tmp_path: Path,
) -> None:
    root = tmp_path / "video-profile"
    root.mkdir()
    provider_calls = 0

    class TrackingVideo:
        async def generate_video(self, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            return _mp4()

        async def close(self) -> None:
            return None

    adapter = GeminiVeoProviderAdapter(
        TrackingVideo(),
        VideoArtifactStore(root),
        readiness_current=lambda: True,
    )
    request = ProviderRequest(
        "video-request-profile",
        "trace-video-profile",
        LogicalCapability.VIDEO_GENERATION,
        "discord-user-1",
        MediaGenerationInput("short animation"),
    )

    with pytest.raises(GeminiVeoProviderError):
        await adapter.execute(
            request,
            _invocation("google-gemini-api", "veo-3.1-generate-preview"),
            execution_allowed=lambda: True,
        )

    assert provider_calls == 0
    assert not list(root.glob("*.mp4"))


async def test_gemini_veo_timeout_is_uncertain_and_never_commits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "video-timeout"
    root.mkdir()
    adapter = GeminiVeoProviderAdapter(
        _HangingVideo(),
        VideoArtifactStore(root),
        readiness_current=lambda: True,
    )
    request = ProviderRequest(
        "video-request-timeout",
        "trace-video-timeout",
        LogicalCapability.VIDEO_GENERATION,
        "discord-user-1",
        MediaGenerationInput("short animation"),
    )

    with pytest.raises(GeminiVeoRemoteOutcomeUncertainError):
        await adapter.execute(
            request,
            _invocation(
                "google-gemini-api",
                "veo-3.1-fast-generate-preview",
                timeout_seconds=0.05,
            ),
            execution_allowed=lambda: True,
        )

    assert not list(root.glob("*.mp4"))


async def test_gemini_veo_real_transport_timeout_is_not_reported_as_external_cancel(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = _HttpSession(
        [
            _HttpResponse(_json_bytes({"name": "operations/op-1"})),
            _HttpResponse(b""),
        ]
    )
    session.responses[1].content = _HangingChunkStream()
    monkeypatch.setattr(
        video_provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: session,
    )
    root = tmp_path / "video-real-transport-timeout"
    root.mkdir()
    adapter = GeminiVeoProviderAdapter(
        AiohttpGeminiVeoTransport(
            api_key="private-gemini-key",
            poll_seconds=0.05,
        ),
        VideoArtifactStore(root),
        readiness_current=lambda: True,
    )
    request = ProviderRequest(
        "video-request-real-timeout",
        "trace-video-real-timeout",
        LogicalCapability.VIDEO_GENERATION,
        "discord-user-1",
        MediaGenerationInput("short animation"),
    )

    with pytest.raises(GeminiVeoRemoteOutcomeUncertainError) as captured:
        await adapter.execute(
            request,
            _invocation(
                "google-gemini-api",
                "veo-3.1-fast-generate-preview",
                timeout_seconds=0.05,
            ),
            execution_allowed=lambda: True,
        )

    assert not isinstance(captured.value, GeminiVeoRemoteOutcomeUncertainCancelledError)
    assert session.closed is True
    assert not list(root.glob("*.mp4"))


async def test_gemini_veo_external_cancellation_propagates_without_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "video-cancel"
    root.mkdir()
    adapter = GeminiVeoProviderAdapter(
        _CancelledVideo(),
        VideoArtifactStore(root),
        readiness_current=lambda: True,
    )
    request = ProviderRequest(
        "video-request-cancel",
        "trace-video-cancel",
        LogicalCapability.VIDEO_GENERATION,
        "discord-user-1",
        MediaGenerationInput("short animation"),
    )

    with pytest.raises(GeminiVeoRemoteOutcomeUncertainCancelledError) as cancelled:
        await adapter.execute(
            request,
            _invocation("google-gemini-api", "veo-3.1-fast-generate-preview"),
            execution_allowed=lambda: True,
        )

    assert isinstance(cancelled.value, asyncio.CancelledError)
    assert not list(root.glob("*.mp4"))


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
