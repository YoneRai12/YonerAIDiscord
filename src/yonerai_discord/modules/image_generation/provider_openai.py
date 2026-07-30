from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import quote

import aiohttp

from yonerai_discord.modules.image_generation.artifacts import MAX_PNG_BYTES, canonicalize_png
from yonerai_discord.provider_registry import (
    ArtifactRef,
    HealthStatus,
    ImageEditingInput,
    LogicalCapability,
    MediaGenerationInput,
    ProviderHealth,
    ProviderInvocation,
    ProviderRequest,
    ProviderResult,
    QualityTier,
    require_execution_allowed,
)
from yonerai_discord.provider_registry.domain import normalize_identifier, utc_now
from yonerai_discord.provider_registry.ports import ExecutionAuthorizationCheck

from .domain import image_artifact_request_binding
from .ports import ImageArtifactStore


OPENAI_IMAGES_PROVIDER_ID = "openai-images-api"
OPENAI_IMAGES_ADAPTER_ID = "openai.images.v1"
OPENAI_API_ORIGIN = "https://api.openai.com"
_MAX_JSON_RESPONSE_BYTES = 12 * 1024 * 1024
_MODEL_ID = re.compile(r"gpt-image-[a-z0-9.-]{1,80}\Z")
_QUALITY = {
    QualityTier.FAST: "low",
    QualityTier.BALANCED: "medium",
    QualityTier.QUALITY: "high",
}
_ROOT_RESPONSE_KEYS = frozenset({"created", "background", "data", "output_format", "quality", "size", "usage"})
_IMAGE_RESPONSE_KEYS = frozenset({"b64_json", "revised_prompt", "url"})


class OpenAIImageProviderError(RuntimeError):
    """Prompt、response、credentialを例外へ含めないprovider境界エラー。"""


class OpenAIImageProviderTimeoutError(TimeoutError):
    """Provider timeoutをRegistryのtimed-out監査へ保ったまま通知する。"""


class OpenAIImageHttpTransport(Protocol):
    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool: ...

    async def generate_image(
        self,
        body: Mapping[str, object],
        *,
        timeout_seconds: float,
    ) -> Mapping[str, object]: ...

    async def edit_image(
        self,
        fields: Mapping[str, str],
        *,
        image: bytes,
        timeout_seconds: float,
    ) -> Mapping[str, object]: ...

    async def close(self) -> None: ...


class ImageEditSourceBytesPort(Protocol):
    async def read_source_png(
        self,
        request: ProviderRequest,
        source: ArtifactRef,
    ) -> bytes: ...


