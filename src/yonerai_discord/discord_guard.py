from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

import discord
from discord import app_commands

from .capabilities import (
    COMMAND_CAPABILITIES,
    COMMAND_RBAC_FLOORS,
    DELEGATABLE_ACTOR_CAPABILITY_IDS,
    SURFACE_RATE_LIMITS,
)
from .config import Settings
from .control_plane import ActorContext, DecisionCode, PolicyDecision, PolicyEngine, RbacLevel, Registry, RiskLevel
from .db import Database
from .discord_policy import actor_context_for_interaction, actor_context_for_member, command_path
from .modules.operations import GateDecision, InputEnvelope, InputGate, InputPolicy


logger = logging.getLogger(__name__)

_EVENT_DENIAL_AUDIT_WINDOW_SECONDS = 60.0
_EVENT_DENIAL_AUDIT_GLOBAL_LIMIT = 120
_EVENT_DENIAL_AUDIT_GUILD_LIMIT = 30
_EVENT_DENIAL_AUDIT_MAX_KEYS = 4_096


class _EventDenialAuditSampler:
    """ambient spamを1件ずつappend-only DBへ増幅しないbounded sampler。"""

    def __init__(self) -> None:
        self._bucket = -1
        self._global_count = 0
        self._guild_counts: dict[int, int] = {}
        self._seen: OrderedDict[tuple[int, int, str, str, str], None] = OrderedDict()

    def allow(self, *, guild_id: int, user_id: int, capability_id: str, surface: str, code: str) -> bool:
        bucket = int(time.monotonic() // _EVENT_DENIAL_AUDIT_WINDOW_SECONDS)
        if bucket != self._bucket:
            self._bucket = bucket
            self._global_count = 0
            self._guild_counts.clear()
            self._seen.clear()
        key = (guild_id, user_id, capability_id, surface, code)
        if key in self._seen:
            return False
        guild_count = self._guild_counts.get(guild_id, 0)
        if self._global_count >= _EVENT_DENIAL_AUDIT_GLOBAL_LIMIT or guild_count >= _EVENT_DENIAL_AUDIT_GUILD_LIMIT:
            return False
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > _EVENT_DENIAL_AUDIT_MAX_KEYS:
            self._seen.popitem(last=False)
        self._global_count += 1
        self._guild_counts[guild_id] = guild_count + 1
        return True


class CapabilityCommandTree(app_commands.CommandTree[Any]):
    """全slash commandをCapability Registryへ通すCommandTree。"""

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if bool(getattr(self.client, "is_closing", False)):
            await _deny(interaction, "Botは停止処理中です。新しい操作は受け付けていません。")
            return False
        guard = getattr(self.client, "capability_guard", None)
        if not isinstance(guard, CapabilityGuard):
            await _deny(interaction, "Botの認可基盤が準備できていません。")
            return False
        return await guard.check(interaction)


class CapabilityGuard:
    def __init__(self, bot: Any, settings: Settings, registry: Registry, database: Database) -> None:
        self.bot = bot
        self.settings = settings
        self.registry = registry
        self.database = database
        self.policy = PolicyEngine(registry)
        self.input_gate = InputGate(InputPolicy())
        self._event_denial_audits = _EventDenialAuditSampler()

    async def actor(self, interaction: discord.Interaction) -> Any:
        return await actor_context_for_interaction(interaction, self.bot, self.settings)

    async def evaluate_fresh_member(
        self,
        capability_id: str,
        *,
        guild: discord.Guild,
        member: discord.Member,
    ) -> PolicyDecision:
        """REST再取得済みのactorで現在の中央policyを再評価する。"""

        actor = await actor_context_for_member(
            member=member,
            guild=guild,
            guild_id=guild.id,
            bot=self.bot,
            settings=self.settings,
        )
        return self._evaluate_actor(capability_id, actor)[0]

    async def check(self, interaction: discord.Interaction) -> bool:
        if self._closing:
            await _deny(interaction, "Botは停止処理中です。新しい操作は受け付けていません。")
            return False
        path = command_path(interaction.data)
        capability_id = COMMAND_CAPABILITIES.get(path or "")
        if capability_id is None:
            actor = await self.actor(interaction)
            self._audit_denial(
                interaction,
                actor_id=int(actor.actor_id),
                command=path or "unknown",
                capability_id=None,
                code="unmapped_command",
            )
            await _deny(interaction, "このコマンドはCapability Registryへ未登録のため停止中です。")
            return False

        return await self.check_capability(
            interaction,
            capability_id,
            surface=path or "unknown",
            floor=COMMAND_RBAC_FLOORS.get(path or "", RbacLevel.EVERYONE),
        )

    async def check_capability(
        self,
        interaction: discord.Interaction,
        capability_id: str,
        *,
        surface: str,
        floor: RbacLevel = RbacLevel.EVERYONE,
    ) -> bool:
        """slash以外のbutton/modal/listener adapterも同じpolicyへ通す。"""

        if self._closing:
            await _deny(interaction, "Botは停止処理中です。新しい操作は受け付けていません。")
            return False

        actor = await self.actor(interaction)
        decision, delegated = self._evaluate_actor(capability_id, actor)
        if decision.allowed and actor.level < floor and not delegated:
            decision = PolicyDecision(
                allowed=False,
                code=DecisionCode.INSUFFICIENT_LEVEL,
                reason=f"このDiscord入口には{floor.name.lower()}以上が必要です",
                capability_id=capability_id,
                required_level=floor,
                actor_level=actor.level,
            )
        if not decision.allowed:
            self._audit_denial(
                interaction,
                actor_id=int(actor.actor_id),
                command=surface,
                capability_id=capability_id,
                code=decision.code.value,
            )
            await _deny(interaction, f"権限または機能設定により利用できません（{decision.code.value}）。")
            return False
        gate_decision = self._input_decision(interaction, capability_id, surface)
        if gate_decision is not GateDecision.ALLOW:
            self._audit_denial(
                interaction,
                actor_id=int(actor.actor_id),
                command=surface,
                capability_id=capability_id,
                code=f"input_gate.{gate_decision.value}",
            )
            await _deny(interaction, "重複実行または利用頻度の上限により停止しました。")
            return False
        return True

    def event_allowed(
        self,
        capability_id: str,
        *,
        surface: str,
        guild_id: int | None,
        channel_id: int,
        event_id: int,
        user_id: int,
        author_is_bot: bool = False,
        actor_level: RbacLevel | str | int | None = None,
    ) -> bool:
        """eventをRegistry・任意RBAC・共通input gateへ通す。"""

        if self._closing:
            return False

        try:
            if actor_level is None:
                allowed = self.registry.capability_status(capability_id, guild_id).executable
                denial_code = "capability_unavailable"
            else:
                policy, _ = self._evaluate_actor(
                    capability_id,
                    ActorContext(user_id, guild_id, RbacLevel.parse(actor_level)),
                )
                allowed = policy.allowed
                denial_code = policy.code.value
            if not allowed:
                self._audit_event_denial(
                    guild_id=guild_id,
                    user_id=user_id,
                    capability_id=capability_id,
                    surface=surface,
                    code=denial_code,
                )
                return False
            envelope = InputEnvelope(
                event_id=f"event:{surface}:{event_id}",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                author_is_bot=author_is_bot,
                received_at=datetime.now(UTC),
                bucket=capability_id,
            )
            decision = self.input_gate.evaluate(
                envelope,
                rate_limit=SURFACE_RATE_LIMITS.get(surface, 30),
            )
        except (KeyError, TypeError, ValueError):
            return False
        if decision is GateDecision.ALLOW:
            return True
        self._audit_event_denial(
            guild_id=guild_id,
            user_id=user_id,
            capability_id=capability_id,
            surface=surface,
            code=f"input_gate.{decision.value}",
        )
        return False

    def currently_allowed(
        self,
        capability_id: str,
        *,
        guild_id: int | None,
        user_id: int,
        actor_level: RbacLevel | str | int | None = None,
        floor: RbacLevel = RbacLevel.EVERYONE,
    ) -> bool:
        """rate/dedupe tokenを再消費せず、現在のshutdown・module・capability・RBACだけを再評価する。"""

        if self._closing:
            return False
        try:
            if actor_level is None:
                return (
                    floor is RbacLevel.EVERYONE
                    and self.registry.capability_status(
                        capability_id,
                        guild_id,
                    ).executable
                )
            level = RbacLevel.parse(actor_level)
            policy, delegated = self._evaluate_actor(
                capability_id,
                ActorContext(user_id, guild_id, level),
            )
            return (level >= floor or delegated) and policy.allowed
        except (KeyError, TypeError, ValueError):
            return False

    def _evaluate_actor(self, capability_id: str, actor: ActorContext) -> tuple[PolicyDecision, bool]:
        decision = self.policy.evaluate(capability_id, actor)
        if decision.allowed or decision.code is not DecisionCode.INSUFFICIENT_LEVEL:
            return decision, False
        if not self._delegated_actor_grant(capability_id, actor):
            return decision, False
        return (
            PolicyDecision(
                allowed=True,
                code=DecisionCode.ALLOWED,
                reason="owner_delegated capability grant",
                capability_id=capability_id,
                required_level=decision.required_level,
                actor_level=actor.level,
            ),
            True,
        )

    def _delegated_actor_grant(self, capability_id: str, actor: ActorContext) -> bool:
        if actor.guild_id is None:
            return False
        try:
            spec = self.registry.capability(capability_id)
            if (
                capability_id not in DELEGATABLE_ACTOR_CAPABILITY_IDS
                or spec.owner_only
                or spec.risk >= RiskLevel.CRITICAL
                or spec.safety_floor >= RbacLevel.BOT_OWNER
            ):
                return False
            record = self.database.get_capability_actor_grant(
                capability_id,
                int(actor.actor_id),
                actor.guild_id,
            )
            if record is None or record.grant_kind != "owner_delegated":
                return False
            return record.granted_by in self.settings.bot_owner_ids
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    @property
    def _closing(self) -> bool:
        return bool(getattr(self.bot, "is_closing", False))

    def _audit_event_denial(
        self,
        *,
        guild_id: int,
        user_id: int,
        capability_id: str,
        surface: str,
        code: str,
    ) -> None:
        if not self._event_denial_audits.allow(
            guild_id=guild_id,
            user_id=user_id,
            capability_id=capability_id,
            surface=surface,
            code=code,
        ):
            return
        try:
            self.database.append_audit(
                "capability.event_denied",
                actor_id=user_id,
                guild_id=guild_id,
                details={
                    "capability_id": capability_id,
                    "decision_code": code,
                    "surface": surface,
                },
            )
        except Exception as exc:
            logger.error("capability_event_audit_failed", extra={"error_type": type(exc).__name__})

    def _input_decision(
        self,
        interaction: discord.Interaction,
        capability_id: str,
        surface: str,
    ) -> GateDecision:
        guild_id = getattr(interaction, "guild_id", None)
        channel_id = getattr(interaction, "channel_id", None)
        interaction_id = getattr(interaction, "id", None)
        user = getattr(interaction, "user", None)
        user_id = getattr(user, "id", None)
        if not all(isinstance(value, int) and value > 0 for value in (guild_id, channel_id, interaction_id, user_id)):
            # DMや古いtest doubleはRegistry/RBACで別途判定する。偽の0値をrate bucketへ入れない。
            return GateDecision.ALLOW
        envelope = InputEnvelope(
            event_id=f"interaction:{interaction_id}",
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            author_is_bot=bool(getattr(user, "bot", False)),
            received_at=datetime.now(UTC),
            bucket=capability_id,
        )
        return self.input_gate.evaluate(
            envelope,
            rate_limit=SURFACE_RATE_LIMITS.get(surface, 30),
        )

    def _audit_denial(
        self,
        interaction: discord.Interaction,
        *,
        actor_id: int,
        command: str,
        capability_id: str | None,
        code: str,
    ) -> None:
        details: dict[str, str] = {"command": command, "decision_code": code}
        if capability_id is not None:
            details["capability_id"] = capability_id
        try:
            self.database.append_audit(
                "capability.denied",
                actor_id=actor_id,
                guild_id=interaction.guild_id,
                details=details,
            )
        except Exception as exc:
            # 監査障害でdenyをpermitへ変えず、入力本文や例外内容もlogへ出さない。
            logger.error("capability_denial_audit_failed", extra={"error_type": type(exc).__name__})


async def _deny(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
