"""サイト公開ドメインの、DiscordやCloudflareに依存しない型定義。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import re


_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}\Z")
_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")
_MAX_HTML_BYTES = 1_048_576


class SiteVisibility(StrEnum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class SiteStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ActorRole(StrEnum):
    MEMBER = "member"
    GUILD_ADMIN = "guild_admin"
    BOT_OWNER = "bot_owner"


class PublishAction(StrEnum):
    PUBLISH = "publish"
    ROLLBACK = "rollback"
    ARCHIVE = "archive"


class IdempotencyStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"


class IdempotencyDisposition(StrEnum):
    NEW = "new"
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"
    UNCERTAIN = "uncertain"


def _identifier(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise ValueError(f"{label} must match [a-z0-9][a-z0-9_-]{{0,95}}")
    return normalized


def _actor_id(value: str | int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError("actor_id must be a string or integer")
    normalized = str(value).strip()
    if not normalized or len(normalized) > 128 or any(ord(character) < 32 for character in normalized):
        raise ValueError("actor_id must contain 1 to 128 printable characters")
    return normalized


def _positive_guild_id(value: int | None, *, required: bool) -> int | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("guild_id must be a positive integer")
    return value


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _sha256(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    return normalized


@dataclass(frozen=True, slots=True)
class SiteActor:
    actor_id: str | int
    guild_id: int | None
    role: ActorRole = ActorRole.MEMBER

    def __post_init__(self) -> None:
        role = ActorRole(self.role)
        guild_id = _positive_guild_id(self.guild_id, required=role is not ActorRole.BOT_OWNER)
        object.__setattr__(self, "actor_id", _actor_id(self.actor_id))
        object.__setattr__(self, "guild_id", guild_id)
        object.__setattr__(self, "role", role)

    @property
    def is_bot_owner(self) -> bool:
        return self.role is ActorRole.BOT_OWNER

    @property
    def is_guild_admin(self) -> bool:
        return self.role in {ActorRole.GUILD_ADMIN, ActorRole.BOT_OWNER}


@dataclass(frozen=True, slots=True)
class Site:
    site_id: str
    guild_id: int
    slug: str
    display_name: str
    visibility: SiteVisibility
    created_by: str | int
    created_at: datetime
    updated_at: datetime
    active_release_id: str | None = None
    status: SiteStatus = SiteStatus.ACTIVE
    archived_at: datetime | None = None

    def __post_init__(self) -> None:
        slug = self.slug.strip().lower() if isinstance(self.slug, str) else ""
        if not _SLUG_RE.fullmatch(slug):
            raise ValueError("slug must be a lowercase DNS-safe label")
        display_name = self.display_name.strip() if isinstance(self.display_name, str) else ""
        if not display_name or len(display_name) > 100 or any(ord(character) < 32 for character in display_name):
            raise ValueError("display_name must contain 1 to 100 printable characters")
        active_release_id = (
            None if self.active_release_id is None else _identifier(self.active_release_id, label="active_release_id")
        )
        object.__setattr__(self, "site_id", _identifier(self.site_id, label="site_id"))
        object.__setattr__(self, "guild_id", _positive_guild_id(self.guild_id, required=True))
        object.__setattr__(self, "slug", slug)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "visibility", SiteVisibility(self.visibility))
        status = SiteStatus(self.status)
        archived_at = None if self.archived_at is None else _aware_utc(self.archived_at, label="archived_at")
        if status is SiteStatus.ARCHIVED and archived_at is None:
            raise ValueError("archived sites require archived_at")
        if status is SiteStatus.ACTIVE and archived_at is not None:
            raise ValueError("active sites must not have archived_at")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "archived_at", archived_at)
        object.__setattr__(self, "created_by", _actor_id(self.created_by))
        object.__setattr__(self, "created_at", _aware_utc(self.created_at, label="created_at"))
        object.__setattr__(self, "updated_at", _aware_utc(self.updated_at, label="updated_at"))
        object.__setattr__(self, "active_release_id", active_release_id)


@dataclass(frozen=True, slots=True)
class SiteRelease:
    release_id: str
    site_id: str
    revision: int
    html: str
    visibility: SiteVisibility
    sha256: str
    byte_size: int
    created_by: str | int
    created_at: datetime
    parent_release_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if not isinstance(self.html, str) or not self.html:
            raise ValueError("html must be a non-empty string")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int) or self.byte_size < 1:
            raise ValueError("byte_size must be a positive integer")
        parent = (
            None if self.parent_release_id is None else _identifier(self.parent_release_id, label="parent_release_id")
        )
        try:
            encoded = self.html.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("html must be valid UTF-8") from exc
        if len(encoded) != self.byte_size:
            raise ValueError("byte_size must match the UTF-8 HTML size")
        if len(encoded) > _MAX_HTML_BYTES:
            raise ValueError("html must be at most 1 MiB")
        normalized_sha256 = _sha256(self.sha256)
        if hashlib.sha256(encoded).hexdigest() != normalized_sha256:
            raise ValueError("sha256 must match the UTF-8 HTML content")
        object.__setattr__(self, "release_id", _identifier(self.release_id, label="release_id"))
        object.__setattr__(self, "site_id", _identifier(self.site_id, label="site_id"))
        object.__setattr__(self, "visibility", SiteVisibility(self.visibility))
        object.__setattr__(self, "sha256", normalized_sha256)
        object.__setattr__(self, "created_by", _actor_id(self.created_by))
        object.__setattr__(self, "created_at", _aware_utc(self.created_at, label="created_at"))
        object.__setattr__(self, "parent_release_id", parent)


@dataclass(frozen=True, slots=True)
class SiteActivation:
    activation_id: int
    site_id: str
    release_id: str
    previous_release_id: str | None
    actor_id: str | int
    action: PublishAction
    reason: str
    created_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.activation_id, bool) or not isinstance(self.activation_id, int) or self.activation_id < 1:
            raise ValueError("activation_id must be a positive integer")
        reason = self.reason.strip() if isinstance(self.reason, str) else ""
        if len(reason) > 500 or any(ord(character) < 32 and character not in "\t" for character in reason):
            raise ValueError("reason must be at most 500 printable characters")
        previous = (
            None
            if self.previous_release_id is None
            else _identifier(self.previous_release_id, label="previous_release_id")
        )
        object.__setattr__(self, "site_id", _identifier(self.site_id, label="site_id"))
        object.__setattr__(self, "release_id", _identifier(self.release_id, label="release_id"))
        object.__setattr__(self, "previous_release_id", previous)
        object.__setattr__(self, "actor_id", _actor_id(self.actor_id))
        object.__setattr__(self, "action", PublishAction(self.action))
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "created_at", _aware_utc(self.created_at, label="created_at"))


@dataclass(frozen=True, slots=True)
class SiteAuditEvent:
    audit_id: int
    event: str
    site_id: str | None
    actor_id: str | int
    details: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DiscordMessageBinding:
    message_id: int
    site_id: str
    release_id: str
    guild_id: int
    user_id: str | int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int) or self.message_id <= 0:
            raise ValueError("message_id must be a positive integer")
        object.__setattr__(self, "site_id", _identifier(self.site_id, label="site_id"))
        object.__setattr__(self, "release_id", _identifier(self.release_id, label="release_id"))
        object.__setattr__(self, "guild_id", _positive_guild_id(self.guild_id, required=True))
        object.__setattr__(self, "user_id", _actor_id(self.user_id))
        object.__setattr__(self, "created_at", _aware_utc(self.created_at, label="created_at"))
        object.__setattr__(self, "updated_at", _aware_utc(self.updated_at, label="updated_at"))


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    scope_id: str
    operation: str
    key: str
    request_sha256: str
    status: IdempotencyStatus
    result: dict[str, object] | None
    outbound: dict[str, object] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    disposition: IdempotencyDisposition
    record: IdempotencyRecord


@dataclass(frozen=True, slots=True)
class PublishRequest:
    request_id: str
    action: PublishAction
    site_id: str
    guild_id: int
    actor_id: str | int
    actor_role: ActorRole
    slug: str
    release_id: str
    expected_active_release_id: str | None
    visibility: SiteVisibility
    html: str
    content_sha256: str
    revision: int

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        expected = (
            None
            if self.expected_active_release_id is None
            else _identifier(self.expected_active_release_id, label="expected_active_release_id")
        )
        object.__setattr__(self, "request_id", _identifier(self.request_id, label="request_id"))
        object.__setattr__(self, "action", PublishAction(self.action))
        object.__setattr__(self, "site_id", _identifier(self.site_id, label="site_id"))
        object.__setattr__(self, "guild_id", _positive_guild_id(self.guild_id, required=True))
        object.__setattr__(self, "actor_id", _actor_id(self.actor_id))
        object.__setattr__(self, "actor_role", ActorRole(self.actor_role))
        slug = self.slug.strip().lower() if isinstance(self.slug, str) else ""
        if not _SLUG_RE.fullmatch(slug):
            raise ValueError("slug must be a lowercase DNS-safe label")
        object.__setattr__(self, "slug", slug)
        object.__setattr__(self, "release_id", _identifier(self.release_id, label="release_id"))
        object.__setattr__(self, "expected_active_release_id", expected)
        object.__setattr__(self, "visibility", SiteVisibility(self.visibility))
        normalized_sha256 = _sha256(self.content_sha256)
        object.__setattr__(self, "content_sha256", normalized_sha256)
        if not isinstance(self.html, str) or not self.html:
            raise ValueError("html must be a non-empty string")
        try:
            encoded = self.html.encode("utf-8", errors="strict")
            actual_sha256 = hashlib.sha256(encoded).hexdigest()
        except UnicodeEncodeError as exc:
            raise ValueError("html must be valid UTF-8") from exc
        if len(encoded) > _MAX_HTML_BYTES:
            raise ValueError("html must be at most 1 MiB")
        if actual_sha256 != normalized_sha256:
            raise ValueError("content_sha256 must match the UTF-8 HTML content")


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    request_id: str
    release_id: str
    deployment_id: str
    site_url: str

    def __post_init__(self) -> None:
        deployment = self.deployment_id.strip() if isinstance(self.deployment_id, str) else ""
        if not deployment or len(deployment) > 200 or any(ord(character) < 33 for character in deployment):
            raise ValueError("deployment_id must contain 1 to 200 non-whitespace characters")
        if not isinstance(self.site_url, str) or not self.site_url.strip():
            raise ValueError("site_url must be a non-empty string")
        object.__setattr__(self, "request_id", _identifier(self.request_id, label="request_id"))
        object.__setattr__(self, "release_id", _identifier(self.release_id, label="release_id"))
        object.__setattr__(self, "deployment_id", deployment)
        object.__setattr__(self, "site_url", self.site_url.strip())


__all__ = [
    "ActorRole",
    "DiscordMessageBinding",
    "IdempotencyClaim",
    "IdempotencyDisposition",
    "IdempotencyRecord",
    "IdempotencyStatus",
    "PublishAction",
    "PublishReceipt",
    "PublishRequest",
    "Site",
    "SiteActivation",
    "SiteActor",
    "SiteAuditEvent",
    "SiteRelease",
    "SiteStatus",
    "SiteVisibility",
]
