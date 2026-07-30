"""Content-free durable audit projection for verified search outcomes."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Protocol

from .orchestrator import SearchOrchestratorOutcome, SearchVerificationState


class SearchAuditPort(Protocol):
    def append_audit(
        self,
        event: str,
        *,
        actor_id: int,
        details: Mapping[str, object] | None = None,
        plugin: str | None = None,
        guild_id: int | str | None = None,
    ) -> int: ...


async def append_search_outcome_audit(
    database: SearchAuditPort,
    outcome: SearchOrchestratorOutcome,
    *,
    actor_id: int,
    guild_id: int,
    database_current: Callable[[], object | None],
) -> bool:
    """Append only receipt metadata, never the query, URL, snippet, or page text."""

    if not isinstance(outcome, SearchOrchestratorOutcome):
        raise TypeError("outcome must be SearchOrchestratorOutcome")
    if (
        isinstance(actor_id, bool)
        or not isinstance(actor_id, int)
        or actor_id <= 0
        or isinstance(guild_id, bool)
        or not isinstance(guild_id, int)
        or guild_id <= 0
        or not callable(database_current)
    ):
        return False
    append = getattr(database, "append_audit", None)
    if not callable(append) or not _database_is_current(database, database_current):
        return False

    receipt = outcome.receipt
    source_classes = Counter(item.source_class.value for item in outcome.result.evidence)
    details: Mapping[str, object] = {
        "backend_ids": receipt.backend_ids,
        "engine_errors": tuple({"backend_id": item.backend_id, "code": item.code} for item in receipt.engine_errors),
        "candidate_count": receipt.candidate_count,
        "fetched_count": receipt.fetched_count,
        "cache_hits": receipt.cache_hits,
        "latency_ms": receipt.latency_ms,
        "vendor_fee_class": receipt.vendor_fee_class.value,
        "paid_fallback_used": receipt.paid_fallback_used,
        "verification_state": SearchVerificationState(outcome.verification_state).value,
        "source_classes": dict(sorted(source_classes.items())),
    }
    try:
        await asyncio.to_thread(
            append,
            "ai.search.evidence.completed",
            actor_id=actor_id,
            guild_id=guild_id,
            plugin="ai",
            details=details,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    return _database_is_current(database, database_current)


def _database_is_current(database: object, database_current: Callable[[], object | None]) -> bool:
    try:
        return database_current() is database
    except Exception:
        return False


__all__ = ["SearchAuditPort", "append_search_outcome_audit"]
