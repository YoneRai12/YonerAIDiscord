from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from ipaddress import ip_address
from urllib.parse import urlparse

from yonerai_discord.execution_gateway.core_contract import (
    DiscordCoreFacts,
    discord_core_conversation_id,
)
from yonerai_discord.execution_gateway.core_files import (
    CoreArtifactOwnerScopeV01,
    CoreFilesContractError,
    core_ref_from_artifact_v01,
)
from yonerai_discord.execution_gateway.local import IdempotencyConflictError
from yonerai_discord.execution_gateway.models import ArtifactReference, RunEvent, RunInput
from yonerai_discord.execution_gateway.protocol import ExecutionGateway
from yonerai_discord.secret_detection import contains_secret_like

from .core_surface import with_discord_core_facts
from .models import MAX_REPLY_SOURCES, AIReply, AIRequest, AISource
from .service import AIUnavailableError, PrivacyBoundaryError


logger = logging.getLogger(__name__)


class DuplicateDiscordRun(RuntimeError):
    """同じDiscord eventで既にrunが開始済みであることをsurfaceへ通知する。"""


def build_discord_core_facts(
    *,
    user_id: int,
    guild_id: int | None,
    channel_id: int,
    message_id: int,
    request_id: str,
    route_mode: str,
    trigger: str,
    thread_id: int | None = None,
    reply_to_message_id: int | None = None,
) -> DiscordCoreFacts:
    if guild_id is None:
        visibility = "dm"
    elif thread_id is None:
        visibility = "guild_channel"
    else:
        visibility = "guild_thread"
    return DiscordCoreFacts(
        user_id=user_id,
        guild_id=guild_id,
        channel_id=channel_id,
        thread_id=thread_id,
        message_id=message_id,
        reply_to_message_id=reply_to_message_id,
        request_id=request_id,
        route_mode=route_mode,
        trigger=trigger,
        visibility=visibility,
    )


async def execute_ai_run(
    gateway: ExecutionGateway,
    request: AIRequest,
    *,
    idempotency_key: str,
    conversation_key: str,
    authorization_check: Callable[[], bool] | None = None,
    fresh_authorization_check: Callable[[], bool | Awaitable[bool]] | None = None,
    tool_capability_check: Callable[[str], bool] | None = None,
    discord_core_facts: DiscordCoreFacts | None = None,
    accept_core_artifact_references: bool = False,
    on_started: Callable[[], Awaitable[None]] | None = None,
    on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
) -> AIReply:
    """既存AIRequestをneutral gatewayへ渡し、安全なAIReplyへ戻す。"""

    if type(accept_core_artifact_references) is not bool:
        raise TypeError("accept_core_artifact_references must be a bool")
    if (
        request.bounded_toolset is not None
        and request.bounded_toolset.effective_tools
        and tool_capability_check is None
    ):
        raise AIUnavailableError("bounded model-tool capability callback is required")
    reference = None
    try:
        reference = await gateway.start(
            with_discord_core_facts(
                RunInput(
                    input_text=request.prompt,
                    idempotency_key=idempotency_key,
                    conversation_key=conversation_key,
                    metadata={"surface": "discord"},
                    local_payload=request,
                    authorization_check=authorization_check,
                    fresh_authorization_check=fresh_authorization_check,
                    capability_authorization_check=tool_capability_check,
                ),
                discord_core_facts,
            )
        )
        if reference.reused:
            raise DuplicateDiscordRun(reference.run_id)
        if on_started is not None:
            await on_started()

        text_deltas: list[str] = []
        artifacts: list[ArtifactReference] = []
        async for event in gateway.events(reference.run_id):
            if on_event is not None:
                try:
                    await on_event(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "ai_gateway_event_callback_failed",
                        extra={"error_type": type(exc).__name__},
                    )
            if event.kind == "text_delta" and event.text:
                text_deltas.append(event.text)
                continue
            if event.kind == "artifact" and event.artifact is not None:
                artifacts.append(event.artifact)
                continue
            if event.kind == "error":
                _raise_gateway_error(event)
            if event.kind != "final":
                # 未知eventも含め、surfaceが理解しないkindは安全に無視する。
                continue
            final_artifacts = event.payload.get("artifacts", ())
            if not isinstance(final_artifacts, (tuple, list)) or any(
                not isinstance(artifact, ArtifactReference) for artifact in final_artifacts
            ):
                raise AIUnavailableError("gateway final response contained invalid artifacts")
            artifacts.extend(final_artifacts)
            text = event.text or "".join(text_deltas)
            if (
                not text.strip()
                and artifacts
                and accept_core_artifact_references
                and event.payload.get("provider") == "yonerai-internal-run-v0.1"
            ):
                text = "成果物を生成しました。"
            reply = _reply_from_final_event(event, text)
            if artifacts and accept_core_artifact_references:
                if reply.provider != "yonerai-internal-run-v0.1" or discord_core_facts is None:
                    raise AIUnavailableError("Core artifact delivery is unavailable")
                return _with_core_artifact_references(
                    reply,
                    artifacts,
                    facts=discord_core_facts,
                )
            if artifacts and reply.provider == "yonerai-internal-run-v0.1":
                if discord_core_facts is None or not accept_core_artifact_references:
                    # Core Files read portがないsurfaceでは、opaque refを成果物名だけの
                    # 成功表示へ落とさず従来どおりfail closedにする。
                    raise AIUnavailableError("Core artifact delivery is unavailable")
            return _with_artifact_references(reply, artifacts)
    except asyncio.CancelledError:
        if reference is not None:
            with suppress(Exception):
                await gateway.cancel(reference.run_id)
        raise
    except IdempotencyConflictError as exc:
        raise AIUnavailableError("gateway idempotency conflict") from exc
    except DuplicateDiscordRun:
        raise
    except Exception:
        if reference is not None:
            with suppress(Exception):
                await gateway.cancel(reference.run_id)
        raise

    raise AIUnavailableError("gateway run ended without a final event")


