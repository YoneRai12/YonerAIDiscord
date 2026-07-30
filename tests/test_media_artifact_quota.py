from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import yonerai_discord.modules.media_pipeline.artifacts as media_artifacts
from yonerai_discord.modules.media_pipeline.artifacts import MediaArtifactStore
from yonerai_discord.modules.media_pipeline.domain import (
    ArtifactKind,
    ArtifactScope,
    MediaIntegrityError,
    MediaPipelineError,
    MediaValidationError,
)
from yonerai_discord.modules.media_pipeline.index import MediaArtifactLimits
from yonerai_discord.modules.media_pipeline.plugin import MEDIA_PIPELINE_MODULE_ID, MediaPipelinePlugin


MIB = 1024 * 1024


def _scope(
    request_id: str,
    *,
    guild_id: int | None = 10,
    channel_id: int = 20,
    user_id: int = 30,
) -> ArtifactScope:
    return ArtifactScope(request_id, guild_id, channel_id, user_id)


def _limits(**overrides: int) -> MediaArtifactLimits:
    values = {
        "global_count": 10,
        "global_bytes": 64 * MIB,
        "guild_count": 10,
        "guild_bytes": 64 * MIB,
        "user_count": 10,
        "user_bytes": 64 * MIB,
        "request_count": 10,
        "request_bytes": 64 * MIB,
    }
    values.update(overrides)
    return MediaArtifactLimits(**values)


def _commit(
    store: MediaArtifactStore,
    scope: ArtifactScope,
    marker: int,
):
    image = Image.new("RGB", (64, 64), (marker, marker, marker))
    try:
        return store.commit_image(
            image,
            scope=scope,
            recipe_digest=f"{marker:064x}",
            kind=ArtifactKind.IMAGE,
            commit_check=lambda: True,
        )
    finally:
        image.close()


