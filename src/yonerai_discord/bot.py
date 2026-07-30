from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from .capabilities import (
    COMMAND_CAPABILITIES,
    COMMAND_RBAC_FLOORS,
    DELEGATABLE_ACTOR_CAPABILITY_IDS,
    build_capability_registry,
    startup_plugins_for_registry,
)
from .config import Settings
from .control_plane import ConfigScope, ConfigTarget, PolicyEngine, RbacLevel, Registry
from .db import Database
from .deployment_current_truth import M10CurrentTruthV1, build_m10_current_truth
from .discord_guard import CapabilityCommandTree, CapabilityGuard
from .plugin import PluginFactory, PluginManager, discover_plugins
from .plugin_manifest import BUILTIN_PLUGIN_MANIFEST
from .modules.operations import InteractionFailureTerminal
from .surface_inventory import SurfaceInventoryReport, reconcile_runtime_surfaces


logger = logging.getLogger(__name__)


_DISCORD_MESSAGE_LIMIT = 1_900


def _bounded_message(lines: list[str] | tuple[str, ...]) -> str:
    """Discordの2,000文字上限へ余白を残し、診断結果を安全に切り詰める。"""

    text = "\n".join(lines).strip() or "表示する情報はありません。"
    if len(text) <= _DISCORD_MESSAGE_LIMIT:
        return text
    suffix = "\n…（表示上限のため省略）"
    return text[: _DISCORD_MESSAGE_LIMIT - len(suffix)].rstrip() + suffix


