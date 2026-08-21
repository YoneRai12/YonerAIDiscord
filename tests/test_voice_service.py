from __future__ import annotations

import asyncio
import inspect
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.music_generation.artifacts import MAX_WAV_BYTES
from yonerai_discord.modules.speech_synthesis import provider_voicevox as voicevox_provider_module
from yonerai_discord.modules.speech_synthesis.provider_voicevox import (
    MAX_VOICEVOX_PLAYBACK_WAV_BYTES,
    canonicalize_voicevox_playback_wav,
    canonicalize_voicevox_wav,
)
from yonerai_discord.modules.voice import SpeechQueue, SpeechRequest, SynthesizedSpeech
from yonerai_discord.modules.voice import VoicePlugin
from yonerai_discord.modules.voice import voicevox as voicevox_client_module
from yonerai_discord.modules.voice.adapter import VoiceGroup
from yonerai_discord.modules.voice.process import VoicevoxProcessError
from yonerai_discord.modules.voice.service import SpeechUnavailableError
from yonerai_discord.modules.voice.voicevox import (
    VoicevoxClient,
    VoicevoxConfigurationError,
    _read_limited,
)
from yonerai_discord.plugin import PluginManager, PluginStatus
from yonerai_discord.voice_contract import MIN_VOICEVOX_WAV_BYTES


class FakeSynthesizer:
    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
        self.calls += 1
        await asyncio.sleep(0)
        return SynthesizedSpeech(wav=b"RIFFfake")


def _voicevox_wav(*, duration_seconds: float = 1.0, sample_rate: int = 24_000) -> bytes:
    frame_count = round(duration_seconds * sample_rate)
    pcm = b"\0\0" * frame_count
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


class ToggleGuard:
    def __init__(self, *, on_evaluate=None) -> None:
        self.allowed = True
        self.evaluate_calls = 0
        self._on_evaluate = on_evaluate

    async def evaluate_fresh_member(self, _capability_id: str, **_kwargs: Any) -> Any:
        self.evaluate_calls += 1
        if self._on_evaluate is not None:
            result = self._on_evaluate(self.evaluate_calls)
            if inspect.isawaitable(result):
                await result
        return SimpleNamespace(allowed=self.allowed, actor_level=RbacLevel.EVERYONE)

    def currently_allowed(self, _capability_id: str, **_kwargs: Any) -> bool:
        return self.allowed


class FakeGuild:
    def __init__(self, *, on_fetch=None) -> None:
        self.id = 100
        self.fetch_calls = 0
        self._on_fetch = on_fetch

    async def fetch_member(self, user_id: int) -> Any:
        self.fetch_calls += 1
        if self._on_fetch is not None:
            self._on_fetch(self.fetch_calls)
        return SimpleNamespace(id=user_id)


class FakeResponse:
    def __init__(self, *, on_defer=None) -> None:
        self.deferred = False
        self._on_defer = on_defer

    async def defer(self, **kwargs: Any) -> None:
        assert kwargs == {"ephemeral": True, "thinking": True}
        self.deferred = True
        if self._on_defer is not None:
            self._on_defer()


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.messages.append((content, kwargs))


def _interaction(*, response: FakeResponse | None = None, guild: FakeGuild | None = None) -> Any:
    return SimpleNamespace(
        guild=guild or FakeGuild(),
        guild_id=100,
        channel_id=200,
        user=SimpleNamespace(id=300),
        response=response or FakeResponse(),
        followup=FakeFollowup(),
    )


@pytest.mark.asyncio
async def test_duplicate_inflight_speech_is_coalesced_and_cached() -> None:
    provider = FakeSynthesizer()
    queue = SpeechQueue(provider)
    request = SpeechRequest(text="こんにちは", guild_id=1, channel_id=2)
    first, second = await asyncio.gather(queue.synthesize(request), queue.synthesize(request))
    third = await queue.synthesize(request)
    assert first == second == third
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_closed_queue_is_unavailable() -> None:
    provider = FakeSynthesizer()
    queue = SpeechQueue(provider)
    await queue.close()
    assert not queue.available
    with pytest.raises(SpeechUnavailableError):
        await queue.synthesize(SpeechRequest(text="closed", guild_id=1, channel_id=2))
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_voice_synthesize_rechecks_policy_after_defer_before_provider_call() -> None:
    guard = ToggleGuard()
    provider = FakeSynthesizer()
    queue = SpeechQueue(provider)
    group = VoiceGroup(SimpleNamespace(capability_guard=guard), queue)
    interaction = _interaction(response=FakeResponse(on_defer=lambda: setattr(guard, "allowed", False)))

    await group.synthesize.callback(group, interaction, "こんにちは", 3)

    assert provider.calls == 0
    assert len(interaction.followup.messages) == 1
    assert "開始しませんでした" in interaction.followup.messages[0][0]
    assert "file" not in interaction.followup.messages[0][1]
    await queue.close()


@pytest.mark.asyncio
async def test_voice_synthesize_policy_off_during_provider_never_uploads_wav() -> None:
    class BlockingSynthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return SynthesizedSpeech(wav=b"RIFFblocked")

    guard = ToggleGuard()
    provider = BlockingSynthesizer()
    queue = SpeechQueue(provider)
    group = VoiceGroup(SimpleNamespace(capability_guard=guard), queue)
    interaction = _interaction()
    task = asyncio.create_task(group.synthesize.callback(group, interaction, "こんにちは", 3))
    await asyncio.wait_for(provider.started.wait(), timeout=1.0)
    guard.allowed = False
    provider.release.set()
    await task

    assert provider.calls == 1
    assert len(interaction.followup.messages) == 1
    assert "file" not in interaction.followup.messages[0][1]
    await queue.close()


