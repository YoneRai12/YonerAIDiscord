from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from .domain import AuditRecord, ProviderHealth, ProviderInvocation, ProviderRequest, ProviderResult

ExecutionAuthorizationCheck = Callable[[], bool | Awaitable[bool]]


class ProviderAdapter(Protocol):
    """API/ローカル共通port。adapterの動的importは行わず、composition rootで明示登録する。"""

    @property
    def provider_id(self) -> str: ...

    @property
    def adapter_id(self) -> str: ...

    async def health(self) -> ProviderHealth: ...

    async def execute(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        execution_allowed: ExecutionAuthorizationCheck | None = None,
    ) -> ProviderResult: ...

    async def close(self) -> None: ...


class ProviderAuditSink(Protocol):
    """Prompt/response本文を含まない構造化監査recordの保存先。"""

    async def append(self, record: AuditRecord) -> None: ...


class SecretResolver(Protocol):
    """Manifest外のcomposition rootだけがsecret値を解決する。"""

    def resolve_secret(self, reference: str) -> str: ...


class SettingResolver(Protocol):
    def resolve_setting(self, reference: str) -> str: ...
