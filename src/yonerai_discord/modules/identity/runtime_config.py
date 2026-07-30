from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from typing import Any


class IdentityRuntimeConfigurationError(ValueError):
    pass


def _boolean(raw: object, name: str, default: bool = False) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    normalized = str(raw).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise IdentityRuntimeConfigurationError(f"{name} must be true or false")


def _value(settings: Any, attribute: str, environ: Mapping[str, str], name: str, default: str = "") -> object:
    configured = getattr(settings, attribute, None)
    if configured is not None:
        return configured
    return environ.get(name, default)


def _integer(raw: object, name: str, default: int) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise IdentityRuntimeConfigurationError(f"{name} must be an integer") from exc
    if not 1 <= value <= 65_535:
        raise IdentityRuntimeConfigurationError(f"{name} is outside the allowed range")
    return value


@dataclass(frozen=True, slots=True)
class IdentityRuntimeConfig:
    enabled: bool = False
    public_base_url: str = ""
    turnstile_secret: str = field(default="", repr=False)
    allow_insecure_localhost: bool = False
    http_enabled: bool = False
    http_host: str = "127.0.0.1"
    http_port: int = 8_765
    turnstile_site_key: str = field(default="", repr=False)

    @property
    def callback_configured(self) -> bool:
        return bool(self.public_base_url and (self.turnstile_secret or self.allow_insecure_localhost))

    @classmethod
    def load(
        cls,
        settings: Any,
        environ: Mapping[str, str] | None = None,
    ) -> IdentityRuntimeConfig:
        values = os.environ if environ is None else environ
        enabled = _boolean(
            _value(settings, "identity_enabled", values, "IDENTITY_ENABLED"),
            "IDENTITY_ENABLED",
        )
        allow_local = _boolean(
            _value(
                settings,
                "identity_allow_insecure_localhost",
                values,
                "IDENTITY_ALLOW_INSECURE_LOCALHOST",
            ),
            "IDENTITY_ALLOW_INSECURE_LOCALHOST",
        )
        public_base_url = str(_value(settings, "identity_public_base_url", values, "IDENTITY_PUBLIC_BASE_URL")).strip()
        turnstile_secret = str(
            _value(settings, "identity_turnstile_secret", values, "IDENTITY_TURNSTILE_SECRET")
        ).strip()
        http_enabled = _boolean(
            _value(settings, "identity_http_enabled", values, "IDENTITY_HTTP_ENABLED"),
            "IDENTITY_HTTP_ENABLED",
        )
        http_host = str(_value(settings, "identity_http_host", values, "IDENTITY_HTTP_HOST", "127.0.0.1")).strip()
        http_port = _integer(
            _value(settings, "identity_http_port", values, "IDENTITY_HTTP_PORT", "8765"),
            "IDENTITY_HTTP_PORT",
            8_765,
        )
        site_key = str(
            _value(
                settings,
                "identity_turnstile_site_key",
                values,
                "IDENTITY_TURNSTILE_SITE_KEY",
            )
        ).strip()
        if any(len(value) > 2_048 for value in (public_base_url, turnstile_secret, http_host, site_key)):
            raise IdentityRuntimeConfigurationError("identity configuration value is too long")
        return cls(
            enabled=enabled,
            public_base_url=public_base_url,
            turnstile_secret=turnstile_secret,
            allow_insecure_localhost=allow_local,
            http_enabled=http_enabled,
            http_host=http_host,
            http_port=http_port,
            turnstile_site_key=site_key,
        )
