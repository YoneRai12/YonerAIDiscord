from __future__ import annotations

import asyncio
import inspect
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from .models import SpeechRequest, SynthesizedSpeech
from .ports import SpeechSynthesizer


CurrentPolicy = Callable[[], bool | Awaitable[bool]]


class SpeechUnavailableError(RuntimeError):
    pass


class SpeechQueue:
    """同一読み上げをまとめ、過負荷時は無制限に溜めない。"""

    def __init__(
        self,
        synthesizer: SpeechSynthesizer | None,
        *,
        max_concurrency: int = 1,
        cache_size: int = 64,
        max_inflight_requests: int = 16,
    ) -> None:
        if max_concurrency < 1 or cache_size < 0 or max_inflight_requests < 1:
            raise ValueError("invalid queue limits")
        self._synthesizer = synthesizer
        self._gate = asyncio.Semaphore(max_concurrency)
        self._cache_size = cache_size
        self._max_inflight_requests = max_inflight_requests
        self._cache: OrderedDict[str, SynthesizedSpeech] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[SynthesizedSpeech]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def available(self) -> bool:
        return self._synthesizer is not None and not self._closed

    async def synthesize(
        self,
        request: SpeechRequest,
        *,
        current_policy: CurrentPolicy | None = None,
    ) -> SynthesizedSpeech:
        self._require_open()
        await self._require_current_policy(current_policy)
        async with self._lock:
            self._require_open()
            cached = self._cache.get(request.key)
            if cached is not None:
                self._cache.move_to_end(request.key)
                return cached
            task = self._inflight.get(request.key)
            if task is None:
                if len(self._inflight) >= self._max_inflight_requests:
                    raise SpeechUnavailableError("speech queue is busy")
                task = asyncio.create_task(self._run_registered(request, current_policy))
                task.add_done_callback(_consume_task_result)
                self._inflight[request.key] = task
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if self._closed:
                raise SpeechUnavailableError("speech queue is closed") from exc
            raise
        self._require_open()
        await self._require_current_policy(current_policy)
        return result

    async def _run(
        self,
        request: SpeechRequest,
        current_policy: CurrentPolicy | None,
    ) -> SynthesizedSpeech:
        assert self._synthesizer is not None
        async with self._gate:
            self._require_open()
            # queue/semaphore待機後、本文をproviderへ渡す直前に再検証する。
            await self._require_current_policy(current_policy)
            try:
                result = await self._synthesizer.synthesize(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise SpeechUnavailableError("speech synthesis failed") from exc
            self._require_open()
            await self._require_current_policy(current_policy)
            return result

    async def _run_registered(
        self,
        request: SpeechRequest,
        current_policy: CurrentPolicy | None,
    ) -> SynthesizedSpeech:
        try:
            result = await self._run(request, current_policy)
            async with self._lock:
                self._require_open()
                self._cache[request.key] = result
                self._cache.move_to_end(request.key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
            return result
        finally:
            current = asyncio.current_task()
            async with self._lock:
                if self._inflight.get(request.key) is current:
                    self._inflight.pop(request.key, None)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = tuple(self._inflight.values())
            self._inflight.clear()
            self._cache.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _require_open(self) -> None:
        if self._closed or self._synthesizer is None:
            raise SpeechUnavailableError("speech synthesizer is not configured")

    @staticmethod
    async def _require_current_policy(current_policy: CurrentPolicy | None) -> None:
        if current_policy is None:
            return
        try:
            result = current_policy()
            if inspect.isawaitable(result):
                result = await result
            allowed = bool(result)
        except Exception:
            allowed = False
        if not allowed:
            raise SpeechUnavailableError("speech synthesis is no longer allowed")


def _consume_task_result(task: asyncio.Task[SynthesizedSpeech]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return
