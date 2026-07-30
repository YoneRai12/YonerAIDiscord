from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from yonerai_discord.db import MIGRATIONS


def _module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "runtime_preflight.py"
    spec = importlib.util.spec_from_file_location("runtime_preflight", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_preflight_reports_only_missing_field_names(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    report = _module().inspect_environment(
        root,
        {
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 2
    assert {"DISCORD_TOKEN", "DISCORD_GUILD_ID または COMMAND_SYNC_GUILD_IDS", "BOT_OWNER_IDS"} <= set(
        report.waiting_for_human
    )
    assert not report.warnings
    assert not report.errors
    assert "preflight-missing-token" not in repr(report)


def test_preflight_accepts_complete_offline_configuration(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "123456789",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "AI_ALLOW_REMOTE": "false",
            "AI_SAFETY_IDENTIFIER_SECRET": "separate-hmac-secret",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 0
    assert not report.waiting_for_human
    assert not report.warnings
    assert not report.errors


def test_remote_preflight_distinguishes_blank_fallback_strong_secret_and_weak_secret(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    base = {
        "DISCORD_TOKEN": "offline-token",
        "DISCORD_GUILD_ID": "123456789",
        "BOT_OWNER_IDS": "987654321",
        "AI_BASE_URL": "https://api.openai.com/v1",
        "AI_ALLOW_REMOTE": "true",
        "OPENAI_API_KEY": "offline-key",
        "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
        "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
        "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
    }

    blank = _module().inspect_environment(root, base)
    assert blank.exit_code == 0
    assert "AI_SAFETY_IDENTIFIER_SECRET（推奨・API key fallbackから用途分離）" in blank.warnings

    strong = _module().inspect_environment(
        root,
        base | {"AI_SAFETY_IDENTIFIER_SECRET": "v7Kp2mQ9xL4sN8dR5tW1yH6cF3zB0jUa"},
    )
    assert strong.exit_code == 0
    assert not any("AI_SAFETY_IDENTIFIER_SECRET" in item for item in strong.warnings)
    assert not strong.errors

    weak_value = "weak-secret"
    weak = _module().inspect_environment(root, base | {"AI_SAFETY_IDENTIFIER_SECRET": weak_value})
    assert weak.exit_code == 1
    assert any("AI_SAFETY_IDENTIFIER_SECRET" in item for item in weak.errors)
    assert weak_value not in repr(weak)


def test_music_preflight_verifies_voice_dependencies_ffmpeg_and_authorized_library(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    library = tmp_path / "music"
    library.mkdir()
    (library / "authorized.wav").write_bytes(b"RIFF" + b"\0" * 64)
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"MZ")
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "123456789",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "MUSIC_ENABLED": "true",
            "MUSIC_LIBRARY_ROOTS": str(library),
            "MUSIC_FFMPEG_PATH": str(ffmpeg),
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 0
    assert any(item.startswith("Discord voice依存 PyNaCl 1.6.") for item in report.ok)
    assert "Discord DAVE依存 davey" in report.ok
    assert "FFmpeg executable" in report.ok
    assert "authorized music library" in report.ok


def test_music_preflight_warns_when_authorized_library_is_empty(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    library = tmp_path / "music"
    library.mkdir()
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"MZ")
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "123456789",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "MUSIC_ENABLED": "true",
            "MUSIC_LIBRARY_ROOTS": str(library),
            "MUSIC_FFMPEG_PATH": str(ffmpeg),
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 0
    assert "MUSIC_LIBRARY_ROOTS（許可済み音源0件）" in report.warnings
    assert not report.waiting_for_human
    assert not report.errors


def test_music_preflight_warns_when_library_roots_are_unset(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"MZ")
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "123456789",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "MUSIC_ENABLED": "true",
            "MUSIC_FFMPEG_PATH": str(ffmpeg),
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 0
    assert "MUSIC_LIBRARY_ROOTS" in report.warnings
    assert not report.waiting_for_human
    assert not report.errors


def test_preflight_version_comparison_rejects_vulnerable_or_prerelease_pynacl() -> None:
    module = _module()

    assert module._version_at_least("1.6.2", (1, 6, 2))
    assert module._version_at_least("1.7.0", (1, 6, 2))
    assert not module._version_at_least("1.5.0", (1, 6, 2))
    assert not module._version_at_least("1.6.2rc1", (1, 6, 2))


def test_music_preflight_blocks_an_installed_vulnerable_pynacl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    root = Path(__file__).parents[1]
    library = tmp_path / "music"
    library.mkdir()
    (library / "authorized.wav").write_bytes(b"RIFF" + b"\0" * 64)
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"MZ")
    real_version = module.metadata.version

    monkeypatch.setattr(
        module.metadata,
        "version",
        lambda name: "1.5.0" if name == "PyNaCl" else real_version(name),
    )
    report = module.inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "123456789",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "MUSIC_ENABLED": "true",
            "MUSIC_LIBRARY_ROOTS": str(library),
            "MUSIC_FFMPEG_PATH": str(ffmpeg),
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 1
    assert "Discord voice依存 PyNaCl>=1.6.2" in report.errors


def test_preflight_treats_zero_guild_id_as_missing(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "0",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 2
    assert report.waiting_for_human == ("DISCORD_GUILD_ID または COMMAND_SYNC_GUILD_IDS",)
    assert not report.warnings
    assert not report.errors


def test_preflight_allows_explicit_global_sync_without_guild_id(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "0",
            "SYNC_GLOBAL_COMMANDS": "true",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 0
    assert not report.waiting_for_human
    assert not report.warnings
    assert not report.errors


def test_preflight_allows_command_sync_guild_ids_without_legacy_guild_id(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "COMMAND_SYNC_GUILD_IDS": "123456789, 987654321, 123456789",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 0
    assert not report.waiting_for_human
    assert not report.warnings
    assert not report.errors


def test_preflight_environment_precedence_matches_runtime(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DISCORD_GUILD_ID=123\nSYNC_GLOBAL_COMMANDS=false\n", encoding="utf-8")

    values = _module().load_effective_environment(
        env_file,
        {"DISCORD_GUILD_ID": "456", "SYNC_GLOBAL_COMMANDS": "true"},
    )

    assert values["DISCORD_GUILD_ID"] == "456"
    assert values["SYNC_GLOBAL_COMMANDS"] == "true"


def test_identity_preflight_lists_human_fields_without_secret_values(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "123456789",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "IDENTITY_ENABLED": "true",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert {
        "ENABLED_PLUGINS（identityを含める）",
        "IDENTITY_PUBLIC_BASE_URL",
        "IDENTITY_TURNSTILE_SECRET",
    } <= set(report.waiting_for_human)
    assert not report.errors


def test_identity_preflight_rejects_insecure_public_url_without_echoing_it(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    unsafe_url = "http://public.example.test"
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "123456789",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "ENABLED_PLUGINS": "identity",
            "IDENTITY_ENABLED": "true",
            "IDENTITY_PUBLIC_BASE_URL": unsafe_url,
            "IDENTITY_TURNSTILE_SECRET": "test-secret",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 1
    assert "本人確認URL/Turnstile境界" in report.errors
    assert unsafe_url not in repr(report)


def test_yonerai_remote_preflight_stops_at_missing_official_contract(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "123456789",
            "BOT_OWNER_IDS": "987654321",
            "AI_BASE_URL": "http://127.0.0.1:1234/v1",
            "ENABLED_PLUGINS": "yonerai",
            "YONERAI_ENABLED": "true",
            "YONERAI_ALLOW_REMOTE": "true",
            "YONERAI_REMOTE_STATUS_OPT_IN": "true",
            "YONERAI_AUTH_TOKEN": "must-not-be-rendered",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert "YonerAI公式readiness contract/adapter" in report.waiting_for_human
    assert "must-not-be-rendered" not in repr(report)
    assert not report.errors


def test_direct_core_preflight_ignores_unselected_local_provider_and_legacy_adapter(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    report = _module().inspect_environment(
        root,
        {
            "DISCORD_TOKEN": "offline-token",
            "DISCORD_GUILD_ID": "123456789",
            "BOT_OWNER_IDS": "987654321",
            "ENABLED_PLUGINS": "ai",
            "AI_EXECUTION_TOPOLOGY": "direct_core",
            "AI_HOSTING_PROFILE": "official_managed",
            "AI_PACKAGING_CANDIDATE": "official_private",
            "AI_BASE_URL": "https://unused-local-provider.example.test/v1",
            "AI_ALLOW_REMOTE": "true",
            "OPENAI_API_KEY": "",
            "YONERAI_ENABLED": "true",
            "YONERAI_ALLOW_REMOTE": "true",
            "YONERAI_REMOTE_STATUS_OPT_IN": "true",
            "YONERAI_AUTH_TOKEN": "offline-direct-core-token",
            "YONERAI_CORE_ORIGIN": "https://core.example.test",
            "CAPABILITY_CATALOG_PATH": str(root / "docs" / "CAPABILITY_COUNTS.json"),
            "DATABASE_PATH": str(tmp_path / "new.sqlite3"),
            "LOG_PATH": str(tmp_path / "logs" / "bot.jsonl"),
        },
    )

    assert report.exit_code == 0
    assert "Direct Core runtime構成" in report.ok
    assert "Local AI provider未選択" in report.ok
    assert "YonerAI Direct Core opt-in" in report.ok
    assert "OPENAI_API_KEY" not in report.waiting_for_human
    assert "ENABLED_PLUGINS（yoneraiを含める）" not in report.waiting_for_human
    assert "YonerAI公式readiness contract/adapter" not in report.waiting_for_human
    assert not report.errors


def test_preflight_json_is_secret_free_and_keeps_the_report_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    hidden_value = "must-never-appear-in-current-truth"
    report = module.PreflightReport(
        ok=("configuration",),
        waiting_for_human=("DISCORD_TOKEN",),
        warnings=(),
        errors=(),
    )
    values = {
        "AI_BASE_URL": "http://127.0.0.1:1234/v1",
        "OPENAI_API_KEY": hidden_value,
        "CAPABILITY_CATALOG_PATH": str(Path(__file__).parents[1] / "docs" / "CAPABILITY_COUNTS.json"),
    }
    monkeypatch.setattr(module, "load_effective_environment", lambda _path: values)
    monkeypatch.setattr(module, "inspect_environment", lambda _root, _values: report)
    monkeypatch.setattr(sys, "argv", ["runtime_preflight.py", "--json"])

    assert module.main() == report.exit_code == 2
    rendered = capsys.readouterr().out.strip()
    payload = json.loads(rendered)
    assert payload["preflight"]["exit_code"] == 2
    assert payload["current_truth"]["selection_configured"] is False
    assert payload["current_truth"]["selected_topology"] is None
    assert payload["current_truth"]["effective_topology"] == "local_standalone"
    assert hidden_value not in rendered


def test_preflight_current_truth_projects_explicit_local_profile_without_a_live_claim() -> None:
    truth = _module().build_preflight_current_truth(
        {
            "AI_EXECUTION_TOPOLOGY": "local_standalone",
            "AI_HOSTING_PROFILE": "full_private_self_host",
            "AI_PACKAGING_CANDIDATE": "local_only",
        }
    )

    assert truth.selection_configured is True
    assert truth.selected_topology.value == "local_standalone"
    assert truth.selected_hosting_profile.value == "full_private_self_host"
    assert truth.selected_packaging.value == "local_only"
    assert truth.effective_topology.value == "local_standalone"
    assert truth.available_ports == ("local",)
    assert truth.required_ports == ("local",)
    assert truth.missing_ports == ()
    assert all(
        source.live_success is None
        for source in (
            truth.provider_source,
            truth.sandbox_source,
            truth.jobs_source,
            truth.audit_source,
        )
    )


@pytest.mark.parametrize(
    ("topology", "hosting_profile", "packaging", "required_ports"),
    (
        ("direct_core", "official_managed", "official_private", ("direct_core",)),
        ("discord_processing", "official_managed", "public_safe_shared", ("discord_processing",)),
        (
            "hybrid",
            "official_hybrid_private",
            "official_private",
            ("hybrid_core", "hybrid_selector", "local"),
        ),
    ),
)
def test_preflight_current_truth_does_not_invent_nonlocal_ports_or_fallback(
    topology: str,
    hosting_profile: str,
    packaging: str,
    required_ports: tuple[str, ...],
) -> None:
    truth = _module().build_preflight_current_truth(
        {
            "AI_EXECUTION_TOPOLOGY": topology,
            "AI_HOSTING_PROFILE": hosting_profile,
            "AI_PACKAGING_CANDIDATE": packaging,
        }
    )

    assert truth.selection_configured is True
    assert truth.selected_topology.value == topology
    assert truth.selected_hosting_profile.value == hosting_profile
    assert truth.selected_packaging.value == packaging
    assert truth.effective_topology.value == topology
    assert truth.available_ports == ()
    assert truth.required_ports == required_ports
    assert truth.missing_ports == required_ports
    assert tuple(blocker for blocker in truth.blockers if blocker.startswith("missing_port.")) == tuple(
        f"missing_port.{port}" for port in required_ports
    )
    assert all(
        source.live_success is None
        for source in (
            truth.provider_source,
            truth.sandbox_source,
            truth.jobs_source,
            truth.audit_source,
        )
    )


@pytest.mark.parametrize("packaging", ("official_private", "local_only"))
def test_preflight_current_truth_projects_code_owned_direct_core_without_live_claim(
    packaging: str,
) -> None:
    truth = _module().build_preflight_current_truth(
        {
            "AI_EXECUTION_TOPOLOGY": "direct_core",
            "AI_HOSTING_PROFILE": "official_managed",
            "AI_PACKAGING_CANDIDATE": packaging,
            "YONERAI_ENABLED": "true",
            "YONERAI_ALLOW_REMOTE": "true",
            "YONERAI_REMOTE_STATUS_OPT_IN": "true",
            "YONERAI_AUTH_TOKEN": "offline-direct-core-token",
            "YONERAI_CORE_ORIGIN": "https://core.example.test",
        }
    )

    assert truth.available_ports == ("direct_core",)
    assert truth.required_ports == ("direct_core",)
    assert truth.missing_ports == ()
    assert truth.provider_source.configured is True
    assert truth.provider_source.ready is False
    assert truth.provider_source.live_success is None


@pytest.mark.parametrize("packaging", ("public_safe_shared", "undecided"))
def test_preflight_current_truth_does_not_project_direct_core_for_incompatible_packaging(
    packaging: str,
) -> None:
    truth = _module().build_preflight_current_truth(
        {
            "AI_EXECUTION_TOPOLOGY": "direct_core",
            "AI_HOSTING_PROFILE": "official_managed",
            "AI_PACKAGING_CANDIDATE": packaging,
            "YONERAI_ENABLED": "true",
            "YONERAI_ALLOW_REMOTE": "true",
            "YONERAI_REMOTE_STATUS_OPT_IN": "true",
            "YONERAI_AUTH_TOKEN": "offline-direct-core-token",
            "YONERAI_CORE_ORIGIN": "https://core.example.test",
        }
    )

    assert truth.available_ports == ()
    assert truth.required_ports == ("direct_core",)
    assert truth.missing_ports == ("direct_core",)
    assert truth.provider_source.configured is False
    assert truth.provider_source.ready is False
    assert truth.provider_source.live_success is None


def test_preflight_normal_output_and_exit_code_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    report = module.PreflightReport(
        ok=("one",),
        waiting_for_human=("two",),
        warnings=("three",),
        errors=(),
    )
    monkeypatch.setattr(module, "load_effective_environment", lambda _path: {})
    monkeypatch.setattr(module, "inspect_environment", lambda _root, _values: report)
    monkeypatch.setattr(sys, "argv", ["runtime_preflight.py"])

    assert module.main() == 2
    assert capsys.readouterr().out.splitlines() == ["OK: one", "WAIT: two", "WARN: three"]


def test_self_host_smoke_is_not_run_without_explicit_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    report = module.PreflightReport(ok=(), waiting_for_human=(), warnings=(), errors=())
    calls = 0

    def forbidden_smoke() -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("self-host smoke must be opt-in")

    monkeypatch.setattr(module, "run_self_host_smoke", forbidden_smoke)
    monkeypatch.setattr(module, "load_effective_environment", lambda _path: {})
    monkeypatch.setattr(module, "inspect_environment", lambda _root, _values: report)
    monkeypatch.setattr(sys, "argv", ["runtime_preflight.py"])

    assert module.main() == 0
    assert calls == 0


def test_self_host_smoke_migrates_disposable_database_and_reports_fixed_json(
    tmp_path: Path,
) -> None:
    module = _module()

    result = module.run_self_host_smoke(temp_root=tmp_path)
    payload = json.loads(module.render_self_host_smoke_json(result))

    assert result.success is True
    assert result.migration_version == max(migration.version for migration in MIGRATIONS)
    assert payload == {
        "error": None,
        "migration_version": result.migration_version,
        "quick_check": "ok",
        "schema_version": "yonerai.discord.self-host-smoke.v1",
        "success": True,
    }
    assert list(tmp_path.iterdir()) == []


def test_self_host_smoke_does_not_read_or_change_configured_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    configured_database = tmp_path / "configured.sqlite3"
    original = b"configured-database-must-not-change"
    configured_database.write_bytes(original)
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_PATH={configured_database}\n", encoding="utf-8")
    real_smoke = module.run_self_host_smoke

    monkeypatch.setattr(
        module,
        "run_self_host_smoke",
        lambda: real_smoke(temp_root=tmp_path / "disposable"),
    )
    monkeypatch.setattr(
        module,
        "load_effective_environment",
        lambda _path: (_ for _ in ()).throw(AssertionError("environment must not be read")),
    )
    (tmp_path / "disposable").mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        ["runtime_preflight.py", "--self-host-smoke", "--json", "--env-file", str(env_file)],
    )

    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)["success"] is True
    assert configured_database.read_bytes() == original
    assert list((tmp_path / "disposable").iterdir()) == []


def test_self_host_smoke_returns_fixed_failure_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    created_paths: list[Path] = []
    closed: list[bool] = []

    class FailingDatabase:
        def __init__(self, path: Path) -> None:
            self.path = path
            created_paths.append(path)

        def open(self) -> None:
            self.path.write_bytes(b"temporary")

        def migrate(self) -> int:
            raise RuntimeError("private migration failure details")

        def quick_check(self) -> tuple[str, ...]:
            raise AssertionError("quick_check must not run after migration failure")

        def close(self) -> None:
            closed.append(True)

    result = module.run_self_host_smoke(
        temp_root=tmp_path,
        database_factory=FailingDatabase,
    )
    rendered = module.render_self_host_smoke_json(result)

    assert result == module.SelfHostSmokeResult(
        migration_version=0,
        quick_check="not_run",
        success=False,
        error="self_host_smoke_failed",
    )
    assert closed == [True]
    assert created_paths
    assert not created_paths[0].parent.exists()
    assert "private migration failure details" not in rendered
    assert str(created_paths[0]) not in rendered
    monkeypatch.setattr(module, "run_self_host_smoke", lambda: result)
    monkeypatch.setattr(sys, "argv", ["runtime_preflight.py", "--self-host-smoke"])
    assert module.main() == 1
    assert capsys.readouterr().out.strip() == "ERROR: self_host_smoke_failed"
