from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from yonerai_discord.execution_gateway.models import ArtifactReference, RunEvent
from yonerai_discord.modules.ai.orchestration import (
    PlanEvent,
    PlanEventType,
    PlanReceipt,
    PlanStatus,
    StepReceipt,
    StepStatus,
)
from yonerai_discord.modules.ai.task_progress import (
    DiscordAITaskProgressRenderer,
    ProgressEditPolicy,
    TaskStatusEmojis,
    build_ai_progress_plan,
    gateway_progress_detail,
)
from yonerai_discord.modules.ai.task_routing import classify_ai_task


class FakeProgressMessage:
    def __init__(self, message_id: int = 2, *, fail_edit: bool = False) -> None:
        self.id = message_id
        self.edits: list[dict[str, object]] = []
        self.edit_completed = asyncio.Event()
        self.deleted = False
        self.fail_edit = fail_edit

    async def edit(self, **kwargs: object) -> FakeProgressMessage:
        if self.fail_edit:
            raise RuntimeError("edit failed")
        self.edits.append(kwargs)
        self.edit_completed.set()
        return self

    async def delete(self) -> None:
        self.deleted = True


class HangingProgressMessage(FakeProgressMessage):
    def __init__(self) -> None:
        super().__init__()
        self.edit_started = asyncio.Event()

    async def edit(self, **kwargs: object) -> FakeProgressMessage:
        del kwargs
        self.edit_started.set()
        await asyncio.Event().wait()
        return self


class BlockingProgressMessage(FakeProgressMessage):
    def __init__(self) -> None:
        super().__init__()
        self.edit_calls = 0
        self.first_edit_started = asyncio.Event()
        self.release_first_edit = asyncio.Event()
        self.followup_edit_completed = asyncio.Event()

    async def edit(self, **kwargs: object) -> FakeProgressMessage:
        self.edit_calls += 1
        if self.edit_calls == 1:
            self.first_edit_started.set()
            await self.release_first_edit.wait()
        self.edits.append(kwargs)
        if self.edit_calls >= 2:
            self.followup_edit_completed.set()
        return self


class FakeSourceMessage:
    def __init__(self, *, fail_reply: bool = False, fail_edit: bool = False) -> None:
        self.replies: list[dict[str, object]] = []
        self.channel_sends: list[dict[str, object]] = []
        self.channel = SimpleNamespace(send=self._send)
        self.progress_message = FakeProgressMessage(fail_edit=fail_edit)
        self.fail_reply = fail_reply
        self.reference = object()

    async def reply(self, **kwargs: object) -> FakeProgressMessage:
        if self.fail_reply:
            raise RuntimeError("reply failed")
        self.replies.append(kwargs)
        return self.progress_message

    async def _send(self, **kwargs: object) -> FakeProgressMessage:
        self.channel_sends.append(kwargs)
        return self.progress_message

    def to_reference(self, *, fail_if_not_exists: bool):
        assert fail_if_not_exists is False
        return self.reference


def test_short_chat_does_not_create_task_board() -> None:
    assert (
        build_ai_progress_plan(
            route=classify_ai_task("おはよう"),
            instruction="おはよう",
            attachment_count=0,
            has_reference=False,
        )
        is None
    )


def test_complex_web_artifact_request_builds_bounded_semantic_tasks() -> None:
    plan = build_ai_progress_plan(
        route=classify_ai_task(
            "Web検索してHTMLサイトを作って",
            web_search=True,
            attachment_count=2,
        ),
        instruction="Web検索してHTMLサイトを作って",
        attachment_count=2,
        has_reference=True,
    )

    assert plan is not None
    assert plan.tasks == (
        "依頼を解析",
        "返信元と添付ファイルを確認",
        "要件を分解",
        "Webを検索して出典を確認",
        "Webサイトを作成",
        "成果物を確認",
        "結果を検証",
        "Discord向けに整形",
    )
    assert plan.tasks[plan.active_index] == "Webを検索して出典を確認"
    assert plan.tasks[plan.artifact_index] == "成果物を確認"


def test_complex_plan_varies_with_input_context_without_extra_ai_calls() -> None:
    instruction = "詳しく分析して、複数の観点から説明して"
    route = classify_ai_task(instruction)

    plain = build_ai_progress_plan(
        route=route,
        instruction=instruction,
        attachment_count=0,
        has_reference=False,
    )
    referenced = build_ai_progress_plan(
        route=route,
        instruction=instruction,
        attachment_count=0,
        has_reference=True,
    )

    assert plain is not None
    assert referenced is not None
    assert "要件を分解" in plain.tasks
    assert "結果を検証" in plain.tasks
    assert "返信元の内容を確認" not in plain.tasks
    assert "返信元の内容を確認" in referenced.tasks
    assert plain.tasks != referenced.tasks
    assert plain == build_ai_progress_plan(
        route=route,
        instruction=instruction,
        attachment_count=0,
        has_reference=False,
    )


