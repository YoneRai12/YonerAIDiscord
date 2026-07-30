from __future__ import annotations

import pytest

from yonerai_discord.discord_markdown import numbered_link, numbered_reference


def test_numbered_link_hides_raw_url_behind_number() -> None:
    rendered = numbered_link(1, "https://example.com/path?q=one")
    assert rendered == "[1](https://example.com/path?q=one)"
    assert not rendered.startswith("https://")


def test_numbered_link_escapes_parentheses_and_rejects_unsafe_urls() -> None:
    assert numbered_link(2, "https://example.com/a_(b)") == "[2](https://example.com/a_%28b%29)"
    with pytest.raises(ValueError):
        numbered_link(1, "javascript:alert(1)")
    with pytest.raises(ValueError):
        numbered_link(1, "https://user:pass@example.com/")
    with pytest.raises(ValueError):
        numbered_link(1, "https://example.com/\nspoof")


def test_numbered_reference_escapes_untrusted_markdown_and_mentions() -> None:
    rendered = numbered_reference(3, "https://example.com/", "[偽](https://evil.test) @everyone")
    assert rendered.startswith("[3](https://example.com/) ")
    assert "\\[偽\\]\\(https://evil\\.test\\)" in rendered
    assert "@everyone" not in rendered
