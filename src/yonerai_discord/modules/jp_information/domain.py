from __future__ import annotations

import csv
import io
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .errors import InvalidPayloadError, PublishedRangeError, UnknownRegionError


_REGION_CODE = re.compile(r"^[0-9]{6}$")
_DATE_HEADERS = frozenset({"国民の祝日・休日月日", "国民の祝日月日", "月日"})
_NAME_HEADERS = frozenset({"国民の祝日・休日名称", "国民の祝日名称", "名称"})


@dataclass(frozen=True, slots=True)
class Region:
    code: str
    name: str
    office_name: str | None = None
    english_name: str | None = None
    parent_code: str | None = None


@dataclass(frozen=True, slots=True)
class RegionCatalog:
    regions: tuple[Region, ...]
    retrieved_at: datetime

    @property
    def codes(self) -> frozenset[str]:
        return frozenset(region.code for region in self.regions)

    def resolve(self, query: str) -> Region:
        normalized = normalize_region_query(query)
        by_code = {region.code: region for region in self.regions}
        if normalized in by_code:
            return by_code[normalized]

        folded = normalized.casefold()
        matches: list[Region] = []
        for region in self.regions:
            aliases = (region.name, region.office_name, region.english_name)
            if any(alias is not None and normalize_region_query(alias).casefold() == folded for alias in aliases):
                matches.append(region)
        unique = {region.code: region for region in matches}
        if len(unique) != 1:
            raise UnknownRegionError("region is not present uniquely in the official JMA area catalog")
        return next(iter(unique.values()))


@dataclass(frozen=True, slots=True)
class ForecastPeriod:
    starts_at: datetime
    area_name: str
    weather: str


@dataclass(frozen=True, slots=True)
class WeatherForecast:
    region: Region
    publishing_office: str
    issued_at: datetime
    retrieved_at: datetime
    headline: str | None
    periods: tuple[ForecastPeriod, ...]
    source_url: str


@dataclass(frozen=True, slots=True)
class WarningItem:
    area_name: str
    name: str
    status: str
    code: str | None = None


@dataclass(frozen=True, slots=True)
class WarningReport:
    region: Region
    publishing_office: str
    issued_at: datetime
    retrieved_at: datetime
    headline: str | None
    warnings: tuple[WarningItem, ...]
    source_url: str


@dataclass(frozen=True, slots=True, order=True)
class Holiday:
    day: date
    name: str


@dataclass(frozen=True, slots=True)
class HolidayYear:
    year: int
    holidays: tuple[Holiday, ...]
    published_from: date
    published_through: date
    retrieved_at: datetime
    source_url: str


@dataclass(frozen=True, slots=True)
class HolidayCalendar:
    holidays: tuple[Holiday, ...]
    retrieved_at: datetime
    source_url: str

    @property
    def published_from(self) -> date:
        if not self.holidays:
            raise InvalidPayloadError("official holiday calendar is empty")
        return self.holidays[0].day

    @property
    def published_through(self) -> date:
        if not self.holidays:
            raise InvalidPayloadError("official holiday calendar is empty")
        return self.holidays[-1].day

    def for_year(self, year: int) -> HolidayYear:
        if isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999:
            raise ValueError("year must be between 1 and 9999")
        if not self.published_from.year <= year <= self.published_through.year:
            raise PublishedRangeError("year is outside the official published CSV range")
        return HolidayYear(
            year=year,
            holidays=tuple(item for item in self.holidays if item.day.year == year),
            published_from=self.published_from,
            published_through=self.published_through,
            retrieved_at=self.retrieved_at,
            source_url=self.source_url,
        )

    def next_on_or_after(self, day: date) -> Holiday | None:
        if isinstance(day, datetime) or not isinstance(day, date):
            raise TypeError("day must be a date")
        if day < self.published_from or day > self.published_through:
            raise PublishedRangeError("date is outside the official published CSV range")
        return next((item for item in self.holidays if item.day >= day), None)


def normalize_region_query(value: str) -> str:
    if not isinstance(value, str):
        raise UnknownRegionError("region must be text")
    normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if (
        not normalized
        or len(normalized) > 100
        or any(unicodedata.category(char).startswith("C") for char in normalized)
    ):
        raise UnknownRegionError("region is invalid")
    return normalized


