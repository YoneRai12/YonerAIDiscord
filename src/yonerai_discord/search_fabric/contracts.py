"""Bounded, provider-neutral Search Fabric v1 contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from yonerai_discord.modules.web_runtime.search import WebSearchSource
from yonerai_discord.secret_detection import contains_secret_like


MAX_SEARCH_JSON_BYTES = 512 * 1024
MAX_SEARCH_EVIDENCE = 20
MAX_SEARCH_BACKENDS = 8
MAX_QUERY_BYTES = 4_096

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,119}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}\Z")
_HOST_PATH = re.compile(
    r"(?i)(?:^|[\s\"'=:(])(?:[a-z]:[\\/]|\\\\|\\device\\|file:(?:/{0,2})|\.\.[\\/]"
    r"|/(?!/)\S*)"
)


class SearchFabricContractError(ValueError):
    """Search Fabric document or value violated the fixed v1 contract."""


class SearchSourceClass(StrEnum):
    PRIMARY_OFFICIAL = "primary_official"
    PEER_REVIEWED = "peer_reviewed"
    SCHOLARLY_METADATA = "scholarly_metadata"
    REPUTABLE_SECONDARY = "reputable_secondary"
    COMMUNITY = "community"
    UNKNOWN = "unknown"


class SearchIntent(StrEnum):
    GENERAL = "general"
    NEWS = "news"
    OFFICIAL = "official"
    SCHOLARLY = "scholarly"
    CODE = "code"


class SearchFetchState(StrEnum):
    METADATA_ONLY = "metadata_only"
    FETCHED = "fetched"
    FAILED = "failed"


class SearchFreshnessState(StrEnum):
    CURRENT = "current"
    DATED = "dated"
    UNKNOWN = "unknown"


class SearchVendorFeeClass(StrEnum):
    ZERO_PER_QUERY = "zero_per_query"


class SearchCorroborationState(StrEnum):
    INDEPENDENT = "independent"
    DUPLICATE_ONLY = "duplicate_only"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SearchEngineErrorV1:
    backend_id: str
    code: str

    def __post_init__(self) -> None:
        validate_backend_id(self.backend_id)
        _require_identifier(self.code, label="engine error code")

    def to_mapping(self) -> dict[str, str]:
        return {"backend_id": self.backend_id, "code": self.code}


@dataclass(frozen=True, slots=True)
class SearchEvidenceV1:
    source: WebSearchSource
    source_class: SearchSourceClass
    fetch_state: SearchFetchState
    published: str | None
    retrieved: str
    content_hash: str | None
    corroboration: SearchCorroborationState
    verification_reasons: tuple[str, ...]
    publisher: str = ""
    freshness_state: SearchFreshnessState = SearchFreshnessState.UNKNOWN
    corroboration_group: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, WebSearchSource):
            raise TypeError("source must be a WebSearchSource")
        object.__setattr__(self, "source_class", SearchSourceClass(self.source_class))
        object.__setattr__(self, "fetch_state", SearchFetchState(self.fetch_state))
        object.__setattr__(
            self,
            "corroboration",
            SearchCorroborationState(self.corroboration),
        )
        object.__setattr__(self, "freshness_state", SearchFreshnessState(self.freshness_state))
        for value in (
            self.source.title,
            self.source.url,
            self.source.snippet,
            self.source.source_id,
            self.publisher,
        ):
            _reject_sensitive_or_host_path(value)
        if len(self.publisher) > 500:
            raise ValueError("publisher is too long")
        _require_optional_timestamp(self.published, label="published")
        _require_timestamp(self.retrieved, label="retrieved")
        if self.fetch_state is SearchFetchState.FETCHED:
            _require_digest(self.content_hash, label="content_hash")
        elif self.content_hash is not None:
            raise ValueError("unfetched evidence must not claim a content hash")
        if not isinstance(self.verification_reasons, tuple):
            raise TypeError("verification_reasons must be a tuple")
        if not 1 <= len(self.verification_reasons) <= 8:
            raise ValueError("verification_reasons count is outside the allowed range")
        if len(set(self.verification_reasons)) != len(self.verification_reasons):
            raise ValueError("verification_reasons must be unique")
        for reason in self.verification_reasons:
            _require_identifier(reason, label="verification reason")
        if self.corroboration_group is not None:
            _require_identifier(self.corroboration_group, label="corroboration_group")

    def to_mapping(self) -> dict[str, object]:
        return {
            "title": self.source.title,
            "url": self.source.url,
            "snippet": self.source.snippet,
            "source_id": self.source.source_id,
            "source_class": self.source_class.value,
            "publisher": self.publisher,
            "fetch_state": self.fetch_state.value,
            "published_at": self.published,
            "retrieved_at": self.retrieved,
            "content_hash": self.content_hash,
            "freshness_state": self.freshness_state.value,
            "corroboration_group": self.corroboration_group,
            "corroboration": self.corroboration.value,
            "verification_reasons": list(self.verification_reasons),
        }

    def to_compat_mapping(self) -> dict[str, str]:
        return {
            "title": self.source.title,
            "url": self.source.url,
            "snippet": self.source.snippet,
            "source_id": self.source.source_id,
        }


@dataclass(frozen=True, slots=True)
class SearchResultV1:
    request_id: str
    query_digest: str
    intent: SearchIntent
    language: str
    evidence: tuple[SearchEvidenceV1, ...]
    backend_ids: tuple[str, ...]
    engine_errors: tuple[SearchEngineErrorV1, ...]
    candidate_count: int
    cache_hits: int
    latency_ms: int

    def __post_init__(self) -> None:
        validate_search_binding(self.request_id, self.query_digest)
        object.__setattr__(self, "intent", SearchIntent(self.intent))
        validate_language(self.language)
        if not isinstance(self.evidence, tuple):
            raise TypeError("evidence must be a tuple")
        if not 0 <= len(self.evidence) <= MAX_SEARCH_EVIDENCE:
            raise ValueError("evidence count is outside the allowed range")
        if any(not isinstance(item, SearchEvidenceV1) for item in self.evidence):
            raise TypeError("evidence must contain SearchEvidenceV1 values")
        urls = [item.source.url for item in self.evidence]
        source_ids = [item.source.source_id for item in self.evidence if item.source.source_id]
        if len(urls) != len(set(urls)) or len(source_ids) != len(set(source_ids)):
            raise ValueError("duplicate search evidence is not allowed")
        if (
            not isinstance(self.backend_ids, tuple)
            or not 1 <= len(self.backend_ids) <= MAX_SEARCH_BACKENDS
            or len(set(self.backend_ids)) != len(self.backend_ids)
        ):
            raise ValueError("backend_ids are invalid")
        for backend_id in self.backend_ids:
            validate_backend_id(backend_id)
        if (
            not isinstance(self.engine_errors, tuple)
            or len(self.engine_errors) > len(self.backend_ids)
            or any(not isinstance(item, SearchEngineErrorV1) for item in self.engine_errors)
            or len({item.backend_id for item in self.engine_errors}) != len(self.engine_errors)
            or any(item.backend_id not in self.backend_ids for item in self.engine_errors)
        ):
            raise ValueError("engine_errors are invalid")
        if (
            isinstance(self.candidate_count, bool)
            or not isinstance(self.candidate_count, int)
            or not len(self.evidence) <= self.candidate_count <= 1_000
        ):
            raise ValueError("candidate_count is outside the allowed range")
        fetched_count = sum(item.fetch_state is SearchFetchState.FETCHED for item in self.evidence)
        if (
            isinstance(self.cache_hits, bool)
            or not isinstance(self.cache_hits, int)
            or not 0 <= self.cache_hits <= fetched_count
        ):
            raise ValueError("cache_hits is outside the allowed range")
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or not 0 <= self.latency_ms <= 120_000
        ):
            raise ValueError("latency_ms is outside the allowed range")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": "yonerai.search-result.v1",
            "request_id": self.request_id,
            "query_digest": self.query_digest,
            "intent": self.intent.value,
            "language": self.language,
            "evidence": [item.to_mapping() for item in self.evidence],
            "backend_ids": list(self.backend_ids),
            "engine_errors": [item.to_mapping() for item in self.engine_errors],
            "candidate_count": self.candidate_count,
            "cache_hits": self.cache_hits,
            "latency_ms": self.latency_ms,
        }

    def to_compat_mapping(self) -> dict[str, object]:
        """Project to the existing exact ``{"sources": [...]}`` boundary."""

        return {"sources": [item.to_compat_mapping() for item in self.evidence]}


def query_digest(query: str) -> str:
    _require_query(query)
    digest = hashlib.sha256(b"yonerai.search-query.v1\x00" + query.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_search_binding(request_id: object, digest: object) -> None:
    _require_request_id(request_id)
    _require_digest(digest, label="query_digest")


def validate_backend_id(value: object) -> None:
    _require_identifier(value, label="backend_id")


def validate_language(value: object) -> None:
    if not isinstance(value, str) or len(value) > 32 or _LANGUAGE.fullmatch(value) is None:
        raise ValueError("language is not a bounded BCP47 tag")


class SearchFabricJsonCodec:
    """Canonical request encoder and strict rich-result decoder."""

    @staticmethod
    def encode_request(
        *,
        request_id: str,
        query: str,
        intent: SearchIntent,
        language: str,
        limit: int,
    ) -> bytes:
        _require_request_id(request_id)
        _require_query(query)
        normalized_intent = SearchIntent(intent)
        validate_language(language)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_EVIDENCE:
            raise ValueError("limit is outside the allowed range")
        body = _canonical_json(
            {
                "schema": "yonerai.search-request.v1",
                "request_id": request_id,
                "query": query,
                "query_digest": query_digest(query),
                "intent": normalized_intent.value,
                "language": language,
                "limit": limit,
            }
        )
        if len(body) > MAX_SEARCH_JSON_BYTES:
            raise SearchFabricContractError("search request exceeds the JSON byte limit")
        return body

    @staticmethod
    def encode_result(result: SearchResultV1) -> bytes:
        if not isinstance(result, SearchResultV1):
            raise TypeError("result must be a SearchResultV1")
        body = _canonical_json(result.to_mapping())
        if len(body) > MAX_SEARCH_JSON_BYTES:
            raise SearchFabricContractError("search result exceeds the JSON byte limit")
        return body

    @staticmethod
    def decode_result(
        body: bytes,
        *,
        expected_request_id: str,
        expected_query_digest: str,
        expected_intent: SearchIntent,
        expected_language: str,
        limit: int,
    ) -> SearchResultV1:
        if type(body) is not bytes:
            raise TypeError("body must be bytes")
        if len(body) > MAX_SEARCH_JSON_BYTES:
            raise SearchFabricContractError("search result exceeds the JSON byte limit")
        validate_search_binding(expected_request_id, expected_query_digest)
        normalized_intent = SearchIntent(expected_intent)
        validate_language(expected_language)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_EVIDENCE:
            raise ValueError("limit is outside the allowed range")
        try:
            document = json.loads(
                body.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, SearchFabricContractError):
            raise SearchFabricContractError("search result is not strict UTF-8 JSON") from None
        if not isinstance(document, dict) or set(document) != {
            "schema",
            "request_id",
            "query_digest",
            "intent",
            "language",
            "evidence",
            "backend_ids",
            "engine_errors",
            "candidate_count",
            "cache_hits",
            "latency_ms",
        }:
            raise SearchFabricContractError("search result fields are invalid")
        if document["schema"] != "yonerai.search-result.v1":
            raise SearchFabricContractError("search result schema is invalid")
        if (
            document["request_id"] != expected_request_id
            or document["query_digest"] != expected_query_digest
            or document["intent"] != normalized_intent.value
            or document["language"] != expected_language
        ):
            raise SearchFabricContractError("search result binding is invalid")
        raw_evidence = document["evidence"]
        if not isinstance(raw_evidence, list) or len(raw_evidence) > limit:
            raise SearchFabricContractError("search evidence count is invalid")
        try:
            return SearchResultV1(
                request_id=document["request_id"],
                query_digest=document["query_digest"],
                intent=SearchIntent(document["intent"]),
                language=document["language"],
                evidence=tuple(_decode_evidence(item) for item in raw_evidence),
                backend_ids=_string_tuple(
                    document["backend_ids"],
                    label="backend_ids",
                    maximum=MAX_SEARCH_BACKENDS,
                ),
                engine_errors=_decode_engine_errors(document["engine_errors"]),
                candidate_count=document["candidate_count"],
                cache_hits=document["cache_hits"],
                latency_ms=document["latency_ms"],
            )
        except (TypeError, ValueError):
            raise SearchFabricContractError("search result values are invalid") from None


def _decode_evidence(item: object) -> SearchEvidenceV1:
    expected = {
        "title",
        "url",
        "snippet",
        "source_id",
        "source_class",
        "publisher",
        "fetch_state",
        "published_at",
        "retrieved_at",
        "content_hash",
        "freshness_state",
        "corroboration_group",
        "corroboration",
        "verification_reasons",
    }
    if not isinstance(item, dict) or set(item) != expected:
        raise SearchFabricContractError("search evidence fields are invalid")
    try:
        return SearchEvidenceV1(
            source=WebSearchSource(
                title=item["title"],
                url=item["url"],
                snippet=item["snippet"],
                source_id=item["source_id"],
            ),
            source_class=SearchSourceClass(item["source_class"]),
            fetch_state=SearchFetchState(item["fetch_state"]),
            published=item["published_at"],
            retrieved=item["retrieved_at"],
            content_hash=item["content_hash"],
            corroboration=SearchCorroborationState(item["corroboration"]),
            verification_reasons=_string_tuple(
                item["verification_reasons"],
                label="verification_reasons",
                maximum=8,
            ),
            publisher=item["publisher"],
            freshness_state=SearchFreshnessState(item["freshness_state"]),
            corroboration_group=item["corroboration_group"],
        )
    except (TypeError, ValueError):
        raise SearchFabricContractError("search evidence values are invalid") from None


def _decode_engine_errors(value: object) -> tuple[SearchEngineErrorV1, ...]:
    if not isinstance(value, list) or len(value) > MAX_SEARCH_BACKENDS:
        raise SearchFabricContractError("engine_errors must be an array")
    result: list[SearchEngineErrorV1] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"backend_id", "code"}:
            raise SearchFabricContractError("engine error fields are invalid")
        result.append(SearchEngineErrorV1(backend_id=item["backend_id"], code=item["code"]))
    return tuple(result)


def _string_tuple(value: object, *, label: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(item, str) for item in value):
        raise SearchFabricContractError(f"{label} must be a string array")
    return tuple(value)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise SearchFabricContractError("search document is not canonical JSON data") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SearchFabricContractError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise SearchFabricContractError("non-finite JSON numbers are forbidden")


def _require_query(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > MAX_QUERY_BYTES
        or any(ord(character) < 32 and character != "\t" for character in value)
    ):
        raise ValueError("query is invalid")
    _reject_sensitive_or_host_path(value)


def _reject_sensitive_or_host_path(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("search text values must be strings")
    if contains_secret_like(value) or _HOST_PATH.search(value):
        raise SearchFabricContractError("search value contains forbidden sensitive or host-path content")


def _require_identifier(value: object, *, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_request_id(value: object) -> None:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request_id is invalid")


def _require_digest(value: object, *, label: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


def _require_optional_timestamp(value: object, *, label: str) -> None:
    if value is not None:
        _require_timestamp(value, label=label)


def _require_timestamp(value: object, *, label: str) -> None:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"{label} is not a canonical UTC timestamp")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError(f"{label} is not a canonical UTC timestamp") from None


__all__ = [
    "MAX_QUERY_BYTES",
    "MAX_SEARCH_BACKENDS",
    "MAX_SEARCH_EVIDENCE",
    "MAX_SEARCH_JSON_BYTES",
    "SearchEngineErrorV1",
    "SearchEvidenceV1",
    "SearchCorroborationState",
    "SearchFabricContractError",
    "SearchFabricJsonCodec",
    "SearchFetchState",
    "SearchFreshnessState",
    "SearchIntent",
    "SearchResultV1",
    "SearchSourceClass",
    "SearchVendorFeeClass",
    "query_digest",
    "validate_backend_id",
    "validate_language",
    "validate_search_binding",
]
