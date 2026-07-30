from __future__ import annotations

import asyncio
import math
import random
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .client import P2PQuakeClient
from .domain import EarthquakeEvent, EventKind, try_parse_event
from .repository import GuildSubscription, SqliteEarthquakeRepository


Clock = Callable[[], datetime]


class EarthquakeNotifier(Protocol):
    async def send(self, subscription: GuildSubscription, event: EarthquakeEvent) -> bool: ...


@dataclass(frozen=True, slots=True)
class EarthquakeSnapshot:
    connected: bool
    reconnects: int
    received_payloads: int
    invalid_payloads: int
    duplicate_payloads: int
    unknown_payloads: int
    stale_eew_payloads: int
    delivered_notifications: int
    last_event_id: str | None
    last_received_at: datetime | None
    last_error_type: str | None


class EventDeduplicator:
    def __init__(self, capacity: int = 4096) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 100_000:
            raise ValueError("capacity must be between 1 and 100000")
        self.capacity = capacity
        self._keys: OrderedDict[tuple[str, str], None] = OrderedDict()

    def seen(self, event: EarthquakeEvent) -> bool:
        key = (event.id, event.payload_hash)
        if key in self._keys:
            self._keys.move_to_end(key)
            return True
        self._keys[key] = None
        while len(self._keys) > self.capacity:
            self._keys.popitem(last=False)
        return False

    def clear(self) -> None:
        self._keys.clear()


