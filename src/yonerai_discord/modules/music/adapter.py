from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from yonerai_discord.discord_markdown import numbered_link
from yonerai_discord.modules.audio_core import LoopMode, RecentTrackState
from yonerai_discord.modules.scheduling.focus import (
    FocusTimerBinding,
    FocusTimerCancelReceipt,
    FocusTimerRequest,
    FocusTimerScheduleReceipt,
)
from yonerai_discord.modules.voice.models import SpeechRequest
from yonerai_discord.modules.voice.presets import (
    DEFAULT_VOICE_PRESET,
    ResolvedVoicePreset,
    VoicePresetRecord,
    VoicePresetRepositoryError,
    VoicePresetScope,
    VoicePresetValues,
)
from yonerai_discord.modules.voice.read_aloud import (
    ReadAloudPolicySnapshot,
    ReadAloudRepositoryError,
    ReadAloudRoute,
)
from yonerai_discord.modules.voice.service import SpeechUnavailableError

from .authorization import MusicFreshCheck, build_music_commit_check
from .links import youtube_search_url
from .models import (
    MusicActor,
    MusicAuthorizationError,
    MusicError,
    MusicSeekUnsupportedError,
    MusicSessionError,
    MusicUnavailableError,
    PlaylistError,
)
from .service import MusicService, playlist_titles, queue_titles

if TYPE_CHECKING:
    from .dashboard import MusicDashboardController


NO_MENTIONS = discord.AllowedMentions.none()


class _ReadAloudPostWriteVisibilityError(RuntimeError):
    pass


