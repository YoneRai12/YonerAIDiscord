from __future__ import annotations

import asyncio
import hashlib
import wave
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import discord
import pytest

from yonerai_discord.modules.speech_transcription import (
    BoundedSpeechAudioStore,
    DiscordSpeechTranscriptionDelivery,
    SpeechAudioArtifactError,
    SpeechTranscriptionPlugin,
    SpeechTranscriptionRequest,
    SpeechTranscriptionService,
    validate_pcm_wav,
)


def _wav(*, seconds: float = 1.0, rate: int = 44_100, channels: int = 1, width: int = 2) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(b"\0" * (int(seconds * rate) * channels * width))
    return output.getvalue()


class _Response:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def defer(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _Followup:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, *args, **kwargs) -> None:
        self.calls.append({"args": args, **kwargs})


class _Interaction:
    def __init__(self) -> None:
        self.id = 400
        self.guild_id = 10
        self.channel_id = 20
        self.user = SimpleNamespace(id=30)
        self.response = _Response()
        self.followup = _Followup()


class _Attachment:
    def __init__(self, data: bytes, *, content_type: str = "audio/wav", filename: str = "voice.wav") -> None:
        self.id = 500
        self.data = data
        self.size = len(data)
        self.content_type = content_type
        self.filename = filename
        self.read_calls = 0
        self.after_read = None

    @property
    def url(self):
        raise AssertionError("arbitrary attachment URL must not be accessed")

    async def read(self) -> bytes:
        self.read_calls += 1
        if self.after_read is not None:
            self.after_read()
        return self.data


async def test_valid_attachment_becomes_exact_request_bound_ref_without_url_fetch() -> None:
    store = BoundedSpeechAudioStore()
    adapter = DiscordSpeechTranscriptionDelivery(
        SpeechTranscriptionService(None, audio_artifact_current=store.current),
        capability_check=lambda *_: True,
        artifact_store=store,
    )
    captured: list[SpeechTranscriptionRequest] = []

    async def deliver(_interaction, request):
        captured.append(request)
        assert store.current(request.audio, request.audio_binding) is True
        assert store.read_wav(request.audio, request_binding=request.audio_binding) == attachment.data
        return True

    adapter.deliver = deliver
    interaction = _Interaction()
    attachment = _Attachment(_wav())

    assert await adapter.transcribe_attachment(interaction, attachment, language_code="ja-JP") is True
    assert attachment.read_calls == 1
    assert interaction.response.calls == [{"ephemeral": True, "thinking": True}]
    request = captured[0]
    assert request.audio.media_type == "audio/wav"
    assert request.audio.size_bytes == len(attachment.data)
    assert request.audio.sha256 == hashlib.sha256(attachment.data).hexdigest()
    assert request.guild_id == 10 and request.channel_id == 20 and request.actor_id == 30
    assert store.current(request.audio, request.audio_binding) is False


@pytest.mark.parametrize(
    ("content_type", "filename", "size"),
    (
        ("audio/mpeg", "voice.wav", None),
        ("audio/wav", "voice.mp3", None),
        ("audio/wav", "voice.wav", 8 * 1024 * 1024 + 1),
    ),
)
async def test_metadata_is_rejected_before_defer_and_download(
    content_type: str, filename: str, size: int | None
) -> None:
    attachment = _Attachment(_wav(), content_type=content_type, filename=filename)
    if size is not None:
        attachment.size = size
    interaction = _Interaction()
    adapter = DiscordSpeechTranscriptionDelivery(
        SpeechTranscriptionService(None),
        capability_check=lambda *_: True,
        artifact_store=BoundedSpeechAudioStore(),
    )
    assert await adapter.transcribe_attachment(interaction, attachment) is False
    assert attachment.read_calls == 0
    assert interaction.response.calls == []


@pytest.mark.parametrize(
    "data",
    (b"not-wave", _wav(width=1), _wav(rate=16_000), _wav(seconds=0.5), _wav(seconds=30.1)),
    ids=("magic", "pcm8", "rate", "short", "long"),
)
def test_downloaded_wav_contract_rejects_magic_pcm_rate_and_duration(data: bytes) -> None:
    with pytest.raises(SpeechAudioArtifactError):
        validate_pcm_wav(data)


async def test_actual_download_size_and_magic_fail_closed_with_private_message() -> None:
    interaction = _Interaction()
    attachment = _Attachment(b"x" * 44)
    attachment.size += 1
    adapter = DiscordSpeechTranscriptionDelivery(
        SpeechTranscriptionService(None),
        capability_check=lambda *_: True,
        artifact_store=BoundedSpeechAudioStore(),
    )
    assert await adapter.transcribe_attachment(interaction, attachment) is False
    assert len(interaction.followup.calls) == 1
    sent = interaction.followup.calls[0]
    assert sent["ephemeral"] is True
    assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


