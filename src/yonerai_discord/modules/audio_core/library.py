from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from enum import StrEnum
from pathlib import Path, PurePosixPath

from .models import Track, _normalize_persistence_fields


ALLOWED_AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm"})


class PersistedTrackResolutionReason(StrEnum):
    LIBRARY_REF_UNAVAILABLE = "library_ref_unavailable"
    CONTENT_CHANGED = "content_changed"


class MediaLibraryError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        reason: PersistedTrackResolutionReason | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason


class LocalMediaLibrary:
    """ownerが許可したroot内の音声だけを名前で解決する。"""

    def __init__(
        self,
        roots: tuple[Path, ...],
        *,
        max_files: int = 5_000,
        max_file_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        if not 1 <= max_files <= 50_000:
            raise ValueError("max_files is outside the allowed range")
        if not 1_024 <= max_file_bytes <= 2 * 1024 * 1024 * 1024:
            raise ValueError("max_file_bytes is outside the allowed range")
        resolved = tuple(dict.fromkeys(Path(root).expanduser().resolve(strict=False) for root in roots))
        self.roots = resolved
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self._index: tuple[Path, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self._index)

    def refresh(self) -> int:
        files: list[Path] = []
        for root in self.roots:
            if len(files) >= self.max_files:
                break
            if not root.is_dir():
                continue
            for candidate in root.rglob("*"):
                if len(files) >= self.max_files:
                    break
                if candidate.suffix.casefold() not in ALLOWED_AUDIO_EXTENSIONS:
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                    stat = resolved.stat()
                except OSError:
                    continue
                if not resolved.is_file() or not self._within_any_root(resolved):
                    continue
                if not 0 < stat.st_size <= self.max_file_bytes:
                    continue
                files.append(resolved)
        self._index = tuple(sorted(dict.fromkeys(files), key=lambda path: path.name.casefold()))
        return len(self._index)

    def search(self, query: str, *, requester_id: int, limit: int = 10) -> tuple[Track, ...]:
        normalized = query.strip().casefold()
        if not normalized or len(normalized) > 200:
            raise MediaLibraryError("query is invalid")
        if not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25")

        def score(path: Path) -> tuple[int, int, str]:
            name = path.name.casefold()
            stem = path.stem.casefold()
            if normalized == stem or normalized == name:
                rank = 4
            elif stem.startswith(normalized):
                rank = 3
            elif normalized in stem:
                rank = 2
            elif normalized in name:
                rank = 1
            else:
                rank = 0
            return (rank, -len(name), name)

        matches = [path for path in self._index if score(path)[0] > 0]
        matches.sort(key=score, reverse=True)
        return tuple(Track(path.stem, path, requester_id) for path in matches[:limit])

    def resolve_track(self, query: str, *, requester_id: int) -> Track:
        matches = self.search(query, requester_id=requester_id, limit=1)
        if not matches:
            raise MediaLibraryError("track was not found in the authorized local library")
        return matches[0]

    def track_rights_identity(self, track: Track) -> tuple[str, str]:
        """Return a root-relative identifier and the current content digest.

        The identifier deliberately never contains an absolute root path.  A
        track must still be an indexed local-library member; callers cannot
        use this helper to authorize an arbitrary filesystem path.
        """
        try:
            source = Path(track.source).resolve(strict=True)
            stat = source.stat()
        except OSError as exc:
            raise MediaLibraryError("track is no longer available") from exc
        if (
            not source.is_file()
            or source not in self._index
            or source.suffix.casefold() not in ALLOWED_AUDIO_EXTENSIONS
            or not 0 < stat.st_size <= self.max_file_bytes
        ):
            raise MediaLibraryError("track is not an indexed local file")
        for index, root in enumerate(self.roots):
            try:
                relative = source.relative_to(root)
            except ValueError:
                continue
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                break
            digest = hashlib.sha256()
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return f"root-{index}:{relative.as_posix()}", digest.hexdigest()
        raise MediaLibraryError("track is outside the local library roots")

    def seal_track(self, track: Track) -> Track:
        """Persist可能なroot-relative identityを現在のindexed trackへ封印する。"""

        library_ref, content_sha256 = self.track_rights_identity(track)
        return replace(
            track,
            library_ref=library_ref,
            content_sha256=content_sha256,
        )

    def resolve_persisted_track(
        self,
        library_ref: str,
        sha256: str,
        requester_id: int,
        retry_count: int,
    ) -> Track:
        """保存済みidentityを現在のlibraryへ再束縛し、内容差替えを拒否する。"""

        try:
            reference, expected_digest, validated_retry_count = _normalize_persistence_fields(
                library_ref=library_ref,
                content_sha256=sha256,
                retry_count=retry_count,
            )
            if reference is None or expected_digest is None or type(requester_id) is not int or requester_id <= 0:
                raise ValueError("persisted track values are invalid")
            prefix, relative_text = reference.split(":", 1)
            root_index = int(prefix.removeprefix("root-"))
            root = self.roots[root_index]
            relative = PurePosixPath(relative_text)
            source = root.joinpath(*relative.parts).resolve(strict=True)
            stat = source.stat()
            if (
                not source.is_file()
                or source not in self._index
                or source.suffix.casefold() not in ALLOWED_AUDIO_EXTENSIONS
                or not 0 < stat.st_size <= self.max_file_bytes
                or not self._within_any_root(source)
            ):
                raise ValueError("persisted track is unavailable")
            track = Track(
                title=source.stem,
                source=source,
                requester_id=requester_id,
                library_ref=reference,
                content_sha256=expected_digest,
                retry_count=validated_retry_count,
            )
            current_reference, current_digest = self.track_rights_identity(track)
        except (IndexError, OSError, TypeError, ValueError):
            raise _persisted_track_error(PersistedTrackResolutionReason.LIBRARY_REF_UNAVAILABLE) from None
        if current_reference != reference:
            raise _persisted_track_error(PersistedTrackResolutionReason.LIBRARY_REF_UNAVAILABLE)
        if not hmac.compare_digest(current_digest, expected_digest):
            raise _persisted_track_error(PersistedTrackResolutionReason.CONTENT_CHANGED)
        return track

    def _within_any_root(self, candidate: Path) -> bool:
        for root in self.roots:
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            return True
        return False


def _persisted_track_error(reason: PersistedTrackResolutionReason) -> MediaLibraryError:
    return MediaLibraryError(reason.value, reason=reason)
