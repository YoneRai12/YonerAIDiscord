from __future__ import annotations

import pytest

from yonerai_discord.control_plane import (
    ActorContext,
    CapabilitySpec,
    ConfigScope,
    ConfigTarget,
    DecisionCode,
    ModuleSpec,
    PolicyEngine,
    RbacLevel,
    Registry,
    RiskLevel,
)


def policy_registry() -> tuple[Registry, PolicyEngine]:
    registry = Registry()
    registry.register_module(ModuleSpec("utility"))
    registry.register_capability(CapabilitySpec("utility.ping", "utility", required_level=RbacLevel.EVERYONE))
    registry.register_capability(CapabilitySpec("utility.default-deny", "utility"))
    registry.register_capability(
        CapabilitySpec(
            "utility.high-risk",
            "utility",
            required_level=RbacLevel.MODERATOR,
            risk=RiskLevel.HIGH,
        )
    )
    registry.register_capability(
        CapabilitySpec(
            "utility.owner-only",
            "utility",
            required_level=RbacLevel.EVERYONE,
            owner_only=True,
        )
    )
    return registry, PolicyEngine(registry)


def test_rbac_levels_are_monotonic_and_default_is_deny() -> None:
    registry, policy = policy_registry()
    everyone = ActorContext("member", guild_id=1)
    assert policy.evaluate("utility.ping", everyone).allowed

    denied = policy.evaluate("utility.default-deny", everyone)
    assert not denied.allowed
    assert denied.code == DecisionCode.INSUFFICIENT_LEVEL
    assert denied.required_level == RbacLevel.BOT_OWNER
    assert policy.evaluate("utility.default-deny", ActorContext("owner", guild_id=1, level=RbacLevel.BOT_OWNER)).allowed


@pytest.mark.parametrize(
    ("level", "allowed"),
    [
        (RbacLevel.EVERYONE, False),
        (RbacLevel.TRUSTED, False),
        (RbacLevel.MODERATOR, True),
        (RbacLevel.GUILD_ADMIN, True),
        (RbacLevel.GUILD_OWNER, True),
        (RbacLevel.BOT_OWNER, True),
    ],
)
def test_all_six_levels_are_evaluated(level: RbacLevel, allowed: bool) -> None:
    _, policy = policy_registry()
    decision = policy.evaluate("utility.high-risk", ActorContext("actor", guild_id=1, level=level))
    assert decision.allowed is allowed


def test_high_risk_and_owner_only_cannot_be_relaxed_by_guild_override() -> None:
    registry, policy = policy_registry()
    registry.state_store.set_level_override("utility.high-risk", RbacLevel.EVERYONE, guild_id=1)
    registry.state_store.set_level_override("utility.owner-only", RbacLevel.EVERYONE, guild_id=1)

    assert registry.required_level("utility.high-risk", 1) == RbacLevel.MODERATOR
    assert registry.required_level("utility.owner-only", 1) == RbacLevel.BOT_OWNER
    assert not policy.evaluate("utility.high-risk", ActorContext("member", 1)).allowed
    assert not policy.evaluate("utility.owner-only", ActorContext("guild-owner", 1, RbacLevel.GUILD_OWNER)).allowed


def test_config_changes_have_separate_global_and_guild_authorization() -> None:
    _, policy = policy_registry()
    guild_admin = ActorContext("admin", guild_id=10, level=RbacLevel.GUILD_ADMIN)
    bot_owner = ActorContext("bot-owner", level=RbacLevel.BOT_OWNER)

    global_denied = policy.evaluate_config_change(
        guild_admin,
        scope=ConfigScope.GLOBAL,
        target=ConfigTarget.MODULE,
        target_id="utility",
    )
    assert global_denied.code == DecisionCode.GLOBAL_CONFIG_REQUIRES_BOT_OWNER
    assert policy.evaluate_config_change(
        bot_owner,
        scope="global",
        target="module",
        target_id="utility",
    ).allowed

    assert policy.evaluate_config_change(
        guild_admin,
        scope="guild",
        guild_id=10,
        target="capability",
        target_id="utility.ping",
    ).allowed
    mismatch = policy.evaluate_config_change(
        guild_admin,
        scope="guild",
        guild_id=20,
        target="capability",
        target_id="utility.ping",
    )
    assert mismatch.code == DecisionCode.GUILD_MISMATCH


def test_config_policy_rejects_guild_attempt_to_lower_safety_floor() -> None:
    _, policy = policy_registry()
    guild_owner = ActorContext("owner", guild_id=1, level=RbacLevel.GUILD_OWNER)
    denied = policy.evaluate_config_change(
        guild_owner,
        scope="guild",
        guild_id=1,
        target="capability_level",
        target_id="utility.high-risk",
        requested_level=RbacLevel.TRUSTED,
    )
    assert denied.code == DecisionCode.SAFETY_FLOOR
    assert denied.required_level == RbacLevel.MODERATOR


def test_disabled_and_unknown_capabilities_are_denied_before_rbac() -> None:
    registry, policy = policy_registry()
    registry.state_store.set_module_override("utility", False, guild_id=1)
    actor = ActorContext("bot-owner", 1, RbacLevel.BOT_OWNER)
    assert policy.evaluate("utility.ping", actor).code == DecisionCode.MODULE_DISABLED
    assert policy.evaluate("missing", actor).code == DecisionCode.UNKNOWN_CAPABILITY
