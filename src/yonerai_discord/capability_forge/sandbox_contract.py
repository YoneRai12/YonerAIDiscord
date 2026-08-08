"""Typed, external-only contract for future dynamic Forge sandbox work."""

from __future__ import annotations

import ast
import hashlib
import io
import ipaddress
import json
import re
import tokenize
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol


SANDBOX_POLICY_REVISION = "1"
MAX_SOURCE_BYTES = 32_768
MAX_JSON_BYTES = 16_384
MAX_OUTPUT_BYTES = 16_384
MAX_ARTIFACTS = 4
MAX_WALL_TIME_MS = 60_000
MAX_CPU_TIME_MS = 30_000
MAX_MEMORY_MIB = 512
MAX_PROCESSES = 0
MAX_FILES = 8
_MAX_STATIC_TEXT_FRAGMENTS = 512
_MAX_STATIC_TEXT_NODES = 2_048
_MAX_STATIC_TEXT_DEPTH = 64
_MAX_STATIC_TEXT_CANDIDATES = 64

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_NONCE = re.compile(r"[a-f0-9]{32}\Z")
_SECRET = re.compile(
    r"(?i)(?:\b(?:basic|bearer)\s+[a-z0-9._~+/=-]{4,}"
    r"|\b(?:api[_-]?key|authorization|cookie|token|secret|password)\s*[:=]\s*\S+"
    r"|\bsk-(?:proj-)?[a-z0-9_-]{8,}"
    r"|\bgithub_pat_[a-z0-9_]{8,}"
    r"|\bgh[pousr]_[a-z0-9]{8,}"
    r"|\bxox[baprs]-[a-z0-9-]{8,}"
    r"|\bAIza[a-z0-9_-]{16,}"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{12,})"
)
_HOST_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/]|\\\\|\\device\\|file:|\.\.[\\/]"
    r"|(?:^|[\s\"'=:(])/[a-z0-9._~-]+(?:/|$)"
    r"|\b(?:glob|import|open|exec|eval|compile|__import__|os|pathlib|shutil|socket|requests|urllib|subprocess|powershell|cmd\.exe)\b)"
)
_PRIVATE_KEY_MARKER = re.compile(r"(?i)-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----")
_NETWORK_LOCATOR = re.compile(
    r"(?i)(?:\b(?:https?|ftp|ws|wss)://"
    r"|\b(?:localhost|ip6-localhost|ip6-loopback)\b)"
)
_IP_LITERAL = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])"
    r"|(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"
)
_HOST_CONTROL_KEY_FRAGMENTS = frozenset(
    {
        "args",
        "argv",
        "cmd",
        "command",
        "cwd",
        "env",
        "environment",
        "exec",
        "file",
        "mount",
        "path",
        "process",
        "shell",
        "socket",
        "uri",
        "url",
    }
)
_SECRET_KEY_FRAGMENTS = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "password",
        "privatekey",
        "secret",
        "token",
    }
)


class SandboxContractError(ValueError):
    """A value is outside the sealed external-sandbox contract."""


class SandboxEntrypoint(StrEnum):
    PYTHON_PURE = "python_pure"


