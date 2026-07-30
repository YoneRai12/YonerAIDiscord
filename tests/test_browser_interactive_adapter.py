from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from yonerai_discord.browser_sandbox.policy import BrowserSandboxPolicy, StaticDnsResolver
from yonerai_discord.modules.ai.discord_renderer import DiscordAIResponseRenderer
from yonerai_discord.modules.browser_rendering.interactive_adapter import (
    DiscordRemoteBrowserInteractiveAdapter,
)
from yonerai_discord.modules.media_pipeline import MediaArtifactStore
from yonerai_discord.modules.web_runtime.cloudflare_browser_run import youtube_playback_evidence_plan
from yonerai_discord.modules.web_runtime.browser import (
    AllowedOriginPolicy,
    BoundedBrowserRunner,
    BrowserArtifactRef,
    BrowserCheckpoint,
    BrowserRunCleanupUnconfirmedError,
    BrowserRunScope,
    BrowserScreenshotArtifact,
)


class _Database:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, object]]] = []

    def append_audit(self, event: str, **kwargs: object) -> None:
        self.rows.append((event, dict(kwargs)))


class _Progress:
    def __init__(self) -> None:
        self.id = 991


class _Message:
    def __init__(self, *, message_id: int = 40) -> None:
        self.id = message_id
        self.guild = SimpleNamespace(id=10)
        self.channel = SimpleNamespace(id=20)
        self.author = SimpleNamespace(id=30)
        self.replies: list[dict[str, object]] = []
        self.progress = _Progress()

    async def reply(self, **kwargs: object) -> _Progress:
        self.replies.append(dict(kwargs))
        return self.progress


class _Renderer(DiscordAIResponseRenderer):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def reply(self, source_message: Any, content: str, **kwargs: object) -> Any:
        self.calls.append(
            {
                "source_message": source_message,
                "content": content,
                **kwargs,
            }
        )
        if self.fail:
            raise RuntimeError("fixed renderer failure")
        attachments = kwargs.get("media_attachments", ())
        return SimpleNamespace(
            primary_message=kwargs.get("existing_message"),
            attachment_filenames=tuple(item.filename for item in attachments),
        )


class _RunnerResult:
    def __init__(self, screenshots: tuple[BrowserScreenshotArtifact, ...]) -> None:
        self.screenshot_artifacts = screenshots


class _RunnerBehavior:
    def __init__(
        self,
        *,
        wrong_scope: bool = False,
        fail: bool = False,
        after_run: Any | None = None,
    ) -> None:
        self.wrong_scope = wrong_scope
        self.fail = fail
        self.after_run = after_run
        self.calls = 0

    async def __call__(self, plan: Any, *, cancellation: Any) -> _RunnerResult:
        self.calls += 1
        assert cancellation is not None
        if self.fail:
            raise RuntimeError("fixed runner failure")
        scope = plan.scope
        if self.wrong_scope:
            scope = BrowserRunScope(
                request_id="other-request",
                guild_id=scope.guild_id,
                channel_id=scope.channel_id,
                user_id=scope.user_id,
            )
        screenshots = tuple(_screenshot(scope, step, color) for step, color in ((4, "red"), (10, "blue")))
        if self.after_run is not None:
            self.after_run()
        return _RunnerResult(screenshots)


def _png(color: str) -> bytes:
    with Image.new("RGB", (3, 2), color=color) as image:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def _screenshot(scope: BrowserRunScope, step: int, color: str) -> BrowserScreenshotArtifact:
    data = _png(color)
    reference = BrowserArtifactRef(
        artifact_id=f"shot-{step}",
        scope=scope,
        global_step_index=step,
        media_type="image/png",
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        source_origin="https://www.youtube.com",
    )
    return BrowserScreenshotArtifact(reference=reference, data=data)


def _runner() -> BoundedBrowserRunner:
    return BoundedBrowserRunner(
        policy=BrowserSandboxPolicy(
            resolver=StaticDnsResolver({"www.youtube.com": ("142.250.72.206",)}),
            allowed_domains=("www.youtube.com",),
        ),
        origin_policy=AllowedOriginPolicy(("https://www.youtube.com",)),
        enabled=False,
    )


