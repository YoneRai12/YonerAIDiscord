from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import discord
import pytest
from discord import app_commands

from yonerai_discord.modules.audio_core import (
    LocalMediaLibrary,
    LoopMode,
    QueueSnapshot,
    RecentTrack,
    RecentTrackState,
)
from yonerai_discord.modules.music import MusicPlugin, setup
from yonerai_discord.modules.music.adapter import MusicGroup, youtube_search_url
from yonerai_discord.modules.music.models import (
    GuildAudioProjection,
    MusicActor,
    MusicAuthorizationError,
    MusicSessionError,
    MusicSpeechReceipt,
    MusicSpeechStatus,
    PersistedMusicTrackRef,
)
from yonerai_discord.modules.music.repository import MusicPlaylistRepository
from yonerai_discord.modules.music.service import (
    ListenerLifecycleAction,
    ListenerLifecycleDecision,
    MusicService,
)
from yonerai_discord.modules.voice.models import SpeechRequest, SynthesizedSpeech
from yonerai_discord.modules.voice.service import SpeechQueue
from yonerai_discord.control_plane import RbacLevel


class FakeTree:
    def __init__(self) -> None:
        self.added = []
        self.removed = []

    def add_command(self, command) -> None:
        self.added.append(command)

    def remove_command(self, name: str, *, type=None):
        self.removed.append((name, type))
        return self.added.pop() if self.added else None


class FakeFactory:
    def create(self, _track):
        raise AssertionError("playback is not part of plugin startup")

    def create_speech(self, _wav):
        raise AssertionError("speech is not part of plugin startup")


class SeekableFakeFactory(FakeFactory):
    def create_at(self, _track, _seconds):
        raise AssertionError("seek is not part of plugin startup")


class PartialSeekFactory:
    def create(self, _track):
        raise AssertionError("playback is not part of plugin startup")

    def create_at(self, _track, _seconds):
        raise AssertionError("seek is not part of plugin startup")


class FakeResponse:
    def __init__(self) -> None:
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs: Any) -> None:
        assert kwargs == {"ephemeral": True, "thinking": True}
        self.done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.messages.append((content, kwargs))


class FakeSpeechQueue:
    available = True

    def __init__(self) -> None:
        self.requests = []

    async def synthesize(self, request, *, current_policy=None):
        assert current_policy is not None and await current_policy() is not None
        self.requests.append(request)
        return SimpleNamespace(wav=b"RIFF-voicevox-wav")


class RecordingMusicService:
    available = True
    reason = "ready"

    def __init__(self) -> None:
        self.speech_calls = []
        self.speech_receipt_log: list[MusicSpeechReceipt] = []
        self.radio_calls: list[tuple[int, MusicActor, bool]] = []

    async def add_speech_wav(
        self,
        guild_id,
        actor,
        wav,
        *,
        commit_check=None,
        receipt_source_channel_id=None,
    ):
        assert commit_check is not None and await commit_check() is not None
        self.speech_calls.append((guild_id, actor, wav))
        receipt = MusicSpeechReceipt(
            guild_id=guild_id,
            source_channel_id=receipt_source_channel_id,
            requester_id=actor.user_id,
            voice_channel_id=actor.voice_channel_id,
            queue_position=1,
        )
        self.speech_receipt_log.append(receipt)
        return receipt

    async def set_local_radio(self, guild_id, actor, enabled, *, commit_check=None):
        assert commit_check is not None and await commit_check() is not None
        self.radio_calls.append((guild_id, actor, enabled))
        return enabled


class ToggleGuard:
    def __init__(self) -> None:
        self.allowed = True
        self.capability_ids: list[str] = []

    async def actor(self, _interaction):
        return SimpleNamespace(level=RbacLevel.EVERYONE)

    def currently_allowed(self, capability_id, **_kwargs) -> bool:
        self.capability_ids.append(capability_id)
        return self.allowed


class FreshGuild:
    id = 100
    voice_client = None

    def __init__(self, member: Any) -> None:
        self.member = member

    async def fetch_member(self, user_id: int) -> Any:
        if user_id != self.member.id:
            raise LookupError("member not found")
        return self.member


class RecordingCloseService:
    def __init__(self) -> None:
        self.close_calls = 0
        self.delete_projection_calls: list[bool] = []
        self.guild_calls: list[int] = []

    async def close(self, *, delete_projections: bool = False) -> None:
        self.close_calls += 1
        self.delete_projection_calls.append(delete_projections)

    async def close_guild(self, guild_id: int) -> bool:
        self.guild_calls.append(guild_id)
        return True


class ListenerVoiceClient:
    def __init__(self, channel: ListenerVoiceChannel) -> None:
        self.channel = channel
        self.disconnect_calls = 0

    async def disconnect(self, *, force: bool = False) -> None:
        assert force
        self.disconnect_calls += 1
        if self.channel.guild.voice_client is self:
            self.channel.guild.voice_client = None


class ListenerVoiceChannel:
    def __init__(self, channel_id: int = 500) -> None:
        self.id = channel_id
        self.guild: ListenerGuild
        self.members: list[Any] = []
        self.connect_calls: list[dict[str, Any]] = []
        self.permissions = SimpleNamespace(view_channel=True, connect=True, speak=True)
        self.connect_started: asyncio.Event | None = None
        self.connect_release: asyncio.Event | None = None
        self.connect_error: Exception | None = None

    def permissions_for(self, _member: Any) -> Any:
        return self.permissions

    async def connect(self, **kwargs: Any) -> ListenerVoiceClient:
        self.connect_calls.append(dict(kwargs))
        if self.connect_started is not None:
            self.connect_started.set()
        if self.connect_release is not None:
            await self.connect_release.wait()
        if self.connect_error is not None:
            raise self.connect_error
        voice = ListenerVoiceClient(self)
        self.guild.voice_client = voice
        return voice


class ListenerGuild:
    def __init__(self, channel: ListenerVoiceChannel, members: list[Any]) -> None:
        self.id = 100
        self.voice_client: ListenerVoiceClient | None = None
        self.channel = channel
        self.channel.guild = self
        self.members = {member.id: member for member in members}
        for member in members:
            member.guild = self

    def get_channel(self, channel_id: int) -> Any | None:
        return self.channel if channel_id == self.channel.id else None

    async def fetch_channel(self, channel_id: int) -> Any:
        if channel_id != self.channel.id:
            raise LookupError("channel not found")
        return self.channel

    async def fetch_member(self, user_id: int) -> Any:
        member = self.members.get(user_id)
        if member is None:
            raise LookupError("member not found")
        return member