async def test_authorization_revoked_during_attachment_read_stops_before_store_and_provider() -> None:
    allowed = True
    store = BoundedSpeechAudioStore()
    interaction = _Interaction()
    attachment = _Attachment(_wav())

    def revoke() -> None:
        nonlocal allowed
        allowed = False

    attachment.after_read = revoke
    adapter = DiscordSpeechTranscriptionDelivery(
        SpeechTranscriptionService(None, audio_artifact_current=store.current),
        capability_check=lambda *_: allowed,
        artifact_store=store,
    )
    called = False

    async def deliver(*_args):
        nonlocal called
        called = True
        return True

    adapter.deliver = deliver
    assert await adapter.transcribe_attachment(interaction, attachment) is False
    assert called is False
    assert store._total_bytes == 0


async def test_cancellation_after_store_commit_always_discards_private_bytes() -> None:
    checks = 0
    store = BoundedSpeechAudioStore()

    def allowed(*_args) -> bool:
        nonlocal checks
        checks += 1
        if checks == 4:
            raise asyncio.CancelledError
        return True

    adapter = DiscordSpeechTranscriptionDelivery(
        SpeechTranscriptionService(None, audio_artifact_current=store.current),
        capability_check=allowed,
        artifact_store=store,
    )
    with pytest.raises(asyncio.CancelledError):
        await adapter.transcribe_attachment(_Interaction(), _Attachment(_wav()))
    assert store._total_bytes == 0
    assert store._items == {}


def test_store_rejects_clones_cross_scope_binding_and_request_replay() -> None:
    store = BoundedSpeechAudioStore(max_artifacts=1)
    data = _wav()
    ref = store.describe_wav(data, artifact_id="stt-1-1")
    request = SpeechTranscriptionRequest("stt-file-1", 10, 20, 30, ref)
    store.put_wav(ref, data, request_binding=request.audio_binding)
    assert store.current(ref, request.audio_binding) is True
    assert store.current(replace(ref), request.audio_binding) is False
    other_scope = SpeechTranscriptionRequest("stt-file-1", 10, 21, 30, ref)
    assert store.current(ref, other_scope.audio_binding) is False
    with pytest.raises(SpeechAudioArtifactError):
        store.put_wav(ref, data, request_binding=request.audio_binding)
    store.begin_close()
    assert store._total_bytes == 0


async def test_provider_unconfigured_is_explicit_fail_closed_and_one_shot_delivery_is_preserved() -> None:
    store = BoundedSpeechAudioStore()
    interaction = _Interaction()
    attachment = _Attachment(_wav())
    adapter = DiscordSpeechTranscriptionDelivery(
        SpeechTranscriptionService(None, audio_artifact_current=store.current),
        capability_check=lambda *_: True,
        artifact_store=store,
    )
    assert await adapter.transcribe_attachment(interaction, attachment) is False
    assert len(interaction.followup.calls) == 1
    assert interaction.followup.calls[0]["ephemeral"] is True
    assert store._total_bytes == 0


class _Tree:
    def __init__(self) -> None:
        self.added = []
        self.removed = []

    def add_command(self, command) -> None:
        self.added.append(command)

    def remove_command(self, name) -> None:
        self.removed.append(name)


async def test_plugin_registers_backup_command_and_stop_clears_private_bytes() -> None:
    tree = _Tree()
    bot = SimpleNamespace(is_closing=False, tree=tree)
    plugin = SpeechTranscriptionPlugin()
    await plugin.start(bot)
    assert tree.added[0].name == "transcribe"
    assert plugin.audio_store is bot.speech_transcription_audio_store
    store = plugin.audio_store
    assert store is not None
    data = _wav()
    ref = store.describe_wav(data, artifact_id="stt-stop-1")
    request = SpeechTranscriptionRequest("stt-stop", 10, 20, 30, ref)
    store.put_wav(ref, data, request_binding=request.audio_binding)

    await plugin.stop()

    assert tree.removed == ["transcribe"]
    assert store._total_bytes == 0
    assert not hasattr(bot, "speech_transcription_audio_store")

    class _BrokenTree(_Tree):
        def remove_command(self, name) -> None:
            super().remove_command(name)
            raise RuntimeError("remove failed")

    broken_bot = SimpleNamespace(is_closing=False, tree=_BrokenTree())
    broken = SpeechTranscriptionPlugin()
    await broken.start(broken_bot)
    broken_store = broken.audio_store
    assert broken_store is not None
    broken_ref = broken_store.describe_wav(data, artifact_id="stt-stop-2")
    broken_request = SpeechTranscriptionRequest("stt-stop-2", 10, 20, 30, broken_ref)
    broken_store.put_wav(broken_ref, data, request_binding=broken_request.audio_binding)
    with pytest.raises(RuntimeError, match="remove failed"):
        await broken.stop()
    assert broken_store._total_bytes == 0
    assert not hasattr(broken_bot, "speech_transcription_audio_store")
