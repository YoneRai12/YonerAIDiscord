from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any


JST = timezone(timedelta(hours=9))
SUPPORTED_CODES = frozenset({551, 556})
SCALE_VALUES = frozenset({10, 20, 30, 40, 45, 46, 50, 55, 60, 70})
SCALE_LABELS = {
    10: "1",
    20: "2",
    30: "3",
    40: "4",
    45: "5弱",
    46: "5弱以上未入電",
    50: "5強",
    55: "6弱",
    60: "6強",
    70: "7",
}


class EventKind(StrEnum):
    QUAKE = "quake"
    EEW = "eew"
    UNKNOWN = "unknown"


class PayloadValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EarthquakeEvent:
    id: str
    code: int
    kind: EventKind
    payload_hash: str
    basic_time: datetime | None
    issue_time: datetime | None
    received_at: datetime
    source: str
    issue_type: str | None
    correction: str | None
    cancelled: bool
    max_scale: int | None
    hypocenter_name: str | None
    latitude: float | None
    longitude: float | None
    depth_km: int | None
    magnitude: float | None
    domestic_tsunami: str | None
    raw: Mapping[str, Any]

    @property
    def scale_label(self) -> str:
        if self.max_scale is None:
            return "不明"
        return SCALE_LABELS.get(self.max_scale, str(self.max_scale))


def parse_event(payload: Mapping[str, Any], *, received_at: datetime) -> EarthquakeEvent:
    """P2PQuakeの共通項目を厳格に、コード固有項目を前方互換に解釈する。"""

    if not isinstance(payload, Mapping):
        raise PayloadValidationError("payload must be an object")
    normalized_received_at = _aware_utc(received_at)
    event_id = _required_text(payload.get("id"), "id", maximum=256)
    code = _required_integer(payload.get("code"), "code")
    basic_time_text = _required_text(payload.get("time"), "time", maximum=64)
    canonical = _canonical_payload(payload)
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    issue = _as_mapping(payload.get("issue"))
    earthquake = _as_mapping(payload.get("earthquake"))
    hypocenter = _as_mapping(earthquake.get("hypocenter"))
    kind = EventKind.QUAKE if code == 551 else EventKind.EEW if code == 556 else EventKind.UNKNOWN

    issue_time = parse_timestamp(_optional_text(issue.get("time"), maximum=64))
    basic_time = parse_timestamp(basic_time_text)
    max_scale = _optional_scale(earthquake.get("maxScale"))
    if max_scale is None and code == 556:
        max_scale = _max_area_scale(payload.get("areas"))

    magnitude_value = hypocenter.get("magnitude", earthquake.get("magnitude"))
    depth = _optional_number(hypocenter.get("depth"), sentinels={-1})
    return EarthquakeEvent(
        id=event_id,
        code=code,
        kind=kind,
        payload_hash=payload_hash,
        basic_time=basic_time,
        issue_time=issue_time,
        received_at=normalized_received_at,
        source=_optional_text(issue.get("source"), maximum=100) or "不明",
        issue_type=_optional_text(issue.get("type"), maximum=100),
        correction=_optional_text(issue.get("correct"), maximum=100),
        cancelled=payload.get("cancelled") is True,
        max_scale=max_scale,
        hypocenter_name=_optional_text(hypocenter.get("name"), maximum=200),
        latitude=_optional_float(hypocenter.get("latitude"), sentinels={-200}),
        longitude=_optional_float(hypocenter.get("longitude"), sentinels={-200}),
        depth_km=int(depth) if depth is not None and depth >= 0 else None,
        magnitude=_optional_float(magnitude_value, sentinels={-1}),
        domestic_tsunami=_optional_text(earthquake.get("domesticTsunami"), maximum=100),
        raw=json.loads(canonical),
    )


def try_parse_event(payload: object, *, received_at: datetime) -> EarthquakeEvent | None:
    try:
        if not isinstance(payload, Mapping):
            return None
        return parse_event(payload, received_at=received_at)
    except (PayloadValidationError, TypeError, ValueError, OverflowError):
        return None


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for pattern in ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(UTC)


def scale_label(value: int | None) -> str:
    return "不明" if value is None else SCALE_LABELS.get(value, str(value))


def _canonical_payload(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PayloadValidationError("payload must contain finite JSON values") from exc


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PayloadValidationError("received_at must be timezone-aware")
    return value.astimezone(UTC)


def _required_text(value: object, label: str, *, maximum: int) -> str:
    normalized = _optional_text(value, maximum=maximum)
    if normalized is None:
        raise PayloadValidationError(f"{label} must be a non-empty string")
    return normalized


def _optional_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return None
    return normalized


def _required_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PayloadValidationError(f"{label} must be an integer")
    return value


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_number(value: object, *, sentinels: set[int]) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value in sentinels:
        return None
    return value


def _optional_float(value: object, *, sentinels: set[int]) -> float | None:
    number = _optional_number(value, sentinels=sentinels)
    return None if number is None else float(number)


def _optional_scale(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value == -1 or value < 0:
        return None
    return value


def _max_area_scale(value: object) -> int | None:
    if not isinstance(value, list):
        return None
    scales = []
    for area in value:
        if not isinstance(area, Mapping):
            continue
        scale = _optional_scale(area.get("scaleTo", area.get("scale")))
        if scale is not None:
            scales.append(scale)
    return max(scales, default=None)
