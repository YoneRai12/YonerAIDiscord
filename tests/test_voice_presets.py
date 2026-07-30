from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from yonerai_discord.modules.voice.presets import (
    DEFAULT_VOICE_PRESET,
    ResolvedVoicePreset,
    SqliteVoicePresetRepository,
    VoicePresetRepositoryError,
    VoicePresetScope,
    VoicePresetValues,
)
from yonerai_discord.modules.voice.read_aloud import SqliteReadAloudRouteRepository


def test_presets_persist_isolate_resolve_and_require_revision_cas(tmp_path: Path) -> None:
    path = tmp_path / "read-aloud.sqlite3"
    repository = SqliteVoicePresetRepository(path)
    repository.open()

    server = repository.set_server(
        1,
        VoicePresetValues(900, 800),
        expected_revision=0,
    )
    own = repository.set_user(
        1,
        10,
        VoicePresetValues(1_200, 1_100),
        expected_revision=0,
    )
    other_guild = repository.set_server(
        2,
        VoicePresetValues(1_500, 700),
        expected_revision=0,
    )
    assert repository.resolve(1, 10) == ResolvedVoicePreset(
        1,
        10,
        own.values,
        VoicePresetScope.USER,
        own.revision,
    )
    assert repository.resolve(1, 11) == ResolvedVoicePreset(
        1,
        11,
        server.values,
        VoicePresetScope.SERVER,
        server.revision,
    )
    assert repository.resolve(2, 10).values == other_guild.values
    assert repository.resolve(3, 10).values == DEFAULT_VOICE_PRESET
    with pytest.raises(VoicePresetRepositoryError, match="preset_revision_conflict"):
        repository.set_user(1, 10, VoicePresetValues(), expected_revision=0)
    repository.close()

    reopened = SqliteVoicePresetRepository(path)
    reopened.open()
    assert reopened.get_server(1) == server
    assert reopened.get_user(1, 10) == own
    assert reopened.clear_user(1, 10, expected_revision=own.revision)
    assert reopened.resolve(1, 10).source is VoicePresetScope.SERVER
    assert reopened.clear_server(1, expected_revision=server.revision)
    assert reopened.resolve(1, 10).source is VoicePresetScope.DEFAULT
    reopened.close()


@pytest.mark.parametrize(
    ("speed_milli", "volume_milli"),
    (
        (499, 1_000),
        (2_001, 1_000),
        (1_000, -1),
        (1_000, 2_001),
        (True, 1_000),
        (1_000, False),
        (1_000.0, 1_000),
        (1_000, float("nan")),
    ),
)
def test_preset_values_reject_non_integer_and_out_of_bounds(
    speed_milli: object,
    volume_milli: object,
) -> None:
    with pytest.raises(ValueError):
        VoicePresetValues(speed_milli=speed_milli, volume_milli=volume_milli)  # type: ignore[arg-type]


def test_corrupt_preset_fails_closed_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "read-aloud.sqlite3"
    repository = SqliteVoicePresetRepository(path)
    repository.open()
    stored = repository.set_server(1, VoicePresetValues(), expected_revision=0)
    repository.close()

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA ignore_check_constraints = ON")
    raw.execute("UPDATE voice_read_aloud_server_presets SET speed_milli = 1000.5 WHERE guild_id = 1")
    raw.commit()
    raw.close()

    repository.open()
    with pytest.raises(VoicePresetRepositoryError, match="preset_corrupt"):
        repository.resolve(1, 10)
    with pytest.raises(VoicePresetRepositoryError, match="preset_corrupt"):
        repository.set_server(1, VoicePresetValues(), expected_revision=stored.revision)
    repository.close()

    raw = sqlite3.connect(path)
    assert raw.execute(
        "SELECT speed_milli, volume_milli, revision FROM voice_read_aloud_server_presets WHERE guild_id = 1"
    ).fetchone() == (1000.5, 1_000, 1)
    assert not {
        "text",
        "content",
        "secret",
        "speaker_id",
        "voice_name",
    }.intersection({row[1] for row in raw.execute("PRAGMA table_info(voice_read_aloud_server_presets)")})
    raw.close()


def test_preset_schema_is_independent_from_route_policy_schema_v2(tmp_path: Path) -> None:
    path = tmp_path / "shared.sqlite3"
    routes = SqliteReadAloudRouteRepository(path)
    routes.open()
    routes.put(
        guild_id=1,
        source_channel_id=2,
        destination_voice_channel_id=3,
        enabled=True,
    )
    routes.set_dictionary(1, "ABC", "えーびーしー", expected_revision=0)

    presets = SqliteVoicePresetRepository(path)
    presets.open()
    presets.set_server(1, VoicePresetValues(1_100, 900), expected_revision=0)
    assert routes.get(1, 2) is not None
    assert routes.get_policy(1).revision == 1
    assert presets.resolve(1, 10).values == VoicePresetValues(1_100, 900)
    presets.close()
    routes.close()

    routes.open()
    assert routes.get(1, 2) is not None
    assert routes.get_policy(1).revision == 1
    routes.close()
    presets.open()
    assert presets.resolve(1, 10).values == VoicePresetValues(1_100, 900)
    presets.close()


def test_preset_schema_version_is_strict_and_unsupported_versions_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema.sqlite3"
    repository = SqliteVoicePresetRepository(path)
    repository.open()
    repository.close()

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA ignore_check_constraints = ON")
    raw.execute("UPDATE voice_read_aloud_preset_schema SET schema_version = 1.5")
    raw.commit()
    raw.close()
    with pytest.raises(VoicePresetRepositoryError, match="preset_schema_version_corrupt"):
        repository.open()

    raw = sqlite3.connect(path)
    raw.execute("UPDATE voice_read_aloud_preset_schema SET schema_version = 2")
    raw.commit()
    raw.close()
    with pytest.raises(VoicePresetRepositoryError, match="preset_schema_version_unsupported"):
        repository.open()
