"""難しいAI依頼だけに表示する、Discordタスク進捗カード。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable

import discord

from yonerai_discord.ai_control import TaskComplexity
from yonerai_discord.discord_payload_budget import (
    DiscordEmbedText,
    fit_embed_description,
    validate_discord_payload_budget,
)
from yonerai_discord.execution_gateway.models import RunEvent

from .orchestration import PlanEvent, PlanEventType, PlanReceipt, PlanStatus, StepStatus
from .task_routing import AIIntent, AITaskRoute


logger = logging.getLogger(__name__)

_MAX_STATIC_TASKS = 8
_MAX_TASKS = 24
_MAX_EXECUTION_STEPS = _MAX_TASKS - 3
_MAX_LABEL_CHARS = 80
_MAX_DETAIL_CHARS = 120
_DISCORD_IO_TIMEOUT_SECONDS = 15.0
_PUBLIC_GATEWAY_PROGRESS_KINDS = frozenset({"status", "action_required", "tool_result", "artifact"})
_EXECUTION_WRAPPER_TASKS = ("依頼を解析", "結果を検証", "Discord向けに整形")
_PLAIN_CUSTOM_EMOJI_ALIAS = re.compile(r"^:[A-Za-z0-9_~-]{1,64}:$")
_DEFAULT_STATUS_EMOJIS = {
    "processing": "🔄",
    "done": "✅",
    "pending": "▫️",
    "failed": "❌",
}


def gateway_progress_detail(event: RunEvent, *, task: str | None = None) -> str | None:
    """安全な工程名だけをGateway進捗として公開する。"""

    if not isinstance(event, RunEvent):
        raise TypeError("event must be a RunEvent")
    if event.kind not in _PUBLIC_GATEWAY_PROGRESS_KINDS:
        return None
    if task is None:
        return "現在の工程を進めています。"
    return f"「{_bounded_line(task, maximum=_MAX_LABEL_CHARS)}」を進めています。"


class TaskProgressState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TaskStatusEmojis:
    """全guildで表示できるUnicodeを既定にしつつ、設定で差し替え可能にする。"""

    processing: str = "🔄"
    done: str = "✅"
    pending: str = "▫️"
    failed: str = "❌"

    def __post_init__(self) -> None:
        for name in ("processing", "done", "pending", "failed"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or "\n" in value or len(value) > 128:
                raise ValueError(f"{name} emoji must be a bounded single-line string")
            normalized = value.strip()
            if _PLAIN_CUSTOM_EMOJI_ALIAS.fullmatch(normalized):
                normalized = _DEFAULT_STATUS_EMOJIS[name]
            object.__setattr__(self, name, normalized)


@dataclass(frozen=True, slots=True)
class ProgressEditPolicy:
    min_edit_interval_seconds: float = 1.5

    def __post_init__(self) -> None:
        value = self.min_edit_interval_seconds
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.25 <= float(value) <= 10.0:
            raise ValueError("min_edit_interval_seconds must be between 0.25 and 10")
        object.__setattr__(self, "min_edit_interval_seconds", float(value))


@dataclass(frozen=True, slots=True)
class AIProgressPlan:
    title: str
    tasks: tuple[str, ...]
    active_index: int
    artifact_index: int | None = None

    def __post_init__(self) -> None:
        title = _bounded_line(self.title, maximum=200)
        tasks = tuple(_bounded_line(task, maximum=_MAX_LABEL_CHARS) for task in self.tasks)
        if not 2 <= len(tasks) <= _MAX_TASKS:
            raise ValueError(f"a progress plan must contain 2 to {_MAX_TASKS} tasks")
        if len(set(tasks)) != len(tasks):
            raise ValueError("progress plan tasks must be unique")
        if isinstance(self.active_index, bool) or not isinstance(self.active_index, int):
            raise TypeError("active_index must be an integer")
        if not 0 <= self.active_index < len(tasks):
            raise ValueError("active_index is outside the task list")
        if self.artifact_index is not None:
            if isinstance(self.artifact_index, bool) or not isinstance(self.artifact_index, int):
                raise TypeError("artifact_index must be an integer or None")
            if not 0 <= self.artifact_index < len(tasks):
                raise ValueError("artifact_index is outside the task list")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "tasks", tasks)


@dataclass(slots=True)
class _MutableTask:
    label: str
    state: TaskProgressState
    detail: str = ""


def build_ai_progress_plan(
    *,
    route: AITaskRoute,
    instruction: str,
    attachment_count: int,
    has_reference: bool,
) -> AIProgressPlan | None:
    """簡単な会話は即答し、処理段階が意味を持つ依頼だけをタスク化する。"""

    if not isinstance(route, AITaskRoute):
        raise TypeError("route must be an AITaskRoute")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("instruction must be a non-empty string")
    if type(has_reference) is not bool:
        raise TypeError("has_reference must be a boolean")
    if isinstance(attachment_count, bool) or not isinstance(attachment_count, int) or attachment_count < 0:
        raise ValueError("attachment_count must be a non-negative integer")

    if not route.show_progress:
        return None

    tasks: list[str] = ["依頼を解析"]
    if has_reference and attachment_count:
        tasks.append("返信元と添付ファイルを確認")
    elif has_reference:
        tasks.append("返信元の内容を確認")
    elif attachment_count:
        tasks.append("添付ファイルを解析")
    if "grounded_tool_evidence" in route.reason_codes:
        tasks.append("取得済みの根拠を確認")
    is_complex = route.complexity is TaskComplexity.COMPLEX
    if is_complex:
        tasks.append("要件を分解")
    active_task: str
    if route.web_search:
        tasks.append("Webを検索して出典を確認")
        active_task = "Webを検索して出典を確認"
    elif route.uses_tools:
        tasks.append("必要な機能を安全に実行")
        active_task = "必要な機能を安全に実行"
    else:
        active_task = ""
    intent_task = _intent_task_label(route, instruction)
    if intent_task:
        tasks.append(intent_task)
        if not active_task:
            active_task = intent_task
    if not active_task:
        active_task = "AIで回答を組み立てる"
        tasks.append(active_task)
    artifact_index: int | None = None
    if route.expects_artifact:
        tasks.append("成果物を確認")
    if is_complex:
        tasks.append("結果を検証")
    tasks.append("Discord向けに整形")

    # 同じ意味の工程が重なってもカードを膨らませない。
    unique_tasks = tuple(dict.fromkeys(tasks))[:_MAX_STATIC_TASKS]
    active_index = unique_tasks.index(active_task)
    if route.expects_artifact and "成果物を確認" in unique_tasks:
        artifact_index = unique_tasks.index("成果物を確認")
    return AIProgressPlan(
        title="YonerAI • タスク / ステータス",
        tasks=unique_tasks,
        active_index=active_index,
        artifact_index=artifact_index,
    )


def _intent_task_label(route: AITaskRoute, instruction: str) -> str | None:
    """入力分類と依頼本文から、秘密を含まない工程名だけを選ぶ。"""

    intent = route.intent
    normalized = instruction.casefold()
    if intent is AIIntent.WEB_RESEARCH and route.expects_artifact:
        if any(marker in normalized for marker in ("web作って", "webを作", "ウェブを作", "サイトを作")):
            return "Webサイトを作成"
        if any(marker in normalized for marker in ("画像を生成", "画像生成", "generate an image")):
            return "画像を生成"
        if any(marker in normalized for marker in ("動画を生成", "動画生成", "generate a video")):
            return "動画を生成"
        if any(marker in normalized for marker in ("音楽を生成", "音楽生成", "generate music")):
            return "音楽を生成"
        return "成果物を作成"
    if intent is AIIntent.SITE:
        return "Webサイトを作成"
    if intent is AIIntent.CODE:
        return "コードを作成"
    if intent is AIIntent.MEDIA:
        if "動画" in normalized or "video" in normalized:
            return "動画を生成"
        if "音楽" in normalized or "music" in normalized:
            return "音楽を生成"
        if "画像" in normalized or "image" in normalized:
            return "画像を生成"
        return "メディアを生成"
    if intent is AIIntent.MUSIC:
        return "音楽の依頼を整理"
    if intent is AIIntent.MEMORY:
        return "記憶内容を整理"
    if intent is AIIntent.KNOWLEDGE:
        return "情報を整理"
    if intent is AIIntent.SELF_EVOLUTION:
        return "改善案を検討"
    if intent is AIIntent.MODERATION:
        return "依頼内容を確認"
    return None


def _validated_execution_steps(
    steps: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(steps, tuple):
        raise TypeError("steps must be a tuple")
    if not 1 <= len(steps) <= _MAX_EXECUTION_STEPS:
        raise ValueError(f"steps must contain 1 to {_MAX_EXECUTION_STEPS} entries")

    step_ids: set[str] = set()
    public_labels: list[str] = []
    validated_ids: list[str] = []
    for step in steps:
        if not isinstance(step, tuple) or len(step) != 2:
            raise TypeError("each step must be a (step_id, public_label) tuple")
        step_id, public_label = step
        if (
            not isinstance(step_id, str)
            or not step_id.strip()
            or "\n" in step_id
            or "\r" in step_id
            or len(step_id) > 128
        ):
            raise ValueError("step_id must be a bounded single-line string")
        if step_id in step_ids:
            raise ValueError("step_id values must be unique")
        step_ids.add(step_id)
        validated_ids.append(step_id)
        public_labels.append(_bounded_line(public_label, maximum=_MAX_LABEL_CHARS))

    unique_labels = _unique_public_labels(tuple(public_labels))
    return tuple(zip(validated_ids, unique_labels, strict=True))


def _unique_public_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    used = set(_EXECUTION_WRAPPER_TASKS)
    unique: list[str] = []
    for label in labels:
        candidate = label
        suffix_number = 2
        while candidate in used:
            suffix = f" ({suffix_number})"
            candidate = f"{label[: _MAX_LABEL_CHARS - len(suffix)].rstrip()}{suffix}"
            suffix_number += 1
        used.add(candidate)
        unique.append(candidate)
    return tuple(unique)


class DiscordAITaskProgressSession:
    """1件のDiscord replyをterminalまで一度だけ更新する。"""

    def __init__(
        self,
        message: Any,
        plan: AIProgressPlan,
        *,
        emojis: TaskStatusEmojis,
        edit_policy: ProgressEditPolicy,
    ) -> None:
        self.message = message
        self.plan = plan
        self.emojis = emojis
        self.edit_policy = edit_policy
        self._tasks = [
            _MutableTask(
                label=label,
                state=(
                    TaskProgressState.DONE
                    if index < plan.active_index
                    else TaskProgressState.RUNNING
                    if index == plan.active_index
                    else TaskProgressState.PENDING
                ),
            )
            for index, label in enumerate(plan.tasks)
        ]
        self._terminal = False
        self._lock = asyncio.Lock()
        self._last_edit_at = time.monotonic()
        self._pending_edit_task: asyncio.Task[None] | None = None
        self._edit_generation = 0
        self._execution_step_indices: dict[str, int] = {}
        self._execution_plan_failed = False

    @property
    def terminal(self) -> bool:
        return self._terminal

    async def bind_execution_steps(
        self,
        steps: tuple[tuple[str, str], ...],
    ) -> bool:
        """検証済みplannerの公開工程名だけを現在のカードへ結び付ける。"""

        validated_steps = _validated_execution_steps(steps)
        tasks = (
            _EXECUTION_WRAPPER_TASKS[0],
            *(label for _, label in validated_steps),
            _EXECUTION_WRAPPER_TASKS[1],
            _EXECUTION_WRAPPER_TASKS[2],
        )
        plan = AIProgressPlan(
            title=self.plan.title,
            tasks=tasks,
            active_index=1,
        )
        step_indices = {step_id: index for index, (step_id, _) in enumerate(validated_steps, start=1)}

        async with self._lock:
            if self._terminal:
                return False
            self.plan = plan
            self._execution_step_indices = step_indices
            self._execution_plan_failed = False
            self._tasks = [
                _MutableTask(
                    label=label,
                    state=(
                        TaskProgressState.DONE
                        if index == 0
                        else TaskProgressState.RUNNING
                        if index == plan.active_index
                        else TaskProgressState.PENDING
                    ),
                )
                for index, label in enumerate(plan.tasks)
            ]
            return await self._request_edit_locked()

    async def apply_plan_event(self, event: PlanEvent) -> bool:
        """planner eventをstep_idで対応する公開工程へ反映する。"""

        if not isinstance(event, PlanEvent):
            raise TypeError("event must be a PlanEvent")
        if not isinstance(event.event_type, PlanEventType):
            raise TypeError("event.event_type must be a PlanEventType")

        async with self._lock:
            if self._terminal or not self._execution_step_indices:
                return False

            event_type = event.event_type
            if event_type is PlanEventType.PLAN_STARTED:
                self._tasks[0].state = TaskProgressState.DONE
            elif event_type in {
                PlanEventType.STEP_STARTED,
                PlanEventType.STEP_COMPLETED,
                PlanEventType.STEP_FAILED,
            }:
                index = self._execution_step_indices.get(event.step_id or "")
                if index is None:
                    return False
                task = self._tasks[index]
                task.detail = ""
                task.state = {
                    PlanEventType.STEP_STARTED: TaskProgressState.RUNNING,
                    PlanEventType.STEP_COMPLETED: TaskProgressState.DONE,
                    PlanEventType.STEP_FAILED: TaskProgressState.FAILED,
                }[event_type]
                self._tasks[0].state = TaskProgressState.DONE
            elif event_type in {
                PlanEventType.PLAN_COMPLETED,
                PlanEventType.PLAN_FAILED,
            }:
                if event_type is PlanEventType.PLAN_COMPLETED:
                    for index in self._execution_step_indices.values():
                        if self._tasks[index].state is not TaskProgressState.FAILED:
                            self._tasks[index].state = TaskProgressState.DONE
                else:
                    self._execution_plan_failed = True
                verification = self._tasks[-2]
                verification.state = TaskProgressState.RUNNING
                verification.detail = ""
            else:
                return False
            return await self._request_edit_locked()

    async def apply_plan_receipt(self, receipt: PlanReceipt) -> bool:
        """observer欠落時も、terminal receiptを実行状態の正本として反映する。"""

        if not isinstance(receipt, PlanReceipt):
            raise TypeError("receipt must be a PlanReceipt")
        async with self._lock:
            if self._terminal or not self._execution_step_indices:
                return False
            receipt_steps = {step.step_id: step for step in receipt.steps}
            self._execution_plan_failed = receipt.status is PlanStatus.FAILED
            self._tasks[0].state = TaskProgressState.DONE
            for step_id, index in self._execution_step_indices.items():
                step = receipt_steps.get(step_id)
                if step is None:
                    if self._execution_plan_failed:
                        self._tasks[index].state = TaskProgressState.PENDING
                    continue
                self._tasks[index].state = {
                    StepStatus.COMPLETED: TaskProgressState.DONE,
                    StepStatus.FAILED: TaskProgressState.FAILED,
                    StepStatus.NOT_RUN: TaskProgressState.PENDING,
                }[step.status]
                self._tasks[index].detail = ""
            verification = self._tasks[-2]
            verification.state = TaskProgressState.RUNNING
            verification.detail = ""
            return await self._request_edit_locked()

    async def set_running(self, index: int, *, detail: str = "") -> bool:
        async with self._lock:
            if self._terminal or not 0 <= index < len(self._tasks):
                return False
            for task_index, task in enumerate(self._tasks):
                if task_index < index and task.state is not TaskProgressState.FAILED:
                    task.state = TaskProgressState.DONE
                elif task_index == index:
                    task.state = TaskProgressState.RUNNING
                    task.detail = _optional_bounded_line(detail, maximum=_MAX_DETAIL_CHARS)
            return await self._request_edit_locked()

    async def apply_gateway_event(self, event: RunEvent) -> bool:
        """安全に公開できるeventだけを現在工程へ反映する。"""

        task_index = self.plan.artifact_index if event.kind == "artifact" else self.plan.active_index
        if task_index is None:
            task_index = self.plan.active_index
        detail = gateway_progress_detail(event, task=self.plan.tasks[task_index])
        if detail is None:
            return False
        return await self.set_running(task_index, detail=detail)

    async def fail(self, public_message: str) -> bool:
        async with self._lock:
            if self._terminal:
                return False
            running = next(
                (task for task in self._tasks if task.state is TaskProgressState.RUNNING),
                self._tasks[-1],
            )
            running.state = TaskProgressState.FAILED
            running.detail = _bounded_line(public_message, maximum=_MAX_DETAIL_CHARS)
            self._terminal = True
            pending = self._cancel_pending_edit_locked()
            embed = self._build_embed(error=True)
        await _drain_cancelled_edit(pending)
        edited = await self._edit_embed(embed)
        if not edited:
            await self._delete_locked()
        return edited

    async def begin_terminal_success(self) -> str:
        """完了状態とterminal claimを原子的に確定し、最終rendererへ渡す。"""

        async with self._lock:
            if self._terminal:
                return self.summary()
            execution_indices = set(self._execution_step_indices.values())
            for index, task in enumerate(self._tasks):
                if task.state is not TaskProgressState.FAILED:
                    if not (
                        self._execution_plan_failed
                        and index in execution_indices
                        and task.state is TaskProgressState.PENDING
                    ):
                        task.state = TaskProgressState.DONE
                task.detail = ""
            self._terminal = True
            pending = self._cancel_pending_edit_locked()
            summary = self.summary()
        await _drain_cancelled_edit(pending)
        return summary

    async def mark_final_fallback(self) -> bool:
        """finalを別replyへ送った場合、旧running cardを完了化または削除する。"""

        async with self._lock:
            pending = self._cancel_pending_edit_locked()
            embed = self._build_embed(footer="回答は次のメッセージに表示しました。")
        await _drain_cancelled_edit(pending)
        edited = await self._edit_embed(embed)
        if not edited:
            await self._delete_locked()
        return edited

    async def final_delivery_failed(self, public_message: str) -> bool:
        """finalが一件も配信されなかった場合に、成功表示を失敗へ戻す。"""

        async with self._lock:
            if self._tasks[-1].state is TaskProgressState.FAILED:
                return False
            execution_indices = set(self._execution_step_indices.values())
            for index, task in enumerate(self._tasks[:-1]):
                if task.state is TaskProgressState.FAILED:
                    continue
                if (
                    self._execution_plan_failed
                    and index in execution_indices
                    and task.state is TaskProgressState.PENDING
                ):
                    continue
                task.state = TaskProgressState.DONE
                task.detail = ""
            self._tasks[-1].state = TaskProgressState.FAILED
            self._tasks[-1].detail = _bounded_line(public_message, maximum=_MAX_DETAIL_CHARS)
            self._terminal = True
            pending = self._cancel_pending_edit_locked()
            embed = self._build_embed(
                error=True,
                footer="最終回答をDiscordへ配信できませんでした。再度お試しください。",
            )
        await _drain_cancelled_edit(pending)
        edited = await self._edit_embed(embed)
        if not edited:
            await self._delete_locked()
        return edited

    def summary(self) -> str:
        return "\n".join(self._task_lines())[:1_024]

    async def _edit_locked(self, *, error: bool = False, footer: str | None = None) -> bool:
        return await self._edit_embed(self._build_embed(error=error, footer=footer))

    async def _edit_embed(self, embed: discord.Embed) -> bool:
        edit = getattr(self.message, "edit", None)
        if not callable(edit):
            return False
        try:
            await _discord_io_call(
                edit(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            )
        except Exception as exc:
            logger.warning("ai_task_progress_edit_failed", extra={"error_type": type(exc).__name__})
            return False
        self._last_edit_at = time.monotonic()
        return True

    async def _request_edit_locked(self) -> bool:
        self._edit_generation += 1
        now = time.monotonic()
        delay = self.edit_policy.min_edit_interval_seconds - (now - self._last_edit_at)
        if self._pending_edit_task is None or self._pending_edit_task.done():
            self._pending_edit_task = asyncio.create_task(
                self._delayed_edit(max(0.0, delay)),
                name="ai-progress-edit",
            )
        return True

    async def _delayed_edit(self, delay: float) -> None:
        try:
            next_delay = max(0.0, delay)
            while True:
                if next_delay > 0:
                    await asyncio.sleep(next_delay)
                async with self._lock:
                    if self._terminal:
                        return
                    generation = self._edit_generation
                    embed = self._build_embed()
                await self._edit_embed(embed)
                async with self._lock:
                    if self._terminal:
                        return
                    if generation == self._edit_generation:
                        if asyncio.current_task() is self._pending_edit_task:
                            self._pending_edit_task = None
                        return
                    next_delay = max(
                        0.0,
                        self.edit_policy.min_edit_interval_seconds - (time.monotonic() - self._last_edit_at),
                    )
        except asyncio.CancelledError:
            return
        finally:
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if current is self._pending_edit_task:
                self._pending_edit_task = None

    def _cancel_pending_edit_locked(self) -> asyncio.Task[None] | None:
        pending = self._pending_edit_task
        self._pending_edit_task = None
        if pending is not None and not pending.done() and pending is not asyncio.current_task():
            pending.cancel()
        return pending

    async def _delete_locked(self) -> bool:
        delete = getattr(self.message, "delete", None)
        if not callable(delete):
            return False
        try:
            await _discord_io_call(delete())
        except Exception as exc:
            logger.warning("ai_task_progress_delete_failed", extra={"error_type": type(exc).__name__})
            return False
        return True

    def _build_embed(self, *, error: bool = False, footer: str | None = None) -> discord.Embed:
        title = self.plan.title
        footer_text = footer or "思考内容ではなく、実行状態だけを表示しています。"
        description, _ = fit_embed_description(
            "\n".join(self._task_lines()),
            title=title,
            footer=footer_text,
        )
        validate_discord_payload_budget(
            embeds=(
                DiscordEmbedText(
                    title=title,
                    description=description,
                    footer=footer_text,
                ),
            )
        )
        embed = discord.Embed(
            title=title,
            description=description,
            colour=discord.Colour.red() if error else discord.Colour.green(),
        )
        embed.set_footer(text=footer_text)
        return embed

    def _task_lines(self) -> list[str]:
        lines: list[str] = []
        for index, task in enumerate(self._tasks, start=1):
            icon = {
                TaskProgressState.PENDING: self.emojis.pending,
                TaskProgressState.RUNNING: self.emojis.processing,
                TaskProgressState.DONE: self.emojis.done,
                TaskProgressState.FAILED: self.emojis.failed,
            }[task.state]
            line = f"{index}. {icon} **{task.label}**"
            if task.detail:
                line += f"\n　└ {task.detail}"
            lines.append(line)
        return lines


class DiscordAITaskProgressRenderer:
    def __init__(
        self,
        *,
        emojis: TaskStatusEmojis | None = None,
        edit_policy: ProgressEditPolicy | None = None,
    ) -> None:
        self.emojis = emojis or TaskStatusEmojis()
        self.edit_policy = edit_policy or ProgressEditPolicy()

    async def start(
        self,
        source_message: Any,
        plan: AIProgressPlan,
    ) -> DiscordAITaskProgressSession | None:
        session_message = await _reply_progress(source_message, plan, self.emojis)
        if session_message is None:
            return None
        return DiscordAITaskProgressSession(
            session_message,
            plan,
            emojis=self.emojis,
            edit_policy=self.edit_policy,
        )


async def _reply_progress(
    source_message: Any,
    plan: AIProgressPlan,
    emojis: TaskStatusEmojis,
) -> Any | None:
    preview = DiscordAITaskProgressSession(
        object(),
        plan,
        emojis=emojis,
        edit_policy=ProgressEditPolicy(),
    )
    kwargs = {
        "embed": preview._build_embed(),
        "mention_author": False,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    reply = getattr(source_message, "reply", None)
    if callable(reply):
        try:
            return await _discord_io_call(reply(**kwargs))
        except TimeoutError:
            logger.warning("ai_task_progress_reply_timeout")
            return None
        except Exception as exc:
            logger.warning("ai_task_progress_reply_failed", extra={"error_type": type(exc).__name__})
    channel_send = getattr(getattr(source_message, "channel", None), "send", None)
    if not callable(channel_send):
        return None
    kwargs.pop("mention_author", None)
    reference = _message_reference(source_message)
    if reference is not None:
        kwargs["reference"] = reference
    try:
        return await _discord_io_call(channel_send(**kwargs))
    except TimeoutError:
        logger.warning("ai_task_progress_fallback_timeout")
        return None
    except Exception as exc:
        logger.warning("ai_task_progress_fallback_failed", extra={"error_type": type(exc).__name__})
    if "reference" in kwargs:
        kwargs.pop("reference", None)
        try:
            return await _discord_io_call(channel_send(**kwargs))
        except TimeoutError:
            logger.warning("ai_task_progress_unreferenced_fallback_timeout")
            return None
        except Exception as exc:
            logger.warning("ai_task_progress_unreferenced_fallback_failed", extra={"error_type": type(exc).__name__})
    return None


async def _discord_io_call(awaitable: Awaitable[Any]) -> Any:
    """進捗カードのDiscord I/Oが会話処理を無期限に保持しないようにする。"""

    async with asyncio.timeout(_DISCORD_IO_TIMEOUT_SECONDS):
        return await awaitable


async def _drain_cancelled_edit(pending: asyncio.Task[None] | None) -> None:
    if pending is None or pending is asyncio.current_task():
        return
    await asyncio.gather(pending, return_exceptions=True)


def _message_reference(source_message: Any) -> Any | None:
    to_reference = getattr(source_message, "to_reference", None)
    if not callable(to_reference):
        return None
    try:
        return to_reference(fail_if_not_exists=False)
    except (AttributeError, TypeError, ValueError):
        return None


def _bounded_line(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError("task text must be a string")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized:
        raise ValueError("task text must not be empty")
    return normalized[:maximum]


def _optional_bounded_line(value: str, *, maximum: int) -> str:
    if not value:
        return ""
    return _bounded_line(value, maximum=maximum)


__all__ = [
    "AIProgressPlan",
    "DiscordAITaskProgressRenderer",
    "DiscordAITaskProgressSession",
    "ProgressEditPolicy",
    "TaskProgressState",
    "TaskStatusEmojis",
    "build_ai_progress_plan",
    "gateway_progress_detail",
]