@pytest.mark.parametrize(
    "final_change",
    ("policy_revoke", "guard_after_fetch", "guard_after_evaluate", "queue_close"),
)
@pytest.mark.asyncio
async def test_voice_synthesize_rechecks_rest_member_immediately_before_send(final_change: str) -> None:
    guard = ToggleGuard()
    provider = FakeSynthesizer()
    queue = SpeechQueue(provider)
    bot = SimpleNamespace(capability_guard=guard)

    def on_fetch(call_number: int) -> None:
        if call_number != 6:
            return
        if final_change == "policy_revoke":
            guard.allowed = False
        elif final_change == "guard_after_fetch":
            bot.capability_guard = ToggleGuard()

    async def on_evaluate(call_number: int) -> None:
        if call_number != 6:
            return
        if final_change == "guard_after_evaluate":
            bot.capability_guard = ToggleGuard()
        elif final_change == "queue_close":
            await queue.close()

    guard._on_evaluate = on_evaluate
    guild = FakeGuild(on_fetch=on_fetch)
    group = VoiceGroup(bot, queue)
    interaction = _interaction(guild=guild)

    await group.synthesize.callback(group, interaction, "こんにちは", 3)

    assert provider.calls == 1
    assert guild.fetch_calls == 6
    assert len(interaction.followup.messages) == 1
    assert "file" not in interaction.followup.messages[0][1]
    await queue.close()


@pytest.mark.asyncio
async def test_voice_queue_stop_during_provider_and_later_request_never_upload_or_restart() -> None:
    class BlockingSynthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            self.calls += 1
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    guard = ToggleGuard()
    provider = BlockingSynthesizer()
    queue = SpeechQueue(provider)
    group = VoiceGroup(SimpleNamespace(capability_guard=guard), queue)
    first = _interaction()
    task = asyncio.create_task(group.synthesize.callback(group, first, "最初", 3))
    await asyncio.wait_for(provider.started.wait(), timeout=1.0)
    await queue.close()
    await task

    assert provider.calls == 1
    assert len(first.followup.messages) == 1
    assert "file" not in first.followup.messages[0][1]

    second = _interaction()
    await group.synthesize.callback(group, second, "停止後", 3)
    assert provider.calls == 1
    assert len(second.followup.messages) == 1
    assert "file" not in second.followup.messages[0][1]


@pytest.mark.asyncio
async def test_voice_plugin_begin_close_cancels_inflight_and_rejects_new_synthesis() -> None:
    class BlockingSynthesizer(FakeSynthesizer):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            self.calls += 1
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    provider = BlockingSynthesizer()
    queue = SpeechQueue(provider)
    plugin = VoicePlugin()
    plugin.queue = queue
    request = SpeechRequest(text="停止確認", guild_id=1, channel_id=2)
    task = asyncio.create_task(queue.synthesize(request))
    await asyncio.wait_for(provider.started.wait(), timeout=1)

    await plugin.begin_close()

    with pytest.raises(SpeechUnavailableError):
        await task
    with pytest.raises(SpeechUnavailableError):
        await queue.synthesize(request)
    assert provider.calls == 1
    await plugin.stop()


def test_voicevox_rejects_remote_endpoint_without_opt_in() -> None:
    with pytest.raises(VoicevoxConfigurationError):
        VoicevoxClient(endpoint="https://voice.example.com")


class FakeHttpResponse:
    def __init__(self, status: int, payload: bytes, *, declared_content_length: int | None = None) -> None:
        self.status = status
        self._payload = payload
        self.content_length = len(payload) if declared_content_length is None else declared_content_length
        self.content = None

    async def __aenter__(self) -> FakeHttpResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def read(self) -> bytes:
        return self._payload


class FakeHttpSession:
    def __init__(self, responses: list[FakeHttpResponse]) -> None:
        self.closed = False
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_voicevox_posts_never_follow_redirects() -> None:
    session = FakeHttpSession([FakeHttpResponse(302, b"redirect")])
    client = VoicevoxClient(endpoint="http://127.0.0.1:50021")
    client._session = session

    with pytest.raises(RuntimeError, match="audio query failed"):
        await client.synthesize(SpeechRequest(text="こんにちは", guild_id=1, channel_id=2))

    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    await client.close()


@pytest.mark.asyncio
async def test_voicevox_requires_http_200_and_disables_redirects_for_both_posts() -> None:
    wav = _voicevox_wav()
    session = FakeHttpSession(
        [
            FakeHttpResponse(200, b'{"speedScale": 1.0}'),
            FakeHttpResponse(200, wav),
        ]
    )
    client = VoicevoxClient(endpoint="http://127.0.0.1:50021")
    client._session = session

    result = await client.synthesize(
        SpeechRequest(
            text="こんにちは",
            guild_id=1,
            channel_id=2,
            speed_scale=1.25,
            volume_scale=0.75,
        )
    )

    assert result.wav[:12] == b"RIFF" + struct.pack("<I", len(result.wav) - 8) + b"WAVE"
    assert struct.unpack_from("<I", result.wav, 24)[0] == 48_000
    assert result.sample_rate == 48_000
    assert len(session.calls) == 2
    assert all(kwargs["allow_redirects"] is False for _, kwargs in session.calls)
    assert session.calls[1][1]["json"]["speedScale"] == 1.25
    assert session.calls[1][1]["json"]["volumeScale"] == 0.75
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("duration_seconds", "text"),
    ((0.25, "short utterance"), (31.0, "x" * 500)),
    ids=("shorter-than-artifact-minimum", "longer-than-artifact-maximum"),
)
async def test_voicevox_direct_playback_accepts_valid_non_artifact_durations(
    duration_seconds: float,
    text: str,
) -> None:
    session = FakeHttpSession(
        [
            FakeHttpResponse(200, b'{"speedScale": 1.0}'),
            FakeHttpResponse(200, _voicevox_wav(duration_seconds=duration_seconds)),
        ]
    )
    client = VoicevoxClient(endpoint="http://127.0.0.1:50021")
    client._session = session

    result = await client.synthesize(SpeechRequest(text=text, guild_id=1, channel_id=2))

    assert result.sample_rate == 48_000
    assert len(result.wav) == 44 + round(duration_seconds * 48_000) * 2
    await client.close()