class SandboxTerminationReason(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SandboxScope:
    request_id: str
    guild_id: int | None
    channel_id: int | None
    user_id: int

    def __post_init__(self) -> None:
        _identifier(self.request_id)
        _optional_id(self.guild_id)
        _optional_id(self.channel_id)
        _positive_id(self.user_id)

    def to_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "channel_id": self.channel_id,
                "guild_id": self.guild_id,
                "request_id": self.request_id,
                "user_id": self.user_id,
            }
        )


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    revision: str = SANDBOX_POLICY_REVISION
    language: SandboxEntrypoint = SandboxEntrypoint.PYTHON_PURE
    max_source_bytes: int = MAX_SOURCE_BYTES
    max_input_bytes: int = MAX_JSON_BYTES
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_cpu_time_ms: int = MAX_CPU_TIME_MS
    max_wall_time_ms: int = MAX_WALL_TIME_MS
    max_memory_mib: int = MAX_MEMORY_MIB
    max_processes: int = MAX_PROCESSES
    max_files: int = MAX_FILES
    network_connections: int = 0
    host_mount: bool = False
    secret_access: bool = False
    environment_access: bool = False
    privileged: bool = False
    docker_socket: bool = False
    clipboard: bool = False
    persistent_profile: bool = False

    def __post_init__(self) -> None:
        if self.revision != SANDBOX_POLICY_REVISION or self.language is not SandboxEntrypoint.PYTHON_PURE:
            raise SandboxContractError("policy is not code-owned")
        for value, maximum in (
            (self.max_source_bytes, MAX_SOURCE_BYTES),
            (self.max_input_bytes, MAX_JSON_BYTES),
            (self.max_output_bytes, MAX_OUTPUT_BYTES),
            (self.max_cpu_time_ms, MAX_CPU_TIME_MS),
            (self.max_wall_time_ms, MAX_WALL_TIME_MS),
            (self.max_memory_mib, MAX_MEMORY_MIB),
            (self.max_files, MAX_FILES),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise SandboxContractError("policy limit is invalid")
        if type(self.max_processes) is not int or self.max_processes != 0:
            raise SandboxContractError("child process limit must be zero")
        if (
            type(self.network_connections) is not int
            or self.network_connections != 0
            or any(
                value is not False
                for value in (
                    self.host_mount,
                    self.secret_access,
                    self.environment_access,
                    self.privileged,
                    self.docker_socket,
                    self.clipboard,
                    self.persistent_profile,
                )
            )
        ):
            raise SandboxContractError("policy containment flags are not exact")

    @property
    def digest(self) -> str:
        encoded = json.dumps(dict(self.to_mapping()), separators=(",", ":"), sort_keys=True).encode("ascii")
        return hashlib.sha256(b"yonerai.capability_forge.sandbox.policy.v1\0" + encoded).hexdigest()

    def to_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "clipboard": self.clipboard,
                "docker_socket": self.docker_socket,
                "environment_access": self.environment_access,
                "host_mount": self.host_mount,
                "language": self.language.value,
                "max_cpu_time_ms": self.max_cpu_time_ms,
                "max_files": self.max_files,
                "max_input_bytes": self.max_input_bytes,
                "max_memory_mib": self.max_memory_mib,
                "max_output_bytes": self.max_output_bytes,
                "max_processes": self.max_processes,
                "max_source_bytes": self.max_source_bytes,
                "max_wall_time_ms": self.max_wall_time_ms,
                "network_connections": self.network_connections,
                "persistent_profile": self.persistent_profile,
                "privileged": self.privileged,
                "revision": self.revision,
                "secret_access": self.secret_access,
            }
        )


