from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord
import pytest

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.music.adapter import MusicGroup
from yonerai_discord.modules.scheduling.focus import (
    FocusTimerCancelReceipt,
    FocusTimerRequest,
    FocusTimerScheduleReceipt,
)
from yonerai_discord.modules.voice.presets import (
    ResolvedVoicePreset,
    VoicePresetRecord,
    VoicePresetScope,
    VoicePresetValues,
)
from yonerai_discord.modules.voice.read_aloud import (
    ReadAloudDictionaryEntry,
    ReadAloudPolicySnapshot,
    ReadAloudRepositoryError,
    ReadAloudRoute,
)


class _Response:
    def __init__(self) -> None:
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs: Any) -> None:
        assert kwargs == {"ephemeral": True, "thinking": True}
        self.done = True


class _Followup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.messages.append((content, kwargs))


class _Guard:
    def __init__(self) -> None:
        self.allowed = True
        self.capabilities: list[str] = []

    async def actor(self, _context: Any) -> Any:
        return SimpleNamespace(level=RbacLevel.GUILD_ADMIN)

    def currently_allowed(self, capability_id: str, **_kwargs: Any) -> bool:
        self.capabilities.append(capability_id)
        return self.allowed


class _Channel:
    def __init__(
        self,
        channel_id: int,
        channel_type: discord.ChannelType,
        guild: Any,
        *,
        read: bool = True,
        speak: bool = True,
    ) -> None:
        self.id = channel_id
        self.type = channel_type
        self.guild = guild
        self.read = read
        self.speak = speak

    def permissions_for(self, _member: Any) -> Any:
        return SimpleNamespace(
            view_channel=self.read,
            read_message_history=self.read,
            connect=self.speak,
            speak=self.speak,
        )


class _Guild:
    id = 100

    def __init__(self) -> None:
        self.channels: dict[int, _Channel] = {}
        self.user = SimpleNamespace(
            id=10,
            voice=SimpleNamespace(channel=None),
            guild_permissions=SimpleNamespace(administrator=False, manage_guild=True),
        )
        self.bot_member = SimpleNamespace(id=999)
        self.me = self.bot_member

    async def fetch_member(self, user_id: int) -> Any:
        if user_id == self.user.id:
            return self.user
        if user_id == self.bot_member.id:
            return self.bot_member
        raise LookupError("member not found")

    def get_channel_or_thread(self, channel_id: int) -> Any | None:
        return self.channels.get(channel_id)

    def get_channel(self, channel_id: int) -> Any | None:
        return self.channels.get(channel_id)


