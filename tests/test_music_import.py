from __future__ import annotations

import asyncio
import struct
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.modules.audio_core import LocalMediaLibrary
from yonerai_discord.modules.music import MusicPlugin
from yonerai_discord.modules.music.adapter import MusicGroup
from yonerai_discord.modules.music.imports import MusicImportStore
from yonerai_discord.modules.music.models import (
    GuildAudioProjection,
    ImportedMusicAsset,
    MusicActor,
    MusicAuthorizationError,
    PlaylistError,
    PersistedMusicTrackRef,
)
from yonerai_discord.modules.music.repository import MusicPlaylistRepository
from yonerai_discord.modules.music.service import MusicService


def _wav() -> bytes:
    sample_rate = 44_100
    pcm = b"\0" * (sample_rate * 2)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


class _Factory:
    def __init__(self) -> None:
        self.create_calls = 0

    def create(self, _track: Any) -> Any:
        self.create_calls += 1
        raise AssertionError("import must not start playback")

    def create_speech(self, _wav_data: bytes) -> Any:
        raise AssertionError("speech is outside import")


class _Tree:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    def add_command(self, command: Any) -> None:
        self.commands.append(command)

    def remove_command(self, _name: str, **_kwargs: Any) -> Any:
        return self.commands.pop() if self.commands else None


def _runtime(tmp_path: Path) -> tuple[MusicService, MusicPlaylistRepository, LocalMediaLibrary, _Factory]:
    root = tmp_path / "library"
    root.mkdir()
    import_root = root / ".yonerai-imports"
    import_root.mkdir()
    repository = MusicPlaylistRepository(tmp_path / "music.sqlite3")
    repository.open()
    library = LocalMediaLibrary((root,))
    library.refresh()
    factory = _Factory()
    service = MusicService(
        library,
        factory,
        repository,
        available=True,
        reason="ready",
        import_store=MusicImportStore(import_root),
    )
    return service, repository, library, factory


@pytest.mark.asyncio
async def test_service_imports_rights_confirmed_wav_then_existing_play_path_queues_it(
    tmp_path: Path,
) -> None:
    service, repository, _library, factory = _runtime(tmp_path)
    actor = MusicActor(10, None, True)

    asset = await service.import_wav(
        100,
        _wav(),
        "集中用ベル",
        actor,
        commit_check=lambda: actor,
    )

    assert asset.display_title == "集中用ベル"
    assert repository.track_rights_allowed(100, asset.library_ref, asset.content_sha256)
    assert await service.search("集中用ベル", actor, guild_id=100) != ()
    track, position = await service.play(100, "集中用ベル", actor, commit_check=lambda: actor)
    assert (track.title, position) == ("集中用ベル", 1)
    assert factory.create_calls == 0
    projection = repository.load_audio_projection(100)
    assert projection is not None
    assert projection.tracks[0].library_ref == asset.library_ref
    repository.close()


@pytest.mark.asyncio
async def test_import_search_keeps_same_digest_titles_scoped_to_each_guild(tmp_path: Path) -> None:
    service, repository, _library, _factory = _runtime(tmp_path)
    actor = MusicActor(10, None, True)

    await service.import_wav(100, _wav(), "秘密A", actor, commit_check=lambda: actor)

    assert await service.search("秘密A", actor, guild_id=200) == ()
    second = await service.import_wav(200, _wav(), "公開B", actor, commit_check=lambda: actor)
    assert second.display_title == "公開B"
    assert [track.title for track in await service.search("秘密A", actor, guild_id=100)] == ["秘密A"]
    assert [track.title for track in await service.search("公開B", actor, guild_id=200)] == ["公開B"]
    assert await service.search("秘密A", actor, guild_id=200) == ()
    repository.close()


def test_import_search_filters_before_limit_for_later_guild_aliases(tmp_path: Path) -> None:
    repository = MusicPlaylistRepository(tmp_path / "music.sqlite3")
    repository.open()
    for index in range(26):
        digest = f"{index + 1:064x}"
        repository.register_imported_asset_and_grant(
            100,
            ImportedMusicAsset(
                library_ref=f"root-0:.yonerai-imports/{digest}/audio.wav",
                content_sha256=digest,
                display_title=f"曲-{index:02d}",
                size_bytes=44,
                duration_milliseconds=1_000,
            ),
        )

    matches = repository.search_imported_assets_for_guild(100, "曲-25", limit=10)

    assert [asset.display_title for asset in matches] == ["曲-25"]
    repository.close()


