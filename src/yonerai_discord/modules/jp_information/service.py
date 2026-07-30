from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Generic, TypeVar, cast

from .client import (
    CAO_HOLIDAY_CSV_URL,
    JMA_FORECAST_URL_TEMPLATE,
    JMA_WARNING_URL_TEMPLATE,
    CabinetOfficeHolidayClient,
    JmaClient,
)
from .domain import (
    Holiday,
    HolidayCalendar,
    HolidayYear,
    RegionCatalog,
    WarningReport,
    WeatherForecast,
    parse_holiday_csv,
    parse_region_catalog,
    parse_warning_report,
    parse_weather_forecast,
)
from .errors import CircuitOpenError, ProviderBusyError


JST = timezone(timedelta(hours=9))
JMA_AREA_CACHE_TTL_SECONDS = 3_600.0
JMA_FORECAST_CACHE_TTL_SECONDS = 1_800.0
JMA_WARNING_CACHE_TTL_SECONDS = 900.0
HOLIDAY_CACHE_TTL_SECONDS = 86_400.0

T = TypeVar("T")
Loader = Callable[[], Awaitable[T]]
DateTimeClock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class ProviderSnapshot:
    name: str
    consecutive_failures: int
    circuit_open: bool
    half_open_probe: bool


@dataclass(frozen=True, slots=True)
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class AsyncTTLCache(Generic[T]):
    """background worker を作らない bounded TTL cache と singleflight。"""

    def __init__(self, *, maximum_entries: int = 512, monotonic: MonotonicClock = time.monotonic) -> None:
        if (
            isinstance(maximum_entries, bool)
            or not isinstance(maximum_entries, int)
            or not 1 <= maximum_entries <= 10_000
        ):
            raise ValueError("maximum_entries must be between 1 and 10000")
        self.maximum_entries = maximum_entries
        self._monotonic = monotonic
        self._entries: dict[Hashable, _CacheEntry[T]] = {}
        self._inflight: dict[Hashable, asyncio.Task[T]] = {}
        self._lock = asyncio.Lock()
        self._closing = False

    async def get_or_load(self, key: Hashable, *, ttl_seconds: float, loader: Loader[T]) -> T:
        ttl = _positive_finite(ttl_seconds, "ttl_seconds")
        async with self._lock:
            if self._closing:
                raise RuntimeError("cache is closing")
            now = self._monotonic()
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > now:
                return entry.value
            if entry is not None:
                self._entries.pop(key, None)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._load_and_store(key, ttl=ttl, loader=loader),
                    name="jp-information-singleflight",
                )
                self._inflight[key] = task
        return await asyncio.shield(task)

    async def _load_and_store(self, key: Hashable, *, ttl: float, loader: Loader[T]) -> T:
        current = asyncio.current_task()
        try:
            value = await loader()
            async with self._lock:
                now = self._monotonic()
                self._prune(now)
                while len(self._entries) >= self.maximum_entries:
                    oldest = min(self._entries, key=lambda item: self._entries[item].expires_at)
                    self._entries.pop(oldest, None)
                self._entries[key] = _CacheEntry(value=value, expires_at=now + ttl)
            return value
        finally:
            async with self._lock:
                if self._inflight.get(key) is current:
                    self._inflight.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()

    async def close(self) -> None:
        """新規loadを拒否し、singleflight中のprovider処理を回収する。"""

        async with self._lock:
            self._closing = True
            self._entries.clear()
            tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _prune(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)