def _reply_from_final_event(event: RunEvent, text: str) -> AIReply:
    model = event.payload.get("model")
    provider = event.payload.get("provider")
    if not isinstance(text, str) or not text.strip():
        raise AIUnavailableError("gateway returned an empty final response")
    if not isinstance(model, str) or not model.strip():
        raise AIUnavailableError("gateway final response omitted model")
    if not isinstance(provider, str) or not provider.strip():
        raise AIUnavailableError("gateway final response omitted provider")
    return AIReply(
        text=text,
        model=model,
        provider=provider,
        sources=_safe_sources(event.payload.get("sources")),
    )


def _safe_sources(value: object) -> tuple[AISource, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    sources: list[AISource] = []
    seen_urls: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        title = raw.get("title")
        url = raw.get("url")
        if not isinstance(title, str) or not isinstance(url, str) or url in seen_urls:
            continue
        try:
            source = AISource(title=title, url=url)
        except (TypeError, ValueError):
            continue
        sources.append(source)
        seen_urls.add(url)
        if len(sources) >= MAX_REPLY_SOURCES:
            break
    return tuple(sources)


def _with_artifact_references(reply: AIReply, artifacts: list[ArtifactReference]) -> AIReply:
    if not artifacts:
        return reply
    lines: list[str] = []
    seen_ids: set[str] = set()
    for artifact in artifacts:
        if artifact.artifact_id in seen_ids:
            continue
        seen_ids.add(artifact.artifact_id)
        label = _escape_markdown(artifact.name or artifact.kind)
        if _public_http_uri(artifact.uri):
            lines.append(f"- [{label}]({artifact.uri})")
        else:
            lines.append(f"- {label}")
    if not lines:
        return reply
    return AIReply(
        text=f"{reply.text}\n\n成果物\n" + "\n".join(lines),
        model=reply.model,
        provider=reply.provider,
        sources=reply.sources,
        synthesis_action_id=reply.synthesis_action_id,
        delivery_handled=reply.delivery_handled,
    )


def _with_core_artifact_references(
    reply: AIReply,
    artifacts: list[ArtifactReference],
    *,
    facts: DiscordCoreFacts,
) -> AIReply:
    try:
        owner_scope = CoreArtifactOwnerScopeV01(
            provider="discord",
            subject_id=str(facts.user_id),
            conversation_id=discord_core_conversation_id(facts),
        )
        unique: list[ArtifactReference] = []
        unique_refs = []
        seen_by_artifact_id: dict[str, object] = {}
        seen_attachment_ids: set[str] = set()
        for artifact in artifacts:
            ref = core_ref_from_artifact_v01(artifact, owner_scope=owner_scope)
            previous = seen_by_artifact_id.get(ref.artifact_id)
            if previous is not None:
                if previous != ref:
                    raise CoreFilesContractError("Core artifact references conflict")
                continue
            if ref.attachment_id in seen_attachment_ids:
                raise CoreFilesContractError("Core artifact references conflict")
            seen_by_artifact_id[ref.artifact_id] = ref
            seen_attachment_ids.add(ref.attachment_id)
            unique.append(artifact)
            unique_refs.append(ref)
            if len(unique) > 4:
                raise CoreFilesContractError("Core artifact reference count is unavailable")
        public_values = (
            reply.text,
            reply.model,
            reply.provider,
            *(value for source in reply.sources for value in (source.title, source.url)),
        )
        if any(
            marker.casefold() in public_value.casefold()
            for ref in unique_refs
            for marker in (ref.artifact_id, ref.attachment_id, ref.sha256)
            for public_value in public_values
        ):
            raise CoreFilesContractError("Core artifact identity leaked into public text")
    except (CoreFilesContractError, TypeError, ValueError):
        raise AIUnavailableError("Core artifact delivery is unavailable") from None
    return AIReply(
        text=reply.text,
        model=reply.model,
        provider=reply.provider,
        sources=reply.sources,
        artifact_references=tuple(unique),
        synthesis_action_id=reply.synthesis_action_id,
        delivery_handled=reply.delivery_handled,
    )


def _public_http_uri(value: str | None) -> bool:
    if value is None:
        return False
    parsed = urlparse(value)
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or contains_secret_like(value)
        or any(character.isspace() or character in "<>()`\\" for character in value)
        or parsed.username is not None
        or parsed.password is not None
        or hostname.casefold() == "localhost"
        or hostname.casefold().endswith(".local")
    ):
        return False
    try:
        address = ip_address(hostname)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)


def _escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _raise_gateway_error(event: RunEvent) -> None:
    error_type = event.payload.get("error_type")
    if error_type == "PrivacyBoundaryError":
        raise PrivacyBoundaryError("provider boundary rejected execution")
    raise AIUnavailableError("gateway execution failed")


__all__ = ["DuplicateDiscordRun", "build_discord_core_facts", "execute_ai_run"]