@pytest.mark.asyncio
async def test_voicevox_direct_playback_separates_download_and_converted_size_limits() -> None:
    raw_size = 20 * 1024 * 1024
    frames = (raw_size - 44) // 2
    payload = _voicevox_wav(duration_seconds=frames / 24_000)
    configured_limit = 25 * 1024 * 1024
    expected_output_size = 44 + (len(payload) - 44) * 2
    assert len(payload) == raw_size
    assert len(payload) < configured_limit < expected_output_size
    assert expected_output_size < MAX_VOICEVOX_PLAYBACK_WAV_BYTES
    session = FakeHttpSession(
        [
            FakeHttpResponse(200, b'{"speedScale": 1.0}'),
            FakeHttpResponse(200, payload),
        ]
    )
    client = VoicevoxClient(endpoint="http://127.0.0.1:50021")
    client._session = session

    result = await client.synthesize(SpeechRequest(text="x" * 500, guild_id=1, channel_id=2))

    assert result.sample_rate == 48_000
    assert len(result.wav) == expected_output_size
    with pytest.raises(RuntimeError, match="invalid WAV"):
        canonicalize_voicevox_wav(payload)
    await client.close()


@pytest.mark.asyncio
async def test_voicevox_client_propagates_configured_response_limit_to_playback_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_limit = MAX_WAV_BYTES + 4_096
    limits: list[int] = []
    delegate = canonicalize_voicevox_playback_wav

    def tracked_canonicalizer(data: object, *, max_input_bytes: int) -> bytes:
        limits.append(max_input_bytes)
        return delegate(data, max_input_bytes=max_input_bytes)

    monkeypatch.setattr(
        voicevox_client_module,
        "canonicalize_voicevox_playback_wav",
        tracked_canonicalizer,
    )
    session = FakeHttpSession(
        [
            FakeHttpResponse(200, b'{"speedScale": 1.0}'),
            FakeHttpResponse(200, _voicevox_wav()),
        ]
    )
    client = VoicevoxClient(
        endpoint="http://127.0.0.1:50021",
        max_response_bytes=configured_limit,
    )
    client._session = session

    await client.synthesize(SpeechRequest(text="voice", guild_id=1, channel_id=2))

    assert limits == [configured_limit]
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        b"NOPE" + _voicevox_wav()[4:],
        _voicevox_wav()[:-1],
        _voicevox_wav(sample_rate=22_050),
    ),
    ids=("malformed", "truncated", "unsupported-format"),
)
async def test_voicevox_direct_playback_rejects_invalid_wav_content_free(payload: bytes) -> None:
    session = FakeHttpSession(
        [
            FakeHttpResponse(200, b'{"speedScale": 1.0}'),
            FakeHttpResponse(200, payload),
        ]
    )
    client = VoicevoxClient(endpoint="http://127.0.0.1:50021")
    client._session = session

    with pytest.raises(RuntimeError, match="invalid WAV") as caught:
        await client.synthesize(SpeechRequest(text="voice", guild_id=1, channel_id=2))

    assert payload not in str(caught.value).encode()
    await client.close()


@pytest.mark.asyncio
async def test_voicevox_response_limit_is_enforced_without_trusting_content_length() -> None:
    response = SimpleNamespace(content_length=None, content=None)

    async def read() -> bytes:
        return b"x" * 1_025

    response.read = read
    with pytest.raises(RuntimeError, match="exceeds"):
        await _read_limited(response, 1_024)


@pytest.mark.asyncio
async def test_voicevox_fixed_wav_cap_stops_chunked_stream_at_boundary_content_free() -> None:
    private_tail = b"private-tail"

    class ChunkedContent:
        def iter_chunked(self, _size: int):
            async def chunks():
                yield b"x" * MAX_WAV_BYTES
                yield private_tail

            return chunks()

    response = SimpleNamespace(content_length=None, content=ChunkedContent())
    with pytest.raises(RuntimeError, match="exceeds") as caught:
        await _read_limited(response, MAX_WAV_BYTES)

    assert private_tail not in str(caught.value).encode()


@pytest.mark.asyncio
async def test_voicevox_synthesis_rejects_raw_configured_limit_plus_one_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_limit = MIN_VOICEVOX_WAV_BYTES + 1
    frames = (configured_limit + 1 - 44) // 2
    payload = _voicevox_wav(duration_seconds=frames / 24_000)
    conversion_started: list[str] = []

    def unexpected_conversion(*_args: object, **_kwargs: object) -> bytes:
        conversion_started.append("started")
        raise AssertionError("WAV conversion started after the configured response limit")

    monkeypatch.setattr(
        voicevox_client_module,
        "canonicalize_voicevox_playback_wav",
        unexpected_conversion,
    )
    session = FakeHttpSession(
        [
            FakeHttpResponse(200, b'{"speedScale": 1.0}'),
            FakeHttpResponse(200, payload),
        ]
    )
    client = VoicevoxClient(
        endpoint="http://127.0.0.1:50021",
        max_response_bytes=configured_limit,
    )
    client._session = session

    with pytest.raises(RuntimeError, match="exceeds"):
        await client.synthesize(SpeechRequest(text="voice", guild_id=1, channel_id=2))

    assert len(payload) == configured_limit + 1
    assert conversion_started == []
    assert len(session.calls) == 2
    await client.close()


def test_voicevox_rejects_unsafe_response_limit() -> None:
    with pytest.raises(VoicevoxConfigurationError, match="MAX_RESPONSE"):
        VoicevoxClient(
            endpoint="http://127.0.0.1:50021",
            max_response_bytes=MIN_VOICEVOX_WAV_BYTES - 1,
        )

    client = VoicevoxClient(
        endpoint="http://127.0.0.1:50021",
        max_response_bytes=MIN_VOICEVOX_WAV_BYTES,
    )
    assert client._max_response_bytes == MIN_VOICEVOX_WAV_BYTES
    maximum = VoicevoxClient(
        endpoint="http://127.0.0.1:50021",
        max_response_bytes=50 * 1024 * 1024,
    )
    assert maximum._max_response_bytes == 50 * 1024 * 1024


@pytest.mark.parametrize(
    "maximum",
    (True, float(25 * 1024 * 1024), "26214400", 50 * 1024 * 1024 + 1),
)
def test_voicevox_rejects_non_code_owned_response_limit_types_content_free(maximum: object) -> None:
    with pytest.raises(VoicevoxConfigurationError, match="MAX_RESPONSE") as caught:
        VoicevoxClient(
            endpoint="http://127.0.0.1:50021",
            max_response_bytes=maximum,  # type: ignore[arg-type]
        )
    assert str(maximum) not in str(caught.value)


