from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from .evidence_fetcher import FetchedEvidence


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class EvidenceCacheKey:
    digest: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.digest, str) or not _DIGEST.fullmatch(self.digest):
            raise ValueError("cache key must be a prefixed lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class EvidenceCacheStats:
    entries: int
    consumed_bytes: int
    max_entries: int
    max_bytes: int


@dataclass(frozen=True, slots=True, repr=False)
class _CacheEntry:
    evidence: FetchedEvidence
    expires_at: float
    weight: int


class BoundedEvidenceCache:
    """Process-local TTL/LRU cache keyed only by an opaque digest."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        max_entries: int = 128,
        max_bytes: int = 8 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(float(ttl_seconds))
            or not 0.1 <= float(ttl_seconds) <= 24 * 60 * 60
        ):
            raise ValueError("ttl_seconds is outside the allowed range")
        _bounded_int("max_entries", max_entries, 1, 4_096)
        _bounded_int("max_bytes", max_bytes, 1_024, 128 * 1024 * 1024)
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._clock = clock
        self._entries: OrderedDict[EvidenceCacheKey, _CacheEntry] = OrderedDict()
        self._consumed_bytes = 0
        self._lock = threading.RLock()

    def get(self, key: EvidenceCacheKey) -> FetchedEvidence | None:
        if not isinstance(key, EvidenceCacheKey):
            raise TypeError("key must be EvidenceCacheKey")
        with self._lock:
            now = self._now()
            self._purge_expired(now)
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry.evidence

    def put(self, key: EvidenceCacheKey, evidence: FetchedEvidence) -> None:
        if not isinstance(key, EvidenceCacheKey):
            raise TypeError("key must be EvidenceCacheKey")
        if not isinstance(evidence, FetchedEvidence):
            raise TypeError("evidence must be FetchedEvidence")
        weight = _evidence_weight(evidence)
        if weight > self._max_bytes:
            raise ValueError("evidence exceeds the cache byte budget")
        with self._lock:
            now = self._now()
            self._purge_expired(now)
            replaced = self._entries.pop(key, None)
            if replaced is not None:
                self._consumed_bytes -= replaced.weight
            while self._entries and (
                len(self._entries) >= self._max_entries or self._consumed_bytes + weight > self._max_bytes
            ):
                _, evicted = self._entries.popitem(last=False)
                self._consumed_bytes -= evicted.weight
            self._entries[key] = _CacheEntry(
                evidence=evidence,
                expires_at=now + self._ttl_seconds,
                weight=weight,
            )
            self._consumed_bytes += weight

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._consumed_bytes = 0

    def stats(self) -> EvidenceCacheStats:
        with self._lock:
            self._purge_expired(self._now())
            return EvidenceCacheStats(
                entries=len(self._entries),
                consumed_bytes=self._consumed_bytes,
                max_entries=self._max_entries,
                max_bytes=self._max_bytes,
            )

    def _purge_expired(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            entry = self._entries.pop(key)
            self._consumed_bytes -= entry.weight

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise RuntimeError("cache clock returned an invalid value")
        return float(value)


def _evidence_weight(evidence: FetchedEvidence) -> int:
    return (
        len(evidence.canonical_url.encode("utf-8"))
        + len(evidence.hostname.encode("ascii"))
        + len(evidence.title.encode("utf-8"))
        + len(evidence.text.encode("utf-8"))
        + len(evidence.media_type)
        + len(evidence.content_hash)
        + 128
    )


def _bounded_int(name: str, value: object, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the allowed range")
