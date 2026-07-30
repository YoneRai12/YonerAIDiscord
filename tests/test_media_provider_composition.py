from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import yonerai_discord.modules.speech_synthesis.plugin as speech_synthesis_plugin_module
from yonerai_discord.config import ConfigurationError, Settings
from yonerai_discord.modules.media_provider_composition import (
    BoundSpeechAudioResolver,
    MediaProviderCompositionCleanupError,
    MediaProviderConfiguration,
    SQLiteMediaProviderAuditSink,
    acquire_media_provider_runtime,
    compose_media_provider_runtime,
    compose_media_provider_runtime_from_settings,
    media_provider_runtime_current,
    release_media_provider_runtime,
)
from yonerai_discord.modules.music_generation.artifacts import MusicArtifactStore
from yonerai_discord.modules.music_generation.domain import MusicGenerationRequest
from yonerai_discord.modules.speech_synthesis.domain import SPEECH_SYNTHESIS_CAPABILITY_ID
from yonerai_discord.modules.music_generation.plugin import MusicGenerationPlugin
from yonerai_discord.modules.speech_synthesis.plugin import SpeechSynthesisPlugin
from yonerai_discord.modules.speech_transcription.artifacts import BoundedSpeechAudioStore
from yonerai_discord.modules.speech_transcription.plugin import SpeechTranscriptionPlugin
from yonerai_discord.modules.video_generation.artifacts import VideoArtifactStore
from yonerai_discord.modules.video_generation.plugin import VideoGenerationPlugin
from yonerai_discord.provider_registry import (
    AuditOutcome,
    HealthStatus,
    LogicalCapability,
    MediaGenerationInput,
    ProviderInvocation,
    ProviderRequest,
    QualityTier,
    ReadinessCode,
    SpeechTranscriptionInput,
)
from yonerai_discord.provider_registry.domain import AuditRecord, utc_now
from yonerai_discord.runtime_readiness import refresh_runtime_readiness


class _Database:
    def __init__(self) -> None:
        self.rows: list[tuple[str, int, dict[str, object], str | None, int | str | None]] = []

    def append_audit(
        self,
        event: str,
        *,
        actor_id: int,
        details: dict[str, object] | None = None,
        plugin: str | None = None,
        guild_id: int | str | None = None,
    ) -> int:
        self.rows.append((event, actor_id, dict(details or {}), plugin, guild_id))
        return len(self.rows)


class _SttTransport:
    def __init__(self, *, probe_ready: bool = True) -> None:
        self.closed = False
        self.probe_ready = probe_ready

    async def probe_model(self, _model: str, *, timeout_seconds: float) -> bool:
        assert timeout_seconds == 5.0
        return self.probe_ready

    async def transcribe(self, **_kwargs: object) -> str:
        return "文字起こし"

    async def close(self) -> None:
        self.closed = True


class _BlockingSttTransport(_SttTransport):
    def __init__(self) -> None:
        super().__init__()
        self.probe_started = asyncio.Event()

    async def probe_model(self, _model: str, *, timeout_seconds: float) -> bool:
        assert timeout_seconds == 5.0
        self.probe_started.set()
        await asyncio.Event().wait()
        return True


class _VoicevoxClient:
    def __init__(self, *, probe_ready: bool = True) -> None:
        self.closed = False
        self.probe_ready = probe_ready

    async def probe_version(self, *, timeout_seconds: float) -> bool:
        assert timeout_seconds == 5.0
        return self.probe_ready

    async def synthesize(self, _request: object) -> object:
        raise AssertionError("health composition must not synthesize")

    async def close(self) -> None:
        self.closed = True


class _RetryingVoicevoxClient(_VoicevoxClient):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("synthetic close failure")
        await super().close()


class _MusicTransport:
    def __init__(self, *, probe_ready: bool = True) -> None:
        self.closed = False
        self.probe_ready = probe_ready

    async def probe_model(self, _model: str, *, timeout_seconds: float) -> bool:
        assert timeout_seconds == 5.0
        return self.probe_ready

    async def compose_instrumental(self, **_kwargs: object) -> bytes:
        raise AssertionError("health composition must not generate music")

    async def close(self) -> None:
        self.closed = True


