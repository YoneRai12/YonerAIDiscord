from __future__ import annotations

import ipaddress
import re
import time
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .models import (
    BrowserPolicyError,
    BrowserResourceLimitError,
    BrowserSessionRequest,
    Navigate,
    Wait,
)


MAX_URL_CHARS = 2_048
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_IP_LIKE_LABEL = re.compile(r"^(?:0x[0-9a-f]+|[0-9]+)$", re.IGNORECASE)


class DnsResolver(Protocol):
    """Resolve one already-normalized ASCII hostname without performing a connection."""

    def resolve(self, hostname: str) -> tuple[str, ...]: ...


class FailClosedDnsResolver:
    def resolve(self, hostname: str) -> tuple[str, ...]:
        del hostname
        raise BrowserPolicyError("DNS resolver is not configured")


@dataclass(frozen=True, slots=True)
class StaticDnsResolver:
    records: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        normalized: dict[str, tuple[str, ...]] = {}
        for host, addresses in self.records.items():
            normalized[_normalize_hostname(host)] = tuple(addresses)
        object.__setattr__(self, "records", MappingProxyType(normalized))

    def resolve(self, hostname: str) -> tuple[str, ...]:
        try:
            return self.records[hostname]
        except KeyError as exc:
            raise BrowserPolicyError("hostname did not resolve") from exc


@dataclass(frozen=True, slots=True)
class BrowserSandboxLimits:
    max_steps: int = 40
    max_redirects: int = 5
    max_network_requests: int = 200
    max_duration_seconds: float = 45.0
    max_total_bytes: int = 25 * 1024 * 1024
    max_total_wait_milliseconds: int = 20_000

    def __post_init__(self) -> None:
        _bounded_int("max_steps", self.max_steps, minimum=1, maximum=200)
        _bounded_int("max_redirects", self.max_redirects, minimum=0, maximum=20)
        _bounded_int("max_network_requests", self.max_network_requests, minimum=1, maximum=2_000)
        if (
            isinstance(self.max_duration_seconds, bool)
            or not isinstance(self.max_duration_seconds, (int, float))
            or not 1.0 <= float(self.max_duration_seconds) <= 300.0
        ):
            raise ValueError("max_duration_seconds is outside the allowed range")
        _bounded_int(
            "max_total_bytes",
            self.max_total_bytes,
            minimum=64 * 1024,
            maximum=100 * 1024 * 1024,
        )
        _bounded_int(
            "max_total_wait_milliseconds",
            self.max_total_wait_milliseconds,
            minimum=0,
            maximum=120_000,
        )