def _store(tmp_path: Path, name: str = "media") -> MediaArtifactStore:
    root = tmp_path / name
    root.mkdir()
    return MediaArtifactStore(root)


def _adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    behavior: _RunnerBehavior | None = None,
    renderer: _Renderer | None = None,
    store_current: Any | None = None,
    checkpoint_store: Any | None = None,
    runtime_current: Any | None = None,
    quarantine_runtime: Any | None = None,
) -> tuple[DiscordRemoteBrowserInteractiveAdapter, _RunnerBehavior, _Renderer, MediaArtifactStore, _Database]:
    runner = _runner()
    selected_behavior = behavior or _RunnerBehavior()
    monkeypatch.setattr(runner, "run", selected_behavior)
    store = _store(tmp_path)
    selected_renderer = renderer or _Renderer()
    current = store_current or (lambda: store)
    adapter = DiscordRemoteBrowserInteractiveAdapter(
        runner=runner,
        store=store,
        store_current=current,
        renderer=selected_renderer,
        checkpoint_store=checkpoint_store,
        runtime_current=runtime_current,
        quarantine_runtime=quarantine_runtime,
    )
    database = _Database()
    adapter.bind_bot(SimpleNamespace(database=database))
    return adapter, selected_behavior, selected_renderer, store, database


@pytest.mark.asyncio
async def test_success_delivers_two_canonical_pngs_by_editing_the_same_progress_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, runner, renderer, store, database = _adapter(tmp_path, monkeypatch)
    message = _Message()

    assert await adapter.run_youtube_for_message(
        message,
        query="safe search",
        authorization_current=lambda: True,
        authorization_current_sync=lambda: True,
    )

    assert runner.calls == 1
    assert len(message.replies) == 1
    assert len(renderer.calls) == 1
    rendered = renderer.calls[0]
    assert rendered["existing_message"] is message.progress
    attachments = rendered["media_attachments"]
    assert [item.filename for item in attachments] == ["media-01.png", "media-02.png"]
    assert all(item.data.startswith(b"\x89PNG\r\n\x1a\n") for item in attachments)
    assert [event for event, _details in database.rows] == [
        "browser_run.requested",
        "browser_run.completed",
    ]
    store.close()


@pytest.mark.asyncio
async def test_same_message_and_query_are_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, runner, renderer, store, database = _adapter(tmp_path, monkeypatch)
    message = _Message()

    first = await adapter.run_youtube_for_message(
        message,
        query="same query",
        authorization_current=lambda: True,
        authorization_current_sync=lambda: True,
    )
    second = await adapter.run_youtube_for_message(
        message,
        query="same query",
        authorization_current=lambda: True,
        authorization_current_sync=lambda: True,
    )

    assert first is True and second is True
    assert runner.calls == 1
    assert len(renderer.calls) == 1
    assert len(database.rows) == 2
    store.close()


@pytest.mark.asyncio
async def test_retry_resumes_after_the_first_segment_without_rerunning_its_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yonerai_discord.modules.browser_rendering.interactive_adapter import (
        BrowserInteractiveCheckpointStore,
    )

    checkpoint_store = BrowserInteractiveCheckpointStore()

    class _SecondSegmentTransientFailure:
        def __init__(self) -> None:
            self.checkpoints: list[int | None] = []

        async def __call__(self, plan: Any, *, cancellation: Any, checkpoint: Any = None) -> _RunnerResult:
            assert cancellation is not None
            self.checkpoints.append(None if checkpoint is None else checkpoint.next_segment_index)
            if checkpoint is None:
                first = _screenshot(plan.scope, 4, "red")
                saved = BrowserCheckpoint(
                    plan_id=plan.plan_id,
                    scope=plan.scope,
                    plan_digest=plan.digest,
                    policy_digest="0" * 64,
                    origin_policy_digest="1" * 64,
                    next_segment_index=1,
                    artifacts=(first.reference,),
                )
                await checkpoint_store.save(saved)
                await checkpoint_store.save_artifacts(saved, (first,))
                raise RuntimeError("second segment transient failure")
            assert checkpoint.next_segment_index == 1
            return _RunnerResult((_screenshot(plan.scope, 10, "blue"),))

    behavior = _SecondSegmentTransientFailure()
    adapter, _runner, renderer, store, _database = _adapter(
        tmp_path,
        monkeypatch,
        behavior=behavior,
        checkpoint_store=checkpoint_store,
    )
    message = _Message()

    assert (
        await adapter.run_youtube_for_message(
            message,
            query="resumable fixed recipe",
            authorization_current=lambda: True,
            authorization_current_sync=lambda: True,
        )
        is False
    )
    assert (
        await adapter.run_youtube_for_message(
            message,
            query="resumable fixed recipe",
            authorization_current=lambda: True,
            authorization_current_sync=lambda: True,
        )
        is True
    )

    assert behavior.checkpoints == [None, 1]
    assert [item.filename for item in renderer.calls[-1]["media_attachments"]] == ["media-01.png", "media-02.png"]
    assert (
        await checkpoint_store.load(
            youtube_playback_evidence_plan(
                plan_id="youtube-40",
                scope=BrowserRunScope(request_id="discord-40", guild_id=10, channel_id=20, user_id=30),
                query="resumable fixed recipe",
            )
        )
        is None
    )
    store.close()


