from __future__ import annotations

import asyncio

import pytest

from yonerai_discord.modules.voice.models import SpeechRequest, SynthesizedSpeech
from yonerai_discord.modules.voice.service import SpeechQueue, SpeechUnavailableError


class BlockingSynthesizer:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def synthesize(self, _request: SpeechRequest) -> SynthesizedSpeech:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        self.finished.set()
        return SynthesizedSpeech(wav=b"RIFFqueue")


class FailingBlockingSynthesizer(BlockingSynthesizer):
    async def synthesize(self, _request: SpeechRequest) -> SynthesizedSpeech:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        self.finished.set()
        raise RuntimeError("provider detail must remain internal")


def _request(text: str) -> SpeechRequest:
    return SpeechRequest(text=text, guild_id=101, channel_id=202)


async def _wait_for_inflight_cleanup(queue: SpeechQueue) -> None:
    for _ in range(40):
        if not queue._inflight:  # noqa: SLF001 - focused lifecycle assertion
            return
        await asyncio.sleep(0)
    raise AssertionError("in-flight task was not retired")


@pytest.mark.asyncio
async def test_queue_accepts_one_distinct_request_and_returns_speech() -> None:
    provider = BlockingSynthesizer()
    queue = SpeechQueue(provider, max_inflight_requests=1)
    task = asyncio.create_task(queue.synthesize(_request("one")))
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    provider.release.set()

    result = await task

    assert result.wav == b"RIFFqueue"
    assert provider.calls == 1
    await queue.close()


@pytest.mark.asyncio
async def test_queue_rejects_a_second_distinct_request_at_inflight_limit() -> None:
    provider = BlockingSynthesizer()
    queue = SpeechQueue(provider, max_inflight_requests=1)
    first = asyncio.create_task(queue.synthesize(_request("first")))
    await asyncio.wait_for(provider.started.wait(), timeout=1)

    with pytest.raises(SpeechUnavailableError, match="busy"):
        await queue.synthesize(_request("second"))

    provider.release.set()
    await first
    assert provider.calls == 1
    await queue.close()


@pytest.mark.asyncio
async def test_same_key_singleflight_uses_one_inflight_slot() -> None:
    provider = BlockingSynthesizer()
    queue = SpeechQueue(provider, max_inflight_requests=1)
    request = _request("same")
    first = asyncio.create_task(queue.synthesize(request))
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    second = asyncio.create_task(queue.synthesize(request))
    await asyncio.sleep(0)

    provider.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result == second_result
    assert provider.calls == 1
    await queue.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leave_completed_task_inflight() -> None:
    provider = BlockingSynthesizer()
    queue = SpeechQueue(provider, max_inflight_requests=1)
    request = _request("cancelled")
    waiter = asyncio.create_task(queue.synthesize(request))
    await asyncio.wait_for(provider.started.wait(), timeout=1)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    provider.release.set()
    await asyncio.wait_for(provider.finished.wait(), timeout=1)
    await _wait_for_inflight_cleanup(queue)

    cached = await queue.synthesize(request)
    assert cached.wav == b"RIFFqueue"
    assert provider.calls == 1
    await queue.close()


@pytest.mark.asyncio
async def test_completed_result_is_cached_before_last_waiter_postcheck() -> None:
    provider = BlockingSynthesizer()
    queue = SpeechQueue(provider, max_inflight_requests=1)
    postcheck_started = asyncio.Event()
    release_postcheck = asyncio.Event()
    policy_calls = 0

    async def policy() -> bool:
        nonlocal policy_calls
        policy_calls += 1
        if policy_calls == 4:
            postcheck_started.set()
            await release_postcheck.wait()
        return True

    request = _request("cache-before-retire")
    first = asyncio.create_task(queue.synthesize(request, current_policy=policy))
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    provider.release.set()
    await asyncio.wait_for(postcheck_started.wait(), timeout=1)

    second = await queue.synthesize(request)
    assert second.wav == b"RIFFqueue"
    assert provider.calls == 1

    release_postcheck.set()
    await first
    await queue.close()


@pytest.mark.asyncio
async def test_cancelled_sole_waiter_provider_failure_is_observed() -> None:
    provider = FailingBlockingSynthesizer()
    queue = SpeechQueue(provider, max_inflight_requests=1)
    request = _request("cancelled-failure")
    loop = asyncio.get_running_loop()
    errors: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        waiter = asyncio.create_task(queue.synthesize(request))
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        provider.release.set()
        await asyncio.wait_for(provider.finished.wait(), timeout=1)
        await _wait_for_inflight_cleanup(queue)
        await asyncio.sleep(0)
        assert errors == []
    finally:
        loop.set_exception_handler(previous_handler)
        await queue.close()


@pytest.mark.asyncio
async def test_close_cancels_inflight_work_and_keeps_queue_closed() -> None:
    provider = BlockingSynthesizer()
    queue = SpeechQueue(provider, max_inflight_requests=1)
    task = asyncio.create_task(queue.synthesize(_request("closing")))
    await asyncio.wait_for(provider.started.wait(), timeout=1)

    await queue.close()

    with pytest.raises(SpeechUnavailableError):
        await task
    with pytest.raises(SpeechUnavailableError):
        await queue.synthesize(_request("later"))
    assert not queue._inflight  # noqa: SLF001 - focused lifecycle assertion
    assert provider.calls == 1
