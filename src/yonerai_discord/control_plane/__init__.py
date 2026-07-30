"""Discord SDK非依存のcapability registry・feature flag・RBACコア。"""

from .catalog import CatalogLoadError, load_capability_catalog, load_capability_counts
from .models import (
    AvailabilityDecision,
    CapabilitySpec,
    DecisionCode,
    ModuleSpec,
    PolicyDecision,
    RBACLevel,
    RbacLevel,
    RiskLevel,
)
from .rbac import ActorContext, ConfigScope, ConfigTarget, PolicyEngine, RbacPolicy
from .registry import CapabilityRegistry, Registry, RegistryIssue, RegistryValidationError
from .state import FeatureStateStore, GuildId, InMemoryStateStore, MemoryStateStore, StateStore

__all__ = [
    "ActorContext",
    "AvailabilityDecision",
    "CapabilityRegistry",
    "CapabilitySpec",
    "CatalogLoadError",
    "ConfigScope",
    "ConfigTarget",
    "DecisionCode",
    "FeatureStateStore",
    "GuildId",
    "InMemoryStateStore",
    "MemoryStateStore",
    "ModuleSpec",
    "PolicyDecision",
    "PolicyEngine",
    "RBACLevel",
    "RbacLevel",
    "RbacPolicy",
    "Registry",
    "RegistryIssue",
    "RegistryValidationError",
    "RiskLevel",
    "StateStore",
    "load_capability_catalog",
    "load_capability_counts",
]