def test_voice_synthesize_exposes_only_code_owned_speaker_choice() -> None:
    group = VoiceGroup(SimpleNamespace(capability_guard=ToggleGuard()), SpeechQueue(FakeSynthesizer()))
    parameter = next(parameter for parameter in group.synthesize.parameters if parameter.name == "speaker_id")

    assert [(choice.name, choice.value) for choice in parameter.choices] == [("3", 3)]


def test_speech_request_has_stable_key() -> None:
    request = SpeechRequest(text="ＡＢＣ", guild_id=1, channel_id=2)
    assert request.text == "ABC"
    assert request.key == request.key
    assert len(request.key) == 64
    assert request.key == SpeechRequest(text="ABC", guild_id=1, channel_id=2).key
    assert (
        request.key
        != SpeechRequest(
            text="ＡＢＣ",
            guild_id=1,
            channel_id=2,
            volume_scale=0.5,
        ).key
    )


@pytest.mark.parametrize("speaker_id", (-1, 0, 1, 7, True, 2**31))
def test_speech_request_rejects_speaker_outside_code_owned_allowlist(speaker_id: object) -> None:
    with pytest.raises(ValueError, match="speaker_not_allowed") as caught:
        SpeechRequest(text="voice", guild_id=1, channel_id=2, speaker_id=speaker_id)  # type: ignore[arg-type]

    assert str(speaker_id) not in str(caught.value)


def test_voicevox_wav_contract_accepts_default_24khz_and_canonicalizes_to_48khz() -> None:
    result = canonicalize_voicevox_wav(_voicevox_wav())

    assert result[:4] == b"RIFF"
    assert result[8:12] == b"WAVE"
    assert struct.unpack_from("<I", result, 4)[0] == len(result) - 8
    assert struct.unpack_from("<I", result, 24)[0] == 48_000


def test_voicevox_playback_preserves_valid_48khz_wav() -> None:
    payload = _voicevox_wav(sample_rate=48_000)

    result = canonicalize_voicevox_playback_wav(
        payload,
        max_input_bytes=MAX_VOICEVOX_PLAYBACK_WAV_BYTES,
    )

    assert result == payload


def test_voicevox_playback_rejects_24khz_expansion_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm_size = (MAX_VOICEVOX_PLAYBACK_WAV_BYTES - 44) // 2 + 2
    pcm = b"\0" * pcm_size
    payload = (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 24_000, 48_000, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )
    conversion_started: list[str] = []

    def unexpected_conversion(*_args: object, **_kwargs: object) -> bytes:
        conversion_started.append("started")
        raise AssertionError("24 kHz conversion started before expanded-size validation")

    monkeypatch.setattr(voicevox_provider_module, "_upsample_24khz_pcm16", unexpected_conversion, raising=False)
    monkeypatch.setattr(voicevox_provider_module, "bytearray", unexpected_conversion, raising=False)

    assert len(payload) < MAX_VOICEVOX_PLAYBACK_WAV_BYTES
    assert 44 + (len(payload) - 44) * 2 > MAX_VOICEVOX_PLAYBACK_WAV_BYTES
    with pytest.raises(RuntimeError, match="exceeds the fixed size limit"):
        canonicalize_voicevox_playback_wav(
            payload,
            max_input_bytes=MAX_VOICEVOX_PLAYBACK_WAV_BYTES,
        )
    assert conversion_started == []


def test_voicevox_playback_accepts_24khz_expansion_above_input_limit() -> None:
    configured_limit = MAX_WAV_BYTES + 4_096
    frames = (configured_limit - 44) // 2
    payload = _voicevox_wav(duration_seconds=frames / 24_000)
    expected_output_size = 44 + (len(payload) - 44) * 2

    assert len(payload) <= configured_limit < expected_output_size
    result = canonicalize_voicevox_playback_wav(
        payload,
        max_input_bytes=configured_limit,
    )

    assert len(result) == expected_output_size


@pytest.mark.parametrize(
    "maximum",
    (True, float(MAX_WAV_BYTES), MIN_VOICEVOX_WAV_BYTES - 1, 50 * 1024 * 1024 + 1),
)
def test_voicevox_playback_rejects_arbitrary_size_limit_content_free(maximum: object) -> None:
    with pytest.raises(RuntimeError, match="size limit") as caught:
        canonicalize_voicevox_playback_wav(
            _voicevox_wav(),
            max_input_bytes=maximum,  # type: ignore[arg-type]
        )
    assert str(maximum) not in str(caught.value)


def test_voicevox_playback_output_limit_is_not_caller_configurable() -> None:
    with pytest.raises(TypeError, match="max_wav_bytes"):
        canonicalize_voicevox_playback_wav(
            _voicevox_wav(),
            max_wav_bytes=MAX_VOICEVOX_PLAYBACK_WAV_BYTES,  # type: ignore[call-arg]
        )


def test_voicevox_playback_upsamples_near_cap_24khz_exactly() -> None:
    frames = (MAX_VOICEVOX_PLAYBACK_WAV_BYTES - 44) // 4
    pairs, tail = divmod(frames, 2)
    pcm = b"\x01\x02\x03\x04" * pairs + b"\x01\x02" * tail
    payload = (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 24_000, 48_000, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )

    result = canonicalize_voicevox_playback_wav(
        payload,
        max_input_bytes=MAX_VOICEVOX_PLAYBACK_WAV_BYTES,
    )

    assert len(result) == MAX_VOICEVOX_PLAYBACK_WAV_BYTES
    assert struct.unpack_from("<I", result, 4)[0] == len(result) - 8
    assert struct.unpack_from("<I", result, 24)[0] == 48_000
    assert struct.unpack_from("<I", result, 40)[0] == len(result) - 44
    assert result[44:60] == b"\x01\x02\x01\x02\x03\x04\x03\x04" * 2
    assert result[-8:] == (b"\x03\x04\x03\x04\x01\x02\x01\x02" if tail else b"\x01\x02\x01\x02\x03\x04\x03\x04")


def test_voicevox_playback_upsamples_stereo_frames_without_channel_reordering() -> None:
    pcm = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    payload = (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 2, 24_000, 96_000, 4, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )

    result = canonicalize_voicevox_playback_wav(
        payload,
        max_input_bytes=MAX_VOICEVOX_PLAYBACK_WAV_BYTES,
    )

    assert struct.unpack_from("<I", result, 24)[0] == 48_000
    assert result[44:] == b"\x01\x02\x03\x04" * 2 + b"\x05\x06\x07\x08" * 2


