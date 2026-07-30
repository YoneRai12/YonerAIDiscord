"""code-owned pure templateだけを実行する宣言recipe runner。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .domain import (
    ForgeFailureCode,
    ForgePrimitiveManifest,
    ForgeValidationError,
    JsonObject,
    RecipeCandidate,
    RecipeReceipt,
    RecipeRunResult,
    RecipeRunStatus,
    RecipeStep,
    StepOutputRef,
    freeze_json_object,
    freeze_run_outputs,
    iter_recipe_text,
)


DEFAULT_STEP_TIMEOUT_SECONDS = 2.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 8.0
MAX_TIMEOUT_SECONDS = 60.0

_WINDOWS_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")
_WINDOWS_DRIVE_RELATIVE = re.compile(r"[A-Za-z]:")
_WINDOWS_DEVICE_COMPONENT = re.compile(
    r"(?:con|prn|aux|nul|conin\$|conout\$|com[1-9]|lpt[1-9])(?:[.:].*)?\Z",
    re.IGNORECASE,
)
_HTTP_URL = re.compile(r"https?://[^\s]+\Z", re.IGNORECASE)
_STATIC_REGISTRY_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class ForgePrimitiveRegistry:
    """ActionRegistryとは独立した、起動時固定・default emptyのtemplate registry。"""

    _manifests: Mapping[str, ForgePrimitiveManifest] = field(repr=False)
    _production_seal: object | None = field(init=False, repr=False, default=None)

    def __init__(self, manifests: tuple[ForgePrimitiveManifest, ...] = ()) -> None:
        if not isinstance(manifests, tuple):
            raise ForgeValidationError(ForgeFailureCode.MANIFEST_REJECTED)
        normalized: dict[str, ForgePrimitiveManifest] = {}
        for manifest in manifests:
            if not isinstance(manifest, ForgePrimitiveManifest) or manifest.primitive_id in normalized:
                raise ForgeValidationError(ForgeFailureCode.MANIFEST_REJECTED)
            normalized[manifest.primitive_id] = manifest
        object.__setattr__(self, "_manifests", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(self, "_production_seal", None)

    @classmethod
    def _from_code_owned_static_allowlist(
        cls,
        manifests: tuple[ForgePrimitiveManifest, ...],
    ) -> ForgePrimitiveRegistry:
        registry = cls(manifests)
        object.__setattr__(registry, "_production_seal", _STATIC_REGISTRY_SEAL)
        return registry

    @property
    def manifests(self) -> tuple[ForgePrimitiveManifest, ...]:
        return tuple(self._manifests.values())

    @property
    def is_production_sealed(self) -> bool:
        return self._production_seal is _STATIC_REGISTRY_SEAL

    def resolve(self, primitive_id: str) -> ForgePrimitiveManifest:
        try:
            return self._manifests[primitive_id]
        except (KeyError, TypeError) as exc:
            raise ForgeValidationError(ForgeFailureCode.UNKNOWN_PRIMITIVE) from exc


@dataclass(frozen=True, slots=True)
class _PreparedRecipe:
    candidate: RecipeCandidate
    manifests: tuple[ForgePrimitiveManifest, ...]


class _StepFailure(Exception):
    def __init__(self, code: ForgeFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class RecipeRunner:
    """trusted template recipe runner。generated codeや実OS sandboxではない。"""

    def __init__(
        self,
        registry: ForgePrimitiveRegistry,
        *,
        step_timeout_seconds: float = DEFAULT_STEP_TIMEOUT_SECONDS,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(registry, ForgePrimitiveRegistry):
            raise TypeError("registry must be a ForgePrimitiveRegistry")
        self._registry = registry
        self._step_timeout_seconds = _validate_timeout(step_timeout_seconds)
        self._total_timeout_seconds = _validate_timeout(total_timeout_seconds)

    def preflight(self, candidate: RecipeCandidate) -> str:
        return self._prepare(candidate).candidate.digest

    async def run(self, candidate: RecipeCandidate) -> RecipeRunResult:
        if not isinstance(candidate, RecipeCandidate):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        try:
            prepared = self._prepare(candidate)
        except ForgeValidationError as exc:
            return _failed_result(
                candidate,
                status=RecipeRunStatus.REJECTED,
                code=exc.code,
                completed_steps=0,
            )

        outputs: dict[str, JsonObject] = {}
        completed_steps = 0
        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                for step, manifest in zip(prepared.candidate.steps, prepared.manifests, strict=True):
                    try:
                        outputs = await asyncio.wait_for(
                            _execute_step(step, manifest, outputs),
                            timeout=self._step_timeout_seconds,
                        )
                    except TimeoutError:
                        return _failed_result(
                            candidate,
                            status=RecipeRunStatus.TIMED_OUT,
                            code=ForgeFailureCode.STEP_TIMEOUT,
                            completed_steps=completed_steps,
                        )
                    except _StepFailure as exc:
                        return _failed_result(
                            candidate,
                            status=(
                                RecipeRunStatus.REJECTED
                                if exc.code is ForgeFailureCode.UNSAFE_PATH
                                else RecipeRunStatus.FAILED
                            ),
                            code=exc.code,
                            completed_steps=completed_steps,
                        )
                    completed_steps += 1
        except TimeoutError:
            return _failed_result(
                candidate,
                status=RecipeRunStatus.TIMED_OUT,
                code=ForgeFailureCode.TOTAL_TIMEOUT,
                completed_steps=completed_steps,
            )

        receipt = RecipeReceipt(
            recipe_digest=candidate.digest,
            status=RecipeRunStatus.SUCCEEDED,
            failure_code=None,
            completed_steps=completed_steps,
            total_steps=len(candidate.steps),
        )
        return RecipeRunResult(receipt=receipt, outputs=MappingProxyType(dict(outputs)))

    def _prepare(self, candidate: RecipeCandidate) -> _PreparedRecipe:
        if not isinstance(candidate, RecipeCandidate):
            raise ForgeValidationError(ForgeFailureCode.INVALID_RECIPE)
        if candidate.declared_effects:
            raise ForgeValidationError(ForgeFailureCode.SPOOFED_EFFECT)
        if any(_looks_like_host_path(text) for text in iter_recipe_text(candidate)):
            raise ForgeValidationError(ForgeFailureCode.UNSAFE_PATH)

        positions = {step.step_id: index for index, step in enumerate(candidate.steps)}
        references = {
            step.step_id: tuple(value.step_id for value in step.inputs.values() if isinstance(value, StepOutputRef))
            for step in candidate.steps
        }
        if any(reference not in positions for step_references in references.values() for reference in step_references):
            raise ForgeValidationError(ForgeFailureCode.UNKNOWN_REFERENCE)
        if _has_cycle(references):
            raise ForgeValidationError(ForgeFailureCode.DEPENDENCY_CYCLE)
        for step in candidate.steps:
            if any(positions[reference] >= positions[step.step_id] for reference in references[step.step_id]):
                raise ForgeValidationError(ForgeFailureCode.FORWARD_REFERENCE)

        manifests: list[ForgePrimitiveManifest] = []
        for step in candidate.steps:
            manifest = self._registry.resolve(step.primitive_id)
            if manifest.revision != step.primitive_revision:
                raise ForgeValidationError(ForgeFailureCode.REVISION_MISMATCH)
            manifests.append(manifest)
        return _PreparedRecipe(candidate=candidate, manifests=tuple(manifests))


def _resolve_inputs(step: RecipeStep, outputs: dict[str, JsonObject]) -> JsonObject:
    resolved: dict[str, object] = {}
    for key, value in step.inputs.items():
        if not isinstance(value, StepOutputRef):
            resolved[key] = value
            continue
        source = outputs.get(value.step_id)
        if source is None or value.output_key not in source:
            raise ForgeValidationError(ForgeFailureCode.INPUT_VALIDATION_FAILED)
        resolved[key] = source[value.output_key]
    return freeze_json_object(resolved)


async def _execute_step(
    step: RecipeStep,
    manifest: ForgePrimitiveManifest,
    prior_outputs: dict[str, JsonObject],
) -> dict[str, JsonObject]:
    try:
        resolved_inputs = _resolve_inputs(step, prior_outputs)
        validated_inputs = freeze_json_object(await manifest.input_validator(resolved_inputs))
    except Exception as exc:
        raise _StepFailure(ForgeFailureCode.INPUT_VALIDATION_FAILED) from exc
    if _object_has_unsafe_path(validated_inputs):
        raise _StepFailure(ForgeFailureCode.UNSAFE_PATH)

    try:
        raw_output = await manifest.executor(validated_inputs)
    except Exception as exc:
        raise _StepFailure(ForgeFailureCode.EXECUTOR_FAILED) from exc

    try:
        bounded_output = freeze_json_object(raw_output)
        validated_output = freeze_json_object(await manifest.output_validator(bounded_output))
        if _object_has_unsafe_path(validated_output):
            raise _StepFailure(ForgeFailureCode.UNSAFE_PATH)
        candidate_outputs = {**prior_outputs, step.step_id: validated_output}
        return dict(freeze_run_outputs(candidate_outputs))
    except _StepFailure:
        raise
    except Exception as exc:
        raise _StepFailure(ForgeFailureCode.OUTPUT_VALIDATION_FAILED) from exc


def _failed_result(
    candidate: RecipeCandidate,
    *,
    status: RecipeRunStatus,
    code: ForgeFailureCode,
    completed_steps: int,
) -> RecipeRunResult:
    receipt = RecipeReceipt(
        recipe_digest=candidate.digest,
        status=status,
        failure_code=code,
        completed_steps=completed_steps,
        total_steps=len(candidate.steps),
    )
    return RecipeRunResult(receipt=receipt, outputs=MappingProxyType({}))


def _has_cycle(references: dict[str, tuple[str, ...]]) -> bool:
    visited: set[str] = set()
    active: set[str] = set()

    def visit(step_id: str) -> bool:
        if step_id in active:
            return True
        if step_id in visited:
            return False
        active.add(step_id)
        if any(visit(reference) for reference in references[step_id]):
            return True
        active.remove(step_id)
        visited.add(step_id)
        return False

    return any(visit(step_id) for step_id in references)


def _looks_like_host_path(value: str) -> bool:
    if _HTTP_URL.fullmatch(value):
        return False
    normalized = value.replace("\\", "/")
    lowered = normalized.lower()
    if (
        lowered.startswith("file:")
        or value.startswith(("\\\\", "//"))
        or lowered.startswith(("//?/", "//./", "/??/", "/device/", "/globalroot/", "/dosdevices/", "globalroot/"))
        or _WINDOWS_ABSOLUTE.match(value)
        or _WINDOWS_DRIVE_RELATIVE.match(value)
        or any(
            _WINDOWS_DEVICE_COMPONENT.fullmatch(component.rstrip(" ."))
            for component in normalized.split("/")
            if component
        )
        or normalized.startswith("/")
        or _looks_like_path_glob(value)
    ):
        return True
    return ".." in normalized.split("/")


def _looks_like_path_glob(value: str) -> bool:
    if not any(character in value for character in "*?["):
        return False
    return "/" in value or "\\" in value or ("." in value and not any(character.isspace() for character in value))


def _object_has_unsafe_path(value: JsonObject) -> bool:
    return any(_looks_like_host_path(text) for text in _iter_object_text(value))


def _iter_object_text(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _iter_object_text(item)
    elif isinstance(value, tuple):
        for item in value:
            yield from _iter_object_text(item)


def _validate_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout is outside the fixed Forge limit")
    return float(value)


__all__ = [
    "DEFAULT_STEP_TIMEOUT_SECONDS",
    "DEFAULT_TOTAL_TIMEOUT_SECONDS",
    "ForgePrimitiveRegistry",
    "MAX_TIMEOUT_SECONDS",
    "RecipeRunner",
]
