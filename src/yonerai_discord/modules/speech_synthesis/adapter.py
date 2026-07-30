from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from .domain import (
    GENERATED_SPEECH_FILENAME,
    SPEECH_SYNTHESIS_CAPABILITY_ID,
    SpeechSynthesisError,
    SpeechSynthesisRequest,
)
from .ports import SpeechSynthesisSink
from .service import SpeechSynthesisService


class SpeechSynthesisDelivery:
    """未登録の将来Discord入口向けdelivery。Stage 1ではfake sinkだけを使う。"""

    def __init__(
        self,
        service: SpeechSynthesisService,
        sink: SpeechSynthesisSink,
        *,
        capability_check: Callable[[str, Any], bool | Awaitable[bool]],
    ) -> None:
        if not isinstance(service, SpeechSynthesisService):
            raise TypeError("service must be a SpeechSynthesisService")
        if not callable(getattr(sink, "send_wav", None)):
            raise TypeError("sink must provide send_wav")
        if not callable(capability_check):
            raise TypeError("capability_check is required")
        self.service = service
        self.sink = sink
        self.capability_check = capability_check
        self._closing = False

    def begin_close(self) -> None:
        self._closing = True
        self.service.begin_close()

    async def deliver(
        self,
        interaction: Any,
        request: SpeechSynthesisRequest,
    ) -> bool:
        if not self._scope_matches(interaction, request):
            return False
        authorization = self._authorization(interaction, request)
        if not await authorization():
            return False
        try:
            synthesized = await self.service.synthesize(
                request,
                authorization_current=authorization,
            )
            if not await self.service.claim_delivery(
                request,
                synthesized,
                authorization_current=authorization,
            ):
                return False
            if not await authorization():
                return False
            if not await self.service.delivery_current(
                request,
                synthesized,
                authorization_current=authorization,
            ):
                return False
            if not await authorization():
                return False
            await self.sink.send_wav(
                interaction,
                synthesized.wav,
                filename=GENERATED_SPEECH_FILENAME,
                ephemeral=True,
                mentions_allowed=False,
            )
            return True
        except asyncio.CancelledError:
            raise
        except (SpeechSynthesisError, TypeError, ValueError):
            return False
        except Exception:
            return False

    def _authorization(
        self,
        interaction: Any,
        request: SpeechSynthesisRequest,
    ):
        async def current() -> bool:
            return self._scope_matches(interaction, request) and await self._allowed(interaction)

        return current

    async def _allowed(self, interaction: Any) -> bool:
        if self._closing or getattr(interaction, "guild_id", None) is None:
            return False
        try:
            value = self.capability_check(
                SPEECH_SYNTHESIS_CAPABILITY_ID,
                interaction,
            )
            value = await value if inspect.isawaitable(value) else value
            return not self._closing and value is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    @staticmethod
    def _scope_matches(
        interaction: Any,
        request: SpeechSynthesisRequest,
    ) -> bool:
        if not isinstance(request, SpeechSynthesisRequest):
            return False
        values = (
            getattr(interaction, "guild_id", None),
            getattr(interaction, "channel_id", None),
            getattr(getattr(interaction, "user", None), "id", None),
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            return False
        guild_id, channel_id, actor_id = values
        return guild_id == request.guild_id and channel_id == request.channel_id and actor_id == request.actor_id


__all__ = ["SpeechSynthesisDelivery"]
