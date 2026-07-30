from __future__ import annotations

from threading import RLock
from typing import Protocol, TypeAlias, runtime_checkable

from .models import RbacLevel, normalize_id


GuildId: TypeAlias = int | str


def normalize_guild_id(guild_id: GuildId | None) -> str | None:
    if guild_id is None:
        return None
    if isinstance(guild_id, bool) or not isinstance(guild_id, (int, str)):
        raise TypeError("guild_id must be an int, string, or None")
    normalized = str(guild_id).strip()
    if not normalized:
        raise ValueError("guild_id must not be empty")
    return normalized


@runtime_checkable
class StateStore(Protocol):
    """Control Planeの永続化adapterが実装する最小interface。"""

    def get_module_override(self, module_id: str, guild_id: GuildId | None = None) -> bool | None: ...

    def set_module_override(self, module_id: str, enabled: bool | None, guild_id: GuildId | None = None) -> None: ...

    def get_capability_override(self, capability_id: str, guild_id: GuildId | None = None) -> bool | None: ...

    def set_capability_override(
        self, capability_id: str, enabled: bool | None, guild_id: GuildId | None = None
    ) -> None: ...

    def get_level_override(self, capability_id: str, guild_id: GuildId | None = None) -> RbacLevel | None: ...

    def set_level_override(
        self,
        capability_id: str,
        level: RbacLevel | str | int | None,
        guild_id: GuildId | None = None,
    ) -> None: ...


class InMemoryStateStore:
    """testと単一process構成用のthread-safe state store。"""

    def __init__(self) -> None:
        self._module_overrides: dict[tuple[str | None, str], bool] = {}
        self._capability_overrides: dict[tuple[str | None, str], bool] = {}
        self._level_overrides: dict[tuple[str | None, str], RbacLevel] = {}
        self._lock = RLock()

    def get_module_override(self, module_id: str, guild_id: GuildId | None = None) -> bool | None:
        key = self._key(module_id, guild_id, label="module_id")
        with self._lock:
            return self._module_overrides.get(key)

    def set_module_override(self, module_id: str, enabled: bool | None, guild_id: GuildId | None = None) -> None:
        self._set_bool(
            self._module_overrides,
            self._key(module_id, guild_id, label="module_id"),
            enabled,
        )

    def get_capability_override(self, capability_id: str, guild_id: GuildId | None = None) -> bool | None:
        key = self._key(capability_id, guild_id, label="capability_id")
        with self._lock:
            return self._capability_overrides.get(key)

    def set_capability_override(
        self, capability_id: str, enabled: bool | None, guild_id: GuildId | None = None
    ) -> None:
        self._set_bool(
            self._capability_overrides,
            self._key(capability_id, guild_id, label="capability_id"),
            enabled,
        )

    def get_level_override(self, capability_id: str, guild_id: GuildId | None = None) -> RbacLevel | None:
        key = self._key(capability_id, guild_id, label="capability_id")
        with self._lock:
            return self._level_overrides.get(key)

    def set_level_override(
        self,
        capability_id: str,
        level: RbacLevel | str | int | None,
        guild_id: GuildId | None = None,
    ) -> None:
        key = self._key(capability_id, guild_id, label="capability_id")
        with self._lock:
            if level is None:
                self._level_overrides.pop(key, None)
            else:
                self._level_overrides[key] = RbacLevel.parse(level)

    def clear_guild(self, guild_id: GuildId) -> None:
        normalized_guild = normalize_guild_id(guild_id)
        with self._lock:
            for values in (self._module_overrides, self._capability_overrides, self._level_overrides):
                for key in tuple(values):
                    if key[0] == normalized_guild:
                        del values[key]

    @staticmethod
    def _key(subject_id: str, guild_id: GuildId | None, *, label: str) -> tuple[str | None, str]:
        return normalize_guild_id(guild_id), normalize_id(subject_id, label=label)

    def _set_bool(
        self,
        target: dict[tuple[str | None, str], bool],
        key: tuple[str | None, str],
        enabled: bool | None,
    ) -> None:
        if enabled is not None and not isinstance(enabled, bool):
            raise TypeError("enabled must be bool or None")
        with self._lock:
            if enabled is None:
                target.pop(key, None)
            else:
                target[key] = enabled


# 意味が明確な別名。SQLite adapter側でも同じinterface名を使える。
FeatureStateStore = StateStore
MemoryStateStore = InMemoryStateStore