class ProviderGuard:
    """provider ごとの bounded concurrency/rate limit と circuit breaker。"""

    def __init__(
        self,
        name: str,
        *,
        maximum_concurrency: int = 2,
        minimum_interval_seconds: float = 0.1,
        maximum_wait_seconds: float = 2.0,
        failure_threshold: int = 3,
        recovery_seconds: float = 30.0,
        monotonic: MonotonicClock = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        normalized_name = name.strip() if isinstance(name, str) else ""
        if not normalized_name or len(normalized_name) > 80:
            raise ValueError("provider name is invalid")
        if (
            isinstance(maximum_concurrency, bool)
            or not isinstance(maximum_concurrency, int)
            or not 1 <= maximum_concurrency <= 32
        ):
            raise ValueError("maximum_concurrency must be between 1 and 32")
        if (
            isinstance(failure_threshold, bool)
            or not isinstance(failure_threshold, int)
            or not 1 <= failure_threshold <= 100
        ):
            raise ValueError("failure_threshold must be between 1 and 100")
        self.name = normalized_name
        self.minimum_interval_seconds = _non_negative_finite(
            minimum_interval_seconds,
            "minimum_interval_seconds",
        )
        self.maximum_wait_seconds = _positive_finite(maximum_wait_seconds, "maximum_wait_seconds")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = _positive_finite(recovery_seconds, "recovery_seconds")
        self._monotonic = monotonic
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(maximum_concurrency)
        self._rate_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._last_started_at: float | None = None
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_probe = False

    async def call(self, operation: Loader[T]) -> T:
        probe = await self._before_call()
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=self.maximum_wait_seconds)
                acquired = True
            except TimeoutError as exc:
                raise ProviderBusyError("provider concurrency limit wait expired") from exc
            await self._take_rate_slot()
            result = operation()
            if not inspect.isawaitable(result):
                raise TypeError("provider operation must return an awaitable")
            value = await result
        except asyncio.CancelledError:
            await self._cancel_probe(probe)
            raise
        except ProviderBusyError:
            await self._cancel_probe(probe)
            raise
        except Exception:
            await self._record_failure()
            raise
        else:
            await self._record_success()
            return value
        finally:
            if acquired:
                self._semaphore.release()

    def snapshot(self) -> ProviderSnapshot:
        return ProviderSnapshot(
            name=self.name,
            consecutive_failures=self._consecutive_failures,
            circuit_open=self._opened_at is not None,
            half_open_probe=self._half_open_probe,
        )

    async def _before_call(self) -> bool:
        async with self._state_lock:
            if self._opened_at is None:
                return False
            if self._monotonic() - self._opened_at < self.recovery_seconds:
                raise CircuitOpenError("provider circuit breaker is open")
            if self._half_open_probe:
                raise CircuitOpenError("provider circuit breaker probe is already running")
            self._half_open_probe = True
            return True

    async def _take_rate_slot(self) -> None:
        async with self._rate_lock:
            now = self._monotonic()
            wait = 0.0
            if self._last_started_at is not None:
                wait = max(0.0, self._last_started_at + self.minimum_interval_seconds - now)
            if wait > self.maximum_wait_seconds:
                raise ProviderBusyError("provider rate limit wait exceeds the configured bound")
            if wait:
                await self._sleep(wait)
            self._last_started_at = self._monotonic()

    async def _record_success(self) -> None:
        async with self._state_lock:
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_probe = False

    async def _record_failure(self) -> None:
        async with self._state_lock:
            self._consecutive_failures = min(self.failure_threshold, self._consecutive_failures + 1)
            if self._consecutive_failures >= self.failure_threshold:
                self._opened_at = self._monotonic()
            self._half_open_probe = False

    async def _cancel_probe(self, probe: bool) -> None:
        if not probe:
            return
        async with self._state_lock:
            self._half_open_probe = False


