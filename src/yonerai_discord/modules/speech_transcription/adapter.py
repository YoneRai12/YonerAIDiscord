from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import app_commands

from yonerai_discord.provider_registry import QualityTier

from .artifacts import BoundedSpeechAudioStore, SpeechAudioArtifactError
from .domain import (
    SPEECH_TRANSCRIPTION_CAPABILITY_ID,
    SpeechTranscriptionError,
    SpeechTranscriptionRequest,
)
from .service import SpeechTranscriptionService


_UNAVAILABLE = "音声の文字起こしを利用できません。"


class TranscribeGroup(app_commands.Group):
    def __init__(self, adapter: DiscordSpeechTranscriptionDelivery) -> None:
        self.adapter = adapter
        super().__init__(name="transcribe", description="音声ファイルを文字起こしします")

    @app_commands.command(name="file", description="WAV音声ファイルを文字起こしします")
    @app_commands.describe(audio="30秒以内のPCM16 WAV", language_code="言語コード（例: ja-JP）")
    async def file(
        self,
        interaction: discord.Interaction,
        audio: discord.Attachment,
        language_code: str | None = None,
    ) -> None:
        await self.adapter.transcribe_attachment(interaction, audio, language_code=language_code)


class DiscordSpeechTranscriptionDelivery:
    """request-bound audioの取得と文字起こし配送をDiscord scopeへ閉じる。"""

    def __init__(
        self,
        service: SpeechTranscriptionService,
        *,
        capability_check: Callable[[str, Any], bool | Awaitable[bool]],
        artifact_store: BoundedSpeechAudioStore | None = None,
    ) -> None:
        if not isinstance(service, SpeechTranscriptionService):
            raise TypeError("service must be a SpeechTranscriptionService")
        if not callable(capability_check):
            raise TypeError("capability_check is required")
        self.service = service
        self.capability_check = capability_check
        self.artifact_store = artifact_store
        self.group = TranscribeGroup(self)
        self._closing = False

    def install(self, tree: Any) -> None:
        if self.artifact_store is not None:
            tree.add_command(self.group)

    def uninstall(self, tree: Any) -> None:
        if self.artifact_store is not None:
            tree.remove_command(self.group.name)

    def begin_close(self) -> None:
        self._closing = True
        self.service.begin_close()

    async def transcribe_attachment(
        self,
        interaction: Any,
        attachment: Any,
        *,
        language_code: str | None = None,
    ) -> bool:
        store = self.artifact_store
        if store is None or not self._attachment_metadata_allowed(attachment) or not await self._allowed(interaction):
            return False
        defer = getattr(getattr(interaction, "response", None), "defer", None)
        read = getattr(attachment, "read", None)
        if not callable(defer) or not callable(read):
            return False
        try:
            await defer(ephemeral=True, thinking=True)
            if not await self._allowed(interaction):
                return False
            data = await read()
            if not await self._allowed(interaction):
                return False
            if not isinstance(data, bytes) or len(data) != attachment.size:
                raise SpeechAudioArtifactError("downloaded WAV size does not match metadata")
            interaction_id = getattr(interaction, "id", None)
            attachment_id = getattr(attachment, "id", None)
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in (interaction_id, attachment_id)
            ):
                raise SpeechAudioArtifactError("Discord identifiers are invalid")
            ref = store.describe_wav(data, artifact_id=f"stt-{interaction_id}-{attachment_id}")
            request = SpeechTranscriptionRequest(
                request_id=f"stt-file-{interaction_id}",
                guild_id=int(interaction.guild_id),
                channel_id=int(interaction.channel_id),
                actor_id=int(interaction.user.id),
                audio=ref,
                language_code=language_code,
                tier=QualityTier.BALANCED,
            )
            store.put_wav(ref, data, request_binding=request.audio_binding)
            try:
                if not await self._allowed(interaction):
                    return False
                return await self.deliver(interaction, request)
            finally:
                store.discard(ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._send_ingress_failure(interaction)
            return False

    async def deliver(self, interaction: Any, request: SpeechTranscriptionRequest) -> bool:
        if not self._scope_matches(interaction, request):
            return False
        authorization = self._authorization(interaction, request)
        if not await authorization():
            return False
        try:
            transcript = await self.service.transcribe(request, authorization_current=authorization)
        except (SpeechTranscriptionError, TypeError, ValueError):
            await self._send_if_allowed(interaction, request, _UNAVAILABLE)
            return False
        except Exception:
            await self._send_if_allowed(interaction, request, _UNAVAILABLE)
            return False
        if not await self.service.claim_delivery(request, transcript, authorization_current=authorization):
            return False
        if not await self.service.delivery_current(request, transcript, authorization_current=authorization):
            return False
        if not await authorization():
            return False
        try:
            await interaction.followup.send(
                transcript.text,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    def _authorization(self, interaction: Any, request: SpeechTranscriptionRequest):
        async def current() -> bool:
            return self._scope_matches(interaction, request) and await self._allowed(interaction)

        return current

    async def _allowed(self, interaction: Any) -> bool:
        if self._closing or getattr(interaction, "guild_id", None) is None:
            return False
        try:
            value = self.capability_check(SPEECH_TRANSCRIPTION_CAPABILITY_ID, interaction)
            value = await value if inspect.isawaitable(value) else value
            return not self._closing and value is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _send_if_allowed(self, interaction: Any, request: SpeechTranscriptionRequest, content: str) -> None:
        if await self._authorization(interaction, request)():
            try:
                await interaction.followup.send(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                return

    async def _send_ingress_failure(self, interaction: Any) -> None:
        if not await self._allowed(interaction):
            return
        send = getattr(getattr(interaction, "followup", None), "send", None)
        if not callable(send):
            return
        try:
            await send(
                _UNAVAILABLE,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            return

    @staticmethod
    def _attachment_metadata_allowed(attachment: Any) -> bool:
        size = getattr(attachment, "size", None)
        filename = getattr(attachment, "filename", None)
        return (
            getattr(attachment, "content_type", None) == "audio/wav"
            and isinstance(size, int)
            and not isinstance(size, bool)
            and 44 <= size <= 8 * 1024 * 1024
            and isinstance(filename, str)
            and filename.lower().endswith(".wav")
        )

    @staticmethod
    def _scope_matches(interaction: Any, request: SpeechTranscriptionRequest) -> bool:
        if not isinstance(request, SpeechTranscriptionRequest):
            return False
        values = (
            getattr(interaction, "guild_id", None),
            getattr(interaction, "channel_id", None),
            getattr(getattr(interaction, "user", None), "id", None),
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            return False
        guild_id, channel_id, user_id = values
        return guild_id == request.guild_id and channel_id == request.channel_id and user_id == request.actor_id


__all__ = ["DiscordSpeechTranscriptionDelivery", "TranscribeGroup"]
