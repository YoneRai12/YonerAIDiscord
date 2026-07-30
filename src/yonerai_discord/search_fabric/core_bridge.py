"""Strict v0.1 Core tool-continuation bridge for Search Fabric."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol

from yonerai_discord.execution_gateway.core_contract import (
    CORE_FACTS_EXTENSION,
    CoreCancelDispositionV01,
    CoreCancelOutcomeV01,
    DiscordCoreFacts,
    discord_core_conversation_id,
    project_core_message_v01,
)
from yonerai_discord.execution_gateway.core_files import (
    CoreArtifactOwnerScopeV01,
    CoreFilesContractError,
    core_ref_from_artifact_v01,
)
from yonerai_discord.execution_gateway.core_v01 import YonerAIInternalRunGatewayV01
from yonerai_discord.execution_gateway.local import IdempotencyConflictError
from yonerai_discord.execution_gateway.models import (
    ArtifactReference,
    CapabilityResult,
    RunEvent,
    RunInput,
)
from yonerai_discord.modules.web_runtime.search import WebSearchRequest

from .contracts import SearchIntent, query_digest, validate_language
from .receipts import SearchGatewayOutcome


SEARCH_EVIDENCE_CAPABILITY = "web.search.evidence"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_MAX_RUNS = 4_096
_MAX_SEARCH_ACTIONS_PER_RUN = 3


class CoreSearchBridgeError(RuntimeError):
    """Content-free bridge failure."""


class SearchEvidencePort(Protocol):
    async def search(
        self,
        request: WebSearchRequest,
        *,
        request_id: str,
        intent: SearchIntent,
        language: str,
    ) -> SearchGatewayOutcome: ...


@dataclass(frozen=True, slots=True)
class CoreSearchBinding:
    """Caller facts which the model and remote Core cannot override."""

    request_id: str
    tenant_id: str
    actor_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        for label, value in (
            ("request_id", self.request_id),
            ("tenant_id", self.tenant_id),
            ("actor_id", self.actor_id),
            ("conversation_id", self.conversation_id),
        ):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{label} is invalid")

    @classmethod
    def from_facts(cls, facts: DiscordCoreFacts) -> CoreSearchBinding:
        if not isinstance(facts, DiscordCoreFacts):
            raise TypeError("facts must be DiscordCoreFacts")
        tenant_id = f"discord:dm:{facts.channel_id}" if facts.guild_id is None else f"discord:guild:{facts.guild_id}"
        return cls(
            request_id=facts.request_id,
            tenant_id=tenant_id,
            actor_id=str(facts.user_id),
            conversation_id=discord_core_conversation_id(facts),
        )


@dataclass(frozen=True, slots=True)
class CoreSearchRunHandle:
    run_id: str
    idempotency_key: str
    binding_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or _IDENTIFIER.fullmatch(self.run_id) is None:
            raise ValueError("run_id is invalid")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key or len(self.idempotency_key) > 512:
            raise ValueError("idempotency_key is invalid")
        if (
            not isinstance(self.binding_digest, str)
            or re.fullmatch(r"sha256:[a-f0-9]{64}", self.binding_digest) is None
        ):
            raise ValueError("binding_digest is invalid")


@dataclass(frozen=True, slots=True)
class CoreSearchRunOutcome:
    handle: CoreSearchRunHandle
    events: tuple[RunEvent, ...]
    terminal_kind: str
    artifacts: tuple[ArtifactReference, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.handle, CoreSearchRunHandle):
            raise TypeError("handle must be a CoreSearchRunHandle")
        if (
            not isinstance(self.events, tuple)
            or not self.events
            or any(not isinstance(event, RunEvent) for event in self.events)
        ):
            raise ValueError("events must be a non-empty tuple of RunEvent values")
        terminals = tuple(event for event in self.events if event.terminal)
        if len(terminals) != 1 or terminals[0] is not self.events[-1] or self.terminal_kind != terminals[0].kind:
            raise ValueError("outcome must contain exactly one final terminal event")
        artifacts = tuple(self.artifacts)
        if any(not isinstance(artifact, ArtifactReference) for artifact in artifacts):
            raise TypeError("artifacts must contain ArtifactReference values")
        if self.terminal_kind == "error" and artifacts:
            raise ValueError("error outcome must not contain artifacts")
        object.__setattr__(self, "artifacts", artifacts)


@dataclass(slots=True)
class _RunState:
    handle: CoreSearchRunHandle
    binding: CoreSearchBinding
    request_fingerprint: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    outcome: CoreSearchRunOutcome | None = None
    cancel_outcome: CoreCancelOutcomeV01 | None = None
    generation: int = 0
    cancel_requested: bool = False
    closed: bool = False
    search_action_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _SearchActionArguments:
    query: str = field(repr=False)
    intent: SearchIntent
    language: str
    limit: int


class _ReplayGuard:
    """Fixed-memory, fail-closed guard for evicted idempotency keys."""

    def __init__(self, capacity: int) -> None:
        self._bit_count = max(8_192, capacity * 64)
        self._bits = bytearray((self._bit_count + 7) // 8)

    def add(self, value: str) -> None:
        for offset in self._offsets(value):
            self._bits[offset // 8] |= 1 << (offset % 8)

    def contains(self, value: str) -> bool:
        return all(self._bits[offset // 8] & (1 << (offset % 8)) for offset in self._offsets(value))

    def _offsets(self, value: str) -> tuple[int, int, int, int]:
        digest = hashlib.sha256(value.encode("utf-8", errors="strict")).digest()
        return (
            int.from_bytes(digest[0:4], "big") % self._bit_count,
            int.from_bytes(digest[4:8], "big") % self._bit_count,
            int.from_bytes(digest[8:12], "big") % self._bit_count,
            int.from_bytes(digest[12:16], "big") % self._bit_count,
        )


class CoreSearchToolBridge:
    """Executes only ``web.search.evidence`` through an existing v0.1 gateway."""

    def __init__(
        self,
        gateway: YonerAIInternalRunGatewayV01,
        search_port: SearchEvidencePort,
        *,
        max_runs: int = _MAX_RUNS,
    ) -> None:
        for method in ("start", "events", "submit_result", "cancel"):
            if not callable(getattr(gateway, method, None)):
                raise TypeError(f"gateway must expose a callable {method} method")
        if not callable(getattr(search_port, "search", None)):
            raise TypeError("search_port must expose a callable search method")
        if isinstance(max_runs, bool) or not isinstance(max_runs, int) or not 1 <= max_runs <= _MAX_RUNS:
            raise ValueError("max_runs is outside the allowed range")
        self._gateway = gateway
        self._gateway_identity = gateway
        self._search_port = search_port
        self._max_runs = max_runs
        self._states: dict[str, _RunState] = {}
        self._idempotency: dict[tuple[str, str], _RunState] = {}
        self._completed_order: deque[str] = deque()
        self._replay_guard = _ReplayGuard(max_runs)
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "CoreSearchToolBridge()"

    async def start(
        self,
        request: RunInput,
        *,
        binding: CoreSearchBinding,
    ) -> CoreSearchRunHandle:
        if not isinstance(request, RunInput):
            raise TypeError("request must be a RunInput")
        if not isinstance(binding, CoreSearchBinding):
            raise TypeError("binding must be a CoreSearchBinding")
        self._require_gateway_identity()
        fingerprint = _require_request_binding(request, binding)
        authorization = _authorization_callbacks(request)
        await _require_authorization(*authorization)
        idempotency_scope = (binding.actor_id, request.idempotency_key)
        replay_key = _replay_key(idempotency_scope)
        async with self._lock:
            state = self._idempotency.get(idempotency_scope)
            if state is not None:
                if state.request_fingerprint != fingerprint or state.binding != binding:
                    raise IdempotencyConflictError(
                        "Core Search idempotency key was reused with different caller facts or content"
                    )
                return state.handle
            if self._replay_guard.contains(replay_key):
                raise IdempotencyConflictError("expired Core Search idempotency key cannot be re-executed")
            if sum(not item.closed for item in self._states.values()) >= self._max_runs:
                raise CoreSearchBridgeError("Core Search bridge capacity is exhausted")
            # Local gateway start is bounded and schedules remote I/O rather than awaiting it.
            # Holding this lock reserves capacity before any Core run can be created.
            self._require_gateway_identity()
            reference = await self._gateway.start(request)
            handle = CoreSearchRunHandle(
                run_id=reference.run_id,
                idempotency_key=reference.idempotency_key,
                binding_digest=_binding_digest(binding, request.idempotency_key),
            )
            if handle.run_id in self._states:
                raise CoreSearchBridgeError("Core Search run identity collided")
            state = _RunState(
                handle=handle,
                binding=binding,
                request_fingerprint=fingerprint,
            )
            self._states[handle.run_id] = state
            self._idempotency[idempotency_scope] = state
            self._replay_guard.add(replay_key)
            return handle

    async def continue_run(
        self,
        handle: CoreSearchRunHandle,
        *,
        authorization_request: RunInput,
    ) -> CoreSearchRunOutcome:
        state = await self._state(handle)
        fingerprint = _require_request_binding(authorization_request, state.binding)
        if fingerprint != state.request_fingerprint:
            raise CoreSearchBridgeError("Core Search authorization request binding changed")
        authorization = _authorization_callbacks(authorization_request)
        async with state.lock:
            events: list[RunEvent] = []
            try:
                self._require_gateway_identity()
                await _require_authorization(*authorization)
                if state.outcome is not None:
                    return state.outcome
                generation = await self._active_generation(state)
                async for event in self._gateway.events(handle.run_id):
                    self._require_gateway_identity()
                    await self._require_active_generation(state, generation)
                    if event.kind == "action_required":
                        await self._complete_search_action(
                            state,
                            event,
                            authorization,
                            generation=generation,
                        )
                    events.append(_content_free_receipt_event(event))
                    if event.terminal:
                        break
                if not events or not events[-1].terminal:
                    raise CoreSearchBridgeError("Core Search run ended without a terminal event")
                terminal = events[-1]
                artifacts = _terminal_artifacts(terminal, binding=state.binding)
                outcome = CoreSearchRunOutcome(
                    handle=handle,
                    events=tuple(events),
                    terminal_kind=terminal.kind,
                    artifacts=artifacts,
                )
                await self._commit_outcome(
                    state,
                    outcome,
                    authorization,
                    generation=generation,
                )
                return outcome
            except asyncio.CancelledError:
                await self._cancel_and_close(state)
                raise
            except CoreSearchBridgeError:
                await self._cancel_and_close(state)
                raise
            except Exception:
                await self._cancel_and_close(state)
                raise CoreSearchBridgeError("Core Search continuation failed") from None

    async def execute(
        self,
        request: RunInput,
        *,
        binding: CoreSearchBinding,
    ) -> CoreSearchRunOutcome:
        handle = await self.start(request, binding=binding)
        return await self.continue_run(handle, authorization_request=request)

    async def cancel(self, handle: CoreSearchRunHandle) -> CoreCancelOutcomeV01:
        state = await self._state(handle)
        try:
            self._require_gateway_identity()
        except CoreSearchBridgeError:
            await self._cancel_state(state)
            raise
        return await self._cancel_state(state)

    async def _complete_search_action(
        self,
        state: _RunState,
        event: RunEvent,
        authorization: tuple[
            Callable[[], bool],
            Callable[[], bool | Awaitable[bool]],
            Callable[[str], bool],
        ],
        *,
        generation: int,
    ) -> None:
        await self._require_active_generation(state, generation)
        await _require_authorization(*authorization)
        payload = dict(event.payload)
        if payload.get("tool") != SEARCH_EVIDENCE_CAPABILITY:
            raise CoreSearchBridgeError("Core requested an unavailable capability")
        tool_call_id = payload.get("tool_call_id")
        if not isinstance(tool_call_id, str) or _IDENTIFIER.fullmatch(tool_call_id) is None:
            raise CoreSearchBridgeError("Core Search tool_call_id is invalid")
        if tool_call_id not in state.search_action_ids:
            if len(state.search_action_ids) >= _MAX_SEARCH_ACTIONS_PER_RUN:
                raise CoreSearchBridgeError("Core Search action limit is exhausted")
            state.search_action_ids.add(tool_call_id)
        arguments = _search_arguments(payload.get("arguments"))
        request_id = _tool_request_id(state.handle.binding_digest, tool_call_id)
        try:
            outcome = await self._search_port.search(
                WebSearchRequest(arguments.query, limit=arguments.limit),
                request_id=request_id,
                intent=arguments.intent,
                language=arguments.language,
            )
            if not isinstance(outcome, SearchGatewayOutcome):
                raise CoreSearchBridgeError("Search Fabric returned an invalid outcome")
            if (
                outcome.result.request_id != request_id
                or outcome.result.query_digest != query_digest(arguments.query)
                or outcome.result.intent is not arguments.intent
                or outcome.result.language != arguments.language
            ):
                raise CoreSearchBridgeError("Search Fabric outcome binding is invalid")
            result = CapabilityResult(
                result_id=tool_call_id,
                capability=SEARCH_EVIDENCE_CAPABILITY,
                output=outcome.to_mapping(),
            )
        except asyncio.CancelledError:
            raise
        except CoreSearchBridgeError:
            raise
        except Exception:
            result = CapabilityResult(
                result_id=tool_call_id,
                capability=SEARCH_EVIDENCE_CAPABILITY,
                output={
                    "schema": "yonerai.search.error.v1",
                    "code": "search_unavailable",
                },
                is_error=True,
            )
        async with state.commit_lock:
            self._require_gateway_identity()
            await self._require_active_generation(state, generation)
            await _require_authorization(*authorization)
            await self._require_active_generation(state, generation)
            await self._gateway.submit_result(state.handle.run_id, result)

    async def _state(self, handle: CoreSearchRunHandle) -> _RunState:
        if not isinstance(handle, CoreSearchRunHandle):
            raise TypeError("handle must be a CoreSearchRunHandle")
        async with self._lock:
            state = self._states.get(handle.run_id)
            if state is None or state.handle != handle:
                raise CoreSearchBridgeError("Core Search run handle is stale or foreign")
            return state

    async def _cancel_and_close(self, state: _RunState) -> None:
        await self._cancel_state(state)

    async def _cancel_state(self, state: _RunState) -> CoreCancelOutcomeV01:
        async with state.cancel_lock:
            async with self._lock:
                if state.outcome is not None:
                    return CoreCancelOutcomeV01.already_terminal()
                if state.cancel_outcome is not None:
                    return state.cancel_outcome
            async with state.commit_lock:
                async with self._lock:
                    if state.outcome is not None:
                        return CoreCancelOutcomeV01.already_terminal()
                    if state.cancel_outcome is not None:
                        return state.cancel_outcome
                    state.cancel_requested = True
                    state.generation += 1
            try:
                candidate = await self._gateway_identity.cancel(state.handle.run_id)
            except asyncio.CancelledError:
                await self._record_cancel_outcome(
                    state,
                    CoreCancelOutcomeV01.local_detached_outcome(),
                )
                raise
            except Exception:
                candidate = CoreCancelOutcomeV01.local_detached_outcome()
            if not isinstance(candidate, CoreCancelOutcomeV01):
                candidate = CoreCancelOutcomeV01.local_detached_outcome()
            elif candidate.disposition is CoreCancelDispositionV01.UNSUPPORTED:
                candidate = CoreCancelOutcomeV01.local_detached_outcome()
            await self._record_cancel_outcome(state, candidate)
            return candidate

    async def _record_cancel_outcome(
        self,
        state: _RunState,
        outcome: CoreCancelOutcomeV01,
    ) -> None:
        async with self._lock:
            if state.cancel_outcome is None:
                state.cancel_outcome = outcome
            self._close_state_locked(state)

    async def _active_generation(self, state: _RunState) -> int:
        async with self._lock:
            if state.closed or state.cancel_requested:
                raise CoreSearchBridgeError("Core Search run is cancelled or closed")
            return state.generation

    async def _require_active_generation(
        self,
        state: _RunState,
        generation: int,
    ) -> None:
        async with self._lock:
            if state.closed or state.cancel_requested or state.generation != generation:
                raise CoreSearchBridgeError("Core Search run generation is stale")

    async def _commit_outcome(
        self,
        state: _RunState,
        outcome: CoreSearchRunOutcome,
        authorization: tuple[
            Callable[[], bool],
            Callable[[], bool | Awaitable[bool]],
            Callable[[str], bool],
        ],
        *,
        generation: int,
    ) -> None:
        async with state.commit_lock:
            self._require_gateway_identity()
            await self._require_active_generation(state, generation)
            await _require_authorization(*authorization)
            await self._require_active_generation(state, generation)
            async with self._lock:
                if state.closed or state.cancel_requested or state.generation != generation:
                    raise CoreSearchBridgeError("Core Search terminal generation is stale")
                state.outcome = outcome
                self._close_state_locked(state)

    def _require_gateway_identity(self) -> None:
        if self._gateway is not self._gateway_identity:
            raise CoreSearchBridgeError("Core Search gateway identity changed")

    def _close_state_locked(self, state: _RunState) -> None:
        if not state.closed:
            state.closed = True
            self._completed_order.append(state.handle.run_id)
        while len(self._completed_order) > self._max_runs:
            evicted_run_id = self._completed_order.popleft()
            evicted = self._states.pop(evicted_run_id, None)
            if evicted is None:
                continue
            idempotency_scope = (
                evicted.binding.actor_id,
                evicted.handle.idempotency_key,
            )
            if self._idempotency.get(idempotency_scope) is evicted:
                self._idempotency.pop(idempotency_scope)


def _require_request_binding(request: RunInput, binding: CoreSearchBinding) -> str:
    facts = dict(request.extensions).get(CORE_FACTS_EXTENSION)
    if not isinstance(facts, DiscordCoreFacts):
        raise CoreSearchBridgeError("Discord Core facts are unavailable")
    expected = CoreSearchBinding.from_facts(facts)
    if expected != binding:
        raise CoreSearchBridgeError("Core Search caller binding does not match Discord facts")
    sanitized = replace(
        request,
        local_payload=None,
        authorization_check=None,
        fresh_authorization_check=None,
        capability_authorization_check=None,
    )
    try:
        projected = project_core_message_v01(sanitized)
    except Exception:
        raise CoreSearchBridgeError("Core Search request projection failed") from None
    if projected.conversation_id != binding.conversation_id or projected.user_identity != {
        "provider": "discord",
        "id": binding.actor_id,
    }:
        raise CoreSearchBridgeError("Core Search request identity binding is invalid")
    return _request_fingerprint(projected.to_mapping())


def _authorization_callbacks(
    request: RunInput,
) -> tuple[
    Callable[[], bool],
    Callable[[], bool | Awaitable[bool]],
    Callable[[str], bool],
]:
    authorization = request.authorization_check
    fresh = request.fresh_authorization_check
    capability = request.capability_authorization_check
    if not callable(authorization) or not callable(fresh) or not callable(capability):
        raise CoreSearchBridgeError("Core Search authorization callbacks are required")
    return authorization, fresh, capability


async def _require_authorization(
    authorization_check: Callable[[], bool],
    fresh_authorization_check: Callable[[], bool | Awaitable[bool]],
    capability_authorization_check: Callable[[str], bool],
) -> None:
    try:
        if authorization_check() is not True:
            raise CoreSearchBridgeError("Core Search execution is not authorized")
        if capability_authorization_check(SEARCH_EVIDENCE_CAPABILITY) is not True:
            raise CoreSearchBridgeError("Core Search capability is not authorized")
        candidate = fresh_authorization_check()
        allowed = await candidate if inspect.isawaitable(candidate) else candidate
        if allowed is not True:
            raise CoreSearchBridgeError("Core Search execution is not freshly authorized")
    except asyncio.CancelledError:
        raise
    except CoreSearchBridgeError:
        raise
    except Exception:
        raise CoreSearchBridgeError("Core Search authorization failed") from None


def _request_fingerprint(mapping: Mapping[str, object]) -> str:
    try:
        rendered = json.dumps(
            dict(mapping),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise CoreSearchBridgeError("Core Search request cannot be fingerprinted") from None
    return hashlib.sha256(rendered).hexdigest()


def _search_arguments(value: object) -> _SearchActionArguments:
    if not isinstance(value, Mapping):
        raise CoreSearchBridgeError("Core Search arguments are invalid")
    copied = dict(value)
    if set(copied) != {"query", "intent", "language", "limit"}:
        raise CoreSearchBridgeError("Core Search argument fields are invalid")
    query = copied["query"]
    language = copied["language"]
    limit = copied["limit"]
    if not isinstance(query, str):
        raise CoreSearchBridgeError("Core Search query is invalid")
    if not isinstance(language, str):
        raise CoreSearchBridgeError("Core Search language is invalid")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise CoreSearchBridgeError("Core Search result limit is invalid")
    try:
        request = WebSearchRequest(query, limit=limit)
        intent = SearchIntent(copied["intent"])
        validate_language(language)
        query_digest(request.query)
    except (TypeError, ValueError):
        raise CoreSearchBridgeError("Core Search arguments are invalid") from None
    if request.limit > 10:
        raise CoreSearchBridgeError("Core Search result limit is too large")
    return _SearchActionArguments(
        query=request.query,
        intent=intent,
        language=language,
        limit=request.limit,
    )


def _binding_digest(binding: CoreSearchBinding, idempotency_key: str) -> str:
    document = {
        "request_id": binding.request_id,
        "tenant_id": binding.tenant_id,
        "actor_id": binding.actor_id,
        "conversation_id": binding.conversation_id,
        "idempotency_key": idempotency_key,
    }
    rendered = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(rendered).hexdigest()}"


def _replay_key(idempotency_scope: tuple[str, str]) -> str:
    return f"{idempotency_scope[0]}\x00{idempotency_scope[1]}"


def _tool_request_id(binding_digest: str, tool_call_id: str) -> str:
    rendered = f"{binding_digest}\x00{tool_call_id}".encode("ascii", errors="strict")
    return f"core_{hashlib.sha256(rendered).hexdigest()[:48]}"


def _terminal_artifacts(
    event: RunEvent,
    *,
    binding: CoreSearchBinding,
) -> tuple[ArtifactReference, ...]:
    if event.kind != "final":
        return ()
    value = event.payload.get("artifacts", ())
    if not isinstance(value, tuple) or any(not isinstance(item, ArtifactReference) for item in value):
        raise CoreSearchBridgeError("Core Search final artifacts are invalid")
    owner_scope = CoreArtifactOwnerScopeV01(
        provider="discord",
        subject_id=binding.actor_id,
        conversation_id=binding.conversation_id,
    )
    try:
        for artifact in value:
            core_ref_from_artifact_v01(artifact, owner_scope=owner_scope)
    except (TypeError, ValueError, CoreFilesContractError):
        raise CoreSearchBridgeError("Core Search final artifacts are not scope-bound Core refs") from None
    return value


def _content_free_receipt_event(event: RunEvent) -> RunEvent:
    if event.kind != "action_required":
        return event
    return RunEvent(
        kind=event.kind,
        payload={
            "tool": event.payload["tool"],
            "tool_call_id": event.payload["tool_call_id"],
        },
        extensions=event.extensions,
        run_id=event.run_id,
        sequence=event.sequence,
    )


__all__ = [
    "CoreSearchBinding",
    "CoreSearchBridgeError",
    "CoreSearchRunHandle",
    "CoreSearchRunOutcome",
    "CoreSearchToolBridge",
    "SEARCH_EVIDENCE_CAPABILITY",
    "SearchEvidencePort",
]
