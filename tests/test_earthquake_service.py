from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

import pytest

from yonerai_discord.modules.earthquake import (
    EarthquakeFeedWorker,
    EarthquakeService,
    ExponentialBackoff,
    SqliteEarthquakeRepository,
)
from test_earthquake_helpers import isolated_workspace_directory


NOW = datetime(2026, 7, 21, 12, 0, 30, tzinfo=UTC)


def quake_payload(event_id="quake-1", *, scale=50):
    return {
        "id": event_id,
        "code": 551,
        "time": "2026-07-21T12:00:01Z",
        "issue": {"source": "気象庁", "time": "2026-07-21T12:00:00Z", "type": "DetailScale"},
        "earthquake": {
            "hypocenter": {"name": "東京湾", "depth": 40, "magnitude": 4.8},
            "maxScale": scale,
        },
    }


def eew_payload(event_id="eew-1", *, issue_time="2026-07-21T12:00:00Z", scale=60):
    return {
        "id": event_id,
        "code": 556,
        "time": issue_time,
        "issue": {"source": "気象庁", "time": issue_time, "type": "緊急地震速報（警報）"},
        "earthquake": {
            "hypocenter": {"name": "茨城県南部", "depth": 50},
            "magnitude": 5.2,
            "maxScale": scale,
        },
        "areas": [],
        "cancelled": False,
    }


class FakeClient:
    def __init__(self, history=()) -> None:
        self.history = tuple(history)
        self.history_calls = []

    async def fetch_history(self, *, codes=(551, 556), limit=20):
        self.history_calls.append((codes, limit))
        return self.history[:limit]


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, subscription, event) -> bool:
        self.sent.append((subscription, event))
        return True


@pytest.fixture
def repository():
    with isolated_workspace_directory() as directory:
        value = SqliteEarthquakeRepository(directory / "suite.sqlite3")
        value.open()
        yield value
        value.close()


@pytest.mark.asyncio
async def test_dispatch_filters_by_subscription_and_deduplicates_id_plus_hash(repository) -> None:
    repository.subscribe(1, 10, min_scale=50, notify_551=True, notify_556=False)
    repository.subscribe(2, 20, min_scale=60, notify_551=True, notify_556=True)
    notifier = RecordingNotifier()
    service = EarthquakeService(FakeClient(), repository, notifier, clock=lambda: NOW)

    assert await service.handle_payload(quake_payload()) == 1
    assert await service.handle_payload(quake_payload()) == 0
    assert [item[0].guild_id for item in notifier.sent] == [1]

    corrected = quake_payload(scale=60)
    assert await service.handle_payload(corrected) == 2
    assert service.snapshot().duplicate_payloads == 1
    assert service.snapshot().delivered_notifications == 3


@pytest.mark.asyncio
async def test_new_service_instance_uses_persistent_dedupe(repository) -> None:
    repository.subscribe(1, 10, min_scale=40)
    first_notifier = RecordingNotifier()
    first = EarthquakeService(FakeClient(), repository, first_notifier, clock=lambda: NOW)
    assert await first.handle_payload(quake_payload("persistent")) == 1

    restarted_notifier = RecordingNotifier()
    restarted = EarthquakeService(FakeClient(), repository, restarted_notifier, clock=lambda: NOW)
    assert await restarted.handle_payload(quake_payload("persistent")) == 0
    assert restarted_notifier.sent == []
    assert restarted.snapshot().duplicate_payloads == 1
    assert await restarted.handle_payload(quake_payload("persistent", scale=55)) == 1
    assert [item[1].max_scale for item in restarted_notifier.sent] == [55]


@pytest.mark.asyncio
async def test_unknown_invalid_and_old_eew_never_notify(repository) -> None:
    repository.subscribe(1, 10, min_scale=40)
    notifier = RecordingNotifier()
    service = EarthquakeService(
        FakeClient(),
        repository,
        notifier,
        clock=lambda: NOW,
        eew_max_age_seconds=120,
    )
    assert await service.handle_payload({"id": "future", "code": 999, "time": "2026-07-21T12:00:00Z"}) == 0
    assert await service.handle_payload({"code": 551}) == 0
    assert await service.handle_payload(eew_payload(issue_time="2026-07-21T11:00:00Z")) == 0
    assert notifier.sent == []
    snapshot = service.snapshot()
    assert snapshot.unknown_payloads == 1
    assert snapshot.invalid_payloads == 1
    assert snapshot.stale_eew_payloads == 1


@pytest.mark.asyncio
async def test_current_eew_notifies_only_eew_subscribers(repository) -> None:
    repository.subscribe(1, 10, min_scale=55, notify_551=False, notify_556=True)
    notifier = RecordingNotifier()
    service = EarthquakeService(FakeClient(), repository, notifier, clock=lambda: NOW)
    assert await service.handle_payload(eew_payload()) == 1
    assert notifier.sent[0][1].code == 556


@pytest.mark.asyncio
async def test_latest_uses_bounded_rest_history_without_dispatch(repository) -> None:
    client = FakeClient([{"code": 551}, quake_payload("latest")])
    notifier = RecordingNotifier()
    service = EarthquakeService(client, repository, notifier, clock=lambda: NOW)
    latest = await service.fetch_latest(limit=5)
    cached = await service.fetch_latest(limit=5)
    assert latest is not None
    assert latest.id == "latest"
    assert cached is latest
    assert client.history_calls == [((551, 556), 5)]
    assert notifier.sent == []


class ReconnectingClient(FakeClient):
    def __init__(self, first_payload, history) -> None:
        super().__init__(history)
        self.first_payload = first_payload
        self.connections = 0
        self.block = asyncio.Event()

    @asynccontextmanager
    async def websocket(self):
        self.connections += 1
        yield self.connections

    async def iter_messages(self, connection):
        if connection == 1:
            yield self.first_payload
            return
        await self.block.wait()
        if False:
            yield {}


@pytest.mark.asyncio
async def test_reconnect_performs_bounded_gap_fill_and_dedupe(repository) -> None:
    repository.subscribe(1, 10, min_scale=40)
    live = quake_payload("live")
    missed = quake_payload("missed", scale=55)
    client = ReconnectingClient(live, [live, missed])
    notifier = RecordingNotifier()
    service = EarthquakeService(client, repository, notifier, clock=lambda: NOW)
    worker = EarthquakeFeedWorker(
        client,  # type: ignore[arg-type]
        service,
        gap_fill_limit=7,
        backoff=ExponentialBackoff(base_seconds=0.01, maximum_seconds=0.02, jitter_ratio=0),
    )
    task = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(2):
            while len(notifier.sent) < 2 or service.snapshot().duplicate_payloads < 1:
                await asyncio.sleep(0.005)
        assert [event.id for _, event in notifier.sent] == ["live", "missed"]
        assert client.history_calls == [((551, 556), 7)]
        assert service.snapshot().reconnects == 1
        assert service.snapshot().duplicate_payloads == 1
    finally:
        worker.request_stop()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        client.block.set()
