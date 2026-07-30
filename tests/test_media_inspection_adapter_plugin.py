from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.control_plane import RbacLevel, RiskLevel
from yonerai_discord.modules.media_inspection import (
    MEDIA_URL_INSPECTION_CAPABILITY_ID,
    DiscordMediaInspectionAdapter,
    GeminiMediaInspectionProvider,
    MediaInspectionResult,
    MediaInspectionPlugin,
)
from yonerai_discord.runtime_manifests.media_inspection import CAPABILITIES
from yonerai_discord.runtime_readiness import refresh_runtime_readiness
from yonerai_discord.modules.media_inspection.hyperv_contract import (
    HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
    HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
    HYPERV_MEDIA_IDENTITY_DIGEST,
    HyperVMediaExecutionResult,
    HyperVMediaProbeResult,
)


class _Content:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def iter_chunked(self, _size: int):
        yield self.body


class _Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, text: str) -> None:
        self.content = _Content(
            json.dumps(
                {
                    "status": "completed",
                    "outputs": [{"type": "model_output", "content": [{"type": "text", "text": text}]}],
                },
                ensure_ascii=False,
            ).encode()
        )

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self, text: str) -> None:
        self.response = _Response(text)
        self.posts = 0

    def post(self, _url: str, **_kwargs: Any) -> _Response:
        self.posts += 1
        return self.response

    async def close(self) -> None:
        return None


class _StructuralProvider:
    def __init__(self, consent_requirement: object = True) -> None:
        self.requires_external_ai_consent = consent_requirement

    async def inspect(self, _url: str, _instruction: str) -> MediaInspectionResult:
        return MediaInspectionResult("structural provider result")


class _ProviderWithoutConsentMetadata:
    async def inspect(self, _url: str, _instruction: str) -> MediaInspectionResult:
        return MediaInspectionResult("fail-safe provider result")


class _ProviderWithBrokenConsentMetadata:
    @property
    def requires_external_ai_consent(self) -> bool:
        raise RuntimeError("broken metadata")

    async def inspect(self, _url: str, _instruction: str) -> MediaInspectionResult:
        return MediaInspectionResult("fail-safe provider result")


class _AuditDatabase:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def append_audit(
        self,
        event: str,
        *,
        actor_id: int,
        details: dict[str, object],
        plugin: str,
        guild_id: int,
    ) -> int:
        self.records.append(
            {
                "event": event,
                "actor_id": actor_id,
                "details": details,
                "plugin": plugin,
                "guild_id": guild_id,
            }
        )
        return len(self.records)


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=2),
        author=SimpleNamespace(id=3),
        id=4,
    )


@pytest.mark.asyncio
async def test_adapter_returns_text_after_pre_and_post_authorization_without_replying() -> None:
    session = _Session("安全な解析結果")
    provider = GeminiMediaInspectionProvider(
        api_key="test-api-key",
        call_reserver=lambda: True,
        session_factory=lambda **_kwargs: session,
    )
    adapter = DiscordMediaInspectionAdapter(provider)
    checks = 0

    async def authorized() -> bool:
        nonlocal checks
        checks += 1
        return True

    text = await adapter.inspect_for_message(
        _message(),
        "https://youtu.be/ABCDEFGHIJK",
        "内容を説明して",
        authorized,
    )

    assert text == "安全な解析結果"
    assert checks == 2
    assert session.posts == 1
    await provider.close()


@pytest.mark.asyncio
async def test_adapter_accepts_structural_provider_and_can_mark_managed_backend_local() -> None:
    provider = _StructuralProvider(False)
    adapter = DiscordMediaInspectionAdapter(provider)

    result = await adapter.inspect_for_message(
        _message(),
        "https://youtu.be/ABCDEFGHIJK",
        "内容を説明して",
        lambda: True,
    )

    assert adapter.requires_external_ai_consent is False
    assert result == "structural provider result"


@pytest.mark.parametrize(
    "provider",
    [
        _ProviderWithoutConsentMetadata(),
        _ProviderWithBrokenConsentMetadata(),
        _StructuralProvider(0),
        _StructuralProvider(None),
    ],
)
def test_adapter_consent_requirement_fails_safe(provider: object) -> None:
    adapter = DiscordMediaInspectionAdapter(provider)  # type: ignore[arg-type]

    assert adapter.requires_external_ai_consent is True


