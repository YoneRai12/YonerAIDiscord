from __future__ import annotations

from datetime import timedelta
from difflib import SequenceMatcher
from typing import Protocol

from .domain import Detection, DetectionContext, MessageEvent, Policy, Severity
from .normalization import domain_is_allowed, extract_domains, normalize_text


class Detector(Protocol):
    name: str

    def detect(self, event: MessageEvent, context: DetectionContext, policy: Policy) -> tuple[Detection, ...]: ...


class MentionDetector:
    name = "mention"

    def __init__(self, limit: int = 5) -> None:
        self.limit = limit

    def detect(self, event: MessageEvent, context: DetectionContext, policy: Policy) -> tuple[Detection, ...]:
        count = len(event.mention_user_ids) + len(event.mention_role_ids) + int(event.mentions_everyone)
        if count < self.limit:
            return ()
        severity = Severity.CRITICAL if event.mentions_everyone and count >= self.limit * 2 else Severity.HIGH
        return (Detection(self.name, "mass_mention", severity, 1.0, {"count": str(count)}),)


class LinkDetector:
    name = "link"
    _invite_domains = frozenset({"discord.gg", "discord.com", "discordapp.com"})

    def detect(self, event: MessageEvent, context: DetectionContext, policy: Policy) -> tuple[Detection, ...]:
        blocked = sorted(
            domain for domain in extract_domains(event.content) if not domain_is_allowed(domain, policy.allowed_domains)
        )
        if not blocked:
            return ()
        invite = any(domain_is_allowed(domain, self._invite_domains) for domain in blocked)
        return (
            Detection(
                self.name,
                "unapproved_invite" if invite else "unapproved_link",
                Severity.HIGH if invite else Severity.MEDIUM,
                1.0,
                {"domains": ",".join(blocked)},
            ),
        )


class FloodDetector:
    name = "flood"

    def __init__(self, message_limit: int = 5, window_seconds: int = 5) -> None:
        self.message_limit = message_limit
        self.window = timedelta(seconds=window_seconds)

    def detect(self, event: MessageEvent, context: DetectionContext, policy: Policy) -> tuple[Detection, ...]:
        count = 1 + sum(1 for item in context.recent_messages if event.occurred_at - item.occurred_at <= self.window)
        if count < self.message_limit:
            return ()
        return (Detection(self.name, "message_flood", Severity.HIGH, 1.0, {"count": str(count)}),)


class CrosspostDetector:
    name = "crosspost"

    def __init__(self, channel_limit: int = 3, similarity: float = 0.9, window_seconds: int = 30) -> None:
        self.channel_limit = channel_limit
        self.similarity = similarity
        self.window = timedelta(seconds=window_seconds)

    def detect(self, event: MessageEvent, context: DetectionContext, policy: Policy) -> tuple[Detection, ...]:
        content = normalize_text(event.content)
        if len(content) < 8:
            return ()
        channels = {event.channel_id}
        for item in context.recent_messages:
            if event.occurred_at - item.occurred_at > self.window:
                continue
            if SequenceMatcher(a=content, b=item.normalized_content).ratio() >= self.similarity:
                channels.add(item.channel_id)
        if len(channels) < self.channel_limit:
            return ()
        return (
            Detection(
                self.name,
                "cross_channel_repeat",
                Severity.HIGH,
                0.95,
                {"channels": str(len(channels))},
            ),
        )


class KeywordDetector:
    name = "keyword"

    def __init__(self, terms: frozenset[str], severity: Severity = Severity.MEDIUM) -> None:
        self.terms = frozenset(normalize_text(term) for term in terms)
        self.severity = severity

    def detect(self, event: MessageEvent, context: DetectionContext, policy: Policy) -> tuple[Detection, ...]:
        content = normalize_text(event.content)
        matches = sorted(term for term in self.terms if term and term in content)
        if not matches:
            return ()
        return (
            Detection(
                self.name,
                "blocked_keyword",
                self.severity,
                1.0,
                {"matches": ",".join(matches)},
            ),
        )