class _VideoTransport:
    def __init__(self, *, probe_ready: bool = True) -> None:
        self.closed = False
        self.probe_ready = probe_ready

    async def probe_model(self, _model: str, *, timeout_seconds: float) -> bool:
        assert timeout_seconds == 5.0
        return self.probe_ready

    async def generate_video(self, **_kwargs: object) -> bytes:
        raise AssertionError("health composition must not generate video")

    async def close(self) -> None:
        self.closed = True


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def add_command(self, command: object) -> None:
        name = getattr(command, "name")
        if name in self.commands:
            raise RuntimeError("command is already registered")
        self.commands[name] = command

    def remove_command(self, name: str) -> None:
        self.commands.pop(name, None)


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    values = tuple(tmp_path / name for name in ("tts", "music", "video"))
    for value in values:
        value.mkdir()
    return values


def _all_configuration(tmp_path: Path) -> MediaProviderConfiguration:
    tts, music, video = _roots(tmp_path)
    return MediaProviderConfiguration(
        stt_openai_enabled=True,
        stt_openai_api_key="openai-test-key",
        tts_voicevox_enabled=True,
        tts_artifact_root=tts,
        music_eleven_enabled=True,
        music_eleven_api_key="eleven-test-key",
        music_artifact_root=music,
        video_veo_enabled=True,
        video_veo_api_key="gemini-test-key",
        video_artifact_root=video,
    )


