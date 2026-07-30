from __future__ import annotations

import base64
import binascii
import hashlib
import struct
import zlib
from pathlib import Path

import pytest

import yonerai_discord.modules.image_generation.provider_openai as provider_module
from yonerai_discord.modules.image_editing.domain import image_edit_output_binding
from yonerai_discord.modules.image_generation.artifacts import ImageArtifactStore
from yonerai_discord.modules.image_generation.domain import image_artifact_request_binding
from yonerai_discord.modules.image_generation.provider_openai import (
    AiohttpOpenAIImageTransport,
    OPENAI_IMAGES_PROVIDER_ID,
    OpenAIImageProviderAdapter,
    OpenAIImageProviderError,
    decode_openai_png,
)
from yonerai_discord.provider_registry import (
    ImageEditingInput,
    LogicalCapability,
    MediaGenerationInput,
    ProviderInvocation,
    ProviderRequest,
    QualityTier,
    ResourceProfile,
)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(*, metadata: bool = False) -> bytes:
    width = height = 64
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanlines = b"".join(b"\0" + bytes(width * 4) for _ in range(height))
    ancillary = _chunk(b"tEXt", b"private\x00metadata") if metadata else b""
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + ancillary
        + _chunk(b"IDAT", zlib.compress(scanlines))
        + _chunk(b"IEND", b"")
    )


class _Transport:
    def __init__(self) -> None:
        self.probes: list[str] = []
        self.generations: list[dict[str, object]] = []
        self.edits: list[tuple[dict[str, str], bytes]] = []
        self.response: object = {
            "created": 1,
            "data": [{"b64_json": base64.b64encode(_png(metadata=True)).decode("ascii")}],
            "output_format": "png",
            "quality": "medium",
            "size": "1024x1024",
        }
        self.closed = False

    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
        assert timeout_seconds == 5.0
        self.probes.append(model)
        return True

    async def generate_image(self, body, *, timeout_seconds: float):
        assert timeout_seconds == 30.0
        self.generations.append(dict(body))
        return self.response

    async def edit_image(self, fields, *, image: bytes, timeout_seconds: float):
        assert timeout_seconds == 30.0
        self.edits.append((dict(fields), image))
        return self.response

    async def close(self) -> None:
        self.closed = True


class _TimeoutTransport(_Transport):
    async def generate_image(self, body, *, timeout_seconds: float):
        self.generations.append(dict(body))
        raise TimeoutError("provider detail must not escape")


class _SourceReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls = 0

    async def read_source_png(self, request: ProviderRequest, source) -> bytes:
        assert request.input_artifacts == (source,)
        self.calls += 1
        return self.data


class _ByteStream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read(self, _maximum: int) -> bytes:
        payload, self.payload = self.payload, b""
        return payload


