from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


KNOWN_EVENT_KINDS = frozenset(
    {
        "status",
        "text_delta",
        "artifact",
        "action_required",
        "tool_result",
        "final",
        "error",
    }
)
TERMINAL_EVENT_KINDS = frozenset({"final", "error"})


def _identifier(value: object, *, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError(f"{label} is invalid")
    return normalized


def _optional_identifier(value: object, *, label: str, maximum: int = 256) -> str | None:
    if value is None:
        return None
    return _identifier(value, label=label, maximum=maximum)


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    copied = dict(value)
    if any(not isinstance(key, str) or not key.strip() for key in copied):
        raise ValueError(f"{label} keys must be non-empty strings")
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """実体を境界へ埋め込まずに扱う、provider非依存のartifact参照。"""

    artifact_id: str
    kind: str
    uri: str | None = None
    name: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    extensions: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _identifier(self.artifact_id, label="artifact_id"))
        object.__setattr__(self, "kind", _identifier(self.kind, label="artifact kind", maximum=128))
        if self.uri is not None:
            if not isinstance(self.uri, str):
                raise TypeError("artifact uri must be a string or None")
            uri = self.uri.strip()
            if not uri or len(uri) > 4_096 or any(ord(character) < 32 or ord(character) == 127 for character in uri):
                raise ValueError("artifact uri is invalid")
            object.__setattr__(self, "uri", uri)
        object.__setattr__(self, "name", _optional_identifier(self.name, label="artifact name", maximum=512))
        object.__setattr__(
            self,
            "media_type",
            _optional_identifier(self.media_type, label="artifact media_type", maximum=256),
        )
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0
        ):
            raise ValueError("artifact size_bytes must be a non-negative integer or None")
        object.__setattr__(self, "metadata", _mapping(self.metadata, label="artifact metadata"))
        object.__setattr__(self, "extensions", _mapping(self.extensions, label="artifact extensions"))


@dataclass(frozen=True, slots=True)
class RunInput:
    """ExecutionGatewayへ渡す中立入力。

    ``local_payload`` は既存Local Runtimeを全面書換えせず包むためだけのopaque値で、
    将来adapterが解釈する契約には含めない。``authorization_check`` もLocal側の
    commit直前再認可を維持するためのcallbackであり、provider request schemaではない。
    """

    input_text: str = field(repr=False)
    idempotency_key: str
    conversation_key: str | None = None
    artifacts: tuple[ArtifactReference, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    extensions: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    local_payload: object | None = field(default=None, repr=False, compare=False)
    authorization_check: Callable[[], bool] | None = field(default=None, repr=False, compare=False)
    fresh_authorization_check: Callable[[], bool | Awaitable[bool]] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    capability_authorization_check: Callable[[str], bool] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.input_text, str):
            raise TypeError("input_text must be a string")
        input_text = self.input_text.strip()
        if not input_text:
            raise ValueError("input_text must not be empty")
        object.__setattr__(self, "input_text", input_text)
        object.__setattr__(
            self,
            "idempotency_key",
            _identifier(self.idempotency_key, label="idempotency_key", maximum=512),
        )
        object.__setattr__(
            self,
            "conversation_key",
            _optional_identifier(self.conversation_key, label="conversation_key", maximum=512),
        )
        artifacts = tuple(self.artifacts)
        if any(not isinstance(artifact, ArtifactReference) for artifact in artifacts):
            raise TypeError("artifacts must contain only ArtifactReference instances")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "metadata", _mapping(self.metadata, label="run metadata"))
        object.__setattr__(self, "extensions", _mapping(self.extensions, label="run extensions"))
        if self.authorization_check is not None and not callable(self.authorization_check):
            raise TypeError("authorization_check must be callable or None")
        if self.fresh_authorization_check is not None and not callable(self.fresh_authorization_check):
            raise TypeError("fresh_authorization_check must be callable or None")
        if self.capability_authorization_check is not None and not callable(self.capability_authorization_check):
            raise TypeError("capability_authorization_check must be callable or None")


@dataclass(frozen=True, slots=True)
class RunReference:
    run_id: str
    idempotency_key: str
    reused: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier(self.run_id, label="run_id"))
        object.__setattr__(
            self,
            "idempotency_key",
            _identifier(self.idempotency_key, label="idempotency_key", maximum=512),
        )
        if type(self.reused) is not bool:
            raise TypeError("reused must be a boolean")


@dataclass(frozen=True, slots=True)
class RunEvent:
    """拡張可能なrun event。

    ``kind`` はenumへ閉じない。未知kindもその文字列とpayloadを保持したままstreamする。
    ``run_id`` と ``sequence`` はLocalExecutionGatewayが発行時に正規化する。
    """

    kind: str
    text: str | None = field(default=None, repr=False)
    artifact: ArtifactReference | None = None
    payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    extensions: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    run_id: str | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _identifier(self.kind, label="event kind", maximum=128))
        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("event text must be a string or None")
        if self.artifact is not None and not isinstance(self.artifact, ArtifactReference):
            raise TypeError("event artifact must be an ArtifactReference or None")
        object.__setattr__(self, "payload", _mapping(self.payload, label="event payload"))
        object.__setattr__(self, "extensions", _mapping(self.extensions, label="event extensions"))
        object.__setattr__(self, "run_id", _optional_identifier(self.run_id, label="run_id"))
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("event sequence must be a non-negative integer")

    @property
    def terminal(self) -> bool:
        return self.kind in TERMINAL_EVENT_KINDS

    @property
    def known(self) -> bool:
        return self.kind in KNOWN_EVENT_KINDS


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """action/tool continuationへ返す中立結果。"""

    result_id: str
    capability: str
    output: object | None = field(default=None, repr=False)
    is_error: bool = False
    artifacts: tuple[ArtifactReference, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)
    extensions: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _identifier(self.result_id, label="result_id"))
        object.__setattr__(self, "capability", _identifier(self.capability, label="capability", maximum=256))
        if type(self.is_error) is not bool:
            raise TypeError("is_error must be a boolean")
        artifacts = tuple(self.artifacts)
        if any(not isinstance(artifact, ArtifactReference) for artifact in artifacts):
            raise TypeError("artifacts must contain only ArtifactReference instances")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "metadata", _mapping(self.metadata, label="result metadata"))
        object.__setattr__(self, "extensions", _mapping(self.extensions, label="result extensions"))


__all__ = [
    "ArtifactReference",
    "CapabilityResult",
    "KNOWN_EVENT_KINDS",
    "RunEvent",
    "RunInput",
    "RunReference",
    "TERMINAL_EVENT_KINDS",
]
