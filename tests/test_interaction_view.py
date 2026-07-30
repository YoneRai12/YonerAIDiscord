from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from yonerai_discord.modules.operations import (
    InteractionFailureDelivery,
    InteractionFailureTerminal,
    SafeInteractionView,
)


class _Database:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def append_audit(self, event: str, **kwargs: Any) -> None:
        self.rows.append((event, kwargs))


class _Response:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.type: discord.InteractionResponseType | None = None
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.calls.append((content, kwargs))
        self._done = True
        self.type = discord.InteractionResponseType.channel_message


class _Followup:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.calls.append((content, kwargs))


class _Interaction:
    def __init__(
        self,
        *,
        interaction_id: int = 101,
        terminal: InteractionFailureTerminal | None = None,
    ) -> None:
        self.id = interaction_id
        self.guild_id = 202
        self.user = SimpleNamespace(id=303)
        self.response = _Response()
        self.followup = _Followup()
        self.client = SimpleNamespace(
            database=_Database(),
            interaction_failure_terminal=terminal,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        discord.ui.Button(custom_id="safe:test:button"),
        discord.ui.Select(custom_id="safe:test:select"),
    ],
)
async def test_component_callback_error_delegates_to_bot_owned_terminal(
    item: discord.ui.Item[Any],
) -> None:
    terminal = InteractionFailureTerminal()
    interaction = _Interaction(terminal=terminal)
    view = SafeInteractionView()

    await view.on_error(
        interaction,  # type: ignore[arg-type]
        RuntimeError("secret callback body"),
        item,
    )

    receipt = terminal.receipt_for(surface="component_view", interaction_id=interaction.id)
    assert receipt is not None
    assert receipt.delivery is InteractionFailureDelivery.DELIVERED
    assert receipt.error_code == "discord.internal"
    assert re.fullmatch(r"ERR-[A-F0-9]{12}", receipt.reference_id)
    assert len(interaction.response.calls) == 1
    visible = interaction.response.calls[0][0]
    assert receipt.reference_id in visible
    assert "secret callback body" not in visible
    [(event, details)] = interaction.client.database.rows
    assert event == "operations.failure"
    assert "secret callback body" not in repr(details)


@pytest.mark.asyncio
async def test_duplicate_component_error_uses_same_terminal_receipt_once() -> None:
    terminal = InteractionFailureTerminal()
    interaction = _Interaction(terminal=terminal)
    view = SafeInteractionView()
    item = discord.ui.Button(custom_id="safe:test:duplicate")

    await view.on_error(interaction, RuntimeError("first secret"), item)  # type: ignore[arg-type]
    first = terminal.receipt_for(surface="component_view", interaction_id=interaction.id)
    await view.on_error(interaction, RuntimeError("second secret"), item)  # type: ignore[arg-type]
    second = terminal.receipt_for(surface="component_view", interaction_id=interaction.id)

    assert first is not None
    assert second == first
    assert len(interaction.response.calls) == 1
    assert len(interaction.followup.calls) == 0
    assert len(interaction.client.database.rows) == 1


@pytest.mark.asyncio
async def test_missing_bot_terminal_uses_view_local_fail_closed_terminal() -> None:
    interaction = _Interaction(terminal=None)
    view = SafeInteractionView()
    item = discord.ui.Select(custom_id="safe:test:fallback")

    await view.on_error(interaction, ValueError("private input"), item)  # type: ignore[arg-type]
    await view.on_error(interaction, RuntimeError("second private body"), item)  # type: ignore[arg-type]

    assert len(interaction.response.calls) == 1
    assert len(interaction.followup.calls) == 0
    assert len(interaction.client.database.rows) == 1
    visible = interaction.response.calls[0][0]
    assert "discord.invalid_input" in visible
    assert "private input" not in visible
    assert "second private body" not in visible
