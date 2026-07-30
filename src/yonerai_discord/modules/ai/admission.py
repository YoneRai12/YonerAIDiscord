"""AI mentionのprocess全体resource admission。

本文、添付、Discord IDを保持・公開せず、active/waiting件数だけを管理する。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from typing import Self, TypeAlias


_AdmissionKey: TypeAlias = tuple[str, int, int]


class AdmissionRejection(StrEnum):
    CLOSING = "closing"
    QUEUE_FULL = "queue_full"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class AdmissionStats:
    active: int
    waiting: int
    active_keys: int
    closing: bool


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    lease: AdmissionLease | None
    rejection: AdmissionRejection | None

    def __post_init__(self) -> None:
        if (self.lease is None) == (self.rejection is None):
            raise ValueError("exactly one of lease or rejection is required")

    @property
    def admitted(self) -> bool:
        return self.lease is not None


class AdmissionLease:
    """取得済みslot。releaseは冪等。"""

    __slots__ = ("_controller", "_key", "_released")

    def __init__(self, controller: AIAdmissionController, key: _AdmissionKey) -> None:
        self._controller = controller
        self._key = key
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._controller._release(self._key)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.release()


class AIAdmissionController:
    """global上限とguildまたはDM user/channel単位の直列化を保証するbounded gate。"""

    def __init__(
        self,
        *,
        max_global: int = 4,
        max_waiters: int = 32,
        wait_timeout_seconds: float = 2.0,
    ) -> None:
        if isinstance(max_global, bool) or not isinstance(max_global, int) or not 1 <= max_global <= 32:
            raise ValueError("max_global must be between 1 and 32")
        if isinstance(max_waiters, bool) or not isinstance(max_waiters, int) or not 1 <= max_waiters <= 512:
            raise ValueError("max_waiters must be between 1 and 512")
        if (
            isinstance(wait_timeout_seconds, bool)
            or not isinstance(wait_timeout_seconds, (int, float))
            or not 0.1 <= float(wait_timeout_seconds) <= 10.0
        ):
            raise ValueError("wait_timeout_seconds must be between 0.1 and 10")
        self.max_global = max_global
        self.max_waiters = max_waiters
        self.wait_timeout_seconds = float(wait_timeout_seconds)
        self._condition = asyncio.Condition()
        self._active_total = 0
        self._active_by_key: dict[_AdmissionKey, int] = {}
        self._waiters: OrderedDict[int, _AdmissionKey] = OrderedDict()
        self._next_ticket = 0
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    def stats(self) -> AdmissionStats:
        return AdmissionStats(
            active=self._active_total,
            waiting=len(self._waiters),
            active_keys=len(self._active_by_key),
            closing=self._closing,
        )

    async def acquire(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
    ) -> AdmissionDecision:
        key = _scope_key(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
        async with self._condition:
            if self._closing:
                return AdmissionDecision(None, AdmissionRejection.CLOSING)
            if not self._waiters and self._can_activate(key):
                return AdmissionDecision(self._activate(key), None)
            if len(self._waiters) >= self.max_waiters:
                return AdmissionDecision(None, AdmissionRejection.QUEUE_FULL)

            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiters[ticket] = key
            try:
                async with asyncio.timeout(self.wait_timeout_seconds):
                    while True:
                        if self._closing:
                            self._waiters.pop(ticket, None)
                            self._condition.notify_all()
                            return AdmissionDecision(None, AdmissionRejection.CLOSING)
                        if self._can_activate(key) and ticket == self._first_eligible_ticket():
                            self._waiters.pop(ticket, None)
                            return AdmissionDecision(self._activate(key), None)
                        await self._condition.wait()
            except TimeoutError:
                self._waiters.pop(ticket, None)
                self._condition.notify_all()
                return AdmissionDecision(None, AdmissionRejection.TIMEOUT)
            except asyncio.CancelledError:
                self._waiters.pop(ticket, None)
                self._condition.notify_all()
                raise

    async def begin_close(self) -> None:
        async with self._condition:
            self._closing = True
            self._condition.notify_all()

    async def drain(self, *, timeout_seconds: float) -> bool:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.1 <= float(timeout_seconds) <= 60.0
        ):
            raise ValueError("timeout_seconds must be between 0.1 and 60")
        try:
            async with asyncio.timeout(float(timeout_seconds)):
                async with self._condition:
                    while self._active_total > 0:
                        await self._condition.wait()
        except TimeoutError:
            return False
        return True

    def _can_activate(self, key: _AdmissionKey) -> bool:
        return self._active_total < self.max_global and key not in self._active_by_key

    def _first_eligible_ticket(self) -> int | None:
        if self._active_total >= self.max_global:
            return None
        return next((ticket for ticket, key in self._waiters.items() if key not in self._active_by_key), None)

    def _activate(self, key: _AdmissionKey) -> AdmissionLease:
        if not self._can_activate(key):
            raise RuntimeError("admission invariant violated")
        self._active_total += 1
        self._active_by_key[key] = 1
        return AdmissionLease(self, key)

    async def _release(self, key: _AdmissionKey) -> None:
        async with self._condition:
            if self._active_by_key.pop(key, None) is None:
                return
            self._active_total -= 1
            self._condition.notify_all()


def _positive_id(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _optional_guild_id(value: object) -> int | None:
    if value is None:
        return None
    return _positive_id(value, "guild_id")


def _scope_key(*, guild_id: object, channel_id: object, user_id: object) -> _AdmissionKey:
    validated_guild_id = _optional_guild_id(guild_id)
    validated_channel_id = _positive_id(channel_id, "channel_id")
    validated_user_id = _positive_id(user_id, "user_id")
    if validated_guild_id is not None:
        return ("guild", validated_guild_id, 0)
    return ("dm", validated_channel_id, validated_user_id)


__all__ = [
    "AIAdmissionController",
    "AdmissionDecision",
    "AdmissionLease",
    "AdmissionRejection",
    "AdmissionStats",
]