@pytest.mark.parametrize(
    ("limited", "first_scope", "second_scope"),
    (
        (
            {"global_count": 1},
            _scope("global-1", guild_id=10, user_id=30),
            _scope("global-2", guild_id=11, user_id=31),
        ),
        (
            {"request_count": 1},
            _scope("request-1"),
            _scope("request-1"),
        ),
        (
            {"guild_count": 1},
            _scope("guild-1", guild_id=10, user_id=30),
            _scope("guild-2", guild_id=10, user_id=31),
        ),
        (
            {"user_count": 1},
            _scope("user-1", guild_id=10, user_id=30),
            _scope("user-2", guild_id=11, user_id=30),
        ),
    ),
)
def test_each_count_cap_fails_closed_without_evicting(
    tmp_path: Path,
    limited: dict[str, int],
    first_scope: ArtifactScope,
    second_scope: ArtifactScope,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    store = MediaArtifactStore(
        root,
        database_path=tmp_path / "suite.sqlite3",
        limits=_limits(**limited),
    )
    try:
        first = _commit(store, first_scope, 1)
        with pytest.raises(MediaValidationError, match="quota"):
            _commit(store, second_scope, 2)
        assert store.read_png(first, scope=first_scope)
        assert tuple(root.glob("*.png")) == (root / f"{first.artifact_id}.png",)
    finally:
        store.close()


@pytest.mark.parametrize(
    "limited",
    (
        {"global_bytes": 1},
        {"request_bytes": 1},
        {"guild_bytes": 1},
        {"user_bytes": 1},
    ),
)
def test_each_byte_cap_rejects_before_publish(tmp_path: Path, limited: dict[str, int]) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    store = MediaArtifactStore(
        root,
        database_path=tmp_path / "suite.sqlite3",
        limits=_limits(**limited),
    )
    try:
        with pytest.raises(MediaValidationError, match="quota"):
            _commit(store, _scope("byte-cap"), 3)
        assert tuple(root.iterdir()) == ()
    finally:
        store.close()


def test_ttl_cleanup_survives_restart_and_removes_only_expired_owned_file(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = tmp_path / "suite.sqlite3"
    now = [100]
    scope = _scope("ttl")
    store = MediaArtifactStore(root, database_path=database, ttl_seconds=50, clock=lambda: now[0])
    ref = _commit(store, scope, 4)
    store.close()
    assert (root / f"{ref.artifact_id}.png").is_file()

    now[0] = 151
    restarted = MediaArtifactStore(root, database_path=database, ttl_seconds=50, clock=lambda: now[0])
    try:
        assert tuple(root.iterdir()) == ()
        with pytest.raises(MediaIntegrityError, match="unavailable"):
            restarted.read_png(ref, scope=scope)
    finally:
        restarted.close()


def test_ttl_db_commit_failure_keeps_indexed_file_for_retry(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = tmp_path / "suite.sqlite3"
    now = [100]
    scope = _scope("ttl-db-failure")
    store = MediaArtifactStore(root, database_path=database, ttl_seconds=50, clock=lambda: now[0])
    ref = _commit(store, scope, 4)
    target = root / f"{ref.artifact_id}.png"
    now[0] = 151
    original_connection = store._index._connection  # type: ignore[attr-defined]
    assert original_connection is not None

    class CommitFailingConnection:
        def __init__(self, delegate) -> None:
            self._delegate = delegate

        def __getattr__(self, name: str):
            return getattr(self._delegate, name)

        def commit(self) -> None:
            raise sqlite3.OperationalError("injected commit failure")

    store._index._connection = CommitFailingConnection(original_connection)  # type: ignore[attr-defined]
    try:
        with pytest.raises(MediaPipelineError, match="index operation failed"):
            store.read_png(ref, scope=scope)
    finally:
        store._index._connection = original_connection  # type: ignore[attr-defined]

    try:
        assert target.is_file()
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM media_artifacts WHERE artifact_id = ?",
                (ref.artifact_id,),
            ).fetchone() == (1,)
        with pytest.raises(MediaIntegrityError, match="unavailable"):
            store.read_png(ref, scope=scope)
        assert not target.exists()
    finally:
        store.close()


def test_db_failure_rolls_back_only_the_captured_publish_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    store = MediaArtifactStore(root, database_path=tmp_path / "suite.sqlite3")
    original_insert = store._index.insert  # type: ignore[attr-defined]

    def fail_after_insert(*args, **kwargs):
        original_insert(*args, **kwargs)
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(store._index, "insert", fail_after_insert)  # type: ignore[attr-defined]
    try:
        with pytest.raises(MediaPipelineError, match="index operation failed"):
            _commit(store, _scope("db-failure"), 5)
        assert tuple(root.iterdir()) == ()
        with sqlite3.connect(tmp_path / "suite.sqlite3") as connection:
            assert connection.execute("SELECT COUNT(*) FROM media_artifacts").fetchone() == (0,)
    finally:
        store.close()


def test_startup_recovers_canonical_unindexed_crash_or_upgrade_orphan(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = tmp_path / "suite.sqlite3"
    store = MediaArtifactStore(root, database_path=database)
    ref = _commit(store, _scope("orphan"), 6)
    store.close()
    orphan = root / f"{ref.artifact_id}.png"
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM media_artifacts WHERE artifact_id = ?", (ref.artifact_id,))
        connection.commit()

    restarted = MediaArtifactStore(root, database_path=database)
    try:
        assert not orphan.exists()
        assert tuple(root.iterdir()) == ()
    finally:
        restarted.close()


@pytest.mark.parametrize("payload", (b"", b"partial-crash-write"))
def test_startup_removes_only_exact_owned_regular_crash_temp(tmp_path: Path, payload: bytes) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    temporary = root / f".mp-{'a' * 64}.{'b' * 16}.tmp"
    temporary.write_bytes(payload)

    store = MediaArtifactStore(root, database_path=tmp_path / "suite.sqlite3")
    try:
        assert not temporary.exists()
        assert tuple(root.iterdir()) == ()
    finally:
        store.close()


def test_crash_temp_identity_race_refuses_cleanup_and_preserves_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    temporary = root / f".mp-{'a' * 64}.{'b' * 16}.tmp"
    temporary.write_bytes(b"partial")
    original = media_artifacts._crash_temp_identity
    replaced = False

    def replace_after_capture(path: Path):
        nonlocal replaced
        identity = original(path)
        if path == temporary and not replaced:
            replaced = True
            path.unlink()
            path.write_bytes(b"foreign-replacement")
        return identity

    monkeypatch.setattr(media_artifacts, "_crash_temp_identity", replace_after_capture)
    with pytest.raises(MediaIntegrityError, match="identity"):
        MediaArtifactStore(root, database_path=tmp_path / "suite.sqlite3")
    assert temporary.read_bytes() == b"foreign-replacement"


def test_tampered_index_identifier_cannot_address_or_delete_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = tmp_path / "suite.sqlite3"
    store = MediaArtifactStore(root, database_path=database)
    ref = _commit(store, _scope("tampered-index"), 6)
    store.close()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"must-remain")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE media_artifacts SET artifact_id = ? WHERE artifact_id = ?",
            ("..\\outside", ref.artifact_id),
        )
        connection.commit()

    with pytest.raises(MediaPipelineError):
        MediaArtifactStore(root, database_path=database)
    assert outside.read_bytes() == b"must-remain"
    assert (root / f"{ref.artifact_id}.png").is_file()


@pytest.mark.parametrize(
    "entry_kind",
    ("unknown", "subdir", "symlink", "reparse", "temp_symlink", "temp_reparse"),
)
def test_unknown_redirected_or_subdirectory_entry_is_never_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    target = root / "foreign.txt"
    if entry_kind == "subdir":
        target = root / "foreign"
        target.mkdir()
    elif entry_kind in {"symlink", "reparse", "temp_symlink", "temp_reparse"}:
        if entry_kind.startswith("temp_"):
            target = root / f".mp-{'a' * 64}.{'b' * 16}.tmp"
        else:
            target = root / f"mp-{'a' * 64}.png"
        target.write_bytes(b"foreign")
        original_lstat = os.lstat

        def marked_lstat(path):
            result = original_lstat(path)
            if Path(path) != target:
                return result
            if entry_kind.endswith("symlink"):
                return SimpleNamespace(
                    st_mode=stat.S_IFLNK,
                    st_dev=result.st_dev,
                    st_ino=result.st_ino,
                    st_size=result.st_size,
                    st_file_attributes=0,
                )
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024),
            )

        monkeypatch.setattr(media_artifacts.os, "lstat", marked_lstat)
    else:
        target.write_bytes(b"foreign")

    with pytest.raises(MediaIntegrityError):
        MediaArtifactStore(root, database_path=tmp_path / "suite.sqlite3")
    assert target.exists()