def test_grounded_evidence_plan_shows_the_real_completed_input_stage() -> None:
    route = replace(
        classify_ai_task("内容を日本語で要約して"),
        show_progress=True,
        reason_codes=("standard_conversation", "grounded_tool_evidence"),
    )

    plan = build_ai_progress_plan(
        route=route,
        instruction="内容を日本語で要約して",
        attachment_count=0,
        has_reference=False,
    )

    assert plan is not None
    assert plan.tasks == (
        "依頼を解析",
        "取得済みの根拠を確認",
        "AIで回答を組み立てる",
        "Discord向けに整形",
    )
    assert plan.active_index == 2


def test_progress_plan_is_bounded_unique_and_never_generates_custom_emoji_tokens() -> None:
    instruction = "Web検索してHTMLサイトを作って"
    plan = build_ai_progress_plan(
        route=classify_ai_task(
            instruction,
            web_search=True,
            attachment_count=3,
        ),
        instruction=instruction,
        attachment_count=3,
        has_reference=True,
    )

    assert plan is not None
    assert 2 <= len(plan.tasks) <= 8
    assert len(plan.tasks) == len(set(plan.tasks))
    assert all(len(task) <= 80 for task in plan.tasks)
    assert all(":conp:" not in task and ":rode:" not in task for task in plan.tasks)


@pytest.mark.asyncio
async def test_bind_execution_steps_shows_twenty_real_steps_within_discord_embed_limit() -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("詳しく分析して"),
        instruction="詳しく分析して",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer().start(source, plan)
    assert session is not None

    steps = tuple((f"step-{index}", f"公開工程 {index}") for index in range(1, 21))
    assert await session.bind_execution_steps(steps) is True

    assert len(session.plan.tasks) == 23
    assert session.plan.tasks[0] == "依頼を解析"
    assert session.plan.tasks[1:21] == tuple(label for _, label in steps)
    assert session.plan.tasks[-2:] == ("結果を検証", "Discord向けに整形")
    assert len(session.summary()) <= 4_096
    embed = session._build_embed()
    total = len(embed.title) + len(embed.description) + len(embed.footer.text)
    assert len(embed.description) <= 4_096
    assert total <= 6_000
    await session.begin_terminal_success()


@pytest.mark.asyncio
async def test_plan_events_update_mapped_rows_without_exposing_ids_or_becoming_terminal() -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("詳しく分析して"),
        instruction="詳しく分析して",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer().start(source, plan)
    assert session is not None
    assert (
        await session.bind_execution_steps(
            (
                ("secret-step-1", "同じ工程"),
                ("secret-step-2", "同じ工程"),
                ("secret-step-3", "結果を検証"),
            )
        )
        is True
    )

    assert session.plan.tasks == (
        "依頼を解析",
        "同じ工程",
        "同じ工程 (2)",
        "結果を検証 (2)",
        "結果を検証",
        "Discord向けに整形",
    )
    common = ("secret-plan-digest", "secret-idempotency-key")
    assert await session.apply_plan_event(
        PlanEvent(
            PlanEventType.STEP_STARTED,
            *common,
            step_id="secret-step-2",
            action_id="secret-action-id",
        )
    )
    assert "🔄 **同じ工程 (2)**" in session.summary()
    assert await session.apply_plan_event(
        PlanEvent(
            PlanEventType.STEP_COMPLETED,
            *common,
            step_id="secret-step-2",
            action_id="secret-action-id",
        )
    )
    assert "✅ **同じ工程 (2)**" in session.summary()
    assert await session.apply_plan_event(
        PlanEvent(
            PlanEventType.STEP_FAILED,
            *common,
            step_id="secret-step-1",
            action_id="secret-action-id",
        )
    )
    assert session.terminal is False
    assert "❌ **同じ工程**" in session.summary()
    assert await session.apply_plan_event(PlanEvent(PlanEventType.PLAN_FAILED, *common))
    assert "🔄 **結果を検証**" in session.summary()
    assert session.terminal is False
    assert (
        await session.apply_plan_event(
            PlanEvent(
                PlanEventType.STEP_STARTED,
                *common,
                step_id="unknown-step",
                action_id="secret-action-id",
            )
        )
        is False
    )

    exposed = session.summary()
    assert all(
        secret not in exposed
        for secret in (
            "secret-step-1",
            "secret-step-2",
            "secret-step-3",
            "secret-plan-digest",
            "secret-idempotency-key",
            "secret-action-id",
        )
    )
    final_summary = await session.begin_terminal_success()
    assert "❌ **同じ工程**" in final_summary
    assert "▫️ **結果を検証 (2)**" in final_summary
    assert "✅ **結果を検証**" in final_summary
    assert await session.final_delivery_failed("最終回答の送信に失敗") is True
    delivery_failed_summary = session.summary()
    assert "❌ **同じ工程**" in delivery_failed_summary
    assert "▫️ **結果を検証 (2)**" in delivery_failed_summary
    assert "❌ **Discord向けに整形**" in delivery_failed_summary


