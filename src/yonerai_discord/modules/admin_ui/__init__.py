"""既定OFF・OAuth/session未接続のread-only Admin Web UI Stage 1。"""

from __future__ import annotations

from typing import Any

from .config import AdminUiConfig, AdminUiConfigurationError
from .plugin import AdminUiPlugin, AdminUiServerFactory
from .projection import (
    AdminAuditProjection,
    AdminCapabilityProjection,
    AdminDeploymentProjection,
    AdminDeploymentSourceProjection,
    AdminModuleProjection,
    AdminUiProjection,
    AdminUiProjectionError,
    build_admin_ui_projection,
)
from .web_adapter import (
    ADMIN_UI_CAPABILITY_ID,
    AdminUiAuthenticator,
    AdminUiHttpResponse,
    AdminUiRequestHandler,
    AdminUiServer,
    AdminUiWebServer,
    render_admin_ui,
)


def setup(manager: Any) -> None:
    register = getattr(manager, "register_plugin", None) or getattr(manager, "register", None)
    if register is None:
        raise TypeError("manager must provide register_plugin() or register()")
    register("admin_ui", AdminUiPlugin)


__all__ = [
    "ADMIN_UI_CAPABILITY_ID",
    "AdminAuditProjection",
    "AdminCapabilityProjection",
    "AdminDeploymentProjection",
    "AdminDeploymentSourceProjection",
    "AdminModuleProjection",
    "AdminUiAuthenticator",
    "AdminUiConfig",
    "AdminUiConfigurationError",
    "AdminUiHttpResponse",
    "AdminUiPlugin",
    "AdminUiProjection",
    "AdminUiProjectionError",
    "AdminUiRequestHandler",
    "AdminUiServer",
    "AdminUiServerFactory",
    "AdminUiWebServer",
    "build_admin_ui_projection",
    "render_admin_ui",
    "setup",
]