@pytest.mark.asyncio
async def test_disabled_configuration_creates_no_runtime_or_transport() -> None:
    called = False

    def transport_factory(_key: str) -> _SttTransport:
        nonlocal called
        called = True
        return _SttTransport()

    result = await compose_media_provider_runtime(
        MediaProviderConfiguration(),
        _Database(),
        runtime_current=lambda: True,
        stt_transport_factory=transport_factory,
    )

    assert result is None
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["credential", "root", "voicevox"])
async def test_enabled_provider_missing_required_injection_is_fail_closed(tmp_path: Path, missing: str) -> None:
    tts, _music, _video = _roots(tmp_path)
    configuration = {
        "credential": MediaProviderConfiguration(stt_openai_enabled=True),
        "root": MediaProviderConfiguration(music_eleven_enabled=True, music_eleven_api_key="key"),
        "voicevox": MediaProviderConfiguration(tts_voicevox_enabled=True, tts_artifact_root=tts),
    }[missing]

    result = await compose_media_provider_runtime(
        configuration,
        _Database(),
        runtime_current=lambda: True,
        voicevox_client_factory=None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_all_explicit_providers_compose_with_isolated_stores_and_routes(tmp_path: Path) -> None:
    stt = _SttTransport()
    voicevox = _VoicevoxClient()
    music = _MusicTransport()
    video = _VideoTransport()
    runtime = await compose_media_provider_runtime(
        _all_configuration(tmp_path),
        _Database(),
        runtime_current=lambda: True,
        stt_transport_factory=lambda _key: stt,
        voicevox_client_factory=lambda: voicevox,
        music_transport_factory=lambda _key: music,
        video_transport_factory=lambda _key: video,
    )

    assert runtime is not None
    assert runtime.ready is True
    assert set(runtime.capabilities) == {
        LogicalCapability.SPEECH_STT,
        LogicalCapability.SPEECH_TTS,
        LogicalCapability.MUSIC_GENERATION,
        LogicalCapability.VIDEO_GENERATION,
    }
    assert len({id(store) for store in runtime.stores.values()}) == 4
    for capability in runtime.capabilities:
        resolution = runtime.registry.resolve(
            capability,
            actor_level="trusted",
            quality_tier=QualityTier.BALANCED,
            consent_verified=True,
        )
        assert resolution.ready is True
        assert resolution.code is ReadinessCode.READY
        assert runtime.ready_for(capability) is True
        provider_id = resolution.provider_id
        assert provider_id is not None
        assert runtime.registry.health_snapshot(provider_id).status is HealthStatus.READY
        manifest = runtime.registry.manifest.provider(provider_id)
        assert manifest is not None
        assert all(model.probe_required is True for model in manifest.models)


@pytest.mark.asyncio
async def test_probe_failure_keeps_runtime_unavailable_until_safe_close(tmp_path: Path) -> None:
    stt = _SttTransport(probe_ready=False)
    runtime = await compose_media_provider_runtime(
        MediaProviderConfiguration(stt_openai_enabled=True, stt_openai_api_key="key"),
        _Database(),
        runtime_current=lambda: True,
        stt_transport_factory=lambda _key: stt,
    )

    assert runtime is not None
    assert runtime.ready is False
    assert runtime.ready_for(LogicalCapability.SPEECH_STT) is False
    assert stt.closed is False

    await runtime.close()

    assert stt.closed is True


@pytest.mark.asyncio
async def test_compose_cancellation_completes_staged_cleanup() -> None:
    stt = _BlockingSttTransport()
    task = asyncio.create_task(
        compose_media_provider_runtime(
            MediaProviderConfiguration(stt_openai_enabled=True, stt_openai_api_key="key"),
            _Database(),
            runtime_current=lambda: True,
            stt_transport_factory=lambda _key: stt,
        )
    )
    await stt.probe_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stt.closed is True


@pytest.mark.asyncio
async def test_runtime_current_false_prevents_readiness(tmp_path: Path) -> None:
    current = True
    tts, _music, _video = _roots(tmp_path)
    runtime = await compose_media_provider_runtime(
        MediaProviderConfiguration(tts_voicevox_enabled=True, tts_artifact_root=tts),
        _Database(),
        runtime_current=lambda: current,
        voicevox_client_factory=_VoicevoxClient,
    )
    assert runtime is not None
    assert runtime.ready is True

    current = False

    assert runtime.ready is False
    assert (await runtime.registry.refresh_health("voicevox-local")).status is HealthStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_begin_close_revokes_store_and_close_unregisters_exact_adapters(tmp_path: Path) -> None:
    stt = _SttTransport()
    voicevox = _VoicevoxClient()
    music = _MusicTransport()
    video = _VideoTransport()
    runtime = await compose_media_provider_runtime(
        _all_configuration(tmp_path),
        _Database(),
        runtime_current=lambda: True,
        stt_transport_factory=lambda _key: stt,
        voicevox_client_factory=lambda: voicevox,
        music_transport_factory=lambda _key: music,
        video_transport_factory=lambda _key: video,
    )
    assert runtime is not None
    stt_store = runtime.stores[LogicalCapability.SPEECH_STT]

    runtime.begin_close()
    await runtime.close()
    await runtime.close()

    assert runtime.ready is False
    assert runtime.closed is True
    assert stt_store._closing is True
    assert stt.closed and voicevox.closed and music.closed and video.closed
    for provider_id in runtime.adapters:
        assert runtime.registry.health_snapshot(provider_id) is None


@pytest.mark.asyncio
async def test_close_failure_keeps_identity_for_bounded_retry(tmp_path: Path) -> None:
    tts, _music, _video = _roots(tmp_path)
    voicevox = _RetryingVoicevoxClient()
    runtime = await compose_media_provider_runtime(
        MediaProviderConfiguration(tts_voicevox_enabled=True, tts_artifact_root=tts),
        _Database(),
        runtime_current=lambda: True,
        voicevox_client_factory=lambda: voicevox,
    )
    assert runtime is not None

    with pytest.raises(RuntimeError, match="shutdown failed safely"):
        await runtime.close()
    assert runtime.registry.health_snapshot("voicevox-local") is not None

    await runtime.close()

    assert runtime.closed is True
    assert voicevox.close_calls == 2
    assert runtime.registry.health_snapshot("voicevox-local") is None


def test_bound_stt_resolver_requires_full_service_fingerprint() -> None:
    store = BoundedSpeechAudioStore()
    data = _wav(seconds=1)
    ref = store.describe_wav(data, artifact_id="stt-" + ("a" * 32))
    binding = "b" * 64
    store.put_wav(ref, data, request_binding=binding)
    resolver = BoundSpeechAudioResolver(store, runtime_current=lambda: True)
    request = ProviderRequest(
        request_id=f"stt-request-{binding}",
        trace_id="trace-stt",
        actor_ref="discord-user-123",
        capability=LogicalCapability.SPEECH_STT,
        quality_tier=QualityTier.BALANCED,
        model_alias="stt.balanced",
        payload=SpeechTranscriptionInput(language_code="ja", prompt=""),
        input_artifacts=(ref,),
    )

    assert resolver.read_audio(request, ref) == data
    with pytest.raises(RuntimeError, match="binding"):
        resolver.read_audio(
            ProviderRequest(
                request_id=f"stt-request-{binding[:48]}",
                trace_id="trace-stt-short",
                actor_ref="discord-user-123",
                capability=LogicalCapability.SPEECH_STT,
                quality_tier=QualityTier.BALANCED,
                model_alias="stt.balanced",
                payload=SpeechTranscriptionInput(language_code="ja", prompt=""),
                input_artifacts=(ref,),
            ),
            ref,
        )


@pytest.mark.asyncio
async def test_cancelled_audit_is_bounded_and_contains_no_sensitive_payload() -> None:
    database = _Database()
    sink = SQLiteMediaProviderAuditSink(database, runtime_current=lambda: True)

    await sink.append(
        AuditRecord(
            request_id="music-request-safe",
            trace_id="trace-safe",
            actor_ref="discord-user-123",
            capability=LogicalCapability.MUSIC_GENERATION,
            outcome=AuditOutcome.CANCELLED,
            occurred_at=utc_now(),
            quality_tier=QualityTier.BALANCED,
            provider_id="elevenlabs-api",
            model_alias="music.balanced",
            duration_ms=10,
            outcome_uncertain=True,
        )
    )

    assert len(database.rows) == 1
    event, actor_id, details, plugin, guild_id = database.rows[0]
    assert (event, actor_id, plugin, guild_id) == ("provider.cancelled", 123, "music_generation", None)
    assert set(details) == {
        "request_id",
        "trace_id",
        "capability",
        "outcome",
        "outcome_uncertain",
        "quality_tier",
        "artifact_count",
        "provider_id",
        "model_alias",
        "duration_ms",
    }
    assert details["outcome_uncertain"] is True
    serialized = repr(database.rows)
    for forbidden in ("prompt", "bytes", "path", "secret", "exception"):
        assert forbidden not in serialized.lower()


@pytest.mark.asyncio
async def test_configuration_rejects_shared_artifact_root(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    configuration = MediaProviderConfiguration(
        tts_voicevox_enabled=True,
        tts_artifact_root=root,
        music_eleven_enabled=True,
        music_eleven_api_key="key",
        music_artifact_root=root,
    )

    result = await compose_media_provider_runtime(
        configuration,
        _Database(),
        runtime_current=lambda: True,
        voicevox_client_factory=_VoicevoxClient,
        music_transport_factory=lambda _key: _MusicTransport(),
    )

    assert result is None


@pytest.mark.asyncio
async def test_configuration_rejects_same_root_through_parent_alias(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    alias = root / ".." / "shared"
    configuration = MediaProviderConfiguration(
        tts_voicevox_enabled=True,
        tts_artifact_root=root,
        music_eleven_enabled=True,
        music_eleven_api_key="key",
        music_artifact_root=alias,
    )

    result = await compose_media_provider_runtime(
        configuration,
        _Database(),
        runtime_current=lambda: True,
        voicevox_client_factory=_VoicevoxClient,
        music_transport_factory=lambda _key: _MusicTransport(),
    )

    assert result is None


class _InvalidRetryingMusicTransport:
    def __init__(self) -> None:
        self.close_calls = 0
        self.closed = False

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise RuntimeError("synthetic close failure")
        self.closed = True


@pytest.mark.asyncio
async def test_partial_constructor_cleanup_preserves_retryable_owner(tmp_path: Path) -> None:
    music_root = tmp_path / "music"
    music_root.mkdir()
    stt = _SttTransport()
    invalid = _InvalidRetryingMusicTransport()
    configuration = MediaProviderConfiguration(
        stt_openai_enabled=True,
        stt_openai_api_key="openai-key",
        music_eleven_enabled=True,
        music_eleven_api_key="music-key",
        music_artifact_root=music_root,
    )

    with pytest.raises(MediaProviderCompositionCleanupError) as raised:
        await compose_media_provider_runtime(
            configuration,
            _Database(),
            runtime_current=lambda: True,
            stt_transport_factory=lambda _key: stt,
            music_transport_factory=lambda _key: invalid,  # type: ignore[arg-type]
        )

    assert stt.closed is True
    assert invalid.close_calls == 1

    await raised.value.owner.close()

    assert invalid.close_calls == 2
    assert invalid.closed is True


@pytest.mark.asyncio
async def test_shared_runtime_is_order_independent_and_last_consumer_closes(tmp_path: Path) -> None:
    tts, _music, _video = _roots(tmp_path)
    voicevox = _VoicevoxClient()
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            tts_voicevox_enabled=True,
            tts_artifact_root=tts,
        ),
        database=_Database(),
        is_closing=False,
    )
    compose_calls = 0

    async def composer(_settings: object, database: object, runtime_current: object):
        nonlocal compose_calls
        compose_calls += 1
        assert callable(runtime_current)
        return await compose_media_provider_runtime(
            MediaProviderConfiguration(tts_voicevox_enabled=True, tts_artifact_root=tts),
            database,  # type: ignore[arg-type]
            runtime_current=runtime_current,
            voicevox_client_factory=lambda: voicevox,
        )

    first = object()
    second = object()
    runtime = await acquire_media_provider_runtime(bot, first, composer=composer)
    same_runtime = await acquire_media_provider_runtime(bot, second, composer=composer)

    assert runtime is not None and same_runtime is runtime
    assert compose_calls == 1
    assert media_provider_runtime_current(bot, runtime) is True

    await release_media_provider_runtime(bot, runtime, second)

    assert media_provider_runtime_current(bot, runtime) is True
    assert runtime.closed is False

    await release_media_provider_runtime(bot, runtime, first)

    assert runtime.closed is True
    assert voicevox.closed is True
    assert not hasattr(bot, "media_provider_runtime")


@pytest.mark.asyncio
async def test_four_plugins_share_runtime_across_start_and_stop_order(tmp_path: Path) -> None:
    tts, _music, _video = _roots(tmp_path)
    voicevox = _VoicevoxClient()
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            tts_voicevox_enabled=True,
            tts_artifact_root=tts,
        ),
        database=_Database(),
        is_closing=False,
        tree=_Tree(),
    )
    compose_calls = 0

    async def composer(_settings: object, database: object, runtime_current: object):
        nonlocal compose_calls
        compose_calls += 1
        assert callable(runtime_current)
        return await compose_media_provider_runtime(
            MediaProviderConfiguration(tts_voicevox_enabled=True, tts_artifact_root=tts),
            database,  # type: ignore[arg-type]
            runtime_current=runtime_current,
            voicevox_client_factory=lambda: voicevox,
        )

    video = VideoGenerationPlugin(runtime_composer=composer)
    transcription = SpeechTranscriptionPlugin(runtime_composer=composer)
    synthesis = SpeechSynthesisPlugin(runtime_composer=composer)
    music = MusicGenerationPlugin(runtime_composer=composer)

    for plugin in (video, transcription, synthesis, music):
        await plugin.start(bot)

    runtime = bot.media_provider_runtime
    assert compose_calls == 1
    assert all(plugin._runtime is runtime for plugin in (video, transcription, synthesis, music))

    for plugin in (synthesis, video, music):
        await plugin.stop()
        assert bot.media_provider_runtime is runtime
        assert runtime.closed is False

    await transcription.stop()

    assert runtime.closed is True
    assert voicevox.closed is True
    assert not hasattr(bot, "media_provider_runtime")