async def _send_ephemeral(interaction: discord.Interaction, lines: list[str] | tuple[str, ...]) -> None:
    kwargs = {
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    message = _bounded_message(lines)
    if interaction.response.is_done():
        await interaction.followup.send(message, **kwargs)
    else:
        await interaction.response.send_message(message, **kwargs)


def _quick_check_ok(result: tuple[str, ...]) -> bool:
    return result == ("ok",)


def _m10_truth_snapshot(bot: YonerAIBot) -> tuple[M10CurrentTruthV1, bool]:
    current = getattr(bot, "deployment_current_truth_current", None)
    if current is not None:
        if not callable(current):
            return build_m10_current_truth(), False
        try:
            refreshed = current()
        except Exception:
            return build_m10_current_truth(), False
        if type(refreshed) is M10CurrentTruthV1 and getattr(bot, "deployment_current_truth", None) is refreshed:
            return refreshed, True
        return build_m10_current_truth(), False
    injected = getattr(bot, "deployment_current_truth", None)
    if type(injected) is M10CurrentTruthV1:
        return injected, True
    return build_m10_current_truth(), False


def _m10_truth_lines(truth: M10CurrentTruthV1) -> tuple[str, ...]:
    def selected(value: object | None) -> str:
        return "未選択" if value is None else str(getattr(value, "value"))

    def identifiers(values: tuple[str, ...], *, maximum: int = 6) -> str:
        visible = values[:maximum]
        text = ", ".join(visible) if visible else "なし"
        if len(values) > maximum:
            text += f"（ほか{len(values) - maximum}件）"
        return text

    def live(value: bool | None) -> str:
        if value is True:
            return "live_success=yes"
        if value is False:
            return "live_success=no"
        return "live_success=未検証"

    lines = [
        f"M10 schema: {truth.schema_version}",
        "M10 selection: "
        f"configured={'yes' if truth.selection_configured else 'no'} / "
        f"topology={selected(truth.selected_topology)} / "
        f"hosting={selected(truth.selected_hosting_profile)} / "
        f"packaging={selected(truth.selected_packaging)}",
        f"M10 effective: topology={truth.effective_topology.value}",
        "M10 ports: "
        f"available={identifiers(truth.available_ports)} / "
        f"required={identifiers(truth.required_ports)} / "
        f"missing={identifiers(truth.missing_ports)}",
        f"M10 blockers: {identifiers(truth.blockers)}",
    ]
    for name, source in (
        ("provider", truth.provider_source),
        ("sandbox", truth.sandbox_source),
        ("jobs", truth.jobs_source),
        ("audit", truth.audit_source),
    ):
        lines.append(
            f"M10 source {name}: "
            f"configured={'yes' if source.configured else 'no'} / "
            f"ready={'yes' if source.ready else 'no'} / "
            f"{live(source.live_success)} / blocker={source.blocker or 'なし'}"
        )
    return tuple(lines)


async def _m10_diagnostics_current(
    bot: YonerAIBot,
    interaction: discord.Interaction,
    *,
    command_path: str,
    expected_truth: M10CurrentTruthV1,
    expected_injected: bool,
) -> bool:
    if bool(getattr(bot, "is_closing", True)):
        return False
    capability_id = COMMAND_CAPABILITIES.get(command_path)
    guild = getattr(interaction, "guild", None)
    guild_id = getattr(interaction, "guild_id", None)
    user = getattr(interaction, "user", None)
    fetch_member = getattr(guild, "fetch_member", None)
    if capability_id is None or guild is None or not callable(fetch_member) or guild_id is None or user is None:
        return False
    try:
        expected_user_id = int(user.id)
        expected_guild_id = int(guild_id)
        if int(guild.id) != expected_guild_id:
            return False
        registry = bot.require_registry()
        guard = bot.require_guard()
        if guard is not getattr(bot, "capability_guard", None) or guard.registry is not registry:
            return False
        member = await fetch_member(expected_user_id)
        if int(member.id) != expected_user_id:
            return False
        decision = await guard.evaluate_fresh_member(
            capability_id,
            guild=guild,
            member=member,
        )
        if (
            decision.allowed is not True
            or guard is not getattr(bot, "capability_guard", None)
            or guard.registry is not registry
            or registry is not bot.require_registry()
            or bool(getattr(bot, "is_closing", True))
        ):
            return False
        if (
            guard.currently_allowed(
                capability_id,
                guild_id=expected_guild_id,
                user_id=expected_user_id,
                actor_level=decision.actor_level,
                floor=COMMAND_RBAC_FLOORS.get(command_path, RbacLevel.EVERYONE),
            )
            is not True
        ):
            return False
    except Exception:
        return False

    current_truth, current_injected = _m10_truth_snapshot(bot)
    if expected_injected:
        return current_injected is True and current_truth is expected_truth
    return current_injected is False


def _runtime_readiness(bot: YonerAIBot) -> tuple[str, ...]:
    ai_service = getattr(bot, "ai_service", None)
    runtime_readiness = getattr(bot, "runtime_capability_readiness", None)
    if isinstance(runtime_readiness, dict) and "cap-can-0161" in runtime_readiness:
        ai_available = runtime_readiness["cap-can-0161"] is True
    else:
        ai_available = bool(ai_service and ai_service.available)
    ai_provider_is_local = getattr(bot, "ai_provider_is_local", None)
    if not ai_available:
        remote_consent_readiness = "provider未起動"
    elif ai_provider_is_local is True:
        remote_consent_readiness = "ローカルproviderのため不要"
    elif bot.settings.ai_remote_consent_ttl_seconds is None:
        remote_consent_readiness = (
            "Discord user ID単位のSQLite永続同意が必要（既定は無期限、再起動後も継続。取消・版変更等で失効）"
        )
    else:
        remote_consent_readiness = (
            "Discord user ID単位のSQLite永続同意が必要"
            f"（TTL {bot.settings.ai_remote_consent_ttl_seconds}秒、再起動後も期限判定を継続）"
        )
    ai_admission = getattr(bot, "ai_admission", None)
    admission_stats = getattr(ai_admission, "stats", lambda: None)()
    speech_queue = getattr(bot, "speech_queue", None)
    music_service = getattr(bot, "music_service", None)
    memory_service = getattr(bot, "personal_memory_service", None)
    earthquake_service = getattr(bot, "earthquake_service", None)
    evolution_service = getattr(bot, "evolution_service", None)
    yonerai_service = getattr(bot, "yonerai_status_service", None)
    yonerai_state = (
        getattr(getattr(yonerai_service, "status", lambda: None)(), "state", None)
        if yonerai_service is not None
        else None
    )
    mention_callbacks = tuple(getattr(bot, "extra_events", {}).get("on_message", ()))
    mention_listener_ready = any(
        getattr(getattr(callback, "__self__", None), "__class__", object).__name__ == "AIMentionListener"
        for callback in mention_callbacks
    )
    try:
        mention_policy_ready = (
            bot.require_registry()
            .capability_status(
                "cap-run-ai-mention-chat",
                bot.settings.guild_id,
            )
            .executable
        )
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        mention_policy_ready = False
    return (
        f"AI: {'利用可能' if ai_available else '未設定/停止'}",
        "AIメンション: "
        + (
            "受信待機中"
            if bot.settings.ai_mention_enabled and mention_listener_ready and mention_policy_ready
            else "停止/利用不可"
        ),
        f"AI外部送信同意: {remote_consent_readiness}",
        "AI負荷制御: "
        + (
            f"active={admission_stats.active} / waiting={admission_stats.waiting}"
            if admission_stats is not None
            else "停止"
        ),
        f"VOICE: {'利用可能' if bool(speech_queue and speech_queue.available) else '未設定/停止'}",
        "MUSIC: "
        + (
            "利用可能（ローカル許可音源＋TTS ducking）"
            if bool(music_service and music_service.available)
            else f"待機（{getattr(music_service, 'reason', 'plugin停止')}）"
        ),
        f"個人メモリ: {'利用可能（本人ごと既定OFF）' if memory_service is not None else 'plugin停止'}",
        f"地震・EEW: {'API利用可能（通知はguildごと既定OFF）' if earthquake_service is not None else 'plugin停止'}",
        f"AutoMod: {'report-only ON' if bot.settings.automod_enabled else '停止（既定）'}",
        "本人確認: "
        + (
            (
                "owner側switch ON / localhost callback稼働"
                if getattr(bot, "identity_web_server", None) is not None
                else "owner側switch ON（外部callbackまたはHTTP未起動）"
            )
            if bot.settings.identity_enabled
            else "停止（既定）"
        ),
        f"Minecraft: {'read-only status ON' if bot.settings.minecraft_enabled else '停止（既定）'}",
        "自己進化proposal: "
        + ("審査機能ON" if bool(evolution_service and evolution_service.enabled) else "停止（既定、安全）"),
        "YonerAI連携: "
        + (str(getattr(yonerai_state, "value", "境界準備済み")) if yonerai_service is not None else "停止（既定）"),
    )


def _worker_readiness(bot: YonerAIBot) -> tuple[str, ...]:
    """現在実装済みの常駐workerだけをtask名から診断する。"""

    tasks = {
        candidate.get_name(): candidate for candidate in asyncio.all_tasks() if candidate is not asyncio.current_task()
    }

    def state(plugin_name: str, task_name: str) -> str:
        task = tasks.get(task_name)
        if not bot.plugins.is_running(plugin_name):
            return "停止（plugin無効）"
        if task is None:
            return "異常（taskなし）"
        if task.done():
            return "異常（task終了）"
        return "稼働中"

    return (
        f"Worker scheduling-reminder: {state('scheduling', 'scheduling-reminder-worker')}",
        f"Worker durable-jobs: {state('jobs', 'durable-jobs-worker')}",
    )


def _workers_healthy(bot: YonerAIBot) -> bool:
    return all("異常" not in line for line in _worker_readiness(bot))


def _recent_audit_records(database: Database, limit: int) -> tuple[object, ...]:
    """DBに専用APIがない版でも、秘密値を読まず最新監査recordだけを保持する。"""

    recent_method = getattr(database, "list_recent_audit", None)
    if callable(recent_method):
        return tuple(recent_method(limit=limit))

    records: deque[object] = deque(maxlen=limit)
    after_id = 0
    while True:
        page = database.list_audit(limit=1_000, after_id=after_id)
        if not page:
            break
        records.extend(page)
        after_id = page[-1].id
        if len(page) < 1_000:
            break
    return tuple(reversed(records))


class SystemGroup(app_commands.Group):
    def __init__(self, bot: YonerAIBot) -> None:
        super().__init__(name="system", description="Botの稼働状態を確認します")
        self.bot = bot

    @app_commands.command(name="ping", description="Botの応答とDiscord遅延を確認します")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(f"Pong! Discord遅延: {self.bot.latency * 1000:.0f}ms", ephemeral=True)

    @app_commands.command(name="health", description="コアとプラグインの健全性を確認します")
    async def health(self, interaction: discord.Interaction) -> None:
        m10_truth, m10_injected = _m10_truth_snapshot(self.bot)
        await interaction.response.defer(ephemeral=True)
        try:
            quick_check = await asyncio.to_thread(self.bot.database.quick_check)
        except Exception as exc:
            logger.error("database_quick_check_failed", extra={"error_type": type(exc).__name__})
            quick_check = ()
        database_ok = _quick_check_ok(quick_check)
        plugins_ok = self.bot.plugins.healthy()
        workers_ok = _workers_healthy(self.bot)
        inventory_ok = bool(self.bot.surface_inventory and not self.bot.surface_inventory.unmapped_command_paths)
        healthy = database_ok and plugins_ok and workers_ok and inventory_ok and not self.bot.is_closing
        uptime = int(time.monotonic() - self.bot.started_at)
        if not await _m10_diagnostics_current(
            self.bot,
            interaction,
            command_path="system health",
            expected_truth=m10_truth,
            expected_injected=m10_injected,
        ):
            await _send_ephemeral(interaction, ["現在の権限では診断情報を表示できません。"])
            return
        await _send_ephemeral(
            interaction,
            [
                f"状態: {'正常' if healthy else '要確認'}",
                *_m10_truth_lines(m10_truth),
                f"SQLite quick_check: {'正常' if database_ok else '異常'}",
                f"プラグイン: {'正常' if plugins_ok else '要確認'}",
                f"Discord surface inventory: {'正常' if inventory_ok else '要確認'}",
                *_worker_readiness(self.bot),
                *_runtime_readiness(self.bot),
                f"稼働時間: {uptime}秒",
            ],
        )

    @app_commands.command(name="plugins", description="登録済みプラグインの状態を表示します")
    async def plugins(self, interaction: discord.Interaction) -> None:
        snapshots = self.bot.plugins.snapshots()
        if not snapshots:
            message = "登録済みプラグインはありません。"
        else:
            lines = [
                f"• `{item.name}`: {item.status.value}" + (f" ({item.error})" if item.error else "")
                for item in snapshots
            ]
            message = "\n".join(lines)[:1900]
        await _send_ephemeral(interaction, [message])

    @app_commands.command(name="audit", description="秘密値を含まない最新監査eventを表示します")
    async def audit(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            records = await asyncio.to_thread(_recent_audit_records, self.bot.database, int(limit))
        except Exception as exc:
            logger.error("system_audit_read_failed", extra={"error_type": type(exc).__name__})
            await _send_ephemeral(
                interaction, ["監査ログを取得できませんでした。管理者はローカルログを確認してください。"]
            )
            return
        if not records:
            await _send_ephemeral(interaction, ["監査ログはまだありません。"])
            return
        lines = [f"最新監査event {len(records)}件（詳細本文・秘密値は表示しません）"]
        for record in records:
            scope = "global" if record.guild_id in (None, 0) else "guild"
            actor = "system" if record.actor_id in (None, 0) else str(record.actor_id)
            plugin = f" / plugin={record.plugin}" if record.plugin else ""
            lines.append(f"• #{record.id} `{record.event}` / {record.created_at} / {scope} / actor={actor}{plugin}")
        await _send_ephemeral(interaction, lines)

    @app_commands.command(name="overrides", description="このguildで有効な設定overrideを表示します")
    async def overrides(self, interaction: discord.Interaction) -> None:
        try:
            records = self.bot.database.list_effective_overrides(interaction.guild_id)
        except Exception as exc:
            logger.error("system_overrides_read_failed", extra={"error_type": type(exc).__name__})
            await _send_ephemeral(interaction, ["設定overrideを取得できませんでした。"])
            return
        if not records:
            await _send_ephemeral(interaction, ["有効な設定overrideはありません。"])
            return
        lines = [f"有効な設定override {len(records)}件（理由・操作者・秘密値は表示しません）"]
        for record in records:
            scope = "global" if record.guild_id == 0 else "guild"
            if record.kind == "permission":
                try:
                    value = RbacLevel(record.required_level).name.lower()
                except (TypeError, ValueError):
                    value = "invalid"
            else:
                value = "ON" if record.enabled else "OFF"
            lines.append(f"• [{scope}] `{record.kind}:{record.subject_id}` = `{value}`")
        await _send_ephemeral(interaction, lines)

    @app_commands.command(name="runtime", description="実command・plugin・workerの接続状態を表示します")
    async def runtime(self, interaction: discord.Interaction) -> None:
        m10_truth, m10_injected = _m10_truth_snapshot(self.bot)
        await interaction.response.defer(ephemeral=True)
        registry = self.bot.require_registry()
        inventory = self.bot.surface_inventory
        snapshots = self.bot.plugins.snapshots()
        plugin_counts = Counter(snapshot.status.value for snapshot in snapshots)
        canonical = sum(not item.capability_id.startswith("cap-run-") for item in registry.capabilities)
        runtime = len(registry.capabilities) - canonical
        available = sum(registry.runtime_available(item.capability_id) is True for item in registry.capabilities)
        implemented = sum(item.implemented for item in registry.capabilities)
        lines = [
            f"Capability registry: {len(registry.capabilities)}（canonical {canonical} / runtime {runtime}）",
            f"実装フラグ: {implemented} / runtime利用可能: {available}",
            *_m10_truth_lines(m10_truth),
        ]
        if inventory is None:
            lines.append("Discord surface inventory: 未作成")
        else:
            lines.extend(
                (
                    f"実Discord command: {len(inventory.actual_command_paths)}",
                    f"未接続command: {len(inventory.missing_command_paths)}",
                    f"未登録surface: {len(inventory.unmapped_command_paths)}",
                    f"利用可能surface capability: {len(inventory.available_capability_ids)}",
                )
            )
        lines.append("Plugin: " + ", ".join(f"{status}={count}" for status, count in sorted(plugin_counts.items())))
        lines.extend(_worker_readiness(self.bot))
        lines.extend(_runtime_readiness(self.bot))
        if not await _m10_diagnostics_current(
            self.bot,
            interaction,
            command_path="system runtime",
            expected_truth=m10_truth,
            expected_injected=m10_injected,
        ):
            await _send_ephemeral(interaction, ["現在の権限では診断情報を表示できません。"])
            return
        await _send_ephemeral(interaction, lines)

    @app_commands.command(name="backup", description="SQLiteの整合性検証付きonline backupを作成します")
    async def backup(self, interaction: discord.Interaction) -> None:
        actor_id = int(interaction.user.id)
        backup_dir = Path(self.bot.settings.database_path).parent / "backups"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        filename = f"yonerai-discord-{timestamp}-{secrets.token_hex(4)}.sqlite3"
        destination = backup_dir / filename
        try:
            self.bot.database.append_audit(
                "database.backup_requested",
                actor_id=actor_id,
                guild_id=interaction.guild_id,
                details={"backup_filename": filename},
            )
        except Exception as exc:
            logger.error("backup_request_audit_failed", extra={"error_type": type(exc).__name__})
            await _send_ephemeral(interaction, ["監査記録を作成できないため、backupを安全側で中止しました。"])
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(self.bot.database.online_backup, destination)
        except Exception as exc:
            logger.error("database_backup_failed", extra={"error_type": type(exc).__name__})
            try:
                self.bot.database.append_audit(
                    "database.backup_failed",
                    actor_id=actor_id,
                    guild_id=interaction.guild_id,
                    details={"error_type": type(exc).__name__},
                )
            except Exception as audit_exc:
                logger.error("backup_failure_audit_failed", extra={"error_type": type(audit_exc).__name__})
            await _send_ephemeral(interaction, ["backupに失敗しました。管理者はローカルログを確認してください。"])
            return
        audit_completed = True
        try:
            self.bot.database.append_audit(
                "database.backup_completed",
                actor_id=actor_id,
                guild_id=interaction.guild_id,
                details={"backup_filename": result.name},
            )
        except Exception as exc:
            audit_completed = False
            logger.error("backup_completion_audit_failed", extra={"error_type": type(exc).__name__})
        await _send_ephemeral(
            interaction,
            [
                "SQLite online backupを作成しました。",
                f"ファイル名: `{result.name}`",
                "backup quick_check: 正常",
                "完了監査: " + ("記録済み" if audit_completed else "要確認（要求監査は記録済み）"),
            ],
        )

    @app_commands.command(name="modules", description="全moduleのON/OFFと実装接続数を表示します")
    async def modules(
        self,
        interaction: discord.Interaction,
        page: app_commands.Range[int, 1, 100] = 1,
    ) -> None:
        registry = self.bot.require_registry()
        guild_id = interaction.guild_id
        per_page = 12
        start = (page - 1) * per_page
        modules = registry.modules
        selected = modules[start : start + per_page]
        if not selected:
            await interaction.response.send_message("そのページにはmoduleがありません。", ephemeral=True)
            return
        lines: list[str] = []
        for module in selected:
            status = registry.module_status(module.module_id, guild_id)
            connected = sum(
                capability.implemented and registry.runtime_available(capability.capability_id) is not False
                for capability in registry.capabilities
                if capability.module_id == module.module_id
            )
            total = sum(1 for capability in registry.capabilities if capability.module_id == module.module_id)
            marker = "ON" if status.executable else f"OFF/{status.code.value}"
            lines.append(f"• `{module.module_id}`: **{marker}**（接続 {connected}/{total}）")
        pages = max(1, (len(modules) + per_page - 1) // per_page)
        await interaction.response.send_message(
            f"Module {len(modules)}件 — {page}/{pages}\n" + "\n".join(lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="capabilities", description="canonical capability台帳を検索します")
    @app_commands.describe(query="ID・名前・moduleの部分一致（空欄で全件）")
    async def capabilities(
        self,
        interaction: discord.Interaction,
        query: str = "",
        page: app_commands.Range[int, 1, 100] = 1,
    ) -> None:
        registry = self.bot.require_registry()
        needle = query.strip().lower()
        capabilities = tuple(
            capability
            for capability in registry.capabilities
            if not needle
            or needle in capability.capability_id
            or needle in capability.module_id
            or needle in capability.name.lower()
        )
        per_page = 8
        start = (page - 1) * per_page
        selected = capabilities[start : start + per_page]
        if not selected:
            await interaction.response.send_message("一致するcapabilityがありません。", ephemeral=True)
            return
        lines: list[str] = []
        for capability in selected:
            status = registry.capability_status(capability.capability_id, interaction.guild_id)
            required = registry.required_level(capability.capability_id, interaction.guild_id)
            runtime = "実行可" if status.executable else status.code.value
            lines.append(
                f"• `{capability.capability_id}` {capability.name[:70]}\n"
                f"  `{capability.module_id}` / {runtime} / {required.name.lower()} / source={capability.source_state}"
            )
        pages = max(1, (len(capabilities) + per_page - 1) // per_page)
        await interaction.response.send_message(
            f"Capability {len(capabilities)}件 — {page}/{pages}\n" + "\n".join(lines)[:1800],
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="module-set", description="moduleをguild別または全体でON/OFFします")
    async def module_set(
        self,
        interaction: discord.Interaction,
        module_id: str,
        enabled: bool,
        scope: Literal["guild", "global"] = "guild",
    ) -> None:
        registry = self.bot.require_registry()
        actor = await self.bot.require_guard().actor(interaction)
        config_scope = ConfigScope(scope)
        guild_id = interaction.guild_id if config_scope is ConfigScope.GUILD else None
        decision = PolicyEngine(registry).evaluate_config_change(
            actor,
            scope=config_scope,
            target=ConfigTarget.MODULE,
            target_id=module_id,
            guild_id=guild_id,
        )
        if not decision.allowed:
            await interaction.response.send_message(f"変更を拒否しました（{decision.code.value}）。", ephemeral=True)
            return
        normalized = registry.module(module_id).module_id
        self.bot.database.set_module_override(
            normalized,
            enabled,
            guild_id,
            updated_by=int(actor.actor_id),
            reason=f"discord {scope} configuration",
        )
        hook_failures = await self.bot.plugins.notify_module_policy(normalized, enabled, guild_id)
        effective = registry.module_status(normalized, interaction.guild_id)
        warning = (
            "\n実行中pluginへの反映に失敗しました。policyは適用済みですが、完全反映には再起動が必要です: "
            + ", ".join(hook_failures)
            if hook_failures
            else ""
        )
        await interaction.response.send_message(
            f"`{normalized}` を {scope} で {'ON' if enabled else 'OFF'} にしました。"
            f" 現在: {effective.code.value}{warning}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="capability-set", description="個別capabilityをON/OFFします")
    async def capability_set(
        self,
        interaction: discord.Interaction,
        capability_id: str,
        enabled: bool,
        scope: Literal["guild", "global"] = "guild",
    ) -> None:
        registry = self.bot.require_registry()
        actor = await self.bot.require_guard().actor(interaction)
        config_scope = ConfigScope(scope)
        guild_id = interaction.guild_id if config_scope is ConfigScope.GUILD else None
        decision = PolicyEngine(registry).evaluate_config_change(
            actor,
            scope=config_scope,
            target=ConfigTarget.CAPABILITY,
            target_id=capability_id,
            guild_id=guild_id,
        )
        if not decision.allowed:
            await interaction.response.send_message(f"変更を拒否しました（{decision.code.value}）。", ephemeral=True)
            return
        normalized = registry.capability(capability_id).capability_id
        if normalized in DELEGATABLE_ACTOR_CAPABILITY_IDS:
            if config_scope is not ConfigScope.GUILD or not actor.is_bot_owner:
                await interaction.response.send_message(
                    "このcapabilityはBOT所有者がguild単位でのみ変更できます。",
                    ephemeral=True,
                )
                return
            self.bot.database.set_owner_managed_capability_enabled(
                normalized,
                enabled,
                int(guild_id),
                updated_by=int(actor.actor_id),
                reason="Discord owner-managed capability configuration",
            )
        else:
            self.bot.database.set_capability_override(
                normalized,
                enabled,
                guild_id,
                updated_by=int(actor.actor_id),
                reason=f"discord {scope} configuration",
            )
        hook_failures = await self.bot.plugins.notify_capability_policy(normalized, enabled, guild_id)
        effective = registry.capability_status(normalized, interaction.guild_id)
        warning = (
            "\n実行中pluginへの反映に失敗しました。policyは適用済みですが、完全反映には再起動が必要です: "
            + ", ".join(hook_failures)
            if hook_failures
            else ""
        )
        await interaction.response.send_message(
            f"`{normalized}` を {scope} で {'ON' if enabled else 'OFF'} にしました。"
            f" 現在: {effective.code.value}{warning}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="permission-set", description="capabilityの必要RBACを設定します")
    async def permission_set(
        self,
        interaction: discord.Interaction,
        capability_id: str,
        level: Literal["everyone", "trusted", "moderator", "guild_admin", "guild_owner", "bot_owner"],
        scope: Literal["guild", "global"] = "guild",
    ) -> None:
        registry = self.bot.require_registry()
        actor = await self.bot.require_guard().actor(interaction)
        requested = RbacLevel.parse(level)
        config_scope = ConfigScope(scope)
        guild_id = interaction.guild_id if config_scope is ConfigScope.GUILD else None
        decision = PolicyEngine(registry).evaluate_config_change(
            actor,
            scope=config_scope,
            target=ConfigTarget.CAPABILITY_LEVEL,
            target_id=capability_id,
            guild_id=guild_id,
            requested_level=requested,
        )
        if not decision.allowed:
            await interaction.response.send_message(f"変更を拒否しました（{decision.code.value}）。", ephemeral=True)
            return
        normalized = registry.capability(capability_id).capability_id
        if normalized in DELEGATABLE_ACTOR_CAPABILITY_IDS and (
            config_scope is not ConfigScope.GUILD or not actor.is_bot_owner or requested is not RbacLevel.BOT_OWNER
        ):
            await interaction.response.send_message(
                "このcapabilityの基準権限はguild単位のBOT所有者から変更できません。",
                ephemeral=True,
            )
            return
        self.bot.database.set_level_override(
            normalized,
            requested,
            guild_id,
            updated_by=int(actor.actor_id),
            reason=f"discord {scope} configuration",
        )
        await interaction.response.send_message(
            f"`{normalized}` の必要権限を {scope} で `{requested.name.lower()}` にしました。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="doctor", description="Bot権限と設定を事前診断します")
    async def doctor(self, interaction: discord.Interaction) -> None:
        m10_truth, m10_injected = _m10_truth_snapshot(self.bot)
        guild = interaction.guild
        if guild is None:
            await _send_ephemeral(interaction, ["サーバー内でのみ利用できます。"])
            return
        member = guild.me or (guild.get_member(self.bot.user.id) if self.bot.user else None)
        if member is None:
            await _send_ephemeral(interaction, ["Bot自身のメンバー情報を取得できません。"])
            return
        await interaction.response.defer(ephemeral=True)
        guild_permissions = member.guild_permissions
        channel = getattr(interaction, "channel", None)
        permissions_for = getattr(channel, "permissions_for", None)
        permissions = permissions_for(member) if callable(permissions_for) else guild_permissions
        checks = {
            "チャンネル閲覧": permissions.view_channel,
            "メッセージ送信": permissions.send_messages,
            "thread内送信": getattr(permissions, "send_messages_in_threads", False),
            "履歴閲覧": permissions.read_message_history,
            "埋め込みリンク": getattr(permissions, "embed_links", False),
            "ファイル添付": getattr(permissions, "attach_files", False),
            "メッセージ管理": guild_permissions.manage_messages,
            "チャンネル管理": guild_permissions.manage_channels,
            "ロール管理": guild_permissions.manage_roles,
            "ニックネーム管理": guild_permissions.manage_nicknames,
            "メンバータイムアウト": guild_permissions.moderate_members,
            "Kick": guild_permissions.kick_members,
            "Ban": guild_permissions.ban_members,
            "VC接続": getattr(guild_permissions, "connect", False),
            "VC発言": getattr(guild_permissions, "speak", False),
        }
        lines = [
            *_m10_truth_lines(m10_truth),
            *(f"{'OK' if ok else '不足'}: {label}" for label, ok in checks.items()),
        ]
        try:
            quick_check = await asyncio.to_thread(self.bot.database.quick_check)
        except Exception as exc:
            logger.error("doctor_database_quick_check_failed", extra={"error_type": type(exc).__name__})
            quick_check = ()
        inventory = self.bot.surface_inventory
        member_events = self.bot.settings.member_events_enabled
        automod_enabled = self.bot.settings.automod_enabled
        ai_reply_continuation_enabled = self.bot.settings.ai_reply_continuation_enabled
        music_read_aloud_enabled = self.bot.settings.music_read_aloud_enabled
        message_content_required = automod_enabled or ai_reply_continuation_enabled or music_read_aloud_enabled
        message_events = (
            self.bot.settings.message_audit_events_enabled
            or automod_enabled
            or self.bot.settings.ai_mention_enabled
            or music_read_aloud_enabled
        )
        lines.extend(
            (
                f"SQLite quick_check: {'OK' if _quick_check_ok(quick_check) else '異常'}",
                f"プラグイン: {'OK' if self.bot.plugins.healthy() else '要確認'}",
                "Discord surface inventory: "
                + ("OK" if inventory is not None and not inventory.unmapped_command_paths else "要確認"),
                f"BOT_OWNER_IDS: {'設定済み' if self.bot.settings.bot_owner_ids else '未設定（所有者機能は利用不可）'}",
                f"DISCORD_GUILD_ID: {'設定済み' if self.bot.settings.guild_id is not None else '未設定'}",
                f"Members intent要求: {'ON' if member_events else 'OFF'} / 実行時: {'ON' if self.bot.intents.members else 'OFF'}",
                f"Message events要求: {'ON' if message_events else 'OFF'} / 実行時: {'ON' if self.bot.intents.guild_messages else 'OFF'}",
                "Message Content intent: "
                + (
                    (
                        "ON（AutoMod/AI返信継続/読み上げに必要）"
                        if self.bot.intents.message_content
                        else "不足（AutoMod/AI返信継続/読み上げに必要）"
                    )
                    if message_content_required
                    else ("ON（停止を推奨）" if self.bot.intents.message_content else "OFF（推奨）")
                ),
                "Developer Portal Server Members Intent: "
                + ("人間による有効化確認が必要" if member_events else "不要（設定OFF）"),
                "Developer Portal Message Content Intent: "
                + (
                    "人間による有効化確認が必要"
                    if message_content_required
                    else "不要（AutoMod/AI返信継続/読み上げ OFF）"
                ),
                *_worker_readiness(self.bot),
                *_runtime_readiness(self.bot),
                "Administrator: "
                + ("付与済み（最小権限への変更を推奨）" if guild_permissions.administrator else "未付与（推奨）"),
            )
        )
        if not await _m10_diagnostics_current(
            self.bot,
            interaction,
            command_path="system doctor",
            expected_truth=m10_truth,
            expected_injected=m10_injected,
        ):
            await _send_ephemeral(interaction, ["現在の権限では診断情報を表示できません。"])
            return
        await _send_ephemeral(interaction, lines)


async def _tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    client = getattr(interaction, "client", None)
    terminal = getattr(client, "interaction_failure_terminal", None)
    if not isinstance(terminal, InteractionFailureTerminal):
        terminal = InteractionFailureTerminal()
    await terminal.fail_once(
        interaction,
        error,
        surface="application_command",
    )


class YonerAIBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = settings.member_events_enabled
        intents.guild_messages = (
            settings.message_audit_events_enabled
            or settings.automod_enabled
            or settings.ai_mention_enabled
            or settings.music_read_aloud_enabled
        )
        intents.voice_states = settings.music_enabled
        # AutoModはreport-onlyかつ本文非保存。それでも検知入力に本文が必要なため、
        # process全体switchを明示ONにした場合だけprivileged intentを要求する。
        intents.message_content = (
            settings.automod_enabled or settings.ai_reply_continuation_enabled or settings.music_read_aloud_enabled
        )
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=CapabilityCommandTree,
        )
        self.settings = settings
        self.database = Database(settings.database_path)
        self.plugins = PluginManager()
        self.capability_registry: Registry | None = None
        self.capability_guard: CapabilityGuard | None = None
        self.interaction_failure_terminal = InteractionFailureTerminal()
        self.surface_inventory: SurfaceInventoryReport | None = None
        self.tree.on_error = _tree_error
        self.started_at = time.monotonic()
        self.is_closing = False
        self._close_lock = asyncio.Lock()

    def register_plugin(self, name: str, factory: PluginFactory) -> None:
        self.plugins.register(name, factory)

    async def setup_hook(self) -> None:
        self.database.open()
        migration_version = self.database.migrate()
        logger.info("database_ready", extra={"migration_version": migration_version})
        self.capability_registry = build_capability_registry(self.settings, self.database)
        self.capability_guard = CapabilityGuard(
            self,
            self.settings,
            self.capability_registry,
            self.database,
        )
        logger.info(
            "capability_registry_ready",
            extra={
                "capability_count": len(self.capability_registry.capabilities),
                "connected_count": sum(item.implemented for item in self.capability_registry.capabilities),
                "module_count": len(self.capability_registry.modules),
            },
        )
        self.tree.add_command(SystemGroup(self))
        discover_plugins(
            self.plugins,
            "yonerai_discord.modules",
            manifest=BUILTIN_PLUGIN_MANIFEST,
        )
        selected_plugins = startup_plugins_for_registry(
            self.settings,
            self.require_registry(),
        )
        await self.plugins.start_all(self, selected_plugins)
        self.surface_inventory = reconcile_runtime_surfaces(
            self.require_registry(),
            self.tree,
            self.plugins,
        )
        logger.info(
            "runtime_surfaces_ready",
            extra={
                "actual_command_count": len(self.surface_inventory.actual_command_paths),
                "available_capability_count": len(self.surface_inventory.available_capability_ids),
                "missing_command_count": len(self.surface_inventory.missing_command_paths),
            },
        )

        await self._sync_application_commands()

    async def _sync_application_commands(self) -> None:
        """同期対象ごとにコマンドを反映し、部分失敗を起動成功として扱わない。"""
        guild_ids = self.settings.command_sync_guild_ids
        leaf_command_path_count = len(self.surface_inventory.actual_command_paths) if self.surface_inventory else 0
        if guild_ids:
            # 既存のDISCORD_GUILD_ID優先方針を複数guildにも拡張する。global同期は実行しない。
            if self.settings.sync_global_commands:
                logger.warning(
                    "global_command_sync_skipped_for_guild_targets",
                    extra={"target_guild_ids": guild_ids},
                )

            failed_guild_ids: list[int] = []
            for guild_id in guild_ids:
                try:
                    guild = discord.Object(id=guild_id)
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                except Exception as exc:
                    failed_guild_ids.append(guild_id)
                    logger.exception(
                        "commands_sync_failed",
                        extra={
                            "sync_scope": "guild",
                            "target_guild_id": guild_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
                logger.info(
                    "commands_sync_finished",
                    extra={
                        "sync_scope": "guild",
                        "target_guild_id": guild_id,
                        "root_command_count": len(synced),
                        "leaf_command_path_count": leaf_command_path_count,
                    },
                )

            if failed_guild_ids:
                failed = ", ".join(str(guild_id) for guild_id in failed_guild_ids)
                raise RuntimeError(f"guild command sync failed for guild IDs: {failed}")
            return

        if self.settings.sync_global_commands:
            synced = await self.tree.sync()
            logger.info(
                "commands_sync_finished",
                extra={
                    "sync_scope": "global",
                    "target_guild_id": None,
                    "root_command_count": len(synced),
                    "leaf_command_path_count": leaf_command_path_count,
                },
            )
            return

        logger.warning("global_command_sync_skipped")

    async def close(self) -> None:
        async with self._close_lock:
            if self.is_closing:
                return
            self.is_closing = True
            logger.info("shutdown_started")
            quiesce_failures = await self.plugins.quiesce_all(
                timeout_per_plugin=min(5.0, float(self.settings.shutdown_timeout_seconds))
            )
            if quiesce_failures:
                logger.warning(
                    "plugin_quiesce_incomplete",
                    extra={"plugin_count": len(quiesce_failures), "plugins": quiesce_failures},
                )
            await self.plugins.stop_all(timeout_per_plugin=float(self.settings.shutdown_timeout_seconds))
            self.database.close()
            await super().close()
            logger.info("shutdown_complete")

    async def on_ready(self) -> None:
        logger.info(
            "bot_ready",
            extra={"bot_user_id": self.user.id if self.user else None, "guild_count": len(self.guilds)},
        )

    async def on_command_error(self, context: commands.Context, error: commands.CommandError) -> None:
        # The application uses slash commands. Direct ``@BOT question`` messages
        # also reach discord.py's legacy mention-prefix parser, so an ordinary AI
        # conversation must not become a false CommandNotFound runtime error.
        if isinstance(error, commands.CommandNotFound):
            return
        await self.interaction_failure_terminal.fail_context_once(
            context,
            error,
            surface="prefix_command",
        )

    def require_registry(self) -> Registry:
        if self.capability_registry is None:
            raise RuntimeError("capability registry is not ready")
        return self.capability_registry

    def require_guard(self) -> CapabilityGuard:
        if self.capability_guard is None:
            raise RuntimeError("capability guard is not ready")
        return self.capability_guard
