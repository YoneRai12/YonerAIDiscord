from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import discord

from yonerai_discord.discord_payload_budget import validate_discord_payload_budget
from yonerai_discord.modules.audio_core import LoopMode, QueueSnapshot, RecentTrackState
from yonerai_discord.modules.operations import SafeInteractionView

from .authorization import MusicFreshCheck, build_music_commit_check
from .models import (
    GuildAudioProjection,
    MusicActor,
    MusicAuthorizationError,
    MusicDashboardBinding,
    MusicError,
    MusicSessionError,
)
from .repository import MusicPlaylistRepository
from .service import MusicService


NO_MENTIONS = discord.AllowedMentions.none()
_PREPARING = "音楽ダッシュボードを準備しています。"
_MAX_DASHBOARD_CHARACTERS = 1_900
_CONTROL_COMMANDS = {
    "pause": "music pause",
    "resume": "music resume",
    "skip": "music skip",
    "stop": "music stop",
    "refresh": "music queue",
}
_LIBRARY_REF_TOKEN = re.compile(r"(?i)(?<![a-z0-9_-])root-[0-9]+:")
_SHA256_TOKEN = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_DRIVE_PATH_TOKEN = re.compile(r"(?i)(?:^|[\s\"'(<])(?:[a-z]:|file:)")
_PRIVATE_TITLE = "非公開の曲"


class MusicDashboardView(SafeInteractionView):
    def __init__(
        self,
        controller: MusicDashboardController,
        binding: MusicDashboardBinding,
    ) -> None:
        super().__init__(timeout=None)
        self.controller = controller
        self.binding = binding

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.controller.scope_is_current(interaction, self.binding):
            return True
        await _component_reply(interaction, "このダッシュボードは現在操作できません。")
        return False

    @discord.ui.button(
        label="一時停止",
        style=discord.ButtonStyle.secondary,
        custom_id="music:dashboard:pause",
    )
    async def pause(self, interaction: discord.Interaction, _button: discord.ui.Button[Any]) -> None:
        await self.controller.control(interaction, self.binding, "pause")

    @discord.ui.button(
        label="再開",
        style=discord.ButtonStyle.secondary,
        custom_id="music:dashboard:resume",
    )
    async def resume(self, interaction: discord.Interaction, _button: discord.ui.Button[Any]) -> None:
        await self.controller.control(interaction, self.binding, "resume")

    @discord.ui.button(
        label="スキップ",
        style=discord.ButtonStyle.primary,
        custom_id="music:dashboard:skip",
    )
    async def skip(self, interaction: discord.Interaction, _button: discord.ui.Button[Any]) -> None:
        await self.controller.control(interaction, self.binding, "skip")

    @discord.ui.button(
        label="停止",
        style=discord.ButtonStyle.danger,
        custom_id="music:dashboard:stop",
    )
    async def stop_music(self, interaction: discord.Interaction, _button: discord.ui.Button[Any]) -> None:
        await self.controller.control(interaction, self.binding, "stop")

    @discord.ui.button(
        label="更新",
        style=discord.ButtonStyle.secondary,
        custom_id="music:dashboard:refresh",
    )
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button[Any]) -> None:
        await self.controller.control(interaction, self.binding, "refresh")