class ListenerService:
    available = True
    reason = "ready"

    def __init__(
        self,
        *,
        active: bool,
        idle_timeout_seconds: int = 0,
        requester_id: int = 10,
        voice_free: bool = False,
    ) -> None:
        self.active = active
        self.voice_free = voice_free
        self.voice_channel_id = 500 if active else None
        self.suspended_channel_id = None if active or voice_free else 500
        self.session_identity = object()
        self.idle_timeout_seconds = idle_timeout_seconds
        self.requester_id = requester_id
        self.suspend_calls: list[tuple[int, int, object]] = []
        self.join_calls: list[tuple[int, Any, MusicActor, int]] = []
        self.close_calls = 0
        self.begin_close_calls = 0

    def session_channel_id(self, _guild_id: int) -> int | None:
        return self.voice_channel_id

    def listener_lifecycle_decision(
        self,
        guild_id: int,
        *,
        connected_voice_channel_id: int | None,
        human_listener_count: int,
        idle_elapsed_seconds: float,
        eligible_listener_voice_channel_id: int | None = None,
        eligible_listener_user_id: int | None = None,
        eligible_listener_can_manage: bool = False,
    ) -> ListenerLifecycleDecision:
        if self.active:
            action = ListenerLifecycleAction.KEEP_ACTIVE
            reason = "eligible_listener_present"
            if connected_voice_channel_id != self.voice_channel_id:
                action = ListenerLifecycleAction.PRESERVE_AND_DISCONNECT
                reason = "voice_connection_lost"
            elif human_listener_count == 0:
                action = (
                    ListenerLifecycleAction.PRESERVE_AND_DISCONNECT
                    if idle_elapsed_seconds >= self.idle_timeout_seconds
                    else ListenerLifecycleAction.WAIT_FOR_LISTENER
                )
                reason = "no_eligible_listeners"
            return ListenerLifecycleDecision(
                action,
                guild_id,
                self.voice_channel_id,
                self.idle_timeout_seconds,
                reason,
                self.session_identity,
            )
        eligible = human_listener_count > 0 and eligible_listener_user_id == self.requester_id
        if not self.voice_free:
            eligible = eligible and eligible_listener_voice_channel_id == self.suspended_channel_id
            eligible = eligible or (
                human_listener_count > 0
                and eligible_listener_voice_channel_id == self.suspended_channel_id
                and eligible_listener_can_manage
            )
        target_channel_id = eligible_listener_voice_channel_id if self.voice_free else self.suspended_channel_id
        return ListenerLifecycleDecision(
            (
                ListenerLifecycleAction.RECONNECT_ELIGIBLE
                if eligible
                else ListenerLifecycleAction.EXPLICIT_JOIN_REQUIRED
            ),
            guild_id,
            target_channel_id,
            self.idle_timeout_seconds,
            "eligible_listener_returned" if eligible else "explicit_join_required",
        )

    async def listener_lifecycle_decision_current(
        self,
        guild_id: int,
        **kwargs: Any,
    ) -> ListenerLifecycleDecision:
        return self.listener_lifecycle_decision(guild_id, **kwargs)

    async def suspend_voice_session(
        self,
        guild_id: int,
        *,
        expected_voice_channel_id: int,
        expected_session_identity: object,
    ) -> GuildAudioProjection:
        assert self.active
        assert expected_session_identity is self.session_identity
        self.suspend_calls.append((guild_id, expected_voice_channel_id, expected_session_identity))
        self.active = False
        self.voice_channel_id = None
        self.suspended_channel_id = expected_voice_channel_id
        return GuildAudioProjection(guild_id=guild_id, tracks=())

    async def join(
        self,
        guild_id: int,
        voice_client: Any,
        actor: MusicActor,
        *,
        voice_channel_id: int,
        commit_check: Any,
    ) -> None:
        assert await commit_check() == actor
        self.join_calls.append((guild_id, voice_client, actor, voice_channel_id))
        self.active = True
        self.voice_free = False
        self.voice_channel_id = voice_channel_id
        self.suspended_channel_id = None

    async def begin_close(self) -> None:
        self.begin_close_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


