"""Typed, bounded autonomy kernel for one code-owned research task.

This module deliberately exposes no shell, arbitrary URL, or free-form plan
surface.  The only template is an offline-testable investigation of official
SearXNG documentation.  Live search, browser, artifact, checkpoint, and
terminal delivery implementations remain injected ports.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from yonerai_discord.browser_sandbox.models import (
    BrowserSessionRequest,
    ExtractText,
    Navigate,
    Screenshot,
)
from yonerai_discord.provider_registry.domain import ArtifactKind, ArtifactRef

from .contracts import SearchIntent, SearchSourceClass, query_digest
from .orchestrator import SearchOrchestratorOutcome


_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_ACTOR_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,191}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_OFFICIAL_PAGES = 3
_MAX_PAGE_TEXT_CHARS = 12_000
_FINALIZATION_TIMEOUT_SECONDS = 1.0


class AutonomyError(RuntimeError):
    """Base error for the bounded autonomy service."""


class AutonomyAuthorizationError(AutonomyError, PermissionError):
    """Fresh execution authorization is no longer current."""


class AutonomyContractError(AutonomyError):
    """An injected port returned a value outside the typed contract."""


class AutonomyCheckpointError(AutonomyError):
    """A checkpoint is inconsistent with the current task binding or plan."""


class AutonomyStep(StrEnum):
    SEARCH = "search"
    BROWSER_INSPECT = "browser_inspect"
    COMPARISON_ARTIFACT = "comparison_artifact"


class AutonomyPlanStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AutonomyTerminalKind(StrEnum):
    FINAL = "final"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AutonomyBinding:
    task_id: str
    actor_ref: str
    actor_id: int
    guild_id: int
    channel_id: int
    user_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or _IDENTIFIER.fullmatch(self.task_id) is None:
            raise ValueError("task_id is invalid")
        if not isinstance(self.actor_ref, str) or _ACTOR_REF.fullmatch(self.actor_ref) is None:
            raise ValueError("actor_ref is invalid")
        for name in ("actor_id", "guild_id", "channel_id", "user_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @property
    def digest(self) -> str:
        return _sha256(
            {
                "task_id": self.task_id,
                "actor_ref": self.actor_ref,
                "actor_id": self.actor_id,
                "guild_id": self.guild_id,
                "channel_id": self.channel_id,
                "user_id": self.user_id,
            }
        )


@dataclass(frozen=True, slots=True)
class ScopedAutonomyArtifact:
    """Opaque artifact bound to one caller, request, and sealed plan."""

    task_id: str
    actor_id: int
    guild_id: int
    channel_id: int
    user_id: int
    plan_digest: str
    artifact: ArtifactRef = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or _IDENTIFIER.fullmatch(self.task_id) is None:
            raise ValueError("artifact task_id is invalid")
        for name in ("actor_id", "guild_id", "channel_id", "user_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"artifact {name} must be a positive integer")
        if not isinstance(self.plan_digest, str) or _DIGEST.fullmatch(self.plan_digest) is None:
            raise ValueError("artifact plan_digest is invalid")
        _require_exact_artifact(self.artifact)

    @classmethod
    def bind(
        cls,
        artifact: ArtifactRef,
        *,
        binding: AutonomyBinding,
        plan_digest: str,
    ) -> ScopedAutonomyArtifact:
        return cls(
            task_id=binding.task_id,
            actor_id=binding.actor_id,
            guild_id=binding.guild_id,
            channel_id=binding.channel_id,
            user_id=binding.user_id,
            plan_digest=plan_digest,
            artifact=artifact,
        )

    def require_current(self, binding: AutonomyBinding, plan_digest: str) -> None:
        if (
            self.task_id != binding.task_id
            or self.actor_id != binding.actor_id
            or self.guild_id != binding.guild_id
            or self.channel_id != binding.channel_id
            or self.user_id != binding.user_id
            or self.plan_digest != plan_digest
        ):
            raise AutonomyCheckpointError("artifact is not bound to the current caller and plan")
        _require_exact_artifact(self.artifact)


@dataclass(frozen=True, slots=True)
class AutonomyTaskTemplate:
    template_id: str
    version: int
    query: str = field(repr=False)
    language: str
    allowed_hosts: tuple[str, ...]
    steps: tuple[AutonomyStep, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.template_id, str) or _IDENTIFIER.fullmatch(self.template_id) is None:
            raise ValueError("template_id is invalid")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version <= 0:
            raise ValueError("template version must be positive")
        query_digest(self.query)
        if self.language != "en":
            raise ValueError("the code-owned template language must be en")
        hosts = tuple(self.allowed_hosts)
        if not hosts or any(host != host.lower() or not host or "/" in host for host in hosts):
            raise ValueError("allowed_hosts are invalid")
        if len(hosts) != len(set(hosts)):
            raise ValueError("allowed_hosts must be unique")
        steps = tuple(self.steps)
        if steps != (
            AutonomyStep.SEARCH,
            AutonomyStep.BROWSER_INSPECT,
            AutonomyStep.COMPARISON_ARTIFACT,
        ):
            raise ValueError("template steps are not the fixed bounded pipeline")
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "steps", steps)

    @property
    def digest(self) -> str:
        return _sha256(
            {
                "template_id": self.template_id,
                "version": self.version,
                "query_digest": query_digest(self.query),
                "language": self.language,
                "allowed_hosts": list(self.allowed_hosts),
                "steps": [step.value for step in self.steps],
            }
        )


SEARXNG_OFFICIAL_RESEARCH_V1 = AutonomyTaskTemplate(
    template_id="searxng.official-research",
    version=1,
    query="site:docs.searxng.org SearXNG search API configuration engines settings",
    language="en",
    allowed_hosts=("docs.searxng.org",),
    steps=(
        AutonomyStep.SEARCH,
        AutonomyStep.BROWSER_INSPECT,
        AutonomyStep.COMPARISON_ARTIFACT,
    ),
)


@dataclass(frozen=True, slots=True)
class AutonomyPolicy:
    max_steps: int = 3
    total_timeout_seconds: float = 90.0
    step_timeout_seconds: float = 30.0
    failure_retries: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int) or not 1 <= self.max_steps <= 8:
            raise ValueError("max_steps is outside the allowed range")
        for name in ("total_timeout_seconds", "step_timeout_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.05 <= float(value) <= 600.0
            ):
                raise ValueError(f"{name} is outside the allowed range")
            object.__setattr__(self, name, float(value))
        if self.step_timeout_seconds > self.total_timeout_seconds:
            raise ValueError("step_timeout_seconds must not exceed total_timeout_seconds")
        if self.failure_retries not in (0, 1):
            raise ValueError("failure_retries must be 0 or 1")


@dataclass(frozen=True, slots=True)
class BrowserResearchRequest:
    binding: AutonomyBinding
    urls: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.binding, AutonomyBinding):
            raise TypeError("binding must be an AutonomyBinding")
        urls = tuple(self.urls)
        if not 1 <= len(urls) <= _MAX_OFFICIAL_PAGES or len(urls) != len(set(urls)):
            raise ValueError("urls are outside the bounded contract")
        for url in urls:
            _require_official_url(url, SEARXNG_OFFICIAL_RESEARCH_V1.allowed_hosts)
        object.__setattr__(self, "urls", urls)

    @property
    def sessions(self) -> tuple[BrowserSessionRequest, ...]:
        """Project each URL to the existing isolated-browser typed actions."""

        return tuple(
            BrowserSessionRequest((Navigate(url), ExtractText(), Screenshot(full_page=True))) for url in self.urls
        )


@dataclass(frozen=True, slots=True)
class OfficialPageInspection:
    url: str
    title: str
    text: str = field(repr=False)
    screenshot: ScopedAutonomyArtifact = field(repr=False)

    def __post_init__(self) -> None:
        _require_official_url(self.url, SEARXNG_OFFICIAL_RESEARCH_V1.allowed_hosts)
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 500:
            raise ValueError("inspection title is invalid")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > _MAX_PAGE_TEXT_CHARS:
            raise ValueError("inspection text is invalid")
        if (
            not isinstance(self.screenshot, ScopedAutonomyArtifact)
            or self.screenshot.artifact.kind is not ArtifactKind.SCREENSHOT
            or self.screenshot.artifact.media_type != "image/png"
        ):
            raise TypeError("inspection screenshot must be a scoped PNG screenshot artifact")


@dataclass(frozen=True, slots=True)
class BrowserResearchResult:
    pages: tuple[OfficialPageInspection, ...]

    def __post_init__(self) -> None:
        pages = tuple(self.pages)
        if not 1 <= len(pages) <= _MAX_OFFICIAL_PAGES:
            raise ValueError("inspection pages are outside the bounded contract")
        if any(not isinstance(page, OfficialPageInspection) for page in pages):
            raise TypeError("pages must contain OfficialPageInspection values")
        if len({page.url for page in pages}) != len(pages):
            raise ValueError("inspection pages must be unique")
        object.__setattr__(self, "pages", pages)


@dataclass(frozen=True, slots=True)
class ComparisonTableRequest:
    binding: AutonomyBinding
    search: SearchOrchestratorOutcome = field(repr=False)
    browser: BrowserResearchResult = field(repr=False)
    columns: tuple[str, ...] = ("official_page", "topic", "documented_behavior", "evidence")

    def __post_init__(self) -> None:
        if not isinstance(self.binding, AutonomyBinding):
            raise TypeError("binding must be an AutonomyBinding")
        if not isinstance(self.search, SearchOrchestratorOutcome):
            raise TypeError("search must be a SearchOrchestratorOutcome")
        if not isinstance(self.browser, BrowserResearchResult):
            raise TypeError("browser must be a BrowserResearchResult")
        if tuple(self.columns) != ("official_page", "topic", "documented_behavior", "evidence"):
            raise ValueError("comparison table columns are code-owned")


AuthorizationCurrent = Callable[[], Awaitable[bool]]


class AutonomySearchPort(Protocol):
    async def search(
        self,
        query: str,
        *,
        request_id: str,
        intent: SearchIntent,
        language: str,
        high_stakes: bool,
        authorization_current: AuthorizationCurrent,
    ) -> SearchOrchestratorOutcome: ...


class OfficialBrowserPort(Protocol):
    async def inspect(
        self,
        request: BrowserResearchRequest,
        *,
        authorization_current: AuthorizationCurrent,
    ) -> BrowserResearchResult: ...


class ComparisonArtifactPort(Protocol):
    async def create(
        self,
        request: ComparisonTableRequest,
        *,
        authorization_current: AuthorizationCurrent,
    ) -> ScopedAutonomyArtifact: ...


@dataclass(frozen=True, slots=True)
class StepAttempt:
    step: AutonomyStep
    attempts: int
    last_failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.step, AutonomyStep):
            raise TypeError("step must be an AutonomyStep")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or not 0 <= self.attempts <= 1_000_000:
            raise ValueError("attempts are outside the bounded contract")
        if self.last_failure_code is not None and (
            not isinstance(self.last_failure_code, str) or _IDENTIFIER.fullmatch(self.last_failure_code) is None
        ):
            raise ValueError("last_failure_code is invalid")


@dataclass(frozen=True, slots=True)
class AutonomyCheckpoint:
    template_id: str
    template_version: int
    plan_digest: str
    binding_digest: str
    status: AutonomyPlanStatus
    completed_steps: tuple[AutonomyStep, ...] = ()
    attempts: tuple[StepAttempt, ...] = ()
    execution_attempts: tuple[StepAttempt, ...] = ()
    search: SearchOrchestratorOutcome | None = field(default=None, repr=False)
    browser: BrowserResearchResult | None = field(default=None, repr=False)
    artifact: ScopedAutonomyArtifact | None = field(default=None, repr=False)
    failure_code: str | None = None
    terminal_emitted: bool = False
    post_authorization_pending: AutonomyStep | None = None
    content_compacted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.template_id, str) or _IDENTIFIER.fullmatch(self.template_id) is None:
            raise ValueError("checkpoint template_id is invalid")
        if isinstance(self.template_version, bool) or not isinstance(self.template_version, int):
            raise ValueError("checkpoint template_version is invalid")
        for label, value in (("plan_digest", self.plan_digest), ("binding_digest", self.binding_digest)):
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise ValueError(f"checkpoint {label} is invalid")
        if not isinstance(self.status, AutonomyPlanStatus):
            raise TypeError("checkpoint status must be an AutonomyPlanStatus")
        completed = tuple(self.completed_steps)
        if completed != SEARXNG_OFFICIAL_RESEARCH_V1.steps[: len(completed)]:
            raise ValueError("completed_steps must be an ordered template prefix")
        attempts = tuple(self.attempts)
        if any(not isinstance(item, StepAttempt) for item in attempts):
            raise TypeError("checkpoint attempts must contain StepAttempt values")
        if len({item.step for item in attempts}) != len(attempts):
            raise ValueError("checkpoint attempts must be unique per step")
        execution_attempts = tuple(self.execution_attempts)
        if any(not isinstance(item, StepAttempt) or item.attempts > 2 for item in execution_attempts):
            raise TypeError("checkpoint execution_attempts are invalid")
        if len({item.step for item in execution_attempts}) != len(execution_attempts):
            raise ValueError("checkpoint execution_attempts must be unique per step")
        totals = {item.step: item for item in attempts}
        if any(
            item.step not in totals
            or totals[item.step].attempts < item.attempts
            or totals[item.step].last_failure_code != item.last_failure_code
            for item in execution_attempts
        ):
            raise ValueError("checkpoint attempt totals are inconsistent")
        if type(self.content_compacted) is not bool:
            raise TypeError("content_compacted must be a boolean")
        if self.content_compacted:
            if self.search is not None or self.browser is not None:
                raise ValueError("compact checkpoints cannot retain transient evidence")
            if (AutonomyStep.COMPARISON_ARTIFACT in completed) != (self.artifact is not None):
                raise ValueError("compact artifact checkpoint state is inconsistent")
            if self.status is AutonomyPlanStatus.RUNNING:
                if (
                    completed != SEARXNG_OFFICIAL_RESEARCH_V1.steps
                    or self.artifact is None
                    or self.post_authorization_pending is not AutonomyStep.COMPARISON_ARTIFACT
                ):
                    raise ValueError("compact running checkpoint must await final artifact authorization")
            elif self.post_authorization_pending is not None:
                raise ValueError("compact checkpoints cannot retain a pending authorization")
        else:
            if (AutonomyStep.SEARCH in completed) != (self.search is not None):
                raise ValueError("search checkpoint state is inconsistent")
            if (AutonomyStep.BROWSER_INSPECT in completed) != (self.browser is not None):
                raise ValueError("browser checkpoint state is inconsistent")
            if (AutonomyStep.COMPARISON_ARTIFACT in completed) != (self.artifact is not None):
                raise ValueError("artifact checkpoint state is inconsistent")
        if self.status is AutonomyPlanStatus.COMPLETED and len(completed) != len(SEARXNG_OFFICIAL_RESEARCH_V1.steps):
            raise ValueError("completed checkpoint is missing a step")
        if self.status is AutonomyPlanStatus.FAILED and self.failure_code is None:
            raise ValueError("failed checkpoint requires a failure_code")
        if self.status is not AutonomyPlanStatus.FAILED and self.failure_code is not None:
            raise ValueError("only failed checkpoints may carry a failure_code")
        if type(self.terminal_emitted) is not bool:
            raise TypeError("terminal_emitted must be a boolean")
        if self.terminal_emitted and self.status is AutonomyPlanStatus.RUNNING:
            raise ValueError("a running checkpoint cannot have emitted a terminal")
        if self.post_authorization_pending is not None and (
            not isinstance(self.post_authorization_pending, AutonomyStep)
            or self.post_authorization_pending not in completed
            or self.status is not AutonomyPlanStatus.RUNNING
        ):
            raise ValueError("post_authorization_pending is inconsistent")
        object.__setattr__(self, "completed_steps", completed)
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "execution_attempts", execution_attempts)


class AutonomyCheckpointPort(Protocol):
    async def load(self, task_id: str) -> AutonomyCheckpoint | DurableAutonomyCheckpoint | None: ...

    async def save(self, task_id: str, checkpoint: AutonomyCheckpoint) -> None: ...


@dataclass(frozen=True, slots=True)
class DurableAutonomyCheckpoint:
    """Content-free checkpoint projection loaded from the durable journal."""

    template_id: str
    template_version: int
    plan_digest: str
    binding_digest: str
    status: AutonomyPlanStatus
    completed_steps: tuple[AutonomyStep, ...] = ()
    attempts: tuple[StepAttempt, ...] = ()
    execution_attempts: tuple[StepAttempt, ...] = ()
    artifact: ArtifactRef | None = field(default=None, repr=False)
    failure_code: str | None = None
    terminal_emitted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.template_id, str) or _IDENTIFIER.fullmatch(self.template_id) is None:
            raise ValueError("durable checkpoint template_id is invalid")
        if isinstance(self.template_version, bool) or not isinstance(self.template_version, int):
            raise ValueError("durable checkpoint template_version is invalid")
        for label, value in (("plan_digest", self.plan_digest), ("binding_digest", self.binding_digest)):
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise ValueError(f"durable checkpoint {label} is invalid")
        if not isinstance(self.status, AutonomyPlanStatus):
            raise TypeError("durable checkpoint status must be an AutonomyPlanStatus")
        completed = tuple(self.completed_steps)
        if completed != SEARXNG_OFFICIAL_RESEARCH_V1.steps[: len(completed)]:
            raise ValueError("durable completed_steps must be an ordered template prefix")
        attempts = tuple(self.attempts)
        if any(not isinstance(item, StepAttempt) for item in attempts):
            raise TypeError("durable attempts must contain StepAttempt values")
        if len({item.step for item in attempts}) != len(attempts):
            raise ValueError("durable attempts must be unique per step")
        execution_attempts = tuple(self.execution_attempts)
        if any(not isinstance(item, StepAttempt) or item.attempts > 2 for item in execution_attempts):
            raise TypeError("durable execution_attempts are invalid")
        if len({item.step for item in execution_attempts}) != len(execution_attempts):
            raise ValueError("durable execution_attempts must be unique per step")
        totals = {item.step: item for item in attempts}
        if any(
            item.step not in totals
            or totals[item.step].attempts < item.attempts
            or totals[item.step].last_failure_code != item.last_failure_code
            for item in execution_attempts
        ):
            raise ValueError("durable attempt totals are inconsistent")
        if self.artifact is not None:
            _require_exact_artifact(self.artifact)
        if (AutonomyStep.COMPARISON_ARTIFACT in completed) != (self.artifact is not None):
            raise ValueError("durable artifact state is inconsistent")
        if self.status is AutonomyPlanStatus.COMPLETED and len(completed) != len(SEARXNG_OFFICIAL_RESEARCH_V1.steps):
            raise ValueError("durable completed checkpoint is missing a step")
        if self.status is AutonomyPlanStatus.FAILED and self.failure_code is None:
            raise ValueError("durable failed checkpoint requires a failure_code")
        if self.status is not AutonomyPlanStatus.FAILED and self.failure_code is not None:
            raise ValueError("only durable failed checkpoints may carry a failure_code")
        if type(self.terminal_emitted) is not bool:
            raise TypeError("durable terminal_emitted must be a boolean")
        if self.terminal_emitted and self.status is AutonomyPlanStatus.RUNNING:
            raise ValueError("a durable running checkpoint cannot have emitted a terminal")
        object.__setattr__(self, "completed_steps", completed)
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "execution_attempts", execution_attempts)


@dataclass(frozen=True, slots=True)
class AutonomyStepReceipt:
    step: AutonomyStep
    completed: bool
    attempts: int
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class AutonomyPlanReceipt:
    task_id: str
    template_id: str
    template_version: int
    plan_digest: str
    binding_digest: str
    status: AutonomyPlanStatus
    steps: tuple[AutonomyStepReceipt, ...]
    artifact_id: str | None = None
    failure_code: str | None = None

    def to_audit_projection(self) -> dict[str, object]:
        """Return metadata only: no query, URL, page text, or screenshot bytes."""

        return {
            "schema": "yonerai.autonomy-audit.v1",
            "task_id": self.task_id,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "plan_digest": self.plan_digest,
            "binding_digest": self.binding_digest,
            "status": self.status.value,
            "steps": [
                {
                    "step": item.step.value,
                    "completed": item.completed,
                    "attempts": item.attempts,
                    "failure_code": item.failure_code,
                }
                for item in self.steps
            ],
            "artifact_id": self.artifact_id,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True, slots=True)
class AutonomyTerminalNotice:
    idempotency_key: str
    kind: AutonomyTerminalKind
    receipt: AutonomyPlanReceipt

    def __post_init__(self) -> None:
        if not isinstance(self.idempotency_key, str) or _DIGEST.fullmatch(self.idempotency_key) is None:
            raise ValueError("terminal idempotency_key is invalid")
        if not isinstance(self.kind, AutonomyTerminalKind):
            raise TypeError("terminal kind must be an AutonomyTerminalKind")
        if not isinstance(self.receipt, AutonomyPlanReceipt):
            raise TypeError("receipt must be an AutonomyPlanReceipt")


class AutonomyTerminalPort(Protocol):
    async def publish_once(self, notice: AutonomyTerminalNotice) -> None:
        """Publish idempotently by ``notice.idempotency_key``."""


@dataclass(frozen=True, slots=True)
class AutonomyOutcome:
    receipt: AutonomyPlanReceipt
    artifact: ScopedAutonomyArtifact | None = field(default=None, repr=False)


class _StepExhausted(AutonomyError):
    def __init__(self, checkpoint: AutonomyCheckpoint, code: str) -> None:
        super().__init__(code)
        self.checkpoint = checkpoint
        self.code = code


class _PlanDeadlineExceeded(TimeoutError):
    def __init__(self, checkpoint: AutonomyCheckpoint) -> None:
        super().__init__("autonomy total timeout exceeded")
        self.checkpoint = checkpoint


class BoundedAutonomyService:
    """Execute or resume the single code-owned task through injected typed ports."""

    def __init__(
        self,
        *,
        search: AutonomySearchPort,
        browser: OfficialBrowserPort,
        artifacts: ComparisonArtifactPort,
        checkpoints: AutonomyCheckpointPort,
        terminal: AutonomyTerminalPort,
        policy: AutonomyPolicy = AutonomyPolicy(),
        template: AutonomyTaskTemplate = SEARXNG_OFFICIAL_RESEARCH_V1,
    ) -> None:
        for label, value, method in (
            ("search", search, "search"),
            ("browser", browser, "inspect"),
            ("artifacts", artifacts, "create"),
            ("checkpoints", checkpoints, "load"),
            ("terminal", terminal, "publish_once"),
        ):
            if not callable(getattr(value, method, None)):
                raise TypeError(f"{label} does not implement {method}()")
        if not callable(getattr(checkpoints, "save", None)):
            raise TypeError("checkpoints does not implement save()")
        if not isinstance(policy, AutonomyPolicy):
            raise TypeError("policy must be an AutonomyPolicy")
        if not isinstance(template, AutonomyTaskTemplate):
            raise TypeError("template must be an AutonomyTaskTemplate")
        if template is not SEARXNG_OFFICIAL_RESEARCH_V1:
            raise ValueError("only the sealed code-owned SearXNG research template is accepted")
        if len(template.steps) > policy.max_steps:
            raise ValueError("template exceeds max_steps")
        self._search = search
        self._browser = browser
        self._artifacts = artifacts
        self._checkpoints = checkpoints
        self._terminal = terminal
        self._policy = policy
        self._template = template
        acquire = getattr(checkpoints, "acquire_execution", None)
        release = getattr(checkpoints, "release_execution", None)
        if callable(acquire) != callable(release):
            raise TypeError("checkpoint execution lease methods must be provided together")
        self._lease_port = checkpoints if callable(acquire) else None

    async def execute(
        self,
        binding: AutonomyBinding,
        *,
        authorization_current: AuthorizationCurrent,
    ) -> AutonomyOutcome:
        if not isinstance(binding, AutonomyBinding):
            raise TypeError("binding must be an AutonomyBinding")
        if not callable(authorization_current):
            raise TypeError("authorization_current must be callable")
        deadline = asyncio.get_running_loop().time() + self._policy.total_timeout_seconds
        if self._lease_port is None:
            return await self._execute_owned(binding, authorization_current, deadline)

        await _require_authorized(authorization_current, deadline)
        acquired = await _bounded_call(
            self._lease_port.acquire_execution(
                binding,
                self._template,
                lease_seconds=(
                    self._policy.total_timeout_seconds
                    + self._policy.step_timeout_seconds
                    + _FINALIZATION_TIMEOUT_SECONDS
                ),
            ),
            deadline=deadline,
            timeout_seconds=self._policy.step_timeout_seconds,
        )
        if acquired is not True:
            raise AutonomyCheckpointError("autonomy task execution is already leased")
        try:
            await _require_authorized(authorization_current, deadline)
            return await self._execute_owned(binding, authorization_current, deadline)
        finally:
            await self._release_execution_lease()

    async def _execute_owned(
        self,
        binding: AutonomyBinding,
        authorization_current: AuthorizationCurrent,
        deadline: float,
    ) -> AutonomyOutcome:
        checkpoint = await self._load(binding, authorization_current, deadline)
        if checkpoint is None:
            checkpoint = AutonomyCheckpoint(
                template_id=self._template.template_id,
                template_version=self._template.version,
                plan_digest=self._template.digest,
                binding_digest=binding.digest,
                status=AutonomyPlanStatus.RUNNING,
            )
            await self._save(binding.task_id, checkpoint, authorization_current, deadline)
        else:
            self._validate_checkpoint(checkpoint, binding)

        if checkpoint.status is not AutonomyPlanStatus.RUNNING:
            checkpoint = await self._emit_terminal(binding, checkpoint, authorization_current, deadline)
            return AutonomyOutcome(
                _receipt(binding, checkpoint, self._template),
                _deliverable_artifact(checkpoint),
            )

        try:
            if checkpoint.post_authorization_pending is not None:
                await _require_authorized(authorization_current, deadline)
                checkpoint = replace(
                    checkpoint,
                    status=(
                        AutonomyPlanStatus.COMPLETED
                        if checkpoint.content_compacted and len(checkpoint.completed_steps) == len(self._template.steps)
                        else checkpoint.status
                    ),
                    post_authorization_pending=None,
                )
                await self._save_completed_result(binding.task_id, checkpoint, deadline)

            if AutonomyStep.SEARCH not in checkpoint.completed_steps:
                checkpoint, searched = await self._run_step(
                    binding,
                    checkpoint,
                    AutonomyStep.SEARCH,
                    authorization_current,
                    deadline,
                    lambda: self._search.search(
                        self._template.query,
                        request_id=binding.task_id,
                        intent=SearchIntent.OFFICIAL,
                        language=self._template.language,
                        high_stakes=False,
                        authorization_current=authorization_current,
                    ),
                    lambda current, result: _complete_search_step(current, result, binding),
                )
                assert isinstance(searched, SearchOrchestratorOutcome)

            if AutonomyStep.BROWSER_INSPECT not in checkpoint.completed_steps:
                assert checkpoint.search is not None
                browser_request = BrowserResearchRequest(
                    binding,
                    _official_urls(checkpoint.search, self._template.allowed_hosts),
                )
                checkpoint, inspected = await self._run_step(
                    binding,
                    checkpoint,
                    AutonomyStep.BROWSER_INSPECT,
                    authorization_current,
                    deadline,
                    lambda: self._browser.inspect(
                        browser_request,
                        authorization_current=authorization_current,
                    ),
                    lambda current, result: _complete_browser_step(
                        current,
                        result,
                        browser_request,
                        binding,
                        self._template.digest,
                    ),
                )
                assert isinstance(inspected, BrowserResearchResult)

            if AutonomyStep.COMPARISON_ARTIFACT not in checkpoint.completed_steps:
                assert checkpoint.search is not None and checkpoint.browser is not None
                request = ComparisonTableRequest(binding, checkpoint.search, checkpoint.browser)
                checkpoint, artifact = await self._run_step(
                    binding,
                    checkpoint,
                    AutonomyStep.COMPARISON_ARTIFACT,
                    authorization_current,
                    deadline,
                    lambda: self._artifacts.create(
                        request,
                        authorization_current=authorization_current,
                    ),
                    lambda current, result: _complete_artifact_step(
                        current,
                        result,
                        binding,
                        self._template.digest,
                    ),
                )
                assert isinstance(artifact, ScopedAutonomyArtifact)

            if len(checkpoint.completed_steps) == len(self._template.steps):
                checkpoint = replace(checkpoint, status=AutonomyPlanStatus.COMPLETED)
                await self._save(binding.task_id, checkpoint, authorization_current, deadline)
        except asyncio.CancelledError:
            raise
        except AutonomyAuthorizationError:
            raise
        except _PlanDeadlineExceeded as exc:
            checkpoint = await self._finalize_plan_timeout(binding, exc.checkpoint)
            return AutonomyOutcome(_receipt(binding, checkpoint, self._template), None)
        except TimeoutError:
            checkpoint = await self._finalize_plan_timeout(binding, checkpoint)
            return AutonomyOutcome(_receipt(binding, checkpoint, self._template), None)
        except _StepExhausted as exc:
            checkpoint = replace(
                exc.checkpoint,
                status=AutonomyPlanStatus.FAILED,
                failure_code=exc.code,
            )
            try:
                await self._save(binding.task_id, checkpoint, authorization_current, deadline)
            except TimeoutError:
                checkpoint = await self._finalize_plan_timeout(binding, checkpoint)
                return AutonomyOutcome(_receipt(binding, checkpoint, self._template), None)
        except (AutonomyContractError, TypeError, ValueError) as exc:
            checkpoint = replace(
                checkpoint,
                status=AutonomyPlanStatus.FAILED,
                failure_code=_failure_code(exc),
            )
            try:
                await self._save(binding.task_id, checkpoint, authorization_current, deadline)
            except TimeoutError:
                checkpoint = await self._finalize_plan_timeout(binding, checkpoint)
                return AutonomyOutcome(_receipt(binding, checkpoint, self._template), None)

        try:
            checkpoint = await self._emit_terminal(binding, checkpoint, authorization_current, deadline)
        except TimeoutError:
            checkpoint = await self._finalize_plan_timeout(binding, checkpoint)
        return AutonomyOutcome(
            _receipt(binding, checkpoint, self._template),
            _deliverable_artifact(checkpoint),
        )

    async def _release_execution_lease(self) -> None:
        assert self._lease_port is not None
        task = asyncio.create_task(self._lease_port.release_execution())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise
        except Exception:
            # The database lease has a bounded expiry. Cleanup failure must not
            # rewrite an already truthful outcome or expose internal details.
            pass

    async def _run_step(
        self,
        binding: AutonomyBinding,
        checkpoint: AutonomyCheckpoint,
        step: AutonomyStep,
        authorization_current: AuthorizationCurrent,
        deadline: float,
        operation: Callable[[], Awaitable[object]],
        complete: Callable[[AutonomyCheckpoint, object], AutonomyCheckpoint],
    ) -> tuple[AutonomyCheckpoint, object]:
        attempt = _attempt_count(checkpoint, step)
        maximum = 1 + self._policy.failure_retries
        while attempt < maximum:
            attempt += 1
            # Persist attempt consumption before entering the port. A process
            # crash therefore cannot reset the bounded retry budget on resume.
            checkpoint = _with_attempt(checkpoint, step, attempt, None)
            await self._save(binding.task_id, checkpoint, authorization_current, deadline)
            await _require_authorized(authorization_current, deadline)
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(min(remaining, self._policy.step_timeout_seconds)):
                    result = await operation()
            except asyncio.CancelledError:
                raise
            except AutonomyAuthorizationError:
                raise
            except Exception as exc:
                code = _failure_code(exc)
                checkpoint = _with_attempt(checkpoint, step, attempt, code)
                await self._save(binding.task_id, checkpoint, authorization_current, deadline)
                if attempt >= maximum:
                    raise _StepExhausted(checkpoint, code) from None
                continue
            checkpoint = complete(checkpoint, result)
            await self._save_completed_result(binding.task_id, checkpoint, deadline)
            try:
                await _require_authorized(authorization_current, deadline)
            except TimeoutError:
                raise _PlanDeadlineExceeded(checkpoint) from None
            checkpoint = replace(checkpoint, post_authorization_pending=None)
            await self._save_completed_result(binding.task_id, checkpoint, deadline)
            return checkpoint, result
        raise _StepExhausted(checkpoint, "attempt_budget_exhausted")

    async def _load(
        self,
        binding: AutonomyBinding,
        authorization_current: AuthorizationCurrent,
        deadline: float,
    ) -> AutonomyCheckpoint | None:
        await _require_authorized(authorization_current, deadline)
        value = await _bounded_call(
            self._checkpoints.load(binding.task_id),
            deadline=deadline,
            timeout_seconds=self._policy.step_timeout_seconds,
        )
        await _require_authorized(authorization_current, deadline)
        if isinstance(value, DurableAutonomyCheckpoint):
            value = _hydrate_durable_checkpoint(value, binding, self._template)
        elif value is not None and not isinstance(value, AutonomyCheckpoint):
            raise AutonomyCheckpointError("checkpoint port returned an invalid value")
        return value

    async def _save(
        self,
        task_id: str,
        checkpoint: AutonomyCheckpoint,
        authorization_current: AuthorizationCurrent,
        deadline: float,
    ) -> None:
        await _require_authorized(authorization_current, deadline)
        await _bounded_call(
            self._checkpoints.save(task_id, checkpoint),
            deadline=deadline,
            timeout_seconds=self._policy.step_timeout_seconds,
        )
        await _require_authorized(authorization_current, deadline)

    async def _save_completed_result(
        self,
        task_id: str,
        checkpoint: AutonomyCheckpoint,
        deadline: float,
    ) -> None:
        """Persist a completed port result before any post-port await can cancel it."""

        loop = asyncio.get_running_loop()
        persistence_deadline = max(
            deadline,
            loop.time() + min(self._policy.step_timeout_seconds, _FINALIZATION_TIMEOUT_SECONDS),
        )
        task = asyncio.create_task(
            _bounded_call(
                self._checkpoints.save(task_id, checkpoint),
                deadline=persistence_deadline,
                timeout_seconds=min(self._policy.step_timeout_seconds, _FINALIZATION_TIMEOUT_SECONDS),
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The port may already have created an external artifact. Finish the
            # bounded checkpoint write so resume cannot silently re-execute it.
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise

    async def _finalize_plan_timeout(
        self,
        binding: AutonomyBinding,
        checkpoint: AutonomyCheckpoint,
    ) -> AutonomyCheckpoint:
        """Boundedly persist and emit one truthful timeout terminal."""

        checkpoint = replace(
            checkpoint,
            status=AutonomyPlanStatus.FAILED,
            failure_code="plan_timeout",
            post_authorization_pending=None,
            terminal_emitted=False,
        )
        loop = asyncio.get_running_loop()
        per_call = min(self._policy.step_timeout_seconds, _FINALIZATION_TIMEOUT_SECONDS)
        try:
            await _bounded_call(
                self._checkpoints.save(binding.task_id, checkpoint),
                deadline=loop.time() + per_call,
                timeout_seconds=per_call,
            )
        except Exception:
            pass

        notice = _terminal_notice(binding, checkpoint, self._template)
        try:
            await _bounded_call(
                self._terminal.publish_once(notice),
                deadline=loop.time() + per_call,
                timeout_seconds=per_call,
            )
        except Exception:
            return checkpoint

        checkpoint = replace(checkpoint, terminal_emitted=True)
        try:
            await _bounded_call(
                self._checkpoints.save(binding.task_id, checkpoint),
                deadline=loop.time() + per_call,
                timeout_seconds=per_call,
            )
        except Exception:
            pass
        return checkpoint

    async def _emit_terminal(
        self,
        binding: AutonomyBinding,
        checkpoint: AutonomyCheckpoint,
        authorization_current: AuthorizationCurrent,
        deadline: float,
    ) -> AutonomyCheckpoint:
        if checkpoint.terminal_emitted:
            return checkpoint
        notice = _terminal_notice(binding, checkpoint, self._template)
        await _require_authorized(authorization_current, deadline)
        try:
            await _bounded_call(
                self._terminal.publish_once(notice),
                deadline=deadline,
                timeout_seconds=self._policy.step_timeout_seconds,
            )
        except TimeoutError:
            # The terminal port may have committed before its acknowledgement
            # timed out. Preserve the original terminal kind and retry the same
            # idempotent notice on resume instead of inventing an ERROR.
            return checkpoint
        checkpoint = replace(checkpoint, terminal_emitted=True)
        try:
            # A successful publish is an externally committed terminal. A
            # deadline crossed while recording that fact must never rewrite a
            # committed FINAL as a FAILED/ERROR terminal.
            await self._save_completed_result(binding.task_id, checkpoint, deadline)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The terminal port is idempotent by the plan-scoped key. A later
            # resume may safely reconcile the checkpoint without changing kind.
            pass
        return checkpoint

    def _validate_checkpoint(self, checkpoint: AutonomyCheckpoint, binding: AutonomyBinding) -> None:
        if (
            checkpoint.template_id != self._template.template_id
            or checkpoint.template_version != self._template.version
            or checkpoint.plan_digest != self._template.digest
            or checkpoint.binding_digest != binding.digest
        ):
            raise AutonomyCheckpointError("checkpoint is not bound to the current task and plan")
        if checkpoint.search is not None:
            try:
                _validate_search(checkpoint.search, binding)
            except AutonomyContractError as exc:
                raise AutonomyCheckpointError("checkpoint search result is invalid") from exc
        if checkpoint.browser is not None:
            try:
                _validate_browser_result(
                    checkpoint.browser,
                    binding,
                    self._template.digest,
                )
            except AutonomyContractError as exc:
                raise AutonomyCheckpointError("checkpoint screenshot artifact is invalid") from exc
        if checkpoint.artifact is not None:
            try:
                _validate_comparison_artifact(
                    checkpoint.artifact,
                    binding,
                    self._template.digest,
                )
            except AutonomyContractError as exc:
                raise AutonomyCheckpointError("checkpoint comparison artifact is invalid") from exc


async def _bounded_call(value: Awaitable[object], *, deadline: float, timeout_seconds: float) -> object:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("autonomy total timeout exceeded")
    async with asyncio.timeout(min(remaining, timeout_seconds)):
        return await value


async def _require_authorized(check: AuthorizationCurrent, deadline: float) -> None:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("autonomy total timeout exceeded")
    try:
        async with asyncio.timeout(remaining):
            allowed = await check()
    except asyncio.CancelledError:
        raise
    except Exception:
        allowed = False
    if allowed is not True:
        raise AutonomyAuthorizationError("autonomy authorization is no longer current")


def _validate_search(value: object, binding: AutonomyBinding) -> None:
    if not isinstance(value, SearchOrchestratorOutcome):
        raise AutonomyContractError("search port returned an invalid result")
    if (
        value.result.request_id != binding.task_id
        or value.result.intent is not SearchIntent.OFFICIAL
        or value.result.query_digest != query_digest(SEARXNG_OFFICIAL_RESEARCH_V1.query)
        or value.result.language != SEARXNG_OFFICIAL_RESEARCH_V1.language
    ):
        raise AutonomyContractError("search result is not bound to the task and official intent")


def _complete_search_step(
    checkpoint: AutonomyCheckpoint,
    value: object,
    binding: AutonomyBinding,
) -> AutonomyCheckpoint:
    _validate_search(value, binding)
    assert isinstance(value, SearchOrchestratorOutcome)
    return replace(
        checkpoint,
        completed_steps=(*checkpoint.completed_steps, AutonomyStep.SEARCH),
        search=value,
        post_authorization_pending=AutonomyStep.SEARCH,
    )


def _complete_browser_step(
    checkpoint: AutonomyCheckpoint,
    value: object,
    request: BrowserResearchRequest,
    binding: AutonomyBinding,
    plan_digest: str,
) -> AutonomyCheckpoint:
    _validate_browser_result(value, binding, plan_digest)
    assert isinstance(value, BrowserResearchResult)
    if tuple(page.url for page in value.pages) != request.urls:
        raise AutonomyContractError("browser result is not bound to requested official URLs")
    return replace(
        checkpoint,
        completed_steps=(*checkpoint.completed_steps, AutonomyStep.BROWSER_INSPECT),
        browser=value,
        post_authorization_pending=AutonomyStep.BROWSER_INSPECT,
    )


def _validate_browser_result(
    value: object,
    binding: AutonomyBinding,
    plan_digest: str,
) -> None:
    if not isinstance(value, BrowserResearchResult):
        raise AutonomyContractError("browser port returned an invalid result")
    for page in value.pages:
        try:
            page.screenshot.require_current(binding, plan_digest)
        except (AutonomyCheckpointError, TypeError, ValueError) as exc:
            raise AutonomyContractError("browser screenshot scope is invalid") from exc
        if page.screenshot.artifact.kind is not ArtifactKind.SCREENSHOT:
            raise AutonomyContractError("browser screenshot kind is invalid")


def _official_urls(search: SearchOrchestratorOutcome, allowed_hosts: tuple[str, ...]) -> tuple[str, ...]:
    urls: list[str] = []
    for evidence in search.result.evidence:
        url = evidence.source.url
        host = (urlsplit(url).hostname or "").lower().removesuffix(".")
        if evidence.source_class is SearchSourceClass.PRIMARY_OFFICIAL and _host_allowed(host, allowed_hosts):
            urls.append(url)
        if len(urls) >= _MAX_OFFICIAL_PAGES:
            break
    if not urls:
        raise AutonomyContractError("search returned no official SearXNG documentation source")
    return tuple(urls)


def _complete_artifact_step(
    checkpoint: AutonomyCheckpoint,
    value: object,
    binding: AutonomyBinding,
    plan_digest: str,
) -> AutonomyCheckpoint:
    _validate_comparison_artifact(value, binding, plan_digest)
    assert isinstance(value, ScopedAutonomyArtifact)
    return replace(
        checkpoint,
        completed_steps=(*checkpoint.completed_steps, AutonomyStep.COMPARISON_ARTIFACT),
        artifact=value,
        post_authorization_pending=AutonomyStep.COMPARISON_ARTIFACT,
    )


def _validate_comparison_artifact(
    value: object,
    binding: AutonomyBinding,
    plan_digest: str,
) -> None:
    if not isinstance(value, ScopedAutonomyArtifact):
        raise AutonomyContractError("artifact port returned an invalid result")
    try:
        value.require_current(binding, plan_digest)
    except (AutonomyCheckpointError, TypeError, ValueError) as exc:
        raise AutonomyContractError("comparison artifact scope is invalid") from exc
    if value.artifact.kind is not ArtifactKind.DOCUMENT or value.artifact.media_type not in {
        "text/csv",
        "text/markdown",
        "text/markdown; charset=utf-8",
    }:
        raise AutonomyContractError("comparison table must be an opaque document ArtifactRef")


def _with_attempt(
    checkpoint: AutonomyCheckpoint,
    step: AutonomyStep,
    attempts: int,
    failure_code: str | None,
) -> AutonomyCheckpoint:
    execution_by_step = {item.step: item for item in checkpoint.execution_attempts}
    previous_execution = execution_by_step.get(step, StepAttempt(step, 0))
    if attempts < previous_execution.attempts:
        raise ValueError("execution attempt count cannot decrease")
    totals_by_step = {item.step: item for item in checkpoint.attempts}
    previous_total = totals_by_step.get(step, StepAttempt(step, 0))
    total = previous_total.attempts + (attempts - previous_execution.attempts)
    totals_by_step[step] = StepAttempt(step, total, failure_code)
    execution_by_step[step] = StepAttempt(step, attempts, failure_code)
    ordered_totals = tuple(
        totals_by_step[item] for item in SEARXNG_OFFICIAL_RESEARCH_V1.steps if item in totals_by_step
    )
    ordered_execution = tuple(
        execution_by_step[item] for item in SEARXNG_OFFICIAL_RESEARCH_V1.steps if item in execution_by_step
    )
    return replace(
        checkpoint,
        attempts=ordered_totals,
        execution_attempts=ordered_execution,
    )


def _attempt_count(checkpoint: AutonomyCheckpoint, step: AutonomyStep) -> int:
    return next((item.attempts for item in checkpoint.execution_attempts if item.step is step), 0)


def _receipt(
    binding: AutonomyBinding,
    checkpoint: AutonomyCheckpoint,
    template: AutonomyTaskTemplate,
) -> AutonomyPlanReceipt:
    attempts = {item.step: item for item in checkpoint.attempts}
    return AutonomyPlanReceipt(
        task_id=binding.task_id,
        template_id=template.template_id,
        template_version=template.version,
        plan_digest=template.digest,
        binding_digest=binding.digest,
        status=checkpoint.status,
        steps=tuple(
            AutonomyStepReceipt(
                step=step,
                completed=step in checkpoint.completed_steps,
                attempts=attempts.get(step, StepAttempt(step, 0)).attempts,
                failure_code=attempts.get(step, StepAttempt(step, 0)).last_failure_code,
            )
            for step in template.steps
        ),
        artifact_id=(
            checkpoint.artifact.artifact.artifact_id
            if checkpoint.status is AutonomyPlanStatus.COMPLETED and checkpoint.artifact is not None
            else None
        ),
        failure_code=checkpoint.failure_code,
    )


def _deliverable_artifact(checkpoint: AutonomyCheckpoint) -> ScopedAutonomyArtifact | None:
    if checkpoint.status is not AutonomyPlanStatus.COMPLETED:
        return None
    return checkpoint.artifact


def _hydrate_durable_checkpoint(
    durable: DurableAutonomyCheckpoint,
    binding: AutonomyBinding,
    template: AutonomyTaskTemplate,
) -> AutonomyCheckpoint:
    if (
        durable.template_id != template.template_id
        or durable.template_version != template.version
        or durable.plan_digest != template.digest
        or durable.binding_digest != binding.digest
    ):
        raise AutonomyCheckpointError("durable checkpoint is not bound to the current task and plan")
    if durable.status is AutonomyPlanStatus.RUNNING:
        if durable.artifact is not None:
            artifact = ScopedAutonomyArtifact.bind(
                durable.artifact,
                binding=binding,
                plan_digest=durable.plan_digest,
            )
            return AutonomyCheckpoint(
                template_id=durable.template_id,
                template_version=durable.template_version,
                plan_digest=durable.plan_digest,
                binding_digest=durable.binding_digest,
                status=durable.status,
                completed_steps=durable.completed_steps,
                attempts=durable.attempts,
                execution_attempts=durable.execution_attempts,
                artifact=artifact,
                post_authorization_pending=AutonomyStep.COMPARISON_ARTIFACT,
                content_compacted=True,
            )
        # Search results and page bodies are deliberately not durable. Resume
        # recomputes the fixed read-only prefix. Successful transient steps do
        # not consume the retry budget of that recomputation; an in-flight or
        # failed boundary step keeps its consumed attempts.
        completed = set(durable.completed_steps)
        return AutonomyCheckpoint(
            template_id=durable.template_id,
            template_version=durable.template_version,
            plan_digest=durable.plan_digest,
            binding_digest=durable.binding_digest,
            status=durable.status,
            attempts=durable.attempts,
            execution_attempts=tuple(item for item in durable.execution_attempts if item.step not in completed),
        )
    artifact = (
        None
        if durable.artifact is None
        else ScopedAutonomyArtifact.bind(
            durable.artifact,
            binding=binding,
            plan_digest=durable.plan_digest,
        )
    )
    return AutonomyCheckpoint(
        template_id=durable.template_id,
        template_version=durable.template_version,
        plan_digest=durable.plan_digest,
        binding_digest=durable.binding_digest,
        status=durable.status,
        completed_steps=durable.completed_steps,
        attempts=durable.attempts,
        execution_attempts=durable.execution_attempts,
        artifact=artifact,
        failure_code=durable.failure_code,
        terminal_emitted=durable.terminal_emitted,
        content_compacted=True,
    )


def _terminal_notice(
    binding: AutonomyBinding,
    checkpoint: AutonomyCheckpoint,
    template: AutonomyTaskTemplate,
) -> AutonomyTerminalNotice:
    kind = (
        AutonomyTerminalKind.FINAL if checkpoint.status is AutonomyPlanStatus.COMPLETED else AutonomyTerminalKind.ERROR
    )
    return AutonomyTerminalNotice(
        idempotency_key=_sha256(
            {
                "task_id": binding.task_id,
                "binding_digest": binding.digest,
                "plan_digest": template.digest,
            }
        ),
        kind=kind,
        receipt=_receipt(binding, checkpoint, template),
    )


def _require_exact_artifact(value: object) -> None:
    if not isinstance(value, ArtifactRef):
        raise TypeError("artifact must be an ArtifactRef")
    if (
        isinstance(value.size_bytes, bool)
        or not isinstance(value.size_bytes, int)
        or value.size_bytes <= 0
        or not isinstance(value.sha256, str)
        or _DIGEST.fullmatch(value.sha256) is None
    ):
        raise ValueError("artifact requires exact positive size and sha256")


def _failure_code(exc: BaseException) -> str:
    name = type(exc).__name__.casefold()
    normalized = re.sub(r"[^a-z0-9._:-]+", "_", name).strip("_")
    return (normalized or "port_failure")[:128]


def _require_official_url(url: object, allowed_hosts: tuple[str, ...]) -> None:
    if not isinstance(url, str):
        raise TypeError("official URL must be a string")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().removesuffix(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not _host_allowed(host, allowed_hosts)
        or parsed.fragment
    ):
        raise ValueError("URL is outside the official SearXNG documentation boundary")


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AutonomyAuthorizationError",
    "AutonomyBinding",
    "AutonomyCheckpoint",
    "AutonomyCheckpointError",
    "AutonomyCheckpointPort",
    "AutonomyContractError",
    "AutonomyError",
    "AutonomyOutcome",
    "AutonomyPlanReceipt",
    "AutonomyPlanStatus",
    "AutonomyPolicy",
    "AutonomySearchPort",
    "AutonomyStep",
    "AutonomyStepReceipt",
    "AutonomyTaskTemplate",
    "AutonomyTerminalKind",
    "AutonomyTerminalNotice",
    "AutonomyTerminalPort",
    "BoundedAutonomyService",
    "BrowserResearchRequest",
    "BrowserResearchResult",
    "ComparisonArtifactPort",
    "ComparisonTableRequest",
    "DurableAutonomyCheckpoint",
    "OfficialBrowserPort",
    "OfficialPageInspection",
    "SEARXNG_OFFICIAL_RESEARCH_V1",
    "ScopedAutonomyArtifact",
    "StepAttempt",
]
