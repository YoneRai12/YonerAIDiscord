from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import urlsplit

from yonerai_discord.browser_sandbox.models import (
    BrowserAction,
    BrowserOutput,
    BrowserOutputKind,
    BrowserSessionRequest,
    BrowserSessionResult,
    Click,
    ExtractText,
    Navigate,
    Screenshot,
    Scroll,
    SelectOption,
    TypeText,
    Wait,
)
from yonerai_discord.browser_sandbox.policy import BrowserSandboxPolicy
from yonerai_discord.browser_sandbox.worker_contract import browser_policy_snapshot_digest
from yonerai_discord.browser_sandbox.worker_session import (
    BrowserWorkerSession,
    BrowserWorkerTransport,
)

from .search import WebBackendAvailability, WebBackendBlockerCode, WebBackendUnavailableError


_WORKER_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")


class BrowserRunError(RuntimeError):
    """Bounded browser plan failed without widening to PC or shell control."""


class BrowserBackendUnavailableError(WebBackendUnavailableError, BrowserRunError):
    """No external browser worker can currently satisfy the existing protocol."""


class BrowserRunContractError(BrowserRunError):
    """The plan, checkpoint, worker, or artifact response violated the contract."""


class BrowserRunCancelledError(BrowserRunError):
    """An explicit cancellation signal stopped the plan after worker cleanup."""


class BrowserRunTimeoutError(BrowserRunError):
    """The total plan deadline expired after worker cleanup."""


class BrowserRunCleanupUnconfirmedError(BrowserRunError):
    """The plan stopped but external worker cleanup could not be confirmed."""


class BrowserRunTransientError(BrowserRunError):
    """A bounded external browser operation failed transiently after cleanup."""