class MusicDashboardController:
    """MusicServiceの公開状態を1 guild 1 messageへcoalesceして反映する薄いDiscord境界。"""

    def __init__(
        self,
        bot: Any,
        service: MusicService,
        repository: MusicPlaylistRepository,
        *,
        runtime_current: Callable[[], bool],
    ) -> None:
        if not callable(runtime_current):
            raise TypeError("runtime_current must be callable")
        self.bot = bot
        self.service = service
        self.repository = repository
        self._runtime_current = runtime_current
        self._closing = False
        self._views: dict[int, MusicDashboardView] = {}
        self._update_serials: dict[int, int] = {}
        self._update_tasks: dict[int, asyncio.Task[None]] = {}

    async def restore_views(self) -> None:
        if not self._current():
            return
        bindings = await asyncio.to_thread(self.repository.list_dashboard_bindings)
        if not self._current():
            return
        add_view = getattr(self.bot, "add_view", None)
        if not callable(add_view):
            return
        for binding in bindings:
            if not self._current():
                return
            view = MusicDashboardView(self, binding)
            add_view(view, message_id=binding.message_id)
            self._remember_view(binding, view)

    async def schedule_all(self) -> None:
        if not self._current():
            return
        bindings = await asyncio.to_thread(self.repository.list_dashboard_bindings)
        if not self._current():
            return
        for binding in bindings:
            self.schedule_update(binding.guild_id)

    async def close(self) -> None:
        self._closing = True
        tasks = tuple(self._update_tasks.values())
        self._update_tasks.clear()
        self._update_serials.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for view in self._views.values():
            view.stop()
        self._views.clear()

    async def create(self, interaction: discord.Interaction) -> MusicDashboardBinding:
        guild_id, channel_id, owner_id = _interaction_scope(interaction)
        guild = interaction.guild
        if guild is None:
            raise MusicSessionError("guild is required")
        fresh = await self._fresh_scope(guild_id, channel_id, owner_id, "music queue")
        if fresh is None:
            raise MusicAuthorizationError("dashboard authorization changed")
        _check, _channel = fresh
        snapshot = await self.service.snapshot(guild_id)
        if await self._fresh_scope(guild_id, channel_id, owner_id, "music queue") is None:
            raise MusicAuthorizationError("dashboard authorization changed")
        content = render_music_dashboard(
            snapshot=snapshot,
            radio_enabled=self.service.local_radio_enabled(guild_id),
        )

        response = interaction.response
        if not response.is_done():
            await response.defer(ephemeral=False, thinking=True)
        if await self._fresh_scope(guild_id, channel_id, owner_id, "music queue") is None:
            raise MusicAuthorizationError("dashboard authorization changed")
        message = await interaction.followup.send(
            _PREPARING,
            wait=True,
            allowed_mentions=NO_MENTIONS,
        )
        message_id = _positive_id(message)
        message_channel_id = _positive_id(getattr(message, "channel", None))
        message_guild_id = _positive_id(getattr(message, "guild", None))
        if (
            message_id is None
            or message_channel_id != channel_id
            or message_guild_id != guild_id
            or await self._fresh_scope(guild_id, channel_id, owner_id, "music queue") is None
        ):
            await _discard_created_message(message)
            raise MusicAuthorizationError("dashboard binding changed")

        binding = MusicDashboardBinding(guild_id, channel_id, message_id, owner_id)
        try:
            await asyncio.to_thread(
                self.repository.save_dashboard_binding,
                binding,
                commit_current=self._current,
            )
        except BaseException:
            await _discard_created_message(message)
            raise
        if not await self._binding_current(binding):
            await self._delete_binding(binding)
            await _discard_created_message(message)
            raise MusicAuthorizationError("dashboard binding changed")
        if await self._fresh_scope(guild_id, channel_id, owner_id, "music queue") is None:
            await self._delete_binding(binding)
            await _discard_created_message(message)
            raise MusicAuthorizationError("dashboard authorization changed")

        view = MusicDashboardView(self, binding)
        try:
            await message.edit(
                content=content,
                view=view,
                allowed_mentions=NO_MENTIONS,
            )
        except (discord.NotFound, discord.Forbidden):
            await self._delete_stale(binding)
            view.stop()
            raise MusicSessionError("dashboard message is unavailable") from None
        except discord.HTTPException:
            self._remember_view(binding, view)
            self.schedule_update(guild_id)
            raise MusicSessionError("dashboard delivery is temporarily unavailable") from None
        except BaseException:
            await self._delete_binding(binding)
            view.stop()
            raise
        self._remember_view(binding, view)
        return binding

    def schedule_update(self, guild_id: int) -> None:
        if not self._current() or _positive_id(guild_id) is None:
            return
        serial = self._update_serials.get(guild_id, 0) + 1
        self._update_serials[guild_id] = serial
        existing = self._update_tasks.get(guild_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._update_loop(guild_id))
        self._update_tasks[guild_id] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._update_tasks.get(guild_id) is completed:
                self._update_tasks.pop(guild_id, None)

        task.add_done_callback(discard)

    async def scope_is_current(
        self,
        interaction: discord.Interaction,
        binding: MusicDashboardBinding,
    ) -> bool:
        message = getattr(interaction, "message", None)
        return bool(
            self._current()
            and getattr(interaction, "guild_id", None) == binding.guild_id
            and getattr(interaction, "channel_id", None) == binding.channel_id
            and _positive_id(message) == binding.message_id
            and await self._binding_current(binding)
        )

    async def control(
        self,
        interaction: discord.Interaction,
        binding: MusicDashboardBinding,
        action: str,
    ) -> None:
        command_path = _CONTROL_COMMANDS.get(action)
        if command_path is None or not await self.scope_is_current(interaction, binding):
            await _component_reply(interaction, "このダッシュボードは現在操作できません。")
            return
        guild = interaction.guild
        user_id = _positive_id(getattr(interaction, "user", None))
        if guild is None or user_id is None:
            await _component_reply(interaction, "このダッシュボードは現在操作できません。")
            return
        fresh = await self._fresh_scope(
            binding.guild_id,
            binding.channel_id,
            user_id,
            command_path,
        )
        if fresh is None or not await self._binding_current(binding):
            await _component_reply(interaction, "操作待機中に権限または音楽機能が変更されました。")
            return
        check, _channel = fresh
        control_current = self._control_commit_check(binding, check)
        actor = await control_current()
        if actor is None:
            await _component_reply(interaction, "操作待機中に権限または音楽機能が変更されました。")
            return
        try:
            if action == "pause":
                await self.service.pause(binding.guild_id, actor, commit_check=control_current)
            elif action == "resume":
                await self.service.resume(binding.guild_id, actor, commit_check=control_current)
            elif action == "skip":
                await self.service.skip(binding.guild_id, actor, commit_check=control_current)
            elif action == "stop":
                await self.service.stop_music(binding.guild_id, actor, commit_check=control_current)
            elif action == "refresh":
                await self.service.snapshot(binding.guild_id)
            else:
                raise ValueError("unknown dashboard action")
        except MusicError:
            await _component_reply(interaction, "この音楽操作は現在実行できません。")
            return
        if (
            await self._fresh_scope(
                binding.guild_id,
                binding.channel_id,
                user_id,
                command_path,
            )
            is None
        ):
            await _component_reply(interaction, "操作は完了しましたが、現在状態の表示権限を確認できません。")
            return
        self.schedule_update(binding.guild_id)
        await _component_reply(interaction, "音楽ダッシュボードを更新しました。")

    async def _update_loop(self, guild_id: int) -> None:
        try:
            while self._current():
                serial = self._update_serials.get(guild_id)
                if serial is None:
                    return
                await asyncio.sleep(0)
                await self._update_once(guild_id)
                if self._update_serials.get(guild_id) == serial:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _update_once(self, guild_id: int) -> bool:
        if not self._current():
            return False
        binding = await asyncio.to_thread(self.repository.load_dashboard_binding, guild_id)
        if binding is None or not self._current():
            return False
        fresh = await self._fresh_delivery(binding)
        if fresh is None:
            return False
        _check, channel = fresh
        content = await self._current_content(binding.guild_id)
        if content is None or not self._current():
            return False
        fetch_message = getattr(channel, "fetch_message", None)
        if not callable(fetch_message):
            return False
        try:
            message = await fetch_message(binding.message_id)
        except (discord.NotFound, discord.Forbidden):
            await self._delete_stale(binding)
            return False
        except (discord.HTTPException, asyncio.TimeoutError):
            return False
        except Exception:
            return False
        if not _message_matches(message, binding):
            return False
        if not await self._binding_current(binding):
            return False
        fresh = await self._fresh_delivery(binding)
        if fresh is None:
            return False
        view = self._views.get(binding.guild_id)
        if view is None or view.binding != binding or view.is_finished():
            view = MusicDashboardView(self, binding)
            self._remember_view(binding, view)
        try:
            await message.edit(
                content=content,
                view=view,
                allowed_mentions=NO_MENTIONS,
            )
        except (discord.NotFound, discord.Forbidden):
            await self._delete_stale(binding)
            return False
        except (discord.HTTPException, asyncio.TimeoutError):
            return False
        return True

    async def _current_content(self, guild_id: int) -> str | None:
        if not self._current():
            return None
        try:
            snapshot = await self.service.snapshot(guild_id)
        except MusicError:
            projection = await asyncio.to_thread(self.repository.load_audio_projection, guild_id)
            if not self._current():
                return None
            return render_pending_music_dashboard(projection)
        if not self._current():
            return None
        return render_music_dashboard(
            snapshot=snapshot,
            radio_enabled=self.service.local_radio_enabled(guild_id),
        )

    async def _fresh_delivery(
        self,
        binding: MusicDashboardBinding,
    ) -> tuple[MusicFreshCheck, Any] | None:
        return await self._fresh_scope(
            binding.guild_id,
            binding.channel_id,
            binding.owner_id,
            "music queue",
        )

    async def _fresh_scope(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        command_path: str,
    ) -> tuple[MusicFreshCheck, Any] | None:
        if not self._current():
            return None
        get_guild = getattr(self.bot, "get_guild", None)
        guild = get_guild(guild_id) if callable(get_guild) else None
        bot_id = _positive_id(getattr(self.bot, "user", None))
        fetch_channel = getattr(guild, "fetch_channel", None)
        fetch_member = getattr(guild, "fetch_member", None)
        if guild is None or bot_id is None or not callable(fetch_channel) or not callable(fetch_member):
            return None
        check = await self._fresh_check(guild, user_id, command_path)
        if await _checked_actor(check) is None:
            return None
        try:
            channel = await fetch_channel(channel_id)
            bot_member = await fetch_member(bot_id)
        except Exception:
            return None
        if (
            _positive_id(channel) != channel_id
            or _positive_id(getattr(channel, "guild", None)) != guild_id
            or _positive_id(bot_member) != bot_id
            or not _bot_can_manage_dashboard(channel, bot_member)
            or await _checked_actor(check) is None
            or not self._current()
        ):
            return None
        return check, channel

    async def _fresh_check(
        self,
        guild: Any,
        user_id: int,
        command_path: str,
    ) -> MusicFreshCheck | None:
        check = await build_music_commit_check(self.bot, guild, user_id, command_path)
        return check if self._current() else None

    async def _binding_current(self, binding: MusicDashboardBinding) -> bool:
        if not self._current():
            return False
        try:
            current = await asyncio.to_thread(self.repository.dashboard_binding_is_current, binding)
        except Exception:
            return False
        return current and self._current()

    def _control_commit_check(
        self,
        binding: MusicDashboardBinding,
        check: MusicFreshCheck,
    ) -> MusicFreshCheck:
        async def current_actor() -> MusicActor | None:
            if not self._current() or not await self._binding_current(binding):
                return None
            actor = await _checked_actor(check)
            if actor is None or not self._current():
                return None
            if not await self._binding_current(binding):
                return None
            return actor

        return current_actor

    async def _delete_stale(self, binding: MusicDashboardBinding) -> bool:
        if not self._current():
            return False
        get_guild = getattr(self.bot, "get_guild", None)
        guild = get_guild(binding.guild_id) if callable(get_guild) else None
        if guild is None:
            return False
        check = await self._fresh_check(guild, binding.owner_id, "music queue")
        if await _checked_actor(check) is None or not self._current():
            return False
        return await self._delete_binding(binding)

    async def _delete_binding(self, binding: MusicDashboardBinding) -> bool:
        if not self._current():
            return False
        try:
            removed = await asyncio.to_thread(self.repository.delete_dashboard_binding, binding)
        except Exception:
            return False
        if removed:
            view = self._views.pop(binding.guild_id, None)
            if view is not None and view.binding == binding:
                view.stop()
        return removed

    def _remember_view(
        self,
        binding: MusicDashboardBinding,
        view: MusicDashboardView,
    ) -> None:
        previous = self._views.get(binding.guild_id)
        if previous is not None and previous is not view:
            previous.stop()
        self._views[binding.guild_id] = view

    def _current(self) -> bool:
        if self._closing:
            return False
        try:
            return self._runtime_current() is True
        except Exception:
            return False


