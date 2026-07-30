from __future__ import annotations

import asyncio

import pytest

from yonerai_discord.modules.ai.admission import AIAdmissionController, AdmissionRejection


@pytest.mark.asyncio
async def test_same_guild_is_fifo_across_users_while_other_guild_runs() -> None:
    admission = AIAdmissionController(max_global=2, max_waiters=4, wait_timeout_seconds=1.0)
    first = await admission.acquire(guild_id=1, channel_id=10, user_id=1)
    assert first.lease is not None

    same_guild_waiter = asyncio.create_task(admission.acquire(guild_id=1, channel_id=11, user_id=2))
    same_guild_second = asyncio.create_task(admission.acquire(guild_id=1, channel_id=12, user_id=4))
    await asyncio.sleep(0)
    other_guild = await admission.acquire(guild_id=2, channel_id=20, user_id=3)
    assert other_guild.lease is not None
    assert admission.stats().active == 2
    assert admission.stats().waiting == 2
    assert same_guild_waiter.done() is False
    assert same_guild_second.done() is False

    await first.lease.release()
    same_guild = await same_guild_waiter
    assert same_guild.lease is not None
    assert same_guild_second.done() is False
    await same_guild.lease.release()
    same_guild_last = await same_guild_second
    assert same_guild_last.lease is not None

    await other_guild.lease.release()
    await same_guild_last.lease.release()
    assert admission.stats().active == 0


@pytest.mark.asyncio
async def test_dm_scope_uses_channel_and_user() -> None:
    admission = AIAdmissionController(max_global=2, max_waiters=4, wait_timeout_seconds=1.0)
    first = await admission.acquire(guild_id=None, channel_id=10, user_id=1)
    assert first.lease is not None

    same_scope_waiter = asyncio.create_task(admission.acquire(guild_id=None, channel_id=10, user_id=1))
    await asyncio.sleep(0)
    other_channel = await admission.acquire(guild_id=None, channel_id=11, user_id=1)
    assert other_channel.lease is not None
    assert same_scope_waiter.done() is False

    await first.lease.release()
    same_scope = await same_scope_waiter
    assert same_scope.lease is not None
    await other_channel.lease.release()
    await same_scope.lease.release()


@pytest.mark.asyncio
async def test_wait_queue_is_bounded_and_timeout_cleans_ticket() -> None:
    admission = AIAdmissionController(max_global=1, max_waiters=1, wait_timeout_seconds=0.1)
    active = await admission.acquire(guild_id=1, channel_id=10, user_id=1)
    assert active.lease is not None
    waiter = asyncio.create_task(admission.acquire(guild_id=2, channel_id=20, user_id=2))
    await asyncio.sleep(0)

    rejected = await admission.acquire(guild_id=3, channel_id=30, user_id=3)
    assert rejected.rejection is AdmissionRejection.QUEUE_FULL
    timed_out = await waiter
    assert timed_out.rejection is AdmissionRejection.TIMEOUT
    assert admission.stats().waiting == 0
    await active.lease.release()


@pytest.mark.asyncio
async def test_cancelled_waiter_cleans_ticket() -> None:
    admission = AIAdmissionController(max_global=1, max_waiters=1, wait_timeout_seconds=1.0)
    active = await admission.acquire(guild_id=1, channel_id=10, user_id=1)
    assert active.lease is not None
    waiter = asyncio.create_task(admission.acquire(guild_id=2, channel_id=20, user_id=2))
    await asyncio.sleep(0)
    assert admission.stats().waiting == 1

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert admission.stats().waiting == 0
    await active.lease.release()


@pytest.mark.asyncio
async def test_lease_releases_on_exception_and_shutdown_wakes_waiters() -> None:
    admission = AIAdmissionController(max_global=1, max_waiters=2, wait_timeout_seconds=1.0)
    decision = await admission.acquire(guild_id=1, channel_id=10, user_id=1)
    assert decision.lease is not None
    with pytest.raises(RuntimeError, match="boom"):
        async with decision.lease:
            raise RuntimeError("boom")
    assert admission.stats().active == 0

    active = await admission.acquire(guild_id=1, channel_id=10, user_id=1)
    assert active.lease is not None
    waiter = asyncio.create_task(admission.acquire(guild_id=2, channel_id=20, user_id=2))
    await asyncio.sleep(0)
    await admission.begin_close()
    assert (await waiter).rejection is AdmissionRejection.CLOSING
    assert (await admission.acquire(guild_id=3, channel_id=30, user_id=3)).rejection is AdmissionRejection.CLOSING
    assert await admission.drain(timeout_seconds=0.1) is False
    await active.lease.release()
    assert await admission.drain(timeout_seconds=0.1) is True


def test_stats_expose_counts_only() -> None:
    admission = AIAdmissionController()
    stats = admission.stats()
    assert (stats.active, stats.waiting, stats.active_keys, stats.closing) == (0, 0, 0, False)
    assert not hasattr(stats, "guild_id")
    assert not hasattr(stats, "user_id")


@pytest.mark.parametrize(
    ("guild_id", "channel_id", "user_id"),
    [
        (True, 1, 1),
        (0, 1, 1),
        (1, 0, 1),
        (1, None, 1),
        (1, False, 1),
        (1, 1, 0),
        (1, 1, None),
        (1, 1, True),
    ],
)
@pytest.mark.asyncio
async def test_scope_ids_fail_closed(
    guild_id: object,
    channel_id: object,
    user_id: object,
) -> None:
    admission = AIAdmissionController()
    with pytest.raises(ValueError):
        await admission.acquire(
            guild_id=guild_id,  # type: ignore[arg-type]
            channel_id=channel_id,  # type: ignore[arg-type]
            user_id=user_id,  # type: ignore[arg-type]
        )