@pytest.mark.asyncio
async def test_terminal_receipt_is_truth_when_failure_events_could_not_be_delivered() -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("詳しく分析して"),
        instruction="詳しく分析して",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer().start(source, plan)
    assert session is not None
    assert await session.bind_execution_steps(
        (
            ("step-1", "完了工程"),
            ("step-2", "失敗工程"),
            ("step-3", "未実行工程"),
        )
    )
    receipt = PlanReceipt(
        request_id="request-id",
        guild_id=1,
        channel_id=2,
        user_id=3,
        idempotency_key="idempotency-key",
        plan_digest="plan-digest",
        status=PlanStatus.FAILED,
        steps=(
            StepReceipt("step-1", "tools.dice", StepStatus.COMPLETED),
            StepReceipt("step-2", "tools.random", StepStatus.FAILED, failure_code="deadline"),
            StepReceipt("step-3", "tools.choose", StepStatus.NOT_RUN),
        ),
    )

    assert await session.apply_plan_receipt(receipt) is True
    summary = await session.begin_terminal_success()

    assert "✅ **完了工程**" in summary
    assert "❌ **失敗工程**" in summary
    assert "▫️ **未実行工程**" in summary


def test_plain_custom_emoji_aliases_fall_back_to_portable_unicode() -> None:
    emojis = TaskStatusEmojis(
        processing=":conp:",
        done=":done:",
        pending=":rode:",
        failed=":fail:",
    )

    assert emojis == TaskStatusEmojis()
    assert all(alias not in repr(emojis) for alias in (":conp:", ":done:", ":rode:", ":fail:"))


@pytest.mark.asyncio
async def test_planner_progress_updates_never_wait_for_hanging_discord_edit() -> None:
    source = FakeSourceMessage()
    source.progress_message = HangingProgressMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("詳しく分析して"),
        instruction="詳しく分析して",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer().start(source, plan)
    assert session is not None
    session._last_edit_at = 0.0  # noqa: SLF001 - force an immediate background edit

    assert (
        await asyncio.wait_for(
            session.bind_execution_steps((("step-1", "公開工程"),)),
            timeout=0.1,
        )
        is True
    )
    await asyncio.wait_for(source.progress_message.edit_started.wait(), timeout=0.1)
    assert (
        await asyncio.wait_for(
            session.apply_plan_event(
                PlanEvent(
                    PlanEventType.STEP_STARTED,
                    "plan-digest",
                    "idempotency-key",
                    step_id="step-1",
                    action_id="internal-action",
                )
            ),
            timeout=0.1,
        )
        is True
    )
    await session.begin_terminal_success()
    assert session._pending_edit_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_update_during_background_edit_is_redrawn_after_inflight_edit() -> None:
    source = FakeSourceMessage()
    source.progress_message = BlockingProgressMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("詳しく分析して"),
        instruction="詳しく分析して",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer(edit_policy=ProgressEditPolicy(min_edit_interval_seconds=0.25)).start(
        source, plan
    )
    assert session is not None
    session._last_edit_at = 0.0  # noqa: SLF001 - force the first background edit
    assert await session.bind_execution_steps((("step-1", "公開工程"),)) is True
    await asyncio.wait_for(source.progress_message.first_edit_started.wait(), timeout=0.1)

    common = ("plan-digest", "idempotency-key")
    assert await session.apply_plan_event(
        PlanEvent(
            PlanEventType.STEP_COMPLETED,
            *common,
            step_id="step-1",
            action_id="internal-action",
        )
    )
    source.progress_message.release_first_edit.set()
    await asyncio.wait_for(source.progress_message.followup_edit_completed.wait(), timeout=0.5)

    assert source.progress_message.edit_calls == 2
    assert "✅ **公開工程**" in source.progress_message.edits[-1]["embed"].description
    await session.begin_terminal_success()


