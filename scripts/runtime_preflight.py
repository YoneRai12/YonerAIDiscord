from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yonerai_discord.capabilities import CATALOG_CONNECTED_CAPABILITY_IDS  # noqa: E402
from yonerai_discord.config import ConfigurationError, Settings  # noqa: E402
from yonerai_discord.control_plane import InMemoryStateStore, load_capability_catalog  # noqa: E402
from yonerai_discord.db import Database  # noqa: E402
from yonerai_discord.deployment_current_truth import (  # noqa: E402
    M10CurrentTruthV1,
    M10SourceTruthV1,
    build_m10_current_truth,
)
from yonerai_discord.modules.ai.execution_profiles import (  # noqa: E402
    ExecutionTopology,
    HostingProfile,
    PackagingCandidate,
)
from yonerai_discord.modules.ai.core_runtime_composition import (  # noqa: E402
    DirectCoreRuntimeCompositionError,
    build_direct_core_gateway_factory,
)
from yonerai_discord.modules.ai.provider import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderConfigurationError,
)
from yonerai_discord.modules.identity.models import IdentityFeatures, IdentityPolicy  # noqa: E402
from yonerai_discord.modules.identity.runtime_config import (  # noqa: E402
    IdentityRuntimeConfig,
    IdentityRuntimeConfigurationError,
)
from yonerai_discord.modules.identity.service import (  # noqa: E402
    InvalidIdentityConfiguration,
    validate_policy,
)
from yonerai_discord.modules.identity.web_adapter import (  # noqa: E402
    IdentityWebConfig,
    IdentityWebConfigurationError,
)
from yonerai_discord.modules.minecraft import MinecraftStatusClient, MinecraftTarget  # noqa: E402
from yonerai_discord.modules.audio_core import LocalMediaLibrary  # noqa: E402
from yonerai_discord.modules.voice.voicevox import VoicevoxClient, VoicevoxConfigurationError  # noqa: E402
from yonerai_discord.modules.yonerai.config import (  # noqa: E402
    YonerAIConfigurationError,
    YonerAIRuntimeConfig,
)
from yonerai_discord.runtime_manifest import register_runtime_capabilities, register_runtime_modules  # noqa: E402


