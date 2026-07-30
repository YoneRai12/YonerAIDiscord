from __future__ import annotations

from typing import Protocol

from .models import SpeechRequest, SynthesizedSpeech


class SpeechSynthesizer(Protocol):
    async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech: ...