@pytest.mark.asyncio
async def test_plugin_start_failure_releases_new_shared_runtime(tmp_path: Path, monkeypatch) -> None:
    tts, _music, _video = _roots(tmp_path)
    voicevox = _VoicevoxClient()
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            tts_voicevox_enabled=True,
            tts_artifact_root=tts,
        ),
        database=_Database(),
        is_closing=False,
    )
    composed = []

    async def composer(_settings: object, database: object, runtime_current: object):
        assert callable(runtime_current)
        runtime = await compose_media_provider_runtime(
            MediaProviderConfiguration(tts_voicevox_enabled=True, tts_artifact_root=tts),
            database,  # type: ignore[arg-type]
            runtime_current=runtime_current,
            voicevox_client_factory=lambda: voicevox,
        )
        composed.append(runtime)
        return runtime

    def fail_adapter(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic adapter failure")

    monkeypatch.setattr(speech_synthesis_plugin_module, "SpeechSynthesisDelivery", fail_adapter)
    plugin = SpeechSynthesisPlugin(runtime_composer=composer)

    with pytest.raises(RuntimeError, match="synthetic adapter failure"):
        await plugin.start(bot)

    assert len(composed) == 1
    assert composed[0] is not None and composed[0].closed is True
    assert voicevox.closed is True
    assert not hasattr(bot, "media_provider_runtime")
    assert plugin._runtime is None


@pytest.mark.asyncio
async def test_plugin_stop_keeps_runtime_owner_for_bounded_retry(tmp_path: Path) -> None:
    tts, _music, _video = _roots(tmp_path)
    voicevox = _RetryingVoicevoxClient()
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            tts_voicevox_enabled=True,
            tts_artifact_root=tts,
        ),
        database=_Database(),
        is_closing=False,
    )

    async def composer(_settings: object, database: object, runtime_current: object):
        assert callable(runtime_current)
        return await compose_media_provider_runtime(
            MediaProviderConfiguration(tts_voicevox_enabled=True, tts_artifact_root=tts),
            database,  # type: ignore[arg-type]
            runtime_current=runtime_current,
            voicevox_client_factory=lambda: voicevox,
        )

    plugin = SpeechSynthesisPlugin(runtime_composer=composer)
    await plugin.start(bot)
    runtime = plugin._runtime
    assert runtime is not None

    with pytest.raises(RuntimeError, match="shutdown failed safely"):
        await plugin.stop()

    assert plugin._runtime is runtime
    assert runtime.closed is False
    assert not hasattr(bot, "media_provider_runtime")

    await plugin.stop()

    assert plugin._runtime is None
    assert runtime.closed is True
    assert voicevox.close_calls == 2


