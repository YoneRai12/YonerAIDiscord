from __future__ import annotations

import ipaddress

import pytest
import yonerai_discord.browser_sandbox.policy as policy_module

from yonerai_discord.browser_sandbox import (
    ALLOWED_BROWSER_ACTION_TYPES,
    BrowserNetworkGuard,
    BrowserPolicyError,
    BrowserResourceLimitError,
    BrowserSandboxLimits,
    BrowserSandboxPolicy,
    BrowserSessionRequest,
    Click,
    CssSelector,
    ExtractText,
    Navigate,
    Screenshot,
    Scroll,
    SelectOption,
    StaticDnsResolver,
    TypeText,
    Wait,
)


PUBLIC_V4 = "93.184.216.34"


def _policy(
    *,
    records: dict[str, tuple[str, ...]] | None = None,
    allowed_domains: tuple[str, ...] = (),
    denied_domains: tuple[str, ...] = (),
    limits: BrowserSandboxLimits | None = None,
    allowed_ports: dict[str, frozenset[int]] | None = None,
) -> BrowserSandboxPolicy:
    return BrowserSandboxPolicy(
        resolver=StaticDnsResolver(records or {"example.com": (PUBLIC_V4,)}),
        allowed_domains=allowed_domains,
        denied_domains=denied_domains,
        limits=limits or BrowserSandboxLimits(),
        allowed_ports=allowed_ports
        or {
            "http": frozenset({80}),
            "https": frozenset({443}),
        },
    )


def test_action_surface_contains_only_the_eight_browser_operations() -> None:
    assert {action_type.__name__ for action_type in ALLOWED_BROWSER_ACTION_TYPES} == {
        "Navigate",
        "Click",
        "TypeText",
        "SelectOption",
        "Scroll",
        "Wait",
        "Screenshot",
        "ExtractText",
    }
    request = BrowserSessionRequest(
        actions=(
            Navigate("https://example.com"),
            Click(CssSelector("button[type='submit']")),
            TypeText(CssSelector("input[name='q']"), "search"),
            SelectOption(CssSelector("select[name='size']"), "medium"),
            Scroll(delta_y=500),
            Wait(100),
            Screenshot(full_page=True),
            ExtractText(CssSelector("main")),
        )
    )
    assert len(request.actions) == 8


def test_untyped_or_subclassed_action_is_rejected() -> None:
    class NavigateSubclass(Navigate):
        pass

    with pytest.raises(TypeError, match="unsupported"):
        BrowserSessionRequest(actions=(NavigateSubclass("https://example.com"),))
    with pytest.raises(TypeError, match="unsupported"):
        BrowserSessionRequest(actions=({"action": "shell"},))  # type: ignore[arg-type]


def test_public_url_is_normalized_and_all_dns_answers_are_bound() -> None:
    policy = _policy(records={"example.com": (PUBLIC_V4, "2606:4700:4700::1111")})

    authorized = policy.authorize_url("HTTPS://EXAMPLE.COM./path?q=1#fragment")

    assert authorized.url == "https://example.com/path?q=1#fragment"
    assert authorized.hostname == "example.com"
    assert authorized.addresses == (
        ipaddress.ip_address(PUBLIC_V4),
        ipaddress.ip_address("2606:4700:4700::1111"),
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "https://user@example.com/",
        "https://user:password@example.com/",
        "https://example.com\\@127.0.0.1/",
        "https://example.com/path with space",
        "https://example.com:99999/",
        "https://example.com.../",
        "https://bad_host.example/",
        "http://2130706433/",
        "http://127.1/",
        "http://0x7f.0.0.1/",
        "//example.com/path",
    ],
)
def test_non_http_userinfo_and_ambiguous_urls_are_rejected(url: str) -> None:
    with pytest.raises(BrowserPolicyError):
        _policy().authorize_url(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "::",
        "::ffff:127.0.0.1",
        "100.64.0.1",
    ],
)
def test_non_public_literal_addresses_are_rejected(address: str) -> None:
    rendered = f"[{address}]" if ":" in address else address
    with pytest.raises(BrowserPolicyError, match="non-public"):
        _policy().authorize_url(f"https://{rendered}/")


def test_one_private_dns_answer_rejects_the_entire_resolution() -> None:
    policy = _policy(records={"example.com": (PUBLIC_V4, "127.0.0.1")})

    with pytest.raises(BrowserPolicyError, match="non-public"):
        policy.authorize_url("https://example.com/")


def test_dns_failure_is_fail_closed() -> None:
    policy = BrowserSandboxPolicy()

    with pytest.raises(BrowserPolicyError, match="DNS resolver is not configured"):
        policy.authorize_url("https://example.com/")