class _Repository:
    is_open = True

    def __init__(self) -> None:
        self.routes: dict[tuple[int, int], ReadAloudRoute] = {}
        self.policies: dict[int, ReadAloudPolicySnapshot] = {}
        self.put_calls: list[tuple[int, int, int, int | None]] = []
        self.delete_calls: list[tuple[int, int, int | None]] = []
        self.policy_calls: list[tuple[str, int, str, int]] = []
        self.after_read: Any | None = None
        self.after_write: Any | None = None
        self.conflict = False

    def get(self, guild_id: int, source_channel_id: int) -> ReadAloudRoute | None:
        route = self.routes.get((guild_id, source_channel_id))
        if callable(self.after_read):
            self.after_read()
        return route

    def put(
        self,
        *,
        guild_id: int,
        source_channel_id: int,
        destination_voice_channel_id: int,
        enabled: bool,
        expected_revision: int | None,
    ) -> ReadAloudRoute:
        assert enabled is True
        if self.conflict:
            raise ReadAloudRepositoryError("route_revision_conflict")
        current = self.routes.get((guild_id, source_channel_id))
        revision = 1 if current is None else current.revision + 1
        route = ReadAloudRoute(
            guild_id,
            source_channel_id,
            destination_voice_channel_id,
            True,
            revision=revision,
        )
        self.put_calls.append((guild_id, source_channel_id, destination_voice_channel_id, expected_revision))
        self.routes[(guild_id, source_channel_id)] = route
        if callable(self.after_write):
            self.after_write()
        return route

    def delete(
        self,
        guild_id: int,
        source_channel_id: int,
        *,
        expected_revision: int | None,
    ) -> bool:
        self.delete_calls.append((guild_id, source_channel_id, expected_revision))
        if self.conflict:
            return False
        current = self.routes.get((guild_id, source_channel_id))
        if current is None or current.revision != expected_revision:
            return False
        del self.routes[(guild_id, source_channel_id)]
        if callable(self.after_write):
            self.after_write()
        return True

    def list_for_guild(self, guild_id: int) -> tuple[ReadAloudRoute, ...]:
        routes = tuple(
            route for (route_guild_id, _source_id), route in sorted(self.routes.items()) if route_guild_id == guild_id
        )
        if callable(self.after_read):
            self.after_read()
        return routes

    def get_policy(self, guild_id: int) -> ReadAloudPolicySnapshot:
        snapshot = self.policies.get(guild_id, ReadAloudPolicySnapshot(guild_id, 0))
        if callable(self.after_read):
            self.after_read()
        return snapshot

    def set_dictionary(
        self,
        guild_id: int,
        term: str,
        pronunciation: str,
        *,
        expected_revision: int,
    ) -> ReadAloudPolicySnapshot:
        return self._policy_write(
            "dictionary-set",
            guild_id,
            term,
            expected_revision,
            pronunciation=pronunciation,
        )

    def delete_dictionary(
        self,
        guild_id: int,
        term: str,
        *,
        expected_revision: int,
    ) -> ReadAloudPolicySnapshot:
        return self._policy_write("dictionary-delete", guild_id, term, expected_revision)

    def add_exclusion(
        self,
        guild_id: int,
        phrase: str,
        *,
        expected_revision: int,
    ) -> ReadAloudPolicySnapshot:
        return self._policy_write("exclude-add", guild_id, phrase, expected_revision)

    def delete_exclusion(
        self,
        guild_id: int,
        phrase: str,
        *,
        expected_revision: int,
    ) -> ReadAloudPolicySnapshot:
        return self._policy_write("exclude-delete", guild_id, phrase, expected_revision)

    def _policy_write(
        self,
        operation: str,
        guild_id: int,
        value: str,
        expected_revision: int,
        *,
        pronunciation: str | None = None,
    ) -> ReadAloudPolicySnapshot:
        current = self.get_policy(guild_id)
        if self.conflict or current.revision != expected_revision:
            raise ReadAloudRepositoryError("policy_revision_conflict")
        dictionary = {item.term: item.pronunciation for item in current.dictionary}
        exclusions = set(current.exclusions)
        if operation == "dictionary-set" and pronunciation is not None:
            dictionary[value] = pronunciation
        elif operation == "dictionary-delete":
            if value not in dictionary:
                raise ReadAloudRepositoryError("dictionary_term_missing")
            dictionary.pop(value)
        elif operation == "exclude-add":
            exclusions.add(value)
        elif operation == "exclude-delete":
            if value not in exclusions:
                raise ReadAloudRepositoryError("exclusion_phrase_missing")
            exclusions.remove(value)
        snapshot = ReadAloudPolicySnapshot(
            guild_id,
            current.revision + 1,
            tuple(ReadAloudDictionaryEntry(term, reading) for term, reading in sorted(dictionary.items())),
            tuple(sorted(exclusions)),
        )
        self.policy_calls.append((operation, guild_id, value, expected_revision))
        self.policies[guild_id] = snapshot
        if callable(self.after_write):
            self.after_write()
        return snapshot