def test_foreign_entry_added_after_startup_poison_reads_without_deletion(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    store = MediaArtifactStore(root, database_path=tmp_path / "suite.sqlite3")
    scope = _scope("read-poison")
    ref = _commit(store, scope, 7)
    foreign = root / "foreign.txt"
    foreign.write_bytes(b"must-remain")
    try:
        with pytest.raises(MediaIntegrityError, match="unknown"):
            store.read_png(ref, scope=scope)
        assert foreign.read_bytes() == b"must-remain"
    finally:
        store.close()


def test_expiry_cleanup_serializes_same_store_read_and_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = tmp_path / "suite.sqlite3"
    now = [100]
    store = MediaArtifactStore(root, database_path=database, ttl_seconds=50, clock=lambda: now[0])
    expired_scope = _scope("expiry-race-old")
    expired = _commit(store, expired_scope, 7)
    expired_path = root / f"{expired.artifact_id}.png"
    now[0] = 151
    unlink_started = threading.Event()
    allow_unlink = threading.Event()
    commit_started = threading.Event()
    original_unlink = media_artifacts._unlink_if_same_file

    def blocked_unlink(path: Path, identity):
        if path == expired_path:
            unlink_started.set()
            assert allow_unlink.wait(timeout=2)
        return original_unlink(path, identity)

    def read_expired() -> str:
        try:
            store.read_png(expired, scope=expired_scope)
        except MediaIntegrityError:
            return "expired"
        return "unexpected"

    def commit_new() -> str:
        commit_started.set()
        _commit(store, _scope("expiry-race-new"), 8)
        return "committed"

    monkeypatch.setattr(media_artifacts, "_unlink_if_same_file", blocked_unlink)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            read_future = executor.submit(read_expired)
            assert unlink_started.wait(timeout=2)
            commit_future = executor.submit(commit_new)
            assert commit_started.wait(timeout=2)
            assert not commit_future.done()
            allow_unlink.set()
            assert read_future.result(timeout=2) == "expired"
            assert commit_future.result(timeout=2) == "committed"
        assert len(tuple(root.glob("*.png"))) == 1
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM media_artifacts").fetchone() == (1,)
    finally:
        allow_unlink.set()
        store.close()


def test_concurrent_stores_serialize_quota_commit(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = tmp_path / "suite.sqlite3"
    limits = _limits(global_count=1)
    left = MediaArtifactStore(root, database_path=database, limits=limits)
    right = MediaArtifactStore(root, database_path=database, limits=limits)

    def run(store: MediaArtifactStore, marker: int) -> str:
        try:
            _commit(store, _scope(f"concurrent-{marker}", guild_id=marker, user_id=marker), marker)
        except MediaValidationError:
            return "denied"
        return "committed"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                executor.map(
                    lambda item: run(*item),
                    ((left, 7), (right, 8)),
                )
            )
        assert sorted(outcomes) == ["committed", "denied"]
        assert len(tuple(root.glob("*.png"))) == 1
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM media_artifacts").fetchone() == (1,)
    finally:
        left.close()
        right.close()


def test_index_has_no_path_and_user_errors_hide_root_digest_and_bytes(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    database = tmp_path / "suite.sqlite3"
    scope = _scope("non-exposure")
    store = MediaArtifactStore(
        root,
        database_path=database,
        limits=_limits(global_bytes=1),
    )
    try:
        with pytest.raises(MediaValidationError) as captured:
            _commit(store, scope, 9)
        rendered = str(captured.value)
        assert str(root) not in rendered
        assert scope.digest not in rendered
        assert "byte" not in rendered.lower()
        with sqlite3.connect(database) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(media_artifacts)").fetchall()}
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(media_artifacts)").fetchall()}
        assert "path" not in columns
        assert {
            "artifact_id",
            "request_id",
            "guild_id",
            "channel_id",
            "user_id",
            "scope_digest",
            "recipe_digest",
            "content_digest",
            "kind",
            "width",
            "height",
            "byte_size",
            "created_at",
            "expires_at",
        } == columns
        assert {
            "idx_media_artifacts_expiry",
            "idx_media_artifacts_request",
            "idx_media_artifacts_guild",
            "idx_media_artifacts_user",
        }.issubset(indexes)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_plugin_resolves_relative_suite_database_before_opening_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Registry:
        def is_module_enabled(self, module_id: str, guild_id: int | None) -> bool:
            assert module_id == MEDIA_PIPELINE_MODULE_ID
            assert guild_id is None
            return True

        def set_runtime_availability(self, capability_id: str, available: bool) -> None:
            assert capability_id
            assert isinstance(available, bool)

    monkeypatch.chdir(tmp_path)
    bot = SimpleNamespace(
        is_closing=False,
        capability_registry=Registry(),
        settings=SimpleNamespace(database_path=Path("data/suite.sqlite3")),
    )
    plugin = MediaPipelinePlugin()
    await plugin.start(bot)
    try:
        assert (tmp_path / "data" / "suite.sqlite3").is_file()
        assert (tmp_path / "data" / "media-pipeline-artifacts").is_dir()
    finally:
        await plugin.stop()


@pytest.mark.asyncio
async def test_plugin_stop_offloads_close_and_cleans_identities_on_cancelled_close_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Registry:
        def is_module_enabled(self, module_id: str, guild_id: int | None) -> bool:
            return module_id == MEDIA_PIPELINE_MODULE_ID and guild_id is None

        def set_runtime_availability(self, capability_id: str, available: bool) -> None:
            assert capability_id
            assert isinstance(available, bool)

    bot = SimpleNamespace(
        is_closing=False,
        capability_registry=Registry(),
        settings=SimpleNamespace(database_path=tmp_path / "suite.sqlite3"),
    )
    plugin = MediaPipelinePlugin()
    await plugin.start(bot)
    store = plugin.store
    assert store is not None
    original_close = store.close
    close_started = threading.Event()
    allow_close = threading.Event()
    pulse_stop = asyncio.Event()
    ticks = 0

    def blocked_close() -> None:
        close_started.set()
        assert allow_close.wait(timeout=2)
        original_close()
        raise RuntimeError("injected close failure")

    async def heartbeat() -> None:
        nonlocal ticks
        while not pulse_stop.is_set():
            ticks += 1
            await asyncio.sleep(0.001)

    monkeypatch.setattr(store, "close", blocked_close)
    pulse = asyncio.create_task(heartbeat())
    stop_task = asyncio.create_task(plugin.stop())
    try:
        assert await asyncio.to_thread(close_started.wait, 2)
        stop_task.cancel()
        before_wait = ticks
        await asyncio.sleep(0.02)
        assert ticks > before_wait
        assert not stop_task.done()
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await stop_task
        assert not hasattr(bot, "media_pipeline_service")
        assert not hasattr(bot, "media_pipeline_store")
        assert not hasattr(bot, "media_pipeline_plugin")
        with pytest.raises(MediaPipelineError, match="closed"):
            store._index._required()  # type: ignore[attr-defined]
    finally:
        allow_close.set()
        pulse_stop.set()
        await pulse
