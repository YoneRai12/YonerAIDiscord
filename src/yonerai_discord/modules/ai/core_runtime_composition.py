from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from yonerai_discord.execution_gateway.core_http_transport import (
    YonerAIInternalRunHttpPortV01,
)
from yonerai_discord.execution_gateway.core_v01 import YonerAIInternalRunGatewayV01
from yonerai_discord.execution_gateway.ora_core_transport import OraCoreHttpTransport
from yonerai_discord.secret_policy import is_loopback_endpoint

from .core_surface import DiscordCoreSurfaceGateway
from .execution_profiles import (
    ExecutionProfileError,
    ExecutionTopology,
    PackagingDependencyClass,
    profile_contract,
    resolve_runtime_execution_profile,
    validate_profile_dependencies,
)

if TYPE_CHECKING:
    from .service import AIService


class DirectCoreRuntimeCompositionError(RuntimeError):
    """Production Direct Coreの安全な構成条件が満たされていない。"""


class DirectCoreRuntimeSettings(Protocol):
    ai_execution_topology: str | None
    ai_hosting_profile: str | None
    ai_packaging_candidate: str | None
    yonerai_enabled: bool
    yonerai_allow_remote: bool
    yonerai_remote_status_opt_in: bool
    yonerai_auth_token: str
    yonerai_core_origin: str
    yonerai_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class DirectCoreGatewayFactory:
    """検証済み設定からstrict v0.1 Direct Core gatewayを都度構成する。"""

    _origin: str = field(repr=False)
    _bearer_token: str = field(repr=False)
    _timeout_seconds: float = field(repr=False)

    def __repr__(self) -> str:
        return "DirectCoreGatewayFactory()"

    def __call__(self, _service: AIService) -> DiscordCoreSurfaceGateway:
        transport = OraCoreHttpTransport(
            self._origin,
            self._bearer_token,
            timeout_seconds=self._timeout_seconds,
        )
        # HTTP接続/無通信timeoutとrun全体deadlineを混同しない。
        # stream totalはstrict portのcode-owned bounded defaultを使う。
        port = YonerAIInternalRunHttpPortV01(transport)
        gateway = YonerAIInternalRunGatewayV01(port)
        return DiscordCoreSurfaceGateway(gateway, files=None)


def build_direct_core_gateway_factory(
    settings: DirectCoreRuntimeSettings,
) -> DirectCoreGatewayFactory:
    """明示opt-in済み設定だけをproduction Direct Core factoryへ変換する。"""

    try:
        enabled = settings.yonerai_enabled
        allow_remote = settings.yonerai_allow_remote
        remote_status_opt_in = settings.yonerai_remote_status_opt_in
        origin = settings.yonerai_core_origin
        bearer_token = settings.yonerai_auth_token
        timeout_seconds = settings.yonerai_timeout_seconds
    except Exception:
        raise DirectCoreRuntimeCompositionError("Direct Core runtime configuration is unavailable") from None

    if enabled is not True:
        raise DirectCoreRuntimeCompositionError("Direct Core runtime is not explicitly enabled")
    if not isinstance(origin, str) or not origin:
        raise DirectCoreRuntimeCompositionError("Direct Core origin is unavailable")
    local_origin = is_loopback_endpoint(origin)
    if not local_origin and (allow_remote is not True or remote_status_opt_in is not True):
        raise DirectCoreRuntimeCompositionError("Direct Core runtime is not explicitly enabled")
    if not isinstance(bearer_token, str) or (not bearer_token and not local_origin):
        raise DirectCoreRuntimeCompositionError("Direct Core authorization is unavailable")

    try:
        selection = resolve_runtime_execution_profile(settings)
        if selection.topology is not ExecutionTopology.DIRECT_CORE:
            raise ExecutionProfileError("Direct Core topology is required")
        validate_profile_dependencies(
            profile_contract(
                selection.topology,
                selection.hosting_profile,
                selection.packaging,
            ),
            (
                PackagingDependencyClass.OFFICIAL_SECRET,
                PackagingDependencyClass.PRIVATE_ENDPOINT,
            ),
        )
        OraCoreHttpTransport(
            origin,
            bearer_token,
            timeout_seconds=timeout_seconds,
        )
    except (ExecutionProfileError, TypeError, ValueError):
        raise DirectCoreRuntimeCompositionError("Direct Core runtime configuration is invalid") from None

    return DirectCoreGatewayFactory(
        _origin=origin,
        _bearer_token=bearer_token,
        _timeout_seconds=float(timeout_seconds),
    )
