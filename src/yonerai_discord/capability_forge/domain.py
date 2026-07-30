"""宣言recipeとcode-owned pure primitiveの不変契約。"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias


RECIPE_SCHEMA = "yonerai.capability_forge.recipe.v1"
FORGE_POLICY_REVISION = "1"
MAX_RECIPE_STEPS = 8
MAX_RECIPE_DEPTH = 4
MAX_RECIPE_INPUTS = 16
MAX_TEXT_CHARS = 4_096
MAX_TEXT_BYTES = 8_192
MAX_RECIPE_BYTES = 65_536
MAX_RUN_OUTPUT_BYTES = 65_536
MAX_COLLECTION_ITEMS = 64

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_PRIMITIVE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_REVISION = re.compile(r"[1-9][0-9]{0,15}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
JsonObject: TypeAlias = Mapping[str, JsonValue]
ForgeValidator: TypeAlias = Callable[[JsonObject], Awaitable[Mapping[str, object]]]
ForgeExecutor: TypeAlias = Callable[[JsonObject], Awaitable[Mapping[str, object]]]


class ForgeFailureCode(StrEnum):
    INVALID_RECIPE = "invalid_recipe"
    LIMIT_EXCEEDED = "limit_exceeded"
    DUPLICATE_STEP = "duplicate_step"
    SPOOFED_EFFECT = "spoofed_effect"
    UNSAFE_PATH = "unsafe_path"
    UNKNOWN_PRIMITIVE = "unknown_primitive"
    REVISION_MISMATCH = "revision_mismatch"
    UNKNOWN_REFERENCE = "unknown_reference"
    FORWARD_REFERENCE = "forward_reference"
    DEPENDENCY_CYCLE = "dependency_cycle"
    INPUT_VALIDATION_FAILED = "input_validation_failed"
    EXECUTOR_FAILED = "executor_failed"
    OUTPUT_VALIDATION_FAILED = "output_validation_failed"
    STEP_TIMEOUT = "step_timeout"
    TOTAL_TIMEOUT = "total_timeout"
    MANIFEST_REJECTED = "manifest_rejected"


class RecipeRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class RecipeReuseClass(StrEnum):
    TEMPLATE_REUSABLE = "template_reusable"


class RecipePublicationState(StrEnum):
    UNOFFICIAL = "unofficial"


class RecipeReadinessState(StrEnum):
    NOT_CLAIMED = "not_claimed"


class ForgeValidationError(ValueError):
    """外へはstable codeだけを返し、例外本文や入力値を漏らさない。"""

    def __init__(self, code: ForgeFailureCode) -> None:
        self.code = ForgeFailureCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class StepOutputRef:
    step_id: str
    output_key: str

    def __post_init__(self) -> None:
        _validate_identifier(self.step_id)
        _validate_identifier(self.output_key)


RecipeInputValue: TypeAlias = JsonValue | StepOutputRef


@dataclass(frozen=True, slots=True)
class RecipeStep:
    step_id: str
    primitive_id: str
    primitive_revision: str
    inputs: Mapping[str, RecipeInputValue]

    def __post_init__(self) -> None:
        _validate_identifier(self.step_id)
        _validate_primitive_id(self.primitive_id)
        _validate_revision(self.primitive_revision)
        if not isinstance(self.inputs, Mapping) or len(self.inputs) > MAX_RECIPE_INPUTS:
            raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)
        normalized: dict[str, RecipeInputValue] = {}
        for key, value in self.inputs.items():
            _validate_identifier(key)
            if isinstance(value, StepOutputRef):
                normalized[key] = value
            else:
                normalized[key] = freeze_json_value(value)
        object.__setattr__(self, "inputs", MappingProxyType(dict(sorted(normalized.items()))))


@dataclass(frozen=True, slots=True)
class RecipeCandidate:
    steps: tuple[RecipeStep, ...]
    declared_effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.steps, tuple) or not 1 <= len(self.steps) <= MAX_RECIPE_STEPS:
            raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)
        if any(not isinstance(step, RecipeStep) for step in self.steps):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        step_ids = tuple(step.step_id for step in self.steps)
        if len(set(step_ids)) != len(step_ids):
            raise ForgeValidationError(ForgeFailureCode.DUPLICATE_STEP)
        if not isinstance(self.declared_effects, tuple) or any(
            not isinstance(effect, str) for effect in self.declared_effects
        ):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        if sum(len(step.inputs) for step in self.steps) > MAX_RECIPE_INPUTS:
            raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)
        if len(self.canonical_bytes) > MAX_RECIPE_BYTES:
            raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> RecipeCandidate:
        if not isinstance(payload, Mapping):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        allowed = {"schema", "policy_revision", "steps", "effects"}
        if set(payload) - allowed:
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        if payload.get("schema") != RECIPE_SCHEMA or payload.get("policy_revision") != FORGE_POLICY_REVISION:
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, (list, tuple)):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        effects = payload.get("effects", ())
        if not isinstance(effects, (list, tuple)) or any(not isinstance(effect, str) for effect in effects):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        return cls(
            steps=tuple(_step_from_json(step) for step in raw_steps),
            declared_effects=tuple(effects),
        )

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "effects": list(self.declared_effects),
                "policy_revision": FORGE_POLICY_REVISION,
                "schema": RECIPE_SCHEMA,
                "steps": [_canonical_step(step) for step in self.steps],
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    @property
    def digest(self) -> str:
        return hashlib.sha256(b"yonerai.capability_forge.recipe.v1\0" + self.canonical_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class ForgePrimitiveManifest:
    primitive_id: str
    revision: str
    code_owned: bool
    pure: bool
    effects: tuple[str, ...]
    input_validator: ForgeValidator
    output_validator: ForgeValidator
    executor: ForgeExecutor
    requires_containment: bool = False
    accepts_generated_code: bool = False

    def __post_init__(self) -> None:
        _validate_primitive_id(self.primitive_id)
        _validate_revision(self.revision)
        if (
            self.code_owned is not True
            or self.pure is not True
            or self.effects != ()
            or self.requires_containment is not False
            or self.accepts_generated_code is not False
            or not callable(self.input_validator)
            or not callable(self.output_validator)
            or not callable(self.executor)
            or not _is_async_callable(self.input_validator)
            or not _is_async_callable(self.output_validator)
            or not _is_async_callable(self.executor)
        ):
            raise ForgeValidationError(ForgeFailureCode.MANIFEST_REJECTED)


@dataclass(frozen=True, slots=True)
class RecipeReceipt:
    recipe_digest: str
    status: RecipeRunStatus
    failure_code: ForgeFailureCode | None
    completed_steps: int
    total_steps: int
    reuse_class: RecipeReuseClass = field(init=False, default=RecipeReuseClass.TEMPLATE_REUSABLE)
    publication_state: RecipePublicationState = field(init=False, default=RecipePublicationState.UNOFFICIAL)
    readiness_state: RecipeReadinessState = field(init=False, default=RecipeReadinessState.NOT_CLAIMED)

    def __post_init__(self) -> None:
        if not isinstance(self.recipe_digest, str) or not _SHA256.fullmatch(self.recipe_digest):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        if (
            isinstance(self.completed_steps, bool)
            or isinstance(self.total_steps, bool)
            or not isinstance(self.completed_steps, int)
            or not isinstance(self.total_steps, int)
            or not 0 <= self.completed_steps <= self.total_steps <= MAX_RECIPE_STEPS
        ):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        if (self.status is RecipeRunStatus.SUCCEEDED) != (self.failure_code is None):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)

    def audit_record(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "completed_steps": self.completed_steps,
                "failure_code": self.failure_code.value if self.failure_code is not None else None,
                "publication_state": self.publication_state.value,
                "readiness_state": self.readiness_state.value,
                "recipe_digest": self.recipe_digest,
                "reuse_class": self.reuse_class.value,
                "status": self.status.value,
                "total_steps": self.total_steps,
            }
        )


@dataclass(frozen=True, slots=True)
class RecipeRunResult:
    receipt: RecipeReceipt
    outputs: Mapping[str, JsonObject] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, RecipeReceipt) or not isinstance(self.outputs, Mapping):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        normalized = freeze_run_outputs(self.outputs)
        if self.receipt.status is RecipeRunStatus.SUCCEEDED:
            if len(normalized) != self.receipt.completed_steps:
                raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        elif normalized:
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        object.__setattr__(self, "outputs", normalized)


def freeze_run_outputs(value: Mapping[str, JsonObject]) -> Mapping[str, JsonObject]:
    if not isinstance(value, Mapping) or len(value) > MAX_RECIPE_STEPS:
        raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)
    normalized: dict[str, JsonObject] = {}
    for step_id, output in value.items():
        _validate_identifier(step_id)
        normalized[step_id] = freeze_json_object(output)
    frozen = MappingProxyType(dict(sorted(normalized.items())))
    serialized = json.dumps(
        {step_id: thaw_json_value(output) for step_id, output in frozen.items()},
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(serialized) > MAX_RUN_OUTPUT_BYTES:
        raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)
    return frozen


def freeze_json_object(value: Mapping[str, object]) -> JsonObject:
    if not isinstance(value, Mapping) or len(value) > MAX_COLLECTION_ITEMS:
        raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)
    normalized: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        _validate_text(key)
        normalized[key] = freeze_json_value(item)
    return MappingProxyType(dict(sorted(normalized.items())))


def freeze_json_value(value: object, *, depth: int = 1) -> JsonValue:
    if depth > MAX_RECIPE_DEPTH:
        raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        return value
    if isinstance(value, str):
        _validate_text(value)
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
            _validate_text(key)
            normalized[key] = freeze_json_value(item, depth=depth + 1)
        return MappingProxyType(dict(sorted(normalized.items())))
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)
        return tuple(freeze_json_value(item, depth=depth + 1) for item in value)
    raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)


def thaw_json_value(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value


def iter_recipe_text(candidate: RecipeCandidate):
    for effect in candidate.declared_effects:
        yield effect
    for step in candidate.steps:
        for value in step.inputs.values():
            if not isinstance(value, StepOutputRef):
                yield from _iter_json_text(value)


def _step_from_json(raw: object) -> RecipeStep:
    if not isinstance(raw, Mapping) or set(raw) != {"id", "primitive_id", "primitive_revision", "inputs"}:
        raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
    inputs = raw["inputs"]
    if not isinstance(inputs, Mapping):
        raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
    return RecipeStep(
        step_id=raw["id"],
        primitive_id=raw["primitive_id"],
        primitive_revision=raw["primitive_revision"],
        inputs={key: _input_from_json(value) for key, value in inputs.items()},
    )


def _input_from_json(value: object) -> RecipeInputValue:
    if isinstance(value, Mapping) and ("$step" in value or "output" in value):
        if set(value) != {"$step", "output"}:
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        return StepOutputRef(step_id=value["$step"], output_key=value["output"])
    return freeze_json_value(value)


def _canonical_step(step: RecipeStep) -> dict[str, object]:
    inputs: dict[str, object] = {}
    for key, value in step.inputs.items():
        inputs[key] = (
            {"$step": value.step_id, "output": value.output_key}
            if isinstance(value, StepOutputRef)
            else thaw_json_value(value)
        )
    return {
        "id": step.step_id,
        "inputs": inputs,
        "primitive_id": step.primitive_id,
        "primitive_revision": step.primitive_revision,
    }


def _iter_json_text(value: JsonValue):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _iter_json_text(item)
    elif isinstance(value, tuple):
        for item in value:
            yield from _iter_json_text(item)


def _validate_identifier(value: object) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)


def _validate_primitive_id(value: object) -> None:
    if not isinstance(value, str) or not _PRIMITIVE_ID.fullmatch(value):
        raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)


def _validate_revision(value: object) -> None:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)


def _validate_text(value: str) -> None:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE) from exc
    if len(value) > MAX_TEXT_CHARS or len(encoded) > MAX_TEXT_BYTES:
        raise ForgeValidationError(ForgeFailureCode.LIMIT_EXCEEDED)


def _is_async_callable(value: object) -> bool:
    call = getattr(value, "__call__", None)
    return inspect.iscoroutinefunction(value) or (call is not None and inspect.iscoroutinefunction(call))


__all__ = [
    "FORGE_POLICY_REVISION",
    "ForgeExecutor",
    "ForgeFailureCode",
    "ForgePrimitiveManifest",
    "ForgeValidationError",
    "ForgeValidator",
    "JsonObject",
    "JsonValue",
    "MAX_RECIPE_BYTES",
    "MAX_RECIPE_DEPTH",
    "MAX_RECIPE_INPUTS",
    "MAX_RECIPE_STEPS",
    "MAX_RUN_OUTPUT_BYTES",
    "MAX_TEXT_BYTES",
    "MAX_TEXT_CHARS",
    "RECIPE_SCHEMA",
    "RecipeCandidate",
    "RecipePublicationState",
    "RecipeReadinessState",
    "RecipeReceipt",
    "RecipeReuseClass",
    "RecipeRunResult",
    "RecipeRunStatus",
    "RecipeStep",
    "StepOutputRef",
    "freeze_json_object",
    "freeze_json_value",
    "freeze_run_outputs",
    "iter_recipe_text",
    "thaw_json_value",
]