@pytest.mark.asyncio
async def test_service_rejects_non_admin_before_private_store_write(tmp_path: Path) -> None:
    service, repository, _library, _factory = _runtime(tmp_path)
    actor = MusicActor(10, None, False)

    with pytest.raises(MusicAuthorizationError):
        await service.import_wav(100, _wav(), "拒否", actor, commit_check=lambda: actor)

    assert repository.list_imported_assets() == ()
    assert tuple((tmp_path / "library" / ".yonerai-imports").iterdir()) == ()
    repository.close()


@pytest.mark.asyncio
async def test_service_rejects_invalid_title_before_private_store_write(tmp_path: Path) -> None:
    service, repository, _library, _factory = _runtime(tmp_path)
    actor = MusicActor(10, None, True)

    with pytest.raises(ValueError):
        await service.import_wav(100, _wav(), "x" * 101, actor, commit_check=lambda: actor)

    store = service.import_store
    assert store is not None and tuple(store.root.iterdir()) == ()
    assert repository.list_imported_assets() == ()
    repository.close()


@pytest.mark.asyncio
async def test_import_cancellation_before_repository_commit_cleans_without_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _library, _factory = _runtime(tmp_path)
    actor = MusicActor(10, None, True)
    store = service.import_store
    assert store is not None
    original_put = store.put_wav
    entered = threading.Event()
    release = threading.Event()

    def blocked_put(data: bytes) -> Any:
        result = original_put(data)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(store, "put_wav", blocked_put)
    task = asyncio.create_task(service.import_wav(100, _wav(), "取消中保存", actor, commit_check=lambda: actor))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert repository.list_imported_assets_for_guild(100) == ()
    assert repository.list_track_rights(100) == ()
    assert tuple(store.root.rglob("audio.wav")) == ()
    repository.close()


