from __future__ import annotations

import base64
import binascii
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import yonerai_discord.modules.image_editing.plugin as image_editing_plugin_module
import yonerai_discord.modules.image_generation.plugin as image_generation_plugin_module
from yonerai_discord.config import ConfigurationError, Settings
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.image_editing import ImageEditingPlugin, ImageEditingRequest
from yonerai_discord.modules.image_generation import (
    ImageGenerationPlugin,
    ImageGenerationRequest,
)
from yonerai_discord.modules.image_generation.provider_composition import (
    OPENAI_IMAGE_MODEL,
    OPENAI_IMAGE_MODEL_ALIASES,
    compose_openai_image_runtime,
)
from yonerai_discord.modules.image_generation.provider_openai import (
    OPENAI_IMAGES_PROVIDER_ID,
    OpenAIImageProviderAdapter,
)
from yonerai_discord.provider_registry import LogicalCapability, QualityTier


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png() -> bytes:
    width = height = 64
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanlines = b"".join(b"\0" + bytes(width * 4) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(scanlines)) + _chunk(b"IEND", b"")
    )


class _Transport:
    def __init__(self, *, probe_ready: bool = True, close_failures: int = 0) -> None:
        self.probe_ready = probe_ready
        self.close_failures = close_failures
        self.probes: list[str] = []
        self.generations: list[dict[str, object]] = []
        self.edits: list[tuple[dict[str, str], bytes]] = []
        self.close_calls = 0

    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
        assert timeout_seconds == 5.0
        self.probes.append(model)
        return self.probe_ready

    async def generate_image(self, body, *, timeout_seconds: float):
        assert timeout_seconds == 90.0
        self.generations.append(dict(body))
        return {
            "data": [{"b64_json": base64.b64encode(_png()).decode("ascii")}],
            "output_format": "png",
        }

    async def edit_image(self, fields, *, image: bytes, timeout_seconds: float):
        assert timeout_seconds == 90.0
        self.edits.append((dict(fields), image))
        return {
            "data": [{"b64_json": base64.b64encode(_png()).decode("ascii")}],
            "output_format": "png",
        }

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls <= self.close_failures:
            raise RuntimeError("transport close failed")


class _Database:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def append_audit(self, event: str, **kwargs) -> int:
        self.records.append({"event": event, **kwargs})
        return len(self.records)


class _Consent:
    async def active_user(self, _actor_id: int) -> bool:
        return True


class _Tree:
    def __init__(self) -> None:
        self.commands: dict[str, object] = {}

    def add_command(self, command: object) -> None:
        self.commands[str(getattr(command, "name"))] = command

    def remove_command(self, name: str) -> None:
        self.commands.pop(name, None)


def _settings(root: Path, *, enabled: bool = True, api_key: str = "test-key") -> SimpleNamespace:
    return SimpleNamespace(
        image_openai_enabled=enabled,
        image_artifact_root=root,
        image_openai_timeout_seconds=90.0,
        openai_api_key=api_key,
    )


