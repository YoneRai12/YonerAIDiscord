from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping
import unicodedata
from urllib.parse import urlparse

from yonerai_discord.ai_control import RiskLevel, TaskComplexity, TaskKind
from yonerai_discord.execution_gateway.models import ArtifactReference
from yonerai_discord.secret_detection import contains_secret_like
from yonerai_discord.v0_contracts import (
    CONTEXT_CONTRACT_VERSION,
    ContextAuthorizationToken,
    FORMAL_PROVIDER_INPUT_DIRECTIVE,
    MemoryAuthorizationToken,
)

from .bounded_tools import (
    BoundedIntent,
    BoundedToolSet,
    ToolExecutionAuthorization,
    ToolScopeBinding,
    WEB_SEARCH_TOOL_ID,
    capability_metadata_digest,
)

MAX_PROMPT_CHARS = 8_000
MAX_SYSTEM_PROMPT_CHARS = 40_000
MAX_TURN_TEXT_CHARS = 8_000
MAX_HISTORY_MESSAGES = 24
MAX_HISTORY_TEXT_CHARS = 24_000
MAX_ATTACHMENTS_PER_TURN = 8
MAX_SINGLE_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_REQUEST_BINARY_BYTES = 50 * 1024 * 1024
MAX_REPLY_SOURCES = 20
MAX_REPLY_ARTIFACT_REFERENCES = 4
MAX_MODEL_TOOLS = 1
MAX_PROVIDER_METADATA_KEY_BYTES = 64
MAX_PROVIDER_METADATA_VALUE_BYTES = 512
MAX_PROVIDER_METADATA_TOTAL_BYTES = 4_096
SUPPORTED_MODEL_TOOLS = frozenset({"web_search"})
PROVIDER_METADATA_ALLOWED_KEYS = frozenset(
    {
        "discord_trigger",
        "model_alias",
        "request_id",
        "source",
        "surface",
        "trace_id",
        "trigger",
    }
)
_SENSITIVE_PROVIDER_METADATA_KEYS = frozenset(
    {
        "apikey",
        "authorization",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "discordtoken",
        "password",
        "passwd",
        "privatekey",
        "proxyauthorization",
        "refreshtoken",
        "secret",
        "sessioncookie",
        "setcookie",
        "token",
        "accesstoken",
    }
)

IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
FILE_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/msword",
        "application/pdf",
        "application/rtf",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/xml",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/xml",
    }
)


@dataclass(frozen=True, slots=True)
class TaskModelRequirement:
    """Injectable logical/physical model constraint for one typed task."""

    logical_alias: str
    provider_model_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("logical_alias", "provider_model_id"):
            value = getattr(self, field_name)
            if value is None:
                continue
            normalized = value.strip()
            if (
                not normalized
                or len(normalized) > 128
                or any(not (character.isalnum() or character in "._-") for character in normalized)
            ):
                raise ValueError(f"{field_name} is invalid")
            object.__setattr__(self, field_name, normalized)


class DataBoundary(StrEnum):
    """本文を送ってよい範囲。既定は端末内だけ。"""

    LOCAL_ONLY = "local_only"
    REMOTE_OPT_IN = "remote_opt_in"


class MessageRole(StrEnum):
    """手動で再送する会話履歴に許可するrole。"""

    USER = "user"
    ASSISTANT = "assistant"


class AttachmentKind(StrEnum):
    IMAGE = "image"
    FILE = "file"


class ImageDetail(StrEnum):
    AUTO = "auto"
    LOW = "low"
    HIGH = "high"
    ORIGINAL = "original"


@dataclass(frozen=True, slots=True)
class AISource:
    """AI toolが実際に参照した、Discordへ安全に表示できるWeb出典。"""

    title: str
    url: str

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("source title must not be empty")
        if len(self.title) > 300:
            raise ValueError("source title is too long")
        if not isinstance(self.url, str) or len(self.url) > 2_048:
            raise ValueError("source URL is invalid")
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("source URL must be an absolute public HTTP(S) URL")


