"""Search Fabric のオフライン契約doctor。

実service、network、環境変数、秘密、出力fileには触れない。既存の
Search Fabric composition、provider-neutral compatibility contract、
EvidenceFetcher、BrowserSandboxPolicyを注入transportで通し、live backendは
明示的に ``unconfigured`` として報告する。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol

from yonerai_discord.browser_sandbox.policy import (
    BrowserSandboxPolicy,
    StaticDnsResolver,
)
from yonerai_discord.modules.web_runtime.search import (
    HttpsRequest,
    HttpsResponse,
    ProviderNeutralWebSearchAdapter,
    WebSearchLimits,
    WebSearchRequest,
)
from yonerai_discord.search_fabric.composition import (
    LocalSearchFabricRuntime,
    build_local_search_fabric_runtime,
)
from yonerai_discord.search_fabric.contracts import query_digest
from yonerai_discord.search_fabric.evidence_fetcher import (
    EvidenceFetcher,
    EvidenceHttpRequest,
    EvidenceHttpResponse,
    EvidencePolicyError,
)
from yonerai_discord.search_fabric.receipts import SearchReceiptV1


_SCHEMA = "yonerai.web.doctor.v1"
_PUBLIC_ADDRESS = "93.184.216.34"
_COMPATIBILITY_HOST = "evidence.example"
_COMPATIBILITY_QUERY = "code-owned web doctor query"
_COMPATIBILITY_BODY = b"verified compatibility evidence"


class WebDoctorState(StrEnum):
    CONTRACT_READY = "contract_ready"
    FAILED = "failed"


class WebDoctorLiveState(StrEnum):
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True, slots=True)
class WebDoctorSettings:
    web_search_enabled: bool
    web_search_backend: str
    yonerai_search_gateway_mode: str
    yonerai_search_gateway_url: str
    search_allow_paid_fallback: bool
    search_timeout_seconds: float
    search_max_response_bytes: int
    search_fetch_max_bytes: int
    search_max_results: int
    search_max_fetches: int
    search_cache_ttl_seconds: int
    search_require_corroboration_for_high_stakes: bool


@dataclass(frozen=True, slots=True)
class WebDoctorReport:
    state: WebDoctorState
    live_state: WebDoctorLiveState
    checks: Mapping[str, bool]
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", WebDoctorState(self.state))
        object.__setattr__(self, "live_state", WebDoctorLiveState(self.live_state))
        if not isinstance(self.checks, Mapping) or set(self.checks) != {
            "exact_loopback_composition",
            "provider_neutral_query",
            "safe_evidence_fetch",
            "private_ip_denied_before_io",
            "paid_fallback_disabled",
            "openai_web_search_calls_zero",
        }:
            raise ValueError("doctor checks do not match the fixed contract")
        if any(type(value) is not bool for value in self.checks.values()):
            raise TypeError("doctor checks must be booleans")
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))
        succeeded = all(self.checks.values())
        if succeeded != (self.state is WebDoctorState.CONTRACT_READY):
            raise ValueError("doctor state disagrees with checks")
        if succeeded != (self.error_code is None):
            raise ValueError("doctor error code disagrees with checks")
        if self.error_code is not None and self.error_code != "contract_check_failed":
            raise ValueError("doctor error code is not fixed")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "state": self.state.value,
            "live": {
                "state": self.live_state.value,
                "ready": False,
                "probe_performed": False,
            },
            "checks": dict(sorted(self.checks.items())),
            "cost_proof": {
                "vendor_fee_class": "zero_per_query",
                "paid_fallback_used": False,
                "openai_web_search_calls": 0,
            },
            "error_code": self.error_code,
        }


class _SearchTransport(Protocol):
    async def request(self, request: HttpsRequest) -> HttpsResponse: ...


class _EvidenceTransport(Protocol):
    async def fetch(self, request: EvidenceHttpRequest) -> EvidenceHttpResponse: ...


@dataclass(slots=True)
class _InjectedSearchTransport:
    calls: list[HttpsRequest] = field(default_factory=list)

    async def request(self, request: HttpsRequest) -> HttpsResponse:
        self.calls.append(request)
        return HttpsResponse(
            status=200,
            body=json.dumps(
                {
                    "sources": [
                        {
                            "title": "Code-owned compatibility source",
                            "url": f"https://{_COMPATIBILITY_HOST}/evidence",
                            "snippet": "bounded compatibility projection",
                            "source_id": "doctor-source",
                        }
                    ]
                },
                separators=(",", ":"),
            ).encode("utf-8"),
            media_type="application/json",
        )


@dataclass(slots=True)
class _InjectedEvidenceTransport:
    calls: list[EvidenceHttpRequest] = field(default_factory=list)

    async def fetch(self, request: EvidenceHttpRequest) -> EvidenceHttpResponse:
        self.calls.append(request)
        return EvidenceHttpResponse(
            status=200,
            body=_COMPATIBILITY_BODY,
            peer_address=_PUBLIC_ADDRESS,
            content_type="text/plain; charset=utf-8",
            content_length=len(_COMPATIBILITY_BODY),
        )


def _settings(gateway_url: str) -> WebDoctorSettings:
    return WebDoctorSettings(
        web_search_enabled=True,
        web_search_backend="yonerai_search_gateway",
        yonerai_search_gateway_mode="loopback",
        yonerai_search_gateway_url=gateway_url,
        search_allow_paid_fallback=False,
        search_timeout_seconds=12.0,
        search_max_response_bytes=512 * 1024,
        search_fetch_max_bytes=2 * 1024 * 1024,
        search_max_results=10,
        search_max_fetches=5,
        search_cache_ttl_seconds=1_800,
        search_require_corroboration_for_high_stakes=True,
    )


async def run_web_doctor(
    gateway_url: str,
    *,
    search_transport: _SearchTransport | None = None,
    evidence_transport: _EvidenceTransport | None = None,
) -> WebDoctorReport:
    """既存contractだけを注入transportで検査し、networkは行わない。"""

    checks = {
        "exact_loopback_composition": False,
        "provider_neutral_query": False,
        "safe_evidence_fetch": False,
        "private_ip_denied_before_io": False,
        "paid_fallback_disabled": False,
        "openai_web_search_calls_zero": False,
    }
    query_transport = search_transport or _InjectedSearchTransport()
    fetch_transport = evidence_transport or _InjectedEvidenceTransport()
    try:
        settings = _settings(gateway_url)
        runtime = build_local_search_fabric_runtime(settings)
        checks["exact_loopback_composition"] = isinstance(runtime, LocalSearchFabricRuntime) and not runtime.ready

        adapter = ProviderNeutralWebSearchAdapter(
            backend_id="yonerai-search-gateway.compat",
            enabled=True,
            endpoint="https://gateway.example/search",
            transport=query_transport,
            limits=WebSearchLimits(max_results=1, retries=0),
        )
        search_result = await adapter.search(WebSearchRequest(query=_COMPATIBILITY_QUERY, limit=1))
        checks["provider_neutral_query"] = (
            search_result.backend_id == "yonerai-search-gateway.compat"
            and len(search_result.sources) == 1
            and search_result.sources[0].source_id == "doctor-source"
        )

        policy = BrowserSandboxPolicy(resolver=StaticDnsResolver({_COMPATIBILITY_HOST: (_PUBLIC_ADDRESS,)}))
        fetcher = EvidenceFetcher(policy=policy, transport=fetch_transport)
        fetched = await fetcher.fetch(search_result.sources[0].url)
        checks["safe_evidence_fetch"] = (
            fetched.hostname == _COMPATIBILITY_HOST
            and fetched.media_type == "text/plain"
            and fetched.text == _COMPATIBILITY_BODY.decode("utf-8")
        )

        calls_before_private_probe = _transport_call_count(fetch_transport)
        try:
            await fetcher.fetch("http://127.0.0.1/private")
        except EvidencePolicyError:
            checks["private_ip_denied_before_io"] = _transport_call_count(fetch_transport) == calls_before_private_probe

        receipt = SearchReceiptV1(
            request_id="web-doctor",
            query_digest=query_digest(_COMPATIBILITY_QUERY),
            backend_ids=("searxng.local",),
            engine_errors=(),
            candidate_count=1,
            fetched_count=1,
            cache_hits=0,
            latency_ms=0,
        )
        checks["paid_fallback_disabled"] = (
            settings.search_allow_paid_fallback is False
            and receipt.paid_fallback_used is False
            and receipt.vendor_fee_class.value == "zero_per_query"
        )
        checks["openai_web_search_calls_zero"] = (
            _transport_call_count(query_transport) == 1 and _transport_call_count(fetch_transport) == 1
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        pass

    ready = all(checks.values())
    return WebDoctorReport(
        state=WebDoctorState.CONTRACT_READY if ready else WebDoctorState.FAILED,
        live_state=WebDoctorLiveState.UNCONFIGURED,
        checks=checks,
        error_code=None if ready else "contract_check_failed",
    )


def _transport_call_count(transport: object) -> int:
    calls = getattr(transport, "calls", None)
    return len(calls) if isinstance(calls, list) else -1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yonerai-discord-web-doctor", description=__doc__)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8787")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = asyncio.run(run_web_doctor(args.gateway_url))
    except KeyboardInterrupt:
        raise
    print(
        json.dumps(
            report.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if report.state is WebDoctorState.CONTRACT_READY else 2


if __name__ == "__main__":
    raise SystemExit(main())
