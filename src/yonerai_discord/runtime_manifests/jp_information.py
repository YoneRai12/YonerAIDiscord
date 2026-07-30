from __future__ import annotations

from .types import RuntimeCapabilityDefinition, _cap


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        "cap-run-weather",
        "operations.public-information",
        "気象庁の公式公開データから地域別天気予報を表示",
        command="weather",
        plugin="jp_information",
    ),
    _cap(
        "cap-run-warning",
        "operations.public-information",
        "気象庁の公式公開データから警報・注意報を表示",
        command="warning",
        plugin="jp_information",
    ),
    _cap(
        "cap-run-holiday-next",
        "operations.public-information",
        "内閣府の公式掲載範囲から次の祝日・休日を表示",
        command="holiday next",
        plugin="jp_information",
    ),
    _cap(
        "cap-run-holiday-year",
        "operations.public-information",
        "内閣府の公式掲載範囲から指定年の祝日・休日を表示",
        command="holiday year",
        plugin="jp_information",
    ),
)