@pytest.mark.asyncio
async def test_bind_execution_steps_rejects_duplicate_step_ids() -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("詳しく分析して"),
        instruction="詳しく分析して",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer().start(source, plan)
    assert session is not None

    with pytest.raises(ValueError, match="step_id values must be unique"):
        await session.bind_execution_steps(
            (
                ("duplicate", "工程1"),
                ("duplicate", "工程2"),
            )
        )


@pytest.mark.parametrize("kind", ["status", "action_required", "tool_result", "artifact"])
def test_gateway_progress_detail_uses_only_safe_task_label(kind: str) -> None:
    event = RunEvent(
        kind=kind,
        text="secret-event-text",
        artifact=ArtifactReference(
            artifact_id="private",
            kind="file",
            name="secret-artifact-name",
            uri="https://private.example/artifact?token=secret-uri",
        ),
        payload={
            "tool": "secret-tool-name",
            "args": "secret-tool-args",
            "output": "secret-tool-output",
            "reasoning": "secret-reasoning",
        },
        extensions={"private": "secret-extension"},
        run_id="secret-run-id",
    )

    assert gateway_progress_detail(event, task="コードを作成") == "「コードを作成」を進めています。"


@pytest.mark.parametrize(
    ("instruction", "expected_task"),
    [
        ("Webサイトを作って", "Webサイトを作成"),
        ("Web検索して調べて", "Webを検索して出典を確認"),
        ("コードを書いて", "コードを作成"),
        ("画像を生成して", "画像を生成"),
        ("動画を生成して", "動画を生成"),
        ("音楽を生成して", "音楽を生成"),
        ("詳しく分析して", "AIで回答を組み立てる"),
    ],
)
def test_progress_plan_uses_safe_intent_based_tasks(instruction: str, expected_task: str) -> None:
    route = classify_ai_task(instruction, web_search="Web検索" in instruction)
    plan = build_ai_progress_plan(
        route=route,
        instruction=instruction,
        attachment_count=0,
        has_reference=False,
    )

    assert plan is not None
    assert expected_task in plan.tasks