def _listener_runtime(
    *,
    active: bool,
    idle_timeout_seconds: int = 0,
    requester_id: int = 10,
    voice_free: bool = False,
) -> tuple[MusicPlugin, Any, ListenerService, ListenerGuild, ListenerVoiceChannel, Any, Any]:
    channel = ListenerVoiceChannel()
    user = SimpleNamespace(
        id=requester_id,
        bot=False,
        voice=SimpleNamespace(channel=channel),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    bot_member = SimpleNamespace(
        id=999,
        bot=True,
        voice=SimpleNamespace(channel=channel if active else None),
        guild_permissions=SimpleNamespace(administrator=True, manage_guild=True),
    )
    guild = ListenerGuild(channel, [user, bot_member])
    channel.members = [user, bot_member]
    guard = ToggleGuard()
    service = ListenerService(
        active=active,
        idle_timeout_seconds=idle_timeout_seconds,
        requester_id=requester_id,
        voice_free=voice_free,
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        capability_guard=guard,
        music_service=service,
        is_closing=False,
    )
    plugin = MusicPlugin()
    plugin.bot = bot
    plugin.service = service  # type: ignore[assignment]
    plugin._voice_lifecycle_enabled = True
    if active:
        guild.voice_client = ListenerVoiceClient(channel)
    return plugin, bot, service, guild, channel, user, bot_member


async def _wait_voice_tasks(plugin: MusicPlugin) -> None:
    for _ in range(20):
        tasks = tuple(plugin._voice_lifecycle_tasks.values())
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
    raise AssertionError("voice lifecycle task did not finish")


def test_music_group_exposes_required_commands_and_owner_playlist_group() -> None:
    bot = SimpleNamespace(speech_queue=None)
    group = MusicGroup(bot, MusicService.unavailable("disabled"))

    names = {command.name for command in group.commands}
    assert {
        "status",
        "join",
        "leave",
        "play",
        "import",
        "search",
        "now",
        "queue",
        "pause",
        "resume",
        "skip",
        "stop",
        "seek",
        "remove",
        "move",
        "clear-mine",
        "shuffle",
        "loop",
        "volume",
        "radio",
        "speak",
        "search-youtube",
        "playlist",
        "read-aloud",
    } == names
    playlist = next(command for command in group.commands if command.name == "playlist")
    assert {command.name for command in playlist.commands} == {"save", "list", "load", "delete"}
    read_aloud = next(command for command in group.commands if command.name == "read-aloud")
    assert {command.name for command in read_aloud.commands} == {
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
async def test_slash_play_without_voice_persists_waiting_queue_without_connecting() -> None:
    class VoiceFreePlayService:
        available = True
        reason = "ready"

        def __init__(self) -> None:
            self.calls: list[tuple[int, str, MusicActor]] = []

        def session_channel_id(self, _guild_id: int) -> None:
            return None

        async def play(
            self,
            guild_id: int,
            query: str,
            actor: MusicActor,
            *,
            commit_check: Any,
        ) -> tuple[Any, int]:
            assert await commit_check() == actor
            self.calls.append((guild_id, query, actor))
            return SimpleNamespace(title=query), 1

    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=None),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    guild = FreshGuild(member)
    service = VoiceFreePlayService()
    bot = SimpleNamespace(
        speech_queue=None,
        capability_guard=ToggleGuard(),
        is_closing=False,
        music_service=service,
    )
    group = MusicGroup(bot, service)  # type: ignore[arg-type]
    interaction = SimpleNamespace(
        guild_id=guild.id,
        guild=guild,
        channel_id=200,
        user=member,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )

    await group.play.callback(group, interaction, "owner-approved")

    assert service.calls == [(100, "owner-approved", MusicActor(10, None, False))]
    assert len(interaction.followup.messages) == 1
    assert "待機キュー 1 番" in interaction.followup.messages[0][0]
    assert "VCへ参加すると開始します" in interaction.followup.messages[0][0]
    assert "再起動後は /music join が必要です" in interaction.followup.messages[0][0]


@pytest.mark.asyncio
async def test_slash_voice_free_play_hides_success_after_post_commit_policy_revoke() -> None:
    class BlockingPlayService:
        available = True
        reason = "ready"

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        def session_channel_id(self, _guild_id: int) -> None:
            return None

        async def play(
            self,
            _guild_id: int,
            query: str,
            actor: MusicActor,
            *,
            commit_check: Any,
        ) -> tuple[Any, int]:
            assert await commit_check() == actor
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(title=query), 1

    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=None),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    guild = FreshGuild(member)
    guard = ToggleGuard()
    service = BlockingPlayService()
    bot = SimpleNamespace(
        speech_queue=None,
        capability_guard=guard,
        is_closing=False,
        music_service=service,
    )
    group = MusicGroup(bot, service)  # type: ignore[arg-type]
    interaction = SimpleNamespace(
        guild_id=guild.id,
        guild=guild,
        channel_id=200,
        user=member,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )
    task = asyncio.create_task(group.play.callback(group, interaction, "secret-title"))
    await asyncio.wait_for(service.started.wait(), timeout=1.0)
    replacement_guard = ToggleGuard()
    bot.capability_guard = replacement_guard
    service.release.set()
    await task

    assert service.calls == 1
    assert len(interaction.followup.messages) == 1
    assert "待機キュー" not in interaction.followup.messages[0][0]
    assert "secret-title" not in interaction.followup.messages[0][0]


@pytest.mark.asyncio
async def test_volume_keeps_music_default_and_supports_separate_tts_bus() -> None:
    class VolumeService:
        available = True
        reason = "ready"

        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        async def set_volume(self, *_args: Any, commit_check: Any = None) -> None:
            assert commit_check is not None and await commit_check() is not None
            self.calls.append(("music", float(_args[-1])))

        async def set_speech_volume(self, *_args: Any, commit_check: Any = None) -> None:
            assert commit_check is not None and await commit_check() is not None
            self.calls.append(("speech", float(_args[-1])))

    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=SimpleNamespace(id=500)),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    bot = SimpleNamespace(speech_queue=None, capability_guard=ToggleGuard(), is_closing=False)
    service = VolumeService()
    group = MusicGroup(bot, service)  # type: ignore[arg-type]

    for percent, bus in (
        (50, None),
        (40, app_commands.Choice(name="TTS", value="speech")),
    ):
        interaction = SimpleNamespace(
            guild_id=100,
            guild=FreshGuild(member),
            channel_id=200,
            user=member,
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
        await group.volume.callback(group, interaction, percent, bus)

    assert service.calls == [("music", 0.5), ("speech", 0.4)]


@pytest.mark.asyncio
async def test_seek_uses_exact_capability_and_fresh_commit_check() -> None:
    class SeekService:
        available = True
        reason = "ready"

        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int]] = []

        async def seek(self, guild_id, actor, seconds, *, commit_check=None):
            assert commit_check is not None and await commit_check() is not None
            self.calls.append((guild_id, actor.user_id, seconds))
            return SimpleNamespace(title="owner-approved")

    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=SimpleNamespace(id=500)),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    guard = ToggleGuard()
    bot = SimpleNamespace(speech_queue=None, capability_guard=guard, is_closing=False)
    service = SeekService()
    group = MusicGroup(bot, service)  # type: ignore[arg-type]
    interaction = SimpleNamespace(
        guild_id=100,
        guild=FreshGuild(member),
        channel_id=200,
        user=member,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )

    await group.seek.callback(group, interaction, 37)

    assert service.calls == [(100, 10, 37)]
    assert guard.capability_ids == ["cap-run-music-seek"]
    assert "37" in interaction.followup.messages[0][0]


def test_youtube_helper_only_builds_official_search_results_url() -> None:
    parsed = urlparse(youtube_search_url("lofi 日本語"))

    assert parsed.scheme == "https"
    assert parsed.netloc == "www.youtube.com"
    assert parsed.path == "/results"
    assert parse_qs(parsed.query) == {"search_query": ["lofi 日本語"]}
    with pytest.raises(ValueError):
        youtube_search_url("")
    with pytest.raises(ValueError):
        youtube_search_url("x" * 201)
    with pytest.raises(ValueError):
        youtube_search_url("line1\nline2")
    with pytest.raises(ValueError):
        youtube_search_url("nul\x00value")

    hostile_url = youtube_search_url("[live](x) <@123456>")
    assert "[" not in hostile_url and "]" not in hostile_url
    assert "<@123456>" not in hostile_url
    assert "%5Blive%5D%28x%29+%3C%40123456%3E" in hostile_url


