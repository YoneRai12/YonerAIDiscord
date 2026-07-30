from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Protocol

from .bounded_tools import (
    ToolAuthorizationCode,
    ToolExecutionAuthorization,
    ToolScopeBinding,
    canonical_revision,
    tool_authorization_current,
)
from .models import AIReply, AIRequest, DataBoundary
from .ports import (
    AIProvider,
    ProviderAuthorizationError,
    _issue_service_sink_verifier,
)


logger = logging.getLogger(__name__)


class AIUnavailableError(RuntimeError):
    pass


class ProviderRetryableError(RuntimeError):
    """A provider-neutral signal that a bounded read-only attempt may be retried."""


class PrivacyBoundaryError(PermissionError):
    pass


class ProviderSelectionError(AIUnavailableError):
    """A preferred route could not be resolved without an unsafe fallback."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"AI provider route is unavailable: {reason}")


@dataclass(frozen=True, slots=True)
class AIProviderSelection:
    provider_id: str
    provider: AIProvider
    model_alias: str | None
    reason: str
    token: str

    def __post_init__(self) -> None:
        for label, value in (
            ("provider_id", self.provider_id),
            ("reason", self.reason),
            ("token", self.token),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string")


class AIProviderSelector(Protocol):
    @property
    def available(self) -> bool: ...

    def select(self, request: AIRequest) -> AIProviderSelection: ...


class AIService:
    """プロンプト本文をログへ出さず、外部送信を明示同意で囲う。"""

    def __init__(
        self,
        provider: AIProvider | None,
        *,
        concurrency: int = 2,
        max_pending: int = 4,
        queue_timeout_seconds: float = 2.0,
        provider_selector: AIProviderSelector | None = None,
        require_prepared_context: bool = False,
        require_authorization: bool = False,
        provider_catalog_revision: str | None = None,
        capability_catalog_revision: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 16:
            raise ValueError("concurrency must be between 1 and 16")
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or not 0 <= max_pending <= 128:
            raise ValueError("max_pending must be between 0 and 128")
        if (
            isinstance(queue_timeout_seconds, bool)
            or not isinstance(queue_timeout_seconds, (int, float))
            or not math.isfinite(float(queue_timeout_seconds))
            or not 0.05 <= float(queue_timeout_seconds) <= 30.0
        ):
            raise ValueError("queue_timeout_seconds must be between 0.05 and 30")
        self._provider = provider
        self._provider_selector = provider_selector
        self._require_prepared_context = bool(require_prepared_context)
        self._require_authorization = bool(require_authorization)
        self._provider_catalog_revision = provider_catalog_revision
        self._capability_catalog_revision = capability_catalog_revision
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._gate = asyncio.Semaphore(concurrency)
        self._maximum_inflight = concurrency + max_pending
        self._queue_timeout_seconds = float(queue_timeout_seconds)
        self._inflight = 0

    @property
    def available(self) -> bool:
        return self._provider is not None or bool(self._provider_selector and self._provider_selector.available)

    @property
    def provider_locality(self) -> bool | None:
        """Return a known fixed locality; routed or absent providers stay unknown."""

        if self._provider_selector is not None or self._provider is None:
            return None
        return bool(self._provider.is_local)

    @property
    def provider_catalog_revision(self) -> str:
        configured = self._provider_catalog_revision
        if configured is not None:
            return configured
        selector_revision = getattr(self._provider_selector, "catalog_revision", None)
        if isinstance(selector_revision, str):
            return selector_revision
        provider = self._provider
        if provider is None:
            return canonical_revision({"provider": "unconfigured"})
        return canonical_revision(
            {
                "is_local": bool(provider.is_local),
                "model_bindings": getattr(provider, "runtime_model_bindings", {}),
                "provider_id": getattr(provider, "runtime_provider_id", "legacy.default"),
                "supports_web_search": bool(getattr(provider, "supports_web_search", False)),
            }
        )

    async def ask(
        self,
        request: AIRequest,
        *,
        provider_call_allowed: Callable[[], bool] | None = None,
        fresh_provider_call_allowed: Callable[[], bool | Awaitable[bool]] | None = None,
        tool_capability_allowed: Callable[[str], bool] | None = None,
    ) -> AIReply:
        if request.bounded_toolset is not None and not request.context_authorization_current():
            raise AIUnavailableError("formal AI request has no current provider envelope")
        if self._require_prepared_context and (
            request.provider_input is None
            or request.context_authorization is None
            or not request.context_authorization_current()
        ):
            raise AIUnavailableError("AI request did not pass the canonical ContextBuilder")
        selection = self._select_provider(request)
        provider = selection.provider
        adapter_provider_id = getattr(
            provider,
            "runtime_provider_id",
            getattr(provider, "provider_id", selection.provider_id),
        )
        if adapter_provider_id != selection.provider_id:
            raise ProviderSelectionError("provider_identity_mismatch")
        routed_request = (
            request
            if request.effective_model_alias == selection.model_alias
            else replace(request, effective_model_alias=selection.model_alias)
        )
        toolset = routed_request.bounded_toolset
        if self._require_authorization and toolset is None:
            raise AIUnavailableError("formal AI execution requires a bounded toolset")
        resolved_model_alias = self._resolved_model_alias(
            provider,
            routed_request,
            selection,
            require_concrete=bool(toolset and toolset.effective_tools),
        )
        if toolset is not None and toolset.provider_catalog_revision != self.provider_catalog_revision:
            self._log_tool_denial(
                ToolAuthorizationCode.PROVIDER_REVISION_CHANGED,
                toolset.capability_catalog_revision,
                self.provider_catalog_revision,
            )
            raise AIUnavailableError("bounded AI execution catalog changed")
        if toolset is not None and toolset.effective_tools:
            if self._capability_catalog_revision is None:
                raise AIUnavailableError("live capability catalog revision source is required")
            if tool_capability_allowed is None:
                raise AIUnavailableError("model-tool capability authorization callback is required")
            try:
                execution_authorization = ToolExecutionAuthorization.seal(
                    toolset,
                    provider_id=selection.provider_id,
                    model_alias=resolved_model_alias,
                    issued_at=self._clock(),
                )
            except (TypeError, ValueError) as exc:
                raise AIUnavailableError("bounded model-tool authorization could not be sealed") from exc
            routed_request = replace(
                routed_request,
                tool_execution_authorization=execution_authorization,
            )
        if not provider.is_local and request.boundary is not DataBoundary.REMOTE_OPT_IN:
            raise PrivacyBoundaryError("remote AI requires explicit opt-in")
        if self._require_authorization and provider_call_allowed is None:
            raise AIUnavailableError("AI execution authorization callback is required")
        # checkとincrementの間にawaitを置かず、同一event loop上でburstを即時拒否する。
        if self._inflight >= self._maximum_inflight:
            raise AIUnavailableError("AI request queue is full")
        self._inflight += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._gate.acquire(), timeout=self._queue_timeout_seconds)
                acquired = True
            except TimeoutError as exc:
                logger.warning("ai_request_queue_expired")
                raise AIUnavailableError("AI request queue wait expired") from exc
            current = self._select_provider(request)
            if current.token != selection.token or current.provider is not provider:
                raise ProviderSelectionError("route_changed_before_provider_call")
            if (
                routed_request.bounded_toolset is not None or self._require_prepared_context
            ) and not routed_request.context_authorization_current():
                raise AIUnavailableError("prepared provider envelope changed before provider call")
            if not self._tool_authorization_allowed(
                routed_request,
                current,
                resolved_model_alias=resolved_model_alias,
                tool_capability_allowed=tool_capability_allowed,
            ):
                raise AIUnavailableError("bounded model-tool authorization expired before provider call")
            if not self._authorization_allowed(provider_call_allowed):
                logger.info("ai_provider_authorization_expired")
                if provider.is_local:
                    raise AIUnavailableError("AI execution authorization expired before provider call")
                raise PrivacyBoundaryError("remote AI authorization expired before provider call")
            if fresh_provider_call_allowed is not None and not await self._authorization_allowed_async(
                fresh_provider_call_allowed
            ):
                logger.info("ai_provider_fresh_authorization_expired")
                if provider.is_local:
                    raise AIUnavailableError("AI execution authorization expired before provider call")
                raise PrivacyBoundaryError("remote AI authorization expired before provider call")
            logger.info(
                "ai_request_started",
                extra={
                    "data_boundary": routed_request.boundary.value,
                    "prompt_length": len(routed_request.prompt),
                    "provider_id": selection.provider_id,
                    "route_reason": selection.reason,
                },
            )
            try:
                if provider_call_allowed is not None:
                    complete_authorized = getattr(provider, "complete_authorized", None)
                    if not callable(complete_authorized):
                        if self._require_authorization or not provider.is_local:
                            logger.warning("ai_provider_missing_sink_authorization")
                            raise ProviderAuthorizationError(
                                "AI provider cannot recheck authorization at its execution boundary"
                            )
                        reply = await provider.complete(routed_request)
                    else:

                        def sink_authorization_current() -> bool:
                            return (
                                self._authorization_allowed(provider_call_allowed)
                                and self._selection_is_current(request, selection)
                                and (
                                    routed_request.bounded_toolset is None
                                    or routed_request.context_authorization_current()
                                )
                                and self._tool_authorization_allowed(
                                    routed_request,
                                    selection,
                                    resolved_model_alias=resolved_model_alias,
                                    tool_capability_allowed=tool_capability_allowed,
                                )
                            )

                        verifier_check: Callable[[], bool | Awaitable[bool]] = sink_authorization_current
                        if fresh_provider_call_allowed is not None:

                            async def fresh_sink_authorization_current() -> bool:
                                return (
                                    await self._authorization_allowed_async(fresh_provider_call_allowed)
                                    and sink_authorization_current()
                                )

                            verifier_check = fresh_sink_authorization_current
                        attempts = 2 if routed_request.web_search and routed_request.has_side_effects is False else 1
                        for attempt in range(attempts):
                            sink_verifier = _issue_service_sink_verifier(
                                request=routed_request,
                                provider=provider,
                                check=verifier_check,
                            )
                            try:
                                reply = await complete_authorized(
                                    routed_request,
                                    sink_verifier,
                                )
                                break
                            except ProviderRetryableError:
                                if attempt + 1 >= attempts:
                                    raise
                        else:
                            raise AssertionError("provider retry loop did not terminate")
                else:
                    reply = await provider.complete(routed_request)
            except ProviderAuthorizationError as exc:
                logger.info("ai_authorization_expired_at_provider_sink")
                if provider.is_local:
                    raise AIUnavailableError("AI authorization expired at the provider boundary") from exc
                raise PrivacyBoundaryError("remote AI authorization expired at the provider boundary") from exc
            except Exception as exc:
                self._record_provider_result(selection.provider_id, success=False)
                logger.warning("ai_request_failed", extra={"error_type": type(exc).__name__})
                raise AIUnavailableError("AI request failed") from exc
        finally:
            if acquired:
                self._gate.release()
            self._inflight -= 1
        self._record_provider_result(selection.provider_id, success=True)
        logger.info("ai_request_completed", extra={"reply_length": len(reply.text)})
        if self._provider_selector is None or reply.provider == selection.provider_id:
            return reply
        return AIReply(
            text=reply.text,
            model=reply.model,
            provider=selection.provider_id,
            sources=reply.sources,
            artifact_references=reply.artifact_references,
            synthesis_action_id=reply.synthesis_action_id,
            delivery_handled=reply.delivery_handled,
        )

    def _select_provider(self, request: AIRequest) -> AIProviderSelection:
        selector = self._provider_selector
        if selector is not None:
            return selector.select(request)
        provider = self._provider
        if provider is None:
            raise AIUnavailableError("AI provider is not configured")
        model_alias = request.required_model_alias or request.effective_model_alias
        if request.required_model_alias is not None:
            bindings = getattr(provider, "runtime_model_bindings", None)
            if (
                not isinstance(bindings, dict)
                or request.required_model_alias not in bindings
                or (
                    request.required_model_id is not None
                    and bindings[request.required_model_alias] != request.required_model_id
                )
            ):
                raise ProviderSelectionError("required_model_unavailable")
        return AIProviderSelection(
            provider_id=getattr(provider, "runtime_provider_id", "legacy.default"),
            provider=provider,
            model_alias=model_alias,
            reason="legacy_default_path",
            token=canonical_revision(
                {
                    "catalog_revision": self.provider_catalog_revision,
                    "model_alias": model_alias,
                    "model_bindings": getattr(provider, "runtime_model_bindings", {}),
                    "provider_id": getattr(provider, "runtime_provider_id", "legacy.default"),
                }
            ),
        )

    def _selection_is_current(self, request: AIRequest, expected: AIProviderSelection) -> bool:
        try:
            current = self._select_provider(request)
        except Exception:
            return False
        return current.token == expected.token and current.provider is expected.provider

    def _record_provider_result(self, provider_id: str, *, success: bool) -> None:
        selector = self._provider_selector
        method = getattr(selector, "record_success" if success else "record_failure", None)
        if not callable(method):
            return
        try:
            method(provider_id)
        except Exception as exc:
            logger.warning(
                "ai_provider_readiness_update_failed",
                extra={"error_type": type(exc).__name__},
            )

    def _tool_authorization_allowed(
        self,
        request: AIRequest,
        selection: AIProviderSelection,
        *,
        resolved_model_alias: str,
        tool_capability_allowed: Callable[[str], bool] | None,
    ) -> bool:
        toolset = request.bounded_toolset
        if toolset is None:
            return not self._require_authorization
        capability_revision = self._current_capability_catalog_revision(toolset.capability_catalog_revision)
        capability_authorizations: dict[str, bool] = {}
        for _, capability_id in toolset.tool_capability_bindings:
            capability_authorizations[capability_id] = self._capability_allowed(
                tool_capability_allowed,
                capability_id,
            )
        decision = tool_authorization_current(
            toolset,
            request.tool_execution_authorization,
            scope=ToolScopeBinding(request.guild_id, request.channel_id, request.user_id),
            intent=request.intent.value,
            complexity=request.complexity.value,
            capability_catalog_revision=capability_revision,
            provider_catalog_revision=self.provider_catalog_revision,
            provider_id=selection.provider_id,
            model_alias=resolved_model_alias,
            now=self._clock(),
            capability_authorizations=capability_authorizations,
        )
        if not decision.allowed:
            self._log_tool_denial(
                decision.code,
                capability_revision,
                self.provider_catalog_revision,
            )
        return decision.allowed

    def _current_capability_catalog_revision(self, fallback: str) -> str:
        getter = self._capability_catalog_revision
        if getter is None:
            return fallback
        try:
            revision = getter()
        except Exception:
            return ""
        return revision if isinstance(revision, str) else ""

    @staticmethod
    def _resolved_model_alias(
        provider: AIProvider,
        request: AIRequest,
        selection: AIProviderSelection,
        *,
        require_concrete: bool,
    ) -> str:
        resolver = getattr(provider, "resolved_model_alias", None)
        if callable(resolver):
            value = resolver(request)
            if isinstance(value, str) and value:
                return value
        if selection.model_alias is not None:
            return selection.model_alias
        if request.required_model_alias is not None:
            return request.required_model_alias
        if require_concrete:
            raise ProviderSelectionError("model_binding_unresolved")
        return "ai.auto"

    @staticmethod
    def _capability_allowed(
        callback: Callable[[str], bool] | None,
        capability_id: str,
    ) -> bool:
        if callback is None:
            return False
        try:
            return callback(capability_id) is True
        except Exception:
            return False

    @staticmethod
    def _log_tool_denial(
        code: ToolAuthorizationCode,
        capability_revision: str,
        provider_revision: str,
    ) -> None:
        logger.info(
            "ai_bounded_tool_authorization_denied",
            extra={
                "denial_code": code.value,
                "capability_revision": capability_revision[:12],
                "provider_revision": provider_revision[:12],
            },
        )

    @staticmethod
    def _authorization_allowed(provider_call_allowed: Callable[[], bool] | None) -> bool:
        if provider_call_allowed is None:
            return True
        try:
            return provider_call_allowed() is True
        except Exception as exc:
            logger.warning(
                "ai_provider_authorization_check_failed",
                extra={"error_type": type(exc).__name__},
            )
            return False

    @staticmethod
    async def _authorization_allowed_async(
        provider_call_allowed: Callable[[], bool | Awaitable[bool]] | None,
    ) -> bool:
        if provider_call_allowed is None:
            return True
        try:
            result = provider_call_allowed()
            if inspect.isawaitable(result):
                result = await result
            return result is True
        except Exception as exc:
            logger.warning(
                "ai_provider_authorization_check_failed",
                extra={"error_type": type(exc).__name__},
            )
            return False