@dataclass(frozen=True, slots=True)
class BrowserRunScope:
    request_id: str
    guild_id: int
    channel_id: int
    user_id: int

    def __post_init__(self) -> None:
        _require_identifier(self.request_id, "request_id")
        for label, value in (
            ("guild_id", self.guild_id),
            ("channel_id", self.channel_id),
            ("user_id", self.user_id),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            {
                "channel_id": self.channel_id,
                "guild_id": self.guild_id,
                "request_id": self.request_id,
                "user_id": self.user_id,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AllowedOriginPolicy:
    """Exact navigation origins; subresources remain governed by BrowserSandboxPolicy."""

    origins: tuple[str, ...]

    def __post_init__(self) -> None:
        origins = tuple(_normalize_origin(origin) for origin in self.origins)
        if not origins or len(origins) > 20 or len(set(origins)) != len(origins):
            raise ValueError("origins must contain 1 to 20 unique exact origins")
        object.__setattr__(self, "origins", origins)

    def authorize_actions(self, actions: tuple[BrowserAction, ...]) -> None:
        for action in actions:
            if isinstance(action, Navigate) and _origin(action.url) not in self.origins:
                raise BrowserRunContractError("navigation origin is not allowlisted")

    @property
    def digest(self) -> str:
        encoded = json.dumps(sorted(self.origins), separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BrowserPlanSegment:
    """A self-contained resume unit. Every segment must re-establish state with Navigate."""

    segment_id: str
    actions: tuple[BrowserAction, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.segment_id, "segment_id")
        try:
            actions = tuple(self.actions)
        except TypeError as exc:
            raise TypeError("actions must be iterable") from exc
        if not actions or not isinstance(actions[0], Navigate):
            raise ValueError("every resumable browser segment must start with Navigate")
        BrowserSessionRequest(actions)
        object.__setattr__(self, "actions", actions)


@dataclass(frozen=True, slots=True)
class BrowserRunPlan:
    plan_id: str
    scope: BrowserRunScope
    segments: tuple[BrowserPlanSegment, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.plan_id, "plan_id")
        if not isinstance(self.scope, BrowserRunScope):
            raise TypeError("scope must be a BrowserRunScope")
        try:
            segments = tuple(self.segments)
        except TypeError as exc:
            raise TypeError("segments must be iterable") from exc
        if not segments or len(segments) > 20 or any(not isinstance(item, BrowserPlanSegment) for item in segments):
            raise ValueError("segments must contain 1 to 20 BrowserPlanSegment values")
        if len({segment.segment_id for segment in segments}) != len(segments):
            raise ValueError("segment_id values must be unique")
        object.__setattr__(self, "segments", segments)

    @property
    def step_count(self) -> int:
        return sum(len(segment.actions) for segment in self.segments)

    @property
    def digest(self) -> str:
        document = {
            "plan_id": self.plan_id,
            "scope_digest": self.scope.digest,
            "segments": [
                {
                    "segment_id": segment.segment_id,
                    "actions": [_action_document(action) for action in segment.actions],
                }
                for segment in self.segments
            ],
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BrowserArtifactRef:
    artifact_id: str
    scope: BrowserRunScope
    global_step_index: int
    media_type: str
    byte_length: int
    sha256: str
    source_origin: str

    def __post_init__(self) -> None:
        _require_identifier(self.artifact_id, "artifact_id")
        if not isinstance(self.scope, BrowserRunScope):
            raise TypeError("scope must be a BrowserRunScope")
        if (
            isinstance(self.global_step_index, bool)
            or not isinstance(self.global_step_index, int)
            or self.global_step_index < 0
        ):
            raise ValueError("global_step_index must be a non-negative integer")
        if self.media_type not in {"image/png", "image/jpeg"}:
            raise ValueError("artifact media_type is invalid")
        if isinstance(self.byte_length, bool) or not isinstance(self.byte_length, int) or self.byte_length < 1:
            raise ValueError("artifact byte_length must be positive")
        if len(self.sha256) != 64 or any(character not in "0123456789abcdef" for character in self.sha256):
            raise ValueError("artifact sha256 is invalid")
        object.__setattr__(self, "source_origin", _normalize_origin(self.source_origin))


@dataclass(frozen=True, slots=True)
class BrowserScreenshotArtifact:
    reference: BrowserArtifactRef
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.reference, BrowserArtifactRef):
            raise TypeError("reference must be a BrowserArtifactRef")
        if type(self.data) is not bytes or len(self.data) != self.reference.byte_length:
            raise ValueError("artifact bytes do not match the reference")
        if hashlib.sha256(self.data).hexdigest() != self.reference.sha256:
            raise ValueError("artifact digest does not match the reference")


@dataclass(frozen=True, slots=True)
class BrowserCheckpoint:
    plan_id: str
    scope: BrowserRunScope
    plan_digest: str
    policy_digest: str
    origin_policy_digest: str
    next_segment_index: int
    artifacts: tuple[BrowserArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.plan_id, "plan_id")
        if not isinstance(self.scope, BrowserRunScope):
            raise TypeError("scope must be a BrowserRunScope")
        for label, value in (
            ("plan_digest", self.plan_digest),
            ("policy_digest", self.policy_digest),
            ("origin_policy_digest", self.origin_policy_digest),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{label} is invalid")
        if (
            isinstance(self.next_segment_index, bool)
            or not isinstance(self.next_segment_index, int)
            or self.next_segment_index < 0
        ):
            raise ValueError("next_segment_index must be a non-negative integer")
        artifacts = tuple(self.artifacts)
        if any(not isinstance(item, BrowserArtifactRef) for item in artifacts):
            raise TypeError("artifacts must contain BrowserArtifactRef values")
        if len({item.artifact_id for item in artifacts}) != len(artifacts):
            raise ValueError("checkpoint artifact IDs must be unique")
        if len({item.global_step_index for item in artifacts}) != len(artifacts):
            raise ValueError("checkpoint artifact steps must be unique")
        if any(item.scope != self.scope for item in artifacts):
            raise ValueError("checkpoint artifact scope does not match the checkpoint")
        object.__setattr__(self, "artifacts", artifacts)


@dataclass(frozen=True, slots=True)
class BrowserStepOutput:
    global_step_index: int
    output: BrowserOutput

    def __post_init__(self) -> None:
        if (
            isinstance(self.global_step_index, bool)
            or not isinstance(self.global_step_index, int)
            or self.global_step_index < 0
        ):
            raise ValueError("global_step_index must be a non-negative integer")
        if not isinstance(self.output, BrowserOutput):
            raise TypeError("output must be a BrowserOutput")


@dataclass(frozen=True, slots=True)
class BrowserRunResult:
    checkpoint: BrowserCheckpoint
    outputs: tuple[BrowserStepOutput, ...]
    screenshot_artifacts: tuple[BrowserScreenshotArtifact, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoint, BrowserCheckpoint):
            raise TypeError("checkpoint must be a BrowserCheckpoint")
        outputs = tuple(self.outputs)
        artifacts = tuple(self.screenshot_artifacts)
        if any(not isinstance(item, BrowserStepOutput) for item in outputs):
            raise TypeError("outputs must contain BrowserStepOutput values")
        if any(not isinstance(item, BrowserScreenshotArtifact) for item in artifacts):
            raise TypeError("screenshot_artifacts must contain BrowserScreenshotArtifact values")
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "screenshot_artifacts", artifacts)


@dataclass(frozen=True, slots=True)
class BrowserRunLimits:
    max_steps: int = 40
    max_segments: int = 10
    total_timeout_seconds: float = 45.0
    max_artifacts: int = 20

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int) or not 1 <= self.max_steps <= 200:
            raise ValueError("max_steps is outside the allowed range")
        if (
            isinstance(self.max_segments, bool)
            or not isinstance(self.max_segments, int)
            or not 1 <= self.max_segments <= 20
        ):
            raise ValueError("max_segments is outside the allowed range")
        if (
            isinstance(self.total_timeout_seconds, bool)
            or not isinstance(self.total_timeout_seconds, (int, float))
            or not 1.0 <= float(self.total_timeout_seconds) <= 300.0
        ):
            raise ValueError("total_timeout_seconds is outside the allowed range")
        if (
            isinstance(self.max_artifacts, bool)
            or not isinstance(self.max_artifacts, int)
            or not 1 <= self.max_artifacts <= 100
        ):
            raise ValueError("max_artifacts is outside the allowed range")


class BrowserCancellation:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class BrowserCheckpointStore(Protocol):
    async def save(self, checkpoint: BrowserCheckpoint) -> None: ...


class BrowserSessionPort(Protocol):
    @property
    def cleanup_confirmed(self) -> bool: ...

    async def execute(self, request: BrowserSessionRequest) -> BrowserSessionResult: ...


class BrowserSessionFactory(Protocol):
    @property
    def configured(self) -> bool: ...

    def create(self, *, request_id: str, policy: BrowserSandboxPolicy) -> BrowserSessionPort: ...


BrowserTransportFactory = Callable[[str, BrowserSandboxPolicy], BrowserWorkerTransport]


class ExistingWorkerSessionFactory:
    """Creates one existing BrowserWorkerSession per independently resumable segment."""

    def __init__(
        self,
        transport_factory: BrowserTransportFactory | None = None,
        *,
        cleanup_timeout_seconds: float = 1.0,
    ) -> None:
        if transport_factory is not None and not callable(transport_factory):
            raise TypeError("transport_factory must be callable")
        if (
            isinstance(cleanup_timeout_seconds, bool)
            or not isinstance(cleanup_timeout_seconds, (int, float))
            or not 0.1 <= float(cleanup_timeout_seconds) <= 5.0
        ):
            raise ValueError("cleanup_timeout_seconds is outside the allowed range")
        self._transport_factory = transport_factory
        self._cleanup_timeout_seconds = float(cleanup_timeout_seconds)

    @property
    def configured(self) -> bool:
        return self._transport_factory is not None

    def create(self, *, request_id: str, policy: BrowserSandboxPolicy) -> BrowserWorkerSession:
        if self._transport_factory is None:
            raise BrowserBackendUnavailableError(
                WebBackendBlockerCode.UNCONFIGURED,
                "Browser worker transport is not configured",
            )
        transport = self._transport_factory(request_id, policy)
        return BrowserWorkerSession(
            request_id=request_id,
            policy=policy,
            transport=transport,
            cleanup_timeout_seconds=self._cleanup_timeout_seconds,
        )


def playwright_worker_availability(*, worker_transport_configured: bool) -> WebBackendAvailability:
    """Honest environment probe only; it never launches or installs Chromium."""

    if type(worker_transport_configured) is not bool:
        raise TypeError("worker_transport_configured must be a boolean")
    try:
        playwright_spec = importlib.util.find_spec("playwright.async_api")
    except ModuleNotFoundError:
        playwright_spec = None
    if playwright_spec is None:
        return WebBackendAvailability(
            False,
            WebBackendBlockerCode.DEPENDENCY_MISSING,
            "Python Playwright dependency is not installed",
        )
    if not worker_transport_configured:
        return WebBackendAvailability(
            False,
            WebBackendBlockerCode.UNCONFIGURED,
            "Playwright worker transport and Chromium executable are not configured",
        )
    return WebBackendAvailability(
        True,
        None,
        "Playwright package and external worker transport are configured; live launch is still unverified",
    )


class BoundedBrowserRunner:
    """Checkpointed adapter over the existing one-shot browser worker protocol."""

    def __init__(
        self,
        *,
        policy: BrowserSandboxPolicy,
        origin_policy: AllowedOriginPolicy,
        session_factory: BrowserSessionFactory | None = None,
        checkpoint_store: BrowserCheckpointStore | None = None,
        limits: BrowserRunLimits = BrowserRunLimits(),
        enabled: bool = False,
    ) -> None:
        if not isinstance(policy, BrowserSandboxPolicy):
            raise TypeError("policy must be a BrowserSandboxPolicy")
        if not isinstance(origin_policy, AllowedOriginPolicy):
            raise TypeError("origin_policy must be an AllowedOriginPolicy")
        if session_factory is not None and not callable(getattr(session_factory, "create", None)):
            raise TypeError("session_factory must implement create")
        if checkpoint_store is not None and not callable(getattr(checkpoint_store, "save", None)):
            raise TypeError("checkpoint_store must implement save")
        if not isinstance(limits, BrowserRunLimits):
            raise TypeError("limits must be BrowserRunLimits")
        if type(enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        self._policy = policy
        self._origin_policy = origin_policy
        self._session_factory = session_factory
        self._checkpoint_store = checkpoint_store
        self._limits = limits
        self._enabled = enabled

    @property
    def availability(self) -> WebBackendAvailability:
        if not self._enabled:
            return WebBackendAvailability(False, WebBackendBlockerCode.DISABLED, "Browser runner is disabled")
        if self._session_factory is None or getattr(self._session_factory, "configured", True) is not True:
            return WebBackendAvailability(
                False,
                WebBackendBlockerCode.UNCONFIGURED,
                "Browser worker session factory or external transport is not configured",
            )
        return WebBackendAvailability(True, None, "External browser worker session factory is configured")

    async def run(
        self,
        plan: BrowserRunPlan,
        *,
        checkpoint: BrowserCheckpoint | None = None,
        cancellation: BrowserCancellation | None = None,
    ) -> BrowserRunResult:
        if not isinstance(plan, BrowserRunPlan):
            raise TypeError("plan must be a BrowserRunPlan")
        if cancellation is not None and not isinstance(cancellation, BrowserCancellation):
            raise TypeError("cancellation must be a BrowserCancellation or None")
        self._require_available()
        self._validate_plan(plan)
        current = self._initial_or_resumed_checkpoint(plan, checkpoint)
        outputs: list[BrowserStepOutput] = []
        screenshot_artifacts: list[BrowserScreenshotArtifact] = []
        active_session: BrowserSessionPort | None = None
        try:
            async with asyncio.timeout(float(self._limits.total_timeout_seconds)):
                for segment_index in range(current.next_segment_index, len(plan.segments)):
                    if cancellation is not None and cancellation.cancelled:
                        raise BrowserRunCancelledError("browser plan was cancelled")
                    segment = plan.segments[segment_index]
                    request = BrowserSessionRequest(segment.actions)
                    self._policy.validate_session(request)
                    self._origin_policy.authorize_actions(segment.actions)
                    assert self._session_factory is not None
                    active_session = self._session_factory.create(
                        request_id=_worker_request_id(plan, segment),
                        policy=self._policy,
                    )
                    result = await _execute_with_cancellation(active_session, request, cancellation)
                    if not active_session.cleanup_confirmed:
                        raise BrowserRunCleanupUnconfirmedError("browser worker cleanup was not confirmed")
                    active_session = None
                    segment_outputs, segment_artifacts = _bind_outputs(
                        plan=plan,
                        segment_index=segment_index,
                        segment=segment,
                        result=result,
                    )
                    outputs.extend(segment_outputs)
                    screenshot_artifacts.extend(segment_artifacts)
                    artifact_refs = current.artifacts + tuple(item.reference for item in segment_artifacts)
                    if len(artifact_refs) > self._limits.max_artifacts:
                        raise BrowserRunContractError("browser artifact budget exceeded")
                    current = BrowserCheckpoint(
                        plan_id=plan.plan_id,
                        scope=plan.scope,
                        plan_digest=plan.digest,
                        policy_digest=browser_policy_snapshot_digest(self._policy),
                        origin_policy_digest=self._origin_policy.digest,
                        next_segment_index=segment_index + 1,
                        artifacts=artifact_refs,
                    )
                    if self._checkpoint_store is not None:
                        await self._checkpoint_store.save(current)
                        # Interactive delivery may retain bounded PNG evidence in
                        # process memory beside this opaque checkpoint.  The
                        # generic checkpoint contract stays ref-only; stores that
                        # do not implement this narrow optional hook continue to
                        # receive only ``save``.
                        save_artifacts = getattr(self._checkpoint_store, "save_artifacts", None)
                        if callable(save_artifacts):
                            await save_artifacts(current, tuple(screenshot_artifacts))
        except asyncio.CancelledError:
            raise
        except BrowserRunCancelledError:
            raise
        except TimeoutError as exc:
            if active_session is not None and active_session.cleanup_confirmed is not True:
                raise BrowserRunCleanupUnconfirmedError(
                    "browser worker cleanup after timeout was not confirmed"
                ) from exc
            raise BrowserRunTimeoutError("browser plan exceeded its total timeout") from exc
        return BrowserRunResult(
            checkpoint=current,
            outputs=tuple(outputs),
            screenshot_artifacts=tuple(screenshot_artifacts),
        )

    def _require_available(self) -> None:
        availability = self.availability
        if not availability.available:
            assert availability.blocker is not None
            raise BrowserBackendUnavailableError(availability.blocker, availability.detail)

    def _validate_plan(self, plan: BrowserRunPlan) -> None:
        if len(plan.segments) > self._limits.max_segments or plan.step_count > self._limits.max_steps:
            raise BrowserRunContractError("browser plan exceeds the configured step or segment limit")
        if plan.step_count > self._policy.limits.max_steps:
            raise BrowserRunContractError("browser plan exceeds the sandbox step limit")
        screenshot_count = sum(
            isinstance(action, Screenshot) for segment in plan.segments for action in segment.actions
        )
        if screenshot_count > self._limits.max_artifacts:
            raise BrowserRunContractError("browser plan exceeds the artifact limit")
        for segment in plan.segments:
            self._policy.validate_session(BrowserSessionRequest(segment.actions))
            self._origin_policy.authorize_actions(segment.actions)

    def _initial_or_resumed_checkpoint(
        self,
        plan: BrowserRunPlan,
        checkpoint: BrowserCheckpoint | None,
    ) -> BrowserCheckpoint:
        policy_digest = browser_policy_snapshot_digest(self._policy)
        if checkpoint is None:
            return BrowserCheckpoint(
                plan_id=plan.plan_id,
                scope=plan.scope,
                plan_digest=plan.digest,
                policy_digest=policy_digest,
                origin_policy_digest=self._origin_policy.digest,
                next_segment_index=0,
            )
        if not isinstance(checkpoint, BrowserCheckpoint):
            raise TypeError("checkpoint must be a BrowserCheckpoint or None")
        if (
            checkpoint.plan_id != plan.plan_id
            or checkpoint.scope != plan.scope
            or checkpoint.plan_digest != plan.digest
            or checkpoint.policy_digest != policy_digest
            or checkpoint.origin_policy_digest != self._origin_policy.digest
            or checkpoint.next_segment_index > len(plan.segments)
            or len(checkpoint.artifacts) > self._limits.max_artifacts
        ):
            raise BrowserRunContractError("checkpoint does not match the current plan and policies")
        completed_actions = [
            action for segment in plan.segments[: checkpoint.next_segment_index] for action in segment.actions
        ]
        expected_artifact_steps = {
            index for index, action in enumerate(completed_actions) if isinstance(action, Screenshot)
        }
        if {artifact.global_step_index for artifact in checkpoint.artifacts} != expected_artifact_steps:
            raise BrowserRunContractError("checkpoint artifacts do not exactly match completed screenshots")
        for artifact in checkpoint.artifacts:
            if artifact.global_step_index >= len(completed_actions) or not isinstance(
                completed_actions[artifact.global_step_index], Screenshot
            ):
                raise BrowserRunContractError("checkpoint artifact does not reference a completed screenshot")
            if artifact.scope != plan.scope:
                raise BrowserRunContractError("checkpoint artifact scope does not match the plan")
            action = completed_actions[artifact.global_step_index]
            assert isinstance(action, Screenshot)
            expected_media_type = "image/png" if action.image_format.value == "png" else "image/jpeg"
            if (
                artifact.media_type != expected_media_type
                or artifact.source_origin != _declared_origin_for_step(plan, artifact.global_step_index)
                or artifact.artifact_id != _artifact_id(plan.digest, artifact.global_step_index, artifact.sha256)
            ):
                raise BrowserRunContractError("checkpoint artifact binding is invalid")
        return checkpoint


async def _execute_with_cancellation(
    session: BrowserSessionPort,
    request: BrowserSessionRequest,
    cancellation: BrowserCancellation | None,
) -> BrowserSessionResult:
    if cancellation is None:
        return await session.execute(request)
    execution = asyncio.create_task(session.execute(request))
    cancellation_wait = asyncio.create_task(cancellation.wait())
    try:
        done, _pending = await asyncio.wait(
            {execution, cancellation_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_wait in done:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            if session.cleanup_confirmed is not True:
                raise BrowserRunCleanupUnconfirmedError("browser worker cleanup after cancellation was not confirmed")
            raise BrowserRunCancelledError("browser plan was cancelled")
        cancellation_wait.cancel()
        await asyncio.gather(cancellation_wait, return_exceptions=True)
        return await execution
    except asyncio.CancelledError:
        execution.cancel()
        cancellation_wait.cancel()
        await asyncio.gather(execution, cancellation_wait, return_exceptions=True)
        raise
    finally:
        if not execution.done():
            execution.cancel()
        if not cancellation_wait.done():
            cancellation_wait.cancel()


def _bind_outputs(
    *,
    plan: BrowserRunPlan,
    segment_index: int,
    segment: BrowserPlanSegment,
    result: BrowserSessionResult,
) -> tuple[list[BrowserStepOutput], list[BrowserScreenshotArtifact]]:
    if not isinstance(result, BrowserSessionResult):
        raise BrowserRunContractError("browser session returned an invalid result")
    step_offset = sum(len(item.actions) for item in plan.segments[:segment_index])
    outputs: list[BrowserStepOutput] = []
    artifacts: list[BrowserScreenshotArtifact] = []
    expected: dict[int, BrowserOutputKind] = {}
    for step_index, action in enumerate(segment.actions):
        if isinstance(action, Screenshot):
            expected[step_index] = BrowserOutputKind.SCREENSHOT
        elif isinstance(action, ExtractText):
            expected[step_index] = BrowserOutputKind.TEXT
    actual: dict[int, BrowserOutputKind] = {}
    for output in result.outputs:
        if output.step_index >= len(segment.actions):
            raise BrowserRunContractError("browser output references an unknown segment step")
        if output.step_index in actual:
            raise BrowserRunContractError("browser output duplicates one segment step")
        actual[output.step_index] = output.kind
        global_step_index = step_offset + output.step_index
        outputs.append(BrowserStepOutput(global_step_index=global_step_index, output=output))
        if output.kind is BrowserOutputKind.SCREENSHOT:
            sha256 = hashlib.sha256(output.data).hexdigest()
            reference = BrowserArtifactRef(
                artifact_id=_artifact_id(plan.digest, global_step_index, sha256),
                scope=plan.scope,
                global_step_index=global_step_index,
                media_type=output.media_type,
                byte_length=len(output.data),
                sha256=sha256,
                source_origin=_declared_origin_for_step(plan, global_step_index),
            )
            artifacts.append(BrowserScreenshotArtifact(reference=reference, data=output.data))
    if actual != expected or list(actual) != list(expected):
        raise BrowserRunContractError("browser outputs do not exactly match the requested actions")
    return outputs, artifacts


def _worker_request_id(plan: BrowserRunPlan, segment: BrowserPlanSegment) -> str:
    value = f"{plan.scope.digest}\0{plan.digest}\0{segment.segment_id}".encode()
    return f"browser-{hashlib.sha256(value).hexdigest()[:32]}"


def _artifact_id(plan_digest: str, global_step_index: int, sha256: str) -> str:
    value = f"{plan_digest}:{global_step_index}:{sha256}".encode()
    return f"shot-{hashlib.sha256(value).hexdigest()[:32]}"


def _declared_origin_for_step(plan: BrowserRunPlan, global_step_index: int) -> str:
    actions = [action for segment in plan.segments for action in segment.actions]
    if global_step_index >= len(actions):
        raise BrowserRunContractError("browser artifact references an unknown plan step")
    current_origin: str | None = None
    for action in actions[: global_step_index + 1]:
        if isinstance(action, Navigate):
            current_origin = _origin(action.url)
    if current_origin is None:
        raise BrowserRunContractError("browser screenshot has no declared navigation origin")
    return current_origin


def _action_document(action: BrowserAction) -> dict[str, object]:
    if isinstance(action, Navigate):
        return {"type": "navigate", "url": action.url}
    if isinstance(action, Click):
        return {"type": "click", "selector": action.selector.value}
    if isinstance(action, TypeText):
        return {
            "type": "type_text",
            "selector": action.selector.value,
            "text_sha256": hashlib.sha256(action.text.encode("utf-8")).hexdigest(),
            "clear_first": action.clear_first,
        }
    if isinstance(action, SelectOption):
        return {"type": "select_option", "selector": action.selector.value, "value": action.value}
    if isinstance(action, Scroll):
        return {"type": "scroll", "delta_x": action.delta_x, "delta_y": action.delta_y}
    if isinstance(action, Wait):
        return {"type": "wait", "milliseconds": action.milliseconds}
    if isinstance(action, Screenshot):
        return {"type": "screenshot", "full_page": action.full_page, "format": action.image_format.value}
    if isinstance(action, ExtractText):
        return {
            "type": "extract_text",
            "selector": None if action.selector is None else action.selector.value,
        }
    raise TypeError("unsupported browser action")


def _normalize_origin(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError("origin must be an exact credential-free HTTP(S) origin")
    default_port = 443 if parsed.scheme == "https" else 80
    rendered_port = "" if port in {None, default_port} else f":{port}"
    return f"{parsed.scheme}://{parsed.hostname.lower()}{rendered_port}"


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    rendered_port = "" if port in {None, default_port} else f":{port}"
    return f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}{rendered_port}"


def _require_identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or _WORKER_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")


__all__ = [
    "AllowedOriginPolicy",
    "BoundedBrowserRunner",
    "BrowserArtifactRef",
    "BrowserBackendUnavailableError",
    "BrowserCancellation",
    "BrowserCheckpoint",
    "BrowserCheckpointStore",
    "BrowserPlanSegment",
    "BrowserRunCancelledError",
    "BrowserRunCleanupUnconfirmedError",
    "BrowserRunContractError",
    "BrowserRunError",
    "BrowserRunLimits",
    "BrowserRunPlan",
    "BrowserRunResult",
    "BrowserRunScope",
    "BrowserRunTimeoutError",
    "BrowserRunTransientError",
    "BrowserScreenshotArtifact",
    "BrowserSessionFactory",
    "BrowserStepOutput",
    "ExistingWorkerSessionFactory",
    "playwright_worker_availability",
]
