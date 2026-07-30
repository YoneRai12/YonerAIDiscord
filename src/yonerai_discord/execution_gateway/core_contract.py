from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from .core_files import CoreArtifactOwnerScopeV01, core_ref_from_artifact_v01
from .models import ArtifactReference, RunInput


CORE_REQUEST_SCHEMA = "yonerai.core.discord-run.v1"
CORE_FACTS_EXTENSION = "discord_core_facts"
CORE_V01_OPTIONS_EXTENSION = "yonerai_core_options_v0_1"
CORE_V01_WIRE_VERSION = "0.1"
MAX_CORE_MESSAGE_CONTENT_BYTES_V01 = 512 * 1024
_SNOWFLAKE_RE = re.compile(r"[1-9][0-9]{0,19}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TRIGGERS = frozenset({"mention", "reply", "dm", "slash"})
_VISIBILITIES = frozenset({"dm", "guild_channel", "guild_thread"})
_REFERENCE_KINDS = frozenset({"file", "image"})
_HISTORY_ROLES = frozenset({"user", "assistant"})


class CoreContractError(ValueError):
    """Coreへ投影できないneutral requestを表す固定境界エラー。"""


@dataclass(frozen=True, slots=True)
class DiscordCoreFacts:
    """Discordが実際に観測した、authorityを含まないrequest facts。"""

    user_id: int
    channel_id: int
    message_id: int
    request_id: str
    route_mode: str
    guild_id: int | None = None
    thread_id: int | None = None
    reply_to_message_id: int | None = None
    trigger: str = "mention"
    visibility: str = "guild_channel"
    trace_id: str | None = None

    def __post_init__(self) -> None:
        for label in ("user_id", "channel_id", "message_id"):
            _snowflake(getattr(self, label), label=label)
        for label in ("guild_id", "thread_id", "reply_to_message_id"):
            value = getattr(self, label)
            if value is not None:
                _snowflake(value, label=label)
        _identifier(self.request_id, label="request_id")
        _identifier(self.route_mode, label="route_mode")
        if self.trace_id is not None:
            _identifier(self.trace_id, label="trace_id")
        if self.trigger not in _TRIGGERS:
            raise CoreContractError("Discord trigger is invalid")
        if self.visibility not in _VISIBILITIES:
            raise CoreContractError("Discord visibility is invalid")
        if self.guild_id is None:
            if self.thread_id is not None or self.visibility != "dm":
                raise CoreContractError("DM facts are inconsistent")
        elif self.thread_id is None:
            if self.visibility != "guild_channel":
                raise CoreContractError("guild channel facts are inconsistent")
        elif self.visibility != "guild_thread":
            raise CoreContractError("thread facts are inconsistent")


@dataclass(frozen=True, slots=True)
class CoreRunRequest:
    """HTTPやauthを持たない、CoreRunPort向けのdata-only request。"""

    content: str = field(repr=False)
    idempotency_key: str
    context_binding: Mapping[str, str]
    user_identity: Mapping[str, str]
    client_context: Mapping[str, str | None]
    request_meta: Mapping[str, str | None]
    route_hint: Mapping[str, str]
    attachments: tuple[Mapping[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_binding", _frozen_mapping(self.context_binding))
        object.__setattr__(self, "user_identity", _frozen_mapping(self.user_identity))
        object.__setattr__(self, "client_context", _frozen_mapping(self.client_context))
        object.__setattr__(self, "request_meta", _frozen_mapping(self.request_meta))
        object.__setattr__(self, "route_hint", _frozen_mapping(self.route_hint))
        object.__setattr__(
            self,
            "attachments",
            tuple(_frozen_mapping(attachment) for attachment in self.attachments),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": CORE_REQUEST_SCHEMA,
            "source": "discord",
            "content": self.content,
            "idempotency_key": self.idempotency_key,
            "context_binding": dict(self.context_binding),
            "user_identity": dict(self.user_identity),
            "client_context": dict(self.client_context),
            "request_meta": dict(self.request_meta),
            "route_hint": dict(self.route_hint),
            "attachments": [dict(attachment) for attachment in self.attachments],
        }


class CoreRunPort(Protocol):
    """将来transportの代わりに注入する、最小のCore run stream port。"""

    def run(self, request: CoreRunRequest) -> AsyncIterator[Mapping[str, object]]: ...


@dataclass(frozen=True, slots=True)
class CoreRunOptionsV01:
    preferred_model: str | None = None
    history_override: tuple[Mapping[str, str], ...] | None = None

    def __post_init__(self) -> None:
        if self.preferred_model is not None:
            _identifier(self.preferred_model, label="preferred_model")
        if self.history_override is None:
            return
        history = tuple(_history_item(item) for item in self.history_override)
        if len(history) > 32:
            raise CoreContractError("history_override has too many messages")
        object.__setattr__(self, "history_override", history)


@dataclass(frozen=True, slots=True)
class CoreMessageRequestV01:
    """YonerAI Internal Run API v0.1のexact `/v1/messages` DTO。"""

    content: str = field(repr=False)
    conversation_id: str
    user_identity: Mapping[str, str]
    attachments: tuple[Mapping[str, str], ...]
    idempotency_key: str
    preferred_model: str | None
    history_override: tuple[Mapping[str, str], ...] | None

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise CoreContractError("Core v0.1 content is invalid")
        content = self.content.strip()
        try:
            content_size = len(content.encode("utf-8", errors="strict"))
        except UnicodeEncodeError:
            raise CoreContractError("Core v0.1 content is invalid") from None
        if content_size > MAX_CORE_MESSAGE_CONTENT_BYTES_V01:
            raise CoreContractError("Core v0.1 content exceeds the size limit")
        object.__setattr__(self, "content", content)
        object.__setattr__(
            self,
            "conversation_id",
            _bounded_identifier(self.conversation_id, label="conversation_id", maximum=512),
        )
        identity = _frozen_mapping(self.user_identity)
        if set(identity) != {"provider", "id"} or identity["provider"] != "discord":
            raise CoreContractError("Core v0.1 user_identity is invalid")
        _snowflake_text(identity["id"], label="user_identity.id")
        object.__setattr__(self, "user_identity", identity)
        attachments = tuple(_frozen_mapping(attachment) for attachment in self.attachments)
        seen: set[str] = set()
        for attachment in attachments:
            copied = dict(attachment)
            if set(copied) != {"type", "attachment_id"} or copied["type"] not in {
                "file_ref",
                "image_ref",
            }:
                raise CoreContractError("Core v0.1 attachment is invalid")
            attachment_id = _bounded_identifier(
                copied["attachment_id"],
                label="attachment_id",
                maximum=256,
            )
            if attachment_id in seen:
                raise CoreContractError("Core v0.1 attachment IDs must be unique")
            seen.add(attachment_id)
        object.__setattr__(self, "attachments", attachments)
        object.__setattr__(
            self,
            "idempotency_key",
            _bounded_identifier(self.idempotency_key, label="idempotency_key", maximum=512),
        )
        if self.preferred_model is not None:
            _identifier(self.preferred_model, label="preferred_model")
        if self.history_override is not None:
            history = tuple(_history_item(item) for item in self.history_override)
            if len(history) > 32:
                raise CoreContractError("history_override has too many messages")
            object.__setattr__(self, "history_override", history)

    def to_mapping(self) -> dict[str, object]:
        return {
            "content": self.content,
            "conversation_id": self.conversation_id,
            "user_identity": dict(self.user_identity),
            "attachments": [dict(attachment) for attachment in self.attachments],
            "idempotency_key": self.idempotency_key,
            "preferred_model": self.preferred_model,
            "history_override": (
                None if self.history_override is None else [dict(message) for message in self.history_override]
            ),
        }


@dataclass(frozen=True, slots=True)
class CoreRunReferenceV01:
    run_id: str
    reused: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _bounded_identifier(self.run_id, label="run_id", maximum=128))
        if type(self.reused) is not bool:
            raise TypeError("reused must be a boolean")


@dataclass(frozen=True, slots=True)
class CoreToolResultV01:
    """v0.1 `/results` のexact outer DTO。"""

    tool: str
    result: object = field(repr=False)
    tool_call_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool", _bounded_identifier(self.tool, label="tool", maximum=256))
        object.__setattr__(
            self,
            "tool_call_id",
            _bounded_identifier(self.tool_call_id, label="tool_call_id", maximum=256),
        )
        _json_value(self.result, depth=0)

    def to_mapping(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "result": self.result,
            "tool_call_id": self.tool_call_id,
        }


class CoreCancelDispositionV01(str, Enum):
    CONFIRMED = "confirmed"
    UNSUPPORTED = "unsupported"
    LOCAL_DETACHED = "local_detached"
    ALREADY_TERMINAL = "already_terminal"


@dataclass(frozen=True, slots=True)
class CoreCancelOutcomeV01:
    disposition: CoreCancelDispositionV01
    remote_confirmed: bool
    local_detached: bool

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, CoreCancelDispositionV01):
            raise TypeError("disposition must be a CoreCancelDispositionV01")
        if type(self.remote_confirmed) is not bool or type(self.local_detached) is not bool:
            raise TypeError("cancel outcome flags must be booleans")
        if self.remote_confirmed != (self.disposition is CoreCancelDispositionV01.CONFIRMED):
            raise CoreContractError("cancel outcome remote confirmation is inconsistent")
        if self.local_detached != (self.disposition is CoreCancelDispositionV01.LOCAL_DETACHED):
            raise CoreContractError("cancel outcome local detach state is inconsistent")

    @classmethod
    def confirmed(cls) -> CoreCancelOutcomeV01:
        return cls(CoreCancelDispositionV01.CONFIRMED, remote_confirmed=True, local_detached=False)

    @classmethod
    def unsupported(cls) -> CoreCancelOutcomeV01:
        return cls(CoreCancelDispositionV01.UNSUPPORTED, remote_confirmed=False, local_detached=False)

    @classmethod
    def local_detached_outcome(cls) -> CoreCancelOutcomeV01:
        return cls(CoreCancelDispositionV01.LOCAL_DETACHED, remote_confirmed=False, local_detached=True)

    @classmethod
    def already_terminal(cls) -> CoreCancelOutcomeV01:
        return cls(CoreCancelDispositionV01.ALREADY_TERMINAL, remote_confirmed=False, local_detached=False)


class YonerAIInternalRunPortV01(Protocol):
    async def start(self, request: CoreMessageRequestV01) -> CoreRunReferenceV01: ...

    def events(self, run_id: str) -> AsyncIterator[Mapping[str, object]]: ...

    async def submit_result(self, run_id: str, result: CoreToolResultV01) -> None: ...

    async def cancel(self, run_id: str) -> CoreCancelOutcomeV01: ...


def project_core_request(request: RunInput) -> CoreRunRequest:
    if not isinstance(request, RunInput):
        raise TypeError("request must be a RunInput")
    if (
        request.local_payload is not None
        or request.authorization_check is not None
        or request.fresh_authorization_check is not None
        or request.capability_authorization_check is not None
    ):
        raise CoreContractError("local payload and authorization callbacks cannot cross the Core boundary")
    if dict(request.metadata) != {"surface": "discord"}:
        raise CoreContractError("run metadata is not the Discord Core projection contract")
    extensions = dict(request.extensions)
    if set(extensions) != {CORE_FACTS_EXTENSION}:
        raise CoreContractError("run extensions are not the Discord Core projection contract")
    facts = extensions[CORE_FACTS_EXTENSION]
    if not isinstance(facts, DiscordCoreFacts):
        raise CoreContractError("Discord Core facts are invalid")

    context_binding, expected_key = _context_binding(facts)
    if request.conversation_key != expected_key:
        raise CoreContractError("conversation_key does not match observed Discord facts")
    attachments = _project_attachments(request.artifacts)
    return CoreRunRequest(
        content=request.input_text,
        idempotency_key=request.idempotency_key,
        context_binding=context_binding,
        user_identity={"provider": "discord", "id": str(facts.user_id)},
        client_context={
            "guild_id": None if facts.guild_id is None else str(facts.guild_id),
            "channel_id": str(facts.channel_id),
            "thread_id": None if facts.thread_id is None else str(facts.thread_id),
            "message_id": str(facts.message_id),
            "reply_to_message_id": (None if facts.reply_to_message_id is None else str(facts.reply_to_message_id)),
            "trigger": facts.trigger,
            "visibility": facts.visibility,
        },
        request_meta={
            "request_id": facts.request_id,
            "trace_id": facts.trace_id,
            "origin": "discord-gateway",
        },
        route_hint={"mode": facts.route_mode},
        attachments=attachments,
    )


def project_core_message_v01(request: RunInput) -> CoreMessageRequestV01:
    """中立RunInputをv0.1 exact allowlistへ投影する。

    旧custom schemaの`schema`、`source`、`context_binding`、`client_context`、
    `request_meta`、`route_hint`はこのwire DTOへ一切含めない。
    """

    if not isinstance(request, RunInput):
        raise TypeError("request must be a RunInput")
    if (
        request.local_payload is not None
        or request.authorization_check is not None
        or request.fresh_authorization_check is not None
        or request.capability_authorization_check is not None
    ):
        raise CoreContractError("local payload and authorization callbacks cannot cross the Core v0.1 boundary")
    if dict(request.metadata) != {"surface": "discord"}:
        raise CoreContractError("run metadata is not the Discord Core v0.1 projection contract")
    extensions = dict(request.extensions)
    if not set(extensions).issubset({CORE_FACTS_EXTENSION, CORE_V01_OPTIONS_EXTENSION}) or (
        CORE_FACTS_EXTENSION not in extensions
    ):
        raise CoreContractError("run extensions are not the Discord Core v0.1 projection contract")
    facts = extensions[CORE_FACTS_EXTENSION]
    if not isinstance(facts, DiscordCoreFacts):
        raise CoreContractError("Discord Core facts are invalid")
    options = extensions.get(CORE_V01_OPTIONS_EXTENSION, CoreRunOptionsV01())
    if not isinstance(options, CoreRunOptionsV01):
        raise CoreContractError("Core v0.1 options are invalid")

    conversation_id = discord_core_conversation_id(facts)
    if request.conversation_key != conversation_id:
        raise CoreContractError("conversation_key does not match observed Discord facts")
    owner_scope = CoreArtifactOwnerScopeV01(
        provider="discord",
        subject_id=str(facts.user_id),
        conversation_id=conversation_id,
    )
    attachments: list[Mapping[str, str]] = []
    seen_ids: set[str] = set()
    for artifact in request.artifacts:
        ref = core_ref_from_artifact_v01(artifact, owner_scope=owner_scope)
        if ref.attachment_id in seen_ids:
            raise CoreContractError("Core v0.1 attachment IDs must be unique")
        seen_ids.add(ref.attachment_id)
        attachments.append(
            MappingProxyType(
                {
                    "type": f"{ref.kind}_ref",
                    "attachment_id": ref.attachment_id,
                }
            )
        )
    return CoreMessageRequestV01(
        content=request.input_text,
        conversation_id=conversation_id,
        user_identity={"provider": "discord", "id": str(facts.user_id)},
        attachments=tuple(attachments),
        idempotency_key=request.idempotency_key,
        preferred_model=options.preferred_model,
        history_override=options.history_override,
    )


def _context_binding(facts: DiscordCoreFacts) -> tuple[dict[str, str], str]:
    conversation_id = discord_core_conversation_id(facts)
    if facts.guild_id is None:
        kind = "dm"
    elif facts.thread_id is not None:
        kind = "thread"
    else:
        kind = "channel"
    return {
        "provider": "discord",
        "kind": kind,
        "external_id": conversation_id,
    }, conversation_id


def discord_core_conversation_id(facts: DiscordCoreFacts) -> str:
    """観測済みDiscord factsからCore用conversation IDを一意に導出する。"""

    if not isinstance(facts, DiscordCoreFacts):
        raise TypeError("facts must be DiscordCoreFacts")
    if facts.guild_id is None:
        return f"dm:{facts.channel_id}:user:{facts.user_id}"
    if facts.thread_id is not None:
        return f"guild:{facts.guild_id}:channel:{facts.channel_id}:thread:{facts.thread_id}"
    return f"guild:{facts.guild_id}:channel:{facts.channel_id}:user:{facts.user_id}"


def _project_attachments(
    artifacts: tuple[ArtifactReference, ...],
) -> tuple[Mapping[str, str], ...]:
    projected: list[Mapping[str, str]] = []
    seen_ids: set[str] = set()
    for artifact in artifacts:
        if (
            artifact.kind not in _REFERENCE_KINDS
            or artifact.uri is not None
            or artifact.metadata
            or artifact.extensions
            or artifact.artifact_id in seen_ids
        ):
            raise CoreContractError("attachments must be unique ref-only file or image artifacts")
        seen_ids.add(artifact.artifact_id)
        projected.append(
            MappingProxyType(
                {
                    "type": f"{artifact.kind}_ref",
                    "attachment_id": artifact.artifact_id,
                }
            )
        )
    return tuple(projected)


def _snowflake(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or _SNOWFLAKE_RE.fullmatch(str(value)) is None:
        raise CoreContractError(f"{label} is invalid")
    return value


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise CoreContractError(f"{label} is invalid")
    return value


def _bounded_identifier(value: object, *, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CoreContractError(f"{label} is invalid")
    return value


def _snowflake_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SNOWFLAKE_RE.fullmatch(value) is None:
        raise CoreContractError(f"{label} is invalid")
    return value


def _history_item(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise CoreContractError("history_override message is invalid")
    copied = dict(value)
    if set(copied) != {"role", "content"} or copied["role"] not in _HISTORY_ROLES:
        raise CoreContractError("history_override message is invalid")
    content = copied["content"]
    if not isinstance(content, str) or not content.strip() or len(content) > 16_000:
        raise CoreContractError("history_override message is invalid")
    return MappingProxyType({"role": copied["role"], "content": content.strip()})


def _json_value(value: object, *, depth: int) -> None:
    if depth > 16:
        raise CoreContractError("Core v0.1 result is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CoreContractError("Core v0.1 result contains a non-finite number")
        return
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise CoreContractError("Core v0.1 result mapping is too large")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise CoreContractError("Core v0.1 result mapping key is invalid")
            _json_value(item, depth=depth + 1)
        return
    if isinstance(value, (tuple, list)):
        if len(value) > 256:
            raise CoreContractError("Core v0.1 result list is too large")
        for item in value:
            _json_value(item, depth=depth + 1)
        return
    raise CoreContractError("Core v0.1 result is not JSON-compatible")


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise CoreContractError("Core request mapping is invalid")
    return MappingProxyType(dict(value))


__all__ = [
    "CORE_FACTS_EXTENSION",
    "CORE_REQUEST_SCHEMA",
    "CORE_V01_OPTIONS_EXTENSION",
    "CORE_V01_WIRE_VERSION",
    "MAX_CORE_MESSAGE_CONTENT_BYTES_V01",
    "CoreCancelDispositionV01",
    "CoreCancelOutcomeV01",
    "CoreContractError",
    "CoreMessageRequestV01",
    "CoreRunPort",
    "CoreRunOptionsV01",
    "CoreRunReferenceV01",
    "CoreRunRequest",
    "CoreToolResultV01",
    "DiscordCoreFacts",
    "YonerAIInternalRunPortV01",
    "discord_core_conversation_id",
    "project_core_message_v01",
    "project_core_request",
]
