from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from yonerai_discord.modules.earthquake import (
    EARTHQUAKE_DELIVERY_CAPABILITY_ID,
    EarthquakePlugin,
    SqliteEarthquakeRepository,
    setup,
)
from test_earthquake_helpers import isolated_workspace_directory


class Tree:
    def __init__(self) -> None:
        self.commands = {}

    def add_command(self, command) -> None:
        self.commands[command.name] = command

    def remove_command(self, name, **_kwargs):
        return self.commands.pop(name, None)


class BlockingWebSocket:
    def __init__(self) -> None:
        self.wait = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.wait.wait()
        raise StopAsyncIteration


class BlockingContext:
    def __init__(self) -> None:
        self.websocket = BlockingWebSocket()
        self.entered = asyncio.Event()
        self.exited = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        return self.websocket

    async def __aexit__(self, *_args):
        self.exited.set()


class FakeSession:
    def __init__(self) -> None:
        self.contexts = []

    def ws_connect(self, url, **kwargs):
        context = BlockingContext()
        self.contexts.append((url, kwargs, context))
        return context

    async def get(self, *_args, **_kwargs):
        raise AssertionError("initial websocket connection must not poll history")


class CapabilityRegistry:
    def __init__(self) -> None:
        self.executable_by_guild: dict[int, bool] = {}
        self.failing_guilds: set[int] = set()

    def capability_status(self, capability_id: str, guild_id: int) -> SimpleNamespace:
        assert capability_id == EARTHQUAKE_DELIVERY_CAPABILITY_ID
        if guild_id in self.failing_guilds:
            raise RuntimeError("registry unavailable")
        return SimpleNamespace(executable=self.executable_by_guild.get(guild_id, True))


def bot(tmp_path, session, *, registry=None):
    return SimpleNamespace(
        settings=SimpleNamespace(
            database_path=tmp_path / "suite.sqlite3",
            earthquake_reconnect_base_seconds=0.01,
            earthquake_reconnect_max_seconds=0.02,
        ),
        earthquake_http_session=session,
        earthquake_delivery_policy=lambda *_args: True,
        capability_registry=registry or CapabilityRegistry(),
        tree=Tree(),
    )


def test_setup_registers_plugin_contract() -> None:
    calls = []
    setup(SimpleNamespace(register=lambda name, factory: calls.append((name, factory))))
    assert calls == [("earthquake", EarthquakePlugin)]
    assert EARTHQUAKE_DELIVERY_CAPABILITY_ID == "cap-run-earthquake-delivery"


@pytest.mark.asyncio
async def test_zero_subscriptions_start_offline_without_websocket() -> None:
    with isolated_workspace_directory() as directory:
        session = FakeSession()
        value_bot = bot(directory, session)
        plugin = EarthquakePlugin()
        await plugin.start(value_bot)
        try:
            await asyncio.sleep(0)
            assert session.contexts == []
            assert plugin.worker is None
            assert value_bot.earthquake_feed_worker is None
            assert "earthquake" in value_bot.tree.commands
            assert plugin.repository is not None and plugin.repository.is_open
        finally:
            await plugin.stop()
        assert "earthquake" not in value_bot.tree.commands
        assert not hasattr(value_bot, "earthquake_service")


