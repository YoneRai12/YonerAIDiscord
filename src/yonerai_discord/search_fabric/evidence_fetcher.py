from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import unicodedata
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

from yonerai_discord.browser_sandbox.models import BrowserPolicyError
from yonerai_discord.browser_sandbox.policy import (
    AuthorizedURL,
    BrowserSandboxPolicy,
    StaticDnsResolver,
)


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_MEDIA_TYPES = frozenset({"text/html", "text/plain"})
_BLOCKED_HTML_ELEMENTS = frozenset(
    {
        "embed",
        "form",
        "iframe",
        "noscript",
        "object",
        "script",
        "style",
        "svg",
        "template",
    }
)
_VOID_HTML_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_WHITESPACE = re.compile(r"\s+")


def _bounded_int(name: str, value: object, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the allowed range")


class EvidenceFetchError(RuntimeError):
    """Base error whose message never includes fetched content or a target URL."""


class EvidencePolicyError(EvidenceFetchError):
    pass


class EvidenceResponseError(EvidenceFetchError):
    pass


class EvidenceLimitError(EvidenceFetchError):
    pass


@dataclass(frozen=True, slots=True)
class EvidenceFetchLimits:
    max_redirects: int = 3
    max_compressed_bytes: int = 512 * 1024
    max_decompressed_bytes: int = 1024 * 1024
    max_text_chars: int = 100_000
    timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        _bounded_int("max_redirects", self.max_redirects, 0, 10)
        _bounded_int("max_compressed_bytes", self.max_compressed_bytes, 1, 8 * 1024 * 1024)
        _bounded_int(
            "max_decompressed_bytes",
            self.max_decompressed_bytes,
            1,
            16 * 1024 * 1024,
        )
        _bounded_int("max_text_chars", self.max_text_chars, 1, 1_000_000)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.01 <= float(self.timeout_seconds) <= 60.0
        ):
            raise ValueError("timeout_seconds is outside the allowed range")


@dataclass(frozen=True, slots=True)
class EvidenceHttpRequest:
    """One pinned GET request. Implementations must disable automatic redirects."""

    url: str = field(repr=False)
    hostname: str
    resolved_addresses: tuple[str, ...] = field(repr=False)
    timeout_seconds: float
    max_response_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ValueError("url must be a non-empty string")
        if not isinstance(self.hostname, str) or not self.hostname:
            raise ValueError("hostname must be a non-empty string")
        if not self.resolved_addresses or any(
            not isinstance(value, str) or not value for value in self.resolved_addresses
        ):
            raise ValueError("resolved_addresses must contain normalized addresses")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or float(self.timeout_seconds) <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        _bounded_int("max_response_bytes", self.max_response_bytes, 1, 8 * 1024 * 1024)


@dataclass(frozen=True, slots=True)
class EvidenceHttpResponse:
    """Raw, non-redirect-following response returned by a trusted HTTP seam."""

    status: int
    body: bytes = field(repr=False)
    peer_address: str = field(repr=False)
    content_type: str | None = None
    content_encoding: str | None = None
    content_length: int | None = None
    location: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _bounded_int("status", self.status, 100, 599)
        if type(self.body) is not bytes:
            raise TypeError("body must be bytes")
        if not isinstance(self.peer_address, str) or not self.peer_address:
            raise ValueError("peer_address must be a non-empty string")
        for name, value in (
            ("content_type", self.content_type),
            ("content_encoding", self.content_encoding),
            ("location", self.location),
        ):
            if value is not None and (
                not isinstance(value, str)
                or len(value) > 2_048
                or any(ord(character) < 32 and character not in "\t" for character in value)
            ):
                raise ValueError(f"{name} is invalid")
        if self.content_length is not None:
            _bounded_int("content_length", self.content_length, 0, 2**31 - 1)


class EvidenceHttpFetchPort(Protocol):
    async def fetch(self, request: EvidenceHttpRequest) -> EvidenceHttpResponse: ...


