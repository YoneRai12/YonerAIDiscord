from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import re


_DICE = re.compile(r"^(?P<count>\d{1,2})d(?P<sides>\d{1,5})(?P<modifier>[+-]\d{1,5})?$", re.IGNORECASE)
_TIMESTAMP_STYLES = frozenset({"t", "T", "d", "D", "f", "F", "R"})
_MAX_DISCORD_SNOWFLAKE = 2**64 - 1


@dataclass(frozen=True, slots=True)
class DiceExpression:
    count: int
    sides: int
    modifier: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.count <= 50:
            raise ValueError("dice count must be between 1 and 50")
        if not 2 <= self.sides <= 100_000:
            raise ValueError("dice sides must be between 2 and 100000")
        if not -100_000 <= self.modifier <= 100_000:
            raise ValueError("dice modifier is too large")


def parse_dice(value: str) -> DiceExpression:
    if not isinstance(value, str):
        raise ValueError("dice must be text")
    matched = _DICE.fullmatch(value.strip())
    if matched is None:
        raise ValueError("dice must look like 2d6+1")
    return DiceExpression(
        count=int(matched.group("count")),
        sides=int(matched.group("sides")),
        modifier=int(matched.group("modifier") or 0),
    )


def parse_choices(value: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError("choices must be text")
    choices = tuple(item.strip() for item in value.split(",") if item.strip())
    if not 2 <= len(choices) <= 20:
        raise ValueError("provide between 2 and 20 choices")
    if any(len(item) > 100 for item in choices):
        raise ValueError("choice is too long")
    if len(set(choices)) != len(choices):
        raise ValueError("choices must be unique")
    return choices


def parse_aware_datetime(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("datetime must be text")
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone offset is required")
    return parsed.astimezone(UTC)


def discord_timestamp(value: str, style: str = "F") -> str:
    if not isinstance(style, str) or style not in _TIMESTAMP_STYLES:
        raise ValueError("invalid timestamp style")
    parsed = parse_aware_datetime(value)
    return f"<t:{int(parsed.timestamp())}:{style}>"


def snowflake_created_at(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > _MAX_DISCORD_SNOWFLAKE:
        raise ValueError("invalid snowflake")
    milliseconds = (value >> 22) + 1_420_070_400_000
    return datetime.fromtimestamp(milliseconds / 1_000, UTC)


def color_from_hex(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError("color must be text")
    normalized = value.strip().removeprefix("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", normalized):
        raise ValueError("color must be six hex digits")
    return int(normalized, 16)


def sha256_text(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_000:
        raise ValueError("text length must be between 1 and 4000")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