@pytest.mark.asyncio
async def test_worker_lazy_starts_once_and_stops_after_last_unsubscribe() -> None:
    with isolated_workspace_directory() as directory:
        session = FakeSession()
        value_bot = bot(directory, session)
        plugin = EarthquakePlugin()
        await plugin.start(value_bot)
        try:
            assert plugin.repository is not None
            plugin.repository.subscribe(1, 10)
            await plugin.reconcile_subscriptions()
            async with asyncio.timeout(2):
                while not session.contexts:
                    await asyncio.sleep(0)
                await session.contexts[0][2].entered.wait()
            assert plugin.worker is not None and plugin.worker.running

            await plugin.reconcile_subscriptions()
            plugin.repository.subscribe(2, 20)
            await plugin.reconcile_subscriptions()
            assert len(session.contexts) == 1

            plugin.repository.unsubscribe(1)
            await plugin.reconcile_subscriptions()
            assert plugin.worker is not None
            plugin.repository.unsubscribe(2)
            await plugin.reconcile_subscriptions()
            assert session.contexts[0][2].exited.is_set()
            assert plugin.worker is None
            assert value_bot.earthquake_feed_worker is None

            plugin.repository.subscribe(1, 10)
            await plugin.reconcile_subscriptions()
            async with asyncio.timeout(2):
                while len(session.contexts) < 2:
                    await asyncio.sleep(0)
        finally:
            await plugin.stop()
        assert session.contexts[-1][2].exited.is_set()


@pytest.mark.asyncio
async def test_begin_close_stops_worker_and_prevents_restart() -> None:
    with isolated_workspace_directory() as directory:
        session = FakeSession()
        value_bot = bot(directory, session)
        plugin = EarthquakePlugin()
        await plugin.start(value_bot)
        try:
            assert plugin.repository is not None
            plugin.repository.subscribe(1, 10)
            await plugin.reconcile_subscriptions()
            async with asyncio.timeout(2):
                while not session.contexts:
                    await asyncio.sleep(0)
                await session.contexts[0][2].entered.wait()

            await plugin.begin_close()

            assert session.contexts[0][2].exited.is_set()
            assert plugin.worker is None
            await plugin.reconcile_subscriptions()
            assert len(session.contexts) == 1
        finally:
            await plugin.stop()


@pytest.mark.asyncio
async def test_existing_enabled_subscription_starts_worker_on_plugin_start() -> None:
    with isolated_workspace_directory() as directory:
        repository = SqliteEarthquakeRepository(directory / "suite.sqlite3")
        repository.open()
        repository.subscribe(1, 10)
        repository.close()

        session = FakeSession()
        plugin = EarthquakePlugin()
        await plugin.start(bot(directory, session))
        try:
            async with asyncio.timeout(2):
                while not session.contexts:
                    await asyncio.sleep(0)
        finally:
            await plugin.stop()


@pytest.mark.asyncio
async def test_process_singleton_is_claimed_only_by_active_worker() -> None:
    with isolated_workspace_directory() as directory:
        first_session = FakeSession()
        second_session = FakeSession()
        first = EarthquakePlugin()
        second = EarthquakePlugin()
        await first.start(bot(directory / "first", first_session))
        await second.start(bot(directory / "second", second_session))
        try:
            assert first.repository is not None and second.repository is not None
            first.repository.subscribe(1, 10)
            await first.reconcile_subscriptions()
            second.repository.subscribe(2, 20)
            with pytest.raises(RuntimeError, match="process-wide"):
                await second.reconcile_subscriptions()
            assert second_session.contexts == []

            first.repository.unsubscribe(1)
            await first.reconcile_subscriptions()
            await second.reconcile_subscriptions()
            async with asyncio.timeout(2):
                while not second_session.contexts:
                    await asyncio.sleep(0)
        finally:
            await second.stop()
            await first.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_id",
    ["operations.earthquake", "operations.scheduling-notification"],
)
async def test_relevant_module_policy_change_stops_and_restarts_worker(module_id: str) -> None:
    with isolated_workspace_directory() as directory:
        session = FakeSession()
        registry = CapabilityRegistry()
        plugin = EarthquakePlugin()
        await plugin.start(bot(directory, session, registry=registry))
        try:
            assert plugin.repository is not None
            plugin.repository.subscribe(1, 10)
            await plugin.reconcile_subscriptions()
            async with asyncio.timeout(2):
                while not session.contexts:
                    await asyncio.sleep(0)
                await session.contexts[0][2].entered.wait()

            registry.executable_by_guild[1] = False
            await plugin.on_module_policy_changed(module_id, False, 1)
            assert session.contexts[0][2].exited.is_set()
            assert plugin.worker is None

            registry.executable_by_guild[1] = True
            await plugin.on_module_policy_changed(module_id, True, 1)
            async with asyncio.timeout(2):
                while len(session.contexts) < 2:
                    await asyncio.sleep(0)
            assert plugin.worker is not None
        finally:
            await plugin.stop()