def parse_region_catalog(payload: object, *, retrieved_at: datetime) -> RegionCatalog:
    _validate_json_tree(payload)
    root = _mapping(payload, "JMA area response")
    offices = _mapping(root.get("offices"), "JMA area offices")
    parsed: list[Region] = []
    for raw_code, raw_region in offices.items():
        if not isinstance(raw_code, str) or _REGION_CODE.fullmatch(raw_code) is None:
            continue
        item = _mapping(raw_region, f"JMA office {raw_code}")
        name = _required_text(item.get("name"), "JMA office name", maximum=120)
        parent = _optional_text(item.get("parent"), maximum=16)
        if parent is not None and _REGION_CODE.fullmatch(parent) is None:
            parent = None
        parsed.append(
            Region(
                code=raw_code,
                name=name,
                office_name=_optional_text(item.get("officeName"), maximum=120),
                english_name=_optional_text(item.get("enName"), maximum=160),
                parent_code=parent,
            )
        )
    if not parsed:
        raise InvalidPayloadError("JMA area response contains no valid forecast offices")
    return RegionCatalog(
        tuple(sorted(parsed, key=lambda region: region.code)), _aware_utc(retrieved_at, "retrieved_at")
    )


def parse_weather_forecast(
    payload: object,
    *,
    region: Region,
    retrieved_at: datetime,
    source_url: str,
) -> WeatherForecast:
    _validate_json_tree(payload)
    reports = _sequence(payload, "JMA forecast response")
    report = next((item for item in reports if isinstance(item, Mapping)), None)
    if report is None:
        raise InvalidPayloadError("JMA forecast response contains no report")
    publishing_office = _required_text(report.get("publishingOffice"), "publishingOffice", maximum=160)
    issued_at = parse_jma_datetime(_required_text(report.get("reportDatetime"), "reportDatetime", maximum=64))
    headline = _optional_text(report.get("headlineText"), maximum=1_000)
    if headline is None:
        headline = _optional_text(report.get("text"), maximum=1_000)
    periods = _forecast_periods(report.get("timeSeries"))
    if not periods and headline is None:
        raise InvalidPayloadError("JMA forecast report has no usable forecast content")
    return WeatherForecast(
        region=region,
        publishing_office=publishing_office,
        issued_at=issued_at,
        retrieved_at=_aware_utc(retrieved_at, "retrieved_at"),
        headline=headline,
        periods=periods,
        source_url=source_url,
    )


def parse_warning_report(
    payload: object,
    *,
    region: Region,
    retrieved_at: datetime,
    source_url: str,
) -> WarningReport:
    _validate_json_tree(payload)
    report = _mapping(payload, "JMA warning response")
    publishing_office = _required_text(report.get("publishingOffice"), "publishingOffice", maximum=160)
    issued_at = parse_jma_datetime(_required_text(report.get("reportDatetime"), "reportDatetime", maximum=64))
    headline = _optional_text(report.get("headlineText"), maximum=1_000)
    area_types = _sequence(report.get("areaTypes"), "warning areaTypes")
    warnings: list[WarningItem] = []
    for area_type in area_types[:16]:
        if not isinstance(area_type, Mapping):
            continue
        areas = area_type.get("areas")
        if not _is_sequence(areas):
            continue
        for area in areas[:256]:
            if not isinstance(area, Mapping):
                continue
            area_name = _optional_text(area.get("name"), maximum=160)
            area_warnings = area.get("warnings")
            if area_name is None or not _is_sequence(area_warnings):
                continue
            for warning in area_warnings[:64]:
                if not isinstance(warning, Mapping):
                    continue
                name = _optional_text(warning.get("name"), maximum=160)
                status = _optional_text(warning.get("status"), maximum=80)
                if name is None or status is None or status == "解除":
                    continue
                warnings.append(
                    WarningItem(
                        area_name=area_name,
                        name=name,
                        status=status,
                        code=_optional_text(warning.get("code"), maximum=32),
                    )
                )
                if len(warnings) >= 512:
                    break
            if len(warnings) >= 512:
                break
        if len(warnings) >= 512:
            break
    return WarningReport(
        region=region,
        publishing_office=publishing_office,
        issued_at=issued_at,
        retrieved_at=_aware_utc(retrieved_at, "retrieved_at"),
        headline=headline,
        warnings=tuple(warnings),
        source_url=source_url,
    )


