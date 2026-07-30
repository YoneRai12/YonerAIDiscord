from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import replace

from yonerai_discord.execution_gateway import (
    ArtifactReference,
    CapabilityResult,
    ExecutionGateway,
    RunEvent,
    RunInput,
    RunReference,
)
from yonerai_discord.execution_gateway.core_contract import (
    CORE_FACTS_EXTENSION,
    CORE_V01_OPTIONS_EXTENSION,
    CoreRunOptionsV01,
    DiscordCoreFacts,
    discord_core_conversation_id,
)
from yonerai_discord.execution_gateway.core_files import (
    CoreArtifactOwnerScopeV01,
    CoreFileRegistrationV01,
    CoreFilesRegistrationPortV01,
    register_core_file_v01,
)

from .models import AIRequest, AttachmentKind, DataBoundary
from .service import AIUnavailableError


class DiscordCoreSurfaceGateway:
    """Discordのインメモリ入力をCore向けref-only RunInputへ変換する。"""

    def __init__(
        self,
        gateway: ExecutionGateway,
        *,
        files: CoreFilesRegistrationPortV01 | None = None,
    ) -> None:
        for method_name in ("start", "events", "submit_result", "cancel"):
            if not callable(getattr(gateway, method_name, None)):
                raise TypeError(f"gateway must provide {method_name}()")
        if files is not None and not callable(getattr(files, "register", None)):
            raise TypeError("files must expose register()")
        self._gateway = gateway
        self._files = files

    @property
    def files_registration_available(self) -> bool:
        """注入済みFiles portの存在だけを示し、live到達成功は主張しない。"""

        return self._files is not None

    async def start(self, request: RunInput) -> RunReference:
        if not isinstance(request, RunInput):
            raise TypeError("request must be a RunInput")
        payload = request.local_payload
        if not isinstance(payload, AIRequest):
            raise AIUnavailableError("Discord Core request payload is unavailable")
        extensions = dict(request.extensions)
        if set(extensions) != {CORE_FACTS_EXTENSION}:
            raise AIUnavailableError("Discord Core facts are unavailable")
        facts = extensions[CORE_FACTS_EXTENSION]
        if not isinstance(facts, DiscordCoreFacts):
            raise AIUnavailableError("Discord Core facts are invalid")
        _validate_scope(request, payload, facts)
        if request.artifacts:
            raise AIUnavailableError("unregistered artifacts cannot cross the Core boundary")

        conversation_id = discord_core_conversation_id(facts)
        artifacts = await self._register_attachments(request, payload, facts, conversation_id=conversation_id)
        options = CoreRunOptionsV01(
            preferred_model=(
                payload.required_model_id or payload.required_model_alias or payload.effective_model_alias
            ),
            history_override=tuple({"role": turn.role.value, "content": turn.text} for turn in payload.history),
        )
        projected = replace(
            request,
            conversation_key=conversation_id,
            artifacts=artifacts,
            extensions={
                CORE_FACTS_EXTENSION: facts,
                CORE_V01_OPTIONS_EXTENSION: options,
            },
        )
        await _authorization_current(request)
        return await self._gateway.start(projected)

    def events(self, run_id: str) -> AsyncIterator[RunEvent]:
        return self._gateway.events(run_id)

    async def submit_result(self, run_id: str, result: CapabilityResult) -> None:
        await self._gateway.submit_result(run_id, result)

    async def cancel(self, run_id: str) -> None:
        await self._gateway.cancel(run_id)

    async def _register_attachments(
        self,
        request: RunInput,
        payload: AIRequest,
        facts: DiscordCoreFacts,
        *,
        conversation_id: str,
    ) -> tuple[ArtifactReference, ...]:
        if not payload.attachments:
            return ()
        files = self._files
        if files is None:
            raise AIUnavailableError("Core Files registration is unavailable")
        owner_scope = CoreArtifactOwnerScopeV01(
            provider="discord",
            subject_id=str(facts.user_id),
            conversation_id=conversation_id,
        )
        registered: list[ArtifactReference] = []
        for index, attachment in enumerate(payload.attachments, start=1):
            await _authorization_current(request)
            registered.append(
                await register_core_file_v01(
                    CoreFileRegistrationV01(
                        local_artifact_id=f"{facts.request_id}:attachment:{index}",
                        kind=("image" if attachment.kind is AttachmentKind.IMAGE else "file"),
                        media_type=attachment.mime_type,
                        owner_scope=owner_scope,
                        retention="conversation",
                        provenance="discord-attachment",
                        content=attachment.data,
                    ),
                    files,
                )
            )
            await _authorization_current(request)
        return tuple(registered)


def with_discord_core_facts(request: RunInput, facts: DiscordCoreFacts | None) -> RunInput:
    """Local既定を変えず、Core選択時だけ使うDiscord観測事実を添付する。"""

    if facts is None:
        return request
    if not isinstance(facts, DiscordCoreFacts):
        raise TypeError("facts must be DiscordCoreFacts or None")
    if request.extensions:
        raise AIUnavailableError("run extensions are already occupied")
    return replace(request, extensions={CORE_FACTS_EXTENSION: facts})


async def _authorization_current(request: RunInput) -> None:
    check = request.authorization_check
    if check is not None:
        try:
            allowed = check()
        except Exception:
            allowed = False
        if allowed is not True:
            raise AIUnavailableError("authorization changed before Core Files registration")
    fresh = request.fresh_authorization_check
    if fresh is None:
        return
    try:
        allowed = fresh()
        if inspect.isawaitable(allowed):
            allowed = await allowed
    except Exception:
        allowed = False
    if allowed is not True:
        raise AIUnavailableError("authorization changed before Core Files registration")


def _validate_scope(
    request: RunInput,
    payload: AIRequest,
    facts: DiscordCoreFacts,
) -> None:
    expected_channel_id = facts.thread_id or facts.channel_id
    if (
        payload.user_id != facts.user_id
        or payload.guild_id != facts.guild_id
        or payload.channel_id != expected_channel_id
        or payload.boundary is not DataBoundary.REMOTE_OPT_IN
        or request.conversation_key is None
        or facts.request_id != request.idempotency_key
    ):
        raise AIUnavailableError("Discord Core request scope does not match")


__all__ = [
    "DiscordCoreSurfaceGateway",
    "with_discord_core_facts",
]