@pytest.mark.parametrize(("block_align", "maximum_slices"), ((2, 2), (4, 4)), ids=("mono", "stereo"))
def test_voicevox_24khz_upsample_has_fixed_slice_and_allocation_shape(
    monkeypatch: pytest.MonkeyPatch,
    block_align: int,
    maximum_slices: int,
) -> None:
    source_size = ((MAX_WAV_BYTES - 44) // 2 // block_align) * block_align
    slices: list[slice] = []

    class SliceCountingBytes(bytes):
        def __getitem__(self, key):
            if isinstance(key, slice):
                slices.append(key)
                if len(slices) > maximum_slices:
                    raise AssertionError("24 kHz upsample exceeded its fixed source-slice budget")
            return super().__getitem__(key)

    source = SliceCountingBytes(b"\0" * source_size)
    allocations: list[int] = []
    allocate = bytearray

    def tracked_bytearray(size: int) -> bytearray:
        allocations.append(size)
        return allocate(size)

    monkeypatch.setattr(voicevox_provider_module, "bytearray", tracked_bytearray, raising=False)

    result = voicevox_provider_module._upsample_24khz_pcm16(source, block_align=block_align)

    assert len(result) == source_size * 2
    assert [(value.start, value.stop, value.step) for value in slices] == [
        (byte_offset, None, block_align) for byte_offset in range(block_align)
    ]
    assert allocations == [source_size * 2]


@pytest.mark.parametrize(
    ("channels", "expected_block_align"),
    ((1, 2), (2, 4)),
    ids=("mono", "stereo"),
)
def test_voicevox_24khz_canonicalizer_calls_bounded_upsample_once(
    monkeypatch: pytest.MonkeyPatch,
    channels: int,
    expected_block_align: int,
) -> None:
    source_size = (voicevox_provider_module.MAX_WAV_BYTES - 44) // 2 // expected_block_align * expected_block_align
    pcm = b"\0" * source_size
    payload = (
        b"RIFF"
        + (36 + len(pcm)).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + channels.to_bytes(2, "little")
        + (24_000).to_bytes(4, "little")
        + (24_000 * expected_block_align).to_bytes(4, "little")
        + expected_block_align.to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + len(pcm).to_bytes(4, "little")
        + pcm
    )
    calls: list[tuple[int, int]] = []
    delegate = voicevox_provider_module._upsample_24khz_pcm16
    builtin_range = range

    def guarded_range(*args: int) -> range:
        if args != (expected_block_align,):
            raise AssertionError("canonicalizer bypassed bounded upsample helper")
        return builtin_range(*args)

    def tracked_upsample(value: bytes, *, block_align: int) -> bytes:
        calls.append((len(value), block_align))
        return delegate(value, block_align=block_align)

    monkeypatch.setattr(voicevox_provider_module, "range", guarded_range, raising=False)
    monkeypatch.setattr(
        voicevox_provider_module,
        "_upsample_24khz_pcm16",
        tracked_upsample,
    )

    result = canonicalize_voicevox_playback_wav(
        payload,
        max_input_bytes=MAX_VOICEVOX_PLAYBACK_WAV_BYTES,
    )

    assert calls == [(source_size, expected_block_align)]
    assert len(result) == 44 + source_size * 2
    assert int.from_bytes(result[24:28], "little") == 48_000


@pytest.mark.parametrize(
    "payload",
    (b"RIFF", _voicevox_wav(duration_seconds=0.5), _voicevox_wav(duration_seconds=31.0)),
    ids=("truncated", "shorter-than-artifact-minimum", "longer-than-artifact-maximum"),
)
def test_voicevox_artifact_wav_contract_rejects_truncated_or_outside_duration_content_free(
    payload: bytes,
) -> None:
    with pytest.raises(RuntimeError, match="VOICEVOX") as caught:
        canonicalize_voicevox_wav(payload)

    assert payload not in str(caught.value).encode()


def test_voicevox_wav_contract_rejects_extra_chunks() -> None:
    wav = _voicevox_wav()
    extra = wav[:36] + b"JUNK\x02\0\0\0\0\0" + wav[36:]
    extra = extra[:4] + struct.pack("<I", len(extra) - 8) + extra[8:]

    with pytest.raises(RuntimeError, match="chunks"):
        canonicalize_voicevox_wav(extra)


def test_speech_request_removes_url_and_discord_identifiers_before_synthesis() -> None:
    request = SpeechRequest(
        text=" ＡＢＣ https://example.invalid/private <@12345678901234567> <#23456789012345678> @everyone @here ",
        guild_id=1,
        channel_id=2,
    )

    assert request.text == "ABC URL メンション メンション 全体通知 全体通知"
    for secret_like_input in ("https://example.invalid/private", "12345678901234567", "23456789012345678"):
        assert secret_like_input not in request.text
        assert secret_like_input not in repr(request)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("\x00not-sent", "text_control_character"),
        ("C1\u0085not-sent", "text_control_character"),
        ("zero\u200bwidth", "text_control_character"),
        ("<@12345678901234567>", "mention_only"),
        ("Ａ" * 501, "text_too_long"),
    ],
)
def test_speech_request_rejects_unsafe_text_before_provider_boundary(text: str, code: str) -> None:
    with pytest.raises(ValueError, match=code) as caught:
        SpeechRequest(text=text, guild_id=1, channel_id=2)

    assert text not in str(caught.value)


