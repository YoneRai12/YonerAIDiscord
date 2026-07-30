from __future__ import annotations

import ipaddress
from dataclasses import dataclass


class AdminUiConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdminUiConfig:
    """外部設定読込を持たない、注入専用のloopback listener設定。"""

    enabled: bool = False
    bind_host: str = "127.0.0.1"
    bind_port: int = 8_766

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise AdminUiConfigurationError("admin UI enabled must be bool")
        if not isinstance(self.bind_host, str):
            raise AdminUiConfigurationError("admin UI host is invalid")
        host = self.bind_host.strip()
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise AdminUiConfigurationError("admin UI listener must use a literal loopback address") from exc
        if not address.is_loopback:
            raise AdminUiConfigurationError("admin UI listener must bind to loopback")
        if isinstance(self.bind_port, bool) or not isinstance(self.bind_port, int):
            raise AdminUiConfigurationError("admin UI port is invalid")
        if not 1 <= self.bind_port <= 65_535:
            raise AdminUiConfigurationError("admin UI port is invalid")
        object.__setattr__(self, "bind_host", address.compressed)


__all__ = ["AdminUiConfig", "AdminUiConfigurationError"]
