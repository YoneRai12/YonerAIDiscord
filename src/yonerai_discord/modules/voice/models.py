from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field

from yonerai_discord.voice_contract import VOICEVOX_ALLOWED_SPEAKER_IDS, VOICEVOX_SPEAKER_ID


_URL = re.compile(r"https?://[^\s<>\u3000]+", re.IGNORECASE)
_MENTION = re.compile(r"<(?:@!?|@&|#)[1-9][0-9]*>")
_BROADCAST_MENTION = re.compile(r"@(?:everyone|here)", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


def normalize_speech_text(text: str) -> str:
    """Return bounded text with transport identifiers removed before synthesis."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized:
        raise ValueError("text_empty")
    if any(
        unicodedata.category(character) in {"Cc", "Cf"} and character not in {"\t", "\n", "\r"}
        for character in normalized
    ):
        raise ValueError("text_control_character")
    without_mentions = _SPACE.sub("", _MENTION.sub("", normalized))
    if not without_mentions:
        raise ValueError("mention_only")
    normalized = _MENTION.sub("メンション", normalized)
    normalized = _URL.sub("URL", normalized)
    normalized = _BROADCAST_MENTION.sub("全体通知", normalized)
    normalized = _SPACE.sub(" ", normalized).strip()
    if not normalized:
        raise ValueError("text_empty")
    if len(normalized) > 500:
        raise ValueError("text_too_long")
    return normalized


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    text: str = field(repr=False)
    guild_id: int
    channel_id: int
    speaker_id: int = VOICEVOX_SPEAKER_ID
    speed_scale: float = 1.0
    volume_scale: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", normalize_speech_text(self.text))
        if (
            type(self.guild_id) is not int
            or type(self.channel_id) is not int
            or self.guild_id <= 0
            or self.channel_id <= 0
        ):
            raise ValueError("IDs must be valid")
        if type(self.speaker_id) is not int or self.speaker_id not in VOICEVOX_ALLOWED_SPEAKER_IDS:
            raise ValueError("speaker_not_allowed")
        _scale(self.speed_scale, "speed_scale", minimum=0.5, maximum=2.0)
        _scale(self.volume_scale, "volume_scale", minimum=0.0, maximum=2.0)

    @property
    def key(self) -> str:
        material = (
            f"{self.guild_id}:{self.channel_id}:{self.speaker_id}:{self.speed_scale}:{self.volume_scale}:{self.text}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    wav: bytes = field(repr=False)
    sample_rate: int = 24_000

    def __post_init__(self) -> None:
        if not self.wav.startswith(b"RIFF"):
            raise ValueError("invalid WAV payload")


def _scale(value: float, name: str, *, minimum: float, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