class _PresetRepository:
    is_open = True

    def __init__(self) -> None:
        self.server: dict[int, VoicePresetRecord] = {}
        self.users: dict[tuple[int, int], VoicePresetRecord] = {}
        self.calls: list[tuple[str, int, int | None, int]] = []
        self.after_read: Any | None = None
        self.after_write: Any | None = None

    def get_server(self, guild_id: int) -> VoicePresetRecord | None:
        record = self.server.get(guild_id)
        if callable(self.after_read):
            self.after_read()
        return record

    def get_user(self, guild_id: int, user_id: int) -> VoicePresetRecord | None:
        record = self.users.get((guild_id, user_id))
        if callable(self.after_read):
            self.after_read()
        return record

    def resolve(self, guild_id: int, user_id: int) -> ResolvedVoicePreset:
        user = self.users.get((guild_id, user_id))
        server = self.server.get(guild_id)
        if user is not None:
            result = ResolvedVoicePreset(
                guild_id,
                user_id,
                user.values,
                VoicePresetScope.USER,
                user.revision,
            )
        elif server is not None:
            result = ResolvedVoicePreset(
                guild_id,
                user_id,
                server.values,
                VoicePresetScope.SERVER,
                server.revision,
            )
        else:
            result = ResolvedVoicePreset(guild_id, user_id)
        if callable(self.after_read):
            self.after_read()
        return result

    def set_server(
        self,
        guild_id: int,
        values: VoicePresetValues,
        *,
        expected_revision: int,
    ) -> VoicePresetRecord:
        current = self.server.get(guild_id)
        assert expected_revision == (0 if current is None else current.revision)
        record = VoicePresetRecord(guild_id, None, values, expected_revision + 1)
        self.server[guild_id] = record
        self.calls.append(("server-set", guild_id, None, expected_revision))
        if callable(self.after_write):
            self.after_write()
        return record

    def set_user(
        self,
        guild_id: int,
        user_id: int,
        values: VoicePresetValues,
        *,
        expected_revision: int,
    ) -> VoicePresetRecord:
        current = self.users.get((guild_id, user_id))
        assert expected_revision == (0 if current is None else current.revision)
        record = VoicePresetRecord(guild_id, user_id, values, expected_revision + 1)
        self.users[(guild_id, user_id)] = record
        self.calls.append(("user-set", guild_id, user_id, expected_revision))
        if callable(self.after_write):
            self.after_write()
        return record

    def clear_server(self, guild_id: int, *, expected_revision: int) -> bool:
        current = self.server.get(guild_id)
        assert expected_revision == (0 if current is None else current.revision)
        removed = self.server.pop(guild_id, None) is not None
        self.calls.append(("server-clear", guild_id, None, expected_revision))
        if callable(self.after_write):
            self.after_write()
        return removed

    def clear_user(self, guild_id: int, user_id: int, *, expected_revision: int) -> bool:
        current = self.users.get((guild_id, user_id))
        assert expected_revision == (0 if current is None else current.revision)
        removed = self.users.pop((guild_id, user_id), None) is not None
        self.calls.append(("user-clear", guild_id, user_id, expected_revision))
        if callable(self.after_write):
            self.after_write()
        return removed


class _FocusTimerService:
    def __init__(self) -> None:
        self.requests: list[FocusTimerRequest] = []
        self.cancel_calls: list[tuple[int, int, int]] = []
        self.after_schedule: Any | None = None

    async def schedule(self, request: FocusTimerRequest) -> FocusTimerScheduleReceipt:
        self.requests.append(request)
        if callable(self.after_schedule):
            self.after_schedule()
        return FocusTimerScheduleReceipt(
            job_id=request.job_id,
            binding_digest=request.binding_digest,
        )

    async def cancel_active(
        self,
        *,
        guild_id: int,
        source_channel_id: int,
        actor_id: int,
    ) -> FocusTimerCancelReceipt:
        self.cancel_calls.append((guild_id, source_channel_id, actor_id))
        return FocusTimerCancelReceipt(
            binding_digest="0" * 64,
            overlay_disabled=True,
            job_cancelled=True,
        )