@dataclass(frozen=True, slots=True)
class Attachment:
    """Responses APIへ渡す、秘密化されたインメモリ添付。"""

    kind: AttachmentKind
    data: bytes = field(repr=False)
    mime_type: str
    filename: str
    detail: ImageDetail | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AttachmentKind):
            raise TypeError("kind must be an AttachmentKind")
        if type(self.data) is not bytes:
            raise TypeError("attachment data must be bytes")
        if not self.data:
            raise ValueError("attachment data must not be empty")
        if len(self.data) > MAX_SINGLE_ATTACHMENT_BYTES:
            raise ValueError("attachment is too large")
        if not isinstance(self.mime_type, str):
            raise TypeError("mime_type must be a string")
        if self.mime_type != self.mime_type.strip().lower() or len(self.mime_type) > 100:
            raise ValueError("mime_type must be a normalized lowercase MIME type")
        if not isinstance(self.filename, str):
            raise TypeError("filename must be a string")
        if (
            not self.filename
            or self.filename != self.filename.strip()
            or self.filename in {".", ".."}
            or "/" in self.filename
            or "\\" in self.filename
            or any(ord(character) < 32 or ord(character) == 127 for character in self.filename)
            or len(self.filename.encode("utf-8")) > 255
        ):
            raise ValueError("filename must be a safe basename")

        if self.kind is AttachmentKind.IMAGE:
            if self.mime_type not in IMAGE_MIME_TYPES:
                raise ValueError("unsupported image MIME type")
            if self.detail is None:
                object.__setattr__(self, "detail", ImageDetail.AUTO)
            elif not isinstance(self.detail, ImageDetail):
                raise TypeError("image detail must be an ImageDetail")
        else:
            if self.mime_type not in FILE_MIME_TYPES:
                raise ValueError("unsupported file MIME type")
            if self.detail is not None:
                raise ValueError("detail is only valid for image attachments")

    @property
    def byte_length(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class Turn:
    """role付き会話メッセージ。本文と添付内容はreprへ出さない。"""

    role: MessageRole
    text: str = field(repr=False)
    attachments: tuple[Attachment, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.role, MessageRole):
            raise TypeError("role must be a MessageRole")
        if not isinstance(self.text, str):
            raise TypeError("turn text must be a string")
        attachments = _validated_attachments(self.attachments)
        object.__setattr__(self, "attachments", attachments)
        if not self.text.strip() and not attachments:
            raise ValueError("turn must contain text or an attachment")
        if len(self.text) > MAX_TURN_TEXT_CHARS:
            raise ValueError("turn text is too long")
        if self.role is MessageRole.ASSISTANT and attachments:
            raise ValueError("assistant turns cannot contain input attachments")

    @property
    def binary_bytes(self) -> int:
        return sum(attachment.byte_length for attachment in self.attachments)


@dataclass(frozen=True, slots=True)
class AIRequest:
    prompt: str = field(repr=False)
    guild_id: int | None
    user_id: int
    channel_id: int | None = None
    provider_input: str | None = field(default=None, repr=False)
    context_authorization: ContextAuthorizationToken | None = field(default=None, repr=False)
    effective_model_alias: str | None = None
    required_model_alias: str | None = None
    required_model_id: str | None = None
    boundary: DataBoundary = DataBoundary.LOCAL_ONLY
    system_prompt: str = field(default="安全で簡潔な日本語で回答してください。", repr=False)
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    task_kind: TaskKind = TaskKind.GENERAL
    complexity: TaskComplexity = TaskComplexity.STANDARD
    risk: RiskLevel = RiskLevel.NORMAL
    uses_tools: bool = False
    web_search: bool = False
    has_side_effects: bool = False
    contains_durable_memory: bool = False
    memory_authorization: MemoryAuthorizationToken | None = field(default=None, repr=False)
    intent: BoundedIntent = BoundedIntent.CONVERSATION
    bounded_toolset: BoundedToolSet | None = field(default=None, repr=False)
    tool_execution_authorization: ToolExecutionAuthorization | None = field(default=None, repr=False)
    allowed_model_tools: tuple[str, ...] = ()
    max_tool_calls: int = 0
    history: tuple[Turn, ...] = field(default=(), repr=False)
    attachments: tuple[Attachment, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be a string")
        prompt = self.prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError("prompt is too long")
        provider_input = self.provider_input
        if provider_input is not None:
            if not isinstance(provider_input, str):
                raise TypeError("provider_input must be a string or None")
            provider_input = provider_input.strip()
            if not provider_input or len(provider_input) > MAX_PROMPT_CHARS:
                raise ValueError("provider_input is invalid")
            object.__setattr__(self, "provider_input", provider_input)
        if not isinstance(self.system_prompt, str):
            raise TypeError("system_prompt must be a string")
        if not self.system_prompt.strip():
            raise ValueError("system_prompt must not be blank")
        if len(self.system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
            raise ValueError("system_prompt is too long")
        if self.guild_id is not None and (
            isinstance(self.guild_id, bool) or not isinstance(self.guild_id, int) or self.guild_id <= 0
        ):
            raise ValueError("guild_id must be a positive integer or None")
        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int) or self.user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        if self.channel_id is not None and (
            isinstance(self.channel_id, bool) or not isinstance(self.channel_id, int) or self.channel_id <= 0
        ):
            raise ValueError("channel_id must be a positive integer or None")
        for field_name in ("effective_model_alias", "required_model_alias", "required_model_id"):
            value = getattr(self, field_name)
            if value is None:
                continue
            alias = value.strip()
            if (
                not alias
                or len(alias) > 128
                or any(not (character.isalnum() or character in "._-") for character in alias)
            ):
                raise ValueError(f"{field_name} is invalid")
            object.__setattr__(self, field_name, alias)
        if self.required_model_id is not None and self.required_model_alias is None:
            raise ValueError("required_model_id requires required_model_alias")
        if not isinstance(self.boundary, DataBoundary):
            raise TypeError("boundary must be a DataBoundary")
        if not isinstance(self.task_kind, TaskKind):
            raise TypeError("task_kind must be a TaskKind")
        if not isinstance(self.complexity, TaskComplexity):
            raise TypeError("complexity must be a TaskComplexity")
        if not isinstance(self.risk, RiskLevel):
            raise TypeError("risk must be a RiskLevel")
        try:
            intent = BoundedIntent(self.intent)
        except (TypeError, ValueError) as exc:
            raise ValueError("intent is invalid") from exc
        object.__setattr__(self, "intent", intent)
        context_toolset = self.bounded_toolset
        if context_toolset is not None and not isinstance(context_toolset, BoundedToolSet):
            raise TypeError("bounded_toolset must be BoundedToolSet or None")
        authorization_context = self.context_authorization
        if authorization_context is not None:
            if not isinstance(authorization_context, ContextAuthorizationToken):
                raise TypeError("context_authorization must be ContextAuthorizationToken or None")
            if context_toolset is not None and authorization_context.contract_version != CONTEXT_CONTRACT_VERSION:
                raise ValueError("formal AI requests require the current context contract")
        if (
            type(self.uses_tools) is not bool
            or type(self.web_search) is not bool
            or type(self.has_side_effects) is not bool
            or type(self.contains_durable_memory) is not bool
        ):
            raise TypeError("uses_tools, web_search, has_side_effects and contains_durable_memory must be booleans")
        authorization = self.memory_authorization
        if (authorization is not None) != self.contains_durable_memory:
            raise ValueError("contains_durable_memory must match memory_authorization")
        if authorization is not None:
            if (
                authorization.scope.guild_id != self.guild_id
                or authorization.scope.user_id != self.user_id
                or authorization.request_channel_id != self.channel_id
            ):
                raise ValueError("memory_authorization must belong to the request identity")

        allowed_model_tools = tuple(self.allowed_model_tools)
        if any(not isinstance(tool, str) or not tool for tool in allowed_model_tools):
            raise TypeError("allowed_model_tools must contain non-empty strings")
        if len(allowed_model_tools) != len(set(allowed_model_tools)) or len(allowed_model_tools) > MAX_MODEL_TOOLS:
            raise ValueError("allowed_model_tools must be empty or contain only web_search")
        unknown_tools = set(allowed_model_tools) - SUPPORTED_MODEL_TOOLS
        if unknown_tools:
            raise ValueError("allowed_model_tools contains an unsupported model tool")
        if self.web_search:
            if allowed_model_tools != (WEB_SEARCH_TOOL_ID,):
                raise ValueError("web_search requests may expose only the web_search model tool")
            if not self.uses_tools:
                raise ValueError("web_search requests must declare uses_tools")
        elif allowed_model_tools:
            raise ValueError("model tools require their typed request flag")
        object.__setattr__(self, "allowed_model_tools", allowed_model_tools)

        max_tool_calls = self.max_tool_calls
        if isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int):
            raise TypeError("max_tool_calls must be an integer")
        if max_tool_calls != len(allowed_model_tools):
            raise ValueError("max_tool_calls must exactly match the 0/1 model toolset")
        object.__setattr__(self, "max_tool_calls", max_tool_calls)
        toolset = self.bounded_toolset
        if toolset is not None:
            scope = ToolScopeBinding(self.guild_id, self.channel_id, self.user_id)
            if toolset.scope != scope:
                raise ValueError("bounded_toolset scope must match the AI request")
            if toolset.intent is not intent or toolset.complexity.value != self.complexity.value:
                raise ValueError("bounded_toolset route must match the AI request")
            if toolset.effective_tools != allowed_model_tools or toolset.max_tool_calls != max_tool_calls:
                raise ValueError("bounded_toolset must match the effective model tool fields")
        if self.web_search and toolset is None:
            raise ValueError("web_search requires a formal bounded_toolset")
        authorization = self.tool_execution_authorization
        if authorization is not None:
            if not isinstance(authorization, ToolExecutionAuthorization):
                raise TypeError("tool_execution_authorization must be ToolExecutionAuthorization or None")
            if toolset is None or not toolset.effective_tools:
                raise ValueError("tool execution authorization requires a non-empty bounded_toolset")
            if authorization.toolset_digest != toolset.digest:
                raise ValueError("tool execution authorization does not match the bounded_toolset")

        history = tuple(self.history)
        if len(history) > MAX_HISTORY_MESSAGES:
            raise ValueError("history contains too many messages")
        if len(history) % 2:
            raise ValueError("history must contain complete user/assistant exchanges")
        for index, turn in enumerate(history):
            if not isinstance(turn, Turn):
                raise TypeError("history entries must be Turn instances")
            expected = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
            if turn.role is not expected:
                raise ValueError("history roles must alternate user and assistant")
        if sum(len(turn.text) for turn in history) > MAX_HISTORY_TEXT_CHARS:
            raise ValueError("history text is too large")
        object.__setattr__(self, "history", history)

        attachments = _validated_attachments(self.attachments)
        object.__setattr__(self, "attachments", attachments)
        binary_bytes = sum(turn.binary_bytes for turn in history) + sum(
            attachment.byte_length for attachment in attachments
        )
        if binary_bytes > MAX_REQUEST_BINARY_BYTES:
            raise ValueError("request attachments are too large")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(canonical_provider_metadata(self.metadata)),
        )
        if context_toolset is not None and authorization_context is None:
            raise ValueError("formal bounded AI requests require context authorization")
        if authorization_context is not None:
            if context_toolset is not None and (
                authorization_context.provider_envelope_sha256 is None
                or self.provider_input != FORMAL_PROVIDER_INPUT_DIRECTIVE
            ):
                raise ValueError("formal AI requests require the canonical provider envelope")
            if not self.context_authorization_current():
                raise ValueError("context_authorization does not match the final provider envelope")

    def context_authorization_current(self) -> bool:
        """Recompute the full prepared-context and provider-envelope binding."""

        authorization = self.context_authorization
        if not isinstance(authorization, ContextAuthorizationToken):
            return False
        toolset = self.bounded_toolset
        if toolset is not None and (
            authorization.contract_version != CONTEXT_CONTRACT_VERSION or authorization.provider_envelope_sha256 is None
        ):
            return False
        if toolset is not None and self.provider_input != FORMAL_PROVIDER_INPUT_DIRECTIVE:
            return False
        try:
            envelope_digest = (
                provider_facing_envelope_digest(
                    prompt=self.prompt,
                    provider_input=self.provider_input,
                    history=self.history,
                    attachments=self.attachments,
                    metadata=self.metadata,
                    task_kind=self.task_kind,
                    complexity=self.complexity,
                    risk=self.risk,
                    uses_tools=self.uses_tools,
                    web_search=self.web_search,
                    has_side_effects=self.has_side_effects,
                    boundary=self.boundary,
                    required_model_alias=self.required_model_alias,
                    required_model_id=self.required_model_id,
                )
                if authorization.provider_envelope_sha256 is not None
                else None
            )
            return authorization.matches(
                prompt=self.system_prompt,
                guild_id=self.guild_id,
                channel_id=self.channel_id,
                user_id=self.user_id,
                memory_authorization=self.memory_authorization,
                bounded_toolset_digest=None if toolset is None else toolset.digest,
                capability_catalog_revision=(None if toolset is None else toolset.capability_catalog_revision),
                provider_catalog_revision=(None if toolset is None else toolset.provider_catalog_revision),
                intent=None if toolset is None else self.intent.value,
                complexity=None if toolset is None else self.complexity.value,
                effective_tools=None if toolset is None else toolset.effective_tools,
                capability_metadata_sha256=(None if toolset is None else capability_metadata_digest(toolset)),
                provider_envelope_sha256=envelope_digest,
            )
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True, slots=True)
class AIReply:
    text: str = field(repr=False)
    model: str
    provider: str
    sources: tuple[AISource, ...] = ()
    artifact_references: tuple[ArtifactReference, ...] = field(default=(), repr=False)
    synthesis_action_id: str | None = None
    delivery_handled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("reply must not be empty")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must not be empty")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must not be empty")
        if type(self.delivery_handled) is not bool:
            raise TypeError("delivery_handled must be a bool")
        sources = tuple(self.sources)
        if len(sources) > MAX_REPLY_SOURCES:
            raise ValueError("reply contains too many sources")
        if any(not isinstance(source, AISource) for source in sources):
            raise TypeError("sources must contain only AISource instances")
        if len({source.url for source in sources}) != len(sources):
            raise ValueError("reply sources must be unique")
        artifact_references = tuple(self.artifact_references)
        if len(artifact_references) > MAX_REPLY_ARTIFACT_REFERENCES:
            raise ValueError("reply contains too many artifact references")
        if any(not isinstance(reference, ArtifactReference) for reference in artifact_references):
            raise TypeError("artifact_references must contain only ArtifactReference instances")
        if len({reference.artifact_id for reference in artifact_references}) != len(artifact_references):
            raise ValueError("reply artifact references must be unique")
        synthesis_action_id = self.synthesis_action_id
        if synthesis_action_id is not None:
            normalized_action_id = synthesis_action_id.strip().lower()
            if (
                not normalized_action_id
                or len(normalized_action_id) > 128
                or any(not (character.isalnum() or character in "._-") for character in normalized_action_id)
            ):
                raise ValueError("synthesis_action_id is invalid")
            object.__setattr__(self, "synthesis_action_id", normalized_action_id)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "artifact_references", artifact_references)