async def test_disabled_missing_or_failed_probe_never_publishes_a_route(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    database = _Database()
    factory_calls = 0

    def factory(_api_key: str) -> _Transport:
        nonlocal factory_calls
        factory_calls += 1
        return _Transport()

    assert (
        await compose_openai_image_runtime(
            _settings(root, enabled=False),
            database,
            runtime_current=lambda: True,
            transport_factory=factory,
        )
        is None
    )
    assert (
        await compose_openai_image_runtime(
            _settings(root, api_key=""),
            database,
            runtime_current=lambda: True,
            transport_factory=factory,
        )
        is None
    )
    assert factory_calls == 0

    failed = _Transport(probe_ready=False)
    runtime = await compose_openai_image_runtime(
        _settings(root),
        database,
        runtime_current=lambda: True,
        transport_factory=lambda _key: failed,
    )
    assert runtime is None
    assert failed.probes == [OPENAI_IMAGE_MODEL]
    assert failed.close_calls == 1


async def test_composed_runtime_routes_both_capabilities_only_after_health(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    transport = _Transport()
    runtime = await compose_openai_image_runtime(
        _settings(root),
        _Database(),
        runtime_current=lambda: True,
        transport_factory=lambda _key: transport,
    )
    assert runtime is not None
    assert runtime.ready is True
    assert transport.probes == [OPENAI_IMAGE_MODEL]

    for capability in (
        LogicalCapability.IMAGE_GENERATION,
        LogicalCapability.IMAGE_EDITING,
    ):
        for tier in QualityTier:
            resolution = runtime.registry.resolve(
                capability,
                actor_level=RbacLevel.TRUSTED,
                quality_tier=tier,
                consent_verified=True,
            )
            assert resolution.ready is True
            assert resolution.provider_model == OPENAI_IMAGE_MODEL
            assert resolution.model_alias in OPENAI_IMAGE_MODEL_ALIASES
    await runtime.close()
    assert transport.close_calls == 1


async def test_generation_plugin_can_own_runtime_without_editing_plugin(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    transport = _Transport()
    bot = SimpleNamespace(
        settings=_settings(root),
        database=_Database(),
        ai_remote_consent_store=_Consent(),
        tree=_Tree(),
        is_closing=False,
    )
    generation = ImageGenerationPlugin(openai_transport_factory=lambda _key: transport)

    await generation.start(bot)
    assert bot.runtime_capability_readiness["cap-run-image-generate"] is True
    assert generation.service is not None
    result = await generation.service.generate(
        ImageGenerationRequest(
            request_id="standalone-image-generation",
            guild_id=10,
            channel_id=20,
            actor_id=30,
            prompt="private standalone prompt",
        ),
        authorization_current=lambda: True,
    )
    assert result.artifact.media_type == "image/png"
    assert len(transport.generations) == 1

    await generation.stop()
    assert transport.close_calls == 1
    assert not hasattr(bot, "image_openai_runtime")
    assert not hasattr(bot, "image_artifact_store")
    assert not hasattr(bot, "image_generation_provider_registry")
    assert not hasattr(bot, "image_editing_provider_registry")


async def test_runtime_close_does_not_unregister_a_foreign_adapter(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    owned_transport = _Transport()
    runtime = await compose_openai_image_runtime(
        _settings(root),
        _Database(),
        runtime_current=lambda: True,
        transport_factory=lambda _key: owned_transport,
    )
    assert runtime is not None
    assert runtime.registry.unregister_adapter(OPENAI_IMAGES_PROVIDER_ID) is runtime.adapter
    replacement_transport = _Transport()
    replacement = OpenAIImageProviderAdapter(
        replacement_transport,
        runtime.store,
        health_models=(OPENAI_IMAGE_MODEL,),
        probed_model_aliases=OPENAI_IMAGE_MODEL_ALIASES,
    )
    runtime.registry.register_adapter(replacement)

    with pytest.raises(RuntimeError, match="registry identity changed"):
        await runtime.close()
    assert owned_transport.close_calls == 1
    assert runtime.registry.unregister_adapter(OPENAI_IMAGES_PROVIDER_ID) is replacement
    assert replacement_transport.close_calls == 0
    await replacement.close()


async def test_last_consumer_retries_runtime_close_after_failure(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    transport = _Transport(close_failures=1)
    bot = SimpleNamespace(
        settings=_settings(root),
        database=_Database(),
        ai_remote_consent_store=_Consent(),
        tree=_Tree(),
        is_closing=False,
    )
    generation = ImageGenerationPlugin(openai_transport_factory=lambda _key: transport)
    await generation.start(bot)

    with pytest.raises(RuntimeError, match="transport close failed"):
        await generation.stop()
    assert transport.close_calls == 1
    assert not hasattr(bot, "image_openai_runtime")

    await generation.stop()
    assert transport.close_calls == 2
    assert generation._runtime is None


async def test_readiness_withdraw_failure_still_closes_owned_runtime(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "images"
    root.mkdir()
    transport = _Transport()
    bot = SimpleNamespace(
        settings=_settings(root),
        database=_Database(),
        ai_remote_consent_store=_Consent(),
        tree=_Tree(),
        is_closing=False,
    )
    generation = ImageGenerationPlugin(openai_transport_factory=lambda _key: transport)
    await generation.start(bot)
    monkeypatch.setattr(
        image_generation_plugin_module,
        "withdraw_runtime_readiness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("readiness withdraw failed")),
    )

    with pytest.raises(RuntimeError, match="readiness withdraw failed"):
        await generation.stop()
    assert transport.close_calls == 1
    assert generation._runtime is None
    assert not hasattr(bot, "image_openai_runtime")


async def test_plugins_share_runtime_and_execute_generation_then_edit(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    transport = _Transport()
    database = _Database()
    bot = SimpleNamespace(
        settings=_settings(root),
        database=database,
        ai_remote_consent_store=_Consent(),
        tree=_Tree(),
        is_closing=False,
    )
    editing = ImageEditingPlugin(openai_transport_factory=lambda _key: transport)
    generation = ImageGenerationPlugin()

    await editing.start(bot)
    await generation.start(bot)

    assert generation.service is not None
    assert editing.service is not None
    assert generation.service.registry is editing.service.registry
    assert generation.service.artifact_store is editing.service.artifact_store
    assert bot.runtime_capability_readiness["cap-run-image-generate"] is True
    assert bot.runtime_capability_readiness["cap-run-image-edit"] is True

    generation_request = ImageGenerationRequest(
        request_id="image-plugin-generation",
        guild_id=10,
        channel_id=20,
        actor_id=30,
        prompt="private generation prompt",
    )
    generated = await generation.service.generate(
        generation_request,
        authorization_current=lambda: True,
    )
    claim = await generation.service.issue_edit_source(
        generation_request,
        generated,
        edit_request_id="image-plugin-edit",
        authorization_current=lambda: True,
    )
    edited = await editing.service.edit(
        ImageEditingRequest(
            request_id="image-plugin-edit",
            guild_id=10,
            channel_id=20,
            actor_id=30,
            instruction="private edit instruction",
            source=claim,
        ),
        authorization_current=lambda: True,
    )

    assert generated.artifact.artifact_id != edited.artifact.artifact_id
    assert len(transport.generations) == len(transport.edits) == 1
    assert database.records
    assert all("private" not in repr(record) for record in database.records)

    await editing.stop()
    assert transport.close_calls == 0
    assert bot.runtime_capability_readiness["cap-run-image-generate"] is True
    generated_after_editing_stop = await generation.service.generate(
        ImageGenerationRequest(
            request_id="image-after-editing-stop",
            guild_id=10,
            channel_id=20,
            actor_id=30,
            prompt="private generation after editing stop",
        ),
        authorization_current=lambda: True,
    )
    assert generated_after_editing_stop.artifact.media_type == "image/png"

    replacement = object()
    bot.image_openai_provider_adapter = replacement
    await generation.stop()
    assert transport.close_calls == 1
    assert bot.image_openai_provider_adapter is replacement
    assert not hasattr(bot, "image_openai_runtime")
    assert not hasattr(bot, "image_artifact_store")
    assert not hasattr(bot, "image_generation_provider_registry")
    assert not hasattr(bot, "image_editing_provider_registry")


async def test_generation_first_stop_keeps_editing_runtime_available(tmp_path: Path) -> None:
    root = tmp_path / "images"
    root.mkdir()
    transport = _Transport()
    bot = SimpleNamespace(
        settings=_settings(root),
        database=_Database(),
        ai_remote_consent_store=_Consent(),
        tree=_Tree(),
        is_closing=False,
    )
    generation = ImageGenerationPlugin(openai_transport_factory=lambda _key: transport)
    editing = ImageEditingPlugin()

    await generation.start(bot)
    await editing.start(bot)
    assert generation.service is not None
    assert editing.service is not None
    assert editing.source_claim_issuer is not None

    source_binding = "a" * 64
    source = bot.image_artifact_store.put_png(_png(), request_binding=source_binding)
    claim = await editing.source_claim_issuer.issue(
        source,
        source_binding=source_binding,
        edit_request_id="edit-after-generation-stop",
        guild_id=10,
        channel_id=20,
        actor_id=30,
        authorization_current=lambda: True,
    )
    await generation.stop()
    assert transport.close_calls == 0
    assert bot.runtime_capability_readiness["cap-run-image-edit"] is True
    edited = await editing.service.edit(
        ImageEditingRequest(
            request_id="edit-after-generation-stop",
            guild_id=10,
            channel_id=20,
            actor_id=30,
            instruction="private edit after generation stop",
            source=claim,
        ),
        authorization_current=lambda: True,
    )
    assert edited.artifact.artifact_id != source.artifact_id
    assert len(transport.edits) == 1

    await editing.stop()
    assert transport.close_calls == 1
    assert not hasattr(bot, "image_openai_runtime")


async def test_plugin_start_failure_withdraws_owned_runtime_and_command(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "images"
    root.mkdir()
    transport = _Transport()
    bot = SimpleNamespace(
        settings=_settings(root),
        database=_Database(),
        ai_remote_consent_store=_Consent(),
        tree=_Tree(),
        is_closing=False,
    )
    editing = ImageEditingPlugin(openai_transport_factory=lambda _key: transport)

    monkeypatch.setattr(
        image_editing_plugin_module,
        "publish_runtime_readiness_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("readiness failed")),
    )
    with pytest.raises(RuntimeError, match="readiness failed"):
        await editing.start(bot)
    assert transport.close_calls == 1
    assert editing.service is editing.adapter is editing.source_claim_issuer is None
    assert not any(name.startswith("image_") for name in vars(bot))

    monkeypatch.undo()
    replacement_transport = _Transport()
    editing = ImageEditingPlugin(openai_transport_factory=lambda _key: replacement_transport)
    await editing.start(bot)
    generation = ImageGenerationPlugin()
    monkeypatch.setattr(
        image_generation_plugin_module,
        "publish_runtime_readiness_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("readiness failed")),
    )
    with pytest.raises(RuntimeError, match="readiness failed"):
        await generation.start(bot)
    assert generation.service is generation.adapter is None
    assert "image" not in bot.tree.commands
    assert not hasattr(bot, "image_generation_service")
    assert not hasattr(bot, "image_generation_adapter")
    await editing.stop()
    assert replacement_transport.close_calls == 1


def test_settings_require_explicit_key_and_absolute_artifact_root(tmp_path: Path) -> None:
    environment = {
        "DISCORD_TOKEN": "test-token-never-used",
        "IMAGE_OPENAI_ENABLED": "true",
        "OPENAI_API_KEY": "test-key",
        "IMAGE_ARTIFACT_ROOT": str(tmp_path.resolve()),
        "IMAGE_OPENAI_TIMEOUT_SECONDS": "90",
    }
    settings = Settings.from_env(environment)
    assert settings.image_openai_enabled is True
    assert settings.image_artifact_root == tmp_path.resolve()
    assert settings.image_openai_timeout_seconds == 90.0
    assert "test-key" not in repr(settings)
    assert str(tmp_path.resolve()) not in repr(settings)

    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        Settings.from_env(environment | {"OPENAI_API_KEY": ""})
    with pytest.raises(ConfigurationError, match="IMAGE_ARTIFACT_ROOT"):
        Settings.from_env(environment | {"IMAGE_ARTIFACT_ROOT": ""})
    with pytest.raises(ConfigurationError, match="絶対path"):
        Settings.from_env(environment | {"IMAGE_ARTIFACT_ROOT": "relative/images"})