def test_speak_exposes_only_code_owned_speaker_choice() -> None:
    group = MusicGroup(SimpleNamespace(), RecordingMusicService())
    parameter = next(parameter for parameter in group.speak.parameters if parameter.name == "speaker_id")

    assert [(choice.name, choice.value) for choice in parameter.choices] == [("3", 3)]


@pytest.mark.asyncio
async def test_speak_uses_existing_speech_queue_then_audio_session_ducking_path() -> None:
    speech_queue = FakeSpeechQueue()
    service = RecordingMusicService()
    bot = SimpleNamespace(
        speech_queue=speech_queue,
        capability_guard=ToggleGuard(),
        is_closing=False,
    )
    group = MusicGroup(bot, service)
    response = FakeResponse()
    followup = FakeFollowup()
    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=SimpleNamespace(id=500)),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    interaction = SimpleNamespace(
        guild_id=100,
        guild=FreshGuild(member),
        channel_id=200,
        user=member,
        response=response,
        followup=followup,
    )

    await group.speak.callback(group, interaction, "こんにちは", 3)

    assert len(speech_queue.requests) == 1
    request = speech_queue.requests[0]
    assert (request.text, request.guild_id, request.channel_id, request.speaker_id) == ("こんにちは", 100, 200, 3)
    assert len(service.speech_calls) == 1
    guild_id, actor, wav = service.speech_calls[0]
    assert guild_id == 100
    assert actor.user_id == 10
    assert actor.voice_channel_id == 500
    assert wav == b"RIFF-voicevox-wav"
    assert service.speech_receipt_log == [MusicSpeechReceipt(100, 200, 10, 500, 1, MusicSpeechStatus.QUEUED)]
    assert response.done
    content, kwargs = followup.messages[0]
    assert "ducking" in content
    assert "queued" in content
    assert "delivered" not in content and "completed" not in content
    assert kwargs["ephemeral"] is True
    allowed_mentions = kwargs["allowed_mentions"]
    assert isinstance(allowed_mentions, discord.AllowedMentions)
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
    assert allowed_mentions.replied_user is False


@pytest.mark.asyncio
async def test_speak_rejects_non_allowlisted_speaker_before_synthesis_or_receipt() -> None:
    speech_queue = FakeSpeechQueue()
    service = RecordingMusicService()
    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=SimpleNamespace(id=500)),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    interaction = SimpleNamespace(
        guild_id=100,
        guild=FreshGuild(member),
        channel_id=200,
        user=member,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )
    group = MusicGroup(
        SimpleNamespace(speech_queue=speech_queue, capability_guard=ToggleGuard(), is_closing=False),
        service,
    )

    await group.speak.callback(group, interaction, "private text", 7)

    assert speech_queue.requests == []
    assert service.speech_calls == []
    assert service.speech_receipt_log == []
    assert "private text" not in repr(interaction.followup.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "enabled"), (("on", True), ("off", False)))
async def test_radio_slash_uses_exact_capability_and_existing_music_service(
    mode: str,
    enabled: bool,
) -> None:
    guard = ToggleGuard()
    service = RecordingMusicService()
    bot = SimpleNamespace(capability_guard=guard, is_closing=False)
    group = MusicGroup(bot, service)  # type: ignore[arg-type]
    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=SimpleNamespace(id=500)),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    interaction = SimpleNamespace(
        guild_id=100,
        guild=FreshGuild(member),
        channel_id=200,
        user=member,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )

    await group.radio.callback(
        group,
        interaction,
        app_commands.Choice(name=mode, value=mode),
    )

    assert len(service.radio_calls) == 1
    guild_id, actor, actual_enabled = service.radio_calls[0]
    assert (guild_id, actor.user_id, actor.voice_channel_id, actual_enabled) == (100, 10, 500, enabled)
    assert guard.capability_ids == ["cap-run-music-radio"]
    assert interaction.followup.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_speak_policy_off_while_waiting_never_sends_text_to_synthesizer() -> None:
    class BlockingSynthesizer:
        def __init__(self) -> None:
            self.requests: list[SpeechRequest] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def synthesize(self, request: SpeechRequest) -> SynthesizedSpeech:
            self.requests.append(request)
            if request.text == "先行":
                self.first_started.set()
                await self.release_first.wait()
            return SynthesizedSpeech(wav=b"RIFF-voicevox-wav")

    class ObservableSpeechQueue(SpeechQueue):
        def __init__(self, synthesizer: BlockingSynthesizer) -> None:
            super().__init__(synthesizer, max_concurrency=1)
            self.blocked_run_started = asyncio.Event()

        async def _run(self, request, current_policy):
            if request.text == "外部へ送らない":
                self.blocked_run_started.set()
            return await super()._run(request, current_policy)

    provider = BlockingSynthesizer()
    speech_queue = ObservableSpeechQueue(provider)
    first = asyncio.create_task(speech_queue.synthesize(SpeechRequest(text="先行", guild_id=100, channel_id=200)))
    await provider.first_started.wait()

    guard = ToggleGuard()
    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=SimpleNamespace(id=500)),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    interaction = SimpleNamespace(
        guild_id=100,
        guild=FreshGuild(member),
        channel_id=200,
        user=member,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )
    service = RecordingMusicService()
    group = MusicGroup(
        SimpleNamespace(speech_queue=speech_queue, capability_guard=guard, is_closing=False),
        service,
    )
    queued = asyncio.create_task(group.speak.callback(group, interaction, "外部へ送らない", 3))
    await speech_queue.blocked_run_started.wait()
    guard.allowed = False
    provider.release_first.set()
    await first
    await queued

    assert [request.text for request in provider.requests] == ["先行"]
    assert service.speech_calls == []
    assert len(interaction.followup.messages) == 1
    await speech_queue.close()