def test_idn_is_ascii_normalized_before_allowlist_and_dns() -> None:
    policy = _policy(
        records={"xn--r8jz45g.xn--zckzah": (PUBLIC_V4,)},
        allowed_domains=("例え.テスト",),
    )

    authorized = policy.authorize_url("https://例え.テスト/資料")

    assert authorized.hostname == "xn--r8jz45g.xn--zckzah"
    assert authorized.url.startswith("https://xn--r8jz45g.xn--zckzah/")


def test_allow_and_deny_domain_rules_are_exact_or_explicit_wildcards() -> None:
    resolver = StaticDnsResolver(
        {
            "example.com": (PUBLIC_V4,),
            "www.example.com": (PUBLIC_V4,),
            "blocked.example.com": (PUBLIC_V4,),
            "notexample.com": (PUBLIC_V4,),
        }
    )
    policy = BrowserSandboxPolicy(
        resolver=resolver,
        allowed_domains=("example.com", "*.example.com"),
        denied_domains=("blocked.example.com",),
    )

    assert policy.authorize_url("https://example.com/").hostname == "example.com"
    assert policy.authorize_url("https://www.example.com/").hostname == "www.example.com"
    with pytest.raises(BrowserPolicyError, match="denied"):
        policy.authorize_url("https://blocked.example.com/")
    with pytest.raises(BrowserPolicyError, match="allowlisted"):
        policy.authorize_url("https://notexample.com/")


def test_default_and_explicit_port_policy() -> None:
    policy = _policy()
    assert policy.authorize_url("https://example.com/").url == "https://example.com/"
    assert policy.authorize_url("http://example.com/").url == "http://example.com/"
    with pytest.raises(BrowserPolicyError, match="port"):
        policy.authorize_url("https://example.com:8443/")

    custom = _policy(
        allowed_ports={
            "http": frozenset({80}),
            "https": frozenset({443, 8443}),
        }
    )
    assert custom.authorize_url("https://example.com:8443/").url == "https://example.com:8443/"


def test_session_step_and_wait_budgets_are_enforced() -> None:
    policy = _policy(
        limits=BrowserSandboxLimits(
            max_steps=2,
            max_total_wait_milliseconds=100,
        )
    )
    with pytest.raises(BrowserResourceLimitError, match="steps"):
        policy.validate_session(BrowserSessionRequest(actions=(Screenshot(), Screenshot(), Screenshot())))
    with pytest.raises(BrowserResourceLimitError, match="wait"):
        policy.validate_session(BrowserSessionRequest(actions=(Wait(60), Wait(50))))


def test_each_redirect_is_reauthorized_and_redirect_budget_is_enforced() -> None:
    policy = _policy(
        records={
            "example.com": (PUBLIC_V4,),
            "cdn.example.com": (PUBLIC_V4,),
            "private.example.com": ("10.0.0.1",),
        },
        limits=BrowserSandboxLimits(max_redirects=1),
    )
    guard = BrowserNetworkGuard(policy)

    guard.authorize_request("https://example.com/")
    guard.authorize_request("https://cdn.example.com/landing", redirect=True)
    with pytest.raises(BrowserResourceLimitError, match="redirect"):
        guard.authorize_request("https://example.com/final", redirect=True)

    second_guard = BrowserNetworkGuard(policy)
    second_guard.authorize_request("https://example.com/")
    with pytest.raises(BrowserPolicyError, match="non-public"):
        second_guard.authorize_request("https://private.example.com/", redirect=True)
    assert second_guard.redirect_count == 0


def test_byte_and_network_request_budgets_fail_before_accounting_overflow() -> None:
    limits = BrowserSandboxLimits(max_network_requests=1, max_total_bytes=64 * 1024)
    guard = BrowserNetworkGuard(_policy(limits=limits))

    guard.authorize_request("https://example.com/")
    with pytest.raises(BrowserResourceLimitError, match="network request"):
        guard.authorize_request("https://example.com/image.png")
    guard.consume_bytes(64 * 1024)
    with pytest.raises(BrowserResourceLimitError, match="byte"):
        guard.consume_bytes(1)
    assert guard.consumed_bytes == 64 * 1024


def test_deadline_is_checked_before_each_network_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = iter((100.0, 102.0))
    monkeypatch.setattr(policy_module.time, "monotonic", lambda: next(readings))
    guard = BrowserNetworkGuard(_policy(limits=BrowserSandboxLimits(max_duration_seconds=1.0)))

    with pytest.raises(BrowserResourceLimitError, match="deadline"):
        guard.authorize_request("https://example.com/")
    assert guard.request_count == 0
