"""Content-free Search Fabric execution receipts."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from .contracts import (
    MAX_SEARCH_BACKENDS,
    MAX_SEARCH_EVIDENCE,
    SearchEngineErrorV1,
    SearchFetchState,
    SearchResultV1,
    SearchVendorFeeClass,
    validate_backend_id,
    validate_search_binding,
)


_PUBLIC_QUERY_DIGEST_KEY = secrets.token_bytes(32)
_OPAQUE_REQUEST_ID_KEY = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True)
class SearchReceiptV1:
    request_id: str
    query_digest: str
    backend_ids: tuple[str, ...]
    engine_errors: tuple[SearchEngineErrorV1, ...]
    candidate_count: int
    fetched_count: int
    cache_hits: int
    latency_ms: int
    vendor_fee_class: SearchVendorFeeClass = SearchVendorFeeClass.ZERO_PER_QUERY
    paid_fallback_used: bool = False

    @classmethod
    def from_result(cls, result: SearchResultV1) -> SearchReceiptV1:
        if not isinstance(result, SearchResultV1):
            raise TypeError("result must be a SearchResultV1")
        return cls(
            request_id=result.request_id,
            query_digest=result.query_digest,
            backend_ids=result.backend_ids,
            engine_errors=result.engine_errors,
            candidate_count=result.candidate_count,
            fetched_count=sum(item.fetch_state is SearchFetchState.FETCHED for item in result.evidence),
            cache_hits=result.cache_hits,
            latency_ms=result.latency_ms,
        )

    def __post_init__(self) -> None:
        validate_search_binding(self.request_id, self.query_digest)
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
        for label, value, maximum in (
            ("candidate_count", self.candidate_count, 1_000),
            ("fetched_count", self.fetched_count, MAX_SEARCH_EVIDENCE),
            ("cache_hits", self.cache_hits, MAX_SEARCH_EVIDENCE),
            ("latency_ms", self.latency_ms, 120_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{label} is outside the allowed range")
        if self.fetched_count > self.candidate_count or self.cache_hits > self.fetched_count:
            raise ValueError("receipt counts are inconsistent")
        object.__setattr__(self, "vendor_fee_class", SearchVendorFeeClass(self.vendor_fee_class))
        if self.vendor_fee_class is not SearchVendorFeeClass.ZERO_PER_QUERY:
            raise ValueError("vendor_fee_class is fixed zero_per_query")
        if type(self.paid_fallback_used) is not bool or self.paid_fallback_used:
            raise ValueError("paid_fallback_used is fixed false")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": "yonerai.search-receipt.v1",
            "request_id": self.request_id,
            "query_digest": _public_query_digest(self.query_digest),
            "backend_ids": list(self.backend_ids),
            "engine_errors": [item.to_mapping() for item in self.engine_errors],
            "candidate_count": self.candidate_count,
            "fetched_count": self.fetched_count,
            "cache_hits": self.cache_hits,
            "latency_ms": self.latency_ms,
            "vendor_fee_class": self.vendor_fee_class.value,
            "paid_fallback_used": self.paid_fallback_used,
        }


@dataclass(frozen=True, slots=True)
class SearchGatewayOutcome:
    result: SearchResultV1
    receipt: SearchReceiptV1

    def __post_init__(self) -> None:
        if not isinstance(self.result, SearchResultV1):
            raise TypeError("result must be a SearchResultV1")
        if not isinstance(self.receipt, SearchReceiptV1):
            raise TypeError("receipt must be a SearchReceiptV1")
        if self.receipt != SearchReceiptV1.from_result(self.result):
            raise ValueError("receipt does not match result")

    def to_mapping(self) -> dict[str, object]:
        """Project the exact reader-facing ``yonerai.search.evidence.v1`` envelope."""

        receipt = self.receipt.to_mapping()
        receipt.pop("schema")
        receipt.pop("request_id")
        receipt.pop("query_digest")
        return {
            "schema": "yonerai.search.evidence.v1",
            "query_digest": _public_query_digest(self.result.query_digest),
            "intent": self.result.intent.value,
            "language": self.result.language,
            "results": [item.to_mapping() for item in self.result.evidence],
            "receipt": receipt,
        }


def _public_query_digest(binding_digest: str) -> str:
    """Project a process-scoped keyed digest; the fixed wire binding stays internal."""

    value = hmac.new(
        _PUBLIC_QUERY_DIGEST_KEY,
        binding_digest.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{value}"


def opaque_search_request_id(local_binding: str) -> str:
    """Hide Discord/local identifiers behind a process-scoped stable wire ID."""

    if (
        not isinstance(local_binding, str)
        or not local_binding
        or len(local_binding) > 256
        or any(ord(character) < 32 for character in local_binding)
    ):
        raise ValueError("local search binding is invalid")
    value = hmac.new(
        _OPAQUE_REQUEST_ID_KEY,
        local_binding.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"search-{value}"


__all__ = ["SearchGatewayOutcome", "SearchReceiptV1", "opaque_search_request_id"]
