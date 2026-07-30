"""Ticket・Poll・Suggestion・Self-roleを提供するコミュニティプラグイン。"""

from __future__ import annotations

from typing import Any

from .domain import (
    ActorPolicy,
    Poll,
    PollResult,
    PollStatus,
    Suggestion,
    SuggestionStatus,
    Ticket,
    TicketStatus,
)
from .plugin import CommunityPlugin
from .policy import (
    can_configure_selfroles,
    can_manage_poll,
    can_manage_ticket,
    can_update_suggestion,
    selfrole_permissions_are_safe,
)
from .repository import CommunityRepository


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register("community", CommunityPlugin)


__all__ = [
    "ActorPolicy",
    "CommunityPlugin",
    "CommunityRepository",
    "Poll",
    "PollResult",
    "PollStatus",
    "Suggestion",
    "SuggestionStatus",
    "Ticket",
    "TicketStatus",
    "can_configure_selfroles",
    "can_manage_poll",
    "can_manage_ticket",
    "can_update_suggestion",
    "selfrole_permissions_are_safe",
    "setup",
]