def render_music_dashboard(*, snapshot: QueueSnapshot, radio_enabled: bool) -> str:
    if not isinstance(snapshot, QueueSnapshot) or not isinstance(radio_enabled, bool):
        raise TypeError("dashboard state is invalid")
    lines = ["🎵 音楽ダッシュボード"]
    if snapshot.current is None:
        lines.append("再生中: なし")
    else:
        lines.append(f"再生中: {_safe_title(snapshot.current.title)} （依頼者: <@{snapshot.current.requester_id}>）")
    lines.append(f"状態: {'一時停止中' if snapshot.paused else '再生可能'}")
    lines.append(f"ループ: {_loop_label(snapshot.loop_mode)}")
    lines.append(f"音量: 音楽 {round(snapshot.volume * 100)}% / TTS {round(snapshot.speech_volume * 100)}%")
    lines.append(f"ローカルラジオ: {'ON' if radio_enabled else 'OFF'}")
    lines.append("待機queue:")
    if snapshot.upcoming:
        for index, track in enumerate(snapshot.upcoming[:10], start=1):
            lines.append(f"{index}. {_safe_title(track.title)}（依頼者: <@{track.requester_id}>）")
        if len(snapshot.upcoming) > 10:
            lines.append(f"ほか {len(snapshot.upcoming) - 10}曲")
    else:
        lines.append("- なし")
    if snapshot.recent:
        labels = {
            RecentTrackState.COMPLETED: "完了",
            RecentTrackState.SKIPPED: "スキップ",
            RecentTrackState.STOPPED: "停止",
            RecentTrackState.FAILED: "失敗",
        }
        lines.append("最近の再生:")
        for item in snapshot.recent[:5]:
            lines.append(f"- {_safe_title(item.title)}（{labels[item.state]}）")
    return _bounded_dashboard(lines)