class MusicGroup(app_commands.Group):
    def __init__(self, bot: Any, service: MusicService) -> None:
        super().__init__(name="music", description="許可済みローカル音源をVCで再生します")
        self.bot = bot
        self.service = service
        self.dashboard_controller: MusicDashboardController | None = None
        self.playlists = PlaylistGroup(bot, service)
        self.read_aloud = ReadAloudGroup(bot, self)
        self.add_command(self.playlists)
        self.add_command(self.read_aloud)

    def bind_service(self, service: MusicService) -> None:
        self.service = service
        self.playlists.service = service

    def bind_dashboard(self, controller: MusicDashboardController | None) -> None:
        self.dashboard_controller = controller

    @app_commands.command(name="status", description="音楽・TTS・ローカルlibraryの利用状態を表示します")
    async def status(self, interaction: discord.Interaction) -> None:
        speech_queue = getattr(self.bot, "speech_queue", None)
        status = self.service.status(speech_available=bool(getattr(speech_queue, "available", False)))
        state = "利用可能" if status.available else f"利用不可（{_reason(status.reason)}）"
        content = (
            f"音楽: {state}\n"
            f"許可済みローカル曲: {status.indexed_tracks}\n"
            f"接続中サーバー: {status.active_sessions}\n"
            f"リアルタイムTTS ducking: {'利用可能' if status.speech_available else '利用不可'}\n"
            "YouTube音声の抽出・ダウンロード・再配信: 非対応"
        )
        await _respond(interaction, content)

    @app_commands.command(name="join", description="自分が参加中のVCへ音楽playerを接続します")
    async def join(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        voice_client = None
        connected_here = False
        try:
            if not self.service.available:
                raise MusicUnavailableError(self.service.reason)
            guild_id, guild = _guild(interaction)
            actor = _actor(interaction)
            channel = _voice_channel(interaction)
            existing_channel = self.service.session_channel_id(guild_id)
            if existing_channel is None:
                if getattr(guild, "voice_client", None) is not None:
                    raise MusicSessionError("another voice feature owns the guild connection")
                voice_client = await channel.connect(self_deaf=True, reconnect=False)
                connected_here = True
            else:
                voice_client = getattr(guild, "voice_client", None)
                if voice_client is None:
                    raise MusicSessionError("music voice connection is unavailable")
            commit_check = await _capability_commit_check(self.bot, interaction, "music join")
            if commit_check is None or await commit_check() is None:
                if connected_here and voice_client is not None:
                    with suppress(Exception):
                        await voice_client.disconnect(force=True)
                await _respond(
                    interaction, "接続待機中に権限または音楽機能の設定が変更されたため、接続を取り消しました。"
                )
                return
            await self.service.join(
                guild_id,
                voice_client,
                actor,
                voice_channel_id=int(channel.id),
                commit_check=commit_check,
            )
        except MusicError as exc:
            if connected_here and voice_client is not None:
                with suppress(Exception):
                    await voice_client.disconnect(force=True)
            await _error(interaction, exc)
            return
        except Exception:
            if connected_here and voice_client is not None:
                with suppress(Exception):
                    await voice_client.disconnect(force=True)
            await _respond(interaction, "接続できませんでした。設定とBot権限を確認してください。")
            return
        await _respond(interaction, "同じVCへ接続しました。ローカルlibraryの曲だけ再生できます。")

    @app_commands.command(name="leave", description="音楽を終了してVCから退出します")
    async def leave(self, interaction: discord.Interaction) -> None:
        await self._simple_control(interaction, "leave")

    @app_commands.command(name="play", description="許可済みローカルlibraryから曲名で再生します")
    @app_commands.describe(query="曲名。URLやfile pathは受け付けません")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await _defer(interaction)
        try:
            guild_id, guild = _guild(interaction)
            actor = _actor(interaction)
            base_check = await _capability_commit_check(self.bot, interaction, "music play")
            if base_check is None:
                raise MusicAuthorizationError("capability policy changed")

            async def commit_check() -> MusicActor | None:
                if (
                    getattr(self.bot, "music_service", None) is not self.service
                    or not self.service.available
                    or bool(getattr(self.bot, "is_closing", False))
                ):
                    return None
                fresh_actor = await base_check()
                if (
                    getattr(self.bot, "music_service", None) is not self.service
                    or not self.service.available
                    or bool(getattr(self.bot, "is_closing", False))
                ):
                    return None
                return fresh_actor

            queued_without_voice = actor.voice_channel_id is None and self.service.session_channel_id(guild_id) is None
            if queued_without_voice and getattr(guild, "voice_client", None) is not None:
                raise MusicSessionError("another voice feature owns the guild connection")
            track, position = await self.service.play(
                guild_id,
                query,
                actor,
                commit_check=commit_check,
            )
            if await commit_check() is None:
                raise MusicAuthorizationError("capability policy changed")
        except MusicError as exc:
            await _error(interaction, exc)
            return
        except Exception:
            await _respond(interaction, "曲を開始できませんでした。")
            return
        if queued_without_voice:
            await _respond(
                interaction,
                f"ローカル曲「{_safe(track.title)}」を待機キュー {position} 番へ追加しました。"
                "VCへ参加すると開始します。再起動後は /music join が必要です。",
            )
            return
        await _respond(interaction, f"ローカル曲「{_safe(track.title)}」をqueue {position}番へ追加しました。")

    @app_commands.command(name="search", description="許可済みローカルlibrary内を曲名で検索します")
    @app_commands.describe(query="検索語")
    async def search_command(self, interaction: discord.Interaction, query: str) -> None:
        await self.search(interaction, query)

    @app_commands.command(name="import", description="権利確認済みPCM WAV添付をprivate local libraryへ取り込みます")
    @app_commands.describe(
        audio="PCM16 WAV（8MiB以下・1〜30秒）",
        title="ローカルlibraryで使う曲名",
        rights_confirmed="この音声を保存・再生する権利を確認済みの場合だけ有効にします",
    )
    async def import_audio(
        self,
        interaction: discord.Interaction,
        audio: discord.Attachment,
        title: str,
        rights_confirmed: bool,
    ) -> None:
        await _defer(interaction)
        try:
            service = self.service

            def runtime_current() -> bool:
                return (
                    self.service is service
                    and getattr(self.bot, "music_service", None) is service
                    and getattr(self.bot, "is_closing", False) is not True
                    and getattr(service, "available", False) is True
                )

            guild_id, _guild_value = _guild(interaction)
            actor = _actor(interaction)
            if rights_confirmed is not True or not actor.manage_guild:
                raise MusicAuthorizationError("confirmed rights and Manage Guild are required")
            size = getattr(audio, "size", None)
            filename = getattr(audio, "filename", None)
            if (
                getattr(audio, "content_type", None) != "audio/wav"
                or isinstance(size, bool)
                or not isinstance(size, int)
                or not 44 <= size <= 8 * 1024 * 1024
                or not isinstance(filename, str)
                or not filename.casefold().endswith(".wav")
            ):
                raise MusicSessionError("attachment metadata is invalid")
            base_commit_check = await _capability_commit_check(self.bot, interaction, "music import")
            if base_commit_check is None:
                raise MusicAuthorizationError("capability policy changed")

            async def commit_check() -> MusicActor | None:
                if not runtime_current():
                    return None
                result = await base_commit_check()
                if not runtime_current():
                    return None
                return result

            if await commit_check() is None:
                raise MusicAuthorizationError("capability policy changed")
            read = getattr(audio, "read", None)
            if not callable(read):
                raise MusicSessionError("attachment download is unavailable")
            data = await read()
            if await commit_check() is None:
                raise MusicAuthorizationError("capability policy changed")
            if not isinstance(data, bytes) or len(data) != size:
                raise MusicSessionError("attachment size changed")
            if not runtime_current():
                raise MusicUnavailableError("music import runtime identity changed")
            asset = await service.import_wav(
                guild_id,
                data,
                title,
                actor,
                commit_check=commit_check,
                runtime_current=runtime_current,
            )
            if await commit_check() is None:
                raise MusicAuthorizationError("capability policy changed")
        except asyncio.CancelledError:
            raise
        except MusicError as exc:
            await _error(interaction, exc)
            return
        except Exception:
            await _respond(interaction, "音声を安全に取り込めませんでした。形式と現在の権限を確認してください。")
            return
        await _respond(
            interaction,
            f"権利確認済みWAVを「{_safe(asset.display_title)}」として取り込みました。"
            "再生は `/music play` を使ってください。",
        )

    async def search(self, interaction: discord.Interaction, query: str) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            tracks = await self.service.search(query, _actor(interaction), guild_id=guild_id, limit=10)
        except MusicError as exc:
            await _error(interaction, exc)
            return
        if not tracks:
            await _respond(interaction, "一致するローカル曲はありません。")
            return
        lines = [f"{index}. {_safe(track.title)}" for index, track in enumerate(tracks, start=1)]
        await _respond(interaction, "許可済みローカル曲:\n" + "\n".join(lines))

    @app_commands.command(name="now", description="現在の再生曲を表示します")
    async def now(self, interaction: discord.Interaction) -> None:
        try:
            guild_id, _ = _guild(interaction)
            snapshot = await self.service.snapshot(guild_id)
        except MusicError as exc:
            await _error(interaction, exc)
            return
        if snapshot.current is None:
            await _respond(interaction, "現在再生中の曲はありません。")
            return
        paused = "・一時停止中" if snapshot.paused else ""
        await _respond(interaction, f"再生中: {_safe(snapshot.current.title)}{paused}")

    @app_commands.command(name="queue", description="現在の再生queueを表示します")
    @app_commands.describe(dashboard="公開チャンネルへ永続操作パネルを作成します")
    async def queue(self, interaction: discord.Interaction, dashboard: bool = False) -> None:
        if dashboard:
            controller = self.dashboard_controller
            if controller is None:
                await _respond(interaction, "永続音楽ダッシュボードは現在利用できません。")
                return
            try:
                await controller.create(interaction)
            except asyncio.CancelledError:
                raise
            except MusicError as exc:
                await _error(interaction, exc)
            except Exception:
                await _respond(interaction, "永続音楽ダッシュボードを作成できませんでした。")
            return
        try:
            guild_id, _ = _guild(interaction)
            snapshot = await self.service.snapshot(guild_id)
        except MusicError as exc:
            await _error(interaction, exc)
            return
        upcoming = queue_titles(snapshot)
        lines = [f"再生中: {_safe(snapshot.current.title)}"] if snapshot.current else ["再生中: なし"]
        lines.extend(f"{index}. {_safe(title)}" for index, title in enumerate(upcoming, start=1))
        if len(snapshot.upcoming) > len(upcoming):
            lines.append(f"ほか {len(snapshot.upcoming) - len(upcoming)}曲")
        if snapshot.recent:
            labels = {
                RecentTrackState.COMPLETED: "完了",
                RecentTrackState.SKIPPED: "スキップ",
                RecentTrackState.STOPPED: "停止",
                RecentTrackState.FAILED: "失敗",
            }
            lines.append("最近の再生:")
            lines.extend(f"- {_safe(item.title)}（{labels[item.state]}）" for item in snapshot.recent[:5])
        await _respond(interaction, "\n".join(lines))

    @app_commands.command(name="pause", description="現在の曲を一時停止します")
    async def pause(self, interaction: discord.Interaction) -> None:
        await self._simple_control(interaction, "pause")

    @app_commands.command(name="resume", description="一時停止中の曲を再開します")
    async def resume(self, interaction: discord.Interaction) -> None:
        await self._simple_control(interaction, "resume")

    @app_commands.command(name="skip", description="現在の曲をskipします")
    async def skip(self, interaction: discord.Interaction) -> None:
        await self._simple_control(interaction, "skip")

    @app_commands.command(name="stop", description="音楽だけ停止しqueueを空にします。TTSは止めません")
    async def stop(self, interaction: discord.Interaction) -> None:
        await self._simple_control(interaction, "stop")

    @app_commands.command(name="radio", description="許可済みローカル曲だけを1曲ずつ自動補充します")
    @app_commands.describe(mode="ローカルラジオの自動補充を開始または停止")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="開始", value="on"),
            app_commands.Choice(name="停止", value="off"),
        ]
    )
    async def radio(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            commit_check = await _capability_commit_check(self.bot, interaction, "music radio")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            enabled = mode.value == "on"
            if not enabled and mode.value != "off":
                raise MusicSessionError("radio mode is invalid")
            await self.service.set_local_radio(
                guild_id,
                _actor(interaction),
                enabled,
                commit_check=commit_check,
            )
        except MusicError as exc:
            await _error(interaction, exc)
            return
        await _respond(
            interaction,
            (
                "許可済みローカル曲だけを使うラジオを開始しました。手動の曲追加を優先します。"
                if enabled
                else "ローカルラジオの自動補充を停止しました。現在の曲はそのまま再生します。"
            ),
        )

    @app_commands.command(name="seek", description="現在の許可済みlocal曲を秒数指定で移動します")
    @app_commands.describe(seconds="先頭からの秒数（0〜86400）")
    async def seek(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 86_400],
    ) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            commit_check = await _capability_commit_check(self.bot, interaction, "music seek")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            track = await self.service.seek(
                guild_id,
                _actor(interaction),
                int(seconds),
                commit_check=commit_check,
            )
        except MusicError as exc:
            await _error(interaction, exc)
            return
        await _respond(interaction, f"「{_safe(track.title)}」の再生位置を {int(seconds)}秒へ移動しました。")

    @app_commands.command(name="remove", description="queue内の自分の曲を位置指定で削除します")
    @app_commands.describe(position="queue表示の番号")
    async def remove(self, interaction: discord.Interaction, position: int) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            commit_check = await _capability_commit_check(self.bot, interaction, "music remove")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            removed = await self.service.remove(
                guild_id,
                _actor(interaction),
                position,
                commit_check=commit_check,
            )
        except MusicError as exc:
            await _error(interaction, exc)
            return
        await _respond(interaction, f"queueから「{_safe(removed.title)}」を削除しました。")

    @app_commands.command(name="move", description="queue内の自分の曲を別の位置へ移動します")
    @app_commands.describe(source_position="移動するqueue番号", target_position="移動先のqueue番号")
    async def move(
        self,
        interaction: discord.Interaction,
        source_position: app_commands.Range[int, 1, 100],
        target_position: app_commands.Range[int, 1, 100],
    ) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            commit_check = await _capability_commit_check(self.bot, interaction, "music move")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            moved = await self.service.move(
                guild_id,
                _actor(interaction),
                int(source_position),
                int(target_position),
                commit_check=commit_check,
            )
        except MusicError as exc:
            await _error(interaction, exc)
            return
        await _respond(interaction, f"「{_safe(moved.title)}」をqueue {int(target_position)}番へ移動しました。")

    @app_commands.command(name="clear-mine", description="待機queueから自分が依頼した曲だけを削除します")
    async def clear_mine(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            commit_check = await _capability_commit_check(self.bot, interaction, "music clear-mine")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            removed = await self.service.clear_requester(
                guild_id,
                _actor(interaction),
                commit_check=commit_check,
            )
        except MusicError as exc:
            await _error(interaction, exc)
            return
        await _respond(interaction, f"待機queueから自分の曲を{removed}曲削除しました。再生中の曲は維持します。")

    @app_commands.command(name="shuffle", description="待機中のqueueをshuffleします")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        await self._simple_control(interaction, "shuffle")

    @app_commands.command(name="loop", description="loop方式を変更します")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Track", value="track"),
            app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            normalized = LoopMode(mode.value)
            commit_check = await _capability_commit_check(self.bot, interaction, "music loop")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            await self.service.set_loop(
                guild_id,
                _actor(interaction),
                normalized,
                commit_check=commit_check,
            )
        except (MusicError, ValueError) as exc:
            await _error(interaction, exc)
            return
        await _respond(interaction, f"loopを {normalized.value} に変更しました。")

    @app_commands.command(name="volume", description="音楽busの音量を0〜200%で設定します")
    @app_commands.describe(percent="0〜200", bus="音楽またはTTS")
    @app_commands.choices(
        bus=[
            app_commands.Choice(name="音楽", value="music"),
            app_commands.Choice(name="TTS", value="speech"),
        ]
    )
    async def volume(
        self,
        interaction: discord.Interaction,
        percent: app_commands.Range[int, 0, 200],
        bus: app_commands.Choice[str] | None = None,
    ) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            commit_check = await _capability_commit_check(self.bot, interaction, "music volume")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            selected_bus = "music" if bus is None else bus.value
            if selected_bus == "music":
                await self.service.set_volume(
                    guild_id,
                    _actor(interaction),
                    int(percent) / 100.0,
                    commit_check=commit_check,
                )
            elif selected_bus == "speech":
                await self.service.set_speech_volume(
                    guild_id,
                    _actor(interaction),
                    int(percent) / 100.0,
                    commit_check=commit_check,
                )
            else:
                raise MusicSessionError("volume bus is invalid")
        except MusicError as exc:
            await _error(interaction, exc)
            return
        label = "音楽" if selected_bus == "music" else "TTS"
        await _respond(interaction, f"{label}音量を {int(percent)}% に変更しました。")

    @app_commands.command(name="speak", description="曲を止めずVOICEVOX TTSを重ね、音楽を自動duckingします")
    @app_commands.describe(text="500文字以内", speaker_id="VOICEVOX話者ID")
    async def speak(self, interaction: discord.Interaction, text: str, speaker_id: int = 3) -> None:
        await _defer(interaction)
        try:
            if not self.service.available:
                raise MusicUnavailableError(self.service.reason)
            guild_id, _ = _guild(interaction)
            if interaction.channel_id is None:
                raise MusicSessionError("text channel is required")
            speech_queue = getattr(self.bot, "speech_queue", None)
            if speech_queue is None or not bool(getattr(speech_queue, "available", False)):
                raise MusicUnavailableError("speech-unavailable")
            commit_check = await _capability_commit_check(self.bot, interaction, "music speak")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            speech = await speech_queue.synthesize(
                SpeechRequest(
                    text=text,
                    guild_id=guild_id,
                    channel_id=int(interaction.channel_id),
                    speaker_id=speaker_id,
                ),
                current_policy=commit_check,
            )
            position = await self.service.add_speech_wav(
                guild_id,
                _actor(interaction),
                speech.wav,
                commit_check=commit_check,
            )
        except (MusicError, SpeechUnavailableError, ValueError) as exc:
            await _error(interaction, exc)
            return
        except Exception:
            await _respond(interaction, "TTSを追加できませんでした。")
            return
        await _respond(interaction, f"TTS queue {position}番へ追加しました。曲は停止せず自動duckingします。")

    @app_commands.command(name="search-youtube", description="YouTube公式検索ページURLだけを返します")
    @app_commands.describe(query="検索語。音声抽出には使いません")
    async def search_youtube(self, interaction: discord.Interaction, query: str) -> None:
        try:
            url = youtube_search_url(query)
        except ValueError:
            await _respond(interaction, "検索語は1〜200文字で指定してください。")
            return
        await _respond(
            interaction,
            f"YouTube公式検索: {numbered_link(1, url)}\n"
            "この機能は検索ページを返すだけで、音声抽出・ダウンロード・再配信をしません。",
        )

    async def _simple_control(self, interaction: discord.Interaction, action: str) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            actor = _actor(interaction)
            command_path = f"music {action}"
            commit_check = await _capability_commit_check(self.bot, interaction, command_path)
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            if action == "leave":
                await self.service.leave(guild_id, actor, commit_check=commit_check)
                message = "音楽を終了してVCから退出しました。"
            elif action == "pause":
                await self.service.pause(guild_id, actor, commit_check=commit_check)
                message = "一時停止しました。TTSは引き続き再生できます。"
            elif action == "resume":
                await self.service.resume(guild_id, actor, commit_check=commit_check)
                message = "再開しました。"
            elif action == "skip":
                track = await self.service.skip(guild_id, actor, commit_check=commit_check)
                message = f"「{_safe(track.title)}」をskipしました。"
            elif action == "stop":
                removed = await self.service.stop_music(guild_id, actor, commit_check=commit_check)
                message = f"音楽を停止し、{removed}曲をqueueから除きました。TTSは停止していません。"
            elif action == "shuffle":
                shuffled = await self.service.shuffle(guild_id, actor, commit_check=commit_check)
                message = f"待機中の{shuffled}曲をshuffleしました。"
            else:
                raise ValueError("unknown action")
        except MusicError as exc:
            await _error(interaction, exc)
            return
        await _respond(interaction, message)


class ReadAloudGroup(app_commands.Group):
    """Manage the current text/thread/VC-chat route without accepting source IDs."""

    def __init__(self, bot: Any, owner: MusicGroup) -> None:
        super().__init__(name="read-aloud", description="現在のチャンネルの読み上げ先VCを管理します")
        self.bot = bot
        self.owner = owner

    @app_commands.command(name="enable", description="現在のチャンネルを指定VCへ読み上げます")
    @app_commands.describe(destination="読み上げ先のボイスチャンネル")
    async def enable(
        self,
        interaction: discord.Interaction,
        destination: discord.VoiceChannel,
    ) -> None:
        await _defer(interaction)
        try:
            source, guild_id, repository, current = await self._operation(
                interaction,
                "music read-aloud enable",
                destination=destination,
            )
            route = repository.get(guild_id, int(source.id))
            if route is not None and (
                not isinstance(route, ReadAloudRoute)
                or route.guild_id != guild_id
                or route.source_channel_id != int(source.id)
            ):
                raise ReadAloudRepositoryError("route_scope_invalid")
            if await current() is None:
                raise MusicAuthorizationError("capability policy changed")
            expected_revision = route.revision if isinstance(route, ReadAloudRoute) else None
            if await current() is None:
                raise MusicAuthorizationError("capability policy changed")
            repository.put(
                guild_id=guild_id,
                source_channel_id=int(source.id),
                destination_voice_channel_id=int(destination.id),
                enabled=True,
                expected_revision=expected_revision,
            )
            if await current() is None:
                raise _ReadAloudPostWriteVisibilityError
        except _ReadAloudPostWriteVisibilityError:
            await _respond(
                interaction,
                "設定結果を表示できませんでした。現在の権限を確認してください。",
            )
            return
        except (MusicError, ReadAloudRepositoryError, TypeError, ValueError):
            await _respond(
                interaction, "読み上げ設定を変更できませんでした。権限と現在のチャンネルを確認してください。"
            )
            return
        except Exception:
            await _respond(interaction, "読み上げ設定を変更できませんでした。")
            return
        await _respond(interaction, f"現在のチャンネルを <#{int(destination.id)}> へ読み上げます。")

    @app_commands.command(name="disable", description="現在のチャンネルの読み上げを無効にします")
    async def disable(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        try:
            source, guild_id, repository, current = await self._operation(
                interaction,
                "music read-aloud disable",
            )
            route = repository.get(guild_id, int(source.id))
            if route is not None and (
                not isinstance(route, ReadAloudRoute)
                or route.guild_id != guild_id
                or route.source_channel_id != int(source.id)
            ):
                raise ReadAloudRepositoryError("route_scope_invalid")
            if await current() is None:
                raise MusicAuthorizationError("capability policy changed")
            if route is None:
                await _respond(interaction, "現在のチャンネルに読み上げ設定はありません。")
                return
            if await current() is None:
                raise MusicAuthorizationError("capability policy changed")
            deleted = repository.delete(
                guild_id,
                int(source.id),
                expected_revision=route.revision,
            )
            if not deleted:
                raise ReadAloudRepositoryError("route_revision_conflict")
            if await current() is None:
                raise _ReadAloudPostWriteVisibilityError
        except _ReadAloudPostWriteVisibilityError:
            await _respond(
                interaction,
                "設定結果を表示できませんでした。現在の権限を確認してください。",
            )
            return
        except (MusicError, ReadAloudRepositoryError, TypeError, ValueError):
            await _respond(
                interaction, "読み上げ設定を変更できませんでした。権限と現在のチャンネルを確認してください。"
            )
            return
        except Exception:
            await _respond(interaction, "読み上げ設定を変更できませんでした。")
            return
        await _respond(interaction, "現在のチャンネルの読み上げを無効にしました。")

    @app_commands.command(
        name="focus-start",
        description="現在のチャンネルを指定VCへ一時的に読み上げます",
    )
    @app_commands.describe(
        destination="一時読み上げ先のボイスチャンネル",
        duration_minutes="1〜240分",
        speak_on_complete="終了時にもVCで知らせる",
    )
    async def focus_start(
        self,
        interaction: discord.Interaction,
        destination: discord.VoiceChannel,
        duration_minutes: app_commands.Range[int, 1, 240],
        speak_on_complete: bool = False,
    ) -> None:
        await _defer(interaction)
        try:
            source, guild_id, _repository, current = await self._operation(
                interaction,
                "music read-aloud focus-start",
                destination=destination,
            )
            actor = await current()
            interaction_id = _positive_id(interaction)
            service = getattr(self.bot, "scheduling_focus_timer_service", None)
            if (
                isinstance(duration_minutes, bool)
                or not isinstance(duration_minutes, int)
                or not 1 <= duration_minutes <= 240
                or not isinstance(speak_on_complete, bool)
                or actor is None
                or actor.voice_channel_id != int(destination.id)
                or interaction_id is None
                or not self._focus_timer_service_current(service)
                or not callable(getattr(service, "schedule", None))
            ):
                raise MusicAuthorizationError("capability policy changed")
            request = FocusTimerRequest(
                binding=FocusTimerBinding(
                    timer_id=f"focus-{int(source.id)}",
                    owner_id=actor.user_id,
                    guild_id=guild_id,
                    source_channel_id=int(source.id),
                    destination_channel_id=int(destination.id),
                    revision=interaction_id,
                ),
                expires_at=datetime.now(UTC) + timedelta(minutes=int(duration_minutes)),
                speak_on_complete=speak_on_complete,
            )
            receipt = await service.schedule(request)
            if (
                not isinstance(receipt, FocusTimerScheduleReceipt)
                or await current() is None
                or not self._focus_timer_service_current(service)
            ):
                raise _ReadAloudPostWriteVisibilityError
        except _ReadAloudPostWriteVisibilityError:
            await _respond(
                interaction,
                "タイマーは受理されましたが、現在の権限では結果を表示できません。",
            )
            return
        except (MusicError, PermissionError, RuntimeError, TypeError, ValueError):
            await _respond(
                interaction,
                "フォーカスタイマーを開始できませんでした。権限、VC、既存タイマーを確認してください。",
            )
            return
        except Exception:
            await _respond(interaction, "フォーカスタイマーを開始できませんでした。")
            return
        await _respond(
            interaction,
            f"{int(duration_minutes)}分の一時読み上げを開始しました。曲は停止しません。",
        )

    @app_commands.command(
        name="focus-cancel",
        description="現在のチャンネルの一時読み上げタイマーを取り消します",
    )
    async def focus_cancel(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        try:
            source, guild_id, _repository, current = await self._operation(
                interaction,
                "music read-aloud focus-cancel",
            )
            actor = await current()
            service = getattr(self.bot, "scheduling_focus_timer_service", None)
            if (
                actor is None
                or not self._focus_timer_service_current(service)
                or not callable(getattr(service, "cancel_active", None))
            ):
                raise MusicAuthorizationError("capability policy changed")
            receipt = await service.cancel_active(
                guild_id=guild_id,
                source_channel_id=int(source.id),
                actor_id=actor.user_id,
            )
            if (
                not isinstance(receipt, FocusTimerCancelReceipt)
                or await current() is None
                or not self._focus_timer_service_current(service)
            ):
                raise _ReadAloudPostWriteVisibilityError
        except _ReadAloudPostWriteVisibilityError:
            await _respond(
                interaction,
                "取消結果を表示できませんでした。現在の権限を確認してください。",
            )
            return
        except (MusicError, PermissionError, RuntimeError, TypeError, ValueError):
            await _respond(interaction, "フォーカスタイマーを取り消せませんでした。")
            return
        except Exception:
            await _respond(interaction, "フォーカスタイマーを取り消せませんでした。")
            return
        if not receipt.overlay_disabled:
            await _respond(interaction, "取り消せるフォーカスタイマーはありません。")
            return
        await _respond(interaction, "一時読み上げとフォーカスタイマーを取り消しました。")

    @app_commands.command(name="list", description="このサーバーの読み上げ設定を最大25件表示します")
    async def list_routes(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        try:
            _source, guild_id, repository, current = await self._operation(
                interaction,
                "music read-aloud list",
            )
            routes = repository.list_for_guild(guild_id)
            if any(not isinstance(route, ReadAloudRoute) or route.guild_id != guild_id for route in routes):
                raise ReadAloudRepositoryError("route_scope_invalid")
            visible = tuple(routes[:25])
            if await current(visible) is None:
                raise MusicAuthorizationError("capability policy changed")
        except (MusicError, ReadAloudRepositoryError, TypeError, ValueError):
            await _respond(interaction, "読み上げ設定を表示できませんでした。権限を確認してください。")
            return
        except Exception:
            await _respond(interaction, "読み上げ設定を表示できませんでした。")
            return
        if not visible:
            await _respond(interaction, "このサーバーに表示できる読み上げ設定はありません。")
            return
        lines = [
            f"{index}. <#{route.source_channel_id}> → <#{route.destination_voice_channel_id}>"
            for index, route in enumerate(visible, start=1)
        ]
        await _respond(interaction, "読み上げ設定:\n" + "\n".join(lines))

    @app_commands.command(name="server-preset", description="サーバー共通の読み上げ速度と音量を設定します")
    @app_commands.describe(
        speed_percent="速度（50〜200%）",
        volume_percent="音量（0〜200%）",
        clear="サーバー設定を削除して既定値へ戻す",
    )
    async def server_preset(
        self,
        interaction: discord.Interaction,
        speed_percent: int | None = None,
        volume_percent: int | None = None,
        clear: bool = False,
    ) -> None:
        await _defer(interaction)
        try:
            values = await self._change_preset(
                interaction,
                "music read-aloud server-preset",
                speed_percent=speed_percent,
                volume_percent=volume_percent,
                clear=clear,
                user_scope=False,
            )
        except _ReadAloudPostWriteVisibilityError:
            await _respond(interaction, "設定結果を表示できませんでした。現在の権限を確認してください。")
            return
        except (MusicError, VoicePresetRepositoryError, TypeError, ValueError):
            await _respond(interaction, "サーバー読み上げpresetを変更できませんでした。権限と入力を確認してください。")
            return
        except Exception:
            await _respond(interaction, "サーバー読み上げpresetを変更できませんでした。")
            return
        await _respond(interaction, f"サーバー読み上げpreset: {_preset_values_label(values)}")

    @app_commands.command(name="my-preset", description="自分の読み上げ速度と音量を設定します")
    @app_commands.describe(
        speed_percent="速度（50〜200%）",
        volume_percent="音量（0〜200%）",
        clear="自分の設定を削除してサーバー設定へ戻す",
    )
    async def my_preset(
        self,
        interaction: discord.Interaction,
        speed_percent: int | None = None,
        volume_percent: int | None = None,
        clear: bool = False,
    ) -> None:
        await _defer(interaction)
        try:
            values = await self._change_preset(
                interaction,
                "music read-aloud my-preset",
                speed_percent=speed_percent,
                volume_percent=volume_percent,
                clear=clear,
                user_scope=True,
            )
        except _ReadAloudPostWriteVisibilityError:
            await _respond(interaction, "設定結果を表示できませんでした。現在の権限を確認してください。")
            return
        except (MusicError, VoicePresetRepositoryError, TypeError, ValueError):
            await _respond(interaction, "自分の読み上げpresetを変更できませんでした。権限と入力を確認してください。")
            return
        except Exception:
            await _respond(interaction, "自分の読み上げpresetを変更できませんでした。")
            return
        await _respond(interaction, f"自分に適用される読み上げpreset: {_preset_values_label(values)}")

    @app_commands.command(name="preset", description="自分に適用される読み上げpresetを表示します")
    async def preset(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        try:
            guild_id, user_id, repository, current = await self._preset_operation(
                interaction,
                "music read-aloud preset",
                require_manage_guild=False,
            )
            server, user, resolved = await asyncio.gather(
                asyncio.to_thread(repository.get_server, guild_id),
                asyncio.to_thread(repository.get_user, guild_id, user_id),
                asyncio.to_thread(repository.resolve, guild_id, user_id),
            )
            if (
                (server is not None and not isinstance(server, VoicePresetRecord))
                or (user is not None and not isinstance(user, VoicePresetRecord))
                or not isinstance(resolved, ResolvedVoicePreset)
                or resolved.guild_id != guild_id
                or resolved.user_id != user_id
            ):
                raise VoicePresetRepositoryError("preset_read_invalid")
            if await current() is None:
                raise MusicAuthorizationError("capability policy changed")
            lines = (
                f"適用中（{_preset_scope_label(resolved.source)}）: {_preset_values_label(resolved.values)}",
                "サーバー: " + ("既定値" if server is None else _preset_values_label(server.values)),
                "自分: " + ("未設定" if user is None else _preset_values_label(user.values)),
            )
            if await current() is None:
                raise MusicAuthorizationError("capability policy changed")
        except (MusicError, VoicePresetRepositoryError, TypeError, ValueError):
            await _respond(interaction, "読み上げpresetを表示できませんでした。現在の権限を確認してください。")
            return
        except Exception:
            await _respond(interaction, "読み上げpresetを表示できませんでした。")
            return
        await _respond(interaction, "\n".join(lines))

    @app_commands.command(name="dictionary-set", description="読み上げ辞書のliteral置換を設定します")
    @app_commands.describe(term="置換する語（64文字以内）", pronunciation="読み（64文字以内）")
    async def dictionary_set(
        self,
        interaction: discord.Interaction,
        term: str,
        pronunciation: str,
    ) -> None:
        await _defer(interaction)
        try:
            await self._policy_change(
                interaction,
                "music read-aloud dictionary-set",
                operation="dictionary-set",
                value=term,
                replacement=pronunciation,
            )
        except _ReadAloudPostWriteVisibilityError:
            await _respond(interaction, "設定結果を表示できませんでした。現在の権限を確認してください。")
            return
        except (MusicError, ReadAloudRepositoryError, TypeError, ValueError):
            await _respond(interaction, "読み上げ辞書を変更できませんでした。権限と入力を確認してください。")
            return
        except Exception:
            await _respond(interaction, "読み上げ辞書を変更できませんでした。")
            return
        await _respond(interaction, "読み上げ辞書を設定しました。")

    @app_commands.command(name="dictionary-delete", description="読み上げ辞書の語を削除します")
    @app_commands.describe(term="削除する語（64文字以内）")
    async def dictionary_delete(
        self,
        interaction: discord.Interaction,
        term: str,
    ) -> None:
        await _defer(interaction)
        try:
            await self._policy_change(
                interaction,
                "music read-aloud dictionary-delete",
                operation="dictionary-delete",
                value=term,
            )
        except _ReadAloudPostWriteVisibilityError:
            await _respond(interaction, "設定結果を表示できませんでした。現在の権限を確認してください。")
            return
        except (MusicError, ReadAloudRepositoryError, TypeError, ValueError):
            await _respond(interaction, "読み上げ辞書を変更できませんでした。権限と入力を確認してください。")
            return
        except Exception:
            await _respond(interaction, "読み上げ辞書を変更できませんでした。")
            return
        await _respond(interaction, "読み上げ辞書から語を削除しました。")

    @app_commands.command(name="exclude-add", description="literal一致を含む本文を読み上げ対象外にします")
    @app_commands.describe(phrase="対象外にするliteral phrase（64文字以内）")
    async def exclude_add(
        self,
        interaction: discord.Interaction,
        phrase: str,
    ) -> None:
        await _defer(interaction)
        try:
            await self._policy_change(
                interaction,
                "music read-aloud exclude-add",
                operation="exclude-add",
                value=phrase,
            )
        except _ReadAloudPostWriteVisibilityError:
            await _respond(interaction, "設定結果を表示できませんでした。現在の権限を確認してください。")
            return
        except (MusicError, ReadAloudRepositoryError, TypeError, ValueError):
            await _respond(interaction, "除外設定を変更できませんでした。権限と入力を確認してください。")
            return
        except Exception:
            await _respond(interaction, "除外設定を変更できませんでした。")
            return
        await _respond(interaction, "読み上げ除外phraseを追加しました。")

    @app_commands.command(name="exclude-delete", description="読み上げ対象外のphraseを削除します")
    @app_commands.describe(phrase="削除するliteral phrase（64文字以内）")
    async def exclude_delete(
        self,
        interaction: discord.Interaction,
        phrase: str,
    ) -> None:
        await _defer(interaction)
        try:
            await self._policy_change(
                interaction,
                "music read-aloud exclude-delete",
                operation="exclude-delete",
                value=phrase,
            )
        except _ReadAloudPostWriteVisibilityError:
            await _respond(interaction, "設定結果を表示できませんでした。現在の権限を確認してください。")
            return
        except (MusicError, ReadAloudRepositoryError, TypeError, ValueError):
            await _respond(interaction, "除外設定を変更できませんでした。権限と入力を確認してください。")
            return
        except Exception:
            await _respond(interaction, "除外設定を変更できませんでした。")
            return
        await _respond(interaction, "読み上げ除外phraseを削除しました。")

    @app_commands.command(name="policy", description="読み上げ辞書と除外設定を最大20行で表示します")
    async def policy(self, interaction: discord.Interaction) -> None:
        await _defer(interaction)
        try:
            _source, guild_id, repository, current = await self._operation(
                interaction,
                "music read-aloud policy",
            )
            snapshot = await asyncio.to_thread(repository.get_policy, guild_id)
            if not isinstance(snapshot, ReadAloudPolicySnapshot) or snapshot.guild_id != guild_id:
                raise ReadAloudRepositoryError("policy_scope_invalid")
            if await current() is None:
                raise MusicAuthorizationError("capability policy changed")
            lines = [
                f"読み上げpolicy rev {snapshot.revision}（辞書 {len(snapshot.dictionary)} / 除外 {len(snapshot.exclusions)}）"
            ]
            entries = [f"辞書: {_safe(entry.term)} → {_safe(entry.pronunciation)}" for entry in snapshot.dictionary]
            entries.extend(f"除外: {_safe(phrase)}" for phrase in snapshot.exclusions)
            lines.extend(entries[:19])
            if await current() is None:
                raise MusicAuthorizationError("capability policy changed")
        except (MusicError, ReadAloudRepositoryError, TypeError, ValueError):
            await _respond(interaction, "読み上げpolicyを表示できませんでした。権限を確認してください。")
            return
        except Exception:
            await _respond(interaction, "読み上げpolicyを表示できませんでした。")
            return
        await _respond(interaction, "\n".join(lines))

    async def _policy_change(
        self,
        interaction: discord.Interaction,
        command_path: str,
        *,
        operation: str,
        value: str,
        replacement: str | None = None,
    ) -> ReadAloudPolicySnapshot:
        _source, guild_id, repository, current = await self._operation(
            interaction,
            command_path,
        )
        before = await asyncio.to_thread(repository.get_policy, guild_id)
        if not isinstance(before, ReadAloudPolicySnapshot) or before.guild_id != guild_id:
            raise ReadAloudRepositoryError("policy_scope_invalid")
        if await current() is None:
            raise MusicAuthorizationError("capability policy changed")
        if operation == "dictionary-set" and isinstance(replacement, str):
            updated = await asyncio.to_thread(
                repository.set_dictionary,
                guild_id,
                value,
                replacement,
                expected_revision=before.revision,
            )
        elif operation == "dictionary-delete":
            updated = await asyncio.to_thread(
                repository.delete_dictionary,
                guild_id,
                value,
                expected_revision=before.revision,
            )
        elif operation == "exclude-add":
            updated = await asyncio.to_thread(
                repository.add_exclusion,
                guild_id,
                value,
                expected_revision=before.revision,
            )
        elif operation == "exclude-delete":
            updated = await asyncio.to_thread(
                repository.delete_exclusion,
                guild_id,
                value,
                expected_revision=before.revision,
            )
        else:
            raise ValueError("unknown policy operation")
        if (
            not isinstance(updated, ReadAloudPolicySnapshot)
            or updated.guild_id != guild_id
            or updated.revision != before.revision + 1
        ):
            raise ReadAloudRepositoryError("policy_write_invalid")
        if await current() is None:
            raise _ReadAloudPostWriteVisibilityError
        after = await asyncio.to_thread(repository.get_policy, guild_id)
        if after != updated:
            raise ReadAloudRepositoryError("policy_read_after_write_mismatch")
        if await current() is None:
            raise _ReadAloudPostWriteVisibilityError
        return updated

    async def _change_preset(
        self,
        interaction: discord.Interaction,
        command_path: str,
        *,
        speed_percent: int | None,
        volume_percent: int | None,
        clear: bool,
        user_scope: bool,
    ) -> VoicePresetValues:
        if not isinstance(clear, bool):
            raise TypeError("clear must be bool")
        if clear and (speed_percent is not None or volume_percent is not None):
            raise ValueError("clear cannot be combined with values")
        if not clear and speed_percent is None and volume_percent is None:
            raise ValueError("at least one preset value is required")
        speed_milli = None if speed_percent is None else _percent_to_milli(speed_percent, minimum=50, maximum=200)
        volume_milli = None if volume_percent is None else _percent_to_milli(volume_percent, minimum=0, maximum=200)
        guild_id, user_id, repository, current = await self._preset_operation(
            interaction,
            command_path,
            require_manage_guild=not user_scope,
        )
        record = await asyncio.to_thread(
            repository.get_user if user_scope else repository.get_server,
            guild_id,
            *([user_id] if user_scope else []),
        )
        if record is not None and not isinstance(record, VoicePresetRecord):
            raise VoicePresetRepositoryError("preset_read_invalid")
        if await current() is None:
            raise MusicAuthorizationError("capability policy changed")
        expected_revision = 0 if record is None else record.revision

        if clear:
            # The final fresh authorization check and the small SQLite CAS write
            # intentionally have no await between them.
            cleared = (repository.clear_user if user_scope else repository.clear_server)(
                guild_id,
                *([user_id] if user_scope else []),
                expected_revision=expected_revision,
            )
            if record is not None and cleared is not True:
                raise VoicePresetRepositoryError("preset_clear_invalid")
            if await current() is None:
                raise _ReadAloudPostWriteVisibilityError
            after = await asyncio.to_thread(
                repository.get_user if user_scope else repository.get_server,
                guild_id,
                *([user_id] if user_scope else []),
            )
            if after is not None:
                raise VoicePresetRepositoryError("preset_read_after_clear_mismatch")
        else:
            if record is not None:
                base = record.values
            elif user_scope:
                resolved = await asyncio.to_thread(repository.resolve, guild_id, user_id)
                if not isinstance(resolved, ResolvedVoicePreset):
                    raise VoicePresetRepositoryError("preset_resolution_invalid")
                base = resolved.values
            else:
                base = DEFAULT_VOICE_PRESET
            values = VoicePresetValues(
                speed_milli=base.speed_milli if speed_milli is None else speed_milli,
                volume_milli=base.volume_milli if volume_milli is None else volume_milli,
            )
            # Resolving an inherited user value awaited above, so recheck once more.
            # The small SQLite CAS write then has no await before its commit boundary.
            if await current() is None:
                raise MusicAuthorizationError("capability policy changed")
            updated = (repository.set_user if user_scope else repository.set_server)(
                guild_id,
                *([user_id] if user_scope else []),
                values,
                expected_revision=expected_revision,
            )
            if (
                not isinstance(updated, VoicePresetRecord)
                or updated.guild_id != guild_id
                or updated.user_id != (user_id if user_scope else None)
                or updated.values != values
                or updated.revision != expected_revision + 1
            ):
                raise VoicePresetRepositoryError("preset_write_invalid")
            if await current() is None:
                raise _ReadAloudPostWriteVisibilityError
            after = await asyncio.to_thread(
                repository.get_user if user_scope else repository.get_server,
                guild_id,
                *([user_id] if user_scope else []),
            )
            if after != updated:
                raise VoicePresetRepositoryError("preset_read_after_write_mismatch")

        if not user_scope:
            if await current() is None:
                raise _ReadAloudPostWriteVisibilityError
            return DEFAULT_VOICE_PRESET if clear else values

        resolved = await asyncio.to_thread(repository.resolve, guild_id, user_id)
        if (
            not isinstance(resolved, ResolvedVoicePreset)
            or resolved.guild_id != guild_id
            or resolved.user_id != user_id
        ):
            raise VoicePresetRepositoryError("preset_resolution_invalid")
        if await current() is None:
            raise _ReadAloudPostWriteVisibilityError
        return resolved.values

    async def _preset_operation(
        self,
        interaction: discord.Interaction,
        command_path: str,
        *,
        require_manage_guild: bool,
    ) -> tuple[int, int, Any, Any]:
        guild_id, guild = _guild(interaction)
        user_id = _positive_id(getattr(interaction, "user", None))
        source = getattr(interaction, "channel", None)
        if user_id is None or not _read_aloud_source_channel(source, guild_id):
            raise MusicSessionError("preset scope is invalid")
        if interaction.channel_id != int(source.id):
            raise MusicSessionError("source channel binding changed")

        route_repository = getattr(self.bot, "music_read_aloud_repository", None)
        preset_repository = getattr(self.bot, "music_read_aloud_preset_repository", None)
        read_aloud_service = getattr(self.bot, "music_read_aloud_service", None)
        music_service = self.owner.service
        guard = getattr(self.bot, "capability_guard", None)
        base_check = await _capability_commit_check(self.bot, interaction, command_path)
        if (
            route_repository is None
            or preset_repository is None
            or read_aloud_service is None
            or guard is None
            or base_check is None
        ):
            raise MusicUnavailableError("read-aloud-unavailable")

        async def current() -> MusicActor | None:
            if not self._runtime_current(
                repository=route_repository,
                read_aloud_service=read_aloud_service,
                music_service=music_service,
                guard=guard,
            ):
                return None
            if getattr(self.bot, "music_read_aloud_preset_repository", None) is not preset_repository:
                return None
            if getattr(preset_repository, "is_open", False) is not True:
                return None
            actor = await base_check()
            if actor is None or actor.user_id != user_id or (require_manage_guild and not actor.manage_guild):
                return None
            bot_member = await _fresh_bot_member(self.bot, guild)
            if bot_member is None:
                return None
            actor = await base_check()
            if actor is None or actor.user_id != user_id or (require_manage_guild and not actor.manage_guild):
                return None
            if (
                getattr(self.bot, "music_read_aloud_preset_repository", None) is not preset_repository
                or getattr(preset_repository, "is_open", False) is not True
                or not _channel_is_current(guild, source)
                or not _bot_can_read(source, bot_member)
            ):
                return None
            return actor

        if await current() is None:
            raise MusicAuthorizationError("capability policy changed")
        return guild_id, user_id, preset_repository, current

    async def _operation(
        self,
        interaction: discord.Interaction,
        command_path: str,
        *,
        destination: Any | None = None,
    ) -> tuple[Any, int, Any, Any]:
        guild_id, guild = _guild(interaction)
        source = getattr(interaction, "channel", None)
        if not _read_aloud_source_channel(source, guild_id):
            raise MusicSessionError("source channel is invalid")
        if interaction.channel_id != int(source.id):
            raise MusicSessionError("source channel binding changed")
        if destination is not None and not _read_aloud_destination_channel(destination, guild_id):
            raise MusicSessionError("destination channel is invalid")

        repository = getattr(self.bot, "music_read_aloud_repository", None)
        read_aloud_service = getattr(self.bot, "music_read_aloud_service", None)
        music_service = self.owner.service
        guard = getattr(self.bot, "capability_guard", None)
        base_check = await _capability_commit_check(self.bot, interaction, command_path)
        if repository is None or read_aloud_service is None or guard is None or base_check is None:
            raise MusicUnavailableError("read-aloud-unavailable")

        async def current(
            listed_routes: tuple[ReadAloudRoute, ...] = (),
        ) -> MusicActor | None:
            if not self._runtime_current(
                repository=repository,
                read_aloud_service=read_aloud_service,
                music_service=music_service,
                guard=guard,
            ):
                return None
            actor = await base_check()
            if actor is None or not actor.manage_guild:
                return None
            bot_member = await _fresh_bot_member(self.bot, guild)
            if bot_member is None:
                return None
            actor = await base_check()
            if actor is None or not actor.manage_guild:
                return None
            if not self._runtime_current(
                repository=repository,
                read_aloud_service=read_aloud_service,
                music_service=music_service,
                guard=guard,
            ):
                return None
            if not _channel_is_current(guild, source) or not _bot_can_read(source, bot_member):
                return None
            if destination is not None and (
                not _channel_is_current(guild, destination) or not _bot_can_speak(destination, bot_member)
            ):
                return None
            for route in listed_routes:
                route_source = _current_channel(guild, route.source_channel_id)
                route_destination = _current_channel(
                    guild,
                    route.destination_voice_channel_id,
                )
                if (
                    not _read_aloud_source_channel(route_source, guild_id)
                    or not _read_aloud_destination_channel(route_destination, guild_id)
                    or not _bot_can_read(route_source, bot_member)
                    or not _bot_can_speak(route_destination, bot_member)
                ):
                    return None
            if not self._runtime_current(
                repository=repository,
                read_aloud_service=read_aloud_service,
                music_service=music_service,
                guard=guard,
            ):
                return None
            return actor

        if await current() is None:
            raise MusicAuthorizationError("capability policy changed")
        return source, guild_id, repository, current

    def _runtime_current(
        self,
        *,
        repository: Any,
        read_aloud_service: Any,
        music_service: Any,
        guard: Any,
    ) -> bool:
        return bool(
            self.owner.read_aloud is self
            and self.owner.service is music_service
            and getattr(self.bot, "music_service", None) is music_service
            and getattr(self.bot, "music_read_aloud_repository", None) is repository
            and getattr(self.bot, "music_read_aloud_service", None) is read_aloud_service
            and getattr(self.bot, "capability_guard", None) is guard
            and getattr(repository, "is_open", False) is True
            and getattr(
                getattr(self.bot, "music_read_aloud_preset_repository", None),
                "is_open",
                False,
            )
            is True
            and getattr(read_aloud_service, "available", False) is True
            and not bool(getattr(self.bot, "is_closing", False))
        )

    def _focus_timer_service_current(self, service: Any) -> bool:
        plugin = getattr(self.bot, "scheduling_plugin", None)
        return bool(
            service is not None
            and getattr(self.bot, "scheduling_focus_timer_service", None) is service
            and getattr(plugin, "focus_service", None) is service
            and not bool(getattr(plugin, "closing", True))
            and not bool(getattr(self.bot, "is_closing", False))
        )


class PlaylistGroup(app_commands.Group):
    def __init__(self, bot: Any, service: MusicService) -> None:
        super().__init__(name="playlist", description="本人所有playlistを管理します")
        self.bot = bot
        self.service = service

    @app_commands.command(name="save", description="現在の曲とqueueを自分のplaylistへ保存します")
    async def save(self, interaction: discord.Interaction, name: str) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            commit_check = await _capability_commit_check(self.bot, interaction, "music playlist save")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            record = await self.service.save_playlist(
                guild_id,
                _actor(interaction),
                name,
                commit_check=commit_check,
            )
        except MusicError as exc:
            await _error(interaction, exc)
            return
        await _respond(interaction, f"自分のplaylist「{_safe(record.name)}」を保存しました。")

    @app_commands.command(name="list", description="このサーバーで自分が所有するplaylistを表示します")
    async def list_playlists(self, interaction: discord.Interaction) -> None:
        try:
            guild_id, _ = _guild(interaction)
            records = await self.service.list_playlists(guild_id, _actor(interaction))
        except MusicError as exc:
            await _error(interaction, exc)
            return
        names = playlist_titles(records)
        if not names:
            await _respond(interaction, "このサーバーに自分のplaylistはありません。")
            return
        await _respond(interaction, "自分のplaylist:\n" + "\n".join(f"- {_safe(name)}" for name in names))

    @app_commands.command(name="load", description="自分のplaylistをlocal libraryで再解決してqueueへ追加します")
    async def load(self, interaction: discord.Interaction, name: str) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            commit_check = await _capability_commit_check(self.bot, interaction, "music playlist load")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            loaded, missing = await self.service.load_playlist(
                guild_id,
                _actor(interaction),
                name,
                commit_check=commit_check,
            )
        except MusicError as exc:
            await _error(interaction, exc)
            return
        await _respond(interaction, f"自分のplaylistから{loaded}曲を追加しました。未解決: {missing}曲。")

    @app_commands.command(name="delete", description="自分が所有するplaylistを削除します")
    async def delete(self, interaction: discord.Interaction, name: str) -> None:
        await _defer(interaction)
        try:
            guild_id, _ = _guild(interaction)
            commit_check = await _capability_commit_check(self.bot, interaction, "music playlist delete")
            if commit_check is None:
                raise MusicAuthorizationError("capability policy changed")
            deleted = await self.service.delete_playlist(
                guild_id,
                _actor(interaction),
                name,
                commit_check=commit_check,
            )
        except MusicError as exc:
            await _error(interaction, exc)
            return
        await _respond(interaction, "削除しました。" if deleted else "自分のplaylistに一致する名前はありません。")


_READ_ALOUD_SOURCE_TYPES = frozenset(
    {
        discord.ChannelType.text,
        discord.ChannelType.news,
        discord.ChannelType.news_thread,
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
        discord.ChannelType.voice,
    }
)


def _read_aloud_source_channel(channel: Any, guild_id: int) -> bool:
    return bool(
        channel is not None
        and getattr(channel, "type", None) in _READ_ALOUD_SOURCE_TYPES
        and _channel_guild_id(channel) == guild_id
        and _positive_id(channel) is not None
    )


def _read_aloud_destination_channel(channel: Any, guild_id: int) -> bool:
    return bool(
        channel is not None
        and getattr(channel, "type", None) is discord.ChannelType.voice
        and _channel_guild_id(channel) == guild_id
        and _positive_id(channel) is not None
    )


def _channel_guild_id(channel: Any) -> int | None:
    guild_id = getattr(getattr(channel, "guild", None), "id", None)
    return int(guild_id) if isinstance(guild_id, int) and not isinstance(guild_id, bool) and guild_id > 0 else None


def _positive_id(value: Any) -> int | None:
    identifier = getattr(value, "id", None)
    return (
        int(identifier) if isinstance(identifier, int) and not isinstance(identifier, bool) and identifier > 0 else None
    )


def _current_channel(guild: Any, channel_id: int) -> Any | None:
    getter = getattr(guild, "get_channel_or_thread", None)
    if callable(getter):
        try:
            channel = getter(channel_id)
        except Exception:
            return None
        if channel is not None:
            return channel
    getter = getattr(guild, "get_channel", None)
    if not callable(getter):
        return None
    try:
        return getter(channel_id)
    except Exception:
        return None


def _channel_is_current(guild: Any, channel: Any) -> bool:
    channel_id = _positive_id(channel)
    return channel_id is not None and _current_channel(guild, channel_id) is channel


async def _fresh_bot_member(bot: Any, guild: Any) -> Any | None:
    bot_user_id = _positive_id(getattr(bot, "user", None))
    fetch_member = getattr(guild, "fetch_member", None)
    if bot_user_id is None or not callable(fetch_member):
        return None
    try:
        member = await fetch_member(bot_user_id)
    except Exception:
        return None
    return member if _positive_id(member) == bot_user_id else None


def _bot_can_read(channel: Any, bot_member: Any) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    try:
        permissions = permissions_for(bot_member)
    except Exception:
        return False
    return bool(getattr(permissions, "view_channel", False) and getattr(permissions, "read_message_history", False))


def _bot_can_speak(channel: Any, bot_member: Any) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    try:
        permissions = permissions_for(bot_member)
    except Exception:
        return False
    return bool(
        getattr(permissions, "view_channel", False)
        and getattr(permissions, "connect", False)
        and getattr(permissions, "speak", False)
    )


def _guild(interaction: discord.Interaction) -> tuple[int, Any]:
    if interaction.guild_id is None or interaction.guild is None:
        raise MusicSessionError("guild is required")
    return int(interaction.guild_id), interaction.guild


def _voice_channel(interaction: discord.Interaction) -> Any:
    voice = getattr(interaction.user, "voice", None)
    channel = getattr(voice, "channel", None)
    if channel is None or getattr(channel, "id", None) is None:
        raise MusicAuthorizationError("voice channel is required")
    return channel


def _actor(interaction: discord.Interaction) -> MusicActor:
    channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
    channel_id = int(channel.id) if channel is not None and getattr(channel, "id", None) is not None else None
    permissions = getattr(interaction.user, "guild_permissions", None) or getattr(interaction, "permissions", None)
    manage = bool(
        permissions is not None
        and (getattr(permissions, "administrator", False) or getattr(permissions, "manage_guild", False))
    )
    return MusicActor(int(interaction.user.id), channel_id, manage)


async def _capability_commit_check(
    bot: Any,
    interaction: discord.Interaction,
    command_path: str,
) -> MusicFreshCheck | None:
    if interaction.guild is None or getattr(interaction.user, "id", None) is None:
        return None
    return await build_music_commit_check(
        bot,
        interaction.guild,
        int(interaction.user.id),
        command_path,
    )


async def _defer(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)


async def _respond(interaction: discord.Interaction, content: str) -> None:
    content = str(content)[:1_950]
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_MENTIONS)


async def _error(interaction: discord.Interaction, error: BaseException) -> None:
    if isinstance(error, MusicUnavailableError):
        message = "音楽機能は利用できません。`/music status` で安全設定を確認してください。"
    elif isinstance(error, MusicAuthorizationError):
        message = "同じVCの曲依頼者、またはサーバー管理権限を持つメンバーだけ操作できます。"
    elif isinstance(error, PlaylistError):
        message = "自分のplaylistを処理できませんでした。名前・曲数・local libraryを確認してください。"
    elif isinstance(error, MusicSeekUnsupportedError):
        message = "この音源は再生位置の変更に対応していません。"
    elif isinstance(error, (MusicSessionError, SpeechUnavailableError, ValueError)):
        message = "この操作は現在実行できません。VC接続・queue・入力値を確認してください。"
    else:
        message = "音楽操作に失敗しました。"
    await _respond(interaction, message)


def _percent_to_milli(value: int, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("preset percentage is out of bounds")
    return value * 10


def _preset_values_label(values: VoicePresetValues) -> str:
    if not isinstance(values, VoicePresetValues):
        raise TypeError("values must be VoicePresetValues")
    return f"速度 {_milli_percent(values.speed_milli)}% / 音量 {_milli_percent(values.volume_milli)}%"


def _milli_percent(value: int) -> str:
    whole, decimal = divmod(value, 10)
    return str(whole) if decimal == 0 else f"{whole}.{decimal}"


def _preset_scope_label(scope: VoicePresetScope) -> str:
    return {
        VoicePresetScope.DEFAULT: "既定値",
        VoicePresetScope.SERVER: "サーバー",
        VoicePresetScope.USER: "自分",
    }[scope]


def _safe(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))[:200]


def _reason(value: str) -> str:
    labels = {
        "disabled": "MUSIC_ENABLED=false",
        "library-not-configured": "許可済みlibrary未設定",
        "library-empty": "許可済み曲なし",
        "ffmpeg-unavailable": "FFmpeg未設定",
        "repository-unavailable": "playlist DB未使用",
        "speech-unavailable": "VOICEVOX未設定",
        "queue-limit-exceeds-durable-projection": "MUSIC_MAX_QUEUEは100以下が必要",
    }
    return labels.get(value, "初期化未完了")