@pytest.mark.asyncio
async def test_join_rolls_back_new_connection_when_policy_changes_during_connect() -> None:
    class VoiceClient:
        def __init__(self) -> None:
            self.disconnect_calls = 0

        async def disconnect(self, *, force: bool = False) -> None:
            assert force
            self.disconnect_calls += 1

    class BlockingChannel:
        id = 500

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.voice_client = VoiceClient()
            self.connect_kwargs: list[dict[str, Any]] = []

        async def connect(self, **kwargs):
            self.connect_kwargs.append(dict(kwargs))
            self.started.set()
            await self.release.wait()
            return self.voice_client

    class JoinService:
        available = True
        reason = "ready"

        def __init__(self) -> None:
            self.join_calls = 0

        def session_channel_id(self, _guild_id: int) -> None:
            return None

        async def join(self, *_args, **_kwargs) -> None:
            self.join_calls += 1

    guard = ToggleGuard()
    channel = BlockingChannel()
    service = JoinService()
    bot = SimpleNamespace(speech_queue=None, capability_guard=guard, is_closing=False)
    group = MusicGroup(bot, service)  # type: ignore[arg-type]
    response = FakeResponse()
    followup = FakeFollowup()
    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=channel),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    interaction = SimpleNamespace(
        guild_id=100,
        guild=FreshGuild(member),
        channel_id=200,
        user=member,
        response=response,
        followup=followup,
    )

    task = asyncio.create_task(group.join.callback(group, interaction))
    await asyncio.wait_for(channel.started.wait(), timeout=1.0)
    guard.allowed = False
    channel.release.set()
    await task

    assert service.join_calls == 0
    assert channel.voice_client.disconnect_calls == 1
    assert channel.connect_kwargs == [{"self_deaf": True, "reconnect": False}]
    assert "接続を取り消しました" in followup.messages[0][0]


@pytest.mark.asyncio
async def test_simple_control_rechecks_policy_after_defer_before_mutation() -> None:
    class ControlService:
        available = True
        reason = "ready"

        def __init__(self) -> None:
            self.pause_calls = 0

        async def pause(self, *_args: Any, commit_check: Any = None) -> None:
            if commit_check is None or await commit_check() is None:
                raise MusicAuthorizationError("capability policy changed")
            self.pause_calls += 1

    class ToggleResponse(FakeResponse):
        async def defer(self, **kwargs: Any) -> None:
            await super().defer(**kwargs)
            guard.allowed = False

    guard = ToggleGuard()
    service = ControlService()
    bot = SimpleNamespace(speech_queue=None, capability_guard=guard, is_closing=False)
    group = MusicGroup(bot, service)  # type: ignore[arg-type]
    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=SimpleNamespace(id=500)),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    interaction = SimpleNamespace(
        guild_id=100,
        guild=FreshGuild(member),
        channel_id=200,
        user=member,
        response=ToggleResponse(),
        followup=FakeFollowup(),
    )

    await group.pause.callback(group, interaction)

    assert service.pause_calls == 0
    assert interaction.followup.messages


@pytest.mark.asyncio
async def test_queue_renders_bounded_sanitized_recent_history() -> None:
    class QueueService:
        available = True
        reason = "ready"

        async def snapshot(self, _guild_id: int) -> QueueSnapshot:
            return QueueSnapshot(
                None,
                (),
                LoopMode.OFF,
                False,
                0.65,
                recent=(
                    RecentTrack("@everyone **recent**", 10, RecentTrackState.COMPLETED),
                    RecentTrack("skipped", 11, RecentTrackState.SKIPPED),
                ),
            )

    class MessageResponse(FakeResponse):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[tuple[str, dict[str, Any]]] = []

        async def send_message(self, content: str, **kwargs: Any) -> None:
            self.done = True
            self.messages.append((content, kwargs))

    response = MessageResponse()
    interaction = SimpleNamespace(
        guild_id=100,
        guild=SimpleNamespace(id=100),
        response=response,
        followup=FakeFollowup(),
    )
    group = MusicGroup(SimpleNamespace(speech_queue=None), QueueService())  # type: ignore[arg-type]

    await group.queue.callback(group, interaction)

    content, kwargs = response.messages[0]
    assert "最近の再生:" in content
    assert "@everyone" not in content
    assert "recent" in content and "完了" in content and "スキップ" in content
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


@pytest.mark.asyncio
async def test_plugin_keeps_status_group_when_music_is_disabled() -> None:
    tree = FakeTree()
    bot = SimpleNamespace(tree=tree, settings=SimpleNamespace(), speech_queue=None)
    plugin = MusicPlugin()

    await plugin.start(bot)
    assert len(tree.added) == 1
    assert tree.added[0].name == "music"
    assert bot.music_service is plugin.service
    assert not plugin.service.available
    assert plugin.service.reason == "disabled"
    assert bot.runtime_capability_readiness["cap-run-music-status"] is True
    assert bot.runtime_capability_readiness["cap-run-music-search-youtube"] is True
    assert bot.runtime_capability_readiness["cap-run-music-play"] is False
    assert bot.runtime_capability_readiness["cap-run-music-seek"] is False
    assert bot.runtime_capability_readiness["cap-run-music-playlist-save"] is False
    assert bot.runtime_capability_readiness["cap-run-music-speak"] is False
    assert bot.runtime_capability_readiness["cap-run-audio-ducking-core"] is False

    await plugin.stop()
    assert tree.removed and tree.removed[0][0] == "music"
    assert not hasattr(bot, "music_service")
    assert bot.runtime_capability_readiness == {"cap-run-audio-ducking-core": False}


