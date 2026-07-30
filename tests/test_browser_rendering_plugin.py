from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.browser_sandbox.models import BrowserOutput, BrowserOutputKind, BrowserSessionResult
from yonerai_discord.browser_sandbox.policy import BrowserSandboxLimits, BrowserSandboxPolicy, StaticDnsResolver
from yonerai_discord.browser_sandbox.remote_service import RemoteBrowserScreenshotService
from yonerai_discord.modules.browser_rendering.discord_adapter import DiscordRemoteBrowserScreenshotAdapter
from yonerai_discord.modules.browser_rendering.plugin import (
    REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID,
    REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID,
    BrowserRenderingPlugin,
)
from yonerai_discord.modules.media_pipeline.artifacts import MediaArtifactStore
from yonerai_discord.modules.web_runtime.search import WebBackendAvailability
from yonerai_discord.plugin import PluginManager, discover_plugins
from yonerai_discord.plugin_manifest import BUILTIN_PLUGIN_MANIFEST
from yonerai_discord.runtime_readiness import refresh_runtime_readiness


_PNG = b"\x89PNG\r\n\x1a\nfixture"


class _Provider:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.calls = 0

    async def start(self) -> None:
        self.started += 1

    async def close(self) -> None:
        self.closed += 1

    async def capture_screenshot(self, request, *, policy):
        del request, policy
        self.calls += 1
        return BrowserSessionResult((BrowserOutput(1, BrowserOutputKind.SCREENSHOT, _PNG, "image/png"),))


class _Sink:
    def __init__(self) -> None:
        self.outputs: list[object] = []

    async def send_screenshot(self, message: object, output: object) -> None:
        del message
        self.outputs.append(output)


class _Database:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.fail_at = fail_at

    def append_audit(self, event: str, **kwargs: object) -> int:
        if self.fail_at == len(self.events) + 1:
            raise RuntimeError("audit unavailable")
        self.events.append((event, dict(kwargs)))
        return len(self.events)


def _message() -> Any:
    return SimpleNamespace(
        id=4,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=2),
        author=SimpleNamespace(id=3),
    )


async def _adapter(*, database: _Database, provider: _Provider | None = None):
    provider = provider or _Provider()
    service = RemoteBrowserScreenshotService(
        policy=BrowserSandboxPolicy(
            resolver=StaticDnsResolver({"example.com": ("1.1.1.1",)}),
            limits=BrowserSandboxLimits(max_duration_seconds=1, max_total_bytes=64 * 1024),
        ),
        provider=provider,
    )
    await service.start()
    sink = _Sink()
    adapter = DiscordRemoteBrowserScreenshotAdapter(service, sink)
    adapter.bind_bot(SimpleNamespace(database=database))
    return adapter, provider, sink, service


@pytest.mark.asyncio
async def test_adapter_audits_before_remote_call_and_before_delivery() -> None:
    adapter, provider, sink, service = await _adapter(database=_Database())
    try:
        assert await adapter.capture_for_message(
            _message(), url="https://example.com/path?key=public-navigation", authorization_current=lambda: True
        )
        assert provider.calls == 1
        assert len(sink.outputs) == 1
        events = adapter._bot.database.events
        assert [item[0] for item in events] == ["browser_rendering.requested", "browser_rendering.completed"]
        assert all("url" not in item[1]["details"] for item in events)
        assert events[0][1]["details"]["target_kind"] == "url"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_adapter_blocks_remote_call_when_request_audit_fails() -> None:
    adapter, provider, sink, service = await _adapter(database=_Database(fail_at=1))
    try:
        assert not await adapter.capture_for_message(
            _message(), url="https://example.com/", authorization_current=lambda: True
        )
        assert provider.calls == 0
        assert sink.outputs == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_adapter_holds_delivery_when_completion_audit_fails() -> None:
    adapter, provider, sink, service = await _adapter(database=_Database(fail_at=2))
    try:
        assert not await adapter.capture_for_message(
            _message(), url="https://example.com/", authorization_current=lambda: True
        )
        assert provider.calls == 1
        assert sink.outputs == []
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    (
        "https://example.com/#fragment",
        "https://user@example.com/",
        "https://example.com/?access_token=never-send",
        "https://example.com/?api-key=never-send",
        "https://example.com/?my_secret=never-send",
        "https://example.com/?X-Amz-Signature=never-send",
        "https://example.com/?X-Goog-Signature=never-send",
        "https://example.com/?oauth_code=never-send",
        "https://example.com/?private_key=never-send",
    ),
)
async def test_adapter_rejects_credential_like_or_ambiguous_urls_before_audit(url: str) -> None:
    database = _Database()
    adapter, provider, sink, service = await _adapter(database=database)
    try:
        assert not await adapter.capture_for_message(_message(), url=url, authorization_current=lambda: True)
        assert provider.calls == 0
        assert sink.outputs == []
        assert database.events == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_adapter_rechecks_authorization_before_remote_egress() -> None:
    adapter, provider, sink, service = await _adapter(database=_Database())
    try:
        assert not await adapter.capture_for_message(
            _message(), url="https://example.com/", authorization_current=lambda: False
        )
        assert provider.calls == 0
        assert sink.outputs == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_adapter_idempotency_keeps_one_remote_call_and_one_delivery_per_message_target() -> None:
    adapter, provider, sink, service = await _adapter(database=_Database())
    try:
        message = _message()
        assert await adapter.capture_for_message(
            message, url="https://example.com/first", authorization_current=lambda: True
        )
        assert await adapter.capture_for_message(
            message, url="https://example.com/first", authorization_current=lambda: True
        )
        assert provider.calls == 1
        assert len(sink.outputs) == 1
        assert [event for event, _details in adapter._bot.database.events] == [
            "browser_rendering.requested",
            "browser_rendering.completed",
        ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_adapter_same_message_can_capture_multiple_distinct_targets_without_retaining_raw_urls() -> None:
    adapter, provider, sink, service = await _adapter(database=_Database())
    try:
        message = _message()
        first_url = "https://example.com/first"
        second_url = "https://example.com/second"
        assert await adapter.capture_for_message(message, url=first_url, authorization_current=lambda: True)
        assert await adapter.capture_for_message(message, url=second_url, authorization_current=lambda: True)

        assert provider.calls == 2
        assert len(sink.outputs) == 2
        assert [event for event, _details in adapter._bot.database.events] == [
            "browser_rendering.requested",
            "browser_rendering.completed",
            "browser_rendering.requested",
            "browser_rendering.completed",
        ]
        keys = tuple(adapter._terminal)
        assert len(keys) == 2
        assert all(len(key) == 3 and isinstance(key[2], bytes) and len(key[2]) == 32 for key in keys)
        serialized_keys = repr(keys)
        assert first_url not in serialized_keys
        assert second_url not in serialized_keys
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_plugin_unconfigured_is_inert_and_withdraws_readiness() -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(cloudflare_browser_rendering_allow_remote=False),
        runtime_capability_readiness={},
    )
    plugin = BrowserRenderingPlugin()
    await plugin.start(bot)
    assert bot.runtime_capability_readiness[REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID] is False
    assert bot.runtime_capability_readiness[REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID] is False
    assert bot.remote_browser_screenshot_service is None
    assert bot.browser_rendering_adapter is None
    await plugin.stop()
    assert bot.runtime_capability_readiness == {}