@pytest.mark.asyncio
async def test_cleanup_unconfirmed_quarantines_runtime_and_rejects_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CleanupFailure:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, _plan: Any, *, cancellation: Any) -> Any:
            self.calls += 1
            raise BrowserRunCleanupUnconfirmedError("cleanup unconfirmed")

    behavior = _CleanupFailure()
    runtime = {"quarantined": False}
    adapter, _runner, renderer, store, _database = _adapter(
        tmp_path,
        monkeypatch,
        behavior=behavior,
        quarantine_runtime=lambda: runtime.update(quarantined=True),
    )
    message = _Message()

    assert not await adapter.run_youtube_for_message(
        message,
        query="cleanup quarantine",
        authorization_current=lambda: True,
        authorization_current_sync=lambda: True,
    )
    assert adapter.quarantined is True
    assert runtime["quarantined"] is True
    assert not await adapter.run_youtube_for_message(
        message,
        query="cleanup quarantine",
        authorization_current=lambda: True,
        authorization_current_sync=lambda: True,
    )
    assert behavior.calls == 1
    assert renderer.calls == []
    store.close()


@pytest.mark.asyncio
async def test_checkpoint_store_enforces_png_and_global_byte_budgets_with_lru(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yonerai_discord.modules.browser_rendering import interactive_adapter as adapter_module
    from yonerai_discord.modules.browser_rendering.interactive_adapter import (
        BrowserInteractiveCheckpointStore,
    )

    data = _png("red")
    monkeypatch.setattr(adapter_module, "_MAX_RETAINED_PNG_BYTES", len(data) + 8)
    monkeypatch.setattr(adapter_module, "_MAX_TOTAL_RETAINED_BYTES", len(data) + 8)
    store = BrowserInteractiveCheckpointStore()

    def plan_and_checkpoint(plan_id: str, request_id: str, color: str) -> tuple[Any, BrowserCheckpoint, Any]:
        scope = BrowserRunScope(request_id=request_id, guild_id=10, channel_id=20, user_id=30)
        plan = youtube_playback_evidence_plan(plan_id=plan_id, scope=scope, query="bounded")
        screenshot = _screenshot(scope, 4, color)
        checkpoint = BrowserCheckpoint(
            plan_id=plan.plan_id,
            scope=scope,
            plan_digest=plan.digest,
            policy_digest="0" * 64,
            origin_policy_digest="1" * 64,
            next_segment_index=1,
            artifacts=(screenshot.reference,),
        )
        return plan, checkpoint, screenshot

    first_plan, first_checkpoint, first_screenshot = plan_and_checkpoint("first", "request-first", "red")
    second_plan, second_checkpoint, second_screenshot = plan_and_checkpoint("second", "request-second", "blue")
    await store.save(first_checkpoint)
    await store.save_artifacts(first_checkpoint, (first_screenshot,))
    await store.save(second_checkpoint)
    await store.save_artifacts(second_checkpoint, (second_screenshot,))

    assert await store.load(first_plan) is None
    assert await store.load(second_plan) is not None
    assert store._total_bytes == len(second_screenshot.data)

    oversized_data = b"\x89PNG\r\n\x1a\n" + b"x" * (len(data) + 8)
    oversized_reference = BrowserArtifactRef(
        artifact_id="oversized-shot",
        scope=second_plan.scope,
        global_step_index=4,
        media_type="image/png",
        byte_length=len(oversized_data),
        sha256=hashlib.sha256(oversized_data).hexdigest(),
        source_origin="https://www.youtube.com",
    )
    oversized = BrowserScreenshotArtifact(reference=oversized_reference, data=oversized_data)
    oversized_checkpoint = BrowserCheckpoint(
        plan_id=second_plan.plan_id,
        scope=second_plan.scope,
        plan_digest=second_plan.digest,
        policy_digest="0" * 64,
        origin_policy_digest="1" * 64,
        next_segment_index=1,
        artifacts=(oversized_reference,),
    )
    await store.save(oversized_checkpoint)
    with pytest.raises(ValueError, match="PNG contract"):
        await store.save_artifacts(oversized_checkpoint, (oversized,))
    assert await store.load(second_plan) is None
    assert store._total_bytes == 0


@pytest.mark.asyncio
async def test_checkpoint_load_cleans_cross_scope_and_inconsistent_state() -> None:
    from yonerai_discord.modules.browser_rendering.interactive_adapter import (
        BrowserInteractiveCheckpointStore,
    )

    store = BrowserInteractiveCheckpointStore()
    first_scope = BrowserRunScope(request_id="scope-one", guild_id=10, channel_id=20, user_id=30)
    first_plan = youtube_playback_evidence_plan(plan_id="same-plan", scope=first_scope, query="bounded")
    first_screenshot = _screenshot(first_scope, 4, "red")
    checkpoint = BrowserCheckpoint(
        plan_id=first_plan.plan_id,
        scope=first_scope,
        plan_digest=first_plan.digest,
        policy_digest="0" * 64,
        origin_policy_digest="1" * 64,
        next_segment_index=1,
        artifacts=(first_screenshot.reference,),
    )
    await store.save(checkpoint)
    await store.save_artifacts(checkpoint, (first_screenshot,))

    other_scope = BrowserRunScope(request_id="scope-two", guild_id=10, channel_id=20, user_id=31)
    other_plan = youtube_playback_evidence_plan(plan_id="same-plan", scope=other_scope, query="bounded")
    assert await store.load(other_plan) is None
    assert store._total_bytes == 0

    await store.save(checkpoint)
    await store.save_artifacts(checkpoint, (first_screenshot,))
    store._total_bytes += 1
    assert await store.load(first_plan) is None
    assert store._total_bytes == 0


@pytest.mark.asyncio
async def test_authorization_revoked_after_runner_prevents_artifact_and_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"allowed": True}
    behavior = _RunnerBehavior(after_run=lambda: state.update(allowed=False))
    adapter, _runner_behavior, renderer, store, database = _adapter(
        tmp_path,
        monkeypatch,
        behavior=behavior,
    )

    assert (
        await adapter.run_youtube_for_message(
            _Message(),
            query="revoke after run",
            authorization_current=lambda: state["allowed"],
            authorization_current_sync=lambda: state["allowed"],
        )
        is False
    )

    assert renderer.calls == []
    assert [event for event, _details in database.rows] == ["browser_run.requested"]
    store.close()