@dataclass(frozen=True, slots=True)
class SandboxCandidate:
    source: str = field(repr=False)
    entrypoint: SandboxEntrypoint = SandboxEntrypoint.PYTHON_PURE
    input_data: object = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.entrypoint is not SandboxEntrypoint.PYTHON_PURE:
            raise SandboxContractError("entrypoint is not allowed")
        _safe_text(self.source, MAX_SOURCE_BYTES, python_source=True)
        frozen = _freeze_json(self.input_data, MAX_JSON_BYTES)
        object.__setattr__(self, "input_data", frozen)

    @property
    def canonical_input_bytes(self) -> bytes:
        return json.dumps(
            _thaw_value(self.input_data), ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("ascii")


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    candidate: SandboxCandidate
    scope: SandboxScope
    policy: SandboxPolicy
    backend_identity: str
    backend_generation: int
    session_nonce: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, SandboxCandidate) or not isinstance(self.scope, SandboxScope):
            raise SandboxContractError("request must be typed")
        if not isinstance(self.policy, SandboxPolicy):
            raise SandboxContractError("request policy must be typed")
        _identifier(self.backend_identity)
        _positive_generation(self.backend_generation)
        if not isinstance(self.session_nonce, str) or _NONCE.fullmatch(self.session_nonce) is None:
            raise SandboxContractError("session nonce is invalid")

    @property
    def policy_digest(self) -> str:
        return self.policy.digest

    @property
    def request_digest(self) -> str:
        encoded = json.dumps(
            {
                "backend_generation": self.backend_generation,
                "backend_identity": self.backend_identity,
                "entrypoint": self.candidate.entrypoint.value,
                "input": _thaw_value(self.candidate.input_data),
                "policy_digest": self.policy_digest,
                "scope": dict(self.scope.to_mapping()),
                "session_nonce": self.session_nonce,
                "source": self.candidate.source,
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(b"yonerai.capability_forge.sandbox.request.v1\0" + encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SandboxHandshake:
    scope: SandboxScope
    backend_identity: str
    backend_generation: int
    policy_digest: str
    request_digest: str
    session_nonce: str = field(repr=False)
    network_connections: int
    host_mount: bool
    secret_access: bool
    environment_access: bool
    privileged: bool
    docker_socket: bool
    clipboard: bool
    persistent_profile: bool
    child_processes: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SandboxScope):
            raise SandboxContractError("handshake scope is invalid")
        _identifier(self.backend_identity)
        _positive_generation(self.backend_generation)
        _digest(self.policy_digest)
        _digest(self.request_digest)
        _nonce(self.session_nonce)
        if type(self.network_connections) is not int or self.network_connections != 0:
            raise SandboxContractError("handshake network contract is invalid")
        if type(self.child_processes) is not int or self.child_processes != 0:
            raise SandboxContractError("handshake process contract is invalid")
        if any(
            value is not False
            for value in (
                self.host_mount,
                self.secret_access,
                self.environment_access,
                self.privileged,
                self.docker_socket,
                self.clipboard,
                self.persistent_profile,
            )
        ):
            raise SandboxContractError("handshake containment contract is invalid")


@dataclass(frozen=True, slots=True)
class SandboxArtifactDescriptor:
    opaque_id: str = field(repr=False)
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        _identifier(self.opaque_id)
        if self.media_type not in {"application/json", "text/plain"}:
            raise SandboxContractError("artifact type is invalid")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= MAX_OUTPUT_BYTES
        ):
            raise SandboxContractError("artifact size is invalid")


@dataclass(frozen=True, slots=True)
class SandboxResult:
    scope: SandboxScope
    backend_identity: str
    backend_generation: int
    policy_digest: str
    request_digest: str
    session_nonce: str = field(repr=False)
    output: object = field(repr=False)
    artifacts: tuple[SandboxArtifactDescriptor, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.backend_identity)
        _positive_generation(self.backend_generation)
        if not isinstance(self.scope, SandboxScope):
            raise SandboxContractError("result binding is invalid")
        _digest(self.policy_digest)
        _digest(self.request_digest)
        _nonce(self.session_nonce)
        object.__setattr__(self, "output", _freeze_json(self.output, MAX_OUTPUT_BYTES))
        if not isinstance(self.artifacts, tuple) or len(self.artifacts) > MAX_ARTIFACTS:
            raise SandboxContractError("artifact list is invalid")
        if any(type(item) is not SandboxArtifactDescriptor for item in self.artifacts):
            raise SandboxContractError("artifact list is invalid")


@dataclass(frozen=True, slots=True)
class SandboxTerminationReceipt:
    scope: SandboxScope
    backend_identity: str
    backend_generation: int
    policy_digest: str
    request_digest: str
    session_nonce: str = field(repr=False)
    reason: SandboxTerminationReason
    worker_terminated: bool
    workspace_destroyed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SandboxScope) or not isinstance(self.reason, SandboxTerminationReason):
            raise SandboxContractError("termination receipt is invalid")
        _identifier(self.backend_identity)
        _positive_generation(self.backend_generation)
        _digest(self.policy_digest)
        _digest(self.request_digest)
        _nonce(self.session_nonce)
        if self.worker_terminated is not True or self.workspace_destroyed is not True:
            raise SandboxContractError("termination receipt is unconfirmed")