@pytest.mark.asyncio
async def test_plugin_configured_lifecycle_publishes_only_with_durable_audit(monkeypatch) -> None:
    provider = _Provider()
    monkeypatch.setattr(
        "yonerai_discord.modules.browser_rendering.plugin.CloudflareQuickActionsScreenshotProvider",
        lambda **_kwargs: provider,
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            cloudflare_browser_rendering_allow_remote=True,
            cloudflare_browser_rendering_account_id="a" * 32,
            cloudflare_browser_rendering_api_token="token",
            cloudflare_browser_rendering_timeout_seconds=0.1,
            cloudflare_browser_rendering_max_output_bytes=1 * 1024 * 1024,
        ),
        database=_Database(),
        runtime_capability_readiness={},
        remote_browser_screenshot_sink=_Sink(),
    )
    plugin = BrowserRenderingPlugin()
    await plugin.start(bot)
    assert provider.started == 1
    assert bot.runtime_capability_readiness[REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID] is True
    assert bot.runtime_capability_readiness[REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID] is False
    assert bot.remote_browser_screenshot_service is plugin.service
    assert bot.browser_rendering_adapter is plugin.adapter
    await plugin.stop()
    assert provider.closed == 1
    assert bot.runtime_capability_readiness == {}
    assert bot.remote_browser_screenshot_service is None
    assert bot.browser_rendering_adapter is None


@pytest.mark.asyncio
async def test_plugin_invalid_credentials_stay_unconfigured_without_crashing() -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            cloudflare_browser_rendering_allow_remote=True,
            cloudflare_browser_rendering_account_id="not-an-account",
            cloudflare_browser_rendering_api_token="",
        ),
        runtime_capability_readiness={},
    )
    plugin = BrowserRenderingPlugin()
    await plugin.start(bot)
    assert bot.runtime_capability_readiness[REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID] is False
    assert bot.runtime_capability_readiness[REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID] is False
    assert bot.remote_browser_screenshot_service is None
    await plugin.stop()


@pytest.mark.asyncio
async def test_plugin_partial_provider_start_is_cleaned_without_leaking_bot_attrs(monkeypatch) -> None:
    class FailingProvider(_Provider):
        async def start(self) -> None:
            self.started += 1
            raise RuntimeError("start failed")

    provider = FailingProvider()
    monkeypatch.setattr(
        "yonerai_discord.modules.browser_rendering.plugin.CloudflareQuickActionsScreenshotProvider",
        lambda **_kwargs: provider,
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            cloudflare_browser_rendering_allow_remote=True,
            cloudflare_browser_rendering_account_id="a" * 32,
            cloudflare_browser_rendering_api_token="token",
            cloudflare_browser_rendering_timeout_seconds=1.0,
            cloudflare_browser_rendering_max_output_bytes=1 * 1024 * 1024,
        ),
        database=_Database(),
        runtime_capability_readiness={},
    )
    plugin = BrowserRenderingPlugin()

    with pytest.raises(Exception, match="failed to start"):
        await plugin.start(bot)

    assert provider.closed == 1
    assert bot.remote_browser_screenshot_service is None
    assert bot.browser_rendering_adapter is None
    assert bot.runtime_capability_readiness == {}


