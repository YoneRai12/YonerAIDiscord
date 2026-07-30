"""Versioned, user-owned consent for sending content to a remote AI."""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .state_repository import AIStateRepository, StoredConsent


REMOTE_CONSENT_GRANT_TEXT = "外部AI送信に同意"
REMOTE_CONSENT_REVOKE_TEXT = "外部AI送信を取り消す"
DEFAULT_POLICY_VERSION = "remote-ai-policy-v1"
DEFAULT_DISCLOSURE_VERSION = "remote-ai-disclosure-v2"


@dataclass(frozen=True, slots=True)
class RemoteConsentStats:
    """Content-free metrics; Discord IDs are never exposed."""

    active_grants: int
    capacity: int
    ttl_seconds: int | None


class RemoteConsentStore:
    """Consent keyed globally by Discord user and invalidated by version change.

    ``guild_id`` may be ``None`` for a DM. Guild/channel IDs validate the
    current event but deliberately do not participate in the durable key: the
    disclosure is user-scoped, so one affirmative consent survives channel
    changes, a guild/DM surface change, and a bot restart.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int | None = None,
        max_grants: int = 1_024,
        clock: Callable[[], float] = time.time,
        policy_version: str = DEFAULT_POLICY_VERSION,
        disclosure_version: str = DEFAULT_DISCLOSURE_VERSION,
        repository: AIStateRepository | None = None,
        database_path: str | Path | None = None,
    ) -> None:
        if ttl_seconds is not None and (
            isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 3_600
        ):
            raise ValueError("ttl_seconds must be between 60 and 3600, or None")
        if isinstance(max_grants, bool) or not isinstance(max_grants, int) or not 1 <= max_grants <= 10_000:
            raise ValueError("max_grants must be between 1 and 10000")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not policy_version or len(policy_version) > 128:
            raise ValueError("policy_version must be a non-empty bounded string")
        if not disclosure_version or len(disclosure_version) > 128:
            raise ValueError("disclosure_version must be a non-empty bounded string")
        if repository is not None and database_path is not None:
            raise ValueError("repository and database_path are mutually exclusive")

        self.ttl_seconds = ttl_seconds
        self.max_grants = max_grants
        self.policy_version = policy_version
        self.disclosure_version = disclosure_version
        self._clock = clock
        self._repository = repository or (AIStateRepository(database_path) if database_path is not None else None)
        self._owns_repository = repository is None and database_path is not None
        self._grants: OrderedDict[int, StoredConsent] = OrderedDict()
        self._observed_clock_floor = 0.0
        self._clock_rolled_back = False
        self._reload()

    def grant(self, *, guild_id: int | None, channel_id: int, user_id: int) -> None:
        _validate_compat_scope(guild_id, channel_id, user_id)
        if self._repository is not None:
            raw_now = self._raw_now()
            try:
                consent, _effective, rolled_back = self._repository.grant_consent(
                    user_id=user_id,
                    policy_version=self.policy_version,
                    disclosure_version=self.disclosure_version,
                    observed_at=raw_now,
                    ttl_seconds=self.ttl_seconds,
                )
            except Exception as exc:
                raise RuntimeError("remote consent grant could not be committed") from exc
            self._clock_rolled_back = rolled_back
            if rolled_back:
                self._invalidate_ttl_grants()
            if consent is None:
                raise RuntimeError("TTL consent cannot be granted while the wall clock is rolled back")
        else:
            now = self._now()
            if self.ttl_seconds is not None and self._clock_rolled_back:
                raise RuntimeError("TTL consent cannot be granted while the wall clock is rolled back")
            consent = StoredConsent(
                user_id=user_id,
                policy_version=self.policy_version,
                disclosure_version=self.disclosure_version,
                granted_at=now,
                expires_at=None if self.ttl_seconds is None else now + self.ttl_seconds,
            )
        self._grants.pop(user_id, None)
        self._grants[user_id] = consent
        self._prune(consent.granted_at)
        while len(self._grants) > self.max_grants:
            evicted_user_id, _ = self._grants.popitem(last=False)
            if self._repository is not None:
                self._repository.delete_consent(evicted_user_id)

    def revoke(self, *, guild_id: int | None, channel_id: int, user_id: int) -> bool:
        _validate_compat_scope(guild_id, channel_id, user_id)
        existed = self._grants.pop(user_id, None) is not None
        if self._repository is not None:
            existed = self._repository.delete_consent(user_id) or existed
        return existed

    def active(self, *, guild_id: int | None, channel_id: int, user_id: int) -> bool:
        _validate_compat_scope(guild_id, channel_id, user_id)
        return self.active_user(user_id)

    def active_user(self, user_id: int) -> bool:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        now = self._now()
        self._prune(now)
        consent = self._grants.get(user_id)
        if self._repository is not None:
            try:
                consent = self._repository.get_consent(user_id)
            except Exception as exc:
                raise RuntimeError("remote consent state is unavailable") from exc
            if consent is None:
                self._grants.pop(user_id, None)
                return False
            self._grants.pop(user_id, None)
            self._grants[user_id] = consent
        if consent is None:
            return False
        if consent.policy_version != self.policy_version or consent.disclosure_version != self.disclosure_version:
            return False
        return consent.expires_at is None or consent.expires_at > now

    def clear(self) -> None:
        """Explicit administrative wipe; lifecycle shutdown must not call this."""

        self._grants.clear()
        if self._repository is not None:
            self._repository.clear_consents()

    def stats(self) -> RemoteConsentStats:
        self._prune(self._now())
        active = sum(
            consent.policy_version == self.policy_version and consent.disclosure_version == self.disclosure_version
            for consent in self._grants.values()
        )
        return RemoteConsentStats(
            active_grants=active,
            capacity=self.max_grants,
            ttl_seconds=self.ttl_seconds,
        )

    def close(self) -> None:
        if self._owns_repository and self._repository is not None:
            self._repository.close()

    def _reload(self) -> None:
        if self._repository is None:
            return
        for consent in self._repository.list_consents():
            if consent.policy_version != self.policy_version or consent.disclosure_version != self.disclosure_version:
                # A changed disclosure requires a fresh affirmative action. The
                # stale row is removed so a later rollback cannot revive it.
                self._repository.delete_consent(consent.user_id)
                continue
            self._grants[consent.user_id] = consent
        self._prune(self._now())
        while len(self._grants) > self.max_grants:
            user_id, _ = self._grants.popitem(last=False)
            self._repository.delete_consent(user_id)

    def _now(self) -> float:
        value = self._raw_now()
        if self._repository is not None:
            try:
                value, rolled_back = self._repository.observe_remote_consent_clock(value)
            except (RuntimeError, TypeError, ValueError) as exc:
                raise RuntimeError("remote consent clock floor is unavailable") from exc
        else:
            rolled_back = value < self._observed_clock_floor
            value = max(value, self._observed_clock_floor)
            self._observed_clock_floor = value
        self._clock_rolled_back = rolled_back
        if rolled_back:
            self._invalidate_ttl_grants()
        return value

    def _raw_now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value) or value < 0:
            raise RuntimeError("clock returned an invalid value")
        return value

    def _invalidate_ttl_grants(self) -> None:
        expiring_user_ids = [user_id for user_id, consent in self._grants.items() if consent.expires_at is not None]
        for user_id in expiring_user_ids:
            self._grants.pop(user_id, None)

    def _prune(self, now: float) -> None:
        expired = [
            user_id
            for user_id, consent in self._grants.items()
            if consent.expires_at is not None and consent.expires_at <= now
        ]
        for user_id in expired:
            self._grants.pop(user_id, None)
            if self._repository is not None:
                self._repository.delete_expired_consent(user_id, observed_at=now)


def _validate_compat_scope(guild_id: object, channel_id: object, user_id: object) -> None:
    if guild_id is not None and (isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0):
        raise ValueError("guild_id must be a positive integer or None for a DM")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (channel_id, user_id)):
        raise ValueError("channel_id and user_id must be positive integers")


__all__ = [
    "DEFAULT_DISCLOSURE_VERSION",
    "DEFAULT_POLICY_VERSION",
    "REMOTE_CONSENT_GRANT_TEXT",
    "REMOTE_CONSENT_REVOKE_TEXT",
    "RemoteConsentStats",
    "RemoteConsentStore",
]