def _fixture(
    *,
    source_type: discord.ChannelType = discord.ChannelType.text,
) -> tuple[Any, MusicGroup, _Repository, _Guard, _Channel, _Channel]:
    guild = _Guild()
    source = _Channel(200, source_type, guild)
    destination = _Channel(300, discord.ChannelType.voice, guild)
    guild.channels = {200: source, 300: destination}
    repository = _Repository()
    preset_repository = _PresetRepository()
    guard = _Guard()
    music_service = SimpleNamespace(available=True)
    read_aloud_service = SimpleNamespace(available=True)
    focus_service = _FocusTimerService()
    scheduling_plugin = SimpleNamespace(
        focus_service=focus_service,
        closing=False,
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        capability_guard=guard,
        is_closing=False,
        music_service=music_service,
        music_read_aloud_repository=repository,
        music_read_aloud_preset_repository=preset_repository,
        music_read_aloud_service=read_aloud_service,
        scheduling_focus_timer_service=focus_service,
        scheduling_plugin=scheduling_plugin,
    )
    group = MusicGroup(bot, music_service)  # type: ignore[arg-type]
    interaction = SimpleNamespace(
        id=777,
        guild_id=guild.id,
        guild=guild,
        channel_id=source.id,
        channel=source,
        user=guild.user,
        response=_Response(),
        followup=_Followup(),
    )
    return interaction, group, repository, guard, source, destination


def test_read_aloud_subgroup_registers_route_and_policy_management_commands() -> None:
    _interaction, group, _repository, _guard, _source, _destination = _fixture()

    assert {command.name for command in group.read_aloud.commands} == {
        "enable",
        "disable",
        "focus-start",
        "focus-cancel",
        "list",
        "dictionary-set",
        "dictionary-delete",
        "exclude-add",
        "exclude-delete",
        "policy",
        "server-preset",
        "my-preset",
        "preset",
    }


@pytest.mark.asyncio
async def test_focus_start_and_cancel_use_durable_scheduling_service() -> None:
    interaction, group, _repository, guard, source, destination = _fixture()
    interaction.guild.user.voice.channel = destination
    service = group.bot.scheduling_focus_timer_service

    await group.read_aloud.focus_start.callback(
        group.read_aloud,
        interaction,
        destination,
        25,
        True,
    )

    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.binding.timer_id == f"focus-{source.id}"
    assert request.binding.owner_id == interaction.user.id
    assert request.binding.guild_id == interaction.guild_id
    assert request.binding.source_channel_id == source.id
    assert request.binding.destination_channel_id == destination.id
    assert request.binding.revision == interaction.id
    assert request.speak_on_complete is True
    assert set(guard.capabilities) == {"cap-run-music-read-aloud-message"}
    assert interaction.followup.messages[-1][1]["allowed_mentions"].everyone is False

    interaction.response = _Response()
    interaction.followup = _Followup()
    await group.read_aloud.focus_cancel.callback(group.read_aloud, interaction)

    assert service.cancel_calls == [(100, source.id, interaction.user.id)]
    assert interaction.followup.messages[-1][0] == ("一時読み上げとフォーカスタイマーを取り消しました。")


@pytest.mark.asyncio
async def test_focus_start_requires_current_voice_and_hides_post_write_revoke() -> None:
    interaction, group, _repository, guard, _source, destination = _fixture()
    service = group.bot.scheduling_focus_timer_service

    await group.read_aloud.focus_start.callback(
        group.read_aloud,
        interaction,
        destination,
        25,
        False,
    )
    assert service.requests == []

    interaction.guild.user.voice.channel = destination
    interaction.response = _Response()
    interaction.followup = _Followup()
    service.after_schedule = lambda: setattr(guard, "allowed", False)
    await group.read_aloud.focus_start.callback(
        group.read_aloud,
        interaction,
        destination,
        25,
        False,
    )

    assert len(service.requests) == 1
    assert "開始しました" not in interaction.followup.messages[-1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_type",
    (
        discord.ChannelType.text,
        discord.ChannelType.public_thread,
        discord.ChannelType.voice,
    ),
)
async def test_enable_binds_current_text_thread_or_vc_chat_to_destination(
    source_type: discord.ChannelType,
) -> None:
    interaction, group, repository, guard, source, destination = _fixture(source_type=source_type)

    await group.read_aloud.enable.callback(
        group.read_aloud,
        interaction,
        destination,
    )

    assert repository.put_calls == [(100, source.id, destination.id, None)]
    assert repository.routes[(100, source.id)].destination_voice_channel_id == destination.id
    assert set(guard.capabilities) == {"cap-run-music-read-aloud-message"}
    content, kwargs = interaction.followup.messages[-1]
    assert f"<#{destination.id}>" in content
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].everyone is False


