"""v0 explicit durable memoryのSQLite row adapter。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable

from yonerai_discord.modules.ai.state_repository import AIStateRepository, StoredV0Memory
from yonerai_discord.secret_detection import contains_secret_like
from yonerai_discord.v0_contracts import (
    MemoryAuthorizationRecordRef,
    MemoryAuthorizationToken,
    MemoryRecord,
    MemoryVisibility,
    Scope,
    memory_record_revision_sha256,
)


DEFAULT_RETENTION_SECONDS = 30 * 24 * 60 * 60


class V0ExplicitMemoryRepository:
    """Only explicit writes enter this table; legacy conversation rows are never read."""

    def __init__(
        self,
        state: AIStateRepository,
        *,
        clock: Callable[[], float] = time.time,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    ) -> None:
        if not isinstance(state, AIStateRepository):
            raise TypeError("state must be an AIStateRepository")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not 60 <= retention_seconds <= 365 * 24 * 60 * 60:
            raise ValueError("retention_seconds is outside the allowed range")
        self._state = state
        self._clock = clock
        self._retention_seconds = retention_seconds

    def remember(self, scope: Scope, content: str) -> MemoryRecord:
        normalized = content.strip()
        if not normalized or len(normalized) > 1_000:
            raise ValueError("memory must contain 1 to 1000 characters")
        if contains_secret_like(normalized):
            raise ValueError("secret-like memory is rejected")
        now = self._now()
        record = MemoryRecord(
            f"memory.{uuid.uuid4().hex}",
            scope,
            normalized,
            now,
            explicit=True,
            retention_seconds=self._retention_seconds,
        )
        self._state.add_v0_memory(
            StoredV0Memory(
                record.memory_id,
                scope.guild_id,
                scope.user_id,
                scope.channel_id,
                scope.dm_channel_id,
                scope.visibility.value,
                record.content,
                record.created_at,
                record.created_at + record.retention_seconds,
            )
        )
        return record

    def list(self, scope: Scope, *, limit: int = 20) -> tuple[MemoryRecord, ...]:
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        rows = self._state.list_v0_memories(
            guild_id=scope.guild_id,
            user_id=scope.user_id,
            channel_id=scope.channel_id,
            dm_channel_id=scope.dm_channel_id,
            visibility=scope.visibility.value,
            now=self._now(),
            limit=limit,
        )
        return tuple(
            MemoryRecord(
                row.memory_id,
                scope,
                row.content,
                row.created_at,
                explicit=True,
                retention_seconds=row.expires_at - row.created_at,
            )
            for row in rows
        )

    def forget(self, scope: Scope, memory_id: str) -> bool:
        return self._state.delete_v0_memory(
            memory_id=memory_id,
            guild_id=scope.guild_id,
            user_id=scope.user_id,
            channel_id=scope.channel_id,
            dm_channel_id=scope.dm_channel_id,
            visibility=scope.visibility.value,
        )

    def clear(self, scope: Scope) -> int:
        return self._state.clear_v0_memories(
            guild_id=scope.guild_id,
            user_id=scope.user_id,
            channel_id=scope.channel_id,
            dm_channel_id=scope.dm_channel_id,
            visibility=scope.visibility.value,
        )

    def clear_all_user_scopes(self, scope: Scope) -> int:
        """privacy変更前の行も含め、本人の現在guild/DMだけを全削除する。"""

        return self._state.clear_v0_memories_for_actor(
            guild_id=scope.guild_id,
            user_id=scope.user_id,
            dm_channel_id=scope.dm_channel_id if scope.guild_id is None else None,
        )

    def recall_scope(self, *, guild_id: int | None, channel_id: int, user_id: int) -> Scope | None:
        if guild_id is None:
            return Scope(
                None,
                user_id,
                dm_channel_id=channel_id,
                visibility=MemoryVisibility.DIRECT_MESSAGE,
            )
        visibility, selected_channel = self._state.get_v0_memory_privacy(guild_id=guild_id, user_id=user_id)
        if visibility == MemoryVisibility.USER_PRIVATE.value:
            return None
        if visibility == MemoryVisibility.CHANNEL_SHARED.value:
            if selected_channel != channel_id:
                return None
            return Scope(
                guild_id,
                user_id,
                channel_id=channel_id,
                visibility=MemoryVisibility.CHANNEL_SHARED,
            )
        if visibility == MemoryVisibility.GUILD_PUBLIC.value:
            return Scope(guild_id, user_id, visibility=MemoryVisibility.GUILD_PUBLIC)
        return None

    def set_privacy(
        self,
        *,
        guild_id: int,
        user_id: int,
        visibility: MemoryVisibility,
        channel_id: int | None,
    ) -> None:
        if visibility not in {
            MemoryVisibility.USER_PRIVATE,
            MemoryVisibility.CHANNEL_SHARED,
            MemoryVisibility.GUILD_PUBLIC,
        }:
            raise ValueError("unsupported guild memory visibility")
        self._state.set_v0_memory_privacy(
            guild_id=guild_id,
            user_id=user_id,
            visibility=visibility.value,
            channel_id=channel_id if visibility is MemoryVisibility.CHANNEL_SHARED else None,
        )

    def privacy(self, *, guild_id: int, user_id: int) -> tuple[MemoryVisibility, int | None]:
        visibility, channel_id = self._state.get_v0_memory_privacy(guild_id=guild_id, user_id=user_id)
        return MemoryVisibility(visibility), channel_id

    def authorization_token(
        self,
        scope: Scope,
        records: tuple[MemoryRecord, ...],
        *,
        request_channel_id: int,
    ) -> MemoryAuthorizationToken | None:
        """選択済みmemoryを本文なしのsink再検査tokenへ固定する。"""

        selected = tuple(records)
        if not selected:
            return None
        if any(record.scope != scope or not record.explicit for record in selected):
            raise ValueError("authorization token requires explicit records in one exact scope")
        current_scope = self.recall_scope(
            guild_id=scope.guild_id,
            channel_id=request_channel_id,
            user_id=scope.user_id,
        )
        if current_scope != scope:
            raise PermissionError("memory privacy no longer permits this scope")
        references: list[MemoryAuthorizationRecordRef] = []
        now = self._now()
        for record in selected:
            current = self._current_record(scope, record.memory_id, now=now)
            if current is None or current[0] != record:
                raise PermissionError("memory changed before authorization token issuance")
            references.append(current[1])
        policy_version, policy_revision, policy_updated_at = self._privacy_policy_version(
            scope,
            request_channel_id=request_channel_id,
        )
        return MemoryAuthorizationToken(
            scope,
            request_channel_id,
            tuple(references),
            policy_revision,
            policy_updated_at,
            policy_version,
        )

    def authorization_current(self, token: MemoryAuthorizationToken) -> bool:
        """forget/expiry/revision/scope/privacy変更が1つでもあればfail-closed。"""

        if not isinstance(token, MemoryAuthorizationToken):
            return False
        try:
            current_scope = self.recall_scope(
                guild_id=token.scope.guild_id,
                channel_id=token.request_channel_id,
                user_id=token.scope.user_id,
            )
            if current_scope != token.scope:
                return False
            policy_version, policy_revision, policy_updated_at = self._privacy_policy_version(
                token.scope,
                request_channel_id=token.request_channel_id,
            )
            if (
                policy_version != token.privacy_policy_version
                or policy_revision != token.privacy_policy_revision
                or policy_updated_at != token.privacy_policy_updated_at
            ):
                return False
            now = self._now()
            for expected in token.records:
                current = self._current_record(token.scope, expected.memory_id, now=now)
                if current is None or current[1] != expected:
                    return False
        except (RuntimeError, TypeError, ValueError):
            return False
        return True

    def _current_record(
        self,
        scope: Scope,
        memory_id: str,
        *,
        now: int,
    ) -> tuple[MemoryRecord, MemoryAuthorizationRecordRef] | None:
        row = self._state.get_v0_memory(
            memory_id=memory_id,
            guild_id=scope.guild_id,
            user_id=scope.user_id,
            channel_id=scope.channel_id,
            dm_channel_id=scope.dm_channel_id,
            visibility=scope.visibility.value,
            now=now,
        )
        revision = self._state.get_v0_memory_revision(memory_id)
        if row is None or revision is None or not revision[2]:
            return None
        current = MemoryRecord(
            row.memory_id,
            scope,
            row.content,
            row.created_at,
            explicit=True,
            retention_seconds=row.expires_at - row.created_at,
        )
        return current, self._record_reference(current, revision=revision[0], updated_at=revision[1])

    @staticmethod
    def _record_reference(
        record: MemoryRecord,
        *,
        revision: int,
        updated_at: int,
    ) -> MemoryAuthorizationRecordRef:
        expires_at = record.created_at + record.retention_seconds
        return MemoryAuthorizationRecordRef(
            record.memory_id,
            revision,
            memory_record_revision_sha256(record),
            updated_at,
            expires_at,
        )

    def _now(self) -> int:
        now = int(self._clock())
        if now <= 0:
            raise RuntimeError("clock returned an invalid timestamp")
        return self._state.observe_v0_memory_clock(now)

    def _privacy_policy_version(self, scope: Scope, *, request_channel_id: int) -> tuple[str, int, int]:
        if scope.guild_id is None:
            policy = {
                "contract": "v0-explicit-memory-privacy-v1",
                "visibility": MemoryVisibility.DIRECT_MESSAGE.value,
                "dm_channel_id": request_channel_id,
            }
            revision = 1
            updated_at = 1
        else:
            visibility, selected_channel = self.privacy(guild_id=scope.guild_id, user_id=scope.user_id)
            policy = {
                "contract": "v0-explicit-memory-privacy-v1",
                "visibility": visibility.value,
                "channel_id": selected_channel,
                "request_channel_id": request_channel_id,
            }
            metadata = self._state.get_v0_memory_policy_revision(
                guild_id=scope.guild_id,
                user_id=scope.user_id,
            )
            if metadata is None:
                revision, updated_at = 0, 0
            else:
                revision, updated_at, present = metadata
                if not present:
                    revision, updated_at = 0, 0
        version = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return version, revision, updated_at


__all__ = ["DEFAULT_RETENTION_SECONDS", "V0ExplicitMemoryRepository"]
