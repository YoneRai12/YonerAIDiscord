from __future__ import annotations

import asyncio
import json
from io import BytesIO
from typing import Literal

import discord
from discord import app_commands

from yonerai_discord.capabilities import COMMAND_CAPABILITIES, COMMAND_RBAC_FLOORS
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.discord_policy import determine_rbac_level
from yonerai_discord.v0_runtime.command_service import (
    CommandActor,
    CommandResult,
    CommandScope,
    MemoryCommand,
    MemoryCommandInput,
    V0CommandService,
)
from yonerai_discord.v0_runtime.renderer import render_command_result

from .domain import MemoryKind
from .service import (
    CONVERSATION_RETENTION_SECONDS,
    FACT_RETENTION_SECONDS,
    MemoryDisabledError,
    PersonalMemoryService,
)


ENABLE_CONFIRMATION = "I_CONSENT"
CLEAR_CONFIRMATION = "CLEAR_MY_MEMORY"
_CLEAR_PARTIAL_MESSAGE = (
    "個人メモリの全保存先で削除完了を確認できませんでした。一部だけ削除された可能性があります。"
    "もう一度 `/memory clear` を実行してください。"
)
_CLEAR_COMPLETED_HIDDEN_MESSAGE = "個人メモリの削除処理は完了しました。現在の権限では削除件数を表示できません。"


