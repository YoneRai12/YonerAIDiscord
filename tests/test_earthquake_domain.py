from __future__ import annotations

from datetime import UTC, datetime

from yonerai_discord.modules.earthquake import EventDeduplicator, EventKind, parse_event, try_parse_event
from yonerai_discord.modules.earthquake.service import ExponentialBackoff


NOW = datetime(2026, 7, 21, 12, 0, 30, tzinfo=UTC)


def quake_payload(**changes):
    payload = {
        "id": "quake-1",
        "code": 551,
        "time": "2026/07/21 21:00:01",
        "issue": {
            "source": "気象庁",
            "time": "2026/07/21 21:00:00",
            "type": "DetailScale",
            "correct": "None",
        },
        "earthquake": {
            "time": "2026/07/21 20:59:30",
            "hypocenter": {
                "name": "東京湾",
                "latitude": 35.5,
                "longitude": 139.8,
                "depth": 40,
                "magnitude": 4.8,
            },
            "maxScale": 50,
            "domesticTsunami": "None",
            "futureField": {"kept": True},
        },
        "points": [],
    }
    payload.update(changes)
    return payload


def test_code_551_is_parsed_without_rejecting_unknown_fields() -> None:
    event = parse_event(quake_payload(), received_at=NOW)
    assert event.kind is EventKind.QUAKE
    assert event.code == 551
    assert event.max_scale == 50
    assert event.scale_label == "5強"
    assert event.hypocenter_name == "東京湾"
    assert event.issue_time == datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    assert event.raw["earthquake"]["futureField"] == {"kept": True}


def test_sentinel_values_become_unknown_instead_of_failing() -> None:
    payload = quake_payload()
    payload["earthquake"]["maxScale"] = -1
    payload["earthquake"]["hypocenter"].update(
        latitude=-200,
        longitude=-200,
        depth=-1,
        magnitude=-1,
    )
    event = parse_event(payload, received_at=NOW)
    assert event.max_scale is None
    assert event.latitude is None
    assert event.longitude is None
    assert event.depth_km is None
    assert event.magnitude is None


def test_code_556_uses_its_own_shape_and_area_scale_fallback() -> None:
    payload = {
        "id": "eew-1",
        "code": 556,
        "time": "2026-07-21T12:00:01Z",
        "issue": {"source": "気象庁", "time": "2026-07-21T12:00:00Z", "type": "緊急地震速報（警報）"},
        "earthquake": {
            "hypocenter": {"name": "茨城県南部", "latitude": 36.0, "longitude": 140.0, "depth": 50},
            "magnitude": 5.2,
        },
        "areas": [{"name": "茨城県南部", "scaleFrom": 45, "scaleTo": 60}],
        "cancelled": False,
    }
    event = parse_event(payload, received_at=NOW)
    assert event.kind is EventKind.EEW
    assert event.max_scale == 60
    assert event.magnitude == 5.2


def test_unknown_code_is_preserved_but_malformed_basic_data_is_rejected() -> None:
    unknown = parse_event(
        {"id": "future-1", "code": 999, "time": "2026-07-21T12:00:00Z", "new": "field"},
        received_at=NOW,
    )
    assert unknown.kind is EventKind.UNKNOWN
    assert unknown.raw["new"] == "field"
    assert try_parse_event({"code": 551, "time": "bad"}, received_at=NOW) is None
    assert try_parse_event(["not", "an", "object"], received_at=NOW) is None


def test_dedupe_uses_both_basic_id_and_canonical_payload_hash() -> None:
    dedupe = EventDeduplicator(capacity=2)
    first = parse_event(quake_payload(), received_at=NOW)
    same = parse_event(quake_payload(), received_at=NOW)
    corrected_payload = quake_payload()
    corrected_payload["earthquake"]["maxScale"] = 55
    corrected = parse_event(corrected_payload, received_at=NOW)
    assert dedupe.seen(first) is False
    assert dedupe.seen(same) is True
    assert dedupe.seen(corrected) is False


def test_exponential_backoff_is_bounded_and_jittered() -> None:
    backoff = ExponentialBackoff(
        base_seconds=1,
        maximum_seconds=4,
        jitter_ratio=0,
        random_value=lambda: 0.5,
    )
    assert [backoff.next_delay() for _ in range(5)] == [1, 2, 4, 4, 4]
    backoff.reset()
    assert backoff.next_delay() == 1
