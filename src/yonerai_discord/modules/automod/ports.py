from __future__ import annotations

from typing import Protocol

from .domain import AuditRecord


class ModerationPort(Protocol):
    """report-only auditの重複防止と記録だけを抽象化するport。"""

    async def claim_action(self, action_key: str) -> bool: ...
    async def write_audit(self, record: AuditRecord) -> None: ...