class _HttpResponse:
    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self.content = _ByteStream(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _HttpSession:
    def __init__(self, response: _HttpResponse, calls: list[dict[str, object]]) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


def _store(tmp_path: Path) -> ImageArtifactStore:
    root = tmp_path / "images"
    root.mkdir()
    return ImageArtifactStore(root)


def _invocation(model: str, tier: QualityTier = QualityTier.BALANCED) -> ProviderInvocation:
    return ProviderInvocation(
        provider_id=OPENAI_IMAGES_PROVIDER_ID,
        quality_tier=tier,
        model_alias=f"image.api.{tier.value}",
        provider_model=model,
        timeout_seconds=30.0,
        resources=ResourceProfile.remote(max_concurrency=1),
    )


def _generation_request() -> ProviderRequest:
    return ProviderRequest(
        request_id="image-request-100",
        trace_id="trace-image-100",
        capability=LogicalCapability.IMAGE_GENERATION,
        actor_ref="discord-user-30",
        payload=MediaGenerationInput("private prompt"),
        quality_tier=QualityTier.BALANCED,
    )


async def test_generation_uses_exact_wire_contract_and_commits_after_recheck(tmp_path: Path) -> None:
    transport = _Transport()
    store = _store(tmp_path)
    adapter = OpenAIImageProviderAdapter(transport, store)
    request = _generation_request()
    invocation = _invocation("gpt-image-2")
    checks = 0

    def allowed() -> bool:
        nonlocal checks
        checks += 1
        return True

    result = await adapter.execute(request, invocation, execution_allowed=allowed)

    assert transport.generations == [
        {
            "model": "gpt-image-2",
            "prompt": "private prompt",
            "n": 1,
            "size": "1024x1024",
            "quality": "medium",
            "output_format": "png",
        }
    ]
    assert checks == 2
    assert result.provider_id == OPENAI_IMAGES_PROVIDER_ID
    assert len(result.artifacts) == 1
    binding = image_artifact_request_binding(
        request,
        provider_id=invocation.provider_id,
        provider_model=invocation.provider_model,
        model_alias=invocation.model_alias,
        quality_tier=invocation.quality_tier,
    )
    stored = store.read_png(result.artifacts[0], request_binding=binding)
    assert b"private" not in stored


async def test_edit_uses_same_provider_and_verified_source(tmp_path: Path) -> None:
    transport = _Transport()
    store = _store(tmp_path)
    source_png = _png()
    source = store.put_png(source_png, request_binding="a" * 64)
    source_png = store.read_png(source, request_binding="a" * 64)
    reader = _SourceReader(source_png)
    adapter = OpenAIImageProviderAdapter(transport, store, reader)
    request = ProviderRequest(
        request_id="image-edit-100",
        trace_id="trace-edit-100",
        capability=LogicalCapability.IMAGE_EDITING,
        actor_ref="discord-user-30",
        payload=ImageEditingInput("private instruction", source_binding_digest="a" * 64),
        quality_tier=QualityTier.QUALITY,
        input_artifacts=(source,),
    )
    invocation = _invocation("gpt-image-2", QualityTier.QUALITY)
    checks = 0

    def allowed() -> bool:
        nonlocal checks
        checks += 1
        return True

    result = await adapter.execute(request, invocation, execution_allowed=allowed)

    assert reader.calls == 1
    assert transport.edits == [
        (
            {
                "model": "gpt-image-2",
                "prompt": "private instruction",
                "n": "1",
                "size": "1024x1024",
                "quality": "high",
                "output_format": "png",
            },
            source_png,
        )
    ]
    assert checks == 3
    assert result.artifacts[0].artifact_id != source.artifact_id
    binding = image_edit_output_binding(
        request,
        provider_id=invocation.provider_id,
        provider_model=invocation.provider_model,
        model_alias=invocation.model_alias,
        quality_tier=invocation.quality_tier,
    )
    assert store.read_png(result.artifacts[0], request_binding=binding)


async def test_source_integrity_failure_stops_before_external_request(tmp_path: Path) -> None:
    transport = _Transport()
    store = _store(tmp_path)
    source_png = _png()
    source = store.put_png(source_png, request_binding="b" * 64)
    adapter = OpenAIImageProviderAdapter(transport, store, _SourceReader(_png(metadata=True)))
    request = ProviderRequest(
        request_id="image-edit-integrity",
        trace_id="trace-edit-integrity",
        capability=LogicalCapability.IMAGE_EDITING,
        actor_ref="discord-user-30",
        payload=ImageEditingInput("edit", source_binding_digest="b" * 64),
        input_artifacts=(source,),
    )

    with pytest.raises(OpenAIImageProviderError, match="integrity"):
        await adapter.execute(
            request,
            _invocation("gpt-image-2"),
            execution_allowed=lambda: True,
        )
    assert transport.edits == []


async def test_revocation_after_response_prevents_artifact_commit(tmp_path: Path) -> None:
    transport = _Transport()
    store = _store(tmp_path)
    adapter = OpenAIImageProviderAdapter(transport, store)
    calls = 0

    def allowed() -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    with pytest.raises(Exception):
        await adapter.execute(
            _generation_request(),
            _invocation("gpt-image-2"),
            execution_allowed=allowed,
        )
    assert len(transport.generations) == 1
    assert not list((tmp_path / "images").glob("*.png"))


async def test_provider_timeout_remains_typed_and_sanitized(tmp_path: Path) -> None:
    transport = _TimeoutTransport()
    adapter = OpenAIImageProviderAdapter(transport, _store(tmp_path))

    with pytest.raises(TimeoutError) as captured:
        await adapter.execute(
            _generation_request(),
            _invocation("gpt-image-2"),
            execution_allowed=lambda: True,
        )

    assert len(transport.generations) == 1
    assert "provider detail" not in str(captured.value)
    assert not list((tmp_path / "images").glob("*.png"))


async def test_generation_rejects_input_artifact_before_http(tmp_path: Path) -> None:
    transport = _Transport()
    store = _store(tmp_path)
    source = store.put_png(_png(), request_binding="d" * 64)
    request = ProviderRequest(
        request_id="image-request-with-input",
        trace_id="trace-image-with-input",
        capability=LogicalCapability.IMAGE_GENERATION,
        actor_ref="discord-user-30",
        payload=MediaGenerationInput("private prompt"),
        input_artifacts=(source,),
    )
    adapter = OpenAIImageProviderAdapter(transport, store)

    with pytest.raises(OpenAIImageProviderError, match="does not accept"):
        await adapter.execute(
            request,
            _invocation("gpt-image-2"),
            execution_allowed=lambda: True,
        )

    assert transport.generations == []


@pytest.mark.parametrize(
    "response",
    [
        {"data": [{"url": "https://example.test/image.png"}], "output_format": "png"},
        {"data": [], "output_format": "png"},
        {"data": [{"b64_json": "not-base64!"}], "output_format": "png"},
        {"data": [{"b64_json": base64.b64encode(b"not-png").decode()}], "output_format": "png"},
        {"data": [{"b64_json": base64.b64encode(_png()).decode()}], "unknown": True},
        {"data": [{"b64_json": base64.b64encode(_png()).decode()}], "output_format": "jpeg"},
    ],
)
def test_response_contract_rejects_url_invalid_or_unknown_output(response: object) -> None:
    with pytest.raises(OpenAIImageProviderError):
        decode_openai_png(response)  # type: ignore[arg-type]


async def test_health_probes_configured_models_and_close_is_sanitized(tmp_path: Path) -> None:
    transport = _Transport()
    adapter = OpenAIImageProviderAdapter(
        transport,
        _store(tmp_path),
        probed_model_aliases=("image.balanced", "image.edit.balanced"),
    )
    health = await adapter.health()
    await adapter.close()

    assert transport.probes == ["gpt-image-2"]
    assert health.status.value == "ready"
    assert health.probed_model_aliases == ("image.balanced", "image.edit.balanced")
    assert transport.closed is True
    actual = AiohttpOpenAIImageTransport("sk-test-not-used")
    assert "sk-test-not-used" not in repr(actual)


@pytest.mark.parametrize("model", ["dall-e-3", "../gpt-image-2", "gpt-image-2/extra"])
async def test_non_gpt_image_model_or_path_is_rejected_before_http(tmp_path: Path, model: str) -> None:
    transport = _Transport()
    adapter = OpenAIImageProviderAdapter(transport, _store(tmp_path))
    with pytest.raises(OpenAIImageProviderError):
        await adapter.execute(
            _generation_request(),
            _invocation(model),
            execution_allowed=lambda: True,
        )
    assert transport.generations == []


async def test_unprobed_model_is_rejected_before_http(tmp_path: Path) -> None:
    transport = _Transport()
    adapter = OpenAIImageProviderAdapter(transport, _store(tmp_path))

    with pytest.raises(OpenAIImageProviderError, match="unavailable"):
        await adapter.execute(
            _generation_request(),
            _invocation("gpt-image-2-2026-04-21"),
            execution_allowed=lambda: True,
        )
    assert transport.generations == []


async def test_close_makes_health_and_execute_fail_closed(tmp_path: Path) -> None:
    transport = _Transport()
    adapter = OpenAIImageProviderAdapter(transport, _store(tmp_path))
    await adapter.close()

    health = await adapter.health()
    with pytest.raises(OpenAIImageProviderError, match="unavailable"):
        await adapter.execute(
            _generation_request(),
            _invocation("gpt-image-2"),
            execution_allowed=lambda: True,
        )

    assert health.status.value == "unavailable"
    assert health.detail_code == "adapter_closing"
    assert transport.probes == []
    assert transport.generations == []


async def test_edit_revocation_before_source_read_stops_locally(tmp_path: Path) -> None:
    transport = _Transport()
    store = _store(tmp_path)
    source = store.put_png(_png(), request_binding="c" * 64)
    reader = _SourceReader(store.read_png(source, request_binding="c" * 64))
    adapter = OpenAIImageProviderAdapter(transport, store, reader)
    request = ProviderRequest(
        request_id="image-edit-revoked",
        trace_id="trace-edit-revoked",
        capability=LogicalCapability.IMAGE_EDITING,
        actor_ref="discord-user-30",
        payload=ImageEditingInput("edit", source_binding_digest="c" * 64),
        input_artifacts=(source,),
    )

    with pytest.raises(Exception):
        await adapter.execute(
            request,
            _invocation("gpt-image-2"),
            execution_allowed=lambda: False,
        )

    assert reader.calls == 0
    assert transport.edits == []


def test_sensitive_values_are_not_in_adapter_or_error_repr(tmp_path: Path) -> None:
    transport = _Transport()
    adapter = OpenAIImageProviderAdapter(transport, _store(tmp_path))
    assert "private prompt" not in repr(adapter)
    error = OpenAIImageProviderError("image provider request failed")
    assert "private prompt" not in repr(error)
    assert hashlib.sha256(b"private prompt").hexdigest() not in repr(error)


async def test_aiohttp_transport_uses_fixed_generation_endpoint_and_no_redirect(monkeypatch) -> None:
    payload = json_bytes(
        {
            "data": [{"b64_json": base64.b64encode(_png()).decode("ascii")}],
            "output_format": "png",
        }
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: _HttpSession(_HttpResponse(payload), calls),
    )
    transport = AiohttpOpenAIImageTransport("test-secret-key")

    result = await transport.generate_image(
        {
            "model": "gpt-image-2",
            "prompt": "private",
            "n": 1,
            "size": "1024x1024",
            "quality": "medium",
            "output_format": "png",
        },
        timeout_seconds=30.0,
    )

    assert result["output_format"] == "png"
    assert calls[0]["url"] == "https://api.openai.com/v1/images/generations"
    assert calls[0]["allow_redirects"] is False
    assert calls[0]["headers"] == {"Authorization": "Bearer test-secret-key"}
    assert "test-secret-key" not in repr(transport)


async def test_aiohttp_transport_uses_fixed_edit_endpoint_and_png_part(monkeypatch) -> None:
    payload = json_bytes(
        {
            "data": [{"b64_json": base64.b64encode(_png()).decode("ascii")}],
            "output_format": "png",
        }
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: _HttpSession(_HttpResponse(payload), calls),
    )
    transport = AiohttpOpenAIImageTransport("test-secret-key")

    result = await transport.edit_image(
        {
            "model": "gpt-image-2",
            "prompt": "private",
            "n": "1",
            "size": "1024x1024",
            "quality": "medium",
            "output_format": "png",
        },
        image=_png(),
        timeout_seconds=30.0,
    )

    form = calls[0]["data"]
    assert isinstance(form, provider_module.aiohttp.FormData)
    assert [field[0]["name"] for field in form._fields] == [  # noqa: SLF001
        "model",
        "prompt",
        "n",
        "size",
        "quality",
        "output_format",
        "image",
    ]
    assert result["output_format"] == "png"
    assert calls[0]["url"] == "https://api.openai.com/v1/images/edits"
    assert calls[0]["allow_redirects"] is False


async def test_aiohttp_transport_rejects_non_json_without_exposing_body(monkeypatch) -> None:
    response = _HttpResponse(b"private-provider-error")
    response.headers = {"Content-Type": "text/plain"}
    monkeypatch.setattr(
        provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: _HttpSession(response, []),
    )
    transport = AiohttpOpenAIImageTransport("test-secret-key")

    with pytest.raises(OpenAIImageProviderError) as captured:
        await transport.generate_image(
            {
                "model": "gpt-image-2",
                "prompt": "private",
                "n": 1,
                "size": "1024x1024",
                "quality": "medium",
                "output_format": "png",
            },
            timeout_seconds=30.0,
        )
    assert "private-provider-error" not in str(captured.value)
    assert "test-secret-key" not in str(captured.value)


async def test_aiohttp_transport_rejects_unknown_request_fields_before_http(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        provider_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: _HttpSession(_HttpResponse(b"{}"), calls),
    )
    transport = AiohttpOpenAIImageTransport("test-secret-key")

    with pytest.raises(ValueError, match="body"):
        await transport.generate_image(
            {
                "model": "gpt-image-2",
                "prompt": "private",
                "n": 1,
                "size": "1024x1024",
                "quality": "medium",
                "output_format": "png",
                "unexpected": "value",
            },
            timeout_seconds=30.0,
        )

    assert calls == []


def json_bytes(value: object) -> bytes:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
