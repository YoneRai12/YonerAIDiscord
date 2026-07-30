"""v0 memory selection runtime helper.

This module is deliberately pure: callers supply already-read records and own
all persistence, policy-loading, and Discord I/O at the integration boundary.
"""

from __future__ import annotations

from collections import Counter
import re
import unicodedata

from yonerai_discord.v0_contracts import (
    ContractReasonCode,
    MemoryRecord,
    MemorySelectionInput,
    MemorySelectionResult,
)

_SEARCH_TOKEN = re.compile(r"[a-z0-9_]+|[\u3040-\u30ff\u3400-\u9fff]", re.IGNORECASE)


class RuntimeMemorySelector:
    """Select explicit records only, with exact scope isolation.

    Explicit IDs preserve caller order.  Remaining explicit records retain the
    source order, which lets a repository choose a stable relevance ordering
    without this pure helper inventing a second ranking policy.
    """

    def select(self, request: MemorySelectionInput) -> MemorySelectionResult:
        in_scope = tuple(record for record in request.records if record.scope == request.scope)
        reasons: list[ContractReasonCode] = []
        if len(in_scope) != len(request.records):
            reasons.append(ContractReasonCode.MEMORY_SCOPE_MISMATCH)

        by_id = {record.memory_id: record for record in in_scope}
        selected: list[MemoryRecord] = []
        for memory_id in request.explicit_memory_ids:
            record = by_id.get(memory_id)
            if record is None:
                reasons.append(ContractReasonCode.EXPLICIT_MEMORY_NOT_FOUND)
            elif not record.explicit:
                reasons.append(ContractReasonCode.MEMORY_NOT_EXPLICIT)
            elif record not in selected:
                selected.append(record)

        remaining = tuple(record for record in in_scope if record.explicit and record not in selected)
        query_tokens = Counter(_tokens(request.query))
        if query_tokens:
            ranked: list[tuple[int, int, MemoryRecord]] = []
            for position, record in enumerate(remaining):
                content_tokens = Counter(_tokens(record.content))
                overlap = sum(min(count, content_tokens[token]) for token, count in query_tokens.items())
                if overlap > 0:
                    ranked.append((overlap, position, record))
            if ranked:
                ranked.sort(key=lambda item: (-item[0], item[1]))
                remaining = tuple(item[2] for item in ranked)
        selected.extend(remaining)
        return MemorySelectionResult(tuple(selected[: request.limit]), tuple(reasons) or (ContractReasonCode.READY,))


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(_SEARCH_TOKEN.findall(normalized))
