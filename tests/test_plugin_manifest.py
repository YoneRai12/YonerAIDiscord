from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from yonerai_discord.plugin import PluginManager, PluginManifestError, discover_plugins
from yonerai_discord.plugin_manifest import BUILTIN_PLUGIN_MANIFEST


def _make_package(tmp_path: Path, package_name: str, modules: dict[str, str]) -> str:
    package = tmp_path / package_name
    package.mkdir()
    (package / "__init__.py").write_text("EVENTS = []\n", encoding="utf-8")
    for name, body in modules.items():
        (package / f"{name}.py").write_text(body, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    return package_name


def _plugin_module(name: str) -> str:
    return f'''from . import EVENTS

class Plugin:
    async def start(self, bot):
        return None

    async def stop(self):
        return None

def setup(manager):
    EVENTS.append("{name}")
    manager.register("{name}", Plugin)
'''


def test_builtin_manifest_has_the_declared_dependency_order() -> None:
    assert BUILTIN_PLUGIN_MANIFEST == (
        "admin_ui",
        "personal_memory",
        "site_publish",
        "media_pipeline",
        "browser_rendering",
        "media_inspection",
        "ai",
        "automod",
        "community",
        "capability_forge",
        "discovery",
        "earthquake",
        "evolution",
        "identity",
        "image_editing",
        "image_generation",
        "jobs",
        "jp_information",
        "nasa_apod",
        "minecraft",
        "moderation",
        "modtools",
        "voice",
        "music",
        "music_generation",
        "operations",
        "scheduling",
        "servertools",
        "speech_synthesis",
        "speech_transcription",
        "utility",
        "video_generation",
        "yonerai",
    )
    assert (
        BUILTIN_PLUGIN_MANIFEST.index("media_pipeline")
        < BUILTIN_PLUGIN_MANIFEST.index("browser_rendering")
        < BUILTIN_PLUGIN_MANIFEST.index("ai")
    )


@pytest.fixture(autouse=True)
def _clean_test_packages() -> None:
    original_path = sys.path.copy()
    yield
    sys.path[:] = original_path
    for name in tuple(sys.modules):
        if name.startswith("manifest_test_"):
            sys.modules.pop(name, None)


def test_discovery_without_manifest_keeps_compatible_failure_isolation(tmp_path: Path) -> None:
    package_name = _make_package(
        tmp_path,
        "manifest_test_compat",
        {
            "alpha": _plugin_module("alpha"),
            "broken": "def setup(manager):\n    raise RuntimeError('broken')\n",
        },
    )
    manager = PluginManager()

    discover_plugins(manager, package_name)

    snapshots = {snapshot.name: snapshot for snapshot in manager.snapshots()}
    assert set(snapshots) == {"alpha", "broken"}
    assert snapshots["broken"].error == "RuntimeError"


def test_manifest_exact_match_registers_in_dependency_order(tmp_path: Path) -> None:
    package_name = _make_package(
        tmp_path,
        "manifest_test_order",
        {"alpha": _plugin_module("alpha"), "beta": _plugin_module("beta")},
    )
    manager = PluginManager()

    discover_plugins(manager, package_name, manifest=("beta", "alpha"))

    package = importlib.import_module(package_name)
    assert package.EVENTS == ["beta", "alpha"]


def test_duplicate_manifest_entry_is_rejected_before_setup(tmp_path: Path) -> None:
    package_name = _make_package(
        tmp_path,
        "manifest_test_duplicate",
        {"alpha": _plugin_module("alpha")},
    )
    manager = PluginManager()

    with pytest.raises(PluginManifestError, match="duplicate"):
        discover_plugins(manager, package_name, manifest=("alpha", "alpha"))

    assert manager.snapshots() == ()


def test_missing_manifest_module_is_rejected_before_setup(tmp_path: Path) -> None:
    package_name = _make_package(
        tmp_path,
        "manifest_test_missing",
        {"alpha": _plugin_module("alpha")},
    )
    manager = PluginManager()

    with pytest.raises(PluginManifestError, match="missing setup modules: beta"):
        discover_plugins(manager, package_name, manifest=("alpha", "beta"))

    assert manager.snapshots() == ()


def test_unlisted_setup_module_is_rejected_before_setup(tmp_path: Path) -> None:
    package_name = _make_package(
        tmp_path,
        "manifest_test_unlisted",
        {"alpha": _plugin_module("alpha"), "beta": _plugin_module("beta")},
    )
    manager = PluginManager()

    with pytest.raises(PluginManifestError, match="unlisted setup modules: beta"):
        discover_plugins(manager, package_name, manifest=("alpha",))

    assert manager.snapshots() == ()


def test_setup_must_register_exactly_its_module_name(tmp_path: Path) -> None:
    package_name = _make_package(
        tmp_path,
        "manifest_test_registration",
        {"alpha": _plugin_module("wrong")},
    )
    manager = PluginManager()

    with pytest.raises(PluginManifestError, match="exactly its module name"):
        discover_plugins(manager, package_name, manifest=("alpha",))

    assert manager.snapshots() == ()