@dataclass(frozen=True, slots=True)
class AuthorizedURL:
    url: str = field(repr=False)
    hostname: str
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class BrowserSandboxPolicy:
    resolver: DnsResolver = field(default_factory=FailClosedDnsResolver, repr=False)
    allowed_domains: tuple[str, ...] = ()
    denied_domains: tuple[str, ...] = ()
    allowed_ports: Mapping[str, frozenset[int]] = field(
        default_factory=lambda: MappingProxyType({"http": frozenset({80}), "https": frozenset({443})})
    )
    limits: BrowserSandboxLimits = field(default_factory=BrowserSandboxLimits)

    def __post_init__(self) -> None:
        if not callable(getattr(self.resolver, "resolve", None)):
            raise TypeError("resolver must implement resolve(hostname)")
        allowed = tuple(_normalize_domain_rule(rule) for rule in self.allowed_domains)
        denied = tuple(_normalize_domain_rule(rule) for rule in self.denied_domains)
        if len(set(allowed)) != len(allowed) or len(set(denied)) != len(denied):
            raise ValueError("domain rules must not contain duplicates")
        object.__setattr__(self, "allowed_domains", allowed)
        object.__setattr__(self, "denied_domains", denied)

        ports: dict[str, frozenset[int]] = {}
        if not isinstance(self.allowed_ports, Mapping):
            raise TypeError("allowed_ports must be a mapping")
        for scheme in _ALLOWED_SCHEMES:
            values = self.allowed_ports.get(scheme, frozenset())
            normalized_values = frozenset(values)
            if not normalized_values:
                raise ValueError(f"allowed_ports[{scheme!r}] must not be empty")
            for value in normalized_values:
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65_535:
                    raise ValueError("allowed ports must be integers from 1 to 65535")
            ports[scheme] = normalized_values
        if set(self.allowed_ports) != _ALLOWED_SCHEMES:
            raise ValueError("allowed_ports must contain only http and https")
        object.__setattr__(self, "allowed_ports", MappingProxyType(ports))

        if not isinstance(self.limits, BrowserSandboxLimits):
            raise TypeError("limits must be BrowserSandboxLimits")

    def validate_session(self, request: BrowserSessionRequest) -> None:
        if not isinstance(request, BrowserSessionRequest):
            raise TypeError("request must be BrowserSessionRequest")
        if len(request.actions) > self.limits.max_steps:
            raise BrowserResourceLimitError("browser session has too many steps")
        total_wait = sum(action.milliseconds for action in request.actions if isinstance(action, Wait))
        if total_wait > self.limits.max_total_wait_milliseconds:
            raise BrowserResourceLimitError("browser session wait budget exceeded")
        for action in request.actions:
            if isinstance(action, Navigate):
                self.authorize_url(action.url)

    def authorize_url(self, value: str) -> AuthorizedURL:
        parsed = _parse_http_url(value)
        hostname = _normalize_hostname(parsed.hostname or "")
        self._enforce_domain_rules(hostname)
        port = _effective_port(parsed)
        if port not in self.allowed_ports[parsed.scheme.lower()]:
            raise BrowserPolicyError("URL port is not allowed")

        literal = _parse_ip_literal(hostname)
        if literal is not None:
            addresses = (literal,)
        else:
            try:
                raw_addresses = tuple(self.resolver.resolve(hostname))
            except BrowserPolicyError:
                raise
            except Exception as exc:
                raise BrowserPolicyError("DNS resolution failed") from exc
            if not raw_addresses:
                raise BrowserPolicyError("hostname did not resolve")
            parsed_addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
            for raw_address in raw_addresses:
                try:
                    parsed_addresses.append(ipaddress.ip_address(raw_address))
                except ValueError as exc:
                    raise BrowserPolicyError("DNS returned an invalid address") from exc
            addresses = tuple(parsed_addresses)

        for address in addresses:
            _require_public_address(address)

        normalized_netloc = _normalized_netloc(parsed, hostname, port)
        normalized_url = urlunsplit(
            (parsed.scheme.lower(), normalized_netloc, parsed.path, parsed.query, parsed.fragment)
        )
        return AuthorizedURL(url=normalized_url, hostname=hostname, addresses=addresses)

    def _enforce_domain_rules(self, hostname: str) -> None:
        if any(_domain_rule_matches(rule, hostname) for rule in self.denied_domains):
            raise BrowserPolicyError("hostname is denied")
        if self.allowed_domains and not any(_domain_rule_matches(rule, hostname) for rule in self.allowed_domains):
            raise BrowserPolicyError("hostname is not allowlisted")