@pytest.mark.asyncio
async def test_policy_change_for_one_guild_keeps_worker_for_another_executable_subscription() -> None:
    with isolated_workspace_directory() as directory:
        session = FakeSession()
        registry = CapabilityRegistry()
        plugin = EarthquakePlugin()
        await plugin.start(bot(directory, session, registry=registry))
        try:
            assert plugin.repository is not None
            plugin.repository.subscribe(1, 10)
            plugin.repository.subscribe(2, 20)
            await plugin.reconcile_subscriptions()
            async with asyncio.timeout(2):
                while not session.contexts:
                    await asyncio.sleep(0)
                await session.contexts[0][2].entered.wait()
            worker = plugin.worker

            registry.executable_by_guild[1] = False
            await plugin.on_module_policy_changed("operations.earthquake", False, 1)
            assert plugin.worker is worker
            assert not session.contexts[0][2].exited.is_set()
            assert len(session.contexts) == 1

            registry.executable_by_guild[2] = False
            await plugin.on_module_policy_changed("operations.earthquake", False, 2)
            assert session.contexts[0][2].exited.is_set()
            assert plugin.worker is None
        finally:
            await plugin.stop()


@pytest.mark.asyncio
async def test_delivery_capability_policy_change_stops_and_restarts_worker() -> None:
    with isolated_workspace_directory() as directory:
        session = FakeSession()
        registry = CapabilityRegistry()
        plugin = EarthquakePlugin()
        await plugin.start(bot(directory, session, registry=registry))
        try:
            assert plugin.repository is not None
            plugin.repository.subscribe(1, 10)
            await plugin.reconcile_subscriptions()
            async with asyncio.timeout(2):
                while not session.contexts:
                    await asyncio.sleep(0)

            registry.executable_by_guild[1] = False
            await plugin.on_capability_policy_changed(EARTHQUAKE_DELIVERY_CAPABILITY_ID, False, 1)
            assert session.contexts[0][2].exited.is_set()
            assert plugin.worker is None

            registry.executable_by_guild[1] = True
            await plugin.on_capability_policy_changed(EARTHQUAKE_DELIVERY_CAPABILITY_ID, True, 1)
            async with asyncio.timeout(2):
                while len(session.contexts) < 2:
                    await asyncio.sleep(0)
            assert plugin.worker is not None
        finally:
            await plugin.stop()


@pytest.mark.asyncio
async def test_registry_missing_or_failure_keeps_websocket_fail_closed() -> None:
    with isolated_workspace_directory() as directory:
        session = FakeSession()
        value_bot = bot(directory, session)
        plugin = EarthquakePlugin()
        await plugin.start(value_bot)
        try:
            assert plugin.repository is not None
            plugin.repository.subscribe(1, 10)

            del value_bot.capability_registry
            await plugin.reconcile_subscriptions()
            assert session.contexts == []
            assert plugin.worker is None

            failing_registry = CapabilityRegistry()
            failing_registry.failing_guilds.add(1)
            value_bot.capability_registry = failing_registry
            await plugin.reconcile_subscriptions()
            assert session.contexts == []
            assert plugin.worker is None

            value_bot.capability_registry = CapabilityRegistry()
            await plugin.reconcile_subscriptions()
            async with asyncio.timeout(2):
                while not session.contexts:
                    await asyncio.sleep(0)
            assert plugin.worker is not None
        finally:
            await plugin.stop()