@dataclass(frozen=True, slots=True)
class FetchedEvidence:
    canonical_url: str = field(repr=False)
    hostname: str
    media_type: str
    title: str = field(repr=False)
    text: str = field(repr=False)
    content_hash: str = field(repr=False)
    redirect_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_url, str) or not self.canonical_url:
            raise ValueError("canonical_url must be a non-empty string")
        if not isinstance(self.hostname, str) or not self.hostname:
            raise ValueError("hostname must be a non-empty string")
        if self.media_type not in _ALLOWED_MEDIA_TYPES:
            raise ValueError("media_type is unsupported")
        if not isinstance(self.title, str) or not isinstance(self.text, str):
            raise TypeError("title and text must be strings")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.content_hash):
            raise ValueError("content_hash must be a prefixed lowercase SHA-256 digest")
        _bounded_int("redirect_count", self.redirect_count, 0, 10)


class AiohttpSafeEvidenceTransport:
    """Pinned aiohttp GET seam preserving the URL hostname for Host and TLS SNI."""

    async def fetch(self, request: EvidenceHttpRequest) -> EvidenceHttpResponse:
        if not isinstance(request, EvidenceHttpRequest):
            raise TypeError("request must be EvidenceHttpRequest")
        _validate_transport_request(request)
        resolver = _PinnedResolver(request.hostname, request.resolved_addresses)
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=False,
            ttl_dns_cache=0,
            limit=1,
            force_close=True,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(total=request.timeout_seconds)
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                connector_owner=True,
                timeout=timeout,
                auto_decompress=False,
                trust_env=False,
                max_line_size=8_192,
                max_field_size=8_192,
                headers={
                    "Accept": "text/html, text/plain",
                    "Accept-Encoding": "gzip, deflate",
                    "User-Agent": "YonerAI-EvidenceFetcher/1",
                },
            ) as session:
                async with session.get(request.url, allow_redirects=False) as response:
                    peer_address = _response_peer_address(response)
                    if not _peer_matches(peer_address, request.resolved_addresses):
                        raise EvidencePolicyError("evidence peer address changed after authorization")
                    declared_length = response.content_length
                    if declared_length is not None and declared_length > request.max_response_bytes:
                        raise EvidenceLimitError("evidence compressed body is too large")
                    chunks: list[bytes] = []
                    consumed = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        consumed += len(chunk)
                        if consumed > request.max_response_bytes:
                            raise EvidenceLimitError("evidence compressed body is too large")
                        chunks.append(bytes(chunk))
                    return EvidenceHttpResponse(
                        status=response.status,
                        body=b"".join(chunks),
                        peer_address=peer_address,
                        content_type=response.headers.get("Content-Type"),
                        content_encoding=response.headers.get("Content-Encoding"),
                        content_length=declared_length,
                        location=response.headers.get("Location"),
                    )
        except asyncio.CancelledError:
            raise
        except EvidenceFetchError:
            raise
        except Exception:
            raise EvidenceFetchError("evidence HTTP transport failed") from None


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, hostname: str, addresses: tuple[str, ...]) -> None:
        self._hostname = _normalize_transport_hostname(hostname)
        normalized: list[tuple[str, socket.AddressFamily]] = []
        for raw_address in addresses:
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError as exc:
                raise EvidencePolicyError("evidence pinned address is invalid") from exc
            mapped = getattr(address, "ipv4_mapped", None)
            address = mapped if mapped is not None else address
            _require_public_peer(address)
            normalized.append(
                (
                    address.compressed,
                    socket.AF_INET if address.version == 4 else socket.AF_INET6,
                )
            )
        if not normalized or len(set(normalized)) != len(normalized):
            raise EvidencePolicyError("evidence pinned addresses are missing or duplicated")
        self._addresses = tuple(normalized)

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[aiohttp.abc.ResolveResult]:
        if _normalize_transport_hostname(host) != self._hostname:
            raise OSError("pinned resolver hostname mismatch")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            raise OSError("pinned resolver port is invalid")
        return [
            {
                "hostname": self._hostname,
                "host": address,
                "port": port,
                "family": int(address_family),
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }
            for address, address_family in self._addresses
            if family in {socket.AF_UNSPEC, address_family}
        ]

    async def close(self) -> None:
        return None