@pytest.mark.asyncio
async def test_store_replacement_or_cross_scope_screenshot_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = _store(tmp_path, "replacement")
    adapter, runner, renderer, store, _database = _adapter(
        tmp_path,
        monkeypatch,
        store_current=lambda: replacement,
    )

    assert (
        await adapter.run_youtube_for_message(
            _Message(),
            query="store replacement",
            authorization_current=lambda: True,
            authorization_current_sync=lambda: True,
        )
        is False
    )
    assert runner.calls == 0
    assert renderer.calls == []

    cross_scope_root = tmp_path / "cross-scope"
    cross_scope_root.mkdir()
    adapter2, runner2, renderer2, store2, _database2 = _adapter(
        cross_scope_root,
        monkeypatch,
        behavior=_RunnerBehavior(wrong_scope=True),
    )
    assert (
        await adapter2.run_youtube_for_message(
            _Message(message_id=41),
            query="wrong scope",
            authorization_current=lambda: True,
            authorization_current_sync=lambda: True,
        )
        is False
    )
    assert runner2.calls == 1
    assert len(renderer2.calls) == 1
    assert renderer2.calls[0].get("media_attachments", ()) == ()
    store.close()
    store2.close()
    replacement.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["runner", "renderer"])
async def test_runner_or_delivery_failure_is_terminal_without_sensitive_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    query = "private-query-never-display"
    behavior = _RunnerBehavior(fail=failure == "runner")
    renderer = _Renderer(fail=failure == "renderer")
    adapter, runner, selected_renderer, store, database = _adapter(
        tmp_path,
        monkeypatch,
        behavior=behavior,
        renderer=renderer,
    )

    assert (
        await adapter.run_youtube_for_message(
            _Message(),
            query=query,
            authorization_current=lambda: True,
            authorization_current_sync=lambda: True,
        )
        is False
    )

    exposed = repr(selected_renderer.calls) + repr(database.rows)
    assert query not in exposed
    assert "shot-4" not in exposed
    assert str(tmp_path) not in exposed
    assert runner.calls == 1
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("runner_failure", [False, True])
async def test_success_and_failure_delivery_recheck_runtime_and_close_before_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_failure: bool,
) -> None:
    class _GatedRenderer(_Renderer):
        def __init__(self) -> None:
            super().__init__()
            self.adapter: DiscordRemoteBrowserInteractiveAdapter | None = None
            self.sent = 0

        async def reply(self, source_message: Any, content: str, **kwargs: object) -> Any:
            assert self.adapter is not None
            self.adapter.begin_close()
            allowed = kwargs["fresh_send_allowed"]()
            if asyncio.iscoroutine(allowed):
                allowed = await allowed
            if allowed is True:
                self.sent += 1
            return SimpleNamespace(primary_message=None, attachment_filenames=())

    renderer = _GatedRenderer()
    adapter, _runner_behavior, _selected, store, _database = _adapter(
        tmp_path,
        monkeypatch,
        behavior=_RunnerBehavior(fail=runner_failure),
        renderer=renderer,
    )
    renderer.adapter = adapter

    assert not await adapter.run_youtube_for_message(
        _Message(),
        query="delivery runtime recheck",
        authorization_current=lambda: True,
        authorization_current_sync=lambda: True,
    )
    assert renderer.sent == 0
    assert adapter.closing is True
    store.close()


