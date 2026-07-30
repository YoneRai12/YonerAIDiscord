from __future__ import annotations

from datetime import UTC, datetime

import pytest

from yonerai_discord.modules.utility import (
    color_from_hex,
    discord_timestamp,
    parse_choices,
    parse_dice,
    sha256_text,
    snowflake_created_at,
)


def test_parse_dice() -> None:
    parsed = parse_dice("2d6+3")
    assert (parsed.count, parsed.sides, parsed.modifier) == (2, 6, 3)


@pytest.mark.parametrize("value", ["0d6", "51d6", "2d1", "hello", "2d6+999999", None])
def test_invalid_dice_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        parse_dice(value)


def test_choices_are_trimmed() -> None:
    assert parse_choices(" 赤, 青 ,緑 ") == ("赤", "青", "緑")
    with pytest.raises(ValueError):
        parse_choices("赤, 赤")
    with pytest.raises(ValueError):
        parse_choices(None)  # type: ignore[arg-type]


def test_timestamp_requires_offset() -> None:
    assert discord_timestamp("2026-07-21T20:00+09:00", "F") == "<t:1784631600:F>"
    with pytest.raises(ValueError):
        discord_timestamp("2026-07-21T20:00", "F")


def test_snowflake_epoch() -> None:
    assert snowflake_created_at(1).replace(microsecond=0) == datetime(2015, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        snowflake_created_at(True)
    with pytest.raises(ValueError):
        snowflake_created_at(2**64)


def test_color_and_hash() -> None:
    assert color_from_hex("#5865F2") == 0x5865F2
    assert sha256_text("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
