from __future__ import annotations

from typing import Any

import discord

from .interaction_terminal import InteractionFailureTerminal


_COMPONENT_FAILURE_SURFACE = "component_view"


class SafeInteractionView(discord.ui.View):
    """Component callback failuresを共通のonce-only終端へ委譲する薄い基底。"""

    def __init__(self, *, timeout: float | None = 180.0) -> None:
        super().__init__(timeout=timeout)
        self._local_failure_terminal = InteractionFailureTerminal()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        del item
        client = getattr(interaction, "client", None)
        terminal = getattr(client, "interaction_failure_terminal", None)
        if not isinstance(terminal, InteractionFailureTerminal):
            terminal = self._local_failure_terminal
        await terminal.fail_once(
            interaction,
            error,
            surface=_COMPONENT_FAILURE_SURFACE,
        )


__all__ = ["SafeInteractionView"]