def render_pending_music_dashboard(projection: GuildAudioProjection | None) -> str:
    lines = ["🎵 音楽ダッシュボード", "状態: VC未接続"]
    if projection is None:
        lines.append("待機queue: なし")
        lines.append("明示的に `/music join` すると再開できます。")
    else:
        lines.append(f"待機queue: {len(projection.tracks)}曲")
        lines.append(f"一時停止: {'ON' if projection.paused else 'OFF'}")
        lines.append(f"ループ: {_loop_label(projection.loop_mode)}")
        lines.append(
            f"音量: 音楽 {round(projection.music_volume * 100)}% / TTS {round(projection.speech_volume * 100)}%"
        )
        lines.append("曲名はVC接続後に権利とcontent identityを再確認して表示します。")
    lines.append("ローカルラジオ: OFF")
    return _bounded_dashboard(lines)


async def _checked_actor(check: MusicFreshCheck | None) -> MusicActor | None:
    if check is None:
        return None
    try:
        actor = await check()
    except Exception:
        return None
    return actor if isinstance(actor, MusicActor) else None


def _interaction_scope(interaction: discord.Interaction) -> tuple[int, int, int]:
    guild_id = getattr(interaction, "guild_id", None)
    channel_id = getattr(interaction, "channel_id", None)
    owner_id = _positive_id(getattr(interaction, "user", None))
    if (
        isinstance(guild_id, bool)
        or not isinstance(guild_id, int)
        or guild_id <= 0
        or isinstance(channel_id, bool)
        or not isinstance(channel_id, int)
        or channel_id <= 0
        or owner_id is None
    ):
        raise MusicSessionError("dashboard scope is invalid")
    return guild_id, channel_id, owner_id