class ExternalSandboxPort(Protocol):
    async def handshake(self, request: SandboxRequest) -> SandboxHandshake: ...

    async def execute(self, request: SandboxRequest) -> SandboxResult: ...

    async def terminate(
        self, request: SandboxRequest, reason: SandboxTerminationReason
    ) -> SandboxTerminationReceipt: ...


def _identifier(value: object) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise SandboxContractError("identifier is invalid")


def _digest(value: object) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise SandboxContractError("digest is invalid")


def _nonce(value: object) -> None:
    if not isinstance(value, str) or _NONCE.fullmatch(value) is None:
        raise SandboxContractError("session nonce is invalid")


def _positive_id(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SandboxContractError("scope identifier is invalid")


def _positive_generation(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise SandboxContractError("backend generation is invalid")


def _optional_id(value: object) -> None:
    if value is not None:
        _positive_id(value)


def _safe_text(value: object, maximum_bytes: int, *, python_source: bool = False) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(char) < 32 and char not in "\n\t" for char in value)
    ):
        raise SandboxContractError("text is invalid")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise SandboxContractError("text is not UTF-8") from exc
    if len(encoded) > maximum_bytes:
        raise SandboxContractError("text is outside sandbox contract")
    if _contains_forbidden_text(value, include_ip=not python_source) or (
        python_source and _contains_python_static_forbidden_text(value)
    ):
        raise SandboxContractError("text is outside sandbox contract")


def _contains_ip_literal(value: str) -> bool:
    for candidate in _IP_LITERAL.finditer(value):
        try:
            ipaddress.ip_address(candidate.group(0))
        except ValueError:
            continue
        return True
    return False


def _contains_forbidden_text(value: str, *, include_ip: bool = True) -> bool:
    return bool(
        _SECRET.search(value)
        or _HOST_PATH.search(value)
        or _PRIVATE_KEY_MARKER.search(value)
        or _NETWORK_LOCATOR.search(value)
        or (include_ip and _contains_ip_literal(value))
    )


def _contains_python_static_forbidden_text(value: str) -> bool:
    """Reject statically reconstructable data while leaving slice operators alone."""

    try:
        tree = ast.parse(value, mode="exec")
        fragments: list[str] = []
        _collect_static_text_fragments(tree, fragments, depth=0, nodes=[0])
        if any(_contains_forbidden_text(fragment) for fragment in fragments):
            return True
        if _static_text_block_contains_forbidden(tree.body, {}):
            return True
        tokens = tokenize.generate_tokens(io.StringIO(value).readline)
        return any(token.type == tokenize.COMMENT and _contains_forbidden_text(token.string) for token in tokens)
    except (IndentationError, RecursionError, SyntaxError, tokenize.TokenError, ValueError):
        return True


def _collect_static_text_fragments(
    node: ast.AST,
    fragments: list[str],
    *,
    depth: int,
    nodes: list[int],
) -> None:
    if depth > _MAX_STATIC_TEXT_DEPTH or nodes[0] >= _MAX_STATIC_TEXT_NODES:
        raise ValueError("Python source is too complex")
    nodes[0] += 1
    if isinstance(node, ast.Constant) and isinstance(node.value, (bytes, str)):
        literal = node.value.decode("ascii", "ignore") if isinstance(node.value, bytes) else node.value
        fragments.append(literal)
        if len(fragments) > _MAX_STATIC_TEXT_FRAGMENTS or sum(len(item) for item in fragments) > MAX_SOURCE_BYTES:
            raise ValueError("Python static text is too large")
        return
    for child in ast.iter_child_nodes(node):
        _collect_static_text_fragments(child, fragments, depth=depth + 1, nodes=nodes)


_StaticTextValue = tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class _StaticTextBinding:
    values: tuple[_StaticTextValue, ...]
    uncertain: bool = False


