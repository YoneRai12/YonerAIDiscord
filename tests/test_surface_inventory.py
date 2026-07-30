from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yonerai_discord.capabilities import build_capability_registry
from yonerai_discord.config import Settings
from yonerai_discord.db import Database
from yonerai_discord.plugin import PluginManager
from yonerai_discord.surface_inventory import (
    SurfaceInventoryError,
    command_paths_from_tree,
    reconcile_runtime_surfaces,
)


@dataclass(frozen=True)
class FakeCommand:
    name: str
    commands: tuple[FakeCommand, ...] = ()


class FakeTree:
    def __init__(self, *commands: FakeCommand, client: Any | None = None) -> None:
        self.commands = commands
        self.client = client

    def get_commands(self) -> tuple[FakeCommand, ...]:
        return self.commands


class NoopPlugin:
    async def start(self, _: Any) -> None:
        return None

    async def stop(self) -> None:
        return None


def _registry(tmp_path: Path) -> tuple[Database, Any]:
    root = Path(__file__).parents[1]
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "unused-test-token",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        }
    )
    database = Database(tmp_path / "surface.sqlite3")
    database.open()
    database.migrate()
    return database, build_capability_registry(settings, database)


def test_command_tree_inventory_flattens_only_leaf_surfaces() -> None:
    tree = FakeTree(
        FakeCommand(
            "system",
            (
                FakeCommand("ping"),
                FakeCommand("nested", (FakeCommand("leaf"),)),
            ),
        )
    )
    assert command_paths_from_tree(tree) == frozenset({"system ping", "system nested leaf"})


@pytest.mark.asyncio
async def test_inventory_uses_real_tree_and_plugin_state(tmp_path: Path) -> None:
    database, registry = _registry(tmp_path)
    plugins = PluginManager()
    plugins.register("community", NoopPlugin)
    await plugins.start_all(object(), {"community"})
    tree = FakeTree(
        FakeCommand("system", (FakeCommand("ping"),)),
        FakeCommand("poll", (FakeCommand("create"),)),
    )
    try:
        report = reconcile_runtime_surfaces(registry, tree, plugins)
        assert report.actual_command_paths == frozenset({"system ping", "poll create"})
        assert "system health" in report.missing_command_paths
        assert registry.runtime_available("cap-can-0265") is True
        assert registry.runtime_available("cap-run-poll-create") is True
        assert registry.runtime_available("cap-run-poll-vote") is True
        assert registry.runtime_available("cap-run-server-member-join") is False
    finally:
        await plugins.stop_all()
        database.close()


@pytest.mark.asyncio
async def test_inventory_marks_command_unavailable_when_plugin_is_off(tmp_path: Path) -> None:
    database, registry = _registry(tmp_path)
    plugins = PluginManager()
    plugins.register("community", NoopPlugin)
    await plugins.start_all(object(), set())
    tree = FakeTree(FakeCommand("poll", (FakeCommand("create"),)))
    try:
        reconcile_runtime_surfaces(registry, tree, plugins)
        assert registry.runtime_available("cap-run-poll-create") is False
        assert registry.runtime_available("cap-run-poll-vote") is False
    finally:
        database.close()


def test_inventory_rejects_unmapped_surface_before_sync(tmp_path: Path) -> None:
    database, registry = _registry(tmp_path)
    try:
        with pytest.raises(SurfaceInventoryError, match="prototype danger"):
            reconcile_runtime_surfaces(
                registry,
                FakeTree(FakeCommand("prototype", (FakeCommand("danger"),))),
                PluginManager(),
            )
    finally:
        database.close()


@pytest.mark.asyncio
async def test_privileged_gateway_events_are_runtime_unavailable_until_opted_in(
    tmp_path: Path,
) -> None:
    database, registry = _registry(tmp_path)
    plugins = PluginManager()
    plugins.register("servertools", NoopPlugin)
    await plugins.start_all(object(), {"servertools"})
    try:
        disabled_tree = FakeTree(
            client=SimpleNamespace(
                settings=SimpleNamespace(
                    member_events_enabled=False,
                    message_audit_events_enabled=False,
                )
            )
        )
        reconcile_runtime_surfaces(registry, disabled_tree, plugins)
        assert registry.runtime_available("cap-run-server-member-join") is False
        assert registry.runtime_available("cap-run-server-message-delete") is False

        enabled_tree = FakeTree(
            client=SimpleNamespace(
                settings=SimpleNamespace(
                    member_events_enabled=True,
                    message_audit_events_enabled=True,
                )
            )
        )
        reconcile_runtime_surfaces(registry, enabled_tree, plugins)
        assert registry.runtime_available("cap-run-server-member-join") is True
        assert registry.runtime_available("cap-run-server-message-delete") is True
    finally:
        await plugins.stop_all()
        database.close()


