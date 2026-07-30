"""Discord接続面で共用する入力gate・失敗分類・進捗抑制。"""

from __future__ import annotations

from typing import Any

from .failure import FailureKind, SafeFailure, classify_failure, format_failure_message
from .gate import GateDecision, InputEnvelope, InputGate, InputPolicy
from .interaction_terminal import (
    InteractionFailureDelivery,
    InteractionFailureReceipt,
    InteractionFailureTerminal,
)
from .interaction_view import SafeInteractionView
from .progress import ProgressThrottle, ProgressUpdate


class OperationsPlugin:
    def __init__(self) -> None:
        self.gate = InputGate(InputPolicy())
        self._bot: Any | None = None

    async def start(self, bot: Any) -> None:
        self._bot = bot
        guard = getattr(bot, "capability_guard", None)
        if guard is not None and getattr(guard, "input_gate", None) is not None:
            self.gate = guard.input_gate
        setattr(bot, "operations_gate", self.gate)

    async def stop(self) -> None:
        if self._bot is not None and getattr(self._bot, "operations_gate", None) is self.gate:
            delattr(self._bot, "operations_gate")
        self._bot = None


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("operations", OperationsPlugin)


__all__ = [
    "FailureKind",
    "GateDecision",
    "InputEnvelope",
    "InputGate",
    "InputPolicy",
    "InteractionFailureDelivery",
    "InteractionFailureReceipt",
    "InteractionFailureTerminal",
    "SafeInteractionView",
    "ProgressThrottle",
    "ProgressUpdate",
    "SafeFailure",
    "classify_failure",
    "format_failure_message",
    "setup",
]
