from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import aiohttp

from yonerai_discord.ai_control import ModelName, TaskProfile, build_responses_request, route_model
from yonerai_discord.secret_policy import strong_safety_identifier_secret

from .models import (
    AISource,
    AIReply,
    AIRequest,
    Attachment,
    AttachmentKind,
    DataBoundary,
    MessageRole,
    Turn,
)
from .ports import ProviderAuthorizationError, _verify_service_sink_async
from .service import ProviderRetryableError
from .bounded_tools import fixed_web_search_payload_fragment


class ProviderConfigurationError(ValueError):
    pass


class ProviderWebSearchUnavailableError(ProviderRetryableError):
    """A bounded read-only web search attempt failed transiently."""


_PROVIDER_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_LOGICAL_MODEL_NAMES = {
    "ai.fast": ModelName.LUNA,
    "ai.balanced": ModelName.TERRA,
    "ai.quality": ModelName.SOL,
}


def _provider_model_id(value: str, *, setting: str) -> str:
    if not isinstance(value, str) or not _PROVIDER_MODEL_ID.fullmatch(value.strip()):
        raise ProviderConfigurationError(f"{setting} contains an invalid provider model ID")
    return value.strip()


def ai_provider_endpoint_is_local(value: str) -> bool:
    """Provider構成とprofile preflightが共有するAI endpoint locality正本。"""

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigurationError("AI_BASE_URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderConfigurationError("AI_BASE_URL must not contain credentials, query, or fragment")
    if parsed.path.rstrip("/") != "/v1":
        raise ProviderConfigurationError("AI_BASE_URL path must be /v1")
    try:
        parsed.port
    except ValueError as exc:
        raise ProviderConfigurationError("AI_BASE_URL contains an invalid port") from exc
    if parsed.hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        openai_api_key: str = "",
        compatible_api_key: str = "",
        allow_remote: bool = False,
        allow_luna: bool = True,
        enable_web_search: bool = False,
        enable_attachments: bool = False,
        fast_model: str = ModelName.LUNA.value,
        balanced_model: str = ModelName.TERRA.value,
        quality_model: str = ModelName.SOL.value,
        safety_identifier_secret: str = "",
        timeout_seconds: float = 20.0,
        max_output_tokens: int = 2_048,
        max_response_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._is_local = ai_provider_endpoint_is_local(self._base_url)
        if not self._is_local and not allow_remote:
            raise ProviderConfigurationError("remote AI endpoint requires AI_ALLOW_REMOTE=true")
        if not self._is_local:
            parsed = urlparse(self._base_url)
            if (
                parsed.scheme != "https"
                or parsed.hostname is None
                or parsed.hostname.lower() != "api.openai.com"
                or parsed.port not in {None, 443}
            ):
                raise ProviderConfigurationError(
                    "remote AI_BASE_URL must be the official https://api.openai.com/v1 endpoint"
                )
        self._api_key = compatible_api_key if self._is_local else openai_api_key
        if not self._is_local and not self._api_key:
            raise ProviderConfigurationError("OPENAI_API_KEY is required for a remote endpoint")
        if (
            not self._is_local
            and safety_identifier_secret
            and not strong_safety_identifier_secret(safety_identifier_secret)
        ):
            raise ProviderConfigurationError("AI_SAFETY_IDENTIFIER_SECRET does not meet the remote minimum strength")
        self._allow_luna = allow_luna
        self._web_search_enabled = bool(enable_web_search)
        self._attachments_enabled = bool(enable_attachments)
        self._model_ids = {
            ModelName.LUNA: _provider_model_id(fast_model, setting="AI_MODEL_FAST"),
            ModelName.TERRA: _provider_model_id(balanced_model, setting="AI_MODEL_BALANCED"),
            ModelName.SOL: _provider_model_id(quality_model, setting="AI_MODEL_QUALITY"),
        }
        identifier_key = safety_identifier_secret or self._api_key or "yonerai-discord-suite-local"
        self._identifier_key = identifier_key.encode("utf-8")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or not 128 <= max_output_tokens <= 8_192
        ):
            raise ProviderConfigurationError("AI_MAX_OUTPUT_TOKENS is outside the allowed range")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 65_536 <= max_response_bytes <= 4 * 1024 * 1024
        ):
            raise ProviderConfigurationError("AI_MAX_RESPONSE_BYTES is outside the allowed range")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 5.0 <= float(timeout_seconds) <= 120.0
        ):
            raise ProviderConfigurationError("AI_TIMEOUT_SECONDS is outside the allowed range")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        # PluginManagerの最小停止猶予（2秒）より短く、受付停止後のrequestだけを短くdrainする。
        self._close_drain_timeout_seconds = 1.0
        self._max_output_tokens = max_output_tokens
        self._max_response_bytes = max_response_bytes
        self._session: aiohttp.ClientSession | None = None
        self._closed = False
        self._inflight = 0
        self._lifecycle_lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def is_local(self) -> bool:
        return self._is_local

    @property
    def supports_web_search(self) -> bool:
        """現行adapterでは公式Responses APIだけがWeb検索toolを提供する。"""

        return self._web_search_enabled and not self._is_local

    @property
    def supports_attachments(self) -> bool:
        """Compositionで明示許可されたmultimodal inputだけを受け入れる。"""

        return self._attachments_enabled

    @property
    def runtime_provider_id(self) -> str:
        """Stable non-secret ID used by the canonical runtime catalog."""

        return "provider.local.openai-compatible" if self._is_local else "provider.openai.responses"

    @property
    def runtime_model_bindings(self) -> dict[str, str]:
        """Logical aliases to configured provider model IDs, with no credentials."""

        fast = ModelName.LUNA if self._allow_luna else ModelName.TERRA
        return {
            "ai.fast": self._model_ids[fast],
            "ai.balanced": self._model_ids[ModelName.TERRA],
            "ai.quality": self._model_ids[ModelName.SOL],
        }

    def resolved_model_alias(self, request: AIRequest) -> str:
        """Resolve the logical alias before sealing a bounded execution request."""

        if request.effective_model_alias is not None:
            requested = _LOGICAL_MODEL_NAMES.get(request.effective_model_alias)
            if requested is None:
                raise RuntimeError("resolved model alias is not supported by this adapter")
            if requested is ModelName.LUNA and not self._allow_luna:
                return "ai.balanced"
            return request.effective_model_alias
        decision = route_model(
            TaskProfile(
                kind=request.task_kind,
                complexity=request.complexity,
                risk=request.risk,
                uses_tools=request.uses_tools or request.web_search,
                has_side_effects=request.has_side_effects,
            )
        )
        if decision.model is ModelName.LUNA and not self._allow_luna:
            return "ai.balanced"
        return {
            ModelName.LUNA: "ai.fast",
            ModelName.TERRA: "ai.balanced",
            ModelName.SOL: "ai.quality",
        }[decision.model]

    def _safety_identifier(self, request: AIRequest) -> str:
        scope = f"guild:{request.guild_id}" if request.guild_id is not None else "dm"
        identity = f"{scope}:user:{request.user_id}".encode("ascii")
        return hmac.new(self._identifier_key, identity, hashlib.sha256).hexdigest()

    async def complete(self, request: AIRequest) -> AIReply:
        if not self._is_local:
            raise ProviderAuthorizationError("remote AI requests require AIService sink authorization")
        if request.bounded_toolset is not None:
            raise ProviderAuthorizationError("formal AI requests require sink authorization")
        return await self._complete(request, provider_sink_verifier=None)

    async def complete_authorized(
        self,
        request: AIRequest,
        provider_sink_verifier: object,
    ) -> AIReply:
        """Recheck remote authorization at the closest pre-HTTP boundary."""

        return await self._complete(request, provider_sink_verifier=provider_sink_verifier)

    async def _complete(
        self,
        request: AIRequest,
        *,
        provider_sink_verifier: object | None,
    ) -> AIReply:
        if _request_contains_attachments(request) and not self.supports_attachments:
            raise ProviderAuthorizationError("AI attachment input is not configured")
        if (not self._is_local or request.bounded_toolset is not None) and provider_sink_verifier is None:
            raise ProviderAuthorizationError("AI provider sink authorization is required")
        session = await self._begin_request()
        try:
            if not self._is_local and request.boundary is not DataBoundary.REMOTE_OPT_IN:
                raise ProviderAuthorizationError("remote AI data boundary is not authorized")
            headers = {"content-type": "application/json"}
            if self._api_key:
                headers["authorization"] = f"Bearer {self._api_key}"
            profile = TaskProfile(
                kind=request.task_kind,
                complexity=request.complexity,
                risk=request.risk,
                uses_tools=request.uses_tools or request.web_search,
                has_side_effects=request.has_side_effects,
            )
            decision = route_model(profile)
            model_override = None
            if request.effective_model_alias is not None:
                requested = _LOGICAL_MODEL_NAMES.get(request.effective_model_alias)
                if requested is None:
                    raise RuntimeError("resolved model alias is not supported by this adapter")
                if requested is ModelName.LUNA and not self._allow_luna:
                    requested = ModelName.TERRA
                model_override = requested.value
            elif decision.model is ModelName.LUNA and not self._allow_luna:
                model_override = ModelName.TERRA.value
            spec = build_responses_request(
                input=request.prompt,
                instructions=request.system_prompt,
                profile=profile,
                metadata=request.metadata,
                model_override=model_override,
            )
            payload = spec.to_payload()
            payload["model"] = self._model_ids[spec.decision.model]
            payload["input"] = _responses_input(request)
            payload["safety_identifier"] = self._safety_identifier(request)
            payload["max_output_tokens"] = self._max_output_tokens
            if request.allowed_model_tools:
                toolset = request.bounded_toolset
                authorization = request.tool_execution_authorization
                if toolset is None or authorization is None:
                    raise ProviderAuthorizationError("bounded model-tool authorization is required")
                if request.allowed_model_tools != toolset.effective_tools or not request.web_search:
                    raise RuntimeError("model tool allowlist does not match the bounded toolset")
                if not self.supports_web_search:
                    raise RuntimeError("web search provider is not configured")
                if (
                    authorization.provider_id != self.runtime_provider_id
                    or authorization.model_alias != self.resolved_model_alias(request)
                ):
                    raise ProviderAuthorizationError("bounded provider/model binding changed")
                payload.update(fixed_web_search_payload_fragment(toolset))
            await self._authorize_http_boundary(
                request=request,
                provider_sink_verifier=provider_sink_verifier,
            )
            data = await self._post_responses(
                session=session,
                request=request,
                headers=headers,
                payload=payload,
            )
            text = _extract_output_text(data)
            sources = _extract_web_sources(data) if request.web_search else ()
            return AIReply(
                text=text[:8_000],
                model=self._model_ids[spec.decision.model],
                provider="openai-compatible-responses",
                sources=sources,
            )
        finally:
            await self._finish_request()

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._closed = True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=self._close_drain_timeout_seconds)
        except TimeoutError:
            # 停止期限内に終わらないrequestはsession closeで中断し、再生成はclosed flagで拒否する。
            pass
        finally:
            async with self._lifecycle_lock:
                session, self._session = self._session, None
            if session is not None and not session.closed:
                await asyncio.shield(session.close())

    async def _begin_request(self) -> aiohttp.ClientSession:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("AI provider is closed")
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._inflight += 1
            self._idle.clear()
            return self._session

    async def _finish_request(self) -> None:
        async with self._lifecycle_lock:
            self._inflight -= 1
            if self._inflight < 0:
                raise RuntimeError("AI provider request counter is invalid")
            if self._inflight == 0:
                self._idle.set()

    async def _authorize_http_boundary(
        self,
        *,
        request: AIRequest,
        provider_sink_verifier: object | None,
    ) -> None:
        if request.bounded_toolset is not None:
            if request.context_authorization is None or not request.context_authorization_current():
                raise ProviderAuthorizationError("formal provider envelope authorization is invalid")
        elif request.context_authorization is not None and not request.context_authorization_current():
            raise ProviderAuthorizationError("prepared provider envelope authorization is invalid")
        if provider_sink_verifier is not None:
            still_allowed = await _verify_service_sink_async(
                provider_sink_verifier,
                request=request,
                provider=self,
            )
            if not still_allowed:
                raise ProviderAuthorizationError("remote AI authorization expired at the provider HTTP boundary")

    async def _post_responses(
        self,
        *,
        session: aiohttp.ClientSession,
        request: AIRequest,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> Any:
        retry_allowed = request.web_search and request.has_side_effects is False and not self._is_local
        transient_errors = (
            TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
            aiohttp.ServerDisconnectedError,
        )
        try:
            async with session.post(
                f"{self._base_url}/responses",
                headers=headers,
                json=payload,
                allow_redirects=False,
            ) as response:
                status = getattr(response, "status", None)
                if retry_allowed and type(status) is int and (status == 429 or 500 <= status < 600):
                    raise ProviderWebSearchUnavailableError("OpenAI web search is temporarily unavailable")
                response.raise_for_status()
                return await _read_json_limited(response, self._max_response_bytes)
        except asyncio.CancelledError:
            raise
        except ProviderWebSearchUnavailableError:
            raise
        except transient_errors:
            if retry_allowed:
                raise ProviderWebSearchUnavailableError("OpenAI web search is temporarily unavailable") from None
            raise

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, "
            f"is_local={self._is_local!r}, max_output_tokens={self._max_output_tokens!r})"
        )