class BrowserNetworkGuard:
    """Stateful egress budget used by an isolated browser adapter before every request."""

    def __init__(self, policy: BrowserSandboxPolicy) -> None:
        self._policy = policy
        self._started_at = time.monotonic()
        self._requests = 0
        self._redirects = 0
        self._bytes = 0

    @property
    def request_count(self) -> int:
        return self._requests

    @property
    def redirect_count(self) -> int:
        return self._redirects

    @property
    def consumed_bytes(self) -> int:
        return self._bytes

    def authorize_request(self, url: str, *, redirect: bool = False) -> AuthorizedURL:
        if type(redirect) is not bool:
            raise TypeError("redirect must be a boolean")
        self.check_deadline()
        next_requests = self._requests + 1
        next_redirects = self._redirects + int(redirect)
        limits = self._policy.limits
        if next_requests > limits.max_network_requests:
            raise BrowserResourceLimitError("browser network request budget exceeded")
        if next_redirects > limits.max_redirects:
            raise BrowserResourceLimitError("browser redirect budget exceeded")
        authorized = self._policy.authorize_url(url)
        self._requests = next_requests
        self._redirects = next_redirects
        return authorized

    def consume_bytes(self, byte_count: int) -> None:
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise ValueError("byte_count must be a non-negative integer")
        self.check_deadline()
        if self._bytes + byte_count > self._policy.limits.max_total_bytes:
            raise BrowserResourceLimitError("browser byte budget exceeded")
        self._bytes += byte_count

    def check_deadline(self) -> None:
        if time.monotonic() - self._started_at > self._policy.limits.max_duration_seconds:
            raise BrowserResourceLimitError("browser session deadline exceeded")


def _parse_http_url(value: str) -> SplitResult:
    if not isinstance(value, str):
        raise TypeError("URL must be a string")
    if not value or value != value.strip() or len(value) > MAX_URL_CHARS:
        raise BrowserPolicyError("URL is blank, padded, or too long")
    if "\\" in value or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise BrowserPolicyError("URL contains an ambiguous or control character")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise BrowserPolicyError("URL is malformed") from exc
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.netloc or not parsed.hostname:
        raise BrowserPolicyError("URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise BrowserPolicyError("URL userinfo is forbidden")
    if "%" in parsed.hostname:
        raise BrowserPolicyError("percent-encoded or scoped hostname is forbidden")
    return parsed


def _normalize_hostname(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("hostname must be a string")
    hostname = unicodedata.normalize("NFC", value).lower()
    if hostname.endswith(".."):
        raise BrowserPolicyError("hostname is malformed")
    hostname = hostname.removesuffix(".")
    if not hostname or len(hostname) > 253 or hostname.startswith(".") or ".." in hostname:
        raise BrowserPolicyError("hostname is malformed")
    if "%" in hostname or any(character.isspace() or ord(character) < 32 for character in hostname):
        raise BrowserPolicyError("hostname is malformed")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise BrowserPolicyError("hostname IDN normalization failed") from exc
    literal = _parse_ip_literal(ascii_hostname)
    if literal is not None:
        return literal.compressed
    labels = ascii_hostname.split(".")
    if len(ascii_hostname) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise BrowserPolicyError("hostname is malformed")
    if all(_IP_LIKE_LABEL.fullmatch(label) for label in labels):
        raise BrowserPolicyError("ambiguous numeric hostname is forbidden")
    return ascii_hostname


def _normalize_domain_rule(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("domain rule must not be blank or padded")
    wildcard = value.startswith("*.")
    if "*" in value[2:] or ("*" in value and not wildcard):
        raise ValueError("only a leading *. wildcard is supported")
    normalized = _normalize_hostname(value[2:] if wildcard else value)
    if _parse_ip_literal(normalized) is not None and wildcard:
        raise ValueError("IP address rules cannot be wildcards")
    return f"*.{normalized}" if wildcard else normalized


def _domain_rule_matches(rule: str, hostname: str) -> bool:
    if rule.startswith("*."):
        suffix = rule[2:]
        return hostname != suffix and hostname.endswith(f".{suffix}")
    return hostname == rule


def _parse_ip_literal(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _require_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        _require_public_address(mapped)
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    ):
        raise BrowserPolicyError("URL resolved to a non-public address")


def _effective_port(parsed: SplitResult) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme.lower() == "https" else 80


def _normalized_netloc(parsed: SplitResult, hostname: str, port: int) -> str:
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return rendered_host if port == default_port and parsed.port is None else f"{rendered_host}:{port}"


def _bounded_int(name: str, value: object, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the allowed range")
