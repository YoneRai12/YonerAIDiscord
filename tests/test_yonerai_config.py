from __future__ import annotations

from types import SimpleNamespace

import pytest

from yonerai_discord.modules.yonerai import (
    YonerAIConfigurationError,
    YonerAIRuntimeConfig,
)


def test_defaults_are_disabled_and_do_not_allow_remote() -> None:
    config = YonerAIRuntimeConfig.load(SimpleNamespace(), {})

    assert config.enabled is False
    assert config.allow_remote is False
    assert config.remote_status_opt_in is False
    assert config.remote_permitted is False
    assert config.timeout_seconds == 5.0
    assert config.max_response_bytes == 65_536


def test_remote_requires_enable_and_two_independent_opt_ins() -> None:
    base = {
        "YONERAI_ENABLED": "true",
        "YONERAI_ALLOW_REMOTE": "true",
        "YONERAI_REMOTE_STATUS_OPT_IN": "true",
    }
    assert YonerAIRuntimeConfig.load(SimpleNamespace(), base).remote_permitted

    for missing in base:
        values = dict(base)
        values[missing] = "false"
        assert not YonerAIRuntimeConfig.load(SimpleNamespace(), values).remote_permitted


def test_settings_fields_take_precedence_over_environment() -> None:
    settings = SimpleNamespace(
        yonerai_enabled=False,
        yonerai_allow_remote=False,
        yonerai_remote_status_opt_in=False,
    )
    values = {
        "YONERAI_ENABLED": "true",
        "YONERAI_ALLOW_REMOTE": "true",
        "YONERAI_REMOTE_STATUS_OPT_IN": "true",
    }
    assert not YonerAIRuntimeConfig.load(settings, values).remote_permitted


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("YONERAI_ENABLED", "sometimes"),
        ("YONERAI_ALLOW_REMOTE", 1),
        ("YONERAI_TIMEOUT_SECONDS", "0.1"),
        ("YONERAI_TIMEOUT_SECONDS", True),
        ("YONERAI_MAX_RESPONSE_BYTES", "512"),
        ("YONERAI_MAX_RESPONSE_BYTES", "1.5"),
        ("YONERAI_AUTH_TOKEN", 123),
    ],
)
def test_invalid_or_unsafe_values_fail_closed(name: str, value: object) -> None:
    with pytest.raises(YonerAIConfigurationError):
        YonerAIRuntimeConfig.load(SimpleNamespace(), {name: value})  # type: ignore[dict-item]


def test_secret_is_redacted_from_repr() -> None:
    token = "test_fixture_private_yonerai_token"
    config = YonerAIRuntimeConfig(auth_token=token)

    assert token not in repr(config)
    assert config.token_configured


def test_no_user_configurable_url_exists_in_runtime_contract() -> None:
    fields = YonerAIRuntimeConfig.__dataclass_fields__
    assert not {name for name in fields if "url" in name or "endpoint" in name or "host" in name}