@pytest.mark.parametrize("volume", (-0.1, 2.1, True, float("nan")))
def test_speech_request_rejects_unsafe_volume_scale(volume: object) -> None:
    with pytest.raises(ValueError, match="volume_scale"):
        SpeechRequest(  # type: ignore[arg-type]
            text="音量",
            guild_id=1,
            channel_id=2,
            volume_scale=volume,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "endpoint", "allow_remote", "expected_ready"),
    [
        (False, "http://127.0.0.1:50021", False, False),
        (True, "http://127.0.0.1:50021", False, True),
        (True, "https://voice.example.com", False, False),
    ],
)
async def test_voice_plugin_uses_typed_settings_and_publishes_readiness(
    enabled: bool,
    endpoint: str,
    allow_remote: bool,
    expected_ready: bool,
) -> None:
    class ProbeClient:
        def __init__(self, **kwargs: Any) -> None:
            if kwargs["endpoint"].startswith("https://") and kwargs["allow_remote"] is False:
                raise VoicevoxConfigurationError("remote disabled")
            self.closed = False

        async def probe_version(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds == 5.0
            return True

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            return SynthesizedSpeech(wav=b"RIFFfake")

        async def close(self) -> None:
            self.closed = True

    added = []
    removed = []
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            voice_enabled=enabled,
            voicevox_url=endpoint,
            voice_allow_remote=allow_remote,
            voice_timeout_seconds=5.0,
            voice_max_response_bytes=1_048_576,
        ),
        tree=SimpleNamespace(add_command=added.append, remove_command=removed.append),
    )
    plugin = VoicePlugin(client_factory=ProbeClient)  # type: ignore[arg-type]

    await plugin.start(bot)
    assert len(added) == 1
    assert bot.runtime_capability_readiness["cap-can-0416"] is expected_ready
    assert bot.speech_queue.available is expected_ready

    await plugin.stop()
    assert removed == ["voice"]
    assert "cap-can-0416" not in bot.runtime_capability_readiness


@pytest.mark.asyncio
async def test_voice_plugin_probe_failure_is_truthfully_unavailable() -> None:
    class UnavailableClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.closed = False

        async def probe_version(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds == 5.0
            return False

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            raise AssertionError("unavailable client must not be used")

        async def close(self) -> None:
            self.closed = True

    added = []
    client: UnavailableClient | None = None

    def client_factory(**kwargs: Any) -> UnavailableClient:
        nonlocal client
        client = UnavailableClient(**kwargs)
        return client

    bot = SimpleNamespace(
        settings=SimpleNamespace(
            voice_enabled=True,
            voicevox_url="http://127.0.0.1:50021",
            voice_allow_remote=False,
            voice_timeout_seconds=5.0,
            voice_max_response_bytes=1_048_576,
            voicevox_managed_process_enabled=False,
        ),
        tree=SimpleNamespace(add_command=added.append, remove_command=lambda _name: None),
    )
    plugin = VoicePlugin(client_factory=client_factory)  # type: ignore[arg-type]

    await plugin.start(bot)

    assert client is not None and client.closed is True
    assert bot.speech_queue.available is False
    assert bot.runtime_capability_readiness["cap-can-0416"] is False
    assert len(added) == 1
    await plugin.stop()


@pytest.mark.asyncio
async def test_voice_plugin_managed_lifecycle_cleanup_survives_partial_start() -> None:
    class ReadyClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.closed = False

        async def probe_version(self, *, timeout_seconds: float) -> bool:
            return True

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            return SynthesizedSpeech(wav=b"RIFFfake")

        async def close(self) -> None:
            self.closed = True

    class Lifecycle:
        def __init__(self, **_kwargs: Any) -> None:
            self.stop_calls = 0

        async def ensure_ready(self, probe: Any) -> bool:
            assert await probe(1.0) is True
            return True

        async def stop(self) -> None:
            self.stop_calls += 1

    lifecycle: Lifecycle | None = None

    def lifecycle_factory(**kwargs: Any) -> Lifecycle:
        nonlocal lifecycle
        lifecycle = Lifecycle(**kwargs)
        return lifecycle

    def add_command(_command: Any) -> None:
        raise RuntimeError("tree unavailable")

    removed = []
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            voice_enabled=True,
            voicevox_url="http://127.0.0.1:50021",
            voice_allow_remote=False,
            voice_timeout_seconds=5.0,
            voice_max_response_bytes=1_048_576,
            voicevox_managed_process_enabled=True,
            voicevox_managed_executable=None,
            voicevox_managed_startup_timeout_seconds=15.0,
            voicevox_managed_shutdown_timeout_seconds=8.0,
        ),
        tree=SimpleNamespace(add_command=add_command, remove_command=removed.append),
    )
    plugin = VoicePlugin(  # type: ignore[arg-type]
        client_factory=ReadyClient,
        process_lifecycle_factory=lifecycle_factory,
    )

    with pytest.raises(RuntimeError, match="tree unavailable"):
        await plugin.start(bot)

    assert lifecycle is not None and lifecycle.stop_calls == 1
    assert not hasattr(bot, "speech_queue")
    assert "cap-can-0416" not in bot.runtime_capability_readiness
    assert plugin.queue is None
    assert plugin._client is None
    assert plugin._process_lifecycle is None
    assert removed == []


@pytest.mark.asyncio
async def test_voice_plugin_managed_start_failure_cleans_lifecycle_and_stays_unavailable() -> None:
    class ReadyClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.closed = False

        async def probe_version(self, *, timeout_seconds: float) -> bool:
            return False

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            raise AssertionError("unavailable client must not be used")

        async def close(self) -> None:
            self.closed = True

    class FailingLifecycle:
        def __init__(self, **_kwargs: Any) -> None:
            self.stop_calls = 0

        async def ensure_ready(self, _probe: Any) -> bool:
            raise VoicevoxProcessError("voicevox_process_start_failed")

        async def stop(self) -> None:
            self.stop_calls += 1

    lifecycle: FailingLifecycle | None = None

    def lifecycle_factory(**kwargs: Any) -> FailingLifecycle:
        nonlocal lifecycle
        lifecycle = FailingLifecycle(**kwargs)
        return lifecycle

    bot = SimpleNamespace(
        settings=SimpleNamespace(
            voice_enabled=True,
            voicevox_url="http://127.0.0.1:50021",
            voice_allow_remote=False,
            voice_timeout_seconds=5.0,
            voice_max_response_bytes=1_048_576,
            voicevox_managed_process_enabled=True,
            voicevox_managed_executable=None,
            voicevox_managed_startup_timeout_seconds=15.0,
            voicevox_managed_shutdown_timeout_seconds=8.0,
        ),
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
    )
    plugin = VoicePlugin(  # type: ignore[arg-type]
        client_factory=ReadyClient,
        process_lifecycle_factory=lifecycle_factory,
    )

    await plugin.start(bot)

    assert lifecycle is not None and lifecycle.stop_calls == 1
    assert bot.speech_queue.available is False
    assert bot.runtime_capability_readiness["cap-can-0416"] is False
    await plugin.stop()
    assert lifecycle.stop_calls == 2