class JpInformationService:
    def __init__(
        self,
        jma_client: JmaClient,
        holiday_client: CabinetOfficeHolidayClient,
        *,
        clock: DateTimeClock = lambda: datetime.now(UTC),
        monotonic: MonotonicClock = time.monotonic,
        area_ttl_seconds: float = JMA_AREA_CACHE_TTL_SECONDS,
        forecast_ttl_seconds: float = JMA_FORECAST_CACHE_TTL_SECONDS,
        warning_ttl_seconds: float = JMA_WARNING_CACHE_TTL_SECONDS,
        holiday_ttl_seconds: float = HOLIDAY_CACHE_TTL_SECONDS,
        jma_guard: ProviderGuard | None = None,
        holiday_guard: ProviderGuard | None = None,
        cache_maximum_entries: int = 512,
    ) -> None:
        self.jma_client = jma_client
        self.holiday_client = holiday_client
        self.clock = clock
        self.area_ttl_seconds = _positive_finite(area_ttl_seconds, "area_ttl_seconds")
        self.forecast_ttl_seconds = _positive_finite(forecast_ttl_seconds, "forecast_ttl_seconds")
        self.warning_ttl_seconds = _positive_finite(warning_ttl_seconds, "warning_ttl_seconds")
        self.holiday_ttl_seconds = _positive_finite(holiday_ttl_seconds, "holiday_ttl_seconds")
        self.jma_guard = jma_guard or ProviderGuard("jma", monotonic=monotonic)
        self.holiday_guard = holiday_guard or ProviderGuard(
            "cabinet-office",
            maximum_concurrency=1,
            minimum_interval_seconds=0.25,
            monotonic=monotonic,
        )
        self._cache: AsyncTTLCache[Any] = AsyncTTLCache(
            maximum_entries=cache_maximum_entries,
            monotonic=monotonic,
        )

    async def get_region_catalog(self) -> RegionCatalog:
        async def load() -> RegionCatalog:
            payload = await self.jma_guard.call(self.jma_client.fetch_area_catalog)
            catalog = parse_region_catalog(payload, retrieved_at=self._now())
            # transport 層にも同じ公式 allowlist を反映し、URL 組み立てを二重に制限する。
            self.jma_client.set_allowed_region_codes(catalog.codes)
            return catalog

        return cast(
            RegionCatalog,
            await self._cache.get_or_load("jma:areas", ttl_seconds=self.area_ttl_seconds, loader=load),
        )

    async def get_weather(self, region_query: str) -> WeatherForecast:
        catalog = await self.get_region_catalog()
        region = catalog.resolve(region_query)

        async def load() -> WeatherForecast:
            payload = await self.jma_guard.call(lambda: self.jma_client.fetch_forecast(region.code))
            return parse_weather_forecast(
                payload,
                region=region,
                retrieved_at=self._now(),
                source_url=JMA_FORECAST_URL_TEMPLATE.format(code=region.code),
            )

        return cast(
            WeatherForecast,
            await self._cache.get_or_load(
                ("jma:forecast", region.code),
                ttl_seconds=self.forecast_ttl_seconds,
                loader=load,
            ),
        )

    async def get_warning(self, region_query: str) -> WarningReport:
        catalog = await self.get_region_catalog()
        region = catalog.resolve(region_query)

        async def load() -> WarningReport:
            payload = await self.jma_guard.call(lambda: self.jma_client.fetch_warning(region.code))
            return parse_warning_report(
                payload,
                region=region,
                retrieved_at=self._now(),
                source_url=JMA_WARNING_URL_TEMPLATE.format(code=region.code),
            )

        return cast(
            WarningReport,
            await self._cache.get_or_load(
                ("jma:warning", region.code),
                ttl_seconds=self.warning_ttl_seconds,
                loader=load,
            ),
        )

    async def get_holiday_calendar(self) -> HolidayCalendar:
        async def load() -> HolidayCalendar:
            text = await self.holiday_guard.call(self.holiday_client.fetch_holiday_csv)
            return parse_holiday_csv(
                text,
                retrieved_at=self._now(),
                source_url=CAO_HOLIDAY_CSV_URL,
            )

        return cast(
            HolidayCalendar,
            await self._cache.get_or_load("cao:holidays", ttl_seconds=self.holiday_ttl_seconds, loader=load),
        )

    async def holidays_for_year(self, year: int) -> HolidayYear:
        calendar = await self.get_holiday_calendar()
        return calendar.for_year(year)

    async def next_holiday(self, on_or_after: date | None = None) -> Holiday | None:
        day = on_or_after if on_or_after is not None else self._now().astimezone(JST).date()
        calendar = await self.get_holiday_calendar()
        return calendar.next_on_or_after(day)

    # slash command 側・親 Registry 統合側で使いやすい短い alias。
    weather = get_weather
    warning = get_warning
    holiday_year = holidays_for_year

    async def clear_cache(self) -> None:
        await self._cache.clear()

    async def begin_close(self) -> None:
        await self._cache.close()

    async def close(self) -> None:
        await self.begin_close()

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


# 短い公開名。既存 package の Service 命名にも合わせる。
InformationService = JpInformationService


def _positive_finite(value: float, label: str) -> float:
    normalized = _non_negative_finite(value, label)
    if normalized <= 0:
        raise ValueError(f"{label} must be positive")
    return normalized


def _non_negative_finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    normalized = float(value)
    if normalized < 0 or not math.isfinite(normalized):
        raise ValueError(f"{label} must be a finite non-negative number")
    return normalized
