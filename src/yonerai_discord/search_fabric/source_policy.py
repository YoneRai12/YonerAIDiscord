"""Code-owned allowlist for public evidence and metadata origins.

This is deliberately a read-only policy registry, not a discovery mechanism:
models and search-result pages cannot add domains to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


class SourcePolicyError(ValueError):
    """A URL is outside the fixed public-source policy."""


@dataclass(frozen=True, slots=True)
class SourcePolicyRule:
    hostname: str
    path_prefixes: tuple[str, ...] = ("/",)

    def __post_init__(self) -> None:
        hostname = self.hostname.strip().lower().removesuffix(".")
        if not hostname or len(hostname) > 253 or any(character.isspace() for character in hostname):
            raise ValueError("policy hostname is invalid")
        if not isinstance(self.path_prefixes, tuple) or not self.path_prefixes:
            raise TypeError("path_prefixes must be a non-empty tuple")
        if any(
            not isinstance(prefix, str)
            or not prefix.startswith("/")
            or "\\" in prefix
            or ".." in prefix
            or len(prefix) > 256
            for prefix in self.path_prefixes
        ):
            raise ValueError("policy path prefix is invalid")
        object.__setattr__(self, "hostname", hostname)


class SourcePolicyRegistry:
    """Immutable exact-host source policy with bounded path prefixes."""

    def __init__(self, rules: tuple[SourcePolicyRule, ...]) -> None:
        if not isinstance(rules, tuple) or not rules or any(not isinstance(rule, SourcePolicyRule) for rule in rules):
            raise TypeError("rules must be a non-empty tuple of SourcePolicyRule")
        if len(rules) > 64 or len({rule.hostname for rule in rules}) != len(rules):
            raise ValueError("policy rules are duplicated or too numerous")
        self._rules = {rule.hostname: rule for rule in rules}

    def allows(self, url: str) -> bool:
        if not isinstance(url, str) or not url or len(url) > 2_048:
            return False
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return False
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
            or "\\" in url
            or any(character.isspace() or ord(character) < 32 for character in url)
            or port not in {None, 443}
        ):
            return False
        rule = self._rules.get(parsed.hostname.casefold().removesuffix("."))
        return rule is not None and any((parsed.path or "/").startswith(prefix) for prefix in rule.path_prefixes)

    def require(self, url: str) -> None:
        if not self.allows(url):
            raise SourcePolicyError("source URL is not allowed by the code-owned policy")


DEFAULT_SOURCE_POLICY = SourcePolicyRegistry(
    (
        SourcePolicyRule("docs.searxng.org"),
        SourcePolicyRule("github.com"),
        SourcePolicyRule("api.crossref.org", ("/works",)),
        SourcePolicyRule("api.openalex.org", ("/works",)),
        SourcePolicyRule("eutils.ncbi.nlm.nih.gov", ("/entrez/eutils/",)),
        SourcePolicyRule("pubmed.ncbi.nlm.nih.gov"),
        SourcePolicyRule("export.arxiv.org", ("/api/",)),
        SourcePolicyRule("arxiv.org", ("/abs/",)),
    )
)


__all__ = ["DEFAULT_SOURCE_POLICY", "SourcePolicyError", "SourcePolicyRegistry", "SourcePolicyRule"]