def _positive_id(value: Any) -> int | None:
    identifier = getattr(value, "id", value)
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
        return None
    return identifier


def _message_matches(message: Any, binding: MusicDashboardBinding) -> bool:
    return bool(
        _positive_id(message) == binding.message_id
        and _positive_id(getattr(message, "channel", None)) == binding.channel_id
        and _positive_id(getattr(message, "guild", None)) == binding.guild_id
    )


def _bot_can_manage_dashboard(channel: Any, bot_member: Any) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    try:
        permissions = permissions_for(bot_member)
    except Exception:
        return False
    can_send = bool(
        getattr(permissions, "send_messages", False) or getattr(permissions, "send_messages_in_threads", False)
    )
    return bool(
        getattr(permissions, "view_channel", False) and getattr(permissions, "read_message_history", False) and can_send
    )


def _safe_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if (
        not normalized
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        or "/" in normalized
        or "\\" in normalized
        or _LIBRARY_REF_TOKEN.search(normalized)
        or _SHA256_TOKEN.search(normalized)
        or _DRIVE_PATH_TOKEN.search(normalized)
    ):
        return _PRIVATE_TITLE
    return discord.utils.escape_mentions(discord.utils.escape_markdown(normalized))[:180]


def _loop_label(mode: LoopMode) -> str:
    return {
        LoopMode.OFF: "オフ",
        LoopMode.TRACK: "この曲",
        LoopMode.QUEUE: "キュー",
    }[LoopMode(mode)]


def _bounded_dashboard(lines: list[str]) -> str:
    accepted: list[str] = []
    for line in lines:
        candidate = "\n".join((*accepted, str(line)))
        if len(candidate) > _MAX_DASHBOARD_CHARACTERS:
            accepted.append("…表示上限のため一部を省略しました。")
            break
        accepted.append(str(line))
    content = "\n".join(accepted)
    validate_discord_payload_budget(content=content)
    return content


async def _component_reply(interaction: discord.Interaction, content: str) -> None:
    kwargs = {
        "ephemeral": True,
        "allowed_mentions": NO_MENTIONS,
    }
    if interaction.response.is_done():
        await interaction.followup.send(str(content)[:1_900], **kwargs)
    else:
        await interaction.response.send_message(str(content)[:1_900], **kwargs)


async def _discard_created_message(message: Any) -> None:
    delete = getattr(message, "delete", None)
    if callable(delete):
        with suppress(Exception):
            await delete()


__all__ = [
    "MusicDashboardController",
    "MusicDashboardView",
    "render_music_dashboard",
    "render_pending_music_dashboard",
]