def _static_text_block_contains_forbidden(
    statements: list[ast.stmt],
    environment: dict[str, _StaticTextBinding],
) -> bool:
    for statement in statements:
        for expression in _statement_header_expressions(statement):
            if _static_text_expression_contains_forbidden(expression, environment):
                return True

        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            assigned = _static_text_value(statement.value, environment) if statement.value is not None else None
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                _update_static_text_bindings(target, assigned, environment)
        elif isinstance(statement, ast.AugAssign):
            assigned = None
            if isinstance(statement.op, ast.Add) and isinstance(statement.target, ast.Name):
                left = environment.get(statement.target.id)
                right = _static_text_value(statement.value, environment)
                assigned = _combine_static_text(left, right)
                if assigned is not None and any(_contains_forbidden_text(value[1]) for value in assigned.values):
                    return True
            _update_static_text_bindings(statement.target, assigned, environment)
        elif isinstance(statement, ast.Delete):
            for target in statement.targets:
                for name in _bound_static_text_names(target):
                    environment[name] = _StaticTextBinding((), uncertain=True)

        if isinstance(statement, ast.If):
            if isinstance(statement.test, ast.Constant) and isinstance(statement.test.value, bool):
                selected = statement.body if statement.test.value else statement.orelse
                if _static_text_block_contains_forbidden(selected, environment):
                    return True
            elif _merge_static_text_branches((statement.body, statement.orelse), environment):
                return True
        elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            loop_environment = dict(environment)
            if isinstance(statement, (ast.For, ast.AsyncFor)):
                _mark_static_text_bindings_uncertain(statement.target, loop_environment)
            if _static_text_block_contains_forbidden(statement.body, loop_environment):
                return True
            paths = [dict(environment), loop_environment]
            if statement.orelse:
                orelse_environment = _merge_static_text_environments(paths)
                if _static_text_block_contains_forbidden(statement.orelse, orelse_environment):
                    return True
                paths.append(orelse_environment)
            environment.clear()
            environment.update(_merge_static_text_environments(paths))
            _mark_static_text_names_uncertain(_mutated_static_text_names(statement.body), environment)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                if item.optional_vars is not None:
                    _mark_static_text_bindings_uncertain(item.optional_vars, environment)
            if _static_text_block_contains_forbidden(statement.body, environment):
                return True
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            paths: list[dict[str, _StaticTextBinding]] = []
            successful = dict(environment)
            if _static_text_block_contains_forbidden(statement.body, successful):
                return True
            if statement.orelse and _static_text_block_contains_forbidden(statement.orelse, successful):
                return True
            paths.append(successful)
            for handler in statement.handlers:
                handled = dict(environment)
                if handler.name is not None:
                    handled[handler.name] = _StaticTextBinding((), uncertain=True)
                if _static_text_block_contains_forbidden(handler.body, handled):
                    return True
                if handler.name is not None:
                    handled.pop(handler.name, None)
                paths.append(handled)
            merged = _merge_static_text_environments(paths)
            if statement.handlers:
                _mark_static_text_names_uncertain(_mutated_static_text_names(statement.body), merged)
            if statement.finalbody and _static_text_block_contains_forbidden(statement.finalbody, merged):
                return True
            environment.clear()
            environment.update(merged)
        elif isinstance(statement, ast.Match):
            branches = [case.body for case in statement.cases]
            branches.append([])
            if _merge_static_text_branches(tuple(branches), environment):
                return True
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested_environment = dict(environment)
            _remove_argument_bindings(statement.args, nested_environment)
            if _static_text_block_contains_forbidden(statement.body, nested_environment):
                return True
            environment.pop(statement.name, None)
        elif isinstance(statement, ast.ClassDef):
            if _static_text_block_contains_forbidden(statement.body, dict(environment)):
                return True
            environment.pop(statement.name, None)
    return False


def _merge_static_text_branches(
    branches: tuple[list[ast.stmt], ...],
    environment: dict[str, _StaticTextBinding],
) -> bool:
    outcomes: list[dict[str, _StaticTextBinding]] = []
    for branch in branches:
        outcome = dict(environment)
        if _static_text_block_contains_forbidden(branch, outcome):
            return True
        outcomes.append(outcome)
    environment.clear()
    environment.update(_merge_static_text_environments(outcomes))
    return False


