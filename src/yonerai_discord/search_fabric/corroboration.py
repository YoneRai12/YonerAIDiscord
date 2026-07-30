from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum

from .classifier import CorroborationState


_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


class DuplicateKind(StrEnum):
    UNIQUE = "unique"
    EXACT_CONTENT = "exact_content"
    SYNDICATED = "syndicated"


@dataclass(frozen=True, slots=True)
class CorroborationCandidate:
    evidence_id: str = field(repr=False)
    hostname: str
    content_hash: str = field(repr=False)
    syndication_hash: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str) or not _OPAQUE_ID.fullmatch(self.evidence_id):
            raise ValueError("evidence_id must be an opaque identifier")
        normalized_hostname = _normalize_hostname(self.hostname)
        object.__setattr__(self, "hostname", normalized_hostname)
        if not isinstance(self.content_hash, str) or not _DIGEST.fullmatch(self.content_hash):
            raise ValueError("content_hash must be a prefixed lowercase SHA-256 digest")
        if self.syndication_hash is not None and (
            not isinstance(self.syndication_hash, str) or not _DIGEST.fullmatch(self.syndication_hash)
        ):
            raise ValueError("syndication_hash must be a prefixed lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class CorroborationGroup:
    group_id: str = field(repr=False)
    member_ids: tuple[str, ...] = field(repr=False)
    hostnames: tuple[str, ...]
    duplicate_kind: DuplicateKind

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.group_id):
            raise ValueError("group_id must be a prefixed SHA-256 digest")
        if not self.member_ids or len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError("member_ids must be non-empty and unique")
        if not self.hostnames or tuple(sorted(set(self.hostnames))) != self.hostnames:
            raise ValueError("hostnames must be sorted and unique")
        if not isinstance(self.duplicate_kind, DuplicateKind):
            raise TypeError("duplicate_kind must be DuplicateKind")


@dataclass(frozen=True, slots=True)
class CorroborationReport:
    groups: tuple[CorroborationGroup, ...]
    independent_group_count: int
    corroboration: CorroborationState

    def __post_init__(self) -> None:
        if (
            isinstance(self.independent_group_count, bool)
            or not isinstance(self.independent_group_count, int)
            or not 0 <= self.independent_group_count <= len(self.groups)
        ):
            raise ValueError("independent_group_count is outside the allowed range")
        if not isinstance(self.corroboration, CorroborationState):
            raise TypeError("corroboration must be CorroborationState")


def group_corroboration(
    candidates: tuple[CorroborationCandidate, ...],
    *,
    max_candidates: int = 32,
) -> CorroborationReport:
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, CorroborationCandidate) for candidate in candidates
    ):
        raise TypeError("candidates must be a tuple of CorroborationCandidate")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 128:
        raise ValueError("max_candidates is outside the allowed range")
    if len(candidates) > max_candidates:
        raise ValueError("too many corroboration candidates")
    if len({candidate.evidence_id for candidate in candidates}) != len(candidates):
        raise ValueError("evidence_id values must be unique")
    if not candidates:
        return CorroborationReport(
            groups=(),
            independent_group_count=0,
            corroboration=CorroborationState.NONE,
        )

    parent = list(range(len(candidates)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    content_owner: dict[str, int] = {}
    syndication_owner: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        prior = content_owner.setdefault(candidate.content_hash, index)
        union(index, prior)
        if candidate.syndication_hash is not None:
            prior = syndication_owner.setdefault(candidate.syndication_hash, index)
            union(index, prior)

    grouped_indices: dict[int, list[int]] = {}
    for index in range(len(candidates)):
        grouped_indices.setdefault(find(index), []).append(index)

    groups: list[CorroborationGroup] = []
    for indices in sorted(grouped_indices.values(), key=lambda values: values[0]):
        members = tuple(candidates[index] for index in indices)
        content_hashes = {member.content_hash for member in members}
        duplicate_kind = DuplicateKind.UNIQUE
        if len(members) > 1:
            duplicate_kind = DuplicateKind.EXACT_CONTENT if len(content_hashes) == 1 else DuplicateKind.SYNDICATED
        member_ids = tuple(member.evidence_id for member in members)
        group_material = "\0".join(sorted(member_ids)).encode("ascii")
        groups.append(
            CorroborationGroup(
                group_id=f"sha256:{hashlib.sha256(group_material).hexdigest()}",
                member_ids=member_ids,
                hostnames=tuple(sorted({member.hostname for member in members})),
                duplicate_kind=duplicate_kind,
            )
        )

    # Different content hashes prove only that pages are not exact duplicates.
    # Claim-level agreement is not available in Stage 1, so unrelated or
    # contradictory pages must never be promoted to independent corroboration.
    if any(group.duplicate_kind is not DuplicateKind.UNIQUE for group in groups):
        state = CorroborationState.DUPLICATE_ONLY
    else:
        state = CorroborationState.NONE
    return CorroborationReport(
        groups=tuple(groups),
        independent_group_count=0,
        corroboration=state,
    )


def _normalize_hostname(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("hostname must be a string")
    normalized = value.strip().lower().removesuffix(".")
    try:
        normalized = normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("hostname is malformed") from exc
    if (
        not _HOSTNAME.fullmatch(normalized)
        or ".." in normalized
        or any(not label or len(label) > 63 for label in normalized.split("."))
    ):
        raise ValueError("hostname is malformed")
    return normalized