@pytest.mark.asyncio
async def test_import_cancellation_during_repository_commit_keeps_file_and_rights_consistent(
    tmp_path: Path,
) -> None:
    service, repository, _library, _factory = _runtime(tmp_path)
    actor = MusicActor(10, None, True)
    main_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()

    def runtime_current() -> bool:
        if threading.get_ident() != main_thread and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return True

    task = asyncio.create_task(
        service.import_wav(
            100,
            _wav(),
            "DB取消中保存",
            actor,
            commit_check=lambda: actor,
            runtime_current=runtime_current,
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assets = repository.list_imported_assets_for_guild(100)
    assert [asset.display_title for asset in assets] == ["DB取消中保存"]
    store = service.import_store
    assert store is not None and store.current_receipt(assets[0].content_sha256) is not None
    assert repository.track_rights_allowed(100, assets[0].library_ref, assets[0].content_sha256)
    repository.close()


@pytest.mark.asyncio
async def test_import_repository_commit_rejects_runtime_identity_change(tmp_path: Path) -> None:
    service, repository, _library, _factory = _runtime(tmp_path)
    actor = MusicActor(10, None, True)
    main_thread = threading.get_ident()

    with pytest.raises(PlaylistError):
        await service.import_wav(
            100,
            _wav(),
            "差替え拒否",
            actor,
            commit_check=lambda: actor,
            runtime_current=lambda: threading.get_ident() == main_thread,
        )

    assert repository.list_imported_assets_for_guild(100) == ()
    assert repository.list_track_rights(100) == ()
    repository.close()


def test_repository_reference_count_protects_rights_and_durable_queue(tmp_path: Path) -> None:
    repository = MusicPlaylistRepository(tmp_path / "music.sqlite3")
    repository.open()
    asset = ImportedMusicAsset(
        library_ref="root-0:.yonerai-imports/" + "a" * 64 + "/audio.wav",
        content_sha256="a" * 64,
        display_title="添付",
        size_bytes=44,
        duration_milliseconds=1_000,
    )
    repository.register_imported_asset_and_grant(100, asset)
    repository.save_audio_projection(
        GuildAudioProjection(
            guild_id=100,
            tracks=(
                PersistedMusicTrackRef(
                    library_ref=asset.library_ref,
                    content_sha256=asset.content_sha256,
                    requester_id=10,
                ),
            ),
        )
    )

    assert repository.imported_asset_reference_count(asset) == 2
    assert repository.imported_digest_reference_count(asset.content_sha256) == 2
    assert repository.revoke_track_rights(100, asset.library_ref)
    assert repository.imported_asset_reference_count(asset) == 1
    assert repository.imported_digest_reference_count(asset.content_sha256) == 1
    assert not repository.delete_imported_asset_if_unreferenced(asset)
    assert repository.delete_audio_projection(100)
    assert repository.imported_asset_reference_count(asset) == 0
    assert repository.imported_digest_reference_count(asset.content_sha256) == 0
    assert repository.imported_digest_reference_count("f" * 64) == 1
    assert repository.imported_digest_reference_count("f" * 64, missing_is_zero=True) == 0
    assert repository.delete_imported_asset_if_unreferenced(asset)
    repository.close()


def test_repository_keeps_imported_display_aliases_scoped_to_the_authorized_guild(tmp_path: Path) -> None:
    repository = MusicPlaylistRepository(tmp_path / "music.sqlite3")
    repository.open()
    library_ref = "root-0:.yonerai-imports/" + "b" * 64 + "/audio.wav"
    digest = "b" * 64
    first = ImportedMusicAsset(
        library_ref=library_ref,
        content_sha256=digest,
        display_title="Guild 100 private title",
        size_bytes=44,
        duration_milliseconds=1_000,
    )
    second = ImportedMusicAsset(
        library_ref=library_ref,
        content_sha256=digest,
        display_title="Guild 200 private title",
        size_bytes=44,
        duration_milliseconds=1_000,
    )

    assert repository.register_imported_asset_and_grant(100, first).display_title == first.display_title
    assert repository.register_imported_asset_and_grant(200, second).display_title == second.display_title
    assert [asset.display_title for asset in repository.list_imported_assets_for_guild(100)] == [first.display_title]
    assert [asset.display_title for asset in repository.search_imported_assets_for_guild(200, "private")] == [
        second.display_title
    ]
    assert repository.resolve_imported_asset_for_guild(100, library_ref, digest) == first
    assert repository.resolve_imported_asset_for_guild(200, library_ref, digest) == second
    assert repository.resolve_imported_asset_for_guild(300, library_ref, digest) is None
    assert repository.list_imported_assets()[0].display_title == "private-import"

    assert repository.revoke_track_rights(100, library_ref)
    assert repository.resolve_imported_asset_for_guild(100, library_ref, digest) is None
    assert repository.resolve_imported_asset_for_guild(200, library_ref, digest) == second
    repository.close()


@pytest.mark.asyncio
async def test_plugin_allows_empty_private_library_to_import_and_cleans_unreferenced_asset(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    bot = SimpleNamespace(
        tree=_Tree(),
        settings=SimpleNamespace(
            music_enabled=True,
            music_library_roots=(root,),
            music_database_path=tmp_path / "music.sqlite3",
        ),
        music_source_factory=_Factory(),
        speech_queue=None,
    )
    plugin = MusicPlugin()
    await plugin.start(bot)

    assert plugin.service.available
    assert plugin.service.import_available
    assert bot.runtime_capability_readiness["cap-run-music-import"] is True
    actor = MusicActor(10, None, True)
    asset = await plugin.service.import_wav(100, _wav(), "一時曲", actor, commit_check=lambda: actor)
    assert plugin.repository is not None
    assert plugin.repository.revoke_track_rights(100, asset.library_ref)
    imported_file = root / ".yonerai-imports" / asset.content_sha256 / "audio.wav"
    assert imported_file.is_file()

    await plugin.stop()

    assert not imported_file.exists()


@pytest.mark.asyncio
async def test_plugin_cleanup_keeps_import_while_durable_queue_references_it(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    bot = SimpleNamespace(
        tree=_Tree(),
        settings=SimpleNamespace(
            music_enabled=True,
            music_library_roots=(root,),
            music_database_path=tmp_path / "music.sqlite3",
        ),
        music_source_factory=_Factory(),
        speech_queue=None,
    )
    plugin = MusicPlugin()
    await plugin.start(bot)
    actor = MusicActor(10, None, True)
    asset = await plugin.service.import_wav(100, _wav(), "保持曲", actor, commit_check=lambda: actor)
    await plugin.service.play(100, "保持曲", actor, commit_check=lambda: actor)
    assert plugin.repository is not None
    assert plugin.repository.revoke_track_rights(100, asset.library_ref)
    imported_file = root / ".yonerai-imports" / asset.content_sha256 / "audio.wav"

    await plugin.stop()

    assert imported_file.is_file()


@pytest.mark.asyncio
async def test_imported_display_title_is_restored_after_plugin_restart(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    settings = SimpleNamespace(
        music_enabled=True,
        music_library_roots=(root,),
        music_database_path=tmp_path / "music.sqlite3",
    )
    first_bot = SimpleNamespace(
        tree=_Tree(),
        settings=settings,
        music_source_factory=_Factory(),
        speech_queue=None,
    )
    first = MusicPlugin()
    await first.start(first_bot)
    actor = MusicActor(10, None, True)
    await first.service.import_wav(100, _wav(), "再起動後タイトル", actor, commit_check=lambda: actor)
    await first.stop()

    second_bot = SimpleNamespace(
        tree=_Tree(),
        settings=settings,
        music_source_factory=_Factory(),
        speech_queue=None,
    )
    second = MusicPlugin()
    await second.start(second_bot)

    matches = await second.service.search(
        "再起動後タイトル",
        MusicActor(10, None),
        guild_id=100,
        limit=10,
    )
    assert [track.title for track in matches] == ["再起動後タイトル"]
    await second.stop()


class _Guard:
    def __init__(self) -> None:
        self.allowed = True

    async def actor(self, _interaction: Any) -> Any:
        return SimpleNamespace(level=RbacLevel.GUILD_ADMIN)

    def currently_allowed(self, _capability_id: str, **_kwargs: Any) -> bool:
        return self.allowed


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


class _Attachment:
    content_type = "audio/wav"
    filename = "sample.wav"

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.size = len(data)
        self.read_calls = 0
        self.after_read = None

    async def read(self) -> bytes:
        self.read_calls += 1
        if self.after_read is not None:
            self.after_read()
        return self.data


class _ImportService:
    available = True
    reason = "ready"

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, MusicActor]] = []

    async def import_wav(
        self,
        guild_id: int,
        _data: bytes,
        title: str,
        actor: MusicActor,
        *,
        commit_check: Any,
        runtime_current: Any,
    ) -> Any:
        assert await commit_check() == actor
        assert runtime_current() is True
        self.calls.append((guild_id, title, actor))
        return SimpleNamespace(display_title=title)


def _interaction(service: _ImportService, guard: _Guard) -> tuple[Any, MusicGroup]:
    member = SimpleNamespace(
        id=10,
        voice=SimpleNamespace(channel=None),
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=True),
    )

    class Guild:
        id = 100

        async def fetch_member(self, user_id: int) -> Any:
            assert user_id == member.id
            return member

    guild = Guild()
    interaction = SimpleNamespace(
        guild_id=100,
        guild=guild,
        channel_id=200,
        user=member,
        response=_Response(),
        followup=_Followup(),
    )
    bot = SimpleNamespace(
        speech_queue=None,
        capability_guard=guard,
        music_service=service,
        is_closing=False,
    )
    return interaction, MusicGroup(bot, service)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_slash_import_reads_once_and_returns_no_path_or_digest() -> None:
    service = _ImportService()
    guard = _Guard()
    interaction, group = _interaction(service, guard)
    attachment = _Attachment(_wav())

    await group.import_audio.callback(group, interaction, attachment, "会議音声", True)

    assert attachment.read_calls == 1
    assert service.calls == [(100, "会議音声", MusicActor(10, None, True))]
    content, kwargs = interaction.followup.messages[0]
    assert "会議音声" in content
    assert "sha256" not in content.casefold()
    assert "root-" not in content
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


@pytest.mark.asyncio
async def test_slash_import_revoked_before_read_keeps_download_and_service_at_zero() -> None:
    service = _ImportService()
    guard = _Guard()
    guard.allowed = False
    interaction, group = _interaction(service, guard)
    attachment = _Attachment(_wav())

    await group.import_audio.callback(group, interaction, attachment, "拒否", True)

    assert attachment.read_calls == 0
    assert service.calls == []


@pytest.mark.asyncio
async def test_slash_import_revoked_during_read_keeps_service_at_zero() -> None:
    service = _ImportService()
    guard = _Guard()
    interaction, group = _interaction(service, guard)
    attachment = _Attachment(_wav())
    attachment.after_read = lambda: setattr(guard, "allowed", False)

    await group.import_audio.callback(group, interaction, attachment, "拒否", True)

    assert attachment.read_calls == 1
    assert service.calls == []


@pytest.mark.asyncio
async def test_slash_import_service_hot_swap_during_read_keeps_old_service_at_zero() -> None:
    service = _ImportService()
    guard = _Guard()
    interaction, group = _interaction(service, guard)
    attachment = _Attachment(_wav())
    attachment.after_read = lambda: setattr(group.bot, "music_service", object())

    await group.import_audio.callback(group, interaction, attachment, "差替え拒否", True)

    assert attachment.read_calls == 1
    assert service.calls == []