@pytest.mark.asyncio
async def test_plugin_initialization_failure_still_leaves_safe_status(tmp_path: Path) -> None:
    class BrokenLibrary:
        def refresh(self) -> int:
            raise OSError("offline initialization failure")

    tree = FakeTree()
    bot = SimpleNamespace(
        tree=tree,
        settings=SimpleNamespace(
            music_enabled=True,
            music_library_roots=(tmp_path,),
            music_database_path=tmp_path / "music.sqlite3",
        ),
        music_library=BrokenLibrary(),
        music_source_factory=FakeFactory(),
        speech_queue=None,
    )
    plugin = MusicPlugin()

    await plugin.start(bot)
    assert tree.added[0].name == "music"
    assert not plugin.service.available
    assert plugin.service.reason == "initialization-error:OSError"
    assert tree.added[0].service is plugin.service
    assert bot.music_service is plugin.service
    assert bot.runtime_capability_readiness["cap-run-music-status"] is True
    assert bot.runtime_capability_readiness["cap-run-music-join"] is False
    assert bot.runtime_capability_readiness["cap-run-music-seek"] is False
    assert bot.runtime_capability_readiness["cap-run-audio-ducking-core"] is False

    await plugin.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory_type", "seek_ready"),
    ((FakeFactory, False), (PartialSeekFactory, False), (SeekableFakeFactory, True)),
)
async def test_plugin_starts_with_injected_offline_source_factory(
    tmp_path: Path,
    factory_type: type[Any],
    seek_ready: bool,
) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()
    (library_root / "owner-approved.mp3").write_bytes(b"local audio")
    tree = FakeTree()
    bot = SimpleNamespace(
        tree=tree,
        settings=SimpleNamespace(
            music_enabled=True,
            music_library_roots=(library_root,),
            music_database_path=tmp_path / "music.sqlite3",
        ),
        music_source_factory=factory_type(),
        speech_queue=SimpleNamespace(available=True),
    )
    plugin = MusicPlugin()

    await plugin.start(bot)
    assert plugin.service.available
    assert plugin.service.reason == "ready"
    assert plugin.service.indexed_tracks == 1
    assert plugin.repository is not None
    assert (tmp_path / "music.sqlite3").is_file()
    readiness = bot.runtime_capability_readiness
    assert readiness["cap-run-music-status"] is True
    assert readiness["cap-run-music-search-youtube"] is True
    assert readiness["cap-run-music-play"] is True
    assert readiness["cap-run-music-seek"] is seek_ready
    assert readiness["cap-run-music-playlist-load"] is True
    assert readiness["cap-run-music-speak"] is True
    assert readiness["cap-run-audio-ducking-core"] is True
    assert plugin.dashboard_controller is not None
    assert plugin.group is not None
    assert plugin.group.dashboard_controller is plugin.dashboard_controller
    dashboard_controller = plugin.dashboard_controller

    await plugin.stop()
    assert plugin.dashboard_controller is None
    assert dashboard_controller._closing is True