def _merge_static_text_environments(
    environments: list[dict[str, _StaticTextBinding]],
) -> dict[str, _StaticTextBinding]:
    merged: dict[str, _StaticTextBinding] = {}
    names = {name for environment in environments for name in environment}
    for name in names:
        bindings = [environment.get(name) for environment in environments]
        values = tuple(dict.fromkeys(value for binding in bindings if binding is not None for value in binding.values))
        if len(values) > _MAX_STATIC_TEXT_CANDIDATES:
            raise ValueError("Python static text has too many candidates")
        merged[name] = _StaticTextBinding(
            values,
            uncertain=any(binding is None or binding.uncertain for binding in bindings),
        )
    return merged


def _statement_header_expressions(statement: ast.stmt) -> tuple[ast.expr, ...]:
    expressions: list[ast.expr] = []

    def collect(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                continue
            if isinstance(child, ast.expr):
                expressions.append(child)
            else:
                collect(child)

    collect(statement)
    return tuple(expressions)


def _static_text_expression_contains_forbidden(
    node: ast.expr,
    environment: dict[str, _StaticTextBinding],
) -> bool:
    if isinstance(node, ast.Lambda):
        nested_environment = dict(environment)
        _remove_argument_bindings(node.args, nested_environment)
        return _static_text_expression_contains_forbidden(node.body, nested_environment)
    value = _static_text_value(node, environment)
    if value is not None and any(_contains_forbidden_text(candidate[1]) for candidate in value.values):
        return True
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr) and _static_text_expression_contains_forbidden(child, environment):
            return True
    return False


def _static_text_value(
    node: ast.expr,
    environment: dict[str, _StaticTextBinding],
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
) -> _StaticTextBinding | None:
    if nodes is None:
        nodes = [0]
    if depth > _MAX_STATIC_TEXT_DEPTH or nodes[0] >= _MAX_STATIC_TEXT_NODES:
        raise ValueError("Python static expression is too complex")
    nodes[0] += 1

    if isinstance(node, ast.Constant) and isinstance(node.value, (bytes, str)):
        if isinstance(node.value, bytes):
            return _StaticTextBinding((("bytes", node.value.decode("ascii", "ignore"), 1),))
        return _StaticTextBinding((("str", node.value, 1),))
    if isinstance(node, ast.Name):
        return environment.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_text_value(node.left, environment, depth=depth + 1, nodes=nodes)
        right = _static_text_value(node.right, environment, depth=depth + 1, nodes=nodes)
        return _combine_static_text(left, right)
    if isinstance(node, ast.JoinedStr):
        combined = _StaticTextBinding((("str", "", 0),))
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                part: _StaticTextBinding | None = _StaticTextBinding((("str", item.value, 1),))
            elif (
                isinstance(item, ast.FormattedValue) and item.conversion in {-1, ord("s")} and item.format_spec is None
            ):
                evaluated = _static_text_value(item.value, environment, depth=depth + 1, nodes=nodes)
                part = (
                    None
                    if evaluated is None
                    else _StaticTextBinding(
                        tuple(("str", value[1], value[2]) for value in evaluated.values),
                        uncertain=evaluated.uncertain,
                    )
                )
            else:
                return None
            combined = _combine_static_text(combined, part)
            if combined is None:
                return None
        return combined
    return None


def _combine_static_text(
    left: _StaticTextBinding | None,
    right: _StaticTextBinding | None,
) -> _StaticTextBinding | None:
    if (left is not None and left.uncertain) or (right is not None and right.uncertain):
        raise ValueError("Python static text depends on an uncertain mutation")
    if left is None or right is None:
        return None
    combined: list[_StaticTextValue] = []
    for left_value in left.values:
        for right_value in right.values:
            if left_value[0] != right_value[0]:
                continue
            fragments = left_value[2] + right_value[2]
            text = left_value[1] + right_value[1]
            if fragments > _MAX_STATIC_TEXT_FRAGMENTS or len(text.encode("utf-8", "strict")) > MAX_SOURCE_BYTES:
                raise ValueError("Python static text is too large")
            combined.append((left_value[0], text, fragments))
            if len(combined) > _MAX_STATIC_TEXT_CANDIDATES:
                raise ValueError("Python static text has too many candidates")
    return _StaticTextBinding(tuple(dict.fromkeys(combined))) if combined else None


