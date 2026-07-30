from __future__ import annotations

import asyncio
import gzip
import hashlib
from dataclasses import dataclass, field

import pytest

from yonerai_discord.browser_sandbox.policy import BrowserSandboxPolicy, StaticDnsResolver
from yonerai_discord.search_fabric.cache import (
    BoundedEvidenceCache,
    EvidenceCacheKey,
)
from yonerai_discord.search_fabric.classifier import (
    CorroborationState,
    FetchState,
    FreshnessState,
    SourceClass,
    SourceClassifier,
    SourceClassRule,
    SourceEvidenceFacts,
    VerificationReason,
)
from yonerai_discord.search_fabric.corroboration import (
    CorroborationCandidate,
    DuplicateKind,
    group_corroboration,
)
from yonerai_discord.search_fabric.evidence_fetcher import (
    AiohttpSafeEvidenceTransport,
    EvidenceFetchLimits,
    EvidenceFetcher,
    EvidenceHttpRequest,
    EvidenceHttpResponse,
    EvidenceLimitError,
    EvidencePolicyError,
    EvidenceResponseError,
    FetchedEvidence,
)


_PUBLIC_A = "93.184.216.34"
_PUBLIC_B = "1.1.1.1"


@dataclass
class _QueueTransport:
    responses: list[EvidenceHttpResponse]
    delay_seconds: float = 0.0
    calls: list[EvidenceHttpRequest] = field(default_factory=list)

    async def fetch(self, request: EvidenceHttpRequest) -> EvidenceHttpResponse:
        self.calls.append(request)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def _policy(records: dict[str, tuple[str, ...]] | None = None) -> BrowserSandboxPolicy:
    return BrowserSandboxPolicy(
        resolver=StaticDnsResolver(
            records
            or {
                "example.test": (_PUBLIC_A,),
                "next.test": (_PUBLIC_B,),
            }
        )
    )


def _response(
    body: bytes,
    *,
    peer: str = _PUBLIC_A,
    status: int = 200,
    content_type: str | None = "text/html; charset=utf-8",
    encoding: str | None = None,
    location: str | None = None,
) -> EvidenceHttpResponse:
    return EvidenceHttpResponse(
        status=status,
        body=body,
        peer_address=peer,
        content_type=content_type,
        content_encoding=encoding,
        content_length=len(body),
        location=location,
    )