@pytest.mark.asyncio
async def test_voice_plugin_owned_process_death_revokes_queue_and_runtime_readiness() -> None:
    class ReadyClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.synthesis_calls = 0

        async def probe_version(self, *, timeout_seconds: float) -> bool:
            return True

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            self.synthesis_calls += 1
            return SynthesizedSpeech(wav=b"RIFFfake")

        async def close(self) -> None:
            return None

    class Lifecycle:
        def __init__(self, **_kwargs: Any) -> None:
            self.owned_process_present = True
            self.owns_process = True

        async def ensure_ready(self, probe: Any) -> bool:
            return await probe(1.0)

        async def stop(self) -> None:
            self.owns_process = False

    client: ReadyClient | None = None
    lifecycle: Lifecycle | None = None

    def client_factory(**kwargs: Any) -> ReadyClient:
        nonlocal client
        client = ReadyClient(**kwargs)
        return client

    def lifecycle_factory(**kwargs: Any) -> Lifecycle:
        nonlocal lifecycle
        lifecycle = Lifecycle(**kwargs)
        return lifecycle

    bot = SimpleNamespace(
        settings=SimpleNamespace(
            voice_enabled=True,
            voicevox_url="http://127.0.0.1:50021",
            voice_allow_remote=False,
            voice_timeout_seconds=5.0,
            voice_max_response_bytes=1_048_576,
            voicevox_managed_process_enabled=True,
            voicevox_managed_executable=None,
            voicevox_managed_startup_timeout_seconds=15.0,
            voicevox_managed_shutdown_timeout_seconds=8.0,
        ),
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
    )
    plugin = VoicePlugin(  # type: ignore[arg-type]
        client_factory=client_factory,
        process_lifecycle_factory=lifecycle_factory,
        readiness_poll_seconds=0.01,
    )
    await plugin.start(bot)
    assert bot.speech_queue.available is True
    assert lifecycle is not None

    lifecycle.owns_process = False

    assert bot.speech_queue.available is False
    with pytest.raises(SpeechUnavailableError, match="not ready"):
        await bot.speech_queue.synthesize(SpeechRequest(text="provider death", guild_id=1, channel_id=2))
    assert client is not None and client.synthesis_calls == 0
    async with asyncio.timeout(1.0):
        while bot.runtime_capability_readiness["cap-can-0416"] is not False:
            await asyncio.sleep(0.01)

    await plugin.stop()


@pytest.mark.asyncio
async def test_voice_plugin_alive_owned_process_still_requires_http_readiness() -> None:
    class FlappingClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.probe_calls = 0

        async def probe_version(self, *, timeout_seconds: float) -> bool:
            self.probe_calls += 1
            return self.probe_calls == 1

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            raise AssertionError("HTTP-unready provider must not be used")

        async def close(self) -> None:
            return None

    class Lifecycle:
        def __init__(self, **_kwargs: Any) -> None:
            self.owned_process_present = True
            self.owns_process = True

        async def ensure_ready(self, probe: Any) -> bool:
            return await probe(1.0)

        async def stop(self) -> None:
            self.owns_process = False

    client: FlappingClient | None = None

    def client_factory(**kwargs: Any) -> FlappingClient:
        nonlocal client
        client = FlappingClient(**kwargs)
        return client

    bot = SimpleNamespace(
        settings=SimpleNamespace(
            voice_enabled=True,
            voicevox_url="http://127.0.0.1:50021",
            voice_allow_remote=False,
            voice_timeout_seconds=5.0,
            voice_max_response_bytes=1_048_576,
            voicevox_managed_process_enabled=True,
            voicevox_managed_executable=None,
            voicevox_managed_startup_timeout_seconds=15.0,
            voicevox_managed_shutdown_timeout_seconds=8.0,
        ),
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
    )
    plugin = VoicePlugin(  # type: ignore[arg-type]
        client_factory=client_factory,
        process_lifecycle_factory=Lifecycle,
        readiness_poll_seconds=0.01,
    )
    await plugin.start(bot)

    async with asyncio.timeout(1.0):
        while bot.runtime_capability_readiness["cap-can-0416"] is not False:
            await asyncio.sleep(0.01)

    assert client is not None and client.probe_calls >= 2
    assert bot.speech_queue.available is False
    await plugin.stop()


@pytest.mark.asyncio
async def test_voice_plugin_external_engine_is_periodically_reprobed() -> None:
    class FlappingClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.probe_calls = 0

        async def probe_version(self, *, timeout_seconds: float) -> bool:
            self.probe_calls += 1
            return self.probe_calls == 1

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            raise AssertionError("stale external provider must not be used")

        async def close(self) -> None:
            return None

    client: FlappingClient | None = None

    def client_factory(**kwargs: Any) -> FlappingClient:
        nonlocal client
        client = FlappingClient(**kwargs)
        return client

    bot = SimpleNamespace(
        settings=SimpleNamespace(
            voice_enabled=True,
            voicevox_url="http://127.0.0.1:50021",
            voice_allow_remote=False,
            voice_timeout_seconds=5.0,
            voice_max_response_bytes=1_048_576,
            voicevox_managed_process_enabled=False,
        ),
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
    )
    plugin = VoicePlugin(client_factory=client_factory, readiness_poll_seconds=0.01)  # type: ignore[arg-type]
    await plugin.start(bot)

    async with asyncio.timeout(1.0):
        while bot.runtime_capability_readiness["cap-can-0416"] is not False:
            await asyncio.sleep(0.01)

    assert client is not None and client.probe_calls >= 2
    assert bot.speech_queue.available is False
    await plugin.stop()