@dataclass(frozen=True, slots=True)
class PreflightReport:
    ok: tuple[str, ...]
    waiting_for_human: tuple[str, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        if self.errors:
            return 1
        if self.waiting_for_human:
            return 2
        return 0


@dataclass(frozen=True, slots=True)
class SelfHostSmokeResult:
    migration_version: int
    quick_check: str
    success: bool
    error: str | None = None
    schema_version: str = "yonerai.discord.self-host-smoke.v1"

    def to_dict(self) -> dict[str, bool | int | str | None]:
        return {
            "error": self.error,
            "migration_version": self.migration_version,
            "quick_check": self.quick_check,
            "schema_version": self.schema_version,
            "success": self.success,
        }


def run_self_host_smoke(
    *,
    temp_root: Path | None = None,
    database_factory: Callable[[Path], Database] = Database,
) -> SelfHostSmokeResult:
    """設定を読まず、一意な一時DBだけでmigrationとquick_checkを検証する。"""

    try:
        with tempfile.TemporaryDirectory(prefix="yonerai-self-host-smoke-", dir=temp_root) as directory:
            database = database_factory(Path(directory) / "smoke.sqlite3")
            try:
                database.open()
                migration_version = database.migrate()
                quick_check = database.quick_check()
            finally:
                database.close()
    except Exception:
        return SelfHostSmokeResult(
            migration_version=0,
            quick_check="not_run",
            success=False,
            error="self_host_smoke_failed",
        )
    success = quick_check == ("ok",)
    return SelfHostSmokeResult(
        migration_version=migration_version,
        quick_check="ok" if success else "failed",
        success=success,
        error=None if success else "self_host_smoke_failed",
    )


def render_self_host_smoke_json(result: SelfHostSmokeResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def inspect_environment(root: Path, values: Mapping[str, str]) -> PreflightReport:
    ok: list[str] = []
    waiting: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    normalized = {str(key): str(value) for key, value in values.items() if value is not None}

    required = ["DISCORD_TOKEN", "BOT_OWNER_IDS"]
    for name in required:
        if not normalized.get(name, "").strip():
            waiting.append(name)

    validation_values = dict(normalized)
    if not validation_values.get("DISCORD_TOKEN", "").strip():
        validation_values["DISCORD_TOKEN"] = "preflight-missing-token"
    try:
        settings = Settings.from_env(validation_values)
    except ConfigurationError as exc:
        errors.append(f"設定形式: {exc}")
        return PreflightReport(tuple(ok), tuple(waiting), tuple(warnings), tuple(errors))
    ok.append("型付き設定")
    local_ai_selected = settings.ai_execution_topology in {
        None,
        ExecutionTopology.LOCAL_STANDALONE.value,
        ExecutionTopology.HYBRID.value,
    }
    if (
        local_ai_selected
        and settings.ai_base_url
        and not _looks_local(settings.ai_base_url)
        and settings.ai_allow_remote
        and not settings.openai_api_key
    ):
        waiting.append("OPENAI_API_KEY")
    direct_core_configured = False
    if settings.ai_execution_topology == ExecutionTopology.DIRECT_CORE.value:
        try:
            build_direct_core_gateway_factory(settings)
        except DirectCoreRuntimeCompositionError:
            errors.append("Direct Core runtime構成")
        else:
            direct_core_configured = True
            ok.append("Direct Core runtime構成")
    elif settings.ai_execution_topology in {
        ExecutionTopology.DISCORD_PROCESSING.value,
        ExecutionTopology.HYBRID.value,
    }:
        waiting.append(f"{settings.ai_execution_topology} production adapter")
    if not settings.command_sync_guild_ids and not settings.sync_global_commands:
        waiting.append("DISCORD_GUILD_ID または COMMAND_SYNC_GUILD_IDS")

    if local_ai_selected and settings.ai_base_url and (_looks_local(settings.ai_base_url) or settings.ai_allow_remote):
        try:
            provider = OpenAICompatibleProvider(
                base_url=settings.ai_base_url,
                openai_api_key=settings.openai_api_key,
                compatible_api_key=settings.ai_api_key,
                allow_remote=settings.ai_allow_remote,
                allow_luna=settings.ai_allow_luna,
                safety_identifier_secret=settings.ai_safety_identifier_secret,
                timeout_seconds=settings.ai_timeout_seconds,
                max_output_tokens=settings.ai_max_output_tokens,
                max_response_bytes=settings.ai_max_response_bytes,
            )
        except ProviderConfigurationError:
            if "OPENAI_API_KEY" not in waiting:
                errors.append("AI endpoint/key境界")
        else:
            del provider
            ok.append("AI endpoint/key境界")
    elif local_ai_selected:
        ok.append("AI remote送信OFF")
    else:
        ok.append("Local AI provider未選択")

    if settings.voice_enabled:
        try:
            VoicevoxClient(
                endpoint=settings.voicevox_url,
                allow_remote=settings.voice_allow_remote,
                timeout_seconds=settings.voice_timeout_seconds,
                max_response_bytes=settings.voice_max_response_bytes,
            )
        except VoicevoxConfigurationError:
            errors.append("VOICEVOX endpoint境界")
        else:
            ok.append("VOICEVOX endpoint境界")
    else:
        ok.append("VOICEVOX既定OFF")

    if settings.music_enabled:
        if "music" not in settings.startup_plugins:
            warnings.append("ENABLED_PLUGINS（musicを含める）")
        if importlib.util.find_spec("nacl") is None:
            warnings.append("Discord voice依存 PyNaCl>=1.6.2")
        else:
            try:
                pynacl_version = metadata.version("PyNaCl")
            except metadata.PackageNotFoundError:
                warnings.append("Discord voice依存 PyNaCl>=1.6.2")
            else:
                if not _version_at_least(pynacl_version, (1, 6, 2)):
                    # 未導入はoptional capabilityのWARNだが、脆弱な旧版を
                    # 実行可能な状態は安全性退行なので起動を止める。
                    errors.append("Discord voice依存 PyNaCl>=1.6.2")
                else:
                    ok.append(f"Discord voice依存 PyNaCl {pynacl_version}")
        if importlib.util.find_spec("davey") is None:
            warnings.append("Discord DAVE依存 davey")
        else:
            ok.append("Discord DAVE依存 davey")

        ffmpeg_value = settings.music_ffmpeg_path or shutil.which("ffmpeg") or ""
        ffmpeg_path = Path(ffmpeg_value).expanduser() if ffmpeg_value else None
        if ffmpeg_path is not None and not ffmpeg_path.is_absolute():
            ffmpeg_path = root / ffmpeg_path
        if ffmpeg_path is None or not ffmpeg_path.is_file():
            warnings.append("MUSIC_FFMPEG_PATH")
        else:
            ok.append("FFmpeg executable")

        roots = tuple(path if path.is_absolute() else root / path for path in settings.music_library_roots)
        if not roots:
            warnings.append("MUSIC_LIBRARY_ROOTS")
        else:
            library = LocalMediaLibrary(
                roots,
                max_files=settings.music_library_max_files,
                max_file_bytes=settings.music_library_max_file_bytes,
            )
            if library.refresh() < 1:
                warnings.append("MUSIC_LIBRARY_ROOTS（許可済み音源0件）")
            else:
                ok.append("authorized music library")
    else:
        ok.append("Music既定OFF")

    if settings.minecraft_enabled:
        try:
            MinecraftStatusClient(
                MinecraftTarget(
                    host=settings.minecraft_host,
                    port=settings.minecraft_port,
                    timeout_seconds=settings.minecraft_timeout_seconds,
                    allow_public=settings.minecraft_allow_public,
                    max_packet_bytes=settings.minecraft_max_packet_bytes,
                )
            )
        except ValueError:
            errors.append("Minecraft接続先境界")
        else:
            ok.append("Minecraft read-only接続先境界")
    else:
        ok.append("Minecraft既定OFF")

    if settings.identity_enabled:
        try:
            identity_config = IdentityRuntimeConfig.load(settings, {})
        except IdentityRuntimeConfigurationError:
            errors.append("本人確認typed設定")
            identity_config = None
        if "identity" not in settings.startup_plugins:
            waiting.append("ENABLED_PLUGINS（identityを含める）")
        if not settings.identity_public_base_url:
            waiting.append("IDENTITY_PUBLIC_BASE_URL")
        if not settings.identity_turnstile_secret and not settings.identity_allow_insecure_localhost:
            waiting.append("IDENTITY_TURNSTILE_SECRET")
        if settings.identity_public_base_url and (
            settings.identity_turnstile_secret or settings.identity_allow_insecure_localhost
        ):
            try:
                validate_policy(
                    IdentityPolicy(
                        public_base_url=settings.identity_public_base_url,
                        features=IdentityFeatures(member_verification=True),
                        captcha_configured=bool(settings.identity_turnstile_secret),
                        allow_insecure_localhost=settings.identity_allow_insecure_localhost,
                    )
                )
            except InvalidIdentityConfiguration:
                errors.append("本人確認URL/Turnstile境界")
            else:
                ok.append("本人確認URL/Turnstile境界")
        if not settings.identity_http_enabled:
            waiting.append("IDENTITY_HTTP_ENABLED（または外部callback adapter）")
        elif identity_config is not None:
            if settings.identity_turnstile_secret and not settings.identity_turnstile_site_key:
                waiting.append("IDENTITY_TURNSTILE_SITE_KEY")
            if settings.identity_public_base_url and (
                settings.identity_turnstile_site_key or settings.identity_allow_insecure_localhost
            ):
                try:
                    IdentityWebConfig(
                        bind_host=settings.identity_http_host,
                        bind_port=settings.identity_http_port,
                        public_base_url=settings.identity_public_base_url,
                        turnstile_site_key=settings.identity_turnstile_site_key,
                        captcha_required=bool(settings.identity_turnstile_secret),
                    )
                except IdentityWebConfigurationError:
                    errors.append("本人確認localhost HTTP境界")
                else:
                    ok.append("本人確認localhost HTTP境界")
    else:
        ok.append("本人確認既定OFF")

    if settings.yonerai_enabled:
        try:
            yonerai_config = YonerAIRuntimeConfig.load(settings, {})
        except YonerAIConfigurationError:
            errors.append("YonerAI typed設定")
        else:
            if direct_core_configured:
                ok.append("YonerAI Direct Core opt-in")
            else:
                if "yonerai" not in settings.startup_plugins:
                    waiting.append("ENABLED_PLUGINS（yoneraiを含める）")
                if yonerai_config.allow_remote and not yonerai_config.remote_status_opt_in:
                    waiting.append("YONERAI_REMOTE_STATUS_OPT_IN")
                if yonerai_config.remote_status_opt_in and not yonerai_config.allow_remote:
                    waiting.append("YONERAI_ALLOW_REMOTE")
                if yonerai_config.remote_permitted:
                    waiting.append("YonerAI公式readiness contract/adapter")
                else:
                    ok.append("YonerAI外部接続なし")
    else:
        ok.append("YonerAI連携既定OFF")

    try:
        registry = load_capability_catalog(
            settings.capability_catalog_path,
            connected_capability_ids=CATALOG_CONNECTED_CAPABILITY_IDS,
            state_store=InMemoryStateStore(),
        )
        register_runtime_modules(registry)
        register_runtime_capabilities(registry)
        registry.validate(raise_on_error=True)
    except Exception:
        errors.append("Capability台帳")
    else:
        canonical = sum(not item.capability_id.startswith("cap-run-") for item in registry.capabilities)
        ok.append(f"Capability台帳 canonical={canonical} runtime={len(registry.capabilities) - canonical}")

    database_path = settings.database_path
    if database_path.exists():
        try:
            uri = database_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                result = tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check"))
        except sqlite3.Error:
            errors.append("既存SQLite quick_check")
        else:
            if result == ("ok",):
                ok.append("既存SQLite quick_check")
            else:
                errors.append("既存SQLite quick_check")
    else:
        ok.append("SQLiteは初回起動時に新規作成")

    if settings.log_path.exists() and settings.log_path.is_symlink():
        errors.append("LOG_PATH symlink")
    elif settings.log_path.parent.exists() and settings.log_path.parent.is_symlink():
        errors.append("LOG_PATH親directory symlink")
    else:
        ok.append("log path境界")

    if _is_tracked(root, ".env"):
        errors.append(".envがGit追跡対象")
    else:
        ok.append(".env Git除外")

    if (
        settings.ai_allow_remote
        and not _looks_local(settings.ai_base_url)
        and bool(settings.openai_api_key)
        and not settings.ai_safety_identifier_secret
    ):
        warnings.append("AI_SAFETY_IDENTIFIER_SECRET（推奨・API key fallbackから用途分離）")
    return PreflightReport(
        tuple(ok),
        tuple(dict.fromkeys(waiting)),
        tuple(dict.fromkeys(warnings)),
        tuple(errors),
    )


def _version_at_least(value: str, minimum: tuple[int, ...]) -> bool:
    """通常のrelease versionだけをfail-closedで比較する。"""

    release = value.split("+", 1)[0].split("-", 1)[0]
    parts = release.split(".")
    try:
        parsed = tuple(int(part) for part in parts[: len(minimum)])
    except ValueError:
        return False
    padded = parsed + (0,) * (len(minimum) - len(parsed))
    return padded >= minimum


def _looks_local(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith("http://127.0.0.1:") or lowered.startswith("http://localhost:")


def _is_tracked(root: Path, relative: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def load_effective_environment(env_file: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """本体のload_dotenv(override=False)と同じく、OS環境を.envより優先する。"""

    file_values = {key: value or "" for key, value in dotenv_values(env_file).items()} if env_file.exists() else {}
    effective = dict(file_values)
    effective.update(os.environ if environ is None else environ)
    return {str(key): str(value) for key, value in effective.items()}


def build_preflight_current_truth(values: Mapping[str, str]) -> M10CurrentTruthV1:
    """Offline設定を、live readinessへ昇格させずに投影する。"""

    normalized = {str(key): str(value) for key, value in values.items() if value is not None}
    validation_values = dict(normalized)
    if not validation_values.get("DISCORD_TOKEN", "").strip():
        validation_values["DISCORD_TOKEN"] = "preflight-missing-token"
    try:
        settings = Settings.from_env(validation_values)
    except ConfigurationError:
        return build_m10_current_truth()

    provider_configured = False
    if settings.ai_base_url:
        try:
            provider = OpenAICompatibleProvider(
                base_url=settings.ai_base_url,
                openai_api_key=settings.openai_api_key,
                compatible_api_key=settings.ai_api_key,
                allow_remote=settings.ai_allow_remote,
                allow_luna=settings.ai_allow_luna,
                safety_identifier_secret=settings.ai_safety_identifier_secret,
                timeout_seconds=settings.ai_timeout_seconds,
                max_output_tokens=settings.ai_max_output_tokens,
                max_response_bytes=settings.ai_max_response_bytes,
            )
        except ProviderConfigurationError:
            pass
        else:
            provider_configured = True
            del provider

    sandbox_configured = bool(
        getattr(settings, "media_url_inspection_use_hyperv", False)
        or (
            getattr(settings, "cloudflare_browser_rendering_allow_remote", False)
            and getattr(settings, "cloudflare_browser_rendering_account_id", None)
            and getattr(settings, "cloudflare_browser_rendering_api_token", "")
        )
    )
    jobs_configured = "jobs" in settings.startup_plugins
    selected_topology = (
        None if settings.ai_execution_topology is None else ExecutionTopology(settings.ai_execution_topology)
    )
    selected_hosting_profile = (
        None if settings.ai_hosting_profile is None else HostingProfile(settings.ai_hosting_profile)
    )
    selected_packaging = (
        None if settings.ai_packaging_candidate is None else PackagingCandidate(settings.ai_packaging_candidate)
    )
    direct_core_configured = False
    if selected_topology is ExecutionTopology.DIRECT_CORE:
        try:
            build_direct_core_gateway_factory(settings)
        except DirectCoreRuntimeCompositionError:
            pass
        else:
            direct_core_configured = True
            provider_configured = True
    if selected_topology is None or selected_topology is ExecutionTopology.LOCAL_STANDALONE:
        available_ports = ("local",)
    elif direct_core_configured:
        available_ports = ("direct_core",)
    else:
        available_ports = ()
    return build_m10_current_truth(
        selected_topology=selected_topology,
        selected_hosting_profile=selected_hosting_profile,
        selected_packaging=selected_packaging,
        # Direct Coreだけはcode-owned factoryの構成条件を静的検査できる。
        # Discord Processing/Hybridは注入objectを観測できないため利用可能としない。
        available_ports=available_ports,
        provider_source=_preflight_source(
            configured=provider_configured,
            configured_blocker="provider_live_probe_not_run",
            unconfigured_blocker="provider_configuration_invalid",
        ),
        sandbox_source=_preflight_source(
            configured=sandbox_configured,
            configured_blocker="sandbox_runtime_not_observed",
            unconfigured_blocker="sandbox_source_not_declared",
        ),
        jobs_source=_preflight_source(
            configured=jobs_configured,
            configured_blocker="jobs_worker_not_observed",
            unconfigured_blocker="jobs_plugin_not_enabled",
        ),
        audit_source=_preflight_source(
            configured=True,
            configured_blocker="audit_source_runtime_not_observed",
            unconfigured_blocker="audit_source_not_declared",
        ),
    )


def render_preflight_json(report: PreflightReport, truth: M10CurrentTruthV1) -> str:
    payload = {
        "current_truth": truth.to_dict(),
        "preflight": {
            "errors": list(report.errors),
            "exit_code": report.exit_code,
            "ok": list(report.ok),
            "waiting_for_human": list(report.waiting_for_human),
            "warnings": list(report.warnings),
        },
        "schema_version": "yonerai.discord.runtime-preflight.v1",
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _preflight_source(
    *,
    configured: bool,
    configured_blocker: str,
    unconfigured_blocker: str,
) -> M10SourceTruthV1:
    return M10SourceTruthV1(
        configured=configured,
        ready=False,
        live_success=None,
        blocker=configured_blocker if configured else unconfigured_blocker,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="秘密値を表示しないDiscord BOT起動前診断")
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--json", action="store_true", help="secret-free deterministic JSONを出力")
    parser.add_argument(
        "--self-host-smoke",
        action="store_true",
        help="設定を読まず一時SQLiteでmigration/quick_checkを検証",
    )
    args = parser.parse_args()
    if args.self_host_smoke:
        smoke = run_self_host_smoke()
        if args.json:
            print(render_self_host_smoke_json(smoke))
        elif smoke.success:
            print(
                "SELF_HOST_SMOKE: "
                f"success=true schema={smoke.schema_version} "
                f"migration_version={smoke.migration_version} quick_check={smoke.quick_check}"
            )
        else:
            print("ERROR: self_host_smoke_failed")
        return 0 if smoke.success else 1
    root = args.workspace.resolve()
    env_file = args.env_file or (root / ".env")
    values = load_effective_environment(env_file)
    report = inspect_environment(root, values)
    if args.json:
        print(render_preflight_json(report, build_preflight_current_truth(values)))
        return report.exit_code
    for item in report.ok:
        print(f"OK: {item}")
    for item in report.waiting_for_human:
        print(f"WAIT: {item}")
    for item in report.warnings:
        print(f"WARN: {item}")
    for item in report.errors:
        print(f"ERROR: {item}")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