class EvidenceFetcher:
    def __init__(
        self,
        *,
        policy: BrowserSandboxPolicy,
        transport: EvidenceHttpFetchPort,
        limits: EvidenceFetchLimits = EvidenceFetchLimits(),
    ) -> None:
        if not isinstance(policy, BrowserSandboxPolicy):
            raise TypeError("policy must be BrowserSandboxPolicy")
        if not callable(getattr(transport, "fetch", None)):
            raise TypeError("transport must implement fetch(request)")
        if not isinstance(limits, EvidenceFetchLimits):
            raise TypeError("limits must be EvidenceFetchLimits")
        self._policy = policy
        self._transport = transport
        self._limits = limits

    async def fetch(self, url: str) -> FetchedEvidence:
        try:
            async with asyncio.timeout(self._limits.timeout_seconds):
                return await self._fetch(url)
        except TimeoutError as exc:
            raise EvidenceLimitError("evidence fetch deadline exceeded") from exc

    async def _fetch(self, initial_url: str) -> FetchedEvidence:
        current_url = initial_url
        redirects = 0
        while True:
            # Public DNS resolution is synchronous in the shared browser policy.
            # Keep it outside the Discord event loop while preserving the
            # resolve-then-pin contract used by the HTTP transport.
            if isinstance(self._policy.resolver, StaticDnsResolver):
                authorized = self._authorize(current_url)
            else:
                authorized = await asyncio.to_thread(self._authorize, current_url)
            request_url = _without_fragment(authorized.url)
            request = EvidenceHttpRequest(
                url=request_url,
                hostname=authorized.hostname,
                resolved_addresses=tuple(_canonical_ip(address) for address in authorized.addresses),
                timeout_seconds=self._limits.timeout_seconds,
                max_response_bytes=self._limits.max_compressed_bytes,
            )
            try:
                response = await self._transport.fetch(request)
            except asyncio.CancelledError:
                raise
            except EvidenceFetchError:
                raise
            except Exception:
                raise EvidenceFetchError("evidence transport failed") from None
            if not isinstance(response, EvidenceHttpResponse):
                raise EvidenceResponseError("evidence transport returned an invalid response")
            self._validate_peer(response.peer_address, authorized)
            self._validate_wire_size(response)

            if response.status in _REDIRECT_STATUSES:
                if redirects >= self._limits.max_redirects:
                    raise EvidenceLimitError("evidence redirect limit exceeded")
                if not response.location:
                    raise EvidenceResponseError("evidence redirect location is missing")
                current_url = urljoin(request_url, response.location)
                redirects += 1
                continue
            if response.status != 200:
                raise EvidenceResponseError("evidence endpoint returned an unusable status")

            media_type, charset = _parse_content_type(response.content_type)
            raw_body = _decode_content(response.body, response.content_encoding, self._limits)
            try:
                decoded = raw_body.decode(charset, errors="strict")
            except UnicodeDecodeError as exc:
                raise EvidenceResponseError("evidence body is not valid UTF-8") from exc
            title, text = _sanitize_text(
                decoded,
                media_type=media_type,
                max_chars=self._limits.max_text_chars,
            )
            return FetchedEvidence(
                canonical_url=request_url,
                hostname=authorized.hostname,
                media_type=media_type,
                title=title,
                text=text,
                content_hash=f"sha256:{hashlib.sha256(raw_body).hexdigest()}",
                redirect_count=redirects,
            )

    def _authorize(self, url: str) -> AuthorizedURL:
        try:
            return self._policy.authorize_url(url)
        except BrowserPolicyError as exc:
            raise EvidencePolicyError("evidence URL is not permitted") from exc
        except (TypeError, ValueError) as exc:
            raise EvidencePolicyError("evidence URL is invalid") from exc

    @staticmethod
    def _validate_peer(peer_address: str, authorized: AuthorizedURL) -> None:
        try:
            peer = ipaddress.ip_address(peer_address)
        except ValueError as exc:
            raise EvidencePolicyError("evidence peer address is invalid") from exc
        peer_mapped = getattr(peer, "ipv4_mapped", None)
        normalized_peer = peer_mapped if peer_mapped is not None else peer
        allowed = {getattr(address, "ipv4_mapped", None) or address for address in authorized.addresses}
        if normalized_peer not in allowed:
            raise EvidencePolicyError("evidence peer address changed after authorization")

    def _validate_wire_size(self, response: EvidenceHttpResponse) -> None:
        size = len(response.body)
        if size > self._limits.max_compressed_bytes:
            raise EvidenceLimitError("evidence compressed body is too large")
        if response.content_length is not None:
            if response.content_length != size:
                raise EvidenceResponseError("evidence content length is inconsistent")
            if response.content_length > self._limits.max_compressed_bytes:
                raise EvidenceLimitError("evidence compressed body is too large")


