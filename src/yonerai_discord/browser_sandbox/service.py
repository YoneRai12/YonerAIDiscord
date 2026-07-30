from __future__ import annotations

import asyncio
from typing import Protocol

from .models import (
    BROWSER_ISOLATION_CONTRACT,
    BrowserAdapterContractError,
    BrowserIsolationContract,
    BrowserOutputKind,
    BrowserSessionRequest,
    BrowserSessionResult,
    BrowserSandboxUnavailableError,
    ExtractText,
    Screenshot,
)
from .policy import BrowserNetworkGuard, BrowserSandboxPolicy


class IsolatedBrowserAdapter(Protocol):
    """Adapter contract for a separately isolated, ephemeral browser worker.

    The adapter must call ``network_guard.authorize_request`` immediately before
    every navigation, subresource request and redirect, and connect only to one
    of the returned addresses. It must stream every received body chunk through
    ``network_guard.consume_bytes`` before retaining or parsing that chunk.
    """

    async def execute(
        self,
        request: BrowserSessionRequest,
        *,
        network_guard: BrowserNetworkGuard,
        isolation_contract: BrowserIsolationContract,
    ) -> BrowserSessionResult: ...


class BrowserSandboxService:
    def __init__(
        self,
        *,
        policy: BrowserSandboxPolicy,
        adapter: IsolatedBrowserAdapter | None = None,
    ) -> None:
        if not isinstance(policy, BrowserSandboxPolicy):
            raise TypeError("policy must be BrowserSandboxPolicy")
        self._policy = policy
        self._adapter = adapter

    @property
    def configured(self) -> bool:
        return self._adapter is not None

    async def execute(self, request: BrowserSessionRequest) -> BrowserSessionResult:
        if self._adapter is None:
            raise BrowserSandboxUnavailableError("isolated browser adapter is not configured")
        self._policy.validate_session(request)
        guard = BrowserNetworkGuard(self._policy)
        try:
            result = await asyncio.wait_for(
                self._adapter.execute(
                    request,
                    network_guard=guard,
                    isolation_contract=BROWSER_ISOLATION_CONTRACT,
                ),
                timeout=self._policy.limits.max_duration_seconds,
            )
        except TimeoutError as exc:
            raise BrowserAdapterContractError("isolated browser adapter timed out") from exc
        if not isinstance(result, BrowserSessionResult):
            raise BrowserAdapterContractError("isolated browser adapter returned an invalid result")
        if any(output.step_index >= len(request.actions) for output in result.outputs):
            raise BrowserAdapterContractError("browser output references an unknown step")
        for output in result.outputs:
            action = request.actions[output.step_index]
            if output.kind is BrowserOutputKind.SCREENSHOT and not isinstance(action, Screenshot):
                raise BrowserAdapterContractError("screenshot output does not match its action")
            if output.kind is BrowserOutputKind.TEXT and not isinstance(action, ExtractText):
                raise BrowserAdapterContractError("text output does not match its action")
        guard.consume_bytes(result.byte_length)
        return result