@pytest.mark.asyncio
async def test_closing_rejects_without_runner_audit_or_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, runner, renderer, store, database = _adapter(tmp_path, monkeypatch)
    adapter.begin_close()

    assert (
        await adapter.run_youtube_for_message(
            _Message(),
            query="must not run",
            authorization_current=lambda: True,
            authorization_current_sync=lambda: True,
        )
        is False
    )
    assert runner.calls == 0
    assert renderer.calls == []
    assert database.rows == []
    store.close()


@pytest.mark.asyncio
async def test_close_cancels_and_drains_a_hung_run_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HungBehavior:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def __call__(self, _plan: Any, *, cancellation: Any) -> Any:
            assert cancellation is not None
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    behavior = _HungBehavior()
    adapter, _runner_behavior, renderer, store, _database = _adapter(
        tmp_path,
        monkeypatch,
        behavior=behavior,
    )
    monkeypatch.setattr(
        "yonerai_discord.modules.browser_rendering.interactive_adapter._CLOSE_DRAIN_TIMEOUT_SECONDS",
        0.01,
    )
    execution = asyncio.create_task(
        adapter.run_youtube_for_message(
            _Message(),
            query="bounded close",
            authorization_current=lambda: True,
            authorization_current_sync=lambda: True,
        )
    )
    await behavior.started.wait()

    await adapter.close()
    await asyncio.gather(execution, return_exceptions=True)

    assert behavior.cancelled.is_set()
    assert adapter._inflight == {}
    assert renderer.calls == []
    store.close()