@pytest.mark.asyncio
async def test_voice_plugin_cleanup_unconfirmed_quarantines_and_second_stop_retries() -> None:
    class ReadyClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.closed = False

        async def probe_version(self, *, timeout_seconds: float) -> bool:
            return True

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            return SynthesizedSpeech(wav=b"RIFFfake")

        async def close(self) -> None:
            self.closed = True

    class RetryLifecycle:
        def __init__(self, **_kwargs: Any) -> None:
            self.stop_calls = 0
            self.owned_process_present = True
            self.owns_process = True

        async def ensure_ready(self, probe: Any) -> bool:
            return await probe(1.0)

        async def stop(self) -> None:
            self.stop_calls += 1
            if self.stop_calls == 1:
                raise VoicevoxProcessError("voicevox_process_cleanup_unconfirmed")
            self.owns_process = False

    lifecycle: RetryLifecycle | None = None

    def lifecycle_factory(**kwargs: Any) -> RetryLifecycle:
        nonlocal lifecycle
        lifecycle = RetryLifecycle(**kwargs)
        return lifecycle

    added = []
    removed = []
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            voice_enabled=True,
            voicevox_url="http://127.0.0.1:50021",
            voice_allow_remote=False,
            voice_timeout_seconds=5.0,
            voice_max_response_bytes=1_048_576,
            voicevox_managed_process_enabled=True,
            voicevox_managed_executable=None,
            voicevox_managed_startup_timeout_seconds=15.0,
            voicevox_managed_shutdown_timeout_seconds=8.0,
        ),
        tree=SimpleNamespace(add_command=added.append, remove_command=removed.append),
    )
    plugin = VoicePlugin(  # type: ignore[arg-type]
        client_factory=ReadyClient,
        process_lifecycle_factory=lifecycle_factory,
        readiness_poll_seconds=30.0,
    )
    await plugin.start(bot)

    with pytest.raises(VoicevoxProcessError, match="cleanup_unconfirmed"):
        await plugin.stop()

    assert lifecycle is not None and lifecycle.stop_calls == 1
    assert plugin._process_lifecycle is lifecycle
    assert plugin._bot is bot
    assert plugin._closing is True
    assert plugin._quarantined is True
    assert not hasattr(bot, "speech_queue")
    assert "cap-can-0416" not in bot.runtime_capability_readiness
    assert removed == ["voice"]
    with pytest.raises(RuntimeError, match="already started"):
        await plugin.start(bot)

    await plugin.stop()

    assert lifecycle.stop_calls == 2
    assert plugin._process_lifecycle is None
    assert plugin._bot is None
    assert plugin._quarantined is False


@pytest.mark.asyncio
async def test_voice_plugin_surface_cleanup_failure_quarantines_until_retry() -> None:
    remove_calls = 0

    def remove_command(_name: str) -> None:
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls == 1:
            raise RuntimeError("surface cleanup failed")

    bot = SimpleNamespace(
        settings=SimpleNamespace(voice_enabled=False),
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=remove_command),
    )
    plugin = VoicePlugin()
    await plugin.start(bot)

    with pytest.raises(RuntimeError, match="surface cleanup failed"):
        await plugin.stop()

    assert plugin._bot is bot
    assert plugin._closing is True
    assert plugin._quarantined is True
    assert not hasattr(bot, "speech_queue")
    assert "cap-can-0416" not in bot.runtime_capability_readiness
    with pytest.raises(RuntimeError, match="already started"):
        await plugin.start(bot)

    await plugin.stop()

    assert remove_calls == 2
    assert plugin._bot is None
    assert plugin._quarantined is False


@pytest.mark.asyncio
async def test_voice_plugin_public_queue_cleanup_failure_retries_same_identity() -> None:
    class Bot(SimpleNamespace):
        fail_delete = True

        def __delattr__(self, name: str) -> None:
            if name == "speech_queue" and self.fail_delete:
                self.fail_delete = False
                raise RuntimeError("queue surface cleanup failed")
            super().__delattr__(name)

    bot = Bot(
        settings=SimpleNamespace(voice_enabled=False),
        tree=SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
    )
    plugin = VoicePlugin()
    await plugin.start(bot)
    queue = plugin.queue

    with pytest.raises(RuntimeError, match="queue surface cleanup failed"):
        await plugin.stop()

    assert queue is not None
    assert plugin.queue is queue
    assert bot.speech_queue is queue
    assert plugin._quarantined is True

    await plugin.stop()

    assert plugin.queue is None
    assert not hasattr(bot, "speech_queue")
    assert plugin._bot is None


@pytest.mark.asyncio
async def test_voice_plugin_start_cleanup_unconfirmed_never_publishes_and_retries() -> None:
    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def probe_version(self, *, timeout_seconds: float) -> bool:
            return False

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            raise AssertionError("unavailable client must not be used")

        async def close(self) -> None:
            return None

    class Lifecycle:
        def __init__(self, **_kwargs: Any) -> None:
            self.stop_calls = 0

        async def ensure_ready(self, _probe: Any) -> bool:
            raise VoicevoxProcessError("voicevox_process_start_failed")

        async def stop(self) -> None:
            self.stop_calls += 1
            if self.stop_calls == 1:
                raise VoicevoxProcessError("voicevox_process_cleanup_unconfirmed")

    lifecycle: Lifecycle | None = None

    def lifecycle_factory(**kwargs: Any) -> Lifecycle:
        nonlocal lifecycle
        lifecycle = Lifecycle(**kwargs)
        return lifecycle

    added = []
    removed = []
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            voice_enabled=True,
            voicevox_url="http://127.0.0.1:50021",
            voice_allow_remote=False,
            voice_timeout_seconds=5.0,
            voice_max_response_bytes=1_048_576,
            voicevox_managed_process_enabled=True,
            voicevox_managed_executable=None,
            voicevox_managed_startup_timeout_seconds=15.0,
            voicevox_managed_shutdown_timeout_seconds=8.0,
        ),
        tree=SimpleNamespace(add_command=added.append, remove_command=removed.append),
    )
    plugin = VoicePlugin(  # type: ignore[arg-type]
        client_factory=Client,
        process_lifecycle_factory=lifecycle_factory,
    )
    manager = PluginManager()
    manager.register("voice", lambda: plugin)

    assert await manager.enable("voice", bot) is True

    assert lifecycle is not None and lifecycle.stop_calls == 1
    assert manager.status("voice") is PluginStatus.RUNNING
    assert plugin._process_lifecycle is lifecycle
    assert plugin._quarantined is True
    assert plugin._bot is bot
    assert not hasattr(bot, "speech_queue")
    assert added == []
    assert removed == []

    assert await manager.disable("voice") is True

    assert lifecycle.stop_calls == 2
    assert manager.status("voice") is PluginStatus.DISABLED
    assert plugin._process_lifecycle is None
    assert plugin._bot is None