@pytest.mark.asyncio
async def test_disable_uses_revision_cas_and_list_is_bounded_to_25() -> None:
    interaction, group, repository, _guard, source, destination = _fixture()
    repository.routes[(100, source.id)] = ReadAloudRoute(100, source.id, destination.id, True)

    await group.read_aloud.disable.callback(group.read_aloud, interaction)

    assert repository.delete_calls == [(100, source.id, 1)]
    assert repository.routes == {}

    interaction, group, repository, _guard, source, _destination = _fixture()
    for offset in range(30):
        route_source = _Channel(1_000 + offset, discord.ChannelType.text, interaction.guild)
        route_destination = _Channel(2_000 + offset, discord.ChannelType.voice, interaction.guild)
        interaction.guild.channels[route_source.id] = route_source
        interaction.guild.channels[route_destination.id] = route_destination
        repository.routes[(100, route_source.id)] = ReadAloudRoute(
            100,
            route_source.id,
            route_destination.id,
            True,
        )

    await group.read_aloud.list_routes.callback(group.read_aloud, interaction)

    content = interaction.followup.messages[-1][0]
    assert content.count(" → ") == 25
    assert "<#1000>" in content
    assert "<#1025>" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("policy", "repository", "service"))
async def test_fresh_downgrade_or_identity_swap_after_read_keeps_mutation_zero(
    change: str,
) -> None:
    interaction, group, repository, guard, _source, destination = _fixture()

    def revoke() -> None:
        if change == "policy":
            guard.allowed = False
        elif change == "repository":
            group.bot.music_read_aloud_repository = object()
        else:
            group.bot.music_read_aloud_service = object()

    repository.after_read = revoke
    await group.read_aloud.enable.callback(group.read_aloud, interaction, destination)

    assert repository.put_calls == []
    assert "変更できませんでした" in interaction.followup.messages[-1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("enable", "disable"))
async def test_post_write_revoke_never_displays_success(operation: str) -> None:
    interaction, group, repository, guard, source, destination = _fixture()
    if operation == "disable":
        repository.routes[(100, source.id)] = ReadAloudRoute(
            100,
            source.id,
            destination.id,
            True,
        )
    repository.after_write = lambda: setattr(guard, "allowed", False)

    if operation == "enable":
        await group.read_aloud.enable.callback(group.read_aloud, interaction, destination)
        assert repository.put_calls
    else:
        await group.read_aloud.disable.callback(group.read_aloud, interaction)
        assert repository.delete_calls

    content = interaction.followup.messages[-1][0]
    assert content == "設定結果を表示できませんでした。現在の権限を確認してください。"
    assert "読み上げます。" not in content
    assert "無効にしました。" not in content


@pytest.mark.asyncio
async def test_bot_permission_denial_and_cas_conflict_are_fail_closed() -> None:
    interaction, group, repository, _guard, _source, destination = _fixture()
    destination.speak = False

    await group.read_aloud.enable.callback(group.read_aloud, interaction, destination)

    assert repository.put_calls == []
    destination.speak = True
    repository.conflict = True
    interaction.response = _Response()
    interaction.followup = _Followup()

    await group.read_aloud.enable.callback(group.read_aloud, interaction, destination)

    assert repository.put_calls == []
    assert "変更できませんでした" in interaction.followup.messages[-1][0]