@pytest.mark.asyncio
async def test_foreign_publication_is_never_overwritten_or_deleted(tmp_path: Path) -> None:
    tts, _music, _video = _roots(tmp_path)
    foreign = object()
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            tts_voicevox_enabled=True,
            tts_artifact_root=tts,
        ),
        database=_Database(),
        is_closing=False,
        speech_synthesis_provider_registry=foreign,
    )
    composer_called = False

    async def composer(_settings: object, _database: object, _runtime_current: object):
        nonlocal composer_called
        composer_called = True
        raise AssertionError("foreign publication must fail before composition")

    with pytest.raises(RuntimeError, match="already occupied"):
        await acquire_media_provider_runtime(bot, object(), composer=composer)

    assert composer_called is False
    assert bot.speech_synthesis_provider_registry is foreign


@pytest.mark.asyncio
async def test_transcription_plugin_preserves_foreign_store_publication() -> None:
    foreign_store = BoundedSpeechAudioStore()
    bot = SimpleNamespace(
        settings=SimpleNamespace(),
        database=_Database(),
        is_closing=False,
        tree=_Tree(),
        speech_transcription_audio_store=foreign_store,
    )
    plugin = SpeechTranscriptionPlugin()

    await plugin.start(bot)
    await plugin.stop()

    assert bot.speech_transcription_audio_store is foreign_store
    assert foreign_store._closing is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store_factory", "plugin_factory", "publication"),
    [
        (
            MusicArtifactStore,
            lambda store: SpeechSynthesisPlugin(artifact_store=store),
            "speech_synthesis_artifact_store",
        ),
        (
            MusicArtifactStore,
            lambda store: MusicGenerationPlugin(artifact_store=store),
            "music_artifact_store",
        ),
        (
            VideoArtifactStore,
            lambda store: VideoGenerationPlugin(artifact_store=store),
            "video_artifact_store",
        ),
    ],
)
async def test_plugins_preserve_preexisting_injected_store_publication(
    tmp_path: Path,
    store_factory: object,
    plugin_factory: object,
    publication: str,
) -> None:
    root = tmp_path / publication
    root.mkdir()
    store = store_factory(root)  # type: ignore[operator]
    plugin = plugin_factory(store)  # type: ignore[operator]
    bot = SimpleNamespace(
        settings=SimpleNamespace(),
        database=_Database(),
        is_closing=False,
        tree=_Tree(),
    )
    setattr(bot, publication, store)

    await plugin.start(bot)
    await plugin.stop()

    assert getattr(bot, publication) is store