def _decode_content(
    body: bytes,
    content_encoding: str | None,
    limits: EvidenceFetchLimits,
) -> bytes:
    encoding = (content_encoding or "identity").strip().lower()
    if encoding in {"", "identity"}:
        if len(body) > limits.max_decompressed_bytes:
            raise EvidenceLimitError("evidence body is too large")
        return body
    if encoding not in {"gzip", "deflate"}:
        raise EvidenceResponseError("evidence content encoding is unsupported")
    try:
        wbits = zlib.MAX_WBITS | 16 if encoding == "gzip" else zlib.MAX_WBITS
        inflater = zlib.decompressobj(wbits)
        decoded = inflater.decompress(body, limits.max_decompressed_bytes + 1)
        if inflater.unconsumed_tail or len(decoded) > limits.max_decompressed_bytes:
            raise EvidenceLimitError("evidence decompressed body is too large")
        decoded += inflater.flush(limits.max_decompressed_bytes + 1 - len(decoded))
    except EvidenceLimitError:
        raise
    except zlib.error as exc:
        raise EvidenceResponseError("evidence compressed body is malformed") from exc
    if (
        len(decoded) > limits.max_decompressed_bytes
        or not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
    ):
        raise EvidenceResponseError("evidence compressed body is malformed")
    return decoded


def _parse_content_type(value: str | None) -> tuple[str, str]:
    if not isinstance(value, str) or not value:
        raise EvidenceResponseError("evidence content type is missing")
    parts = [part.strip() for part in value.split(";")]
    media_type = parts[0].lower()
    if media_type not in _ALLOWED_MEDIA_TYPES:
        raise EvidenceResponseError("evidence content type is unsupported")
    charset = "utf-8"
    for parameter in parts[1:]:
        if not parameter:
            continue
        name, separator, raw_value = parameter.partition("=")
        if name.strip().lower() != "charset" or not separator:
            continue
        normalized = raw_value.strip().strip("\"'").lower()
        if normalized not in {"utf-8", "utf8", "us-ascii"}:
            raise EvidenceResponseError("evidence charset is unsupported")
        charset = "ascii" if normalized == "us-ascii" else "utf-8"
    return media_type, charset


def _sanitize_text(value: str, *, media_type: str, max_chars: int) -> tuple[str, str]:
    if media_type == "text/plain":
        normalized = _normalize_visible_text(value)
        if len(normalized) > max_chars:
            raise EvidenceLimitError("evidence text is too large")
        return "", normalized
    extractor = _BoundedVisibleTextExtractor(max_chars=max_chars)
    try:
        extractor.feed(value)
        extractor.close()
    except EvidenceLimitError:
        raise
    except Exception:
        raise EvidenceResponseError("evidence HTML is malformed") from None
    return extractor.result()