def test_gemini_provider_always_requires_external_ai_consent() -> None:
    provider = GeminiMediaInspectionProvider(
        api_key="test-api-key",
        call_reserver=lambda: True,
    )

    assert provider.requires_external_ai_consent is True
    with pytest.raises(AttributeError):
        provider.requires_external_ai_consent = False  # type: ignore[misc]


@pytest.mark.asyncio
async def test_adapter_revocation_after_provider_suppresses_result() -> None:
    session = _Session("外へ返さない結果")
    provider = GeminiMediaInspectionProvider(
        api_key="test-api-key",
        call_reserver=lambda: True,
        session_factory=lambda **_kwargs: session,
    )
    adapter = DiscordMediaInspectionAdapter(provider)
    decisions = iter((True, False))

    result = await adapter.inspect_for_message(
        _message(),
        "https://youtu.be/ABCDEFGHIJK",
        "内容を説明して",
        lambda: next(decisions),
    )

    assert result is None
    assert session.posts == 1
    await provider.close()


@pytest.mark.asyncio
async def test_adapter_denial_before_provider_makes_no_post() -> None:
    session = _Session("unused")
    provider = GeminiMediaInspectionProvider(
        api_key="test-api-key",
        call_reserver=lambda: True,
        session_factory=lambda **_kwargs: session,
    )
    adapter = DiscordMediaInspectionAdapter(provider)

    result = await adapter.inspect_for_message(
        _message(),
        "https://youtu.be/ABCDEFGHIJK",
        "内容を説明して",
        lambda: False,
    )

    assert result is None
    assert session.posts == 0
    await provider.close()


@pytest.mark.asyncio
async def test_plugin_is_not_ready_without_remote_opt_in_and_key() -> None:
    bot = SimpleNamespace(settings=SimpleNamespace())
    plugin = MediaInspectionPlugin()

    await plugin.start(bot)

    assert bot.media_url_inspection_provider is None
    assert bot.media_url_inspection_adapter is None
    assert bot.runtime_capability_readiness[MEDIA_URL_INSPECTION_CAPABILITY_ID] is False
    await plugin.stop()


@pytest.mark.asyncio
async def test_plugin_publishes_ready_only_with_valid_configuration() -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            media_url_inspection_allow_remote=True,
            media_url_inspection_api_key="configured-api-key",
            media_url_inspection_timeout_seconds=60.0,
            media_url_inspection_max_response_bytes=256 * 1024,
            media_url_inspection_daily_call_limit=2,
        ),
        database=SimpleNamespace(reserve_media_url_inspection_call=lambda _limit: True),
    )
    plugin = MediaInspectionPlugin()

    await plugin.start(bot)

    assert isinstance(bot.media_url_inspection_provider, GeminiMediaInspectionProvider)
    assert isinstance(bot.media_url_inspection_adapter, DiscordMediaInspectionAdapter)
    assert bot.runtime_capability_readiness[MEDIA_URL_INSPECTION_CAPABILITY_ID] is True
    await plugin.stop()
    assert MEDIA_URL_INSPECTION_CAPABILITY_ID not in bot.runtime_capability_readiness