@pytest.mark.asyncio
async def test_api_runtime_requires_existing_global_user_consent_store() -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            stt_openai_enabled=True,
            openai_api_key="openai-test-key",
        ),
        database=_Database(),
        is_closing=False,
    )
    composer_called = False

    async def composer(_settings: object, _database: object, _runtime_current: object):
        nonlocal composer_called
        composer_called = True
        raise AssertionError("missing consent store must fail closed before composition")

    assert await acquire_media_provider_runtime(bot, object(), composer=composer) is None
    assert composer_called is False
    assert not hasattr(bot, "media_provider_runtime")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("voicevox_url", "voice_allow_remote"),
    [
        ("https://voicevox.example.test", False),
        ("http://127.0.0.1:50021", True),
    ],
)
async def test_production_voicevox_composition_is_loopback_only(
    tmp_path: Path,
    voicevox_url: str,
    voice_allow_remote: bool,
) -> None:
    tts, _music, _video = _roots(tmp_path)
    settings = SimpleNamespace(
        tts_voicevox_enabled=True,
        tts_artifact_root=tts,
        voicevox_url=voicevox_url,
        voice_allow_remote=voice_allow_remote,
    )

    result = await compose_media_provider_runtime_from_settings(
        settings,
        _Database(),
        lambda: True,
    )

    assert result is None


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_plugin_publication_identity_changes(tmp_path: Path) -> None:
    tts, _music, _video = _roots(tmp_path)
    voicevox = _VoicevoxClient()
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            tts_voicevox_enabled=True,
            tts_artifact_root=tts,
        ),
        database=_Database(),
        is_closing=False,
    )

    async def composer(_settings: object, database: object, runtime_current: object):
        assert callable(runtime_current)
        return await compose_media_provider_runtime(
            MediaProviderConfiguration(tts_voicevox_enabled=True, tts_artifact_root=tts),
            database,  # type: ignore[arg-type]
            runtime_current=runtime_current,
            voicevox_client_factory=lambda: voicevox,
        )

    plugin = SpeechSynthesisPlugin(runtime_composer=composer)
    await plugin.start(bot)
    assert bot.runtime_capability_readiness[SPEECH_SYNTHESIS_CAPABILITY_ID] is True

    foreign_service = object()
    bot.speech_synthesis_service = foreign_service

    assert refresh_runtime_readiness(bot, SPEECH_SYNTHESIS_CAPABILITY_ID) is False

    await plugin.stop()

    assert bot.speech_synthesis_service is foreign_service


