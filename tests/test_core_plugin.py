from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from yonerai_discord.modules.ai import AIPlugin
from yonerai_discord.modules.jobs.plugin import JobsPlugin
from yonerai_discord.modules.scheduling.plugin import SchedulingPlugin
from yonerai_discord.modules.voice import VoicePlugin
from yonerai_discord.plugin import PluginManager, PluginStatus, discover_plugins
from yonerai_discord.plugin_manifest import BUILTIN_PLUGIN_MANIFEST


class RecordingPlugin:
    def __init__(self, events: list[str], name: str, fail_start: bool = False, fail_stop: bool = False) -> None:
        self.events = events
        self.name = name
        self.fail_start = fail_start
        self.fail_stop = fail_stop

    async def start(self, bot: object) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise RuntimeError("起動失敗")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError("停止失敗")


class PolicyAwarePlugin(RecordingPlugin):
    def __init__(self, events: list[str], name: str, *, fail_policy: bool = False) -> None:
        super().__init__(events, name)
        self.fail_policy = fail_policy

    async def on_module_policy_changed(self, module_id: str, enabled: bool, guild_id: int | None) -> None:
        self.events.append(f"policy:{self.name}:{module_id}:{enabled}:{guild_id}")
        if self.fail_policy:
            raise RuntimeError("policy hook failed")

    async def on_capability_policy_changed(
        self,
        capability_id: str,
        enabled: bool,
        guild_id: int | None,
    ) -> None:
        self.events.append(f"capability:{self.name}:{capability_id}:{enabled}:{guild_id}")
        if self.fail_policy:
            raise RuntimeError("policy hook failed")


class QuiescePlugin(RecordingPlugin):
    def __init__(
        self,
        events: list[str],
        name: str,
        *,
        async_hook: bool = True,
        fail: bool = False,
        wait_forever: bool = False,
    ) -> None:
        super().__init__(events, name)
        self.async_hook = async_hook
        self.fail = fail
        self.wait_forever = wait_forever
        if not async_hook:
            self.begin_close = self._begin_close_sync  # type: ignore[method-assign]

    async def begin_close(self) -> None:
        self.events.append(f"quiesce:{self.name}")
        if self.fail:
            raise RuntimeError("quiesce failed")
        if self.wait_forever:
            await asyncio.Event().wait()

    def _begin_close_sync(self) -> None:
        self.events.append(f"quiesce:{self.name}")
        if self.fail:
            raise RuntimeError("quiesce failed")
        if self.wait_forever:
            threading.Event().wait()


def test_plugin_failures_are_isolated_and_shutdown_is_reversed() -> None:
    async def scenario() -> None:
        events: list[str] = []
        manager = PluginManager()
        manager.register("first", lambda: RecordingPlugin(events, "first"))
        manager.register("broken", lambda: RecordingPlugin(events, "broken", fail_start=True))
        manager.register("last", lambda: RecordingPlugin(events, "last"))

        await manager.start_all(object(), enabled={"first", "broken", "last"})
        snapshots = {item.name: item for item in manager.snapshots()}
        assert snapshots["first"].status is PluginStatus.RUNNING
        assert snapshots["broken"].status is PluginStatus.FAILED
        assert snapshots["last"].status is PluginStatus.RUNNING
        assert not manager.healthy()

        await manager.stop_all()
        assert events == ["start:first", "start:broken", "stop:broken", "start:last", "stop:last", "stop:first"]

    asyncio.run(scenario())


def test_failed_start_cleanup_failure_is_isolated_and_later_plugins_still_start() -> None:
    async def scenario() -> None:
        events: list[str] = []
        manager = PluginManager()
        manager.register(
            "broken",
            lambda: RecordingPlugin(events, "broken", fail_start=True, fail_stop=True),
        )
        manager.register("later", lambda: RecordingPlugin(events, "later"))

        await manager.start_all(object(), enabled={"broken", "later"})

        assert manager.status("broken") is PluginStatus.FAILED
        assert manager.status("later") is PluginStatus.RUNNING
        assert events == ["start:broken", "stop:broken", "start:later"]

    asyncio.run(scenario())


