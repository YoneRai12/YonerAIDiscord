from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import pkgutil
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


logger = logging.getLogger(__name__)


_START_FAILURE_CLEANUP_TIMEOUT_SECONDS = 5.0


class Plugin(Protocol):
    async def start(self, bot: Any) -> None: ...
    async def stop(self) -> None: ...


PluginFactory = Callable[[], Plugin]


class PluginStatus(StrEnum):
    REGISTERED = "registered"
    DISABLED = "disabled"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class PluginSnapshot:
    name: str
    status: PluginStatus
    error: str | None = None


@dataclass(slots=True)
class _Entry:
    factory: PluginFactory
    instance: Plugin | None = None
    status: PluginStatus = PluginStatus.REGISTERED
    error: str | None = None


class PluginManager:
    """プラグインのライフサイクルを個別に隔離して管理する。"""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._start_order: list[str] = []
        self._unknown_enabled: set[str] = set()

    def register(self, name: str, factory: PluginFactory) -> None:
        normalized = self._normalize_name(name)
        if normalized in self._entries:
            raise ValueError(f"plugin already registered: {normalized}")
        self._entries[normalized] = _Entry(factory=factory)

    def record_failure(self, name: str, error_type: str) -> None:
        """検出・登録段階の障害も状態一覧へ残す。"""
        normalized = self._normalize_name(name)
        if normalized in self._entries:
            entry = self._entries[normalized]
            entry.instance = None
            entry.status = PluginStatus.FAILED
            entry.error = error_type
            return
        self._entries[normalized] = _Entry(
            factory=lambda: _UnavailablePlugin(), status=PluginStatus.FAILED, error=error_type
        )

    async def start_all(self, bot: Any, enabled: Collection[str]) -> None:
        selected = {self._normalize_name(name) for name in enabled}
        unknown = selected - self._entries.keys()
        self._unknown_enabled = set(unknown)
        if unknown:
            logger.warning("unknown_plugins", extra={"plugins": sorted(unknown)})
        for name in self._entries:
            # 空集合は「全有効」ではなく「全無効」。allowlistが隔離処理などで
            # 空になった時に全pluginが起動するfail-openを防ぐ。
            if name not in selected:
                self._entries[name].status = PluginStatus.DISABLED
                continue
            await self.enable(name, bot)

    async def enable(self, name: str, bot: Any) -> bool:
        normalized = self._normalize_name(name)
        entry = self._entry(normalized)
        if entry.status in {PluginStatus.STARTING, PluginStatus.RUNNING}:
            return True
        entry.status = PluginStatus.STARTING
        entry.error = None
        instance: Plugin | None = None
        try:
            instance = entry.factory()
            await instance.start(bot)
        except asyncio.CancelledError:
            if instance is not None:
                await self._cleanup_failed_start(normalized, instance)
            entry.instance = None
            entry.status = PluginStatus.FAILED
            entry.error = "CancelledError"
            raise
        except Exception as exc:
            if instance is not None:
                await self._cleanup_failed_start(normalized, instance)
            entry.instance = None
            entry.status = PluginStatus.FAILED
            entry.error = type(exc).__name__
            logger.error("plugin_start_failed", extra={"plugin": normalized, "error_type": type(exc).__name__})
            return False
        entry.instance = instance
        entry.status = PluginStatus.RUNNING
        if normalized not in self._start_order:
            self._start_order.append(normalized)
        logger.info("plugin_started", extra={"plugin": normalized})
        return True

    @staticmethod
    async def _cleanup_failed_start(name: str, instance: Plugin) -> None:
        """部分start後のresourceをbounded best-effortでrollbackする。"""

        try:
            await asyncio.wait_for(instance.stop(), timeout=_START_FAILURE_CLEANUP_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.error(
                "plugin_start_cleanup_failed",
                extra={"plugin": name, "error_type": type(exc).__name__},
            )

    async def disable(self, name: str, timeout: float = 10.0) -> bool:
        normalized = self._normalize_name(name)
        entry = self._entry(normalized)
        instance = entry.instance
        if instance is None:
            entry.status = PluginStatus.DISABLED
            entry.error = None
            return True
        try:
            await asyncio.wait_for(instance.stop(), timeout=timeout)
        except Exception as exc:
            entry.status = PluginStatus.FAILED
            entry.error = type(exc).__name__
            logger.error("plugin_stop_failed", extra={"plugin": normalized, "error_type": type(exc).__name__})
            return False
        entry.instance = None
        entry.status = PluginStatus.DISABLED
        entry.error = None
        if normalized in self._start_order:
            self._start_order.remove(normalized)
        logger.info("plugin_disabled", extra={"plugin": normalized})
        return True

    async def stop_all(self, timeout_per_plugin: float = 10.0) -> None:
        for name in reversed(self._start_order.copy()):
            entry = self._entries[name]
            instance = entry.instance
            if instance is None:
                continue
            try:
                await asyncio.wait_for(instance.stop(), timeout=timeout_per_plugin)
            except Exception as exc:
                entry.status = PluginStatus.FAILED
                entry.error = type(exc).__name__
                logger.error("plugin_stop_failed", extra={"plugin": name, "error_type": type(exc).__name__})
            else:
                entry.status = PluginStatus.STOPPED
                entry.error = None
            finally:
                entry.instance = None
        self._start_order.clear()

    async def quiesce_all(self, timeout_per_plugin: float = 5.0) -> tuple[str, ...]:
        """RUNNING pluginの新規受付をbounded best-effortで停止する。

        ``begin_close`` は任意hookで、sync/asyncのどちらでもよい。ここでは
        pluginのstatusやstop順を変更せず、呼び出し側がその後 ``stop_all`` を行う。
        """

        if timeout_per_plugin <= 0:
            raise ValueError("timeout_per_plugin must be positive")
        candidates: list[tuple[str, Plugin]] = []
        for name in tuple(self._start_order):
            entry = self._entries[name]
            if entry.status is not PluginStatus.RUNNING or entry.instance is None:
                continue
            if callable(getattr(entry.instance, "begin_close", None)):
                candidates.append((name, entry.instance))

        async def quiesce_one(name: str, instance: Plugin) -> tuple[str, bool]:
            hook = getattr(instance, "begin_close")
            try:
                await asyncio.wait_for(self._invoke_lifecycle_hook(hook), timeout=timeout_per_plugin)
            except Exception as exc:
                logger.error(
                    "plugin_quiesce_failed",
                    extra={"plugin": name, "error_type": type(exc).__name__},
                )
                return name, False
            return name, True

        # 1 pluginのtimeoutが他pluginの受付停止開始を遅らせないよう並行通知する。
        results = await asyncio.gather(*(quiesce_one(name, instance) for name, instance in candidates))
        return tuple(name for name, succeeded in results if not succeeded)

    @staticmethod
    async def _invoke_lifecycle_hook(hook: Callable[[], Any]) -> None:
        if inspect.iscoroutinefunction(hook):
            await hook()
            return
        # sync hookがevent loopをblockしないようdaemon threadで呼び出す。
        # timeout後にhookが戻らなくてもexecutor shutdownを待たせない。
        loop = asyncio.get_running_loop()
        completed: asyncio.Future[Any] = loop.create_future()

        def worker() -> None:
            try:
                result = hook()
            except Exception as exc:

                def callback(error: Exception = exc) -> None:
                    if not completed.done():
                        completed.set_exception(error)

            else:

                def callback(value: Any = result) -> None:
                    if not completed.done():
                        completed.set_result(value)

            try:
                loop.call_soon_threadsafe(callback)
            except RuntimeError:
                # event loop終了後にtimeout済みhookが戻っても何も更新しない。
                return

        threading.Thread(target=worker, name="plugin-quiesce", daemon=True).start()
        result = await completed
        if inspect.isawaitable(result):
            await result

    def snapshots(self) -> tuple[PluginSnapshot, ...]:
        registered = tuple(
            PluginSnapshot(name=name, status=entry.status, error=entry.error)
            for name, entry in sorted(self._entries.items())
        )
        unknown = tuple(
            PluginSnapshot(name=name, status=PluginStatus.FAILED, error="not registered")
            for name in sorted(self._unknown_enabled)
        )
        return registered + unknown

    def healthy(self) -> bool:
        return not self._unknown_enabled and all(
            entry.status not in {PluginStatus.FAILED, PluginStatus.STARTING} for entry in self._entries.values()
        )

    def status(self, name: str) -> PluginStatus:
        return self._entry(self._normalize_name(name)).status

    def is_running(self, name: str) -> bool:
        try:
            return self.status(name) is PluginStatus.RUNNING
        except KeyError:
            return False

    async def notify_module_policy(
        self,
        module_id: str,
        enabled: bool,
        guild_id: int | None,
    ) -> tuple[str, ...]:
        """実行中pluginへmodule policy変更を通知し、長寿命副作用を即時停止・再評価させる。"""

        return await self._notify_policy(
            "on_module_policy_changed",
            subject_kind="module",
            subject_id=module_id,
            enabled=enabled,
            guild_id=guild_id,
        )

    async def notify_capability_policy(
        self,
        capability_id: str,
        enabled: bool,
        guild_id: int | None,
    ) -> tuple[str, ...]:
        """実行中pluginへcapability policy変更を通知し、常駐処理へ即時反映させる。"""

        return await self._notify_policy(
            "on_capability_policy_changed",
            subject_kind="capability",
            subject_id=capability_id,
            enabled=enabled,
            guild_id=guild_id,
        )

    async def _notify_policy(
        self,
        hook_name: str,
        *,
        subject_kind: str,
        subject_id: str,
        enabled: bool,
        guild_id: int | None,
    ) -> tuple[str, ...]:
        """policy hook失敗をplugin単位で隔離し、全pluginへの通知を継続する。"""

        failures: list[str] = []
        for name in tuple(self._start_order):
            entry = self._entries[name]
            instance = entry.instance
            hook = getattr(instance, hook_name, None) if instance is not None else None
            if not callable(hook):
                continue
            try:
                result = hook(subject_id, enabled, guild_id)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                failures.append(name)
                logger.error(
                    "plugin_policy_hook_failed",
                    extra={
                        "plugin": name,
                        "subject_kind": subject_kind,
                        "subject_id": subject_id,
                        "error_type": type(exc).__name__,
                    },
                )
        return tuple(failures)

    def _entry(self, name: str) -> _Entry:
        try:
            return self._entries[name]
        except KeyError as exc:
            raise KeyError(f"unknown plugin: {name}") from exc

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip().lower()
        if not normalized or not normalized.replace("_", "").replace("-", "").isalnum():
            raise ValueError("plugin name must contain only letters, digits, '-' or '_'")
        return normalized


class _UnavailablePlugin:
    async def start(self, bot: Any) -> None:
        raise RuntimeError("plugin unavailable")

    async def stop(self) -> None:
        return None


class PluginManifestError(RuntimeError):
    """Plugin manifestと実際のpackageが一致しない。"""


def discover_plugins(
    manager: PluginManager,
    package_name: str,
    *,
    manifest: tuple[str, ...] | None = None,
) -> None:
    """指定package直下から ``setup(manager)`` を持つpluginを検出する。

    ``manifest`` を指定しない場合は、従来どおり各moduleの失敗を隔離して検出を続行する。
    指定した場合は全直下moduleを先にimportし、callableなsetupの集合がmanifestと
    完全一致することを確認してからmanifest順に登録する。
    """
    package = importlib.import_module(package_name)

    if manifest is None:
        _discover_plugins_compat(manager, package_name, package.__path__)
        return

    normalized_manifest = tuple(manager._normalize_name(name) for name in manifest)
    duplicate_names = sorted({name for name in normalized_manifest if normalized_manifest.count(name) > 1})
    if duplicate_names:
        raise PluginManifestError(f"duplicate plugin manifest entries: {', '.join(duplicate_names)}")

    module_infos = sorted(
        pkgutil.iter_modules(package.__path__, prefix=f"{package_name}."),
        key=lambda item: item.name,
    )
    setup_modules: dict[str, Callable[[PluginManager], None]] = {}
    import_failures: list[tuple[str, str]] = []
    for module_info in module_infos:
        short_name = module_info.name.rsplit(".", 1)[-1]
        try:
            module = importlib.import_module(module_info.name)
        except Exception as exc:
            import_failures.append((short_name, type(exc).__name__))
            continue
        setup = getattr(module, "setup", None)
        if callable(setup):
            setup_modules[short_name] = setup

    if import_failures:
        details = ", ".join(f"{name} ({error})" for name, error in import_failures)
        raise PluginManifestError(f"plugin module import failed: {details}")

    manifest_names = set(normalized_manifest)
    setup_names = set(setup_modules)
    missing = sorted(manifest_names - setup_names)
    unlisted = sorted(setup_names - manifest_names)
    if missing or unlisted:
        details: list[str] = []
        if missing:
            details.append(f"missing setup modules: {', '.join(missing)}")
        if unlisted:
            details.append(f"unlisted setup modules: {', '.join(unlisted)}")
        raise PluginManifestError("; ".join(details))

    initial_entries = manager._entries.copy()
    try:
        for name in normalized_manifest:
            before = frozenset(manager._entries)
            setup_modules[name](manager)
            registered = frozenset(manager._entries) - before
            if registered != {name}:
                actual = ", ".join(sorted(registered)) or "none"
                raise PluginManifestError(
                    f"plugin setup must register exactly its module name: {name}; registered: {actual}"
                )
    except Exception as exc:
        manager._entries.clear()
        manager._entries.update(initial_entries)
        if isinstance(exc, PluginManifestError):
            raise
        raise PluginManifestError(f"plugin setup failed: {name} ({type(exc).__name__})") from exc


def _discover_plugins_compat(
    manager: PluginManager,
    package_name: str,
    package_path: Any,
) -> None:
    for module_info in pkgutil.iter_modules(package_path, prefix=f"{package_name}."):
        short_name = module_info.name.rsplit(".", 1)[-1]
        try:
            module = importlib.import_module(module_info.name)
            setup = getattr(module, "setup", None)
            if callable(setup):
                setup(manager)
        except Exception as exc:
            manager.record_failure(short_name, type(exc).__name__)
            logger.error(
                "plugin_discovery_failed",
                extra={"plugin": short_name, "error_type": type(exc).__name__},
            )