def test_media_provider_settings_default_off_and_enabled_values_are_strict(tmp_path: Path) -> None:
    defaults = Settings.from_env({"DISCORD_TOKEN": "offline-test-token"})
    assert (
        defaults.stt_openai_enabled,
        defaults.tts_voicevox_enabled,
        defaults.music_elevenlabs_enabled,
        defaults.video_veo_enabled,
    ) == (False, False, False, False)

    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        Settings.from_env(
            {
                "DISCORD_TOKEN": "offline-test-token",
                "STT_OPENAI_ENABLED": "true",
            }
        )

    tts, music, video = _roots(tmp_path)
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "offline-test-token",
            "OPENAI_API_KEY": "openai-test-key",
            "STT_OPENAI_ENABLED": "true",
            "TTS_VOICEVOX_ENABLED": "true",
            "TTS_ARTIFACT_ROOT": str(tts),
            "MUSIC_ELEVENLABS_ENABLED": "true",
            "ELEVENLABS_API_KEY": "eleven-test-key",
            "MUSIC_GENERATION_ARTIFACT_ROOT": str(music),
            "VIDEO_VEO_ENABLED": "true",
            "GEMINI_API_KEY": "gemini-test-key",
            "VIDEO_ARTIFACT_ROOT": str(video),
        }
    )
    assert settings.stt_openai_enabled is True
    assert settings.tts_artifact_root == tts
    assert settings.music_generation_artifact_root == music
    assert settings.video_artifact_root == video