@pytest.mark.asyncio
async def test_plugin_prefers_ready_hyperv_without_remote_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LocalProvider:
        requires_external_ai_consent = False

        def __init__(self, *, project_root: Path, timeout_seconds: float) -> None:
            assert project_root == Path.cwd().resolve(strict=True)
            assert timeout_seconds == 45.0
            self.ready = True
            self.closed = False
            self.inspections = 0
            self.attestation: HyperVMediaProbeResult | HyperVMediaExecutionResult | None = None

        async def probe(self) -> HyperVMediaProbeResult:
            receipt = HyperVMediaProbeResult(
                True,
                True,
                HYPERV_MEDIA_IDENTITY_DIGEST,
                HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
                HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
            )
            self.attestation = receipt
            return receipt

        async def inspect(self, _url: str, _instruction: str) -> MediaInspectionResult:
            self.inspections += 1
            self.attestation = HyperVMediaExecutionResult(
                "local",
                True,
                HYPERV_MEDIA_IDENTITY_DIGEST,
                HYPERV_MEDIA_EFFECTIVE_POLICY_REVISION,
                HYPERV_MEDIA_EFFECTIVE_POLICY_DIGEST,
            )
            return MediaInspectionResult("local")

        def begin_close(self) -> None:
            self.ready = False

        async def close(self) -> None:
            self.closed = True
            self.ready = False

    monkeypatch.setattr(
        "yonerai_discord.modules.media_inspection.plugin.HyperVMediaInspectionProvider",
        LocalProvider,
    )
    database = _AuditDatabase()
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            media_url_inspection_use_hyperv=True,
            media_url_inspection_hyperv_timeout_seconds=45.0,
        ),
        database=database,
    )
    plugin = MediaInspectionPlugin()

    await plugin.start(bot)

    adapter = bot.media_url_inspection_adapter
    assert adapter.requires_external_ai_consent is False
    assert bot.runtime_capability_readiness[MEDIA_URL_INSPECTION_CAPABILITY_ID] is True
    assert bot.media_capability_broker is not None
    text = await bot.media_url_inspection_adapter.inspect_for_message(
        _message(),
        "https://youtu.be/ABCDEFGHIJK",
        "inspect only the public clip",
        lambda: True,
    )
    assert text == "local"
    rendered_audit = repr(database.records)
    assert "youtu.be" not in rendered_audit
    assert "inspect only the public clip" not in rendered_audit
    assert "local" not in rendered_audit
    provider = bot.media_url_inspection_provider
    bot.media_url_inspection_adapter = object()
    assert refresh_runtime_readiness(bot, MEDIA_URL_INSPECTION_CAPABILITY_ID) is False
    assert (
        await adapter.inspect_for_message(
            _message(),
            "https://youtu.be/ABCDEFGHIJK",
            "inspect the public clip after replacement",
            lambda: True,
        )
        is None
    )
    assert provider.inspections == 1
    bot.media_url_inspection_adapter = adapter
    bot.database = _AuditDatabase()
    assert refresh_runtime_readiness(bot, MEDIA_URL_INSPECTION_CAPABILITY_ID) is False
    assert (
        await adapter.inspect_for_message(
            _message(),
            "https://youtu.be/ABCDEFGHIJK",
            "inspect the public clip after database replacement",
            lambda: True,
        )
        is None
    )
    assert provider.inspections == 1
    await plugin.stop()
    assert provider.closed is True
    assert bot.media_capability_broker is None


@pytest.mark.asyncio
async def test_failed_hyperv_probe_does_not_fall_back_to_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedLocalProvider:
        requires_external_ai_consent = False

        def __init__(self, *, project_root: Path, timeout_seconds: float) -> None:
            self.ready = False
            self.closed = False
            self.attestation = None

        async def probe(self) -> HyperVMediaProbeResult:
            return None  # type: ignore[return-value]

        def begin_close(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "yonerai_discord.modules.media_inspection.plugin.HyperVMediaInspectionProvider",
        FailedLocalProvider,
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            media_url_inspection_use_hyperv=True,
            media_url_inspection_hyperv_timeout_seconds=30.0,
            media_url_inspection_allow_remote=True,
            media_url_inspection_api_key="must-not-be-used",
            media_url_inspection_timeout_seconds=60.0,
            media_url_inspection_max_response_bytes=256 * 1024,
            media_url_inspection_daily_call_limit=2,
        ),
        database=_AuditDatabase(),
    )
    plugin = MediaInspectionPlugin()

    await plugin.start(bot)

    assert bot.media_url_inspection_provider is None
    assert bot.media_url_inspection_adapter is None
    assert bot.runtime_capability_readiness[MEDIA_URL_INSPECTION_CAPABILITY_ID] is False
    await plugin.stop()


def test_runtime_manifest_is_default_off_owner_only_high_risk() -> None:
    assert len(CAPABILITIES) == 1
    capability = CAPABILITIES[0]
    assert capability.capability_id == MEDIA_URL_INSPECTION_CAPABILITY_ID
    assert capability.module_id == "media.url-inspection"
    assert capability.plugin == "media_inspection"
    assert capability.level is RbacLevel.BOT_OWNER
    assert capability.risk is RiskLevel.HIGH
    assert capability.default_enabled is False
    assert capability.owner_only is True
