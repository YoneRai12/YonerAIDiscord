"""Fixed Cloudflare Browser Run recipe to canonical media delivery.

This boundary deliberately does not expose selectors, CDP, arbitrary URLs, or
raw browser bytes to the Action Router.  It runs the single code-owned YouTube
evidence recipe, republishes its two PNGs through ``MediaArtifactStore``, then
uses the existing Discord renderer.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import inspect
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import discord
from PIL import Image

from yonerai_discord.modules.ai.discord_renderer import DiscordAIResponseRenderer
from yonerai_discord.modules.ai.display_preferences import DisplayMode
from yonerai_discord.modules.ai.orchestration import PlanArtifactOutput
from yonerai_discord.modules.media_pipeline.artifacts import MediaArtifactStore
from yonerai_discord.modules.media_pipeline.delivery import MediaArtifactDeliveryPreparer
from yonerai_discord.modules.media_pipeline.domain import ArtifactKind, ArtifactScope
from yonerai_discord.modules.web_runtime.browser import (
    BoundedBrowserRunner,
    BrowserCancellation,
    BrowserCheckpoint,
    BrowserRunCleanupUnconfirmedError,
    BrowserRunContractError,
    BrowserRunPlan,
    BrowserRunScope,
    BrowserRunCancelledError,
    BrowserScreenshotArtifact,
)
from yonerai_discord.modules.web_runtime.cloudflare_browser_run import youtube_playback_evidence_plan


_ACTION_ID = "browser.youtube-playback-evidence"
_MODEL_LABEL = "cloudflare-browser-run"
_PROGRESS_TEXT = "🔄 YouTubeの検索・再生画面を安全なremote browserで確認しています。"
_SUCCESS_TEXT = "YouTubeの検索結果画面と再生後画面を取得しました。"
_FAILURE_TEXT = "remote browser操作を完了できませんでした。設定・権限を確認して再試行してください。"
_IDEMPOTENCY_LIMIT = 256
_DISCORD_IO_TIMEOUT_SECONDS = 15.0
_CLOSE_DRAIN_TIMEOUT_SECONDS = 5.0
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_RETAINED_PNG_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_RETAINED_BYTES = 32 * 1024 * 1024


AuthorizationCurrent = Callable[[], bool | Awaitable[bool]]
AuthorizationCurrentSync = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class BrowserInteractiveRequest:
    request_id: str
    guild_id: int
    channel_id: int
    actor_id: int
    message_id: int
    query: str

    @classmethod
    def from_message(cls, message: object, query: str) -> "BrowserInteractiveRequest":
        values = (
            getattr(getattr(message, "guild", None), "id", None),
            getattr(getattr(message, "channel", None), "id", None),
            getattr(getattr(message, "author", None), "id", None),
            getattr(message, "id", None),
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError("Discord browser scope is unavailable")
        guild_id, channel_id, actor_id, message_id = values
        normalized = query.strip() if isinstance(query, str) else ""
        if normalized != query or not normalized or len(normalized) > 200:
            raise ValueError("browser query is invalid")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("browser query is invalid")
        return cls(
            request_id=f"discord-{message_id}",
            guild_id=guild_id,
            channel_id=channel_id,
            actor_id=actor_id,
            message_id=message_id,
            query=normalized,
        )

    @property
    def scope(self) -> ArtifactScope:
        return ArtifactScope(self.request_id, self.guild_id, self.channel_id, self.actor_id)

    @property
    def browser_scope(self) -> BrowserRunScope:
        return BrowserRunScope(
            request_id=self.request_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            user_id=self.actor_id,
        )

    @property
    def fingerprint(self) -> bytes:
        return hashlib.sha256(self.query.encode("utf-8")).digest()


@dataclass(frozen=True, slots=True)
class _StoredBrowserResume:
    checkpoint: BrowserCheckpoint
    screenshots: tuple[BrowserScreenshotArtifact, ...]


class BrowserInteractiveCheckpointStore:
    """Bounded process-local evidence retained only for an interrupted fixed recipe."""

    def __init__(self, *, limit: int = _IDEMPOTENCY_LIMIT) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _IDEMPOTENCY_LIMIT:
            raise ValueError("checkpoint limit is outside the allowed range")
        self._limit = limit
        self._checkpoints: OrderedDict[tuple[str, str, str], BrowserCheckpoint] = OrderedDict()
        self._screenshots: dict[tuple[str, str, str], dict[str, BrowserScreenshotArtifact]] = {}
        self._total_bytes = 0

    async def save(self, checkpoint: BrowserCheckpoint) -> None:
        if not isinstance(checkpoint, BrowserCheckpoint):
            raise TypeError("checkpoint must be BrowserCheckpoint")
        key = _checkpoint_key(checkpoint)
        previous = self._checkpoints.get(key)
        if previous is not None and checkpoint.next_segment_index < previous.next_segment_index:
            raise ValueError("checkpoint cannot move backwards")
        self._checkpoints[key] = checkpoint
        self._checkpoints.move_to_end(key)
        while len(self._checkpoints) > self._limit:
            self._drop(next(iter(self._checkpoints)))

    async def save_artifacts(
        self,
        checkpoint: BrowserCheckpoint,
        screenshots: tuple[BrowserScreenshotArtifact, ...],
    ) -> None:
        key = _checkpoint_key(checkpoint)
        if self._checkpoints.get(key) != checkpoint:
            raise ValueError("checkpoint is no longer current")
        expected = {reference.artifact_id: reference for reference in checkpoint.artifacts}
        retained = dict(self._screenshots.get(key, {}))
        for screenshot in screenshots:
            if not isinstance(screenshot, BrowserScreenshotArtifact):
                self._drop(key)
                raise TypeError("screenshots must be BrowserScreenshotArtifact values")
            if expected.get(screenshot.reference.artifact_id) != screenshot.reference:
                self._drop(key)
                raise ValueError("screenshot does not match the checkpoint")
            if (
                len(screenshot.data) > _MAX_RETAINED_PNG_BYTES
                or screenshot.reference.media_type != "image/png"
                or not screenshot.data.startswith(_PNG_SIGNATURE)
            ):
                self._drop(key)
                raise ValueError("screenshot exceeds the retained PNG contract")
            retained[screenshot.reference.artifact_id] = screenshot
        if set(retained) != set(expected) or any(
            retained[artifact_id].reference != reference for artifact_id, reference in expected.items()
        ):
            self._drop(key)
            raise ValueError("retained screenshots do not exactly match the checkpoint")
        previous_size = sum(len(item.data) for item in self._screenshots.get(key, {}).values())
        retained_size = sum(len(item.data) for item in retained.values())
        if retained_size > _MAX_TOTAL_RETAINED_BYTES:
            self._drop(key)
            raise ValueError("retained screenshots exceed the byte budget")
        while self._total_bytes - previous_size + retained_size > _MAX_TOTAL_RETAINED_BYTES:
            oldest = next((item for item in self._checkpoints if item != key), None)
            if oldest is None:
                self._drop(key)
                raise ValueError("retained screenshots exceed the byte budget")
            self._drop(oldest)
        self._screenshots[key] = retained
        self._total_bytes = self._total_bytes - previous_size + retained_size

    async def load(self, plan: BrowserRunPlan) -> _StoredBrowserResume | None:
        if not isinstance(plan, BrowserRunPlan):
            raise TypeError("plan must be BrowserRunPlan")
        key = _checkpoint_key_for_plan(plan)
        for stale in tuple(self._checkpoints):
            if stale[0] == plan.plan_id and stale != key:
                self._drop(stale)
        if not self._state_consistent():
            self.clear()
            return None
        checkpoint = self._checkpoints.get(key)
        if checkpoint is None:
            return None
        retained = self._screenshots.get(key, {})
        try:
            screenshots = tuple(retained[reference.artifact_id] for reference in checkpoint.artifacts)
        except KeyError:
            self.discard(plan)
            return None
        self._checkpoints.move_to_end(key)
        return _StoredBrowserResume(checkpoint=checkpoint, screenshots=screenshots)

    def discard(self, plan: BrowserRunPlan) -> None:
        self._drop(_checkpoint_key_for_plan(plan))

    def clear(self) -> None:
        self._checkpoints.clear()
        self._screenshots.clear()
        self._total_bytes = 0

    def _drop(self, key: tuple[str, str, str]) -> None:
        self._checkpoints.pop(key, None)
        screenshots = self._screenshots.pop(key, {})
        self._total_bytes -= sum(len(item.data) for item in screenshots.values())
        if self._total_bytes < 0:
            self.clear()

    def _state_consistent(self) -> bool:
        if set(self._screenshots) - set(self._checkpoints):
            return False
        actual_bytes = 0
        for key, screenshots in self._screenshots.items():
            checkpoint = self._checkpoints[key]
            expected = {reference.artifact_id: reference for reference in checkpoint.artifacts}
            if set(screenshots) != set(expected):
                return False
            for artifact_id, screenshot in screenshots.items():
                if screenshot.reference != expected[artifact_id] or len(screenshot.data) > _MAX_RETAINED_PNG_BYTES:
                    return False
                actual_bytes += len(screenshot.data)
        return actual_bytes == self._total_bytes <= _MAX_TOTAL_RETAINED_BYTES


def _checkpoint_key(checkpoint: BrowserCheckpoint) -> tuple[str, str, str]:
    return (checkpoint.plan_id, checkpoint.scope.digest, checkpoint.plan_digest)


def _checkpoint_key_for_plan(plan: BrowserRunPlan) -> tuple[str, str, str]:
    return (plan.plan_id, plan.scope.digest, plan.digest)


class DiscordRemoteBrowserInteractiveAdapter:
    """One code-owned browser recipe with scope-bound media delivery."""

    def __init__(
        self,
        *,
        runner: BoundedBrowserRunner,
        store: MediaArtifactStore,
        store_current: Callable[[], MediaArtifactStore | None],
        renderer: DiscordAIResponseRenderer | None = None,
        checkpoint_store: BrowserInteractiveCheckpointStore | None = None,
        runtime_current: Callable[[], bool] | None = None,
        quarantine_runtime: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(runner, BoundedBrowserRunner):
            raise TypeError("runner must be a BoundedBrowserRunner")
        if not isinstance(store, MediaArtifactStore) or not callable(store_current):
            raise TypeError("interactive browser delivery requires the current MediaArtifactStore")
        if renderer is not None and not isinstance(renderer, DiscordAIResponseRenderer):
            raise TypeError("renderer must be a DiscordAIResponseRenderer")
        if checkpoint_store is not None and not isinstance(checkpoint_store, BrowserInteractiveCheckpointStore):
            raise TypeError("checkpoint_store must be BrowserInteractiveCheckpointStore or None")
        if runtime_current is not None and not callable(runtime_current):
            raise TypeError("runtime_current must be callable or None")
        if quarantine_runtime is not None and not callable(quarantine_runtime):
            raise TypeError("quarantine_runtime must be callable or None")
        self._runner = runner
        self._store = store
        self._store_current = store_current
        self._preparer = MediaArtifactDeliveryPreparer(store, store_current=store_current)
        self._renderer = renderer or DiscordAIResponseRenderer()
        self._checkpoint_store = checkpoint_store or BrowserInteractiveCheckpointStore()
        self._runtime_current = runtime_current or (lambda: True)
        self._quarantine_runtime = quarantine_runtime or (lambda: None)
        self._bot: Any | None = None
        self._closing = False
        self._quarantined = False
        self._lock = asyncio.Lock()
        self._inflight: dict[tuple[int, bytes], asyncio.Task[bool]] = {}
        self._cancellations: dict[tuple[int, bytes], BrowserCancellation] = {}
        self._terminal: OrderedDict[tuple[int, bytes], bool] = OrderedDict()

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def quarantined(self) -> bool:
        return self._quarantined

    def bind_bot(self, bot: Any) -> None:
        self._bot = bot

    def begin_close(self) -> None:
        self._closing = True
        self._checkpoint_store.clear()
        for cancellation in tuple(self._cancellations.values()):
            cancellation.cancel()

    async def close(self) -> None:
        self.begin_close()
        tasks = tuple(self._inflight.values())
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=_CLOSE_DRAIN_TIMEOUT_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                _cancelled, pending = await asyncio.wait(
                    pending,
                    timeout=_CLOSE_DRAIN_TIMEOUT_SECONDS,
                )
            if pending:
                raise RuntimeError("remote browser operations did not stop")
            for task in tasks:
                if not task.cancelled():
                    task.exception()
        async with self._lock:
            for key, task in tuple(self._inflight.items()):
                if task in tasks and task.done():
                    self._inflight.pop(key, None)
                    self._cancellations.pop(key, None)

    async def run_youtube_for_message(
        self,
        message: Any,
        *,
        query: str,
        authorization_current: AuthorizationCurrent,
        authorization_current_sync: AuthorizationCurrentSync,
    ) -> bool:
        if (
            self._closing
            or self._quarantined
            or not callable(authorization_current)
            or not callable(authorization_current_sync)
        ):
            return False
        try:
            request = BrowserInteractiveRequest.from_message(message, query)
        except (TypeError, ValueError):
            return False
        key = (request.message_id, request.fingerprint)
        async with self._lock:
            terminal = self._terminal.get(key)
            if terminal is not None:
                return terminal
            task = self._inflight.get(key)
            if task is None:
                cancellation = BrowserCancellation()
                task = asyncio.create_task(
                    self._run_once(
                        message,
                        request=request,
                        cancellation=cancellation,
                        authorization_current=authorization_current,
                        authorization_current_sync=authorization_current_sync,
                    )
                )
                self._inflight[key] = task
                self._cancellations[key] = cancellation
                task.add_done_callback(lambda completed: asyncio.create_task(self._remember(key, completed)))
        return await asyncio.shield(task)

    async def _remember(self, key: tuple[int, bytes], task: asyncio.Task[bool]) -> None:
        async with self._lock:
            if self._inflight.get(key) is not task:
                return
            self._inflight.pop(key, None)
            self._cancellations.pop(key, None)
            if task.cancelled():
                return
            try:
                completed = task.result()
            except Exception:
                completed = False
            if completed is True:
                self._terminal[key] = True
                self._terminal.move_to_end(key)
                while len(self._terminal) > _IDEMPOTENCY_LIMIT:
                    self._terminal.popitem(last=False)

    async def _run_once(
        self,
        message: Any,
        *,
        request: BrowserInteractiveRequest,
        cancellation: BrowserCancellation,
        authorization_current: AuthorizationCurrent,
        authorization_current_sync: AuthorizationCurrentSync,
    ) -> bool:
        progress_message: Any | None = None
        plan = youtube_playback_evidence_plan(
            plan_id=f"youtube-{request.message_id}",
            scope=request.browser_scope,
            query=request.query,
        )
        try:
            if not await self._authorized(authorization_current) or not self._sync_current(authorization_current_sync):
                self._checkpoint_store.discard(plan)
                return False
            if not await self._append_audit(message, request, event="browser_run.requested"):
                return False
            progress_message = await self._start_progress(message)
            if (
                progress_message is None
                or not await self._authorized(authorization_current)
                or not self._sync_current(authorization_current_sync)
            ):
                self._checkpoint_store.discard(plan)
                return False
            resume = await self._checkpoint_store.load(plan)
            if not await self._authorized(authorization_current) or not self._sync_current(authorization_current_sync):
                self._checkpoint_store.discard(plan)
                return False
            if resume is None:
                result = await self._runner.run(plan, cancellation=cancellation)
                retained_screenshots: tuple[BrowserScreenshotArtifact, ...] = ()
            else:
                result = await self._runner.run(
                    plan,
                    checkpoint=resume.checkpoint,
                    cancellation=cancellation,
                )
                retained_screenshots = resume.screenshots
            if not await self._authorized(authorization_current) or not self._sync_current(authorization_current_sync):
                self._checkpoint_store.discard(plan)
                return False
            screenshots = _validated_screenshots(retained_screenshots + result.screenshot_artifacts, request=request)
            outputs = await asyncio.to_thread(
                self._commit_screenshots,
                screenshots,
                request=request,
                plan_digest=plan.digest,
                authorization_current=authorization_current_sync,
            )
            if not await self._authorized(authorization_current) or not self._sync_current(authorization_current_sync):
                self._checkpoint_store.discard(plan)
                return False
            attachments = await asyncio.to_thread(
                self._preparer.prepare,
                outputs,
                scope=request.scope,
                authorization_current=authorization_current_sync,
            )
            if not await self._authorized(authorization_current) or not self._sync_current(authorization_current_sync):
                self._checkpoint_store.discard(plan)
                return False
            if not await self._append_audit(message, request, event="browser_run.completed"):
                return False
            if not await self._authorized(authorization_current) or not self._sync_current(authorization_current_sync):
                self._checkpoint_store.discard(plan)
                return False
            rendered = await self._renderer.reply(
                message,
                _SUCCESS_TEXT,
                model=_MODEL_LABEL,
                existing_message=progress_message,
                display_mode=DisplayMode.CARD,
                media_attachments=attachments,
                fresh_send_allowed=lambda: self._delivery_allowed(
                    authorization_current,
                    authorization_current_sync,
                ),
            )
            succeeded = rendered.primary_message is not None and len(rendered.attachment_filenames) == 2
            if succeeded:
                self._checkpoint_store.discard(plan)
            elif not await self._authorized(authorization_current) or not self._sync_current(
                authorization_current_sync
            ):
                self._checkpoint_store.discard(plan)
            return succeeded
        except asyncio.CancelledError:
            self._checkpoint_store.discard(plan)
            raise
        except BrowserRunCancelledError:
            self._checkpoint_store.discard(plan)
            if progress_message is not None:
                await self._replace_progress_with_failure(
                    message,
                    progress_message=progress_message,
                    authorization_current=authorization_current,
                    authorization_current_sync=authorization_current_sync,
                )
            return False
        except BrowserRunCleanupUnconfirmedError:
            self._quarantine()
            return False
        except BrowserRunContractError:
            self._checkpoint_store.discard(plan)
            if progress_message is not None:
                await self._replace_progress_with_failure(
                    message,
                    progress_message=progress_message,
                    authorization_current=authorization_current,
                    authorization_current_sync=authorization_current_sync,
                )
            return False
        except Exception:
            if progress_message is not None:
                await self._replace_progress_with_failure(
                    message,
                    progress_message=progress_message,
                    authorization_current=authorization_current,
                    authorization_current_sync=authorization_current_sync,
                )
            if not await self._authorized(authorization_current) or not self._sync_current(authorization_current_sync):
                self._checkpoint_store.discard(plan)
            return False

    def _commit_screenshots(
        self,
        screenshots: tuple[BrowserScreenshotArtifact, ...],
        *,
        request: BrowserInteractiveRequest,
        plan_digest: str,
        authorization_current: AuthorizationCurrentSync,
    ) -> tuple[PlanArtifactOutput, ...]:
        outputs: list[PlanArtifactOutput] = []
        for index, screenshot in enumerate(screenshots, start=1):
            if not self._sync_current(authorization_current):
                raise RuntimeError("browser artifact authorization changed")
            with Image.open(io.BytesIO(screenshot.data)) as image:
                if image.format != "PNG":
                    raise ValueError("browser screenshot is not PNG")
                image.load()
                detached = image.copy()
            recipe_digest = hashlib.sha256(
                (
                    f"yonerai.browser-run.youtube-evidence.v1\0{plan_digest}\0{screenshot.reference.global_step_index}"
                ).encode("ascii")
            ).hexdigest()
            ref = self._store.commit_image(
                detached,
                scope=request.scope,
                recipe_digest=recipe_digest,
                kind=ArtifactKind.IMAGE,
                commit_check=lambda: self._sync_current(authorization_current),
            )
            outputs.append(
                PlanArtifactOutput(
                    step_id=f"browser-shot-{index:02d}",
                    action_id=_ACTION_ID,
                    artifact=ref,
                )
            )
        return tuple(outputs)

    async def _append_audit(
        self,
        message: Any,
        request: BrowserInteractiveRequest,
        *,
        event: str,
    ) -> bool:
        append = getattr(getattr(self._bot, "database", None), "append_audit", None)
        if not callable(append):
            return False
        details: Mapping[str, object] = {
            "channel_id": request.channel_id,
            "message_id": request.message_id,
            "recipe": "youtube_playback_evidence_v1",
        }
        try:
            await asyncio.to_thread(
                append,
                event,
                actor_id=request.actor_id,
                guild_id=request.guild_id,
                plugin="browser_rendering",
                details=details,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return not self._closing

    async def _start_progress(self, message: Any) -> Any | None:
        reply = getattr(message, "reply", None)
        if not callable(reply):
            return None
        try:
            async with asyncio.timeout(_DISCORD_IO_TIMEOUT_SECONDS):
                return await reply(
                    content=_PROGRESS_TEXT,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _replace_progress_with_failure(
        self,
        message: Any,
        *,
        progress_message: Any,
        authorization_current: AuthorizationCurrent,
        authorization_current_sync: AuthorizationCurrentSync,
    ) -> None:
        try:
            await self._renderer.reply(
                message,
                _FAILURE_TEXT,
                model=_MODEL_LABEL,
                existing_message=progress_message,
                display_mode=DisplayMode.CARD,
                fresh_send_allowed=lambda: self._delivery_allowed(
                    authorization_current,
                    authorization_current_sync,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _authorized(self, check: AuthorizationCurrent) -> bool:
        if self._closing or self._quarantined:
            return False
        try:
            allowed = check()
            if inspect.isawaitable(allowed):
                allowed = await allowed
            return not self._closing and not self._quarantined and allowed is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    def _sync_current(self, check: AuthorizationCurrentSync) -> bool:
        if self._closing or self._quarantined:
            return False
        try:
            return self._runtime_current() is True and self._store_current() is self._store and check() is True
        except Exception:
            return False

    async def _delivery_allowed(
        self,
        authorization_current: AuthorizationCurrent,
        authorization_current_sync: AuthorizationCurrentSync,
    ) -> bool:
        return await self._authorized(authorization_current) and self._sync_current(authorization_current_sync)

    def _quarantine(self) -> None:
        self._quarantined = True
        self._checkpoint_store.clear()
        for cancellation in tuple(self._cancellations.values()):
            cancellation.cancel()
        try:
            self._quarantine_runtime()
        except Exception:
            pass


def _validated_screenshots(
    screenshots: object,
    *,
    request: BrowserInteractiveRequest,
) -> tuple[BrowserScreenshotArtifact, ...]:
    if not isinstance(screenshots, tuple) or len(screenshots) != 2:
        raise ValueError("browser screenshot evidence is unavailable")
    expected_steps = (4, 10)
    for screenshot, expected_step in zip(screenshots, expected_steps, strict=True):
        if not isinstance(screenshot, BrowserScreenshotArtifact):
            raise TypeError("browser screenshot evidence is unavailable")
        reference = screenshot.reference
        if (
            reference.scope != request.browser_scope
            or reference.global_step_index != expected_step
            or reference.media_type != "image/png"
            or reference.source_origin != "https://www.youtube.com"
            or not screenshot.data.startswith(_PNG_SIGNATURE)
        ):
            raise ValueError("browser screenshot evidence binding is invalid")
    return screenshots


__all__ = [
    "BrowserInteractiveCheckpointStore",
    "BrowserInteractiveRequest",
    "DiscordRemoteBrowserInteractiveAdapter",
]
