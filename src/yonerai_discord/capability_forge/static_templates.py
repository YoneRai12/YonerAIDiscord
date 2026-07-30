"""Production hostで許可する静的code-owned pure template catalog。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .domain import ForgeFailureCode, ForgePrimitiveManifest, ForgeValidationError
from .recipe import (
    DEFAULT_STEP_TIMEOUT_SECONDS,
    DEFAULT_TOTAL_TIMEOUT_SECONDS,
    ForgePrimitiveRegistry,
    RecipeRunner,
)


@dataclass(frozen=True, slots=True)
class CodeOwnedTemplateIdentity:
    primitive_id: str
    revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.primitive_id, str) or not isinstance(self.revision, str):
            raise ForgeValidationError(ForgeFailureCode.MANIFEST_REJECTED)


async def _validate_text_identity(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"text"} or not isinstance(value["text"], str):
        raise ValueError("invalid static template input")
    return {"text": value["text"]}


async def _execute_text_identity(value: Mapping[str, object]) -> dict[str, object]:
    return {"text": value["text"]}


_TEXT_IDENTITY_V1 = ForgePrimitiveManifest(
    primitive_id="text.identity",
    revision="1",
    code_owned=True,
    pure=True,
    effects=(),
    input_validator=_validate_text_identity,
    output_validator=_validate_text_identity,
    executor=_execute_text_identity,
)
_STATIC_ALLOWLIST = MappingProxyType(
    {
        ("text.identity", "1"): _TEXT_IDENTITY_V1,
    }
)


class ProductionRecipeRunner(RecipeRunner):
    """静的allowlist manifest identityとfactory sealを検証するhost runtime境界。"""

    def __init__(
        self,
        registry: ForgePrimitiveRegistry,
        *,
        step_timeout_seconds: float = DEFAULT_STEP_TIMEOUT_SECONDS,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        if (
            type(registry) is not ForgePrimitiveRegistry
            or not registry.is_production_sealed
            or any(
                _STATIC_ALLOWLIST.get((manifest.primitive_id, manifest.revision)) is not manifest
                for manifest in registry.manifests
            )
        ):
            raise ForgeValidationError(ForgeFailureCode.MANIFEST_REJECTED)
        super().__init__(
            registry,
            step_timeout_seconds=step_timeout_seconds,
            total_timeout_seconds=total_timeout_seconds,
        )


def available_code_owned_templates() -> tuple[CodeOwnedTemplateIdentity, ...]:
    return tuple(CodeOwnedTemplateIdentity(*identity) for identity in sorted(_STATIC_ALLOWLIST))


def build_production_registry(
    identities: tuple[CodeOwnedTemplateIdentity, ...] = (),
) -> ForgePrimitiveRegistry:
    if not isinstance(identities, tuple) or any(
        type(identity) is not CodeOwnedTemplateIdentity for identity in identities
    ):
        raise ForgeValidationError(ForgeFailureCode.MANIFEST_REJECTED)
    manifests: list[ForgePrimitiveManifest] = []
    seen: set[tuple[str, str]] = set()
    for identity in identities:
        key = (identity.primitive_id, identity.revision)
        manifest = _STATIC_ALLOWLIST.get(key)
        if manifest is None or key in seen:
            raise ForgeValidationError(ForgeFailureCode.MANIFEST_REJECTED)
        seen.add(key)
        manifests.append(manifest)
    return ForgePrimitiveRegistry._from_code_owned_static_allowlist(tuple(manifests))


__all__ = [
    "CodeOwnedTemplateIdentity",
    "ProductionRecipeRunner",
    "available_code_owned_templates",
    "build_production_registry",
]