def test_empty_allowlist_disables_every_plugin_instead_of_failing_open() -> None:
    async def scenario() -> None:
        events: list[str] = []
        manager = PluginManager()
        manager.register("first", lambda: RecordingPlugin(events, "first"))
        manager.register("second", lambda: RecordingPlugin(events, "second"))

        await manager.start_all(object(), enabled=set())

        assert events == []
        assert {item.status for item in manager.snapshots()} == {PluginStatus.DISABLED}

    asyncio.run(scenario())


def test_enabled_plugin_filter_and_runtime_disable() -> None:
    async def scenario() -> None:
        events: list[str] = []
        manager = PluginManager()
        manager.register("enabled", lambda: RecordingPlugin(events, "enabled"))
        manager.register("off", lambda: RecordingPlugin(events, "off"))
        await manager.start_all(object(), enabled={"enabled"})
        snapshots = {item.name: item.status for item in manager.snapshots()}
        assert snapshots == {"enabled": PluginStatus.RUNNING, "off": PluginStatus.DISABLED}
        assert await manager.disable("enabled")
        assert events == ["start:enabled", "stop:enabled"]

    asyncio.run(scenario())


def test_unknown_enabled_plugin_makes_health_unhealthy() -> None:
    async def scenario() -> None:
        manager = PluginManager()
        await manager.start_all(object(), enabled={"missing"})
        assert not manager.healthy()
        assert manager.snapshots()[0].name == "missing"
        assert manager.snapshots()[0].status is PluginStatus.FAILED

    asyncio.run(scenario())


def test_duplicate_and_invalid_plugin_names_are_rejected() -> None:
    manager = PluginManager()
    manager.register("safe-plugin", lambda: RecordingPlugin([], "safe"))
    with pytest.raises(ValueError, match="already registered"):
        manager.register("SAFE-PLUGIN", lambda: RecordingPlugin([], "other"))
    with pytest.raises(ValueError, match="plugin name"):
        manager.register("bad name", lambda: RecordingPlugin([], "bad"))


def test_builtin_plugins_are_discovered() -> None:
    manager = PluginManager()
    discover_plugins(
        manager,
        "yonerai_discord.modules",
        manifest=BUILTIN_PLUGIN_MANIFEST,
    )
    names = tuple(snapshot.name for snapshot in manager.snapshots())
    assert set(names) == set(BUILTIN_PLUGIN_MANIFEST)
    assert "image_generation" in names
    assert "video_generation" in names
    assert "music_generation" in names


def test_module_policy_hooks_are_isolated_and_report_failures() -> None:
    async def scenario() -> None:
        events: list[str] = []
        manager = PluginManager()
        manager.register("first", lambda: PolicyAwarePlugin(events, "first"))
        manager.register("broken", lambda: PolicyAwarePlugin(events, "broken", fail_policy=True))
        manager.register("plain", lambda: RecordingPlugin(events, "plain"))
        await manager.start_all(object(), enabled={"first", "broken", "plain"})

        failures = await manager.notify_module_policy("media.music", False, 123)

        assert failures == ("broken",)
        assert "policy:first:media.music:False:123" in events
        assert "policy:broken:media.music:False:123" in events
        assert manager.status("first") is PluginStatus.RUNNING
        assert manager.status("broken") is PluginStatus.RUNNING

        capability_failures = await manager.notify_capability_policy("cap-run-earthquake-delivery", False, None)
        assert capability_failures == ("broken",)
        assert "capability:first:cap-run-earthquake-delivery:False:None" in events
        assert "capability:broken:cap-run-earthquake-delivery:False:None" in events

    asyncio.run(scenario())


def test_quiesce_all_calls_every_hook_before_stop_order_and_supports_sync_hooks() -> None:
    async def scenario() -> None:
        events: list[str] = []
        manager = PluginManager()
        manager.register("first", lambda: QuiescePlugin(events, "first", async_hook=False))
        manager.register("plain", lambda: RecordingPlugin(events, "plain"))
        manager.register("last", lambda: QuiescePlugin(events, "last"))
        await manager.start_all(object(), enabled={"first", "plain", "last"})

        failures = await manager.quiesce_all(timeout_per_plugin=0.5)
        await manager.stop_all()

        assert failures == ()
        first_stop = min(index for index, event in enumerate(events) if event.startswith("stop:"))
        assert set(events[:first_stop]) >= {
            "start:first",
            "start:plain",
            "start:last",
            "quiesce:first",
            "quiesce:last",
        }
        assert events[first_stop:] == ["stop:last", "stop:plain", "stop:first"]

    asyncio.run(scenario())


