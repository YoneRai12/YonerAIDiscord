from __future__ import annotations

from typing import Protocol

from .domain import DestructiveCommand, DestructiveResult


class DestructiveModerationPort(Protocol):
    """破壊的操作をDiscord adapterへ委譲するport。"""

    async def execute_destructive(self, action_key: str, command: DestructiveCommand) -> DestructiveResult: ...
