from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from yonerai_discord.modules.jp_information import JpInformationService, ProviderGuard, UnknownRegionError


NOW = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)


class FakeJmaClient:
    def __init__(self) -> None:
        self.area_calls = 0
        self.forecast_calls = 0
        self.warning_calls = 0
        self.allowed = frozenset()
        self.forecast_entered = asyncio.Event()
        self.forecast_release = asyncio.Event()

    def set_allowed_region_codes(self, codes) -> None:
        self.allowed = frozenset(codes)

    async def fetch_area_catalog(self):
        self.area_calls += 1
        return {"offices": {"130000": {"name": "東京都", "officeName": "気象庁"}}}

    async def fetch_forecast(self, code):
        assert code in self.allowed
        self.forecast_calls += 1
        self.forecast_entered.set()
        await self.forecast_release.wait()
        return [
            {
                "publishingOffice": "気象庁",
                "reportDatetime": "2026-07-21T11:00:00+09:00",
                "headlineText": "晴れ",
            }
        ]

    async def fetch_warning(self, code):
        assert code in self.allowed
        self.warning_calls += 1
        return {
            "publishingOffice": "気象庁",
            "reportDatetime": "2026-07-21T11:00:00+09:00",
            "areaTypes": [],
        }


class FakeHolidayClient:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch_holiday_csv(self):
        self.calls += 1
        return "国民の祝日・休日月日,国民の祝日・休日名称\n2026/1/1,元日\n2026/11/3,文化の日\n"


def service(jma=None, holiday=None):
    return JpInformationService(
        jma or FakeJmaClient(),  # type: ignore[arg-type]
        holiday or FakeHolidayClient(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        jma_guard=ProviderGuard("test-jma", minimum_interval_seconds=0),
        holiday_guard=ProviderGuard("test-cao", minimum_interval_seconds=0),
    )


@pytest.mark.asyncio
async def test_weather_cache_is_singleflight_and_reused() -> None:
    client = FakeJmaClient()
    value = service(jma=client)
    first = asyncio.create_task(value.get_weather("東京都"))
    second = asyncio.create_task(value.get_weather("130000"))
    await client.forecast_entered.wait()
    assert client.area_calls == 1
    assert client.forecast_calls == 1
    client.forecast_release.set()
    results = await asyncio.gather(first, second)
    assert results[0] is results[1]
    assert await value.get_weather("東京都") is results[0]
    assert client.forecast_calls == 1


@pytest.mark.asyncio
async def test_unknown_region_never_reaches_forecast_endpoint() -> None:
    client = FakeJmaClient()
    value = service(jma=client)
    with pytest.raises(UnknownRegionError):
        await value.get_weather("攻撃者指定URL https://example.invalid")
    assert client.area_calls == 1
    assert client.forecast_calls == 0


@pytest.mark.asyncio
async def test_warning_and_holiday_results_are_cached() -> None:
    jma = FakeJmaClient()
    holiday = FakeHolidayClient()
    value = service(jma=jma, holiday=holiday)
    await value.get_warning("東京都")
    await value.get_warning("130000")
    assert jma.warning_calls == 1
    assert (await value.holidays_for_year(2026)).holidays[0].name == "元日"
    assert (await value.holidays_for_year(2026)).holidays[-1].name == "文化の日"
    assert holiday.calls == 1


@pytest.mark.asyncio
async def test_begin_close_cancels_inflight_load_and_rejects_new_work() -> None:
    client = FakeJmaClient()
    value = service(jma=client)
    task = asyncio.create_task(value.get_weather("東京都"))
    await client.forecast_entered.wait()
    await value.begin_close()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(RuntimeError, match="closing"):
        await value.get_weather("東京都")