def test_env_example_exposes_actual_media_providers_as_safe_defaults() -> None:
    contents = (Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1) for line in contents.splitlines() if line and not line.startswith("#") and "=" in line
    )

    assert values["STT_OPENAI_ENABLED"] == "false"
    assert values["TTS_VOICEVOX_ENABLED"] == "false"
    assert values["MUSIC_ELEVENLABS_ENABLED"] == "false"
    assert values["VIDEO_VEO_ENABLED"] == "false"
    assert values["AI_ALLOW_REMOTE"] == "false"
    assert values["ELEVENLABS_API_KEY"] == ""
    assert values["GEMINI_API_KEY"] == ""
    assert values["TTS_ARTIFACT_ROOT"] == ""
    assert values["MUSIC_GENERATION_ARTIFACT_ROOT"] == ""
    assert values["VIDEO_ARTIFACT_ROOT"] == ""
    settings = Settings.from_env(values | {"DISCORD_TOKEN": "offline-test-token"})
    assert (
        settings.stt_openai_enabled,
        settings.tts_voicevox_enabled,
        settings.music_elevenlabs_enabled,
        settings.video_veo_enabled,
    ) == (False, False, False, False)


@pytest.mark.asyncio
async def test_music_execution_proof_is_exact_identity_bound_and_short_lived(tmp_path: Path) -> None:
    _tts, music_root, _video = _roots(tmp_path)
    runtime = await compose_media_provider_runtime(
        MediaProviderConfiguration(
            music_eleven_enabled=True,
            music_eleven_api_key="eleven-test-key",
            music_artifact_root=music_root,
        ),
        _Database(),
        runtime_current=lambda: True,
        music_transport_factory=lambda _key: _MusicTransport(),
    )
    assert runtime is not None
    request = MusicGenerationRequest(
        request_id="music-proof-1",
        guild_id=10,
        channel_id=20,
        actor_id=30,
        prompt="original ambient instrumental",
        duration_seconds=15,
        rights_confirmed=True,
    )
    provider_request = ProviderRequest(
        request_id=request.provider_request_id,
        trace_id=request.trace_id,
        capability=LogicalCapability.MUSIC_GENERATION,
        actor_ref=request.actor_ref,
        payload=MediaGenerationInput(
            prompt=request.prompt,
            duration_seconds=request.duration_seconds,
        ),
        quality_tier=request.tier,
    )
    resolution = runtime.registry.resolve(
        LogicalCapability.MUSIC_GENERATION,
        actor_level="trusted",
        quality_tier=request.tier,
        consent_verified=True,
    )
    provider = runtime.registry.manifest.provider(resolution.provider_id or "")
    assert resolution.ready is True and resolution.resources is not None and provider is not None
    invocation = ProviderInvocation(
        provider_id=resolution.provider_id or "",
        quality_tier=resolution.quality_tier,
        model_alias=resolution.model_alias,
        provider_model=resolution.provider_model,
        timeout_seconds=provider.timeouts.request_seconds,
        resources=resolution.resources,
    )

    token = runtime.music_execution_proofs.issue(request, provider_request, resolution)

    assert runtime.music_execution_proofs.current(provider_request, invocation) is True
    cloned_request = ProviderRequest(
        request_id=provider_request.request_id,
        trace_id=provider_request.trace_id,
        capability=provider_request.capability,
        actor_ref=provider_request.actor_ref,
        payload=provider_request.payload,
        quality_tier=provider_request.quality_tier,
    )
    assert runtime.music_execution_proofs.current(cloned_request, invocation) is False
    runtime.music_execution_proofs.revoke(token)
    assert runtime.music_execution_proofs.current(provider_request, invocation) is False

    await runtime.close()


def _wav(*, seconds: int) -> bytes:
    sample_rate = 44_100
    channels = 1
    bits = 16
    frames = sample_rate * seconds
    block_align = channels * bits // 8
    payload = b"\0" * (frames * block_align)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(payload))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, sample_rate * block_align, block_align, bits)
        + b"data"
        + struct.pack("<I", len(payload))
        + payload
    )