@pytest.mark.asyncio
async def test_policy_commands_set_delete_and_display_safe_bounded_list() -> None:
    interaction, group, repository, guard, _source, _destination = _fixture()

    await group.read_aloud.dictionary_set.callback(
        group.read_aloud,
        interaction,
        "@everyone_*",
        "えぶりわん",
    )
    assert repository.policy_calls == [("dictionary-set", 100, "@everyone_*", 0)]
    assert set(guard.capabilities) == {"cap-run-music-read-aloud-message"}
    assert interaction.followup.messages[-1][1]["ephemeral"] is True

    interaction.response = _Response()
    await group.read_aloud.exclude_add.callback(
        group.read_aloud,
        interaction,
        "秘密",
    )
    assert repository.policy_calls[-1] == ("exclude-add", 100, "秘密", 1)

    for index in range(25):
        current = repository.get_policy(100)
        repository.set_dictionary(
            100,
            f"term-{index}",
            "reading",
            expected_revision=current.revision,
        )
    interaction.response = _Response()
    await group.read_aloud.policy.callback(group.read_aloud, interaction)
    content, kwargs = interaction.followup.messages[-1]
    assert len(content.splitlines()) == 20
    assert "@everyone" not in content
    assert "\\_" in content
    assert kwargs["allowed_mentions"].everyone is False

    interaction.response = _Response()
    await group.read_aloud.dictionary_delete.callback(
        group.read_aloud,
        interaction,
        "@everyone_*",
    )
    assert repository.policy_calls[-1][0] == "dictionary-delete"
    interaction.response = _Response()
    await group.read_aloud.exclude_delete.callback(
        group.read_aloud,
        interaction,
        "秘密",
    )
    assert repository.policy_calls[-1][0] == "exclude-delete"


@pytest.mark.asyncio
async def test_policy_command_fresh_revoke_and_cas_conflict_keep_sink_zero() -> None:
    interaction, group, repository, guard, _source, _destination = _fixture()
    repository.after_read = lambda: setattr(guard, "allowed", False)

    await group.read_aloud.dictionary_set.callback(
        group.read_aloud,
        interaction,
        "term",
        "reading",
    )
    assert repository.policy_calls == []
    assert "変更できませんでした" in interaction.followup.messages[-1][0]

    interaction, group, repository, _guard, _source, _destination = _fixture()
    repository.conflict = True
    await group.read_aloud.exclude_add.callback(
        group.read_aloud,
        interaction,
        "phrase",
    )
    assert repository.policy_calls == []
    assert "変更できませんでした" in interaction.followup.messages[-1][0]


@pytest.mark.asyncio
async def test_policy_post_write_revoke_never_displays_success() -> None:
    interaction, group, repository, guard, _source, _destination = _fixture()
    repository.after_write = lambda: setattr(guard, "allowed", False)

    await group.read_aloud.dictionary_set.callback(
        group.read_aloud,
        interaction,
        "term",
        "reading",
    )

    assert repository.policy_calls
    assert interaction.followup.messages[-1][0] == ("設定結果を表示できませんでした。現在の権限を確認してください。")


@pytest.mark.asyncio
async def test_server_and_user_presets_set_clear_and_display_effective_values() -> None:
    interaction, group, _repository, guard, _source, _destination = _fixture()
    preset_repository = group.bot.music_read_aloud_preset_repository

    await group.read_aloud.server_preset.callback(
        group.read_aloud,
        interaction,
        125,
        80,
        False,
    )
    assert preset_repository.calls == [("server-set", 100, None, 0)]
    assert preset_repository.server[100].values == VoicePresetValues(1_250, 800)
    assert "速度 125%" in interaction.followup.messages[-1][0]
    assert set(guard.capabilities) == {"cap-run-music-read-aloud-message"}

    interaction.guild.user.guild_permissions.manage_guild = False
    interaction.response = _Response()
    await group.read_aloud.my_preset.callback(
        group.read_aloud,
        interaction,
        150,
        60,
        False,
    )
    assert preset_repository.calls[-1] == ("user-set", 100, 10, 0)
    assert preset_repository.users[(100, 10)].values == VoicePresetValues(1_500, 600)

    interaction.response = _Response()
    await group.read_aloud.preset.callback(group.read_aloud, interaction)
    content, kwargs = interaction.followup.messages[-1]
    assert "適用中（自分）: 速度 150% / 音量 60%" in content
    assert "サーバー: 速度 125% / 音量 80%" in content
    assert "自分: 速度 150% / 音量 60%" in content
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].everyone is False

    interaction.guild.user.guild_permissions.manage_guild = True
    interaction.response = _Response()
    await group.read_aloud.server_preset.callback(
        group.read_aloud,
        interaction,
        140,
        70,
        False,
    )
    content = interaction.followup.messages[-1][0]
    assert "サーバー読み上げpreset: 速度 140% / 音量 70%" in content
    assert "速度 150%" not in content

    interaction.guild.user.guild_permissions.manage_guild = False
    interaction.response = _Response()
    await group.read_aloud.my_preset.callback(
        group.read_aloud,
        interaction,
        None,
        None,
        True,
    )
    assert preset_repository.calls[-1] == ("user-clear", 100, 10, 1)
    assert (100, 10) not in preset_repository.users
    assert "速度 140% / 音量 70%" in interaction.followup.messages[-1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("permission", "repository"))
