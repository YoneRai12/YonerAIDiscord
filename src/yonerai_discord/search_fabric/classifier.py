from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class SourceClass(StrEnum):
    PRIMARY_OFFICIAL = "primary_official"
    PEER_REVIEWED = "peer_reviewed"
    SCHOLARLY_METADATA = "scholarly_metadata"
    REPUTABLE_SECONDARY = "reputable_secondary"
    COMMUNITY = "community"
    UNKNOWN = "unknown"


class FetchState(StrEnum):
    FETCHED = "fetched"
    METADATA_ONLY = "metadata_only"
    FAILED = "failed"


class FreshnessState(StrEnum):
    CURRENT = "current"
    DATED = "dated"
    UNKNOWN = "unknown"


class CorroborationState(StrEnum):
    INDEPENDENT = "independent"
    DUPLICATE_ONLY = "duplicate_only"
    NONE = "none"
    UNKNOWN = "unknown"


class VerificationReason(StrEnum):
    OFFICIAL_DOMAIN_RULE = "official_domain_rule"
    PEER_REVIEWED_DOMAIN_RULE = "peer_reviewed_domain_rule"
    SCHOLARLY_METADATA_DOMAIN_RULE = "scholarly_metadata_domain_rule"
    REPUTABLE_SECONDARY_DOMAIN_RULE = "reputable_secondary_domain_rule"
    COMMUNITY_DOMAIN_RULE = "community_domain_rule"
    UNCLASSIFIED_DOMAIN = "unclassified_domain"
    DIRECT_FETCH_VERIFIED = "direct_fetch_verified"
    CONTENT_HASH_VERIFIED = "content_hash_verified"
    TRANSPORT_AUTHENTICATED = "transport_authenticated"
    TRANSPORT_UNAUTHENTICATED = "transport_unauthenticated"
    FETCH_NOT_PERFORMED = "fetch_not_performed"
    FETCH_FAILED = "fetch_failed"
    FRESHNESS_CONFIRMED = "freshness_confirmed"
    CONTENT_STALE = "content_stale"
    FRESHNESS_UNKNOWN = "freshness_unknown"
    INDEPENDENT_CORROBORATION = "independent_corroboration"
    DUPLICATE_ONLY = "duplicate_only"
    NO_CORROBORATION = "no_corroboration"
    CORROBORATION_UNKNOWN = "corroboration_unknown"


_CLASS_REASON = {
    SourceClass.PRIMARY_OFFICIAL: VerificationReason.OFFICIAL_DOMAIN_RULE,
    SourceClass.PEER_REVIEWED: VerificationReason.PEER_REVIEWED_DOMAIN_RULE,
    SourceClass.SCHOLARLY_METADATA: VerificationReason.SCHOLARLY_METADATA_DOMAIN_RULE,
    SourceClass.REPUTABLE_SECONDARY: VerificationReason.REPUTABLE_SECONDARY_DOMAIN_RULE,
    SourceClass.COMMUNITY: VerificationReason.COMMUNITY_DOMAIN_RULE,
    SourceClass.UNKNOWN: VerificationReason.UNCLASSIFIED_DOMAIN,
}


@dataclass(frozen=True, slots=True)
class SourceClassRule:
    domain: str
    source_class: SourceClass

    def __post_init__(self) -> None:
        if not isinstance(self.source_class, SourceClass) or self.source_class is SourceClass.UNKNOWN:
            raise ValueError("source_class rule must be a known class")
        wildcard = self.domain.startswith("*.")
        raw_domain = self.domain[2:] if wildcard else self.domain
        normalized = _normalize_hostname(raw_domain)
        object.__setattr__(self, "domain", f"*.{normalized}" if wildcard else normalized)


@dataclass(frozen=True, slots=True)
class SourceEvidenceFacts:
    hostname: str
    fetch_state: FetchState
    transport_authenticated: bool = False
    fetched_at_epoch_seconds: float | None = None
    published_at_epoch_seconds: float | None = None
    content_hash: str | None = None
    corroboration: CorroborationState = CorroborationState.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "hostname", _normalize_hostname(self.hostname))
        if not isinstance(self.fetch_state, FetchState):
            raise TypeError("fetch_state must be FetchState")
        if type(self.transport_authenticated) is not bool:
            raise TypeError("transport_authenticated must be a boolean")
        if not isinstance(self.corroboration, CorroborationState):
            raise TypeError("corroboration must be CorroborationState")
        if self.fetch_state is FetchState.FETCHED:
            if (
                isinstance(self.fetched_at_epoch_seconds, bool)
                or not isinstance(self.fetched_at_epoch_seconds, (int, float))
                or not math.isfinite(float(self.fetched_at_epoch_seconds))
                or float(self.fetched_at_epoch_seconds) < 0
            ):
                raise ValueError("direct fetch requires a valid fetch timestamp")
            if not isinstance(self.content_hash, str) or not _SHA256.fullmatch(self.content_hash):
                raise ValueError("direct fetch requires a prefixed lowercase SHA-256 content hash")
        elif self.fetched_at_epoch_seconds is not None or self.content_hash is not None:
            raise ValueError("unfetched evidence cannot claim a timestamp or content hash")
        if self.published_at_epoch_seconds is not None and (
            isinstance(self.published_at_epoch_seconds, bool)
            or not isinstance(self.published_at_epoch_seconds, (int, float))
            or not math.isfinite(float(self.published_at_epoch_seconds))
            or float(self.published_at_epoch_seconds) < 0
        ):
            raise ValueError("publication timestamp is invalid")


