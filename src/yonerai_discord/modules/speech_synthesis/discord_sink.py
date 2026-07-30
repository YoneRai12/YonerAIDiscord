from __future__ import annotations

import re
from io import BytesIO
from typing import Any

import discord


_SAFE_WAV_FILENAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\.wav\Z")


class DiscordSpeechSynthesisSink:
    """合成済みWAVだけをDiscord interactionのephemeral followupへ配送する。"""

    async def send_wav(
        self,
        interaction: Any,
        wav: bytes,
        *,
        filename: str,
        ephemeral: bool,
        mentions_allowed: bool,
    ) -> None:
        if not isinstance(wav, bytes) or not wav:
            raise ValueError("wav must be non-empty bytes")
        if not isinstance(filename, str) or not _SAFE_WAV_FILENAME.fullmatch(filename):
            raise ValueError("filename must be a safe WAV filename")
        if ephemeral is not True or mentions_allowed is not False:
            raise ValueError("speech delivery must be ephemeral with mentions disabled")
        followup = getattr(interaction, "followup", None)
        send = getattr(followup, "send", None)
        if not callable(send):
            raise RuntimeError("Discord interaction followup is unavailable")
        await send(
            file=discord.File(BytesIO(wav), filename=filename),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


__all__ = ["DiscordSpeechSynthesisSink"]