def test_quiesce_timeout_and_failure_are_isolated_without_skipping_stop() -> None:
    async def scenario() -> None:
        events: list[str] = []
        manager = PluginManager()
        manager.register("timeout", lambda: QuiescePlugin(events, "timeout", wait_forever=True))
        manager.register(
            "sync-timeout",
            lambda: QuiescePlugin(events, "sync-timeout", async_hook=False, wait_forever=True),
        )
        manager.register("broken", lambda: QuiescePlugin(events, "broken", fail=True))
        manager.register("healthy", lambda: QuiescePlugin(events, "healthy"))
        await manager.start_all(object(), enabled={"timeout", "sync-timeout", "broken", "healthy"})

        failures = await manager.quiesce_all(timeout_per_plugin=0.01)

        assert failures == ("timeout", "sync-timeout", "broken")
        assert "quiesce:healthy" in events
        assert all(
            manager.status(name) is PluginStatus.RUNNING for name in ("timeout", "sync-timeout", "broken", "healthy")
        )

        await manager.stop_all()
        assert events[-4:] == ["stop:healthy", "stop:broken", "stop:sync-timeout", "stop:timeout"]

    asyncio.run(scenario())


def test_quiesce_all_is_backward_compatible_when_no_plugin_has_hook() -> None:
    async def scenario() -> None:
        events: list[str] = []
        manager = PluginManager()
        manager.register("plain", lambda: RecordingPlugin(events, "plain"))
        await manager.start_all(object(), enabled={"plain"})

        assert await manager.quiesce_all(timeout_per_plugin=0.1) == ()
        assert manager.status("plain") is PluginStatus.RUNNING
        await manager.stop_all()
        assert events == ["start:plain", "stop:plain"]

    asyncio.run(scenario())


def test_real_long_running_plugin_hooks_all_quiesce_before_any_stop() -> None:
    async def scenario() -> None:
        events: list[str] = []
        manager = PluginManager()

        async def ai_close() -> None:
            events.append("quiesce:ai")

        async def voice_close() -> None:
            events.append("quiesce:voice")

        ai = AIPlugin()
        ai._mention_listener = SimpleNamespace(begin_close=ai_close)
        ai._action_router = SimpleNamespace(begin_close=lambda: None)
        jobs = JobsPlugin()
        jobs.worker = SimpleNamespace(request_stop=lambda: events.append("quiesce:jobs"))
        scheduling = SchedulingPlugin()
        scheduling.worker = SimpleNamespace(request_stop=lambda: events.append("quiesce:scheduling"))
        voice = VoicePlugin()
        voice.queue = SimpleNamespace(close=voice_close)
        instances = {
            "ai": ai,
            "jobs": jobs,
            "scheduling": scheduling,
            "voice": voice,
        }

        for name, instance in instances.items():
            original_stop = instance.stop

            async def start(_bot: object, *, plugin_name: str = name) -> None:
                events.append(f"start:{plugin_name}")

            async def stop(*, plugin_name: str = name, original=original_stop) -> None:
                events.append(f"stop:{plugin_name}")
                await original()

            instance.start = start  # type: ignore[method-assign]
            instance.stop = stop  # type: ignore[method-assign]
            manager.register(name, lambda value=instance: value)

        await manager.start_all(object(), enabled=set(instances))
        assert await manager.quiesce_all(timeout_per_plugin=0.5) == ()
        await manager.stop_all()

        first_stop = min(index for index, event in enumerate(events) if event.startswith("stop:"))
        assert set(events[:first_stop]) >= {
            "quiesce:ai",
            "quiesce:jobs",
            "quiesce:scheduling",
            "quiesce:voice",
        }

    asyncio.run(scenario())
