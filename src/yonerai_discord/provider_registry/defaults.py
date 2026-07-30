from __future__ import annotations

import json
from importlib.resources import files

from .manifest import ProviderCatalogManifest


DEFAULT_MANIFEST_RESOURCE = "manifests/provider-catalog.v1.json"


def load_default_catalog() -> ProviderCatalogManifest:
    resource = files("yonerai_discord.provider_registry").joinpath("manifests").joinpath("provider-catalog.v1.json")
    with resource.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return ProviderCatalogManifest.from_mapping(raw)


DEFAULT_CATALOG = load_default_catalog()


LEGACY_MODEL_ALIASES: dict[str, str] = {
    item.source: item.target for item in DEFAULT_CATALOG.compatibility_aliases if item.source.startswith("gpt-")
}