@pytest.mark.parametrize("kind", ["text_delta", "final", "error", "tool_start", "future.progress.v2"])
def test_gateway_progress_detail_ignores_non_public_event_kinds(kind: str) -> None:
    assert (
        gateway_progress_detail(
            RunEvent(
                kind=kind,
                text="secret-event-text",
                payload={"reasoning": "secret-reasoning"},
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_progress_card_uses_portable_unicode_status_and_blocks_zombie_updates() -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("コードを書いて"),
        instruction="コードを書いて",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None

    session = await DiscordAITaskProgressRenderer().start(source, plan)
    assert session is not None
    initial = source.replies[0]
    assert initial["mention_author"] is False
    assert "🔄" in initial["embed"].description
    assert ":rode:" not in initial["embed"].description

    summary = await session.begin_terminal_success()
    assert "✅" in summary
    assert ":conp:" not in summary
    before = len(source.progress_message.edits)

    assert await session.set_running(0, detail="遅れて届いた更新") is False
    assert len(source.progress_message.edits) == before


@pytest.mark.asyncio
async def test_gateway_progress_event_exposes_only_safe_task_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("コードを書いて"),
        instruction="コードを書いて",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer(edit_policy=ProgressEditPolicy(min_edit_interval_seconds=0.25)).start(
        source, plan
    )
    assert session is not None
    await asyncio.sleep(0.26)
    secrets = (
        "secret-event-text",
        "secret-tool-name",
        "secret-tool-args",
        "secret-tool-output",
        "secret-reasoning",
        "secret-artifact-name",
        "https://private.example/artifact?token=secret-uri",
        "secret-run-id",
    )
    event = RunEvent(
        kind="artifact",
        text=secrets[0],
        artifact=ArtifactReference(
            artifact_id="private",
            kind="file",
            name=secrets[5],
            uri=secrets[6],
        ),
        payload={
            "tool": secrets[1],
            "args": secrets[2],
            "output": secrets[3],
            "reasoning": secrets[4],
        },
        extensions={"private": secrets[3]},
        run_id=secrets[7],
    )

    assert await session.apply_gateway_event(event) is True
    await asyncio.wait_for(source.progress_message.edit_completed.wait(), timeout=0.5)

    embed = source.progress_message.edits[-1]["embed"]
    assert "「成果物を確認」を進めています。" in embed.description
    exposed = embed.description + caplog.text
    assert all(secret not in exposed for secret in secrets)
    await session.begin_terminal_success()


@pytest.mark.asyncio
async def test_ignored_gateway_events_never_edit_progress_message() -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("コードを書いて"),
        instruction="コードを書いて",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer().start(source, plan)
    assert session is not None

    for kind in ("text_delta", "final", "error", "tool_start", "future.progress.v2"):
        assert (
            await session.apply_gateway_event(
                RunEvent(
                    kind=kind,
                    text="secret-event-text",
                    payload={"reasoning": "secret-reasoning"},
                )
            )
            is False
        )

    assert source.progress_message.edits == []


@pytest.mark.asyncio
async def test_progress_failure_is_terminal_and_contains_no_reasoning() -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("詳しく分析して"),
        instruction="詳しく分析して",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer(
        emojis=TaskStatusEmojis(processing="🔄", done="✅", pending="▫️", failed="❌")
    ).start(source, plan)
    assert session is not None

    assert await session.fail("AIを利用できませんでした。") is True
    failed_embed = source.progress_message.edits[-1]["embed"]
    assert "❌" in failed_embed.description
    assert "AIを利用できませんでした。" in failed_embed.description
    assert "思考内容" in failed_embed.footer.text
    assert await session.fail("二重終端") is False


@pytest.mark.asyncio
async def test_progress_fallback_keeps_message_reference() -> None:
    source = FakeSourceMessage(fail_reply=True)
    plan = build_ai_progress_plan(
        route=classify_ai_task("Web検索して", web_search=True),
        instruction="Web検索して",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None

    session = await DiscordAITaskProgressRenderer().start(source, plan)

    assert session is not None
    assert source.channel_sends[0]["reference"] is source.reference


@pytest.mark.asyncio
async def test_progress_updates_are_coalesced_and_terminal_cancels_pending_edit() -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("コードを書いて"),
        instruction="コードを書いて",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    renderer = DiscordAITaskProgressRenderer(edit_policy=ProgressEditPolicy(min_edit_interval_seconds=10.0))
    session = await renderer.start(source, plan)
    assert session is not None

    for index in range(100):
        assert await session.set_running(plan.active_index, detail=f"更新 {index}") is True
    assert len(source.progress_message.edits) == 0

    await session.begin_terminal_success()
    await asyncio.sleep(0)
    assert session.terminal is True
    assert len(source.progress_message.edits) == 0


@pytest.mark.asyncio
async def test_gateway_progress_burst_coalesces_and_late_events_are_rejected() -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("コードを書いて"),
        instruction="コードを書いて",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer(edit_policy=ProgressEditPolicy(min_edit_interval_seconds=10.0)).start(
        source, plan
    )
    assert session is not None

    for _ in range(100):
        assert await session.apply_gateway_event(RunEvent(kind="status")) is True
    assert len(source.progress_message.edits) == 0

    await session.begin_terminal_success()
    assert await session.apply_gateway_event(RunEvent(kind="artifact")) is False
    assert await session.apply_gateway_event(RunEvent(kind="future.progress.v2")) is False
    await asyncio.sleep(0)
    assert len(source.progress_message.edits) == 0


@pytest.mark.asyncio
async def test_failed_terminal_edit_deletes_running_card() -> None:
    source = FakeSourceMessage(fail_edit=True)
    plan = build_ai_progress_plan(
        route=classify_ai_task("詳しく分析して"),
        instruction="詳しく分析して",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer().start(source, plan)
    assert session is not None

    assert await session.fail("停止しました") is False
    assert source.progress_message.deleted is True


@pytest.mark.asyncio
async def test_final_delivery_failure_never_claims_a_followup_message_exists() -> None:
    source = FakeSourceMessage()
    plan = build_ai_progress_plan(
        route=classify_ai_task("コードを書いて"),
        instruction="コードを書いて",
        attachment_count=0,
        has_reference=False,
    )
    assert plan is not None
    session = await DiscordAITaskProgressRenderer().start(source, plan)
    assert session is not None

    await session.begin_terminal_success()
    assert await session.final_delivery_failed("最終回答の送信に失敗") is True

    failed_embed = source.progress_message.edits[-1]["embed"]
    assert "❌" in failed_embed.description
    assert "次のメッセージに表示" not in failed_embed.footer.text
