from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


def normalize_id(value: str, *, label: str) -> str:
    """Registry用IDを小文字の安定した形に正規化する。"""

    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    if any(not (character.isalnum() or character in "._-") for character in normalized):
        raise ValueError(f"{label} contains an invalid character")
    return normalized


class RbacLevel(IntEnum):
    """Discord SDKに依存しない、単調増加の権限レベル。"""

    EVERYONE = 0
    TRUSTED = 10
    MODERATOR = 20
    GUILD_ADMIN = 30
    GUILD_OWNER = 40
    BOT_OWNER = 50

    @classmethod
    def parse(cls, value: RbacLevel | str | int) -> RbacLevel:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError as exc:
                raise ValueError(f"unknown RBAC level: {value}") from exc
        try:
            return cls(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown RBAC level: {value}") from exc


class RiskLevel(IntEnum):
    LOW = 0
    MEDIUM = 10
    HIGH = 20
    CRITICAL = 30

    @classmethod
    def parse(cls, value: RiskLevel | str | int) -> RiskLevel:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError as exc:
                raise ValueError(f"unknown risk level: {value}") from exc
        try:
            return cls(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown risk level: {value}") from exc


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    module_id: str
    default_enabled: bool | None = None
    implemented: bool = True
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        module_id = normalize_id(self.module_id, label="module_id")
        dependencies = tuple(normalize_id(value, label="module dependency") for value in self.dependencies)
        if module_id in dependencies:
            raise ValueError("a module cannot depend on itself")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("module dependencies must be unique")

        # Minecraftは外部serverへの影響があるため、宣言だけでは起動しない。
        default_enabled = self.default_enabled
        if default_enabled is None:
            default_enabled = module_id != "minecraft" and not module_id.endswith(".minecraft")

        object.__setattr__(self, "module_id", module_id)
        object.__setattr__(self, "default_enabled", bool(default_enabled))
        object.__setattr__(self, "dependencies", dependencies)

    @property
    def id(self) -> str:
        return self.module_id


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    capability_id: str
    module_id: str
    name: str = ""
    source_state: str | None = None
    default_enabled: bool = True
    implemented: bool = True
    required_level: RbacLevel = RbacLevel.BOT_OWNER
    minimum_level: RbacLevel = RbacLevel.EVERYONE
    risk: RiskLevel = RiskLevel.LOW
    owner_only: bool = False
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        capability_id = normalize_id(self.capability_id, label="capability_id")
        module_id = normalize_id(self.module_id, label="module_id")
        dependencies = tuple(normalize_id(value, label="capability dependency") for value in self.dependencies)
        if capability_id in dependencies:
            raise ValueError("a capability cannot depend on itself")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("capability dependencies must be unique")

        required_level = RbacLevel.parse(self.required_level)
        minimum_level = RbacLevel.parse(self.minimum_level)
        risk = RiskLevel.parse(self.risk)
        name = self.name.strip()
        source_state = self.source_state.strip().lower() if self.source_state is not None else None

        if self.owner_only:
            required_level = RbacLevel.BOT_OWNER
            minimum_level = RbacLevel.BOT_OWNER
        elif risk >= RiskLevel.HIGH:
            # 高risk機能の宣言時権限は、guild設定から緩和できない。
            minimum_level = max(minimum_level, required_level)

        if minimum_level > required_level:
            required_level = minimum_level

        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "module_id", module_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "source_state", source_state)
        object.__setattr__(self, "required_level", required_level)
        object.__setattr__(self, "minimum_level", minimum_level)
        object.__setattr__(self, "risk", risk)
        object.__setattr__(self, "dependencies", dependencies)

    @property
    def id(self) -> str:
        return self.capability_id

    @property
    def safety_floor(self) -> RbacLevel:
        return self.minimum_level

    @property
    def handler_connected(self) -> bool:
        """runtimeで実行handlerが接続済みか。source_stateとは無関係。"""

        return self.implemented


class DecisionCode(StrEnum):
    ALLOWED = "allowed"
    UNKNOWN_MODULE = "unknown_module"
    UNKNOWN_CAPABILITY = "unknown_capability"
    MODULE_UNIMPLEMENTED = "module_unimplemented"
    CAPABILITY_UNIMPLEMENTED = "capability_unimplemented"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    MODULE_DISABLED = "module_disabled"
    CAPABILITY_DISABLED = "capability_disabled"
    MISSING_DEPENDENCY = "missing_dependency"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    DEPENDENCY_CYCLE = "dependency_cycle"
    INSUFFICIENT_LEVEL = "insufficient_level"
    GUILD_CONTEXT_REQUIRED = "guild_context_required"
    GUILD_MISMATCH = "guild_mismatch"
    GLOBAL_CONFIG_REQUIRES_BOT_OWNER = "global_config_requires_bot_owner"
    GUILD_CONFIG_REQUIRES_ADMIN = "guild_config_requires_admin"
    SAFETY_FLOOR = "safety_floor"


_REASON_MESSAGES: dict[DecisionCode, str] = {
    DecisionCode.ALLOWED: "許可されました",
    DecisionCode.UNKNOWN_MODULE: "未登録のmoduleです",
    DecisionCode.UNKNOWN_CAPABILITY: "未登録のcapabilityです",
    DecisionCode.MODULE_UNIMPLEMENTED: "moduleが未実装です",
    DecisionCode.CAPABILITY_UNIMPLEMENTED: "capabilityが未実装です",
    DecisionCode.RUNTIME_UNAVAILABLE: "runtimeのhandlerが利用できません",
    DecisionCode.MODULE_DISABLED: "moduleが無効です",
    DecisionCode.CAPABILITY_DISABLED: "capabilityが無効です",
    DecisionCode.MISSING_DEPENDENCY: "登録されていない依存関係があります",
    DecisionCode.DEPENDENCY_UNAVAILABLE: "依存先が実行不能です",
    DecisionCode.DEPENDENCY_CYCLE: "依存関係が循環しています",
    DecisionCode.INSUFFICIENT_LEVEL: "実行に必要な権限レベルを満たしていません",
    DecisionCode.GUILD_CONTEXT_REQUIRED: "guildの指定が必要です",
    DecisionCode.GUILD_MISMATCH: "actorと設定対象のguildが一致しません",
    DecisionCode.GLOBAL_CONFIG_REQUIRES_BOT_OWNER: "global設定のbot_owner権限が必要です",
    DecisionCode.GUILD_CONFIG_REQUIRES_ADMIN: "guild設定のguild_admin以上が必要です",
    DecisionCode.SAFETY_FLOOR: "guild設定で安全下限より権限を緩和できません",
}


@dataclass(frozen=True, slots=True)
class AvailabilityDecision:
    executable: bool
    code: DecisionCode
    subject_id: str
    detail: str | None = None

    @property
    def allowed(self) -> bool:
        return self.executable

    @property
    def reason(self) -> str:
        base = _REASON_MESSAGES[self.code]
        return f"{base}: {self.detail}" if self.detail else base


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    code: DecisionCode
    reason: str
    capability_id: str | None = None
    required_level: RbacLevel | None = None
    actor_level: RbacLevel | None = None


def reason_for(code: DecisionCode, detail: str | None = None) -> str:
    base = _REASON_MESSAGES[code]
    return f"{base}: {detail}" if detail else base


# Acronym表記を好むadapter向けの互換名。
RBACLevel = RbacLevel