async def test_preset_fresh_revoke_during_threaded_read_keeps_write_zero(
    change: str,
) -> None:
    interaction, group, _repository, guard, _source, _destination = _fixture()
    preset_repository = group.bot.music_read_aloud_preset_repository

    def revoke() -> None:
        if change == "permission":
            guard.allowed = False
        else:
            group.bot.music_read_aloud_preset_repository = object()

    preset_repository.after_read = revoke
    await group.read_aloud.server_preset.callback(
        group.read_aloud,
        interaction,
        125,
        80,
        False,
    )

    assert preset_repository.calls == []
    assert preset_repository.server == {}
    assert "変更できませんでした" in interaction.followup.messages[-1][0]


@pytest.mark.asyncio
async def test_user_preset_rechecks_after_inherited_value_resolution_before_write() -> None:
    interaction, group, _repository, guard, _source, _destination = _fixture()
    interaction.guild.user.guild_permissions.manage_guild = False
    preset_repository = group.bot.music_read_aloud_preset_repository
    reads = 0

    def revoke_after_resolve() -> None:
        nonlocal reads
        reads += 1
        if reads == 2:
            guard.allowed = False

    preset_repository.after_read = revoke_after_resolve
    await group.read_aloud.my_preset.callback(
        group.read_aloud,
        interaction,
        125,
        None,
        False,
    )

    assert reads == 2
    assert preset_repository.calls == []
    assert preset_repository.users == {}
    assert "変更できませんでした" in interaction.followup.messages[-1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_scope", (False, True))
async def test_preset_post_write_revoke_never_displays_success(user_scope: bool) -> None:
    interaction, group, _repository, guard, _source, _destination = _fixture()
    preset_repository = group.bot.music_read_aloud_preset_repository
    preset_repository.after_write = lambda: setattr(guard, "allowed", False)

    if user_scope:
        await group.read_aloud.my_preset.callback(
            group.read_aloud,
            interaction,
            110,
            90,
            False,
        )
    else:
        await group.read_aloud.server_preset.callback(
            group.read_aloud,
            interaction,
            110,
            90,
            False,
        )

    assert preset_repository.calls
    content = interaction.followup.messages[-1][0]
    assert content == "設定結果を表示できませんでした。現在の権限を確認してください。"
    assert "速度 110%" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("speed_percent", "volume_percent", "clear"),
    (
        (49, 100, False),
        (201, 100, False),
        (100, -1, False),
        (100, 201, False),
        (100, 100, True),
        (None, None, False),
    ),
)
async def test_preset_invalid_input_never_mutates(
    speed_percent: int | None,
    volume_percent: int | None,
    clear: bool,
) -> None:
    interaction, group, _repository, _guard, _source, _destination = _fixture()
    preset_repository = group.bot.music_read_aloud_preset_repository

    await group.read_aloud.server_preset.callback(
        group.read_aloud,
        interaction,
        speed_percent,
        volume_percent,
        clear,
    )

    assert preset_repository.calls == []
    assert preset_repository.server == {}