@pytest.mark.asyncio
async def test_plugin_rejects_queue_limit_above_durable_projection_bound(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()
    (library_root / "owner-approved.mp3").write_bytes(b"local audio")
    bot = SimpleNamespace(
        tree=FakeTree(),
        settings=SimpleNamespace(
            music_enabled=True,
            music_library_roots=(library_root,),
            music_database_path=tmp_path / "music.sqlite3",
            music_max_queue=101,
        ),
        music_source_factory=FakeFactory(),
        speech_queue=None,
    )
    plugin = MusicPlugin()

    await plugin.start(bot)

    assert not plugin.service.available
    assert plugin.service.reason == "queue-limit-exceeds-durable-projection"
    assert plugin.repository is None
    assert not (tmp_path / "music.sqlite3").exists()
    await plugin.stop()


@pytest.mark.asyncio
async def test_plugin_stop_cleans_runtime_when_final_projection_save_fails() -> None:
    class FailingCloseService:
        available = True
        reason = "ready"

        async def close(self) -> None:
            raise MusicSessionError("audio projections could not be saved")

    class RecordingRepository:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    tree = FakeTree()
    service = FailingCloseService()
    repository = RecordingRepository()
    bot = SimpleNamespace(
        tree=tree,
        music_service=service,
        runtime_capability_readiness={},
    )
    plugin = MusicPlugin()
    plugin.bot = bot
    plugin.service = service  # type: ignore[assignment]
    plugin.repository = repository  # type: ignore[assignment]
    plugin._command_registered = True

    with pytest.raises(MusicSessionError, match="could not be saved"):
        await plugin.stop()

    assert repository.closed
    assert not hasattr(bot, "music_service")
    assert tree.removed == [("music", discord.AppCommandType.chat_input)]
    assert plugin.bot is None
    assert plugin.repository is None
    assert not plugin.service.available
    assert plugin.service.reason == "stopped"


@pytest.mark.asyncio
async def test_plugin_start_loads_pending_projection_without_resolving_or_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()
    (library_root / "owner-approved.mp3").write_bytes(b"local audio")
    library = LocalMediaLibrary((library_root,))
    assert library.refresh() == 1
    sealed = library.seal_track(library.resolve_track("owner-approved", requester_id=10))
    assert sealed.library_ref is not None and sealed.content_sha256 is not None
    database_path = tmp_path / "music.sqlite3"
    repository = MusicPlaylistRepository(database_path)
    repository.open()
    repository.grant_track_rights(100, sealed.library_ref, sealed.content_sha256)
    repository.save_audio_projection(
        GuildAudioProjection(
            guild_id=100,
            tracks=(
                PersistedMusicTrackRef(
                    library_ref=sealed.library_ref,
                    content_sha256=sealed.content_sha256,
                    requester_id=10,
                ),
            ),
            paused=True,
        )
    )
    repository.close()

    def forbidden_resolve(*_args, **_kwargs):
        raise AssertionError("startup must not resolve pending media")

    monkeypatch.setattr(library, "resolve_persisted_track", forbidden_resolve)
    bot = SimpleNamespace(
        tree=FakeTree(),
        settings=SimpleNamespace(
            music_enabled=True,
            music_library_roots=(library_root,),
            music_database_path=database_path,
        ),
        music_library=library,
        music_source_factory=FakeFactory(),
        speech_queue=None,
    )
    plugin = MusicPlugin()

    await plugin.start(bot)
    assert plugin.service.available
    assert plugin.service.session_channel_id(100) is None
    assert tuple(plugin.service._pending_projections) == (100,)

    await plugin.stop()


@pytest.mark.asyncio
async def test_on_ready_refreshes_speak_readiness_after_voice_plugin_start(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()
    (library_root / "owner-approved.mp3").write_bytes(b"local audio")
    listeners = {}

    def add_listener(callback, name):
        listeners[name] = callback

    def remove_listener(callback, name):
        assert listeners.get(name) == callback
        listeners.pop(name)

    bot = SimpleNamespace(
        tree=FakeTree(),
        settings=SimpleNamespace(
            music_enabled=True,
            music_library_roots=(library_root,),
            music_database_path=tmp_path / "music.sqlite3",
        ),
        music_source_factory=FakeFactory(),
        speech_queue=None,
        add_listener=add_listener,
        remove_listener=remove_listener,
    )
    plugin = MusicPlugin()

    await plugin.start(bot)
    assert set(listeners) == {"on_ready", "on_voice_state_update"}
    assert bot.runtime_capability_readiness["cap-run-music-speak"] is False
    bot.speech_queue = SimpleNamespace(available=True)
    await listeners["on_ready"]()
    assert bot.runtime_capability_readiness["cap-run-music-speak"] is True

    await plugin.stop()
    assert listeners == {}


@pytest.mark.asyncio
async def test_listener_idle_timeout_preserves_queue_binding_without_cross_guild_action() -> None:
    plugin, _bot, service, guild, channel, user, bot_member = _listener_runtime(active=True)
    channel.members = [bot_member]
    user.voice.channel = None

    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=channel),
        SimpleNamespace(channel=None),
    )
    await _wait_voice_tasks(plugin)

    assert len(service.suspend_calls) == 1
    assert service.suspend_calls[0][0:2] == (100, 500)
    assert service.voice_channel_id is None
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_listener_return_before_idle_timeout_cancels_stale_disconnect() -> None:
    plugin, _bot, service, guild, channel, user, bot_member = _listener_runtime(
        active=True,
        idle_timeout_seconds=60,
    )
    channel.members = [bot_member]
    user.voice.channel = None
    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=channel),
        SimpleNamespace(channel=None),
    )
    assert len(plugin._voice_lifecycle_tasks) == 1

    user.voice.channel = channel
    channel.members = [user, bot_member]
    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=channel),
    )

    assert service.suspend_calls == []
    assert plugin._voice_lifecycle_tasks == {}
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_unrelated_voice_event_does_not_cancel_active_channel_idle_timer() -> None:
    plugin, _bot, _service, guild, channel, user, bot_member = _listener_runtime(
        active=True,
        idle_timeout_seconds=60,
    )
    channel.members = [bot_member]
    user.voice.channel = None
    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=channel),
        SimpleNamespace(channel=None),
    )
    pending = plugin._voice_lifecycle_tasks[100]
    unrelated = SimpleNamespace(id=501, guild=guild, members=[user])

    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=unrelated),
        SimpleNamespace(channel=None),
    )
    await plugin._on_voice_state_update(
        bot_member,
        SimpleNamespace(channel=channel),
        SimpleNamespace(channel=channel),
    )
    other_bot = SimpleNamespace(id=998, bot=True, guild=guild)
    await plugin._on_voice_state_update(
        other_bot,
        SimpleNamespace(channel=channel),
        SimpleNamespace(channel=None),
    )

    assert plugin._voice_lifecycle_tasks[100] is pending
    assert not pending.done()
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_unrelated_pending_listener_does_not_cancel_existing_reconnect_task() -> None:
    plugin, _bot, service, guild, _channel, _user, _bot_member = _listener_runtime(active=False)
    keep_running = asyncio.Event()
    pending = asyncio.create_task(keep_running.wait())
    plugin._voice_lifecycle_tasks[100] = pending  # type: ignore[assignment]
    unrelated_channel = ListenerVoiceChannel(501)
    unrelated_channel.guild = guild
    unrelated = SimpleNamespace(
        id=20,
        bot=False,
        guild=guild,
        voice=SimpleNamespace(channel=unrelated_channel),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
    )
    guild.members[20] = unrelated

    await plugin._on_voice_state_update(
        unrelated,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=unrelated_channel),
    )

    assert plugin._voice_lifecycle_tasks[100] is pending
    assert not pending.done()
    assert service.join_calls == []
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_listener_requester_return_reconnects_with_fresh_policy_and_no_internal_retry() -> None:
    plugin, bot, service, guild, channel, user, _bot_member = _listener_runtime(active=False)

    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=channel),
    )
    await _wait_voice_tasks(plugin)

    assert channel.connect_calls == [{"self_deaf": True, "reconnect": False}]
    assert len(service.join_calls) == 1
    assert guild.voice_client is service.join_calls[0][1]
    assert service.join_calls[0][2] == MusicActor(10, 500, False)
    assert {
        "cap-run-music-play",
        "cap-run-music-join",
        "cap-run-audio-ducking-core",
    }.issubset(set(bot.capability_guard.capability_ids))
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_voice_free_requester_join_starts_pending_queue_but_restart_pending_stays_explicit() -> None:
    plugin, _bot, service, _guild, channel, user, _bot_member = _listener_runtime(
        active=False,
        voice_free=True,
    )

    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=channel),
    )
    await _wait_voice_tasks(plugin)

    assert channel.connect_calls == [{"self_deaf": True, "reconnect": False}]
    assert len(service.join_calls) == 1
    await plugin._disable_voice_lifecycle(plugin.bot)

    restart_plugin, _restart_bot, restart_service, _guild, restart_channel, restart_user, _ = _listener_runtime(
        active=False
    )
    restart_service.suspended_channel_id = None
    await restart_plugin._on_voice_state_update(
        restart_user,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=restart_channel),
    )
    await _wait_voice_tasks(restart_plugin)

    assert restart_channel.connect_calls == []
    assert restart_service.join_calls == []
    await restart_plugin._disable_voice_lifecycle(restart_plugin.bot)


