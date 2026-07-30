"""Bounded, request-scoped readable documents for Search Fabric.

The store is deliberately process-local.  It accepts only the visible-text
result of :class:`EvidenceFetcher`; URLs, content hashes, and the original
text stay inside this boundary and never appear in exception messages or
object representations.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import secrets
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from .evidence_fetcher import (
    EvidenceFetchError,
    EvidenceFetcher,
    EvidenceLimitError,
    EvidenceResponseError,
    FetchedEvidence,
)


_SUPPORTED_MEDIA_TYPES: Final = frozenset({"text/html", "text/plain"})
_SPACE: Final = re.compile(r"\s+")

AuthorizationCurrent = Callable[[], bool | Awaitable[bool]]


class SearchDocumentError(RuntimeError):
    """Content-free document boundary failure."""


class SearchDocumentAuthorizationError(SearchDocumentError):
    """The current caller is no longer allowed to read a document."""


class SearchDocumentNotFoundError(SearchDocumentError):
    """The opaque document reference is missing, expired, or out of scope."""


class SearchDocumentUnsupportedError(SearchDocumentError):
    """The fetched representation is not readable in this stage."""


class SearchDocumentLimitError(SearchDocumentError):
    """A bounded document, query, or continuation value was rejected."""


@dataclass(frozen=True, slots=True)
class SearchDocumentScope:
    """Origin binding; continuation access remains exact to guild/channel/user."""

    request_id: str = field(repr=False)
    guild_id: str = field(repr=False)
    channel_id: str = field(repr=False)
    user_id: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.request_id, self.guild_id, self.channel_id, self.user_id):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError("document scope is invalid")


@dataclass(frozen=True, slots=True)
class SearchDocumentReference:
    """Opaque immutable handle.  Its sensitive bindings are intentionally hidden."""

    value: str = field(repr=False)
    _scope: SearchDocumentScope = field(repr=False, compare=False)
    _url_hash: str = field(repr=False, compare=False)
    _content_hash: str = field(repr=False, compare=False)
    _expires_at: float = field(repr=False, compare=False)
    _store_identity: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not re.fullmatch(r"[0-9a-f]{32}", self.value):
            raise ValueError("document reference is invalid")
        if not isinstance(self._scope, SearchDocumentScope):
            raise TypeError("document reference scope is invalid")
        for digest in (self._url_hash, self._content_hash):
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("document reference digest is invalid")
        if not isinstance(self._expires_at, float) or self._expires_at <= 0:
            raise ValueError("document reference expiry is invalid")


@dataclass(frozen=True, slots=True)
class SearchDocumentExcerpt:
    """A bounded visible-text page; callers cannot request a full dump."""

    title: str = field(repr=False)
    text: str = field(repr=False)
    offset: int
    next_offset: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or len(self.title) > 4_000:
            raise ValueError("document title is invalid")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("document excerpt is invalid")
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("document offset is invalid")
        if self.next_offset is not None and (
            isinstance(self.next_offset, bool)
            or not isinstance(self.next_offset, int)
            or self.next_offset <= self.offset
        ):
            raise ValueError("document continuation is invalid")


@dataclass(frozen=True, slots=True)
class SearchDocumentFetchResult:
    """A successful fetch provides an opaque handle plus only the first bounded page."""

    reference: str = field(repr=False)
    title: str = field(repr=False)
    preview: str = field(repr=False)
    media_type: str
    content_hash: str = field(repr=False)
    next_offset: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or not re.fullmatch(r"[0-9a-f]{32}", self.reference):
            raise TypeError("document reference is invalid")
        if not isinstance(self.title, str) or len(self.title) > 4_000:
            raise ValueError("document title is invalid")
        if not isinstance(self.preview, str) or not self.preview:
            raise ValueError("document preview is invalid")
        if self.media_type not in _SUPPORTED_MEDIA_TYPES:
            raise ValueError("document media type is invalid")
        if not isinstance(self.content_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.content_hash):
            raise ValueError("document content hash is invalid")
        if self.next_offset is not None and (
            isinstance(self.next_offset, bool) or not isinstance(self.next_offset, int) or self.next_offset <= 0
        ):
            raise ValueError("document continuation is invalid")


@dataclass(frozen=True, slots=True)
class SearchDocumentFindHit:
    """A bounded context window around one literal text match."""

    text: str = field(repr=False)
    offset: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("document find hit is invalid")
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("document find offset is invalid")


@dataclass(frozen=True, slots=True)
class SearchDocumentFindResult:
    hits: tuple[SearchDocumentFindHit, ...]
    next_offset: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.hits, tuple):
            raise ValueError("document find result is invalid")
        if not all(isinstance(hit, SearchDocumentFindHit) for hit in self.hits):
            raise TypeError("document find hits are invalid")
        if self.next_offset is not None and (
            isinstance(self.next_offset, bool) or not isinstance(self.next_offset, int) or self.next_offset < 0
        ):
            raise ValueError("document find continuation is invalid")


@dataclass(frozen=True, slots=True)
class SearchDocumentStoreLimits:
    ttl_seconds: float = 900.0
    max_documents: int = 64
    max_total_chars: int = 500_000
    max_excerpt_chars: int = 1_200
    max_find_hits: int = 5
    max_find_context_chars: int = 320

    def __post_init__(self) -> None:
        if (
            isinstance(self.ttl_seconds, bool)
            or not isinstance(self.ttl_seconds, (int, float))
            or not 1.0 <= float(self.ttl_seconds) <= 3_600.0
        ):
            raise ValueError("document TTL is invalid")
        for value, minimum, maximum in (
            (self.max_documents, 1, 256),
            (self.max_total_chars, 1_024, 2_000_000),
            (self.max_excerpt_chars, 80, 4_000),
            (self.max_find_hits, 1, 10),
            (self.max_find_context_chars, 80, 1_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError("document limits are invalid")


@dataclass(frozen=True, slots=True)
class _StoredDocument:
    scope: SearchDocumentScope = field(repr=False)
    url_hash: str = field(repr=False)
    content_hash: str = field(repr=False)
    expires_at: float = field(repr=False)
    title: str = field(repr=False)
    text: str = field(repr=False)


class BoundedSearchDocumentStore:
    """Small LRU-like in-memory store; it deliberately has no persistence seam."""

    def __init__(self, limits: SearchDocumentStoreLimits, *, clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(limits, SearchDocumentStoreLimits):
            raise TypeError("document limits are required")
        if not callable(clock):
            raise TypeError("document clock must be callable")
        self._limits = limits
        self._clock = clock
        self._entries: OrderedDict[str, _StoredDocument] = OrderedDict()
        self._total_chars = 0

    def put(self, *, scope: SearchDocumentScope, evidence: FetchedEvidence) -> SearchDocumentReference:
        if not isinstance(scope, SearchDocumentScope) or not isinstance(evidence, FetchedEvidence):
            raise TypeError("document input is invalid")
        if evidence.media_type not in _SUPPORTED_MEDIA_TYPES:
            raise SearchDocumentUnsupportedError("document representation is unsupported")
        if not evidence.text:
            raise SearchDocumentUnsupportedError("document representation is unsupported")
        now = self._clock()
        self._expire(now)
        if len(evidence.text) > self._limits.max_total_chars:
            raise SearchDocumentLimitError("document is too large")
        while self._entries and (
            len(self._entries) >= self._limits.max_documents
            or self._total_chars + len(evidence.text) > self._limits.max_total_chars
        ):
            _, removed = self._entries.popitem(last=False)
            self._total_chars -= len(removed.text)
        if self._total_chars + len(evidence.text) > self._limits.max_total_chars:
            raise SearchDocumentLimitError("document store is full")
        value = secrets.token_hex(16)
        expires_at = now + float(self._limits.ttl_seconds)
        url_hash = _sha256(evidence.canonical_url)
        entry = _StoredDocument(
            scope=scope,
            url_hash=url_hash,
            content_hash=evidence.content_hash,
            expires_at=expires_at,
            title=evidence.title,
            text=evidence.text,
        )
        self._entries[value] = entry
        self._total_chars += len(entry.text)
        return SearchDocumentReference(
            value=value,
            _scope=scope,
            _url_hash=url_hash,
            _content_hash=evidence.content_hash,
            _expires_at=expires_at,
            _store_identity=self,
        )

    def get(self, reference: SearchDocumentReference, scope: SearchDocumentScope) -> _StoredDocument:
        if not isinstance(reference, SearchDocumentReference) or not isinstance(scope, SearchDocumentScope):
            raise SearchDocumentNotFoundError("document is unavailable")
        now = self._clock()
        self._expire(now)
        if reference._store_identity is not self or reference._scope != scope or reference._expires_at <= now:
            raise SearchDocumentNotFoundError("document is unavailable")
        entry = self._entries.get(reference.value)
        if (
            entry is None
            or entry.scope != scope
            or entry.url_hash != reference._url_hash
            or entry.content_hash != reference._content_hash
            or entry.expires_at != reference._expires_at
        ):
            raise SearchDocumentNotFoundError("document is unavailable")
        self._entries.move_to_end(reference.value)
        return entry

    def get_value(self, value: str, scope: SearchDocumentScope) -> _StoredDocument:
        """Resolve a displayed opaque token for the same guild/channel/user only.

        The original request ID remains immutable inside the stored record.  A
        continuation arrives in a new interaction, so it intentionally cannot
        supply or replace that origin request binding.
        """
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            raise SearchDocumentNotFoundError("document is unavailable")
        if not isinstance(scope, SearchDocumentScope):
            raise SearchDocumentNotFoundError("document is unavailable")
        now = self._clock()
        self._expire(now)
        entry = self._entries.get(value)
        if entry is None or not _same_access_scope(entry.scope, scope):
            raise SearchDocumentNotFoundError("document is unavailable")
        self._entries.move_to_end(value)
        return entry

    def discard(self, reference: SearchDocumentReference) -> bool:
        """Drop exactly one just-created record without accepting a raw token."""
        if not isinstance(reference, SearchDocumentReference) or reference._store_identity is not self:
            return False
        entry = self._entries.get(reference.value)
        if (
            entry is None
            or entry.scope != reference._scope
            or entry.url_hash != reference._url_hash
            or entry.content_hash != reference._content_hash
            or entry.expires_at != reference._expires_at
        ):
            return False
        del self._entries[reference.value]
        self._total_chars -= len(entry.text)
        return True

    def clear(self) -> None:
        self._entries.clear()
        self._total_chars = 0

    def _expire(self, now: float) -> None:
        for value, entry in tuple(self._entries.items()):
            if entry.expires_at <= now:
                del self._entries[value]
                self._total_chars -= len(entry.text)


class SearchDocumentService:
    """Fetches and exposes bounded text only while the same authorization remains current."""

    def __init__(
        self,
        *,
        fetcher: EvidenceFetcher,
        limits: SearchDocumentStoreLimits = SearchDocumentStoreLimits(),
        store: BoundedSearchDocumentStore | None = None,
    ) -> None:
        if not isinstance(fetcher, EvidenceFetcher):
            raise TypeError("fetcher must be EvidenceFetcher")
        if not isinstance(limits, SearchDocumentStoreLimits):
            raise TypeError("document limits are required")
        if store is not None and not isinstance(store, BoundedSearchDocumentStore):
            raise TypeError("document store is invalid")
        self._fetcher = fetcher
        self._limits = limits
        self._store = store or BoundedSearchDocumentStore(limits)
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True
        self._store.clear()

    async def fetch(
        self,
        url: str,
        *,
        scope: SearchDocumentScope,
        authorization_current: AuthorizationCurrent,
    ) -> SearchDocumentFetchResult:
        store = self._store
        await self._require_current(store, authorization_current)
        try:
            evidence = await self._fetcher.fetch(url)
        except asyncio.CancelledError:
            raise
        except EvidenceLimitError as exc:
            raise SearchDocumentLimitError("document fetch is unavailable") from exc
        except EvidenceResponseError as exc:
            # Existing EvidenceFetcher rejects application/pdf before text is
            # materialized.  Keep that outcome typed and do not mark runtime
            # readiness false just because PDF parsing is intentionally absent.
            raise SearchDocumentUnsupportedError("document representation is unsupported") from exc
        except EvidenceFetchError as exc:
            raise SearchDocumentError("document fetch is unavailable") from exc
        await self._require_current(store, authorization_current)
        reference = store.put(scope=scope, evidence=evidence)
        preview_end = min(len(evidence.text), self._limits.max_excerpt_chars)
        result = SearchDocumentFetchResult(
            reference=reference.value,
            title=evidence.title,
            preview=evidence.text[:preview_end],
            media_type=evidence.media_type,
            content_hash=evidence.content_hash,
            next_offset=preview_end if preview_end < len(evidence.text) else None,
        )
        try:
            await self._require_current(store, authorization_current)
        except SearchDocumentAuthorizationError:
            if not store.discard(reference):
                # A store race means the content cannot safely be accounted
                # for or removed.  Quarantine this service identity rather
                # than leaving it available for subsequent callers.
                self._closing = True
            raise
        return result

    async def read(
        self,
        reference: str,
        *,
        scope: SearchDocumentScope,
        authorization_current: AuthorizationCurrent,
        offset: int = 0,
    ) -> SearchDocumentExcerpt:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise SearchDocumentLimitError("document continuation is invalid")
        store = self._store
        await self._require_current(store, authorization_current)
        entry = store.get_value(reference, scope)
        await self._require_current(store, authorization_current)
        if offset >= len(entry.text):
            raise SearchDocumentNotFoundError("document continuation is unavailable")
        ending = min(len(entry.text), offset + self._limits.max_excerpt_chars)
        excerpt = SearchDocumentExcerpt(
            title=entry.title,
            text=entry.text[offset:ending],
            offset=offset,
            next_offset=ending if ending < len(entry.text) else None,
        )
        await self._require_current(store, authorization_current)
        return excerpt

    async def find(
        self,
        reference: str,
        query: str,
        *,
        scope: SearchDocumentScope,
        authorization_current: AuthorizationCurrent,
        offset: int = 0,
    ) -> SearchDocumentFindResult:
        normalized_query = _normalize_query(query)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise SearchDocumentLimitError("document continuation is invalid")
        store = self._store
        await self._require_current(store, authorization_current)
        entry = store.get_value(reference, scope)
        await self._require_current(store, authorization_current)
        matches = re.finditer(re.escape(normalized_query), entry.text, flags=re.IGNORECASE)
        hits: list[SearchDocumentFindHit] = []
        next_offset: int | None = None
        for match in matches:
            if match.start() < offset:
                continue
            if len(hits) >= self._limits.max_find_hits:
                next_offset = match.start()
                break
            start = max(0, match.start() - self._limits.max_find_context_chars // 2)
            end = min(len(entry.text), match.end() + self._limits.max_find_context_chars // 2)
            hits.append(SearchDocumentFindHit(text=entry.text[start:end], offset=match.start()))
        result = SearchDocumentFindResult(hits=tuple(hits), next_offset=next_offset)
        await self._require_current(store, authorization_current)
        return result

    async def _require_current(
        self,
        store: BoundedSearchDocumentStore,
        authorization_current: AuthorizationCurrent,
    ) -> None:
        if self._closing or self._store is not store or not callable(authorization_current):
            raise SearchDocumentAuthorizationError("document is not currently authorized")
        try:
            allowed = authorization_current()
            if inspect.isawaitable(allowed):
                allowed = await allowed
        except asyncio.CancelledError:
            raise
        except Exception:
            allowed = False
        if allowed is not True or self._closing or self._store is not store:
            raise SearchDocumentAuthorizationError("document is not currently authorized")


def _normalize_query(value: object) -> str:
    if not isinstance(value, str):
        raise SearchDocumentLimitError("document query is invalid")
    normalized = _SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    if not 1 <= len(normalized) <= 200 or any(
        unicodedata.category(character).startswith("C") for character in normalized
    ):
        raise SearchDocumentLimitError("document query is invalid")
    return normalized


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _same_access_scope(origin: SearchDocumentScope, current: SearchDocumentScope) -> bool:
    return (
        origin.guild_id == current.guild_id
        and origin.channel_id == current.channel_id
        and origin.user_id == current.user_id
    )


__all__ = [
    "AuthorizationCurrent",
    "BoundedSearchDocumentStore",
    "SearchDocumentAuthorizationError",
    "SearchDocumentError",
    "SearchDocumentExcerpt",
    "SearchDocumentFetchResult",
    "SearchDocumentFindHit",
    "SearchDocumentFindResult",
    "SearchDocumentLimitError",
    "SearchDocumentNotFoundError",
    "SearchDocumentReference",
    "SearchDocumentScope",
    "SearchDocumentService",
    "SearchDocumentStoreLimits",
    "SearchDocumentUnsupportedError",
]