def _validated_attachments(value: object) -> tuple[Attachment, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError("attachments must be an iterable of Attachment instances")
    try:
        attachments = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError("attachments must be an iterable of Attachment instances") from exc
    if len(attachments) > MAX_ATTACHMENTS_PER_TURN:
        raise ValueError("too many attachments")
    if any(not isinstance(attachment, Attachment) for attachment in attachments):
        raise TypeError("attachments must contain only Attachment instances")
    if sum(attachment.byte_length for attachment in attachments) > MAX_REQUEST_BINARY_BYTES:
        raise ValueError("attachments are too large")
    return attachments


def provider_facing_envelope_digest(
    *,
    prompt: str,
    provider_input: str | None,
    history: tuple[Turn, ...],
    attachments: tuple[Attachment, ...],
    metadata: Mapping[str, str],
    task_kind: TaskKind,
    complexity: TaskComplexity,
    risk: RiskLevel,
    uses_tools: bool,
    web_search: bool,
    has_side_effects: bool,
    boundary: DataBoundary,
    required_model_alias: str | None = None,
    required_model_id: str | None = None,
) -> str:
    """Hash provider-facing inputs plus routing claims without retaining raw bytes."""

    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    if provider_input is not None and not isinstance(provider_input, str):
        raise TypeError("provider_input must be a string or None")
    if not isinstance(task_kind, TaskKind):
        raise TypeError("task_kind must be a TaskKind")
    if not isinstance(complexity, TaskComplexity):
        raise TypeError("complexity must be a TaskComplexity")
    if not isinstance(risk, RiskLevel):
        raise TypeError("risk must be a RiskLevel")
    if not isinstance(boundary, DataBoundary):
        raise TypeError("boundary must be a DataBoundary")
    if any(type(value) is not bool for value in (uses_tools, web_search, has_side_effects)):
        raise TypeError("provider route flags must be booleans")
    for label, value in (
        ("required_model_alias", required_model_alias),
        ("required_model_id", required_model_id),
    ):
        if value is not None and (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 128):
            raise ValueError(f"{label} is invalid")
    history = tuple(history)
    attachments = _validated_attachments(attachments)
    canonical_metadata = canonical_provider_metadata(metadata)
    if any(not isinstance(turn, Turn) for turn in history):
        raise TypeError("history must contain only Turn instances")
    provider_history = () if provider_input is not None else history
    effective_input = provider_input if provider_input is not None else prompt
    payload = {
        "surface_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "provider_input": {
            "present": provider_input is not None,
            "sha256": (None if provider_input is None else hashlib.sha256(provider_input.encode("utf-8")).hexdigest()),
        },
        "effective_input_sha256": hashlib.sha256(effective_input.encode("utf-8")).hexdigest(),
        "history": [_turn_envelope_claim(turn) for turn in provider_history],
        "attachments": [_attachment_envelope_claim(item) for item in attachments],
        "metadata": canonical_metadata,
        "route": {
            "boundary": boundary.value,
            "complexity": complexity.value,
            "has_side_effects": has_side_effects,
            "required_model_alias": required_model_alias,
            "required_model_id": required_model_id,
            "risk": risk.value,
            "task_kind": task_kind.value,
            "uses_tools": uses_tools,
            "web_search": web_search,
        },
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _turn_envelope_claim(turn: Turn) -> dict[str, object]:
    return {
        "role": turn.role.value,
        "text_sha256": hashlib.sha256(turn.text.encode("utf-8")).hexdigest(),
        "attachments": [_attachment_envelope_claim(item) for item in turn.attachments],
    }


def _attachment_envelope_claim(attachment: Attachment) -> dict[str, object]:
    return {
        "kind": attachment.kind.value,
        "mime_type": attachment.mime_type,
        "filename": attachment.filename,
        "detail": None if attachment.detail is None else attachment.detail.value,
        "byte_length": attachment.byte_length,
        "data_sha256": hashlib.sha256(attachment.data).hexdigest(),
    }


def canonical_provider_metadata(value: Mapping[str, str]) -> dict[str, str]:
    """Validate and deterministically order provider-visible string metadata."""

    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")
    copied = dict(value)
    if len(copied) > 16:
        raise ValueError("metadata must contain at most 16 entries")
    if any(not isinstance(key, str) for key in copied):
        raise ValueError("metadata keys must be strings")
    result: dict[str, str] = {}
    for key, item in sorted(copied.items()):
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ValueError("metadata keys must be normalized non-empty strings")
        if not isinstance(item, str):
            raise ValueError("metadata values must be strings")
        if (
            len(key.encode("utf-8")) > MAX_PROVIDER_METADATA_KEY_BYTES
            or len(item.encode("utf-8")) > MAX_PROVIDER_METADATA_VALUE_BYTES
        ):
            raise ValueError("metadata key or value exceeds the UTF-8 byte limit")
        inspection_key = unicodedata.normalize("NFKC", key).casefold()
        inspection_value = unicodedata.normalize("NFKC", item)
        normalized_key = "".join(character for character in inspection_key if character.isalnum())
        if normalized_key in _SENSITIVE_PROVIDER_METADATA_KEYS or (
            contains_secret_like(key)
            or contains_secret_like(item)
            or contains_secret_like(f"{inspection_key}={inspection_value}")
        ):
            raise ValueError("metadata must not contain secret-like values")
        if key not in PROVIDER_METADATA_ALLOWED_KEYS:
            raise ValueError("metadata key is not in the fixed provider allowlist")
        result[key] = item
    if (
        len(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        > MAX_PROVIDER_METADATA_TOTAL_BYTES
    ):
        raise ValueError("metadata exceeds the total UTF-8 byte limit")
    return result