@pytest.mark.asyncio
async def test_listener_reconnect_rolls_back_exact_client_after_runtime_swap() -> None:
    plugin, _bot, service, guild, channel, user, _bot_member = _listener_runtime(active=False)
    channel.connect_started = asyncio.Event()
    channel.connect_release = asyncio.Event()

    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=channel),
    )
    await asyncio.wait_for(channel.connect_started.wait(), timeout=1.0)
    replacement = ListenerService(active=False)
    plugin.service = replacement  # type: ignore[assignment]
    channel.connect_release.set()
    await _wait_voice_tasks(plugin)

    assert service.join_calls == []
    assert guild.voice_client is None
    assert channel.connect_calls == [{"self_deaf": True, "reconnect": False}]
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_listener_reconnect_rolls_back_when_capability_is_revoked_during_connect() -> None:
    plugin, bot, service, guild, channel, user, _bot_member = _listener_runtime(active=False)
    channel.connect_started = asyncio.Event()
    channel.connect_release = asyncio.Event()

    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=channel),
    )
    await asyncio.wait_for(channel.connect_started.wait(), timeout=1.0)
    bot.capability_guard.allowed = False
    channel.connect_release.set()
    await _wait_voice_tasks(plugin)

    assert service.join_calls == []
    assert guild.voice_client is None
    assert channel.connect_calls == [{"self_deaf": True, "reconnect": False}]
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_listener_reconnect_rejects_capability_guard_replacement_during_connect() -> None:
    plugin, bot, service, guild, channel, user, _bot_member = _listener_runtime(active=False)
    channel.connect_started = asyncio.Event()
    channel.connect_release = asyncio.Event()

    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=channel),
    )
    await asyncio.wait_for(channel.connect_started.wait(), timeout=1.0)
    replacement_guard = ToggleGuard()
    replacement_guard.allowed = False
    bot.capability_guard = replacement_guard
    channel.connect_release.set()
    await _wait_voice_tasks(plugin)

    assert service.join_calls == []
    assert guild.voice_client is None
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_listener_lifecycle_disabled_before_event_prevents_new_task_or_connect() -> None:
    plugin, _bot, service, _guild, channel, user, _bot_member = _listener_runtime(active=False)
    plugin._voice_lifecycle_enabled = False

    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=channel),
    )

    assert plugin._voice_lifecycle_tasks == {}
    assert channel.connect_calls == []
    assert service.join_calls == []


@pytest.mark.asyncio
async def test_listener_disable_barrier_prevents_inflight_callback_from_creating_task() -> None:
    plugin, _bot, service, _guild, channel, user, _bot_member = _listener_runtime(active=False)
    cancel_started = asyncio.Event()
    cancel_release = asyncio.Event()

    async def blocked_cancel(_guild_id: int) -> None:
        cancel_started.set()
        await cancel_release.wait()

    plugin._cancel_voice_lifecycle_task = blocked_cancel  # type: ignore[method-assign]
    callback = asyncio.create_task(
        plugin._on_voice_state_update(
            user,
            SimpleNamespace(channel=None),
            SimpleNamespace(channel=channel),
        )
    )
    await asyncio.wait_for(cancel_started.wait(), timeout=1.0)
    await plugin._disable_voice_lifecycle(plugin.bot)
    cancel_release.set()
    await callback

    assert plugin._voice_lifecycle_tasks == {}
    assert channel.connect_calls == []
    assert service.join_calls == []


@pytest.mark.asyncio
async def test_listener_connect_exception_is_collected_without_joining(caplog: pytest.LogCaptureFixture) -> None:
    plugin, _bot, service, _guild, channel, user, _bot_member = _listener_runtime(active=False)
    channel.connect_error = OSError("simulated Discord transport failure")

    await plugin._on_voice_state_update(
        user,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=channel),
    )
    await _wait_voice_tasks(plugin)

    assert service.join_calls == []
    assert "simulated Discord transport failure" not in caplog.text
    assert "music_listener_reconnect_failed" in caplog.text
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_bot_voice_disconnect_preserves_exact_active_session_immediately() -> None:
    plugin, bot, service, guild, channel, _user, bot_member = _listener_runtime(active=True)
    bot_member.voice.channel = None
    guild.voice_client = None

    await plugin._on_voice_state_update(
        bot_member,
        SimpleNamespace(channel=channel),
        SimpleNamespace(channel=None),
    )
    await _wait_voice_tasks(plugin)

    assert len(service.suspend_calls) == 1
    assert service.suspend_calls[0][2] is service.session_identity
    assert bot.music_service is service
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_bot_return_to_bound_channel_reestablishes_no_human_idle_timer() -> None:
    plugin, _bot, service, guild, channel, _user, bot_member = _listener_runtime(
        active=True,
        idle_timeout_seconds=60,
    )
    channel.members = [bot_member]
    guild.voice_client = None
    bot_member.voice.channel = None
    disconnect_started = asyncio.Event()

    original_preserve = plugin._preserve_voice_session

    async def observable_preserve(*args: Any, delay_seconds: int, **kwargs: Any) -> None:
        if delay_seconds == 0:
            disconnect_started.set()
            await asyncio.Event().wait()
        await original_preserve(*args, delay_seconds=delay_seconds, **kwargs)

    plugin._preserve_voice_session = observable_preserve  # type: ignore[method-assign]
    await plugin._on_voice_state_update(
        bot_member,
        SimpleNamespace(channel=channel),
        SimpleNamespace(channel=None),
    )
    await asyncio.wait_for(disconnect_started.wait(), timeout=1.0)

    guild.voice_client = ListenerVoiceClient(channel)
    bot_member.voice.channel = channel
    await plugin._on_voice_state_update(
        bot_member,
        SimpleNamespace(channel=None),
        SimpleNamespace(channel=channel),
    )

    pending = plugin._voice_lifecycle_tasks[100]
    assert not pending.done()
    assert service.suspend_calls == []
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_idle_preserve_requires_known_channel_membership() -> None:
    plugin, bot, service, guild, channel, _user, _bot_member = _listener_runtime(active=True)
    channel.members = None  # type: ignore[assignment]

    await plugin._preserve_voice_session(
        bot,
        service,
        guild,
        500,
        service.session_identity,
        delay_seconds=0,
    )

    assert service.suspend_calls == []
    await plugin._disable_voice_lifecycle(plugin.bot)


@pytest.mark.asyncio
async def test_policy_off_immediately_closes_long_lived_audio_sessions() -> None:
    plugin = MusicPlugin()
    service = RecordingCloseService()
    plugin.service = service  # type: ignore[assignment]

    await plugin.on_capability_policy_changed("cap-run-music-play", False, 123)
    await plugin.on_capability_policy_changed("cap-run-music-status", False, 123)
    await plugin.on_capability_policy_changed("cap-run-music-speak", True, 123)
    await plugin.on_capability_policy_changed("cap-run-audio-ducking-core", False, None)
    await plugin.on_module_policy_changed("media.music", False, 456)

    assert service.guild_calls == [123, 456]
    assert service.close_calls == 1
    assert service.delete_projection_calls == [True]


def test_setup_registers_music_plugin() -> None:
    calls = []
    setup(SimpleNamespace(register=lambda name, factory: calls.append((name, factory))))

    assert calls == [("music", MusicPlugin)]