class ExponentialBackoff:
    def __init__(
        self,
        *,
        base_seconds: float = 1.0,
        maximum_seconds: float = 60.0,
        jitter_ratio: float = 0.2,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if base_seconds <= 0 or maximum_seconds < base_seconds:
            raise ValueError("backoff bounds are invalid")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")
        self.base_seconds = float(base_seconds)
        self.maximum_seconds = float(maximum_seconds)
        self.jitter_ratio = float(jitter_ratio)
        self.random_value = random_value
        self._failures = 0

    def reset(self) -> None:
        self._failures = 0

    def next_delay(self) -> float:
        exponent = min(self._failures, 62)
        raw = min(self.maximum_seconds, self.base_seconds * (2**exponent))
        self._failures += 1
        bounded_random = min(1.0, max(0.0, float(self.random_value())))
        jitter = raw * self.jitter_ratio
        return max(0.0, raw - jitter + (2 * jitter * bounded_random))


class EarthquakeService:
    def __init__(
        self,
        client: P2PQuakeClient,
        repository: SqliteEarthquakeRepository,
        notifier: EarthquakeNotifier,
        *,
        clock: Clock = lambda: datetime.now(UTC),
        dedupe_capacity: int = 4096,
        dedupe_retention_seconds: float = 604_800.0,
        eew_max_age_seconds: float = 120.0,
        latest_cache_seconds: float = 5.0,
    ) -> None:
        if eew_max_age_seconds <= 0:
            raise ValueError("eew_max_age_seconds must be positive")
        if (
            isinstance(dedupe_retention_seconds, bool)
            or not isinstance(dedupe_retention_seconds, (int, float))
            or not math.isfinite(float(dedupe_retention_seconds))
            or dedupe_retention_seconds <= 0
        ):
            raise ValueError("dedupe_retention_seconds must be positive and finite")
        if latest_cache_seconds <= 0:
            raise ValueError("latest_cache_seconds must be positive")
        self.client = client
        self.repository = repository
        self.notifier = notifier
        self.clock = clock
        self.deduplicator = EventDeduplicator(dedupe_capacity)
        self.dedupe_capacity = dedupe_capacity
        self.dedupe_retention_seconds = float(dedupe_retention_seconds)
        self.eew_max_age = timedelta(seconds=float(eew_max_age_seconds))
        self.latest_cache = timedelta(seconds=float(latest_cache_seconds))
        self._latest_lock = asyncio.Lock()
        self._latest_cached_at: datetime | None = None
        self._latest_event: EarthquakeEvent | None = None
        self._connected = False
        self._reconnects = 0
        self._received_payloads = 0
        self._invalid_payloads = 0
        self._duplicate_payloads = 0
        self._unknown_payloads = 0
        self._stale_eew_payloads = 0
        self._delivered_notifications = 0
        self._last_event_id: str | None = None
        self._last_received_at: datetime | None = None
        self._last_error_type: str | None = None

    async def fetch_latest(self, *, limit: int = 20) -> EarthquakeEvent | None:
        now = self._now()
        if self._latest_cached_at is not None and now - self._latest_cached_at < self.latest_cache:
            return self._latest_event
        async with self._latest_lock:
            now = self._now()
            if self._latest_cached_at is not None and now - self._latest_cached_at < self.latest_cache:
                return self._latest_event
            payloads = await self.client.fetch_history(limit=limit)
            latest = None
            for payload in payloads:
                event = try_parse_event(payload, received_at=now)
                if event is not None and event.kind is not EventKind.UNKNOWN:
                    latest = event
                    break
            self._latest_event = latest
            self._latest_cached_at = now
            return latest

    async def handle_payload(self, payload: object, *, received_at: datetime | None = None) -> int:
        now = self._normalize_time(received_at or self._now())
        self._received_payloads += 1
        event = try_parse_event(payload, received_at=now)
        if event is None:
            self._invalid_payloads += 1
            return 0
        claimed = await asyncio.to_thread(
            self.repository.claim_event,
            event.id,
            event.payload_hash,
            seen_at=now,
            retention_seconds=self.dedupe_retention_seconds,
            capacity=self.dedupe_capacity,
        )
        if not claimed:
            self._duplicate_payloads += 1
            return 0
        self.deduplicator.seen(event)
        self._last_event_id = event.id
        self._last_received_at = event.received_at
        if event.kind is EventKind.UNKNOWN:
            self._unknown_payloads += 1
            return 0
        if self._is_stale_eew(event, now):
            self._stale_eew_payloads += 1
            return 0

        subscriptions = await asyncio.to_thread(self.repository.list_enabled)
        delivered = 0
        for subscription in subscriptions:
            if not subscription.accepts(event.code, event.max_scale):
                continue
            try:
                sent = await self.notifier.send(subscription, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.record_error(exc)
                continue
            if sent:
                delivered += 1
        self._delivered_notifications += delivered
        return delivered

    def snapshot(self) -> EarthquakeSnapshot:
        return EarthquakeSnapshot(
            connected=self._connected,
            reconnects=self._reconnects,
            received_payloads=self._received_payloads,
            invalid_payloads=self._invalid_payloads,
            duplicate_payloads=self._duplicate_payloads,
            unknown_payloads=self._unknown_payloads,
            stale_eew_payloads=self._stale_eew_payloads,
            delivered_notifications=self._delivered_notifications,
            last_event_id=self._last_event_id,
            last_received_at=self._last_received_at,
            last_error_type=self._last_error_type,
        )

    def mark_connected(self, *, reconnect: bool) -> None:
        self._connected = True
        if reconnect:
            self._reconnects += 1
        self._last_error_type = None

    def mark_disconnected(self) -> None:
        self._connected = False

    def record_error(self, error: BaseException) -> None:
        self._last_error_type = type(error).__name__

    def _is_stale_eew(self, event: EarthquakeEvent, now: datetime) -> bool:
        if event.kind is not EventKind.EEW:
            return False
        reference = event.issue_time or event.basic_time
        if reference is None:
            return True
        if reference > now + timedelta(minutes=5):
            return True
        return now - reference > self.eew_max_age

    def _now(self) -> datetime:
        return self._normalize_time(self.clock())

    @staticmethod
    def _normalize_time(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return value.astimezone(UTC)


class EarthquakeFeedWorker:
    def __init__(
        self,
        client: P2PQuakeClient,
        service: EarthquakeService,
        *,
        gap_fill_limit: int = 25,
        backoff: ExponentialBackoff | None = None,
    ) -> None:
        if isinstance(gap_fill_limit, bool) or not isinstance(gap_fill_limit, int) or not 1 <= gap_fill_limit <= 100:
            raise ValueError("gap_fill_limit must be between 1 and 100")
        self.client = client
        self.service = service
        self.gap_fill_limit = gap_fill_limit
        self.backoff = backoff or ExponentialBackoff()
        self._stop = asyncio.Event()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("earthquake feed worker is already running")
        self._running = True
        connected_once = False
        try:
            while not self._stop.is_set():
                received_on_connection = False
                try:
                    async with self.client.websocket() as websocket:
                        reconnect = connected_once
                        connected_once = True
                        self.service.mark_connected(reconnect=reconnect)
                        if reconnect:
                            await self._gap_fill()
                        async for payload in self.client.iter_messages(websocket):
                            if self._stop.is_set():
                                break
                            received_on_connection = True
                            self.backoff.reset()
                            await self.service.handle_payload(payload)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.service.record_error(exc)
                finally:
                    self.service.mark_disconnected()
                if self._stop.is_set():
                    break
                if received_on_connection:
                    self.backoff.reset()
                delay = self.backoff.next_delay()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            self.service.mark_disconnected()
            self._running = False

    async def _gap_fill(self) -> None:
        try:
            payloads = await self.client.fetch_history(limit=self.gap_fill_limit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.service.record_error(exc)
            return
        for payload in reversed(payloads):
            if self._stop.is_set():
                return
            await self.service.handle_payload(payload)