@pytest.mark.asyncio
async def test_plugin_cancelled_stop_withdraws_public_state_and_retries_provider_cleanup(monkeypatch) -> None:
    class CancelOnceProvider(_Provider):
        async def close(self) -> None:
            self.closed += 1
            if self.closed == 1:
                raise asyncio.CancelledError

    provider = CancelOnceProvider()
    monkeypatch.setattr(
        "yonerai_discord.modules.browser_rendering.plugin.CloudflareQuickActionsScreenshotProvider",
        lambda **_kwargs: provider,
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            cloudflare_browser_rendering_allow_remote=True,
            cloudflare_browser_rendering_account_id="a" * 32,
            cloudflare_browser_rendering_api_token="token",
            cloudflare_browser_rendering_timeout_seconds=1.0,
            cloudflare_browser_rendering_max_output_bytes=1 * 1024 * 1024,
        ),
        database=_Database(),
        runtime_capability_readiness={},
    )
    plugin = BrowserRenderingPlugin()
    await plugin.start(bot)
    with pytest.raises(asyncio.CancelledError):
        await plugin.stop()
    assert bot.runtime_capability_readiness == {}
    assert bot.remote_browser_screenshot_service is None
    assert bot.browser_rendering_adapter is None
    assert plugin.service is not None
    assert plugin._bot is bot
    assert bot.remote_browser_rendering_status.configured is True
    assert bot.remote_browser_rendering_status.ready is False
    assert bot.remote_browser_rendering_status.detail == "停止処理未完了"

    await plugin.stop()
    assert provider.closed == 2
    assert plugin.service is None
    assert plugin._bot is None
    assert bot.remote_browser_rendering_status.detail == "停止"


@pytest.mark.asyncio
async def test_plugin_interactive_lifecycle_is_separate_and_requires_media_store(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _Factory:
        availability = WebBackendAvailability(True, None, "local dependency ready")
        configured = True

        def create(self, **_kwargs: object) -> object:
            raise AssertionError("startup must not connect to Cloudflare")

    monkeypatch.setattr(
        "yonerai_discord.modules.browser_rendering.plugin.CloudflareBrowserRunSessionFactory",
        lambda **_kwargs: _Factory(),
    )
    root = tmp_path / "artifacts"
    root.mkdir()
    store = MediaArtifactStore(root, database_path=tmp_path / "index.sqlite3")
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            cloudflare_browser_rendering_allow_remote=False,
            cloudflare_browser_run_interactive_enabled=True,
            cloudflare_browser_rendering_account_id="a" * 32,
            cloudflare_browser_rendering_api_token="token",
            cloudflare_browser_rendering_timeout_seconds=45.0,
        ),
        database=_Database(),
        media_pipeline_store=store,
        runtime_capability_readiness={},
    )
    plugin = BrowserRenderingPlugin()
    try:
        await plugin.start(bot)
        assert bot.runtime_capability_readiness[REMOTE_BROWSER_SCREENSHOT_CAPABILITY_ID] is False
        assert bot.runtime_capability_readiness[REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID] is True
        assert bot.remote_browser_screenshot_service is None
        assert bot.browser_rendering_adapter is None
        assert bot.remote_browser_run_service is plugin.interactive_runner
        assert bot.browser_run_adapter is plugin.interactive_adapter
        assert bot.remote_browser_interactive_status.ready is True

        bot.media_pipeline_store = object()
        assert refresh_runtime_readiness(bot, REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID) is False
        bot.media_pipeline_store = store
        assert refresh_runtime_readiness(bot, REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID) is True
        await plugin.begin_close()
        assert refresh_runtime_readiness(bot, REMOTE_BROWSER_INTERACTIVE_CAPABILITY_ID) is False
        await plugin.stop()
        assert bot.runtime_capability_readiness == {}
        assert bot.remote_browser_run_service is None
        assert bot.browser_run_adapter is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_plugin_does_not_overwrite_foreign_public_state() -> None:
    foreign = object()
    bot = SimpleNamespace(
        settings=SimpleNamespace(cloudflare_browser_rendering_allow_remote=False),
        runtime_capability_readiness={},
        browser_run_adapter=foreign,
    )
    plugin = BrowserRenderingPlugin()

    with pytest.raises(RuntimeError, match="already owned"):
        await plugin.start(bot)

    assert bot.browser_run_adapter is foreign
    assert plugin._bot is None
    assert bot.runtime_capability_readiness == {}


def test_browser_rendering_is_discoverable_from_builtin_manifest() -> None:
    manager = PluginManager()

    discover_plugins(
        manager,
        "yonerai_discord.modules",
        manifest=BUILTIN_PLUGIN_MANIFEST,
    )

    assert manager.status("browser_rendering").value == "registered"
