from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from threading import Lock

from .models import ChallengeBinding, ChallengeIntent, ChallengeRecord, ClaimedChallenge, ChallengePurpose


class InMemoryChallengeRepository:
    """テスト・単一プロセス開発用。永続環境では同じ契約のDB adapterへ交換する。"""

    def __init__(self) -> None:
        self._records: dict[str, ChallengeRecord] = {}
        self._claims: dict[str, str] = {}
        self._lock = Lock()

    def add(self, record: ChallengeRecord) -> None:
        with self._lock:
            if record.token_digest in self._records:
                raise ValueError("challenge digest already exists")
            self._records[record.token_digest] = record

    def eligible(
        self,
        token_digest: str,
        purpose: ChallengePurpose,
        intent: ChallengeIntent,
        now: datetime,
    ) -> bool:
        with self._lock:
            record = self._records.get(token_digest)
            return bool(
                record is not None
                and record.purpose is purpose
                and record.binding.intent is intent
                and record.expires_at > now
                and record.finalized_at is None
                and (record.claim_id is None or record.claim_expires_at is None or record.claim_expires_at <= now)
            )

    def claim(
        self,
        token_digest: str,
        purpose: ChallengePurpose,
        binding: ChallengeBinding,
        claim_id: str,
        now: datetime,
    ) -> ClaimedChallenge | None:
        with self._lock:
            record = self._records.get(token_digest)
            if (
                record is None
                or record.purpose is not purpose
                or record.binding != binding
                or record.expires_at <= now
                or (
                    record.claim_id is not None
                    and record.claim_expires_at is not None
                    and record.claim_expires_at > now
                )
                or record.finalized_at is not None
            ):
                return None
            if record.claim_id is not None:
                self._claims.pop(record.claim_id, None)
            claimed = replace(
                record,
                claim_id=claim_id,
                claimed_at=now,
                claim_expires_at=now + timedelta(seconds=120),
            )
            self._records[token_digest] = claimed
            self._claims[claim_id] = token_digest
            return ClaimedChallenge(claim_id, purpose, binding, record.expires_at)

    def claim_for_intent(
        self,
        token_digest: str,
        purpose: ChallengePurpose,
        intent: ChallengeIntent,
        claim_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedChallenge | None:
        with self._lock:
            record = self._records.get(token_digest)
            if (
                record is None
                or record.purpose is not purpose
                or record.binding.intent is not intent
                or record.expires_at <= now
                or record.finalized_at is not None
                or (
                    record.claim_id is not None
                    and record.claim_expires_at is not None
                    and record.claim_expires_at > now
                )
            ):
                return None
            if record.claim_id is not None:
                self._claims.pop(record.claim_id, None)
            claim_expires_at = now + timedelta(seconds=lease_seconds)
            claimed = replace(
                record,
                claim_id=claim_id,
                claimed_at=now,
                claim_expires_at=claim_expires_at,
            )
            self._records[token_digest] = claimed
            self._claims[claim_id] = token_digest
            return ClaimedChallenge(claim_id, purpose, record.binding, record.expires_at)

    def finalize(self, claim_id: str, now: datetime) -> bool:
        with self._lock:
            digest = self._claims.get(claim_id)
            record = self._records.get(digest) if digest else None
            if record is None or record.claim_id != claim_id or record.finalized_at is not None:
                return False
            self._records[digest] = replace(record, finalized_at=now)
            self._claims.pop(claim_id, None)
            return True

    def release(self, claim_id: str) -> bool:
        with self._lock:
            digest = self._claims.get(claim_id)
            record = self._records.get(digest) if digest else None
            if record is None or record.claim_id != claim_id or record.finalized_at is not None:
                return False
            self._records[digest] = replace(
                record,
                claim_id=None,
                claimed_at=None,
                claim_expires_at=None,
            )
            self._claims.pop(claim_id, None)
            return True

    def purge_expired(self, now: datetime) -> int:
        with self._lock:
            expired = [
                digest
                for digest, record in self._records.items()
                if record.expires_at <= now
                and (record.claim_id is None or record.claim_expires_at is None or record.claim_expires_at <= now)
            ]
            for digest in expired:
                record = self._records.pop(digest)
                if record.claim_id:
                    self._claims.pop(record.claim_id, None)
            return len(expired)

    def revoke_unclaimed(self, binding: ChallengeBinding, now: datetime) -> int:
        with self._lock:
            digests = [
                digest
                for digest, record in self._records.items()
                if record.binding == binding and record.claim_id is None and record.finalized_at is None
            ]
            for digest in digests:
                self._records.pop(digest, None)
            return len(digests)

    def snapshot(self) -> tuple[ChallengeRecord, ...]:
        """診断用。生tokenはRepositoryへ保存されない。"""
        with self._lock:
            return tuple(self._records.values())