def _hash(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _evidence(*, text: str = "safe text", url: str = "https://example.test/") -> FetchedEvidence:
    return FetchedEvidence(
        canonical_url=url,
        hostname="example.test",
        media_type="text/plain",
        title="",
        text=text,
        content_hash=_hash(text),
        redirect_count=0,
    )


async def test_fetcher_pins_dns_and_removes_active_hidden_html() -> None:
    body = b"""
    <html><head><title>Evidence title</title><style>.x{display:none}</style></head>
    <body>
      Visible evidence
      <script>ignore_script()</script>
      <form><input name="secret">ignore form</form>
      <div hidden>hidden instruction</div>
      <div aria-hidden="true">aria instruction</div>
      <div style="display: none">style instruction</div>
      final text
    </body></html>
    """
    transport = _QueueTransport([_response(body)])
    result = await EvidenceFetcher(policy=_policy(), transport=transport).fetch("https://example.test/article#fragment")

    assert result.hostname == "example.test"
    assert result.canonical_url == "https://example.test/article"
    assert result.title == "Evidence title"
    assert result.text == "Visible evidence final text"
    assert result.content_hash == f"sha256:{hashlib.sha256(body).hexdigest()}"
    assert len(transport.calls) == 1
    assert transport.calls[0].resolved_addresses == (_PUBLIC_A,)
    assert transport.calls[0].url == "https://example.test/article"
    rendered = repr(result)
    assert "Visible evidence" not in rendered
    assert "example.test/article" not in rendered
    assert result.content_hash not in rendered


@pytest.mark.parametrize(
    ("url", "records"),
    [
        ("http://127.0.0.1/", {}),
        ("http://169.254.169.254/latest/meta-data/", {}),
        ("https://example.test:8443/", {"example.test": (_PUBLIC_A,)}),
        ("https://user@example.test/", {"example.test": (_PUBLIC_A,)}),
        ("https://private.test/", {"private.test": ("10.0.0.8",)}),
        ("https://mixed.test/", {"mixed.test": (_PUBLIC_A, "169.254.10.1")}),
    ],
)
async def test_fetcher_rejects_ssrf_ambiguous_ports_and_userinfo_before_io(
    url: str,
    records: dict[str, tuple[str, ...]],
) -> None:
    transport = _QueueTransport([_response(b"unused")])
    fetcher = EvidenceFetcher(policy=_policy(records), transport=transport)

    with pytest.raises(EvidencePolicyError):
        await fetcher.fetch(url)
    assert transport.calls == []


async def test_fetcher_rejects_dns_rebinding_and_unsafe_redirect() -> None:
    rebinding = _QueueTransport([_response(b"body", peer=_PUBLIC_B)])
    with pytest.raises(EvidencePolicyError, match="peer address changed"):
        await EvidenceFetcher(policy=_policy(), transport=rebinding).fetch("https://example.test/")
    assert len(rebinding.calls) == 1

    redirect = _QueueTransport(
        [
            _response(
                b"",
                status=302,
                location="http://169.254.169.254/latest/meta-data/",
            )
        ]
    )
    with pytest.raises(EvidencePolicyError):
        await EvidenceFetcher(policy=_policy(), transport=redirect).fetch("https://example.test/")
    assert len(redirect.calls) == 1


async def test_fetcher_follows_only_manually_reauthorized_bounded_redirects() -> None:
    transport = _QueueTransport(
        [
            _response(b"", status=302, location="https://next.test/final"),
            _response(b"final", peer=_PUBLIC_B, content_type="text/plain"),
        ]
    )
    result = await EvidenceFetcher(policy=_policy(), transport=transport).fetch("https://example.test/start")
    assert result.text == "final"
    assert result.redirect_count == 1
    assert [call.hostname for call in transport.calls] == ["example.test", "next.test"]

    overflow = _QueueTransport(
        [
            _response(b"", status=302, location="/again"),
            _response(b"", status=302, location="/again"),
        ]
    )
    with pytest.raises(EvidenceLimitError, match="redirect"):
        await EvidenceFetcher(
            policy=_policy(),
            transport=overflow,
            limits=EvidenceFetchLimits(max_redirects=1),
        ).fetch("https://example.test/start")


@pytest.mark.parametrize("mode", ["content_type", "decompression", "deadline"])
async def test_fetcher_bounds_media_decompression_and_total_deadline(mode: str) -> None:
    limits = EvidenceFetchLimits(
        max_compressed_bytes=512,
        max_decompressed_bytes=32,
        max_text_chars=32,
        timeout_seconds=0.01,
    )
    if mode == "content_type":
        transport = _QueueTransport([_response(b"{}", content_type="application/json")])
        expected = EvidenceResponseError
    elif mode == "decompression":
        compressed = gzip.compress(b"A" * 128)
        transport = _QueueTransport([_response(compressed, content_type="text/plain", encoding="gzip")])
        expected = EvidenceLimitError
    else:
        transport = _QueueTransport([_response(b"ok", content_type="text/plain")], 0.05)
        expected = EvidenceLimitError

    with pytest.raises(expected):
        await EvidenceFetcher(
            policy=_policy(),
            transport=transport,
            limits=limits,
        ).fetch("https://example.test/")


async def test_actual_aiohttp_transport_rejects_unsafe_direct_requests_without_io() -> None:
    transport = AiohttpSafeEvidenceTransport()
    nonstandard_port = EvidenceHttpRequest(
        url="https://example.test:8443/",
        hostname="example.test",
        resolved_addresses=(_PUBLIC_A,),
        timeout_seconds=1,
        max_response_bytes=1_024,
    )
    private_pin = EvidenceHttpRequest(
        url="https://example.test/",
        hostname="example.test",
        resolved_addresses=("127.0.0.1",),
        timeout_seconds=1,
        max_response_bytes=1_024,
    )
    literal_bypass = EvidenceHttpRequest(
        url="http://169.254.169.254/latest/meta-data/",
        hostname="169.254.169.254",
        resolved_addresses=(_PUBLIC_A,),
        timeout_seconds=1,
        max_response_bytes=1_024,
    )

    with pytest.raises(EvidencePolicyError):
        await transport.fetch(nonstandard_port)
    with pytest.raises(EvidencePolicyError):
        await transport.fetch(private_pin)
    with pytest.raises(EvidencePolicyError):
        await transport.fetch(literal_bypass)


async def test_actual_aiohttp_transport_uses_pinned_resolver_and_no_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector_kwargs: dict[str, object] = {}
    session_kwargs: dict[str, object] = {}
    get_calls: list[tuple[str, bool]] = []

    class _Transport:
        @staticmethod
        def get_extra_info(name: str) -> tuple[str, int] | None:
            return (_PUBLIC_A, 443) if name == "peername" else None

    class _Content:
        async def iter_chunked(self, size: int):
            assert size == 64 * 1024
            yield b"safe "
            yield b"body"

    class _Response:
        status = 200
        content_length = 9
        headers = {"Content-Type": "text/plain"}
        connection = type("_Connection", (), {"transport": _Transport()})()
        content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class _Session:
        def __init__(self, **kwargs: object) -> None:
            session_kwargs.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def get(self, url: str, *, allow_redirects: bool):
            get_calls.append((url, allow_redirects))
            return _Response()

    def _connector(**kwargs: object) -> object:
        connector_kwargs.update(kwargs)
        return object()

    monkeypatch.setattr(
        "yonerai_discord.search_fabric.evidence_fetcher.aiohttp.TCPConnector",
        _connector,
    )
    monkeypatch.setattr(
        "yonerai_discord.search_fabric.evidence_fetcher.aiohttp.ClientSession",
        _Session,
    )
    request = EvidenceHttpRequest(
        url="https://example.test/article",
        hostname="example.test",
        resolved_addresses=(_PUBLIC_A,),
        timeout_seconds=1,
        max_response_bytes=1_024,
    )
    response = await AiohttpSafeEvidenceTransport().fetch(request)

    assert response.body == b"safe body"
    assert response.peer_address == _PUBLIC_A
    assert get_calls == [("https://example.test/article", False)]
    assert session_kwargs["auto_decompress"] is False
    assert session_kwargs["trust_env"] is False
    assert connector_kwargs["use_dns_cache"] is False
    resolver = connector_kwargs["resolver"]
    resolved = await resolver.resolve("example.test", 443, 0)  # type: ignore[union-attr]
    assert resolved[0]["host"] == _PUBLIC_A
    with pytest.raises(OSError, match="hostname mismatch"):
        await resolver.resolve("attacker.test", 443, 0)  # type: ignore[union-attr]


def test_classifier_uses_code_owned_rules_direct_fetch_and_exact_contract_values() -> None:
    classifier = SourceClassifier(
        rules=(
            SourceClassRule("agency.example", SourceClass.PRIMARY_OFFICIAL),
            SourceClassRule("*.journals.example", SourceClass.PEER_REVIEWED),
        ),
        fresh_for_seconds=60,
    )
    current = classifier.assess(
        SourceEvidenceFacts(
            hostname="agency.example",
            fetch_state=FetchState.FETCHED,
            transport_authenticated=True,
            fetched_at_epoch_seconds=950,
            published_at_epoch_seconds=950,
            content_hash=_hash("official"),
            corroboration=CorroborationState.INDEPENDENT,
        ),
        now_epoch_seconds=1_000,
    )
    assert current.source_class is SourceClass.PRIMARY_OFFICIAL
    assert current.fetch_state.value == "fetched"
    assert current.freshness_state is FreshnessState.CURRENT
    assert current.content_hash == _hash("official")
    assert current.corroboration is CorroborationState.INDEPENDENT
    assert current.verification_reasons == (
        VerificationReason.OFFICIAL_DOMAIN_RULE,
        VerificationReason.DIRECT_FETCH_VERIFIED,
        VerificationReason.CONTENT_HASH_VERIFIED,
        VerificationReason.TRANSPORT_AUTHENTICATED,
        VerificationReason.FRESHNESS_CONFIRMED,
        VerificationReason.INDEPENDENT_CORROBORATION,
    )
    assert current.to_mapping()["freshness_state"] == "current"

    metadata_only = classifier.assess(
        SourceEvidenceFacts(
            hostname="unknown.example",
            fetch_state=FetchState.METADATA_ONLY,
            corroboration=CorroborationState.UNKNOWN,
        ),
        now_epoch_seconds=1_000,
    )
    assert metadata_only.source_class is SourceClass.UNKNOWN
    assert metadata_only.fetch_state.value == "metadata_only"
    assert metadata_only.freshness_state.value == "unknown"
    assert metadata_only.content_hash is None


def test_classifier_marks_old_direct_content_dated_and_rejects_false_fetch_claims() -> None:
    classifier = SourceClassifier(fresh_for_seconds=10)
    assessment = classifier.assess(
        SourceEvidenceFacts(
            hostname="example.test",
            fetch_state=FetchState.FETCHED,
            transport_authenticated=True,
            fetched_at_epoch_seconds=1,
            published_at_epoch_seconds=1,
            content_hash=_hash("old"),
            corroboration=CorroborationState.NONE,
        ),
        now_epoch_seconds=100,
    )
    assert assessment.freshness_state is FreshnessState.DATED
    assert VerificationReason.CONTENT_STALE in assessment.verification_reasons

    with pytest.raises(ValueError, match="content hash"):
        SourceEvidenceFacts(
            hostname="example.test",
            fetch_state=FetchState.FETCHED,
            transport_authenticated=True,
            fetched_at_epoch_seconds=1,
            content_hash=None,
        )


def test_classifier_never_promotes_unauthenticated_http_content_to_official() -> None:
    classifier = SourceClassifier(
        rules=(SourceClassRule("docs.example", SourceClass.PRIMARY_OFFICIAL),),
    )
    assessment = classifier.assess(
        SourceEvidenceFacts(
            hostname="docs.example",
            fetch_state=FetchState.FETCHED,
            transport_authenticated=False,
            fetched_at_epoch_seconds=1,
            content_hash=_hash("interceptable body"),
        ),
        now_epoch_seconds=2,
    )

    assert assessment.source_class is SourceClass.UNKNOWN
    assert VerificationReason.TRANSPORT_UNAUTHENTICATED in assessment.verification_reasons
    assert VerificationReason.OFFICIAL_DOMAIN_RULE not in assessment.verification_reasons


def test_corroboration_groups_exact_and_syndicated_duplicates_without_rank_trust() -> None:
    exact_hash = _hash("same wire story")
    report = group_corroboration(
        (
            CorroborationCandidate("a", "one.example", exact_hash),
            CorroborationCandidate("b", "two.example", exact_hash),
            CorroborationCandidate("c", "three.example", _hash("independent account")),
        )
    )
    assert report.independent_group_count == 0
    assert report.corroboration is CorroborationState.DUPLICATE_ONLY
    assert report.groups[0].duplicate_kind is DuplicateKind.EXACT_CONTENT
    assert report.groups[0].hostnames == ("one.example", "two.example")
    assert report.groups[1].duplicate_kind is DuplicateKind.UNIQUE

    syndicated = group_corroboration(
        (
            CorroborationCandidate(
                "a",
                "one.example",
                _hash("edited one"),
                _hash("wire-source"),
            ),
            CorroborationCandidate(
                "b",
                "two.example",
                _hash("edited two"),
                _hash("wire-source"),
            ),
        )
    )
    assert syndicated.independent_group_count == 0
    assert syndicated.corroboration is CorroborationState.DUPLICATE_ONLY
    assert syndicated.groups[0].duplicate_kind is DuplicateKind.SYNDICATED

    unrelated_same_publisher = group_corroboration(
        (
            CorroborationCandidate("a", "same.example", _hash("claim a")),
            CorroborationCandidate("b", "same.example", _hash("contradictory claim b")),
        )
    )
    assert unrelated_same_publisher.independent_group_count == 0
    assert unrelated_same_publisher.corroboration is CorroborationState.NONE


def test_cache_is_digest_keyed_ttl_bounded_lru_and_sqlite_free() -> None:
    now = [100.0]
    cache = BoundedEvidenceCache(
        ttl_seconds=10,
        max_entries=2,
        max_bytes=4_096,
        clock=lambda: now[0],
    )
    first = EvidenceCacheKey(_hash("first"))
    second = EvidenceCacheKey(_hash("second"))
    third = EvidenceCacheKey(_hash("third"))
    cache.put(first, _evidence(text="first"))
    cache.put(second, _evidence(text="second"))
    assert cache.get(first) is not None
    cache.put(third, _evidence(text="third"))
    assert cache.get(second) is None
    assert cache.get(first) is not None
    assert cache.stats().entries == 2
    assert "first" not in repr(first)

    now[0] = 111.0
    assert cache.get(first) is None
    assert cache.stats().entries == 0
    with pytest.raises(ValueError):
        EvidenceCacheKey("raw user query")

    small_cache = BoundedEvidenceCache(max_entries=1, max_bytes=1_024)
    with pytest.raises(ValueError, match="byte budget"):
        small_cache.put(EvidenceCacheKey(_hash("large")), _evidence(text="x" * 2_000))
