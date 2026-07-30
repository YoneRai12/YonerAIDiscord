from __future__ import annotations

from types import SimpleNamespace

import pytest

from yonerai_discord.execution_gateway.core_http_transport import (
    AiohttpCoreHttpTransport,
    YonerAIInternalRunHttpPortV01,
)
from yonerai_discord.execution_gateway.core_v01 import YonerAIInternalRunGatewayV01
from yonerai_discord.modules.ai.core_runtime_composition import (
    DirectCoreGatewayFactory,
    DirectCoreRuntimeCompositionError,
    build_direct_core_gateway_factory,
)
from yonerai_discord.modules.ai.core_surface import DiscordCoreSurfaceGateway


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "ai_execution_topology": "direct_core",
        "ai_hosting_profile": "official_managed",
        "ai_packaging_candidate": "official_private",
        "yonerai_enabled": True,
        "yonerai_allow_remote": True,
        "yonerai_remote_status_opt_in": True,
        "yonerai_auth_token": "offline-bearer-token",
        "yonerai_core_origin": "https://core.example.test",
        "yonerai_timeout_seconds": 12.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("packaging", ("official_private", "local_only"))
def test_factory_builds_the_existing_strict_direct_core_stack(packaging: str) -> None:
    factory = build_direct_core_gateway_factory(_settings(ai_packaging_candidate=packaging))
    surface = factory(object())

    assert isinstance(factory, DirectCoreGatewayFactory)
    assert isinstance(surface, DiscordCoreSurfaceGateway)
    assert surface._files is None
    assert isinstance(surface._gateway, YonerAIInternalRunGatewayV01)
    assert isinstance(surface._gateway._port, YonerAIInternalRunHttpPortV01)
    transport = surface._gateway._port._transport
    assert isinstance(transport, AiohttpCoreHttpTransport)
    assert transport._origin == "https://core.example.test"
    assert surface._gateway._port._stream_total_timeout_seconds == 120.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("yonerai_enabled", False),
        ("yonerai_enabled", 1),
        ("yonerai_allow_remote", False),
        ("yonerai_remote_status_opt_in", False),
    ],
)
def test_factory_requires_all_three_exact_opt_in_gates(field: str, value: object) -> None:
    with pytest.raises(DirectCoreRuntimeCompositionError, match="not explicitly enabled"):
        build_direct_core_gateway_factory(_settings(**{field: value}))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"yonerai_auth_token": ""}, "authorization is unavailable"),
        ({"yonerai_auth_token": "bad token"}, "configuration is invalid"),
        ({"yonerai_core_origin": ""}, "origin is unavailable"),
        ({"yonerai_core_origin": "http://public.example.test"}, "configuration is invalid"),
        ({"yonerai_timeout_seconds": True}, "configuration is invalid"),
        ({"yonerai_timeout_seconds": 0}, "configuration is invalid"),
    ],
)
def test_factory_rejects_missing_or_invalid_connection_configuration(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(DirectCoreRuntimeCompositionError, match=message):
        build_direct_core_gateway_factory(_settings(**overrides))


@pytest.mark.parametrize("packaging", ("public_safe_shared", "undecided"))
def test_factory_rejects_packaging_without_secret_and_private_endpoint_dependencies(
    packaging: str,
) -> None:
    with pytest.raises(DirectCoreRuntimeCompositionError, match="configuration is invalid"):
        build_direct_core_gateway_factory(_settings(ai_packaging_candidate=packaging))


def test_missing_settings_attribute_fails_closed_without_leaking_values() -> None:
    settings = _settings()
    del settings.yonerai_core_origin

    with pytest.raises(DirectCoreRuntimeCompositionError) as caught:
        build_direct_core_gateway_factory(settings)

    assert str(caught.value) == "Direct Core runtime configuration is unavailable"


def test_factory_and_errors_do_not_expose_origin_or_token() -> None:
    origin = "https://private-core.example.test"
    token = "secret-offline-token"
    factory = build_direct_core_gateway_factory(_settings(yonerai_core_origin=origin, yonerai_auth_token=token))

    rendered = repr(factory)
    assert rendered == "DirectCoreGatewayFactory()"
    assert origin not in rendered
    assert token not in rendered

    with pytest.raises(DirectCoreRuntimeCompositionError) as caught:
        build_direct_core_gateway_factory(
            _settings(
                yonerai_core_origin=origin,
                yonerai_auth_token=f"{token}\n",
            )
        )
    assert origin not in str(caught.value)
    assert token not in str(caught.value)