def parse_holiday_csv(text: str, *, retrieved_at: datetime, source_url: str) -> HolidayCalendar:
    if not isinstance(text, str) or not text or "\x00" in text:
        raise InvalidPayloadError("holiday CSV is empty or contains NUL")
    try:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(rows)
    except (csv.Error, StopIteration) as exc:
        raise InvalidPayloadError("holiday CSV header is invalid") from exc
    normalized_header = [column.strip().lstrip("\ufeff") for column in header]
    date_index = next((index for index, value in enumerate(normalized_header) if value in _DATE_HEADERS), None)
    name_index = next((index for index, value in enumerate(normalized_header) if value in _NAME_HEADERS), None)
    if date_index is None or name_index is None:
        raise InvalidPayloadError("holiday CSV required columns are missing")

    by_day: dict[date, Holiday] = {}
    try:
        for row_number, row in enumerate(rows, start=2):
            if row_number > 20_000:
                raise InvalidPayloadError("holiday CSV contains too many rows")
            if not row or all(not column.strip() for column in row):
                continue
            if max(date_index, name_index) >= len(row):
                raise InvalidPayloadError("holiday CSV row is missing a required column")
            day = _parse_holiday_date(row[date_index].strip())
            name = _required_text(row[name_index], "holiday name", maximum=160)
            existing = by_day.get(day)
            if existing is not None and existing.name != name:
                raise InvalidPayloadError("holiday CSV contains conflicting duplicate dates")
            by_day[day] = Holiday(day=day, name=name)
    except csv.Error as exc:
        raise InvalidPayloadError("holiday CSV is malformed") from exc
    if not by_day:
        raise InvalidPayloadError("holiday CSV contains no holidays")
    return HolidayCalendar(
        holidays=tuple(sorted(by_day.values())),
        retrieved_at=_aware_utc(retrieved_at, "retrieved_at"),
        source_url=source_url,
    )


def parse_jma_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidPayloadError("JMA datetime is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidPayloadError("JMA datetime must include a timezone")
    return parsed.astimezone(UTC)


def _forecast_periods(value: object) -> tuple[ForecastPeriod, ...]:
    if not _is_sequence(value):
        return ()
    periods: list[ForecastPeriod] = []
    seen: set[tuple[datetime, str, str]] = set()
    for series in value[:32]:
        if not isinstance(series, Mapping):
            continue
        raw_times = series.get("timeDefines")
        raw_areas = series.get("areas")
        if not _is_sequence(raw_times) or not _is_sequence(raw_areas):
            continue
        times: list[datetime | None] = []
        for raw_time in raw_times[:32]:
            try:
                times.append(parse_jma_datetime(raw_time) if isinstance(raw_time, str) else None)
            except InvalidPayloadError:
                times.append(None)
        for area in raw_areas[:128]:
            if not isinstance(area, Mapping):
                continue
            area_meta = area.get("area")
            area_name = _optional_text(area_meta.get("name"), maximum=160) if isinstance(area_meta, Mapping) else None
            weathers = area.get("weathers")
            if area_name is None or not _is_sequence(weathers):
                continue
            for starts_at, raw_weather in zip(times, weathers[:32], strict=False):
                weather = _optional_text(raw_weather, maximum=800)
                if starts_at is None or weather is None:
                    continue
                key = (starts_at, area_name, weather)
                if key in seen:
                    continue
                seen.add(key)
                periods.append(ForecastPeriod(starts_at=starts_at, area_name=area_name, weather=weather))
                if len(periods) >= 256:
                    return tuple(periods)
    return tuple(periods)


def _parse_holiday_date(value: str) -> date:
    for pattern in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise InvalidPayloadError("holiday date is invalid")


def _validate_json_tree(value: object) -> None:
    stack = [value]
    active_containers: set[int] = set()
    nodes = 0
    while stack:
        current = stack.pop()
        nodes += 1
        if nodes > 200_000:
            raise InvalidPayloadError("JSON payload is too complex")
        if isinstance(current, float) and not math.isfinite(current):
            raise InvalidPayloadError("JSON payload contains a non-finite number")
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in active_containers:
                raise InvalidPayloadError("JSON payload contains a cycle")
            active_containers.add(identity)
            stack.extend(current.keys())
            stack.extend(current.values())
        elif _is_sequence(current):
            identity = id(current)
            if identity in active_containers:
                raise InvalidPayloadError("JSON payload contains a cycle")
            active_containers.add(identity)
            stack.extend(current)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidPayloadError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not _is_sequence(value):
        raise InvalidPayloadError(f"{label} must be an array")
    return value


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview))


def _required_text(value: object, label: str, *, maximum: int) -> str:
    text = _optional_text(value, maximum=maximum)
    if text is None:
        raise InvalidPayloadError(f"{label} must be non-empty text")
    return text


def _optional_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > maximum or "\x00" in text:
        return None
    return text


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidPayloadError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)
