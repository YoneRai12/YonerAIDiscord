from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from yonerai_discord.modules.audio_core import LocalMediaLibrary, MediaLibraryError, Track

from .models import AuthorizedMusicTrackRef
from .repository import MusicPlaylistRepository


_MAX_CANDIDATES = 25
_MAX_EXCLUSIONS = 100
_MAX_CURSOR = 2_147_483_647


@dataclass(frozen=True, slots=True)
class RelatedTrackRequest:
    guild_id: int
    requester_id: int
    cursor: int = 0
    max_candidates: int = 10
    seed_title: str | None = field(default=None, repr=False)
    excluded_library_refs: frozenset[str] = field(default_factory=frozenset, repr=False)
    excluded_titles: frozenset[str] = field(default_factory=frozenset, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.guild_id, bool) or not isinstance(self.guild_id, int) or self.guild_id <= 0:
            raise ValueError("guild_id must be a positive integer")
        if isinstance(self.requester_id, bool) or not isinstance(self.requester_id, int) or self.requester_id <= 0:
            raise ValueError("requester_id must be a positive integer")
        if isinstance(self.cursor, bool) or not isinstance(self.cursor, int) or not 0 <= self.cursor <= _MAX_CURSOR:
            raise ValueError("cursor is invalid")
        if (
            isinstance(self.max_candidates, bool)
            or not isinstance(self.max_candidates, int)
            or not 1 <= self.max_candidates <= _MAX_CANDIDATES
        ):
            raise ValueError("max_candidates must be between 1 and 25")

        references = self.excluded_library_refs
        if not isinstance(references, frozenset) or len(references) > _MAX_EXCLUSIONS:
            raise ValueError("excluded_library_refs is invalid")
        normalized_references: set[str] = set()
        for reference in references:
            validated = AuthorizedMusicTrackRef(
                library_ref=reference,
                content_sha256="0" * 64,
            )
            normalized_references.add(validated.library_ref)

        titles = self.excluded_titles
        if not isinstance(titles, frozenset) or len(titles) > _MAX_EXCLUSIONS:
            raise ValueError("excluded_titles is invalid")
        normalized_titles = {_normalized_title(title) for title in titles}
        seed_title = None if self.seed_title is None else _normalized_title(self.seed_title)
        if seed_title is not None:
            normalized_titles.add(seed_title)

        object.__setattr__(self, "seed_title", seed_title)
        object.__setattr__(self, "excluded_library_refs", frozenset(normalized_references))
        object.__setattr__(self, "excluded_titles", frozenset(normalized_titles))


@dataclass(frozen=True, slots=True)
class RelatedTrackSelection:
    candidate: Track | None = field(repr=False)
    next_cursor: int
    examined_count: int

    def __post_init__(self) -> None:
        if self.candidate is not None and not isinstance(self.candidate, Track):
            raise TypeError("candidate must be a Track or None")
        if isinstance(self.next_cursor, bool) or not isinstance(self.next_cursor, int) or self.next_cursor < 0:
            raise ValueError("next_cursor is invalid")
        if (
            isinstance(self.examined_count, bool)
            or not isinstance(self.examined_count, int)
            or not 0 <= self.examined_count <= _MAX_CANDIDATES
        ):
            raise ValueError("examined_count is invalid")


class RelatedTrackPort(Protocol):
    async def select_related(self, request: RelatedTrackRequest) -> RelatedTrackSelection: ...


class AuthorizedLocalRelatedTrackAdapter:
    """Select one bounded candidate from the guild's local rights ledger."""

    def __init__(
        self,
        library: LocalMediaLibrary,
        repository: MusicPlaylistRepository,
    ) -> None:
        if not isinstance(library, LocalMediaLibrary):
            raise TypeError("library must be a LocalMediaLibrary")
        if not isinstance(repository, MusicPlaylistRepository):
            raise TypeError("repository must be a MusicPlaylistRepository")
        self.library = library
        self.repository = repository

    async def select_related(self, request: RelatedTrackRequest) -> RelatedTrackSelection:
        if not isinstance(request, RelatedTrackRequest):
            raise TypeError("request must be a RelatedTrackRequest")
        return await asyncio.to_thread(self._select_related, request)

    def _select_related(self, request: RelatedTrackRequest) -> RelatedTrackSelection:
        grants = self.repository.list_track_rights(
            request.guild_id,
            limit=_MAX_CANDIDATES,
        )
        if not grants:
            return RelatedTrackSelection(None, 0, 0)

        start = request.cursor % len(grants)
        examined_count = 0
        next_cursor = start
        for offset in range(min(request.max_candidates, len(grants))):
            grant = grants[(start + offset) % len(grants)]
            examined_count += 1
            next_cursor = (start + offset + 1) % len(grants)
            if grant.library_ref in request.excluded_library_refs:
                continue
            track = self._resolve(grant, request.requester_id)
            if track is None or track.title.strip().casefold() in request.excluded_titles:
                continue
            if not self.repository.track_rights_allowed(
                request.guild_id,
                grant.library_ref,
                grant.content_sha256,
            ):
                continue
            return RelatedTrackSelection(track, next_cursor, examined_count)
        return RelatedTrackSelection(None, next_cursor, examined_count)

    def _resolve(
        self,
        grant: AuthorizedMusicTrackRef,
        requester_id: int,
    ) -> Track | None:
        try:
            return self.library.resolve_persisted_track(
                grant.library_ref,
                grant.content_sha256,
                requester_id,
                0,
            )
        except MediaLibraryError:
            return None


def _normalized_title(value: object) -> str:
    title = value.strip().casefold() if isinstance(value, str) else ""
    if not title or len(title) > 200 or any(ord(character) < 32 or ord(character) == 127 for character in title):
        raise ValueError("excluded title is invalid")
    return title