def _update_static_text_bindings(
    target: ast.expr,
    value: _StaticTextBinding | None,
    environment: dict[str, _StaticTextBinding],
) -> None:
    for name in _bound_static_text_names(target):
        if value is None:
            environment[name] = _StaticTextBinding((), uncertain=True)
        elif not isinstance(target, ast.Name):
            environment[name] = _StaticTextBinding((), uncertain=True)
        else:
            environment[name] = value


def _mark_static_text_bindings_uncertain(
    target: ast.expr,
    environment: dict[str, _StaticTextBinding],
) -> None:
    for name in _bound_static_text_names(target):
        environment[name] = _StaticTextBinding((), uncertain=True)


def _mark_static_text_names_uncertain(
    names: set[str],
    environment: dict[str, _StaticTextBinding],
) -> None:
    for name in names:
        binding = environment.get(name)
        environment[name] = (
            _StaticTextBinding((), uncertain=True)
            if binding is None
            else _StaticTextBinding(
                binding.values,
                uncertain=True,
            )
        )


def _mutated_static_text_names(statements: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    names.update(_bound_static_text_names(target))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                names.update(_bound_static_text_names(node.target))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                names.update(_bound_static_text_names(node.target))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        names.update(_bound_static_text_names(item.optional_vars))
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    names.update(_bound_static_text_names(target))
    return names


def _bound_static_text_names(target: ast.expr) -> tuple[str, ...]:
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, (ast.List, ast.Tuple)):
        return tuple(name for item in target.elts for name in _bound_static_text_names(item))
    if isinstance(target, ast.Starred):
        return _bound_static_text_names(target.value)
    return ()


def _remove_argument_bindings(arguments: ast.arguments, environment: dict[str, _StaticTextBinding]) -> None:
    for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
        environment.pop(argument.arg, None)
    if arguments.vararg is not None:
        environment.pop(arguments.vararg.arg, None)
    if arguments.kwarg is not None:
        environment.pop(arguments.kwarg.arg, None)


def _freeze_json(value: object, maximum_bytes: int) -> object:
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SandboxContractError("JSON data is invalid") from exc
    if len(encoded) > maximum_bytes:
        raise SandboxContractError("JSON data exceeds its bound")
    _scan_json(decoded)
    return _freeze_value(decoded)


def _scan_json(value: object) -> None:
    if isinstance(value, str):
        _safe_json_text(value, MAX_JSON_BYTES)
    elif isinstance(value, list):
        if len(value) > 64:
            raise SandboxContractError("JSON collection is too large")
        for item in value:
            _scan_json(item)
    elif isinstance(value, dict):
        if len(value) > 64:
            raise SandboxContractError("JSON object is too large")
        for key, item in value.items():
            _safe_json_text(key, 128)
            normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
            if any(fragment in normalized_key for fragment in _HOST_CONTROL_KEY_FRAGMENTS):
                raise SandboxContractError("host-control JSON key is outside sandbox contract")
            if any(fragment in normalized_key for fragment in _SECRET_KEY_FRAGMENTS):
                raise SandboxContractError("secret-bearing JSON key is outside sandbox contract")
            _scan_json(item)
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise SandboxContractError("JSON value is invalid")


def _safe_json_text(value: object, maximum_bytes: int) -> None:
    _safe_text(value, maximum_bytes)
    if any(character in value for character in ("*", "?", "[")):
        raise SandboxContractError("glob-like JSON text is outside sandbox contract")


def _freeze_value(value: object) -> object:
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in sorted(value.items())})
    return value


def _thaw_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    return value


__all__ = [
    "ExternalSandboxPort",
    "SANDBOX_POLICY_REVISION",
    "SandboxArtifactDescriptor",
    "SandboxCandidate",
    "SandboxContractError",
    "SandboxEntrypoint",
    "SandboxHandshake",
    "SandboxPolicy",
    "SandboxRequest",
    "SandboxResult",
    "SandboxScope",
    "SandboxTerminationReceipt",
    "SandboxTerminationReason",
]
