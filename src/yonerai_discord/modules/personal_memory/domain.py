from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class MemoryKind(StrEnum):
    FACT = "fact"
    CONVERSATION = "conversation"


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: int
    guild_id: int
    user_id: int
    kind: MemoryKind
    content: str = field(repr=False)
    created_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if min(self.id, self.guild_id, self.user_id, self.created_at, self.expires_at) <= 0:
            raise ValueError("memory identifiers and timestamps must be positive")
        if not isinstance(self.kind, MemoryKind):
            raise TypeError("kind must be a MemoryKind")
        if not self.content.strip() or len(self.content) > 4_000:
            raise ValueError("memory content is invalid")
        if self.expires_at <= self.created_at:
            raise ValueError("memory expiry must be after creation")
