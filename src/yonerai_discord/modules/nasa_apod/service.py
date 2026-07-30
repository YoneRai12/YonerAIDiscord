from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime

from .domain import ApodItem, validate_apod_request_date
from .errors import ApodDateError, ApodResponseError
from .source import ApodSource


DateClock = Callable[[], datetime]


class NasaApodService:
    """slash/natural actionから明示された1件だけをsourceへ要求する。"""

    def __init__(
        self,
        source: ApodSource,
        *,
        clock: DateClock = lambda: datetime.now(UTC),
    ) -> None:
        self.source = source
        self._clock = clock

    async def get(self, value: str | date | None = None) -> ApodItem:
        day = self.parse_date(value)
        item = await self.source.fetch(day)
        if day is not None and item.day != day:
            raise ApodResponseError("NASA APOD response date does not match the request")
        if item.day > self.today:
            raise ApodResponseError("NASA APOD response date is in the future")
        return item

    def parse_date(self, value: str | date | None) -> date | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime) or not isinstance(value, (str, date)):
            raise ApodDateError("APOD date must be an ISO calendar date")
        if isinstance(value, str):
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
                raise ApodDateError("APOD date must be YYYY-MM-DD")
            try:
                day = date.fromisoformat(value)
            except ValueError:
                raise ApodDateError("APOD date must be YYYY-MM-DD") from None
        else:
            day = value
        return validate_apod_request_date(day, today=self.today)

    @property
    def today(self) -> date:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC).date()


__all__ = ["NasaApodService"]
