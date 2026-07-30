from __future__ import annotations

import struct
from collections import deque

import pytest

from yonerai_discord.modules.audio_core import PCM_FRAME_BYTES, DuckingMixer, mix_pcm16le


def _pcm(value: int) -> bytes:
    return struct.pack("<h", value) * (PCM_FRAME_BYTES // 2)


class FakeSource:
    def __init__(self, *frames: bytes, opus: bool = False) -> None:
        self.frames = deque(frames)
        self.opus = opus
        self.cleaned = False
        self.reads = 0

    def read(self) -> bytes:
        self.reads += 1
        return self.frames.popleft() if self.frames else b""

    def is_opus(self) -> bool:
        return self.opus

    def cleanup(self) -> None:
        self.cleaned = True


def _sample(frame: bytes) -> int:
    return struct.unpack_from("<h", frame)[0]


def test_pcm_mix_saturates_and_always_returns_discord_frame() -> None:
    result = mix_pcm16le(_pcm(30_000), _pcm(30_000), primary_gain=1.0, overlay_gain=1.0)

    assert len(result) == PCM_FRAME_BYTES
    assert _sample(result) == 32_767


def test_speech_ducks_music_then_release_restores_it_gradually() -> None:
    music = FakeSource(*[_pcm(10_000) for _ in range(8)])
    speech = FakeSource(_pcm(1_000), _pcm(1_000))
    mixer = DuckingMixer(
        music,
        music_volume=1.0,
        speech_volume=1.0,
        ducking_ratio=0.2,
        attack_frames=2,
        release_frames=2,
        hold_frames=0,
    )
    mixer.add_speech(speech)

    first = _sample(mixer.read())
    second = _sample(mixer.read())
    third = _sample(mixer.read())
    fourth = _sample(mixer.read())

    assert first == 7_000
    assert second == 3_000
    assert third == 6_000
    assert fourth == 10_000
    assert speech.cleaned


def test_pausing_music_does_not_pause_or_consume_tts() -> None:
    music = FakeSource(_pcm(9_000), _pcm(9_000))
    speech = FakeSource(_pcm(2_000))
    mixer = DuckingMixer(music, music_volume=1.0, attack_frames=1)
    mixer.pause_music()
    mixer.add_speech(speech)

    assert _sample(mixer.read()) == 2_000
    assert music.reads == 0
    assert mixer.snapshot().music_paused

    mixer.resume_music()
    assert _sample(mixer.read()) > 0
    assert music.reads == 1


def test_clear_and_cleanup_release_all_sources_once() -> None:
    music = FakeSource(_pcm(1_000))
    first = FakeSource(_pcm(1_000))
    second = FakeSource(_pcm(1_000))
    mixer = DuckingMixer(music)
    mixer.add_speech(first)
    mixer.add_speech(second)

    mixer.clear_music()
    mixer.cleanup()
    mixer.cleanup()

    assert music.cleaned and first.cleaned and second.cleaned
    assert mixer.read() == b""


def test_opus_sources_are_rejected_because_they_cannot_be_mixed() -> None:
    with pytest.raises(ValueError):
        DuckingMixer(FakeSource(opus=True))

    mixer = DuckingMixer()
    with pytest.raises(ValueError):
        mixer.add_speech(FakeSource(opus=True))


def test_speech_volume_can_be_changed_without_replacing_the_mixer() -> None:
    speech = FakeSource(_pcm(2_000))
    mixer = DuckingMixer(speech_volume=1.0)
    mixer.set_speech_volume(0.25)
    mixer.add_speech(speech)

    assert _sample(mixer.read()) == 500

    with pytest.raises(ValueError):
        mixer.set_speech_volume(2.1)


def test_back_to_back_speech_keeps_ducking_nested_until_both_sources_finish() -> None:
    music = FakeSource(*[_pcm(10_000) for _ in range(10)])
    first = FakeSource(_pcm(1_000))
    second = FakeSource(_pcm(2_000))
    mixer = DuckingMixer(
        music,
        music_volume=1.0,
        ducking_ratio=0.2,
        attack_frames=1,
        hold_frames=1,
        release_frames=2,
    )
    mixer.add_speech(first)
    mixer.add_speech(second)

    assert _sample(mixer.read()) == 3_000
    assert _sample(mixer.read()) == 4_000
    assert first.cleaned and not second.cleaned
    assert mixer.snapshot().effective_music_gain == pytest.approx(0.2)

    mixer.read()
    assert second.cleaned
    assert mixer.snapshot().effective_music_gain == pytest.approx(0.2)
    mixer.read()
    mixer.read()
    assert mixer.snapshot().effective_music_gain == pytest.approx(1.0)


def test_cancelled_mixer_cleanup_cannot_leave_a_reusable_ducking_gain() -> None:
    music = FakeSource(*[_pcm(10_000) for _ in range(4)])
    speech = FakeSource(*[_pcm(1_000) for _ in range(4)])
    mixer = DuckingMixer(music, ducking_ratio=0.1, attack_frames=1)
    mixer.add_speech(speech)

    mixer.read()
    assert mixer.snapshot().effective_music_gain < 0.75
    mixer.cleanup()

    assert mixer.read() == b""
    assert music.cleaned and speech.cleaned


@pytest.mark.parametrize("invalid_volume", [True, float("nan"), float("inf"), float("-inf")])
def test_invalid_speech_volume_is_rejected_before_mixer_mutation(invalid_volume) -> None:
    with pytest.raises(ValueError):
        DuckingMixer(speech_volume=invalid_volume)
    with pytest.raises(ValueError):
        DuckingMixer(music_volume=invalid_volume)

    music = FakeSource(_pcm(1_000))
    speech = FakeSource(_pcm(2_000))
    mixer = DuckingMixer(music, music_volume=1.0, speech_volume=1.0)
    with pytest.raises(ValueError):
        mixer.set_speech_volume(invalid_volume)
    with pytest.raises(ValueError):
        mixer.set_music_volume(invalid_volume)
    mixer.add_speech(speech)

    assert _sample(mixer.read()) > 2_000