class _BoundedVisibleTextExtractor(HTMLParser):
    def __init__(self, *, max_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self._max_chars = max_chars
        self._used_chars = 0
        self._blocked_depth = 0
        self._title_depth = 0
        self._stack: list[tuple[str, bool, bool]] = []
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        blocked = self._blocked_depth > 0 or _element_is_hidden(normalized_tag, attrs)
        title = not blocked and normalized_tag == "title"
        if normalized_tag not in _VOID_HTML_ELEMENTS:
            self._stack.append((normalized_tag, blocked, title))
            if blocked:
                self._blocked_depth += 1
            if title:
                self._title_depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag, attrs

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        match_index = next(
            (index for index in range(len(self._stack) - 1, -1, -1) if self._stack[index][0] == normalized_tag),
            None,
        )
        if match_index is None:
            return
        for _, blocked, title in reversed(self._stack[match_index:]):
            if blocked:
                self._blocked_depth = max(0, self._blocked_depth - 1)
            if title:
                self._title_depth = max(0, self._title_depth - 1)
        del self._stack[match_index:]

    def handle_data(self, data: str) -> None:
        if self._blocked_depth:
            return
        self._used_chars += len(data)
        if self._used_chars > self._max_chars:
            raise EvidenceLimitError("evidence text is too large")
        if self._title_depth:
            self._title_parts.append(data)
        else:
            self._text_parts.append(data)

    def result(self) -> tuple[str, str]:
        title = _normalize_visible_text(" ".join(self._title_parts))
        text = _normalize_visible_text(" ".join(self._text_parts))
        if len(title) + len(text) > self._max_chars:
            raise EvidenceLimitError("evidence text is too large")
        return title, text


def _element_is_hidden(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag in _BLOCKED_HTML_ELEMENTS:
        return True
    values = {name.lower(): (value or "") for name, value in attrs}
    if "hidden" in values or values.get("aria-hidden", "").strip().lower() == "true":
        return True
    if tag == "input" and values.get("type", "").strip().lower() == "hidden":
        return True
    style = re.sub(r"\s+", "", values.get("style", "").lower())
    return "display:none" in style or "visibility:hidden" in style


def _normalize_visible_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    filtered = "".join(
        character
        for character in normalized
        if character in "\t\n\r" or unicodedata.category(character) not in {"Cc", "Cf", "Cs", "Co"}
    )
    return _WHITESPACE.sub(" ", filtered).strip()


def _without_fragment(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _canonical_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped if mapped is not None else address).compressed


def _validate_transport_request(request: EvidenceHttpRequest) -> None:
    try:
        parsed = urlsplit(request.url)
    except ValueError as exc:
        raise EvidencePolicyError("evidence transport URL is malformed") from exc
    if parsed.fragment:
        raise EvidencePolicyError("evidence transport URL is not permitted")
    try:
        authorized = BrowserSandboxPolicy(
            resolver=StaticDnsResolver({request.hostname: request.resolved_addresses})
        ).authorize_url(request.url)
    except (BrowserPolicyError, TypeError, ValueError) as exc:
        raise EvidencePolicyError("evidence transport URL is not permitted") from exc
    if authorized.hostname != _normalize_transport_hostname(request.hostname):
        raise EvidencePolicyError("evidence transport hostname binding mismatch")
    resolver = _PinnedResolver(request.hostname, request.resolved_addresses)
    authorized_addresses = {_canonical_ip(address) for address in authorized.addresses}
    if authorized_addresses != {address for address, _ in resolver._addresses}:
        raise EvidencePolicyError("evidence transport address binding mismatch")


def _normalize_transport_hostname(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidencePolicyError("evidence transport hostname is invalid")
    normalized = value.lower().removesuffix(".")
    try:
        return normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise EvidencePolicyError("evidence transport hostname is invalid") from exc


def _require_public_peer(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    ):
        raise EvidencePolicyError("evidence pinned address is not public")


def _peer_matches(peer_address: str, allowed_addresses: tuple[str, ...]) -> bool:
    try:
        peer = ipaddress.ip_address(peer_address)
        peer = getattr(peer, "ipv4_mapped", None) or peer
        allowed = {
            getattr(address, "ipv4_mapped", None) or address
            for address in (ipaddress.ip_address(value) for value in allowed_addresses)
        }
    except ValueError:
        return False
    return peer in allowed


def _response_peer_address(response: aiohttp.ClientResponse) -> str:
    connection = response.connection
    transport = getattr(connection, "transport", None)
    peer = transport.get_extra_info("peername") if transport is not None else None
    if not isinstance(peer, tuple) or not peer or not isinstance(peer[0], str):
        raise EvidencePolicyError("evidence peer address is unavailable")
    return peer[0]