def _responses_input(request: AIRequest) -> str | list[dict[str, Any]]:
    """単純textは互換string、履歴・添付がある時だけ公式typed inputへ変換する。"""

    input_text = request.provider_input or request.prompt
    # provider_inputがあるrequestはContextBuilderが履歴をsystem contextへ既に
    # 合成済み。AIRequest.historyはdeterministic hook/監査互換のため保持するが、
    # providerへ二重送信しない。
    provider_history = () if request.provider_input is not None else request.history
    if not provider_history and not request.attachments:
        return input_text

    messages = [_turn_payload(turn) for turn in provider_history]
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": input_text},
                *(_attachment_payload(attachment) for attachment in request.attachments),
            ],
        }
    )
    return messages


def _request_contains_attachments(request: AIRequest) -> bool:
    return bool(request.attachments or any(turn.attachments for turn in request.history))


def _turn_payload(turn: Turn) -> dict[str, Any]:
    # 公式の手動会話例どおり、過去のassistant本文はmessageのstring contentとして
    # 再送する。``input_text`` はuser入力partであり、assistant出力へ偽装しない。
    if turn.role is MessageRole.ASSISTANT:
        if turn.attachments:
            raise RuntimeError("assistant history must not contain attachments")
        return {"role": "assistant", "content": turn.text}
    if not turn.attachments:
        return {"role": "user", "content": turn.text}
    content: list[dict[str, str]] = [{"type": "input_text", "text": turn.text}]
    content.extend(_attachment_payload(attachment) for attachment in turn.attachments)
    return {"role": "user", "content": content}


