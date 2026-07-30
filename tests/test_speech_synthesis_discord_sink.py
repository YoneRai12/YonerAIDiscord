from __future__ import annotations

from types import SimpleNamespace

import pytest

from yonerai_discord.modules.speech_synthesis import DiscordSpeechSynthesisSink, SpeechSynthesisPlugin


class _Followup:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    async def send(self, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("send failed")
        self.calls.append(kwargs)


async def test_send_wav_uses_one_ephemeral_followup_file_with_mentions_disabled() -> None:
    followup = _Followup()
    sink = DiscordSpeechSynthesisSink()

    await sink.send_wav(
        SimpleNamespace(followup=followup),
        b"RIFF-test-wav",
        filename="generated-speech.wav",
        ephemeral=True,
        mentions_allowed=False,
    )

    assert len(followup.calls) == 1
    sent = followup.calls[0]
    assert sent["ephemeral"] is True
    assert sent["allowed_mentions"].everyone is False
    assert sent["allowed_mentions"].users is False
    assert sent["allowed_mentions"].roles is False
    assert sent["file"].filename == "generated-speech.wav"
    assert sent["file"].fp.read() == b"RIFF-test-wav"


@pytest.mark.parametrize("filename", ("../speech.wav", "speech.mp3", "speech name.wav", "speech.WAV"))
async def test_send_wav_rejects_unsafe_filenames_before_followup(filename: str) -> None:
    followup = _Followup()
    with pytest.raises(ValueError, match="safe WAV"):
        await DiscordSpeechSynthesisSink().send_wav(
            SimpleNamespace(followup=followup),
            b"wav",
            filename=filename,
            ephemeral=True,
            mentions_allowed=False,
        )
    assert followup.calls == []


@pytest.mark.parametrize("wav,ephemeral,mentions", ((b"", True, False), (b"wav", False, False), (b"wav", True, True)))
async def test_send_wav_rejects_non_private_or_empty_payloads_before_followup(
    wav: bytes, ephemeral: bool, mentions: bool
) -> None:
    followup = _Followup()
    with pytest.raises(ValueError):
        await DiscordSpeechSynthesisSink().send_wav(
            SimpleNamespace(followup=followup),
            wav,
            filename="generated-speech.wav",
            ephemeral=ephemeral,
            mentions_allowed=mentions,
        )
    assert followup.calls == []


async def test_send_wav_fails_closed_without_followup_or_when_followup_raises() -> None:
    sink = DiscordSpeechSynthesisSink()
    with pytest.raises(RuntimeError, match="followup"):
        await sink.send_wav(
            SimpleNamespace(), b"wav", filename="generated-speech.wav", ephemeral=True, mentions_allowed=False
        )
    with pytest.raises(RuntimeError, match="send failed"):
        await sink.send_wav(
            SimpleNamespace(followup=_Followup(fail=True)),
            b"wav",
            filename="generated-speech.wav",
            ephemeral=True,
            mentions_allowed=False,
        )


class _ExternalSink:
    async def send_wav(self, *_args, **_kwargs) -> None:
        return None


async def test_plugin_prefers_external_sink_or_uses_standard_sink_then_releases_reference() -> None:
    external = _ExternalSink()
    external_bot = SimpleNamespace(is_closing=False, speech_synthesis_delivery_sink=external)
    external_plugin = SpeechSynthesisPlugin()
    await external_plugin.start(external_bot)
    assert external_plugin.sink is external
    assert external_plugin.adapter is not None
    await external_plugin.stop()
    assert external_plugin.sink is None

    standard_bot = SimpleNamespace(is_closing=False)
    standard_plugin = SpeechSynthesisPlugin()
    await standard_plugin.start(standard_bot)
    assert isinstance(standard_plugin.sink, DiscordSpeechSynthesisSink)
    assert standard_plugin.adapter is not None
    await standard_plugin.stop()
    assert standard_plugin.sink is None
