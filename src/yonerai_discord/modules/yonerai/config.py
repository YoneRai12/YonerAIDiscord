from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from typing import Any


class YonerAIConfigurationError(ValueError):
    """YonerAI 境界の安全性を保てない設定を表す。"""


def _value(settings: Any, attribute: str, environ: Mapping[str, str], name: str, default: object) -> object:
    configured = getattr(settings, attribute, None)
    return environ.get(name, default) if configured is None else configured


def _boolean(value: object, name: str, *, default: bool = False) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise YonerAIConfigurationError(f"{name} must be true or false")
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise YonerAIConfigurationError(f"{name} must be true or false")


def _integer(value: object, name: str, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        raise YonerAIConfigurationError(f"{name} must be an integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise YonerAIConfigurationError(f"{name} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise YonerAIConfigurationError(f"{name} must be an integer")
    if not minimum <= parsed <= maximum:
        raise YonerAIConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _floating(value: object, name: str, *, default: float, minimum: float, maximum: float) -> float:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        raise YonerAIConfigurationError(f"{name} must be a number")
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise YonerAIConfigurationError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise YonerAIConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _secret(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise YonerAIConfigurationError(f"{name} must be a string")
    normalized = value.strip()
    if len(normalized) > 4_096:
        raise YonerAIConfigurationError(f"{name} is too long")
    return normalized


@dataclass(frozen=True, slots=True)
class YonerAIRuntimeConfig:
    """将来連携のfail-closed設定。

    外部接続には ``enabled`` に加え、運用者の許可と機能単位の明示的な
    opt-in の両方が必要になる。接続URLは設定として受け取らない。
    """

    enabled: bool = False
    allow_remote: bool = False
    remote_status_opt_in: bool = False
    auth_token: str = field(default="", repr=False)
    timeout_seconds: float = 5.0
    max_response_bytes: int = 65_536

    def __post_init__(self) -> None:
        for name in ("enabled", "allow_remote", "remote_status_opt_in"):
            if not isinstance(getattr(self, name), bool):
                raise YonerAIConfigurationError(f"{name} must be a boolean")
        if not isinstance(self.auth_token, str) or len(self.auth_token) > 4_096:
            raise YonerAIConfigurationError("auth_token is invalid")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise YonerAIConfigurationError("timeout_seconds must be a number")
        if not 0.25 <= float(self.timeout_seconds) <= 15.0:
            raise YonerAIConfigurationError("timeout_seconds must be between 0.25 and 15")
        if isinstance(self.max_response_bytes, bool) or not isinstance(self.max_response_bytes, int):
            raise YonerAIConfigurationError("max_response_bytes must be an integer")
        if not 1_024 <= self.max_response_bytes <= 262_144:
            raise YonerAIConfigurationError("max_response_bytes must be between 1024 and 262144")

    @property
    def remote_permitted(self) -> bool:
        return self.enabled and self.allow_remote and self.remote_status_opt_in

    @property
    def token_configured(self) -> bool:
        return bool(self.auth_token)

    @classmethod
    def load(
        cls,
        settings: Any,
        environ: Mapping[str, str] | None = None,
    ) -> YonerAIRuntimeConfig:
        values = os.environ if environ is None else environ
        return cls(
            enabled=_boolean(
                _value(settings, "yonerai_enabled", values, "YONERAI_ENABLED", False),
                "YONERAI_ENABLED",
            ),
            allow_remote=_boolean(
                _value(settings, "yonerai_allow_remote", values, "YONERAI_ALLOW_REMOTE", False),
                "YONERAI_ALLOW_REMOTE",
            ),
            remote_status_opt_in=_boolean(
                _value(
                    settings,
                    "yonerai_remote_status_opt_in",
                    values,
                    "YONERAI_REMOTE_STATUS_OPT_IN",
                    False,
                ),
                "YONERAI_REMOTE_STATUS_OPT_IN",
            ),
            auth_token=_secret(
                _value(settings, "yonerai_auth_token", values, "YONERAI_AUTH_TOKEN", ""),
                "YONERAI_AUTH_TOKEN",
            ),
            timeout_seconds=_floating(
                _value(settings, "yonerai_timeout_seconds", values, "YONERAI_TIMEOUT_SECONDS", 5.0),
                "YONERAI_TIMEOUT_SECONDS",
                default=5.0,
                minimum=0.25,
                maximum=15.0,
            ),
            max_response_bytes=_integer(
                _value(
                    settings,
                    "yonerai_max_response_bytes",
                    values,
                    "YONERAI_MAX_RESPONSE_BYTES",
                    65_536,
                ),
                "YONERAI_MAX_RESPONSE_BYTES",
                default=65_536,
                minimum=1_024,
                maximum=262_144,
            ),
        )