@dataclass(frozen=True, slots=True)
class SourceAssessment:
    source_class: SourceClass
    fetch_state: FetchState
    freshness_state: FreshnessState
    content_hash: str | None
    corroboration: CorroborationState
    verification_reasons: tuple[VerificationReason, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "source_class": self.source_class.value,
            "fetch_state": self.fetch_state.value,
            "freshness_state": self.freshness_state.value,
            "content_hash": self.content_hash,
            "corroboration": self.corroboration.value,
            "verification_reasons": [reason.value for reason in self.verification_reasons],
        }


class SourceClassifier:
    """Code-owned domain rules plus direct-fetch facts; search rank is never an input."""

    def __init__(
        self,
        *,
        rules: tuple[SourceClassRule, ...] = (),
        fresh_for_seconds: float = 24 * 60 * 60,
    ) -> None:
        if not isinstance(rules, tuple) or any(not isinstance(rule, SourceClassRule) for rule in rules):
            raise TypeError("rules must be a tuple of SourceClassRule")
        if len(rules) > 256 or len({rule.domain for rule in rules}) != len(rules):
            raise ValueError("source classification rules are duplicated or too numerous")
        if (
            isinstance(fresh_for_seconds, bool)
            or not isinstance(fresh_for_seconds, (int, float))
            or not math.isfinite(float(fresh_for_seconds))
            or not 1 <= float(fresh_for_seconds) <= 31 * 24 * 60 * 60
        ):
            raise ValueError("fresh_for_seconds is outside the allowed range")
        self._rules = rules
        self._fresh_for_seconds = float(fresh_for_seconds)

    def assess(
        self,
        facts: SourceEvidenceFacts,
        *,
        now_epoch_seconds: float,
    ) -> SourceAssessment:
        if not isinstance(facts, SourceEvidenceFacts):
            raise TypeError("facts must be SourceEvidenceFacts")
        if (
            isinstance(now_epoch_seconds, bool)
            or not isinstance(now_epoch_seconds, (int, float))
            or not math.isfinite(float(now_epoch_seconds))
            or float(now_epoch_seconds) < 0
        ):
            raise ValueError("now_epoch_seconds must be a finite non-negative number")

        source_class = self._source_class_for(facts.hostname)
        if facts.fetch_state is FetchState.FETCHED and facts.transport_authenticated is not True:
            source_class = SourceClass.UNKNOWN
        reasons: list[VerificationReason] = [_CLASS_REASON[source_class]]
        freshness = FreshnessState.UNKNOWN
        if facts.fetch_state is FetchState.FETCHED:
            assert facts.fetched_at_epoch_seconds is not None
            assert facts.content_hash is not None
            if facts.fetched_at_epoch_seconds > float(now_epoch_seconds) + 300:
                raise ValueError("fetch timestamp is implausibly in the future")
            reasons.extend((VerificationReason.DIRECT_FETCH_VERIFIED, VerificationReason.CONTENT_HASH_VERIFIED))
            reasons.append(
                VerificationReason.TRANSPORT_AUTHENTICATED
                if facts.transport_authenticated
                else VerificationReason.TRANSPORT_UNAUTHENTICATED
            )
            if facts.published_at_epoch_seconds is None:
                reasons.append(VerificationReason.FRESHNESS_UNKNOWN)
            else:
                if facts.published_at_epoch_seconds > float(now_epoch_seconds) + 300:
                    raise ValueError("publication timestamp is implausibly in the future")
                freshness = (
                    FreshnessState.CURRENT
                    if float(now_epoch_seconds) - facts.published_at_epoch_seconds <= self._fresh_for_seconds
                    else FreshnessState.DATED
                )
                reasons.append(
                    VerificationReason.FRESHNESS_CONFIRMED
                    if freshness is FreshnessState.CURRENT
                    else VerificationReason.CONTENT_STALE
                )
        elif facts.fetch_state is FetchState.FAILED:
            reasons.extend((VerificationReason.FETCH_FAILED, VerificationReason.FRESHNESS_UNKNOWN))
        else:
            reasons.extend((VerificationReason.FETCH_NOT_PERFORMED, VerificationReason.FRESHNESS_UNKNOWN))

        reasons.append(
            {
                CorroborationState.INDEPENDENT: VerificationReason.INDEPENDENT_CORROBORATION,
                CorroborationState.DUPLICATE_ONLY: VerificationReason.DUPLICATE_ONLY,
                CorroborationState.NONE: VerificationReason.NO_CORROBORATION,
                CorroborationState.UNKNOWN: VerificationReason.CORROBORATION_UNKNOWN,
            }[facts.corroboration]
        )
        return SourceAssessment(
            source_class=source_class,
            fetch_state=facts.fetch_state,
            freshness_state=freshness,
            content_hash=facts.content_hash,
            corroboration=facts.corroboration,
            verification_reasons=tuple(reasons),
        )

    def _source_class_for(self, hostname: str) -> SourceClass:
        exact = next((rule for rule in self._rules if rule.domain == hostname), None)
        if exact is not None:
            return exact.source_class
        wildcard = next(
            (
                rule
                for rule in self._rules
                if rule.domain.startswith("*.")
                and hostname != rule.domain[2:]
                and hostname.endswith(f".{rule.domain[2:]}")
            ),
            None,
        )
        return wildcard.source_class if wildcard is not None else SourceClass.UNKNOWN


def _normalize_hostname(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("hostname must be a string")
    normalized = unicodedata.normalize("NFC", value).strip().lower().removesuffix(".")
    if not normalized or len(normalized) > 253 or normalized.startswith(".") or ".." in normalized:
        raise ValueError("hostname is malformed")
    try:
        ascii_hostname = normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("hostname is malformed") from exc
    if any(not _DNS_LABEL.fullmatch(label) for label in ascii_hostname.split(".")):
        raise ValueError("hostname is malformed")
    return ascii_hostname