@pytest.mark.asyncio
async def test_registered_command_can_be_unavailable_when_its_service_is_not_ready(
    tmp_path: Path,
) -> None:
    database, registry = _registry(tmp_path)
    plugins = PluginManager()
    plugins.register("community", NoopPlugin)
    await plugins.start_all(object(), {"community"})
    tree = FakeTree(
        FakeCommand("poll", (FakeCommand("create"),)),
        client=SimpleNamespace(
            runtime_capability_readiness={"cap-run-poll-create": False},
        ),
    )
    try:
        reconcile_runtime_surfaces(registry, tree, plugins)
        assert registry.runtime_available("cap-run-poll-create") is False
    finally:
        await plugins.stop_all()
        database.close()


@pytest.mark.asyncio
async def test_service_only_readiness_is_gated_by_owning_plugin(tmp_path: Path) -> None:
    database, registry = _registry(tmp_path)
    plugins = PluginManager()
    plugins.register("music", NoopPlugin)
    await plugins.start_all(object(), {"music"})
    try:
        report = reconcile_runtime_surfaces(
            registry,
            FakeTree(client=SimpleNamespace(runtime_capability_readiness={"cap-run-audio-ducking-core": True})),
            plugins,
        )
        assert registry.runtime_available("cap-run-audio-ducking-core") is True
        assert "cap-run-audio-ducking-core" in report.available_capability_ids

        await plugins.disable("music")
        reconcile_runtime_surfaces(
            registry,
            FakeTree(client=SimpleNamespace(runtime_capability_readiness={"cap-run-audio-ducking-core": True})),
            plugins,
        )
        assert registry.runtime_available("cap-run-audio-ducking-core") is False
    finally:
        await plugins.stop_all()
        database.close()


def test_unknown_service_readiness_id_fails_closed(tmp_path: Path) -> None:
    database, registry = _registry(tmp_path)
    try:
        with pytest.raises(SurfaceInventoryError, match="unknown runtime readiness"):
            reconcile_runtime_surfaces(
                registry,
                FakeTree(client=SimpleNamespace(runtime_capability_readiness={"cap-run-typo": True})),
                PluginManager(),
            )
    finally:
        database.close()


@pytest.mark.asyncio
async def test_automod_event_surfaces_require_explicit_global_switch(tmp_path: Path) -> None:
    database, registry = _registry(tmp_path)
    plugins = PluginManager()
    plugins.register("automod", NoopPlugin)
    await plugins.start_all(object(), {"automod"})
    commands = FakeCommand(
        "automod",
        (FakeCommand("status"), FakeCommand("channel"), FakeCommand("policy")),
    )
    try:
        reconcile_runtime_surfaces(
            registry,
            FakeTree(commands, client=SimpleNamespace(settings=SimpleNamespace(automod_enabled=False))),
            plugins,
        )
        assert registry.runtime_available("cap-run-automod-message-create") is False

        reconcile_runtime_surfaces(
            registry,
            FakeTree(commands, client=SimpleNamespace(settings=SimpleNamespace(automod_enabled=True))),
            plugins,
        )
        assert registry.runtime_available("cap-run-automod-message-create") is True
        assert registry.runtime_available("cap-run-automod-message-edit") is True
    finally:
        await plugins.stop_all()
        database.close()


@pytest.mark.asyncio
async def test_read_aloud_event_surface_requires_explicit_global_switch(tmp_path: Path) -> None:
    database, registry = _registry(tmp_path)
    plugins = PluginManager()
    plugins.register("music", NoopPlugin)
    await plugins.start_all(object(), {"music"})
    try:
        reconcile_runtime_surfaces(
            registry,
            FakeTree(
                client=SimpleNamespace(
                    settings=SimpleNamespace(music_read_aloud_enabled=False),
                    runtime_capability_readiness={
                        "cap-run-audio-ducking-core": True,
                        "cap-run-music-read-aloud-message": True,
                    },
                )
            ),
            plugins,
        )
        assert registry.runtime_available("cap-run-music-read-aloud-message") is False

        reconcile_runtime_surfaces(
            registry,
            FakeTree(
                client=SimpleNamespace(
                    settings=SimpleNamespace(music_read_aloud_enabled=True),
                    runtime_capability_readiness={
                        "cap-run-audio-ducking-core": True,
                        "cap-run-music-read-aloud-message": True,
                    },
                )
            ),
            plugins,
        )
        assert registry.runtime_available("cap-run-music-read-aloud-message") is True
    finally:
        await plugins.stop_all()
        database.close()