class AiohttpOpenAIImageTransport:
    """固定OpenAI originだけへ接続する実HTTP transport。"""

    def __init__(
        self,
        api_key: str,
        *,
        origin: str = OPENAI_API_ORIGIN,
        max_response_bytes: int = _MAX_JSON_RESPONSE_BYTES,
    ) -> None:
        if not isinstance(api_key, str) or not api_key or len(api_key) > 4_096:
            raise ValueError("api_key is unavailable")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in api_key):
            raise ValueError("api_key is invalid")
        if origin != OPENAI_API_ORIGIN:
            raise ValueError("OpenAI image transport origin is fixed")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1_024 <= max_response_bytes <= 32 * 1024 * 1024
        ):
            raise ValueError("max_response_bytes is outside the allowed range")
        self._api_key = api_key
        self._origin = origin
        self._max_response_bytes = max_response_bytes

    def __repr__(self) -> str:
        return "AiohttpOpenAIImageTransport()"

    async def probe_model(self, model: str, *, timeout_seconds: float) -> bool:
        model = _model_id(model)
        timeout = _timeout(timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(
                    f"{self._origin}/v1/models/{quote(model, safe='')}",
                    headers=self._headers(),
                    allow_redirects=False,
                ) as response:
                    return response.status == 200
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def generate_image(
        self,
        body: Mapping[str, object],
        *,
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        _validate_generation_body(body)
        return await self._post(
            "/v1/images/generations",
            json_body=dict(body),
            timeout_seconds=timeout_seconds,
        )

    async def edit_image(
        self,
        fields: Mapping[str, str],
        *,
        image: bytes,
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        _validate_edit_fields(fields)
        if not isinstance(image, bytes) or not image or len(image) > MAX_PNG_BYTES:
            raise ValueError("image is outside the allowed range")
        form = aiohttp.FormData()
        for key, value in fields.items():
            form.add_field(key, value)
        form.add_field(
            "image",
            image,
            filename="source.png",
            content_type="image/png",
        )
        return await self._post(
            "/v1/images/edits",
            form=form,
            timeout_seconds=timeout_seconds,
        )

    async def close(self) -> None:
        return None

    async def _post(
        self,
        path: str,
        *,
        timeout_seconds: float,
        json_body: Mapping[str, object] | None = None,
        form: aiohttp.FormData | None = None,
    ) -> Mapping[str, object]:
        if path not in {"/v1/images/generations", "/v1/images/edits"}:
            raise OpenAIImageProviderError("image endpoint is not allowed")
        timeout = _timeout(timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(
                    f"{self._origin}{path}",
                    headers=self._headers(),
                    json=json_body,
                    data=form,
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise OpenAIImageProviderError("image provider request failed")
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "application/json" not in content_type:
                        raise OpenAIImageProviderError("image provider response type is invalid")
                    payload = await _read_bounded(response.content, self._max_response_bytes)
        except asyncio.CancelledError:
            raise
        except OpenAIImageProviderError:
            raise
        except TimeoutError:
            raise OpenAIImageProviderTimeoutError("image provider request timed out") from None
        except Exception:
            raise OpenAIImageProviderError("image provider request failed") from None
        return _strict_json_object(payload)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}


class OpenAIImageProviderAdapter:
    def __init__(
        self,
        transport: OpenAIImageHttpTransport,
        artifact_store: ImageArtifactStore,
        source_reader: ImageEditSourceBytesPort | None = None,
        *,
        health_models: tuple[str, ...] = ("gpt-image-2",),
        probed_model_aliases: tuple[str, ...] = (),
    ) -> None:
        if not _transport_valid(transport):
            raise TypeError("transport does not implement the OpenAI image HTTP contract")
        if not callable(getattr(artifact_store, "put_png", None)):
            raise TypeError("artifact_store must expose put_png")
        self._transport = transport
        self._artifact_store = artifact_store
        if source_reader is not None and not callable(getattr(source_reader, "read_source_png", None)):
            raise TypeError("source_reader must expose read_source_png")
        models = tuple(_model_id(model) for model in health_models)
        if not models or len(models) > 4 or len(set(models)) != len(models):
            raise ValueError("health_models must contain 1 to 4 unique models")
        aliases = tuple(normalize_identifier(alias, label="probed model alias") for alias in probed_model_aliases)
        if len(aliases) > 16 or len(set(aliases)) != len(aliases):
            raise ValueError("probed_model_aliases must contain at most 16 unique aliases")
        self._source_reader = source_reader
        self._health_models = models
        self._probed_model_aliases = aliases
        self._closing = False

    @property
    def provider_id(self) -> str:
        return OPENAI_IMAGES_PROVIDER_ID

    @property
    def adapter_id(self) -> str:
        return OPENAI_IMAGES_ADAPTER_ID

    async def health(self) -> ProviderHealth:
        if self._closing:
            return ProviderHealth(
                provider_id=self.provider_id,
                status=HealthStatus.UNAVAILABLE,
                checked_at=utc_now(),
                detail_code="adapter_closing",
            )
        try:
            async with asyncio.timeout(5.0):
                probes = await asyncio.gather(
                    *(self._transport.probe_model(model, timeout_seconds=5.0) for model in self._health_models)
                )
            ready = all(value is True for value in probes)
        except asyncio.CancelledError:
            raise
        except Exception:
            ready = False
        return ProviderHealth(
            provider_id=self.provider_id,
            status=HealthStatus.READY if ready else HealthStatus.UNAVAILABLE,
            checked_at=utc_now(),
            detail_code=None if ready else "model_probe_failed",
            probed_model_aliases=self._probed_model_aliases if ready else (),
        )

    async def execute(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        execution_allowed=None,
    ) -> ProviderResult:
        _require_invocation(invocation, self.provider_id)
        model = _model_id(invocation.provider_model)
        if self._closing or model not in self._health_models:
            raise OpenAIImageProviderError("image provider is unavailable")
        if request.capability is LogicalCapability.IMAGE_GENERATION and isinstance(
            request.payload, MediaGenerationInput
        ):
            if request.input_artifacts:
                raise OpenAIImageProviderError("image generation does not accept input artifacts")
            return await self._generate(
                request,
                invocation,
                model=model,
                execution_allowed=execution_allowed,
            )
        if request.capability is LogicalCapability.IMAGE_EDITING and isinstance(request.payload, ImageEditingInput):
            return await self._edit(
                request,
                invocation,
                model=model,
                execution_allowed=execution_allowed,
            )
        raise OpenAIImageProviderError("image provider request is invalid")

    async def _generate(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        model: str,
        execution_allowed: ExecutionAuthorizationCheck | None,
    ) -> ProviderResult:
        assert isinstance(request.payload, MediaGenerationInput)
        body = {
            "model": model,
            "prompt": request.payload.prompt,
            "n": 1,
            "size": "1024x1024",
            "quality": _QUALITY[invocation.quality_tier],
            "output_format": "png",
        }
        await require_execution_allowed(execution_allowed)
        try:
            async with asyncio.timeout(invocation.timeout_seconds):
                response = await self._transport.generate_image(
                    body,
                    timeout_seconds=invocation.timeout_seconds,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise OpenAIImageProviderTimeoutError("image generation timed out") from None
        except OpenAIImageProviderError:
            raise
        except Exception:
            raise OpenAIImageProviderError("image generation failed") from None
        png = decode_openai_png(response)
        request_binding = image_artifact_request_binding(
            request,
            provider_id=invocation.provider_id,
            provider_model=invocation.provider_model,
            model_alias=invocation.model_alias,
            quality_tier=invocation.quality_tier,
        )
        await require_execution_allowed(execution_allowed)
        try:
            artifact = self._artifact_store.put_png(png, request_binding=request_binding)
        except Exception:
            raise OpenAIImageProviderError("image artifact commit failed") from None
        return ProviderResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            provider_model=model,
            artifacts=(artifact,),
        )

    async def _edit(
        self,
        request: ProviderRequest,
        invocation: ProviderInvocation,
        *,
        model: str,
        execution_allowed: ExecutionAuthorizationCheck | None,
    ) -> ProviderResult:
        from yonerai_discord.modules.image_editing.domain import image_edit_output_binding

        if len(request.input_artifacts) != 1 or self._source_reader is None:
            raise OpenAIImageProviderError("image editing source is unavailable")
        assert isinstance(request.payload, ImageEditingInput)
        if request.payload.source_binding_digest is None:
            raise OpenAIImageProviderError("image editing source binding is unavailable")
        source_ref = request.input_artifacts[0]
        await require_execution_allowed(execution_allowed)
        try:
            source = self._source_reader.read_source_png(request, source_ref)
            if inspect.isawaitable(source):
                source = await source
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OpenAIImageProviderError("source image is unavailable") from None
        if not isinstance(source, bytes) or not source or len(source) > MAX_PNG_BYTES:
            raise OpenAIImageProviderError("source image is unavailable")
        try:
            canonical_source = canonicalize_png(source).data
        except Exception:
            raise OpenAIImageProviderError("source image is invalid") from None
        if (
            canonical_source != source
            or source_ref.size_bytes != len(source)
            or source_ref.sha256 != hashlib.sha256(source).hexdigest()
        ):
            raise OpenAIImageProviderError("source image integrity is invalid")
        fields = {
            "model": model,
            "prompt": request.payload.instruction,
            "n": "1",
            "size": "1024x1024",
            "quality": _QUALITY[invocation.quality_tier],
            "output_format": "png",
        }
        await require_execution_allowed(execution_allowed)
        try:
            async with asyncio.timeout(invocation.timeout_seconds):
                response = await self._transport.edit_image(
                    fields,
                    image=source,
                    timeout_seconds=invocation.timeout_seconds,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise OpenAIImageProviderTimeoutError("image editing timed out") from None
        except OpenAIImageProviderError:
            raise
        except Exception:
            raise OpenAIImageProviderError("image editing failed") from None
        png = decode_openai_png(response)
        request_binding = image_edit_output_binding(
            request,
            provider_id=invocation.provider_id,
            provider_model=invocation.provider_model,
            model_alias=invocation.model_alias,
            quality_tier=invocation.quality_tier,
        )
        await require_execution_allowed(execution_allowed)
        try:
            artifact = self._artifact_store.put_png(png, request_binding=request_binding)
        except Exception:
            raise OpenAIImageProviderError("edited image artifact commit failed") from None
        return ProviderResult(
            request_id=request.request_id,
            provider_id=self.provider_id,
            provider_model=model,
            artifacts=(artifact,),
        )

    def begin_close(self) -> None:
        self._closing = True

    async def close(self) -> None:
        self.begin_close()
        await self._transport.close()


def decode_openai_png(response: Mapping[str, object]) -> bytes:
    if not isinstance(response, Mapping) or not set(response).issubset(_ROOT_RESPONSE_KEYS):
        raise OpenAIImageProviderError("image provider response is invalid")
    if response.get("output_format", "png") != "png":
        raise OpenAIImageProviderError("image provider output format is invalid")
    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1:
        raise OpenAIImageProviderError("image provider output count is invalid")
    item = data[0]
    if not isinstance(item, Mapping) or not set(item).issubset(_IMAGE_RESPONSE_KEYS):
        raise OpenAIImageProviderError("image provider output item is invalid")
    if "url" in item:
        raise OpenAIImageProviderError("URL image output is not accepted")
    encoded = item.get("b64_json")
    if not isinstance(encoded, str) or not encoded or len(encoded) > ((MAX_PNG_BYTES + 2) // 3) * 4:
        raise OpenAIImageProviderError("image provider payload is invalid")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise OpenAIImageProviderError("image provider payload is invalid") from None
    try:
        return canonicalize_png(decoded).data
    except Exception:
        raise OpenAIImageProviderError("image provider PNG is invalid") from None


async def _read_bounded(stream: Any, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(65_536, maximum + 1 - total))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise OpenAIImageProviderError("image provider response is invalid")
        total += len(chunk)
        if total > maximum:
            raise OpenAIImageProviderError("image provider response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _strict_json_object(payload: bytes) -> Mapping[str, object]:
    try:
        decoded = payload.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid numeric constant")),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise OpenAIImageProviderError("image provider response is invalid") from None
    if not isinstance(value, dict):
        raise OpenAIImageProviderError("image provider response is invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _transport_valid(transport: object) -> bool:
    return all(
        callable(getattr(transport, name, None)) for name in ("probe_model", "generate_image", "edit_image", "close")
    )


def _validate_generation_body(body: Mapping[str, object]) -> None:
    if not isinstance(body, Mapping) or set(body) != {
        "model",
        "prompt",
        "n",
        "size",
        "quality",
        "output_format",
    }:
        raise ValueError("image generation body is invalid")
    _model_id(body["model"])
    _prompt(body["prompt"])
    if (
        isinstance(body["n"], bool)
        or body["n"] != 1
        or body["size"] != "1024x1024"
        or body["quality"] not in frozenset(_QUALITY.values())
        or body["output_format"] != "png"
    ):
        raise ValueError("image generation body is invalid")


def _validate_edit_fields(fields: Mapping[str, str]) -> None:
    if not isinstance(fields, Mapping) or set(fields) != {
        "model",
        "prompt",
        "n",
        "size",
        "quality",
        "output_format",
    }:
        raise ValueError("image editing fields are invalid")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in fields.items()):
        raise TypeError("image editing fields must be strings")
    _model_id(fields["model"])
    _prompt(fields["prompt"])
    if (
        fields["n"] != "1"
        or fields["size"] != "1024x1024"
        or fields["quality"] not in frozenset(_QUALITY.values())
        or fields["output_format"] != "png"
    ):
        raise ValueError("image editing fields are invalid")


def _prompt(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("image prompt must be a string")
    if (
        not value
        or len(value) > 50_000
        or any(ord(character) < 0x20 and character not in {"\n", "\t"} for character in value)
        or "\x7f" in value
    ):
        raise ValueError("image prompt is outside the allowed range")
    return value


def _model_id(value: object) -> str:
    if not isinstance(value, str) or not _MODEL_ID.fullmatch(value):
        raise OpenAIImageProviderError("provider model is not an allowed GPT Image model")
    return value


def _timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1.0 <= float(value) <= 900.0:
        raise ValueError("timeout_seconds is outside the allowed range")
    return float(value)


def _require_invocation(invocation: ProviderInvocation, provider_id: str) -> None:
    if not isinstance(invocation, ProviderInvocation) or invocation.provider_id != provider_id:
        raise OpenAIImageProviderError("provider invocation is invalid")


__all__ = [
    "AiohttpOpenAIImageTransport",
    "OPENAI_API_ORIGIN",
    "OPENAI_IMAGES_ADAPTER_ID",
    "OPENAI_IMAGES_PROVIDER_ID",
    "ImageEditSourceBytesPort",
    "OpenAIImageProviderAdapter",
    "OpenAIImageHttpTransport",
    "OpenAIImageProviderError",
    "OpenAIImageProviderTimeoutError",
    "decode_openai_png",
]