class MemoryGroup(app_commands.Group):
    def __init__(self, service: PersonalMemoryService, *, v0_commands: V0CommandService | None = None) -> None:
        super().__init__(name="memory", description="本人だけが管理できる個人AIメモリ")
        self.service = service
        self.v0_commands = v0_commands

    @app_commands.command(name="status", description="自分の個人メモリ状態を確認します")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        enabled = await asyncio.to_thread(self.service.is_enabled, guild_id, interaction.user.id)
        items = await asyncio.to_thread(self.service.list_items, guild_id, interaction.user.id, limit=100)
        counts = {kind: sum(item.kind is kind for item in items) for kind in MemoryKind}
        await _reply(
            interaction,
            f"個人メモリ: {'ON' if enabled else 'OFF'}\n保存中: {len(items)}件"
            f"（明示メモ {counts[MemoryKind.FACT]} / 会話 {counts[MemoryKind.CONVERSATION]}）\n"
            f"会話保持: {CONVERSATION_RETENTION_SECONDS // 86400}日 / 明示メモ: {FACT_RETENTION_SECONDS // 86400}日",
        )

    @app_commands.command(name="enable", description="個人メモリを本人の同意で有効化します")
    @app_commands.describe(confirmation=f"有効化する場合は {ENABLE_CONFIRMATION} と入力")
    @app_commands.guild_only()
    async def enable(self, interaction: discord.Interaction, confirmation: str) -> None:
        if interaction.guild_id is None:
            return
        if confirmation.strip() != ENABLE_CONFIRMATION:
            await _reply(
                interaction,
                "有効化すると、BOTへの@会話とAI回答をローカルSQLiteへ平文保存し、"
                "次回以降OpenAIへ文脈として送ります。アプリ層の暗号化はまだありません。"
                f"同意する場合だけ `{ENABLE_CONFIRMATION}` と入力してください。",
            )
            return
        try:
            await asyncio.to_thread(self.service.enable, interaction.guild_id, interaction.user.id)
        except MemoryDisabledError:
            await _reply(interaction, "個人メモリは現在利用できません。")
            return
        await _reply(interaction, "個人メモリを有効化しました。管理・削除できるのはあなた自身だけです。")

    @app_commands.command(name="disable", description="今後の記録と利用を停止します")
    @app_commands.guild_only()
    async def disable(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            return
        try:
            await asyncio.to_thread(self.service.disable, interaction.guild_id, interaction.user.id)
        except MemoryDisabledError:
            await _reply(interaction, "個人メモリは現在利用できません。")
            return
        await _reply(interaction, "個人メモリを停止しました。既存データは /memory clear で削除できます。")

    @app_commands.command(name="remember", description="本人用の長期メモを明示的に保存します")
    @app_commands.describe(text="保存する内容（最大1000文字）")
    async def remember(self, interaction: discord.Interaction, text: str) -> None:
        await self._run_v0(interaction, MemoryCommand.REMEMBER, text, path="memory remember")

    @app_commands.command(name="list", description="自分の保存内容だけを表示します")
    @app_commands.describe(limit="表示件数")
    async def list_items(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 20] = 10,
    ) -> None:
        del limit
        await self._run_v0(interaction, MemoryCommand.LIST, path="memory list")

    @app_commands.command(name="forget", description="自分のメモをID指定で削除します")
    async def forget(self, interaction: discord.Interaction, memory_id: str) -> None:
        await self._run_v0(interaction, MemoryCommand.FORGET, memory_id, path="memory forget")

    @app_commands.command(name="clear", description="自分の個人メモリを全削除します")
    @app_commands.describe(confirmation=f"全削除する場合は {CLEAR_CONFIRMATION} と入力")
    async def clear(self, interaction: discord.Interaction, confirmation: str) -> None:
        await self._run_v0(interaction, MemoryCommand.CLEAR, confirmation, path="memory clear")

    @app_commands.command(name="search", description="自分の個人メモリを関連度で検索します")
    @app_commands.describe(query="探す内容", limit="表示件数")
    @app_commands.guild_only()
    async def search(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 1, 200],
        limit: app_commands.Range[int, 1, 10] = 5,
    ) -> None:
        if interaction.guild_id is None:
            return
        items = await asyncio.to_thread(
            self.service.search,
            interaction.guild_id,
            interaction.user.id,
            query,
            limit=limit,
        )
        if not items:
            await _reply(interaction, "関連する個人メモリはありません。")
            return
        lines = []
        for item in items:
            label = "明示メモ" if item.kind is MemoryKind.FACT else "会話"
            lines.append(f"`{item.id}` [{label}] {item.content.replace(chr(10), ' ')[:300]}")
        await _reply(interaction, "\n".join(lines))

    @app_commands.command(name="preview", description="AIへ渡る自分の記憶文脈を確認します")
    async def preview(self, interaction: discord.Interaction) -> None:
        await self._run_v0(interaction, MemoryCommand.PREVIEW, path="memory preview")

    @app_commands.command(name="export", description="自分の個人メモリをJSONで取得します")
    @app_commands.guild_only()
    async def export(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            return
        try:
            payload = await asyncio.to_thread(
                self.service.export_payload,
                interaction.guild_id,
                interaction.user.id,
            )
        except MemoryDisabledError:
            await _reply(interaction, "個人メモリは現在利用できません。")
            return
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        await interaction.response.send_message(
            "あなた自身の個人メモリです。内容に注意して保管してください。",
            file=discord.File(BytesIO(encoded), filename="personal-memory.json"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="privacy", description="保存・参照範囲を表示または変更します")
    async def privacy(
        self,
        interaction: discord.Interaction,
        mode: Literal["show", "private", "channel", "guild"] = "show",
    ) -> None:
        await self._run_v0(
            interaction,
            MemoryCommand.PRIVACY,
            "" if mode == "show" else mode,
            path="memory privacy",
        )

    async def _run_v0(
        self,
        interaction: discord.Interaction,
        command: MemoryCommand,
        value: str = "",
        *,
        path: str,
    ) -> None:
        actor = _v0_actor(interaction)
        if self.v0_commands is None or actor is None or not _memory_command_allowed(interaction, path):
            result = CommandResult(False, "actor_not_authorized", {})
        else:
            mutation = command in {
                MemoryCommand.REMEMBER,
                MemoryCommand.FORGET,
                MemoryCommand.CLEAR,
            } or (command is MemoryCommand.PRIVACY and bool(value.strip()))
            if mutation:
                actor = await _fresh_memory_mutation_actor(interaction, path)
            if actor is None:
                result = CommandResult(False, "authorization_changed", {})
                await _reply(interaction, render_command_result(result))
                return
            result = self.v0_commands.execute_memory(
                MemoryCommandInput(actor, command, value),
                commit_check=(
                    (
                        lambda checked_actor, _operation: (
                            checked_actor == actor and _memory_command_allowed(interaction, path)
                        )
                    )
                    if mutation
                    else None
                ),
            )
            if command is MemoryCommand.CLEAR and result.ok:
                refreshed_actor = await _fresh_memory_mutation_actor(interaction, path)
                if refreshed_actor != actor or not _memory_command_allowed(interaction, path):
                    result = CommandResult(False, "memory_clear_partial", {})
                else:
                    result = self._clear_legacy_memory(actor, result)
            if not _memory_command_allowed(interaction, path):
                if command is MemoryCommand.CLEAR and result.code == "memory_cleared":
                    result = CommandResult(True, "memory_clear_completed_hidden", {})
                elif command is not MemoryCommand.CLEAR or result.code != "memory_clear_partial":
                    result = CommandResult(False, "authorization_changed", {})
        await _reply(interaction, _render_memory_result(result))

    def _clear_legacy_memory(self, actor: CommandActor, v0_result: CommandResult) -> CommandResult:
        guild_id = actor.scope.guild_id
        if guild_id is None:
            return v0_result
        v0_deleted = v0_result.data.get("deleted")
        if type(v0_deleted) is not int or v0_deleted < 0:
            return CommandResult(False, "memory_clear_partial", {})
        try:
            legacy_deleted = self.service.clear(guild_id, actor.user_id)
        except Exception:
            return CommandResult(False, "memory_clear_partial", {})
        if type(legacy_deleted) is not int or legacy_deleted < 0:
            return CommandResult(False, "memory_clear_partial", {})
        return CommandResult(True, "memory_cleared", {"deleted": v0_deleted + legacy_deleted})


def _render_memory_result(result: CommandResult) -> str:
    if result.code == "memory_clear_partial":
        return _CLEAR_PARTIAL_MESSAGE
    if result.code == "memory_clear_completed_hidden":
        return _CLEAR_COMPLETED_HIDDEN_MESSAGE
    return render_command_result(result)


def _v0_actor(interaction: discord.Interaction) -> CommandActor | None:
    user = getattr(interaction, "user", None)
    user_id = getattr(user, "id", None)
    channel_id = getattr(interaction, "channel_id", None)
    guild_id = getattr(interaction, "guild_id", None)
    if not isinstance(user_id, int) or not isinstance(channel_id, int):
        return None
    scope = (
        CommandScope(None, dm_channel_id=channel_id)
        if guild_id is None
        else CommandScope(int(guild_id), channel_id=channel_id)
    )
    can_share = bool(getattr(getattr(user, "guild_permissions", None), "manage_guild", False))
    return CommandActor(user_id, scope, True, can_share)


def _memory_command_allowed(interaction: discord.Interaction, path: str) -> bool:
    client = getattr(interaction, "client", None)
    if client is None or bool(getattr(client, "is_closing", False)):
        return False
    guild_id = getattr(interaction, "guild_id", None)
    capability_id = COMMAND_CAPABILITIES.get(path)
    checker = getattr(getattr(client, "capability_guard", None), "currently_allowed", None)
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if capability_id is None or not callable(checker) or not isinstance(user_id, int):
        return False
    try:
        return bool(
            checker(
                capability_id,
                guild_id=guild_id,
                user_id=user_id,
                actor_level=_memory_actor_level(interaction),
                floor=COMMAND_RBAC_FLOORS.get(path, RbacLevel.EVERYONE),
            )
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _memory_actor_level(interaction: discord.Interaction) -> RbacLevel:
    user = getattr(interaction, "user", None)
    guild = getattr(interaction, "guild", None)
    settings = getattr(getattr(interaction, "client", None), "settings", None)
    if user is None or settings is None:
        return RbacLevel.EVERYONE
    roles = getattr(user, "roles", ()) or ()
    role_ids = frozenset(
        int(role.id)
        for role in roles
        if isinstance(getattr(role, "id", None), int) and not isinstance(role.id, bool) and role.id > 0
    )
    try:
        return determine_rbac_level(
            user_id=int(user.id),
            guild_owner_id=(
                int(guild.owner_id) if guild is not None and isinstance(getattr(guild, "owner_id", None), int) else None
            ),
            permissions=getattr(user, "guild_permissions", None),
            role_ids=role_ids,
            settings=settings,
        )
    except (AttributeError, TypeError, ValueError):
        return RbacLevel.EVERYONE


async def _fresh_memory_mutation_actor(
    interaction: discord.Interaction,
    path: str,
) -> CommandActor | None:
    guild = getattr(interaction, "guild", None)
    if guild is None:
        actor = _v0_actor(interaction)
        return actor if actor is not None and _memory_command_allowed(interaction, path) else None
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    channel_id = getattr(interaction, "channel_id", None)
    fetch_member = getattr(guild, "fetch_member", None)
    guard = getattr(getattr(interaction, "client", None), "capability_guard", None)
    evaluate = getattr(guard, "evaluate_fresh_member", None)
    capability_id = COMMAND_CAPABILITIES.get(path)
    if (
        not isinstance(user_id, int)
        or not isinstance(channel_id, int)
        or capability_id is None
        or not callable(fetch_member)
        or not callable(evaluate)
    ):
        return None
    try:
        member = await fetch_member(user_id)
        if getattr(member, "id", None) != user_id:
            return None
        decision = await evaluate(capability_id, guild=guild, member=member)
        actor_level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
        if not bool(getattr(decision, "allowed", False)) or actor_level < COMMAND_RBAC_FLOORS.get(
            path, RbacLevel.EVERYONE
        ):
            return None
        permissions = getattr(member, "guild_permissions", None)
        return CommandActor(
            user_id,
            CommandScope(int(guild.id), channel_id=channel_id),
            True,
            bool(getattr(permissions, "manage_guild", False)),
        )
    except (discord.HTTPException, AttributeError, KeyError, TypeError, ValueError):
        return None


async def _reply(interaction: discord.Interaction, text: str) -> None:
    content = text.strip()[:1_900] or "表示する内容はありません。"
    kwargs = {"ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)
