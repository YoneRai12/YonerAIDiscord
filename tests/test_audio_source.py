from __future__ import annotations

from pathlib import Path

import discord
import pytest

from yonerai_discord.modules.audio_core import FfmpegPCMSourceFactory, Track


def test_ffmpeg_seek_uses_code_owned_numeric_before_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"test executable")
    media_root = tmp_path / "library"
    media_root.mkdir()
    media = media_root / "song.wav"
    media.write_bytes(b"test audio")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_ffmpeg(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(discord, "FFmpegPCMAudio", fake_ffmpeg)
    factory = FfmpegPCMSourceFactory(executable, (media_root,))

    factory.create_at(Track("song", media, 10), 12.5)

    assert calls == [
        (
            (str(media.resolve()),),
            {
                "executable": str(executable.resolve()),
                "before_options": ("-ss 12.500 -nostdin -hide_banner -loglevel error"),
                "options": "-vn",
            },
        )
    ]


@pytest.mark.parametrize(
    "seconds",
    [True, -1, 86_400.1, float("nan"), float("inf")],
)
def test_ffmpeg_seek_rejects_non_numeric_or_unbounded_positions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seconds: object,
) -> None:
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"test executable")
    media = tmp_path / "song.wav"
    media.write_bytes(b"test audio")
    calls = 0

    def fake_ffmpeg(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return object()

    monkeypatch.setattr(discord, "FFmpegPCMAudio", fake_ffmpeg)
    factory = FfmpegPCMSourceFactory(executable, (tmp_path,))

    with pytest.raises(ValueError, match="seek position"):
        factory.create_at(Track("song", media, 10), seconds)  # type: ignore[arg-type]

    assert calls == 0