def _attachment_payload(attachment: Attachment) -> dict[str, str]:
    encoded = base64.b64encode(attachment.data).decode("ascii")
    data_url = f"data:{attachment.mime_type};base64,{encoded}"
    if attachment.kind is AttachmentKind.IMAGE:
        if attachment.detail is None:  # models.Attachmentが保証するが、境界でもfail closedにする。
            raise RuntimeError("image attachment is missing detail")
        return {
            "type": "input_image",
            "image_url": data_url,
            "detail": attachment.detail.value,
        }
    return {
        "type": "input_file",
        "file_data": data_url,
        "filename": attachment.filename,
    }


def _extract_output_text(data: Any) -> str:
    if not isinstance(data, Mapping):
        raise RuntimeError("AI provider returned an invalid response")
    output = data.get("output")
    if not isinstance(output, list):
        raise RuntimeError("AI provider returned an invalid response")

    parts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    combined = "".join(parts).strip()
    if not combined:
        raise RuntimeError("AI provider returned an invalid response")
    return combined


def _extract_web_sources(data: Any) -> tuple[AISource, ...]:
    """Responsesのcitation注釈とweb_search_call sourceを正規化・重複排除する。"""

    if not isinstance(data, Mapping):
        return ()
    output = data.get("output")
    if not isinstance(output, list):
        return ()
    candidates: list[tuple[object, object]] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        # Source adoption is deliberately limited to the actual fixed web tool
        # result. Model text, citations and arbitrary output items are not a
        # provenance boundary for Discord links.
        if item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        if isinstance(action, Mapping):
            sources = action.get("sources")
            if isinstance(sources, list):
                for source in sources:
                    if isinstance(source, Mapping):
                        candidates.append((source.get("title"), source.get("url")))

    result: list[AISource] = []
    seen: set[str] = set()
    for raw_title, raw_url in candidates:
        if not isinstance(raw_url, str) or raw_url in seen:
            continue
        title = raw_title.strip() if isinstance(raw_title, str) else ""
        try:
            source = AISource(title=title or raw_url, url=raw_url)
        except (TypeError, ValueError):
            continue
        result.append(source)
        seen.add(source.url)
        if len(result) >= 20:
            break
    return tuple(result)


async def _read_json_limited(response: Any, limit: int) -> Any:
    declared = getattr(response, "content_length", None)
    if isinstance(declared, int) and declared > limit:
        raise RuntimeError("AI provider response exceeds the configured limit")
    content = getattr(response, "content", None)
    iterator = getattr(content, "iter_chunked", None)
    if callable(iterator):
        chunks: list[bytes] = []
        length = 0
        async for chunk in iterator(64 * 1024):
            length += len(chunk)
            if length > limit:
                raise RuntimeError("AI provider response exceeds the configured limit")
            chunks.append(chunk)
        payload = b"".join(chunks)
    else:
        reader = getattr(response, "read", None)
        if not callable(reader):
            raise RuntimeError("AI provider returned an invalid response")
        payload = await reader()
        if len(payload) > limit:
            raise RuntimeError("AI provider response exceeds the configured limit")
    try:
        return json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("AI provider returned an invalid response") from exc
