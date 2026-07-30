from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from yonerai_discord.modules.jp_information import (
    InvalidPayloadError,
    PublishedRangeError,
    UnknownRegionError,
    parse_holiday_csv,
    parse_region_catalog,
    parse_warning_report,
    parse_weather_forecast,
)


NOW = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)


def area_payload():
    return {
        "offices": {
            "130000": {
                "name": "東京都",
                "enName": "Tokyo",
                "officeName": "気象庁",
                "parent": "010100",
                "futureField": {"ignored": True},
            },
            "140000": {"name": "神奈川県", "officeName": "横浜地方気象台"},
        },
        "new2026Section": {"ignored": True},
    }


def test_region_catalog_is_exact_allowlist_for_code_and_name() -> None:
    catalog = parse_region_catalog(area_payload(), retrieved_at=NOW)
    assert catalog.resolve("130000").name == "東京都"
    assert catalog.resolve(" 東京都 ").code == "130000"
    assert catalog.resolve("Tokyo").code == "130000"
    with pytest.raises(UnknownRegionError):
        catalog.resolve("東京")
    with pytest.raises(UnknownRegionError):
        catalog.resolve("https://example.invalid")


def test_forecast_ignores_unknown_fields_but_requires_minimum_schema() -> None:
    region = parse_region_catalog(area_payload(), retrieved_at=NOW).resolve("東京都")
    payload = [
        {
            "publishingOffice": "気象庁",
            "reportDatetime": "2026-07-21T11:00:00+09:00",
            "headlineText": "晴れの見込み",
            "timeSeries": [
                {
                    "timeDefines": ["2026-07-21T12:00:00+09:00"],
                    "areas": [{"area": {"name": "東京地方"}, "weathers": ["晴れ"]}],
                }
            ],
            "unknown": [1, 2, 3],
        }
    ]
    result = parse_weather_forecast(payload, region=region, retrieved_at=NOW, source_url="https://fixed.invalid")
    assert result.periods[0].weather == "晴れ"
    assert result.issued_at.tzinfo is UTC

    payload[0].pop("reportDatetime")
    with pytest.raises(InvalidPayloadError):
        parse_weather_forecast(payload, region=region, retrieved_at=NOW, source_url="https://fixed.invalid")


def test_warning_keeps_active_items_and_drops_releases() -> None:
    region = parse_region_catalog(area_payload(), retrieved_at=NOW).resolve("130000")
    result = parse_warning_report(
        {
            "publishingOffice": "気象庁",
            "reportDatetime": "2026-07-21T12:00:00+09:00",
            "areaTypes": [
                {
                    "areas": [
                        {
                            "name": "東京都",
                            "warnings": [
                                {"name": "大雨警報", "status": "発表", "code": "03"},
                                {"name": "強風注意報", "status": "解除", "code": "15"},
                            ],
                        }
                    ]
                }
            ],
            "unknown": "ignored",
        },
        region=region,
        retrieved_at=NOW,
        source_url="https://fixed.invalid",
    )
    assert [(item.name, item.status) for item in result.warnings] == [("大雨警報", "発表")]


def test_holidays_are_only_from_official_csv_range() -> None:
    calendar = parse_holiday_csv(
        "国民の祝日・休日月日,国民の祝日・休日名称\n2026/1/1,元日\n2026/2/11,建国記念の日\n",
        retrieved_at=NOW,
        source_url="https://fixed.invalid",
    )
    assert calendar.next_on_or_after(date(2026, 1, 2)).name == "建国記念の日"
    assert [item.name for item in calendar.for_year(2026).holidays] == ["元日", "建国記念の日"]
    with pytest.raises(PublishedRangeError):
        calendar.for_year(2027)
    with pytest.raises(PublishedRangeError):
        calendar.next_on_or_after(date(2027, 1, 1))


def test_holiday_csv_rejects_missing_required_columns() -> None:
    with pytest.raises(InvalidPayloadError):
        parse_holiday_csv("date,name\n2026/1/1,元日\n", retrieved_at=NOW, source_url="https://fixed.invalid")
