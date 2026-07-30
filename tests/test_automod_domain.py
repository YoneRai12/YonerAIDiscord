from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from yonerai_discord.modules.automod.detectors import (
    CrosspostDetector,
    FloodDetector,
    KeywordDetector,
    LinkDetector,
    MentionDetector,
)
from yonerai_discord.modules.automod.domain import (
    DetectionContext,
    EventKind,
    HistoricalMessage,
    MessageEvent,
    Policy,
    Severity,
)
from yonerai_discord.modules.automod.normalization import extract_domains, normalize_text

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def event(content: str = "", **overrides: object) -> MessageEvent:
    values: dict[str, object] = {
        "kind": EventKind.MESSAGE_CREATE,
        "guild_id": 1,
        "channel_id": 2,
        "message_id": 3,
        "author_id": 4,
        "content": content,
        "occurred_at": NOW,
    }
    values.update(overrides)
    return MessageEvent(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("ＡＢＣ　テスト", "abcテスト"),
        ("危\u200b険\u2060語", "危険語"),
        ("ＥＸＡＭＰＬＥ［．］ＣＯＭ", "example.com"),
        ("hxxps : // example dot com /x", "https://example.com/x"),
    ],
)
def test_normalize_text_handles_japanese_fullwidth_and_obfuscation(source: str, expected: str) -> None:
    assert normalize_text(source) == expected


def test_extract_domains_handles_obfuscated_urls() -> None:
    domains = extract_domains("hxxps://discord [.] gg/raid と example。com/path")
    assert domains == frozenset({"discord.gg", "example.com"})


def test_link_detector_honors_parent_domain_allowlist() -> None:
    detector = LinkDetector()
    policy = Policy(allowed_domains=frozenset({"example.com"}))
    assert detector.detect(event("https://docs.example.com/help"), DetectionContext(), policy) == ()
    finding = detector.detect(event("discord［．］gg/raid"), DetectionContext(), policy)[0]
    assert finding.rule == "unapproved_invite"
    assert finding.severity is Severity.HIGH


def test_mention_and_keyword_detectors_are_pure() -> None:
    target = event(
        "こ\u200bろす",
        mention_user_ids=frozenset(range(5)),
        mentions_everyone=False,
    )
    mention = MentionDetector(limit=5)
    keyword = KeywordDetector(frozenset({"ころす"}), Severity.HIGH)
    first = mention.detect(target, DetectionContext(), Policy())
    second = mention.detect(target, DetectionContext(), Policy())
    assert first == second
    assert first[0].rule == "mass_mention"
    assert keyword.detect(target, DetectionContext(), Policy())[0].rule == "blocked_keyword"


def test_flood_uses_supplied_history_without_internal_state() -> None:
    history = tuple(HistoricalMessage(2, f"m{i}", NOW - timedelta(seconds=i)) for i in range(4))
    detector = FloodDetector(message_limit=5, window_seconds=5)
    finding = detector.detect(event("new"), DetectionContext(history), Policy())
    assert finding[0].evidence["count"] == "5"
    assert detector.detect(event("new"), DetectionContext(), Policy()) == ()


def test_crosspost_counts_distinct_channels_and_supports_edit_event() -> None:
    history = (
        HistoricalMessage(10, normalize_text("同じ宣伝メッセージです"), NOW),
        HistoricalMessage(11, normalize_text("同じ宣伝メッセージです!"), NOW),
    )
    target = event(
        "同じ宣伝メッセージです",
        kind=EventKind.MESSAGE_EDIT,
        channel_id=12,
    )
    finding = CrosspostDetector(channel_limit=3, similarity=0.85).detect(
        target,
        DetectionContext(history),
        Policy(),
    )
    assert finding[0].evidence["channels"] == "3"
