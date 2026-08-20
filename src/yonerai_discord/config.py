from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .secret_policy import is_loopback_endpoint, strong_safety_identifier_secret
from .voice_contract import MIN_VOICEVOX_WAV_BYTES


SAFE_DEFAULT_PLUGINS = frozenset(
    {
        "ai",
        "automod",
        "community",
        "discovery",
        "evolution",
        "earthquake",
        "jobs",
        "jp_information",
        "moderation",
        "modtools",
        "music",
        "operations",
        "personal_memory",
        "scheduling",
        "servertools",
        "site_publish",
        "utility",
        "voice",
    }
)
# 2026-07-21のmodule別security reviewと回帰testを通過した4 pluginは
# runtime capability/RBACへ接続した。今後の実験pluginはここへ追加する。
QUARANTINED_PLUGINS: frozenset[str] = frozenset({"experimental"})
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_AI_EXECUTION_TOPOLOGIES = frozenset({"local_standalone", "direct_core", "discord_processing", "hybrid"})
_AI_HOSTING_PROFILES = frozenset({"official_managed", "official_hybrid_private", "full_private_self_host"})
_AI_PACKAGING_CANDIDATES = frozenset({"public_safe_shared", "official_private", "local_only", "undecided"})
_WEB_SEARCH_BACKENDS = frozenset({"yonerai_search_gateway"})
_YONERAI_SEARCH_GATEWAY_MODES = frozenset({"loopback"})
_LOGGER = logging.getLogger(__name__)


class ConfigurationError(ValueError):
    """起動できない設定不備。値そのものは例外文へ含めない。"""


def _integer(values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = values.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} は整数で指定してください") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} は {minimum}〜{maximum} の範囲で指定してください")
    return value


def _plugin_names(raw: str) -> frozenset[str]:
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    invalid = [name for name in names if not name.replace("_", "").replace("-", "").isalnum()]
    if invalid:
        raise ConfigurationError("ENABLED_PLUGINS に使用できない名前が含まれています")
    return frozenset(names)


def _path_list(raw: str, name: str, *, maximum: int = 20) -> tuple[Path, ...]:
    if "\x00" in raw:
        raise ConfigurationError(f"{name} にNUL文字は使用できません")
    parts = [part.strip() for part in raw.replace("\r", "\n").replace(";", "\n").split("\n")]
    values = tuple(dict.fromkeys(Path(part).expanduser() for part in parts if part))
    if len(values) > maximum:
        raise ConfigurationError(f"{name} は最大{maximum}件です")
    return values


def _snowflake_ids(raw: str, name: str) -> frozenset[int]:
    values: set[int] = set()
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            value = int(candidate)
        except ValueError as exc:
            raise ConfigurationError(f"{name} はカンマ区切りのDiscord IDで指定してください") from exc
        if value <= 0 or value >= 2**63:
            raise ConfigurationError(f"{name} に不正なDiscord IDが含まれています")
        values.add(value)
    return frozenset(values)


def _boolean(values: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = values.get(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} は true または false で指定してください")


def _floating(
    values: Mapping[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = values.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} は数値で指定してください") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} は {minimum}〜{maximum} の範囲で指定してください")
    return value


def _model_id(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default).strip()
    if not _MODEL_ID.fullmatch(value):
        raise ConfigurationError(f"{name} は安全なprovider model IDで指定してください")
    return value


def _single_line_setting(values: Mapping[str, str], name: str, default: str, *, maximum: int = 128) -> str:
    value = values.get(name, default).strip()
    if not value or "\n" in value or "\r" in value or "\x00" in value or len(value) > maximum:
        raise ConfigurationError(f"{name} は{maximum}文字以内の1行で指定してください")
    return value


def _optional_choice(values: Mapping[str, str], name: str, allowed: frozenset[str]) -> str | None:
    value = values.get(name, "").strip()
    if not value:
        return None
    if value not in allowed:
        raise ConfigurationError(f"{name} は定義済みの値で指定してください")
    return value


def _https_origin(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default).strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ConfigurationError(f"{name} はパスを含まないHTTPS originで指定してください")
    return value


def _optional_https_origin(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        return ""
    return _https_origin({name: value}, name, value)


def _optional_core_origin(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (parsed.scheme == "http" and not is_loopback_endpoint(value))
    ):
        raise ConfigurationError(f"{name} はHTTPSまたはloopback HTTPの固定originで指定してください")
    return f"{parsed.scheme}://{parsed.netloc}"


def _loopback_http_origin(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default).strip().rstrip("/")
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as exc:
        raise ConfigurationError(f"{name} はliteral loopback IPで指定してください") from exc
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not address.is_loopback
    ):
        raise ConfigurationError(f"{name} はliteral loopback HTTP originで指定してください")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(f"{name} のportが不正です") from exc
    if port is None:
        raise ConfigurationError(f"{name} は固定portを含めてください")
    return f"http://{parsed.netloc}"


def _https_url(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default).strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(f"{name} はHTTPS URLで指定してください")
    return value


def _optional_https_url(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        return ""
    return _https_url({name: value}, name, value)


def _optional_consent_ttl(values: Mapping[str, str]) -> int | None:
    name = "AI_REMOTE_CONSENT_TTL_SECONDS"
    raw = values.get(name, "0").strip().lower()
    if raw in {"", "0", "none", "never"}:
        return None
    return _integer(values, name, 0, 60, 3_600)


def _provider_credential(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 4_096
        and all(ord(character) >= 0x20 and ord(character) != 0x7F for character in value)
    )


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str = field(repr=False)
    guild_id: int | None
    command_sync_guild_ids: tuple[int, ...]
    database_path: Path
    log_level: int
    log_path: Path
    log_max_bytes: int
    log_backup_count: int
    shutdown_timeout_seconds: int
    enabled_plugins: frozenset[str]
    sync_global_commands: bool
    bot_owner_ids: frozenset[int]
    trusted_role_ids: frozenset[int]
    moderator_role_ids: frozenset[int]
    capability_catalog_path: Path
    allow_quarantined_plugins: bool
    automod_enabled: bool
    identity_enabled: bool
    identity_public_base_url: str
    identity_turnstile_secret: str = field(repr=False)
    identity_allow_insecure_localhost: bool
    identity_http_enabled: bool
    identity_http_host: str
    identity_http_port: int
    identity_turnstile_site_key: str = field(repr=False)
    minecraft_enabled: bool
    minecraft_host: str
    minecraft_port: int
    minecraft_timeout_seconds: float
    minecraft_allow_public: bool
    minecraft_max_packet_bytes: int
    self_evolution_enabled: bool
    ai_base_url: str
    openai_api_key: str = field(repr=False)
    ai_api_key: str = field(repr=False)
    ai_allow_remote: bool
    ai_mention_enabled: bool
    ai_mention_guild_ids: frozenset[int]
    ai_mention_allow_all_guilds: bool
    ai_dm_enabled: bool
    ai_reply_continuation_enabled: bool
    ai_attachments_enabled: bool
    ai_admission_global_concurrency: int
    ai_admission_max_waiters: int
    ai_admission_wait_timeout_seconds: float
    ai_admission_drain_timeout_seconds: float
    ai_remote_consent_ttl_seconds: int | None
    ai_remote_consent_max_grants: int
    ai_conversation_ttl_seconds: int
    ai_conversation_max_turns: int
    ai_conversation_max_sessions: int
    ai_conversation_max_total_binary_bytes: int
    ai_attachment_max_files: int
    ai_attachment_max_file_bytes: int
    ai_attachment_max_total_bytes: int
    ai_allow_luna: bool
    ai_safety_identifier_secret: str = field(repr=False)
    ai_timeout_seconds: float
    ai_max_output_tokens: int
    ai_max_response_bytes: int
    member_events_enabled: bool
    message_audit_events_enabled: bool
    voice_enabled: bool
    voicevox_url: str
    voice_allow_remote: bool
    voice_timeout_seconds: float
    voice_max_response_bytes: int
    voicevox_managed_process_enabled: bool
    voicevox_managed_executable: Path | None = field(repr=False)
    voicevox_managed_startup_timeout_seconds: float
    voicevox_managed_shutdown_timeout_seconds: float
    music_enabled: bool
    music_library_roots: tuple[Path, ...]
    music_database_path: Path
    music_ffmpeg_path: str
    music_library_max_files: int
    music_library_max_file_bytes: int
    music_max_queue: int
    music_max_speech_queue: int
    music_default_volume: float
    music_read_aloud_enabled: bool
    music_max_playlists_per_user: int
    music_max_tracks_per_playlist: int
    jobs_poll_seconds: float
    jobs_batch_size: int
    jobs_lease_seconds: int
    jobs_execution_timeout_seconds: float
    jobs_drain_timeout_seconds: float
    jobs_backoff_base_seconds: int
    jobs_backoff_max_seconds: int
    jobs_disabled_defer_seconds: int
    yonerai_enabled: bool
    yonerai_allow_remote: bool
    yonerai_remote_status_opt_in: bool
    yonerai_auth_token: str = field(repr=False)
    yonerai_core_origin: str = field(repr=False)
    yonerai_timeout_seconds: float
    yonerai_max_response_bytes: int
    ai_execution_topology: str | None = None
    ai_hosting_profile: str | None = None
    ai_packaging_candidate: str | None = None
    web_search_enabled: bool = True
    web_search_backend: str = "yonerai_search_gateway"
    yonerai_search_gateway_mode: str = "loopback"
    yonerai_search_gateway_url: str = "http://127.0.0.1:8787"
    search_max_queries_per_run: int = 3
    search_max_results: int = 10
    search_max_fetches: int = 5
    search_timeout_seconds: float = 12.0
    search_max_response_bytes: int = 524_288
    search_fetch_max_bytes: int = 2_097_152
    search_cache_ttl_seconds: int = 1_800
    search_raw_query_retention_seconds: int = 0
    search_require_corroboration_for_high_stakes: bool = True
    search_allow_paid_fallback: bool = False
    openai_web_search_tool_enabled: bool = False
    brave_search_api_enabled: bool = False
    ai_web_search_enabled: bool = False
    ai_model_fast: str = "gpt-5.6-luna"
    ai_model_balanced: str = "gpt-5.6-terra"
    ai_model_quality: str = "gpt-5.6-sol"
    ai_status_emoji_processing: str = "🔄"
    ai_status_emoji_done: str = "✅"
    ai_status_emoji_pending: str = "▫️"
    ai_status_emoji_failed: str = "❌"
    ai_orchestration_durable_enabled: bool = False
    site_publish_enabled: bool = False
    site_publish_auto_enabled: bool = False
    site_publish_allow_remote: bool = False
    site_publish_base_url: str = ""
    site_publish_api_url: str = ""
    site_publish_hmac_secret: str = field(default="", repr=False)
    site_publish_timeout_seconds: float = 20.0
    site_publish_max_response_bytes: int = 262_144
    nasa_apod_allow_remote: bool = False
    nasa_apod_api_key: str = field(default="", repr=False)
    cloudflare_browser_rendering_allow_remote: bool = False
    cloudflare_browser_rendering_account_id: str | None = None
    cloudflare_browser_rendering_api_token: str = field(default="", repr=False)
    cloudflare_browser_rendering_timeout_seconds: float = 45.0
    cloudflare_browser_rendering_max_output_bytes: int = 8 * 1024 * 1024
    cloudflare_browser_run_interactive_enabled: bool = False
    media_url_inspection_allow_remote: bool = False
    media_url_inspection_api_key: str = field(default="", repr=False)
    media_url_inspection_timeout_seconds: float = 60.0
    media_url_inspection_max_response_bytes: int = 262_144
    media_url_inspection_daily_call_limit: int = 0
    media_url_inspection_use_hyperv: bool = False
    media_url_inspection_hyperv_timeout_seconds: float = 60.0
    image_openai_enabled: bool = False
    image_artifact_root: Path | None = field(default=None, repr=False)
    image_openai_timeout_seconds: float = 90.0
    stt_openai_enabled: bool = False
    stt_openai_timeout_seconds: float = 120.0
    tts_voicevox_enabled: bool = False
    tts_artifact_root: Path | None = field(default=None, repr=False)
    tts_voicevox_timeout_seconds: float = 60.0
    music_elevenlabs_enabled: bool = False
    elevenlabs_api_key: str = field(default="", repr=False)
    music_generation_artifact_root: Path | None = field(default=None, repr=False)
    music_elevenlabs_timeout_seconds: float = 180.0
    video_veo_enabled: bool = False
    gemini_api_key: str = field(default="", repr=False)
    video_artifact_root: Path | None = field(default=None, repr=False)
    video_veo_timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        execution_profile_fields = (
            self.ai_execution_topology,
            self.ai_hosting_profile,
            self.ai_packaging_candidate,
        )
        configured_execution_profile_fields = tuple(value is not None for value in execution_profile_fields)
        if any(configured_execution_profile_fields) and not all(configured_execution_profile_fields):
            raise ConfigurationError(
                "AI_EXECUTION_TOPOLOGY、AI_HOSTING_PROFILE、AI_PACKAGING_CANDIDATE は3項目すべて指定してください"
            )
        if self.jobs_execution_timeout_seconds >= self.jobs_lease_seconds:
            raise ConfigurationError("JOBS_EXECUTION_TIMEOUT_SECONDS は JOBS_LEASE_SECONDS 未満にしてください")
        if self.jobs_drain_timeout_seconds >= self.shutdown_timeout_seconds:
            raise ConfigurationError("JOBS_DRAIN_TIMEOUT_SECONDS は SHUTDOWN_TIMEOUT_SECONDS 未満にしてください")
        if self.jobs_backoff_base_seconds > self.jobs_backoff_max_seconds:
            raise ConfigurationError("JOBS_BACKOFF_BASE_SECONDS は JOBS_BACKOFF_MAX_SECONDS 以下にしてください")
        if self.ai_reply_continuation_enabled and not self.ai_mention_enabled:
            raise ConfigurationError("AI_REPLY_CONTINUATION_ENABLED には AI_MENTION_ENABLED=true が必要です")
        if self.ai_attachments_enabled and not self.ai_mention_enabled:
            raise ConfigurationError("AI_ATTACHMENTS_ENABLED には AI_MENTION_ENABLED=true が必要です")
        if self.ai_orchestration_durable_enabled and not self.ai_mention_enabled:
            raise ConfigurationError("AI_ORCHESTRATION_DURABLE_ENABLED には AI_MENTION_ENABLED=true が必要です")
        if self.music_read_aloud_enabled and not self.music_enabled:
            raise ConfigurationError("MUSIC_READ_ALOUD_ENABLED には MUSIC_ENABLED=true が必要です")
        if self.music_read_aloud_enabled and not self.voice_enabled:
            raise ConfigurationError("MUSIC_READ_ALOUD_ENABLED には VOICE_ENABLED=true が必要です")
        if self.music_read_aloud_enabled and self.voice_allow_remote:
            raise ConfigurationError("MUSIC_READ_ALOUD_ENABLED はloopback VOICEVOXだけを許可します")
        if self.voicevox_managed_process_enabled:
            if not self.voice_enabled:
                raise ConfigurationError("VOICEVOX_MANAGED_PROCESS_ENABLED=true には VOICE_ENABLED=true が必要です")
            if self.voice_allow_remote:
                raise ConfigurationError("managed VOICEVOX はloopbackだけを許可します")
            parsed_voicevox = urlsplit(self.voicevox_url)
            try:
                voicevox_address = ipaddress.ip_address(parsed_voicevox.hostname or "")
                voicevox_port = parsed_voicevox.port
            except (ValueError, TypeError) as exc:
                raise ConfigurationError("managed VOICEVOX はliteral loopback HTTP originで指定してください") from exc
            if (
                parsed_voicevox.scheme != "http"
                or parsed_voicevox.username is not None
                or parsed_voicevox.password is not None
                or parsed_voicevox.query
                or parsed_voicevox.fragment
                or parsed_voicevox.path not in {"", "/"}
                or not voicevox_address.is_loopback
                or voicevox_port is None
            ):
                raise ConfigurationError("managed VOICEVOX はliteral loopback HTTP originで指定してください")
            executable = self.voicevox_managed_executable
            if (
                executable is None
                or not executable.is_absolute()
                or executable.name.casefold() != "run.exe"
                or executable.is_symlink()
                or not executable.is_file()
            ):
                raise ConfigurationError("VOICEVOX_MANAGED_EXECUTABLE は実在する絶対run.exeを指定してください")
        if self.ai_admission_wait_timeout_seconds >= self.ai_admission_drain_timeout_seconds:
            raise ConfigurationError(
                "AI_ADMISSION_WAIT_TIMEOUT_SECONDS は AI_ADMISSION_DRAIN_TIMEOUT_SECONDS 未満にしてください"
            )
        if self.ai_admission_drain_timeout_seconds >= self.shutdown_timeout_seconds:
            raise ConfigurationError(
                "AI_ADMISSION_DRAIN_TIMEOUT_SECONDS は SHUTDOWN_TIMEOUT_SECONDS 未満にしてください"
            )
        if self.ai_mention_enabled and not self.ai_mention_guild_ids and not self.ai_mention_allow_all_guilds:
            raise ConfigurationError(
                "AI_MENTION_ENABLED=true には DISCORD_GUILD_ID/AI_MENTION_GUILD_IDS、"
                "または明示的な AI_MENTION_ALLOW_ALL_GUILDS=true が必要です"
            )
        if self.ai_attachment_max_total_bytes < self.ai_attachment_max_file_bytes:
            raise ConfigurationError("AI_ATTACHMENT_MAX_TOTAL_BYTES は AI_ATTACHMENT_MAX_FILE_BYTES 以上にしてください")
        if self.ai_conversation_max_total_binary_bytes < self.ai_attachment_max_total_bytes:
            raise ConfigurationError(
                "AI_CONVERSATION_MAX_TOTAL_BINARY_BYTES は AI_ATTACHMENT_MAX_TOTAL_BYTES 以上にしてください"
            )
        if self.search_max_fetches > self.search_max_results:
            raise ConfigurationError("SEARCH_MAX_FETCHES は SEARCH_MAX_RESULTS 以下にしてください")
        if self.web_search_backend not in _WEB_SEARCH_BACKENDS:
            raise ConfigurationError("WEB_SEARCH_BACKEND はcode-owned backendで指定してください")
        if self.yonerai_search_gateway_mode not in _YONERAI_SEARCH_GATEWAY_MODES:
            raise ConfigurationError("YONERAI_SEARCH_GATEWAY_MODE はloopbackに限定してください")
        if self.search_raw_query_retention_seconds != 0:
            raise ConfigurationError("SEARCH_RAW_QUERY_RETENTION_SECONDS はM11では0に固定してください")
        if self.brave_search_api_enabled:
            raise ConfigurationError("BRAVE_SEARCH_API_ENABLED は未接続のためfalseにしてください")
        if (
            self.ai_allow_remote
            and self.ai_base_url
            and not is_loopback_endpoint(self.ai_base_url)
            and self.ai_safety_identifier_secret
            and not strong_safety_identifier_secret(self.ai_safety_identifier_secret)
        ):
            raise ConfigurationError(
                "AI_SAFETY_IDENTIFIER_SECRET はremote利用時にUTF-8で32bytes以上の専用乱数を設定してください"
            )
        if self.site_publish_enabled:
            if not self.site_publish_allow_remote:
                raise ConfigurationError("SITE_PUBLISH_ENABLED=true には SITE_PUBLISH_ALLOW_REMOTE=true が必要です")
            if len(self.site_publish_hmac_secret.encode("utf-8")) < 32:
                raise ConfigurationError("SITE_PUBLISH_HMAC_SECRET はUTF-8で32bytes以上の専用乱数を設定してください")
            if not self.site_publish_base_url or not self.site_publish_api_url:
                raise ConfigurationError(
                    "SITE_PUBLISH_BASE_URL と SITE_PUBLISH_API_URL は専用HTTPS originで明示してください"
                )
            api = urlsplit(self.site_publish_api_url)
            public = urlsplit(self.site_publish_base_url)
            if (api.scheme, api.hostname, api.port) != (public.scheme, public.hostname, public.port):
                raise ConfigurationError(
                    "SITE_PUBLISH_API_URL は SITE_PUBLISH_BASE_URL と同じ専用originに限定してください"
                )
            if api.path.rstrip("/") != "/.yonerai/api/v1":
                raise ConfigurationError("SITE_PUBLISH_API_URL は専用originの /.yonerai/api/v1 に限定してください")
        if self.image_artifact_root is not None and not self.image_artifact_root.is_absolute():
            raise ConfigurationError("IMAGE_ARTIFACT_ROOT は既存の絶対pathで指定してください")
        if self.image_openai_enabled:
            if not self.openai_api_key:
                raise ConfigurationError("IMAGE_OPENAI_ENABLED=true には OPENAI_API_KEY が必要です")
            if self.image_artifact_root is None:
                raise ConfigurationError("IMAGE_OPENAI_ENABLED=true には IMAGE_ARTIFACT_ROOT が必要です")
        for enabled, root, label in (
            (self.tts_voicevox_enabled, self.tts_artifact_root, "TTS_ARTIFACT_ROOT"),
            (
                self.music_elevenlabs_enabled,
                self.music_generation_artifact_root,
                "MUSIC_GENERATION_ARTIFACT_ROOT",
            ),
            (self.video_veo_enabled, self.video_artifact_root, "VIDEO_ARTIFACT_ROOT"),
        ):
            if enabled and (
                not isinstance(root, Path) or not root.is_absolute() or not root.is_dir() or root.is_symlink()
            ):
                raise ConfigurationError(f"{label} は既存の非symlink絶対directoryで指定してください")
        if self.stt_openai_enabled and not _provider_credential(self.openai_api_key):
            raise ConfigurationError("STT_OPENAI_ENABLED=true には有効な OPENAI_API_KEY が必要です")
        if self.music_elevenlabs_enabled and not _provider_credential(self.elevenlabs_api_key):
            raise ConfigurationError("MUSIC_ELEVENLABS_ENABLED=true には有効な ELEVENLABS_API_KEY が必要です")
        if self.video_veo_enabled and not _provider_credential(self.gemini_api_key):
            raise ConfigurationError("VIDEO_VEO_ENABLED=true には有効な GEMINI_API_KEY が必要です")
        for value, label in (
            (self.stt_openai_timeout_seconds, "STT_OPENAI_TIMEOUT_SECONDS"),
            (self.tts_voicevox_timeout_seconds, "TTS_VOICEVOX_TIMEOUT_SECONDS"),
            (self.music_elevenlabs_timeout_seconds, "MUSIC_ELEVENLABS_TIMEOUT_SECONDS"),
            (self.video_veo_timeout_seconds, "VIDEO_VEO_TIMEOUT_SECONDS"),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 5.0 <= float(value) <= 900.0:
                raise ConfigurationError(f"{label} は5秒以上900秒以下で指定してください")

    @property
    def startup_plugins(self) -> frozenset[str]:
        """明示指定がなければ、監査済みの安全なプラグインだけを起動する。"""
        selected = set(self.enabled_plugins or SAFE_DEFAULT_PLUGINS)
        if self.minecraft_enabled:
            selected.add("minecraft")
        else:
            selected.discard("minecraft")
        if self.allow_quarantined_plugins:
            return frozenset(selected)
        return frozenset(selected - QUARANTINED_PLUGINS)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if environ is None else environ
        if values.get("AI_WEB_SEARCH_ENABLED", "").strip():
            _LOGGER.warning(
                "AI_WEB_SEARCH_ENABLED is deprecated and does not enable standard search; "
                "use WEB_SEARCH_ENABLED and the separate paid-search controls"
            )
        token = values.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigurationError("DISCORD_TOKEN が未設定です")
        if any(char.isspace() for char in token):
            raise ConfigurationError("DISCORD_TOKEN の形式が不正です")

        guild_id_raw = _integer(values, "DISCORD_GUILD_ID", 0, 0, 2**63 - 1)
        command_sync_guild_ids = _snowflake_ids(
            values.get("COMMAND_SYNC_GUILD_IDS", ""),
            "COMMAND_SYNC_GUILD_IDS",
        )
        # DISCORD_GUILD_ID は既存の単一guild設定として残しつつ、複数指定時も
        # unionを数値順へ正規化して起動ごとの同期順と監査ログを安定させる。
        command_sync_guild_id_order = tuple(
            sorted(command_sync_guild_ids | ({guild_id_raw} if guild_id_raw else set()))
        )
        database_raw = values.get("DATABASE_PATH", "data/yonerai-discord.sqlite3").strip()
        if not database_raw or "\x00" in database_raw:
            raise ConfigurationError("DATABASE_PATH が不正です")
        database_path = Path(database_raw).expanduser()
        music_database_raw = values.get("MUSIC_DATABASE_PATH", "").strip()
        if "\x00" in music_database_raw:
            raise ConfigurationError("MUSIC_DATABASE_PATH が不正です")
        music_database_path = Path(music_database_raw).expanduser() if music_database_raw else database_path
        music_ffmpeg_path = values.get("MUSIC_FFMPEG_PATH", "").strip()
        if "\x00" in music_ffmpeg_path:
            raise ConfigurationError("MUSIC_FFMPEG_PATH が不正です")
        voicevox_managed_executable_raw = values.get("VOICEVOX_MANAGED_EXECUTABLE", "").strip()
        if "\x00" in voicevox_managed_executable_raw:
            raise ConfigurationError("VOICEVOX_MANAGED_EXECUTABLE が不正です")
        voicevox_managed_executable = (
            Path(voicevox_managed_executable_raw).expanduser() if voicevox_managed_executable_raw else None
        )
        catalog_default = Path(__file__).resolve().parents[2] / "docs" / "CAPABILITY_COUNTS.json"
        catalog_raw = values.get("CAPABILITY_CATALOG_PATH", str(catalog_default)).strip()
        if not catalog_raw or "\x00" in catalog_raw:
            raise ConfigurationError("CAPABILITY_CATALOG_PATH が不正です")
        log_path_raw = values.get("LOG_PATH", "logs/yonerai-discord.jsonl").strip()
        if not log_path_raw or "\x00" in log_path_raw:
            raise ConfigurationError("LOG_PATH が不正です")
        image_artifact_root_raw = values.get("IMAGE_ARTIFACT_ROOT", "").strip()
        if "\x00" in image_artifact_root_raw:
            raise ConfigurationError("IMAGE_ARTIFACT_ROOT が不正です")
        image_artifact_root = Path(image_artifact_root_raw).expanduser() if image_artifact_root_raw else None
        media_artifact_roots: dict[str, Path | None] = {}
        for name in (
            "TTS_ARTIFACT_ROOT",
            "MUSIC_GENERATION_ARTIFACT_ROOT",
            "VIDEO_ARTIFACT_ROOT",
        ):
            raw = values.get(name, "").strip()
            if "\x00" in raw:
                raise ConfigurationError(f"{name} が不正です")
            media_artifact_roots[name] = Path(raw).expanduser() if raw else None

        level_name = values.get("LOG_LEVEL", "INFO").strip().upper()
        level = logging.getLevelNamesMapping().get(level_name)
        if not isinstance(level, int):
            raise ConfigurationError("LOG_LEVEL は DEBUG, INFO, WARNING, ERROR, CRITICAL から選んでください")

        shutdown_timeout = _integer(values, "SHUTDOWN_TIMEOUT_SECONDS", 15, 2, 120)
        jobs_lease_seconds = _integer(values, "JOBS_LEASE_SECONDS", 60, 5, 3_600)
        ai_mention_guild_ids = _snowflake_ids(
            values.get("AI_MENTION_GUILD_IDS", ""),
            "AI_MENTION_GUILD_IDS",
        )
        if not ai_mention_guild_ids and guild_id_raw:
            ai_mention_guild_ids = frozenset({guild_id_raw})

        return cls(
            discord_token=token,
            guild_id=guild_id_raw or None,
            command_sync_guild_ids=command_sync_guild_id_order,
            database_path=database_path,
            log_level=level,
            log_path=Path(log_path_raw).expanduser(),
            log_max_bytes=_integer(values, "LOG_MAX_BYTES", 10 * 1024 * 1024, 1_048_576, 104_857_600),
            log_backup_count=_integer(values, "LOG_BACKUP_COUNT", 10, 1, 20),
            shutdown_timeout_seconds=shutdown_timeout,
            enabled_plugins=_plugin_names(values.get("ENABLED_PLUGINS", "")),
            sync_global_commands=_boolean(values, "SYNC_GLOBAL_COMMANDS"),
            bot_owner_ids=_snowflake_ids(values.get("BOT_OWNER_IDS", ""), "BOT_OWNER_IDS"),
            trusted_role_ids=_snowflake_ids(values.get("TRUSTED_ROLE_IDS", ""), "TRUSTED_ROLE_IDS"),
            moderator_role_ids=_snowflake_ids(values.get("MODERATOR_ROLE_IDS", ""), "MODERATOR_ROLE_IDS"),
            capability_catalog_path=Path(catalog_raw).expanduser(),
            allow_quarantined_plugins=_boolean(values, "ALLOW_QUARANTINED_PLUGINS"),
            automod_enabled=_boolean(values, "AUTOMOD_ENABLED"),
            identity_enabled=_boolean(values, "IDENTITY_ENABLED"),
            identity_public_base_url=values.get("IDENTITY_PUBLIC_BASE_URL", "").strip(),
            identity_turnstile_secret=values.get("IDENTITY_TURNSTILE_SECRET", "").strip(),
            identity_allow_insecure_localhost=_boolean(values, "IDENTITY_ALLOW_INSECURE_LOCALHOST"),
            identity_http_enabled=_boolean(values, "IDENTITY_HTTP_ENABLED"),
            identity_http_host=values.get("IDENTITY_HTTP_HOST", "127.0.0.1").strip(),
            identity_http_port=_integer(values, "IDENTITY_HTTP_PORT", 8_765, 1, 65_535),
            identity_turnstile_site_key=values.get("IDENTITY_TURNSTILE_SITE_KEY", "").strip(),
            minecraft_enabled=_boolean(values, "MINECRAFT_ENABLED"),
            minecraft_host=values.get("MINECRAFT_HOST", "127.0.0.1").strip(),
            minecraft_port=_integer(values, "MINECRAFT_PORT", 25_565, 1, 65_535),
            minecraft_timeout_seconds=_floating(values, "MINECRAFT_TIMEOUT_SECONDS", 3.0, 0.25, 10.0),
            minecraft_allow_public=_boolean(values, "MINECRAFT_ALLOW_PUBLIC"),
            minecraft_max_packet_bytes=_integer(values, "MINECRAFT_MAX_PACKET_BYTES", 32_768, 1_024, 262_144),
            self_evolution_enabled=_boolean(values, "SELF_EVOLUTION_ENABLED"),
            ai_base_url=values.get("AI_BASE_URL", "https://api.openai.com/v1").strip(),
            openai_api_key=values.get("OPENAI_API_KEY", "").strip(),
            ai_api_key=values.get("AI_API_KEY", "").strip(),
            ai_allow_remote=_boolean(values, "AI_ALLOW_REMOTE"),
            image_openai_enabled=_boolean(values, "IMAGE_OPENAI_ENABLED"),
            image_artifact_root=image_artifact_root,
            image_openai_timeout_seconds=_floating(
                values,
                "IMAGE_OPENAI_TIMEOUT_SECONDS",
                90.0,
                5.0,
                900.0,
            ),
            stt_openai_enabled=_boolean(values, "STT_OPENAI_ENABLED"),
            stt_openai_timeout_seconds=_floating(
                values,
                "STT_OPENAI_TIMEOUT_SECONDS",
                120.0,
                5.0,
                900.0,
            ),
            tts_voicevox_enabled=_boolean(values, "TTS_VOICEVOX_ENABLED"),
            tts_artifact_root=media_artifact_roots["TTS_ARTIFACT_ROOT"],
            tts_voicevox_timeout_seconds=_floating(
                values,
                "TTS_VOICEVOX_TIMEOUT_SECONDS",
                60.0,
                5.0,
                900.0,
            ),
            music_elevenlabs_enabled=_boolean(values, "MUSIC_ELEVENLABS_ENABLED"),
            elevenlabs_api_key=values.get("ELEVENLABS_API_KEY", "").strip(),
            music_generation_artifact_root=media_artifact_roots["MUSIC_GENERATION_ARTIFACT_ROOT"],
            music_elevenlabs_timeout_seconds=_floating(
                values,
                "MUSIC_ELEVENLABS_TIMEOUT_SECONDS",
                180.0,
                5.0,
                900.0,
            ),
            video_veo_enabled=_boolean(values, "VIDEO_VEO_ENABLED"),
            gemini_api_key=values.get("GEMINI_API_KEY", "").strip(),
            video_artifact_root=media_artifact_roots["VIDEO_ARTIFACT_ROOT"],
            video_veo_timeout_seconds=_floating(
                values,
                "VIDEO_VEO_TIMEOUT_SECONDS",
                600.0,
                5.0,
                900.0,
            ),
            ai_mention_enabled=_boolean(values, "AI_MENTION_ENABLED"),
            ai_mention_guild_ids=ai_mention_guild_ids,
            ai_mention_allow_all_guilds=_boolean(values, "AI_MENTION_ALLOW_ALL_GUILDS"),
            ai_dm_enabled=_boolean(values, "AI_DM_ENABLED"),
            ai_reply_continuation_enabled=_boolean(values, "AI_REPLY_CONTINUATION_ENABLED"),
            ai_attachments_enabled=_boolean(values, "AI_ATTACHMENTS_ENABLED"),
            ai_admission_global_concurrency=_integer(values, "AI_ADMISSION_GLOBAL_CONCURRENCY", 4, 1, 32),
            ai_admission_max_waiters=_integer(values, "AI_ADMISSION_MAX_WAITERS", 32, 1, 512),
            ai_admission_wait_timeout_seconds=_floating(
                values,
                "AI_ADMISSION_WAIT_TIMEOUT_SECONDS",
                2.0,
                0.1,
                10.0,
            ),
            ai_admission_drain_timeout_seconds=_floating(
                values,
                "AI_ADMISSION_DRAIN_TIMEOUT_SECONDS",
                5.0,
                0.1,
                60.0,
            ),
            ai_remote_consent_ttl_seconds=_optional_consent_ttl(values),
            ai_remote_consent_max_grants=_integer(values, "AI_REMOTE_CONSENT_MAX_GRANTS", 1_024, 1, 10_000),
            ai_conversation_ttl_seconds=_integer(values, "AI_CONVERSATION_TTL_SECONDS", 7_200, 300, 86_400),
            # AIRequestはuser/assistant 24 message（12往復）を上限にする。
            ai_conversation_max_turns=_integer(values, "AI_CONVERSATION_MAX_TURNS", 12, 2, 12),
            ai_conversation_max_sessions=_integer(values, "AI_CONVERSATION_MAX_SESSIONS", 128, 1, 4_096),
            ai_conversation_max_total_binary_bytes=_integer(
                values,
                "AI_CONVERSATION_MAX_TOTAL_BINARY_BYTES",
                64 * 1024 * 1024,
                1024 * 1024,
                512 * 1024 * 1024,
            ),
            ai_attachment_max_files=_integer(values, "AI_ATTACHMENT_MAX_FILES", 4, 1, 8),
            ai_attachment_max_file_bytes=_integer(
                values, "AI_ATTACHMENT_MAX_FILE_BYTES", 8 * 1024 * 1024, 65_536, 25 * 1024 * 1024
            ),
            ai_attachment_max_total_bytes=_integer(
                values, "AI_ATTACHMENT_MAX_TOTAL_BYTES", 16 * 1024 * 1024, 65_536, 50 * 1024 * 1024
            ),
            ai_allow_luna=_boolean(values, "AI_ALLOW_LUNA", default=True),
            ai_safety_identifier_secret=values.get("AI_SAFETY_IDENTIFIER_SECRET", "").strip(),
            ai_timeout_seconds=_floating(values, "AI_TIMEOUT_SECONDS", 30.0, 5.0, 120.0),
            ai_max_output_tokens=_integer(values, "AI_MAX_OUTPUT_TOKENS", 2_048, 128, 8_192),
            ai_max_response_bytes=_integer(values, "AI_MAX_RESPONSE_BYTES", 2 * 1024 * 1024, 65_536, 4 * 1024 * 1024),
            web_search_enabled=_boolean(values, "WEB_SEARCH_ENABLED", default=True),
            web_search_backend=_single_line_setting(
                values,
                "WEB_SEARCH_BACKEND",
                "yonerai_search_gateway",
                maximum=64,
            ),
            yonerai_search_gateway_mode=_single_line_setting(
                values,
                "YONERAI_SEARCH_GATEWAY_MODE",
                "loopback",
                maximum=32,
            ),
            yonerai_search_gateway_url=_loopback_http_origin(
                values,
                "YONERAI_SEARCH_GATEWAY_URL",
                "http://127.0.0.1:8787",
            ),
            search_max_queries_per_run=_integer(values, "SEARCH_MAX_QUERIES_PER_RUN", 3, 1, 3),
            search_max_results=_integer(values, "SEARCH_MAX_RESULTS", 10, 1, 20),
            search_max_fetches=_integer(values, "SEARCH_MAX_FETCHES", 5, 1, 10),
            search_timeout_seconds=_floating(values, "SEARCH_TIMEOUT_SECONDS", 12.0, 1.0, 60.0),
            search_max_response_bytes=_integer(
                values,
                "SEARCH_MAX_RESPONSE_BYTES",
                512 * 1024,
                16 * 1024,
                4 * 1024 * 1024,
            ),
            search_fetch_max_bytes=_integer(
                values,
                "SEARCH_FETCH_MAX_BYTES",
                2 * 1024 * 1024,
                64 * 1024,
                16 * 1024 * 1024,
            ),
            search_cache_ttl_seconds=_integer(values, "SEARCH_CACHE_TTL_SECONDS", 1_800, 1, 86_400),
            search_raw_query_retention_seconds=_integer(
                values,
                "SEARCH_RAW_QUERY_RETENTION_SECONDS",
                0,
                0,
                3_600,
            ),
            search_require_corroboration_for_high_stakes=_boolean(
                values,
                "SEARCH_REQUIRE_CORROBORATION_FOR_HIGH_STAKES",
                default=True,
            ),
            search_allow_paid_fallback=_boolean(values, "SEARCH_ALLOW_PAID_FALLBACK"),
            openai_web_search_tool_enabled=_boolean(values, "OPENAI_WEB_SEARCH_TOOL_ENABLED"),
            brave_search_api_enabled=_boolean(values, "BRAVE_SEARCH_API_ENABLED"),
            ai_web_search_enabled=_boolean(values, "AI_WEB_SEARCH_ENABLED"),
            ai_model_fast=_model_id(values, "AI_MODEL_FAST", "gpt-5.6-luna"),
            ai_model_balanced=_model_id(values, "AI_MODEL_BALANCED", "gpt-5.6-terra"),
            ai_model_quality=_model_id(values, "AI_MODEL_QUALITY", "gpt-5.6-sol"),
            ai_status_emoji_processing=_single_line_setting(
                values,
                "AI_STATUS_EMOJI_PROCESSING",
                "🔄",
            ),
            ai_status_emoji_done=_single_line_setting(
                values,
                "AI_STATUS_EMOJI_DONE",
                "✅",
            ),
            ai_status_emoji_pending=_single_line_setting(values, "AI_STATUS_EMOJI_PENDING", "▫️"),
            ai_status_emoji_failed=_single_line_setting(values, "AI_STATUS_EMOJI_FAILED", "❌"),
            ai_orchestration_durable_enabled=_boolean(
                values,
                "AI_ORCHESTRATION_DURABLE_ENABLED",
            ),
            site_publish_enabled=_boolean(values, "SITE_PUBLISH_ENABLED"),
            site_publish_auto_enabled=_boolean(values, "SITE_PUBLISH_AUTO_ENABLED", default=False),
            site_publish_allow_remote=_boolean(values, "SITE_PUBLISH_ALLOW_REMOTE"),
            site_publish_base_url=_optional_https_origin(values, "SITE_PUBLISH_BASE_URL"),
            site_publish_api_url=_optional_https_url(values, "SITE_PUBLISH_API_URL"),
            site_publish_hmac_secret=values.get("SITE_PUBLISH_HMAC_SECRET", "").strip(),
            site_publish_timeout_seconds=_floating(
                values,
                "SITE_PUBLISH_TIMEOUT_SECONDS",
                20.0,
                1.0,
                60.0,
            ),
            site_publish_max_response_bytes=_integer(
                values,
                "SITE_PUBLISH_MAX_RESPONSE_BYTES",
                262_144,
                1_024,
                1_048_576,
            ),
            nasa_apod_allow_remote=_boolean(values, "NASA_APOD_ALLOW_REMOTE"),
            nasa_apod_api_key=values.get("NASA_APOD_API_KEY", "").strip(),
            cloudflare_browser_rendering_allow_remote=_boolean(
                values,
                "CLOUDFLARE_BROWSER_RENDERING_ALLOW_REMOTE",
            ),
            cloudflare_browser_rendering_account_id=(
                values.get("CLOUDFLARE_BROWSER_RENDERING_ACCOUNT_ID", "").strip() or None
            ),
            cloudflare_browser_rendering_api_token=values.get(
                "CLOUDFLARE_BROWSER_RENDERING_API_TOKEN",
                "",
            ).strip(),
            cloudflare_browser_rendering_timeout_seconds=_floating(
                values,
                "CLOUDFLARE_BROWSER_RENDERING_TIMEOUT_SECONDS",
                45.0,
                0.1,
                60.0,
            ),
            cloudflare_browser_rendering_max_output_bytes=_integer(
                values,
                "CLOUDFLARE_BROWSER_RENDERING_MAX_OUTPUT_BYTES",
                8 * 1024 * 1024,
                1 * 1024 * 1024,
                8 * 1024 * 1024,
            ),
            cloudflare_browser_run_interactive_enabled=_boolean(
                values,
                "CLOUDFLARE_BROWSER_RUN_INTERACTIVE_ENABLED",
            ),
            media_url_inspection_allow_remote=_boolean(
                values,
                "MEDIA_URL_INSPECTION_ALLOW_REMOTE",
            ),
            media_url_inspection_api_key=values.get(
                "MEDIA_URL_INSPECTION_API_KEY",
                "",
            ).strip(),
            media_url_inspection_timeout_seconds=_floating(
                values,
                "MEDIA_URL_INSPECTION_TIMEOUT_SECONDS",
                60.0,
                1.0,
                60.0,
            ),
            media_url_inspection_max_response_bytes=_integer(
                values,
                "MEDIA_URL_INSPECTION_MAX_RESPONSE_BYTES",
                262_144,
                16_384,
                1_048_576,
            ),
            media_url_inspection_daily_call_limit=_integer(
                values,
                "MEDIA_URL_INSPECTION_DAILY_CALL_LIMIT",
                0,
                0,
                1_000,
            ),
            media_url_inspection_use_hyperv=_boolean(
                values,
                "MEDIA_URL_INSPECTION_USE_HYPERV",
            ),
            media_url_inspection_hyperv_timeout_seconds=_floating(
                values,
                "MEDIA_URL_INSPECTION_HYPERV_TIMEOUT_SECONDS",
                60.0,
                1.0,
                60.0,
            ),
            member_events_enabled=_boolean(values, "SERVER_MEMBER_EVENTS_ENABLED"),
            message_audit_events_enabled=_boolean(values, "SERVER_MESSAGE_AUDIT_EVENTS_ENABLED"),
            voice_enabled=_boolean(values, "VOICE_ENABLED"),
            voicevox_url=values.get("VOICEVOX_URL", "http://127.0.0.1:50021").strip(),
            voice_allow_remote=_boolean(values, "VOICE_ALLOW_REMOTE"),
            voice_timeout_seconds=_floating(values, "VOICE_TIMEOUT_SECONDS", 15.0, 1.0, 60.0),
            voice_max_response_bytes=_integer(
                values,
                "VOICE_MAX_RESPONSE_BYTES",
                25 * 1024 * 1024,
                MIN_VOICEVOX_WAV_BYTES,
                50 * 1024 * 1024,
            ),
            voicevox_managed_process_enabled=_boolean(
                values,
                "VOICEVOX_MANAGED_PROCESS_ENABLED",
            ),
            voicevox_managed_executable=voicevox_managed_executable,
            voicevox_managed_startup_timeout_seconds=_floating(
                values,
                "VOICEVOX_MANAGED_STARTUP_TIMEOUT_SECONDS",
                15.0,
                1.0,
                120.0,
            ),
            voicevox_managed_shutdown_timeout_seconds=_floating(
                values,
                "VOICEVOX_MANAGED_SHUTDOWN_TIMEOUT_SECONDS",
                8.0,
                1.0,
                30.0,
            ),
            music_enabled=_boolean(values, "MUSIC_ENABLED"),
            music_library_roots=_path_list(values.get("MUSIC_LIBRARY_ROOTS", ""), "MUSIC_LIBRARY_ROOTS"),
            music_database_path=music_database_path,
            music_ffmpeg_path=music_ffmpeg_path,
            music_library_max_files=_integer(values, "MUSIC_LIBRARY_MAX_FILES", 5_000, 1, 50_000),
            music_library_max_file_bytes=_integer(
                values,
                "MUSIC_LIBRARY_MAX_FILE_BYTES",
                512 * 1024 * 1024,
                1_024,
                2 * 1024 * 1024 * 1024,
            ),
            music_max_queue=_integer(values, "MUSIC_MAX_QUEUE", 50, 1, 1_000),
            music_max_speech_queue=_integer(values, "MUSIC_MAX_SPEECH_QUEUE", 10, 1, 100),
            music_default_volume=_floating(values, "MUSIC_DEFAULT_VOLUME", 0.65, 0.0, 2.0),
            music_read_aloud_enabled=_boolean(values, "MUSIC_READ_ALOUD_ENABLED"),
            music_max_playlists_per_user=_integer(values, "MUSIC_MAX_PLAYLISTS_PER_USER", 50, 1, 500),
            music_max_tracks_per_playlist=_integer(values, "MUSIC_MAX_TRACKS_PER_PLAYLIST", 100, 1, 1_000),
            jobs_poll_seconds=_floating(values, "JOBS_POLL_SECONDS", 5.0, 0.25, 60.0),
            jobs_batch_size=_integer(values, "JOBS_BATCH_SIZE", 10, 1, 100),
            jobs_lease_seconds=jobs_lease_seconds,
            jobs_execution_timeout_seconds=_floating(
                values,
                "JOBS_EXECUTION_TIMEOUT_SECONDS",
                min(45.0, float(jobs_lease_seconds - 1)),
                1.0,
                3_599.0,
            ),
            jobs_drain_timeout_seconds=_floating(
                values,
                "JOBS_DRAIN_TIMEOUT_SECONDS",
                min(12.0, float(shutdown_timeout - 1)),
                1.0,
                119.0,
            ),
            jobs_backoff_base_seconds=_integer(values, "JOBS_BACKOFF_BASE_SECONDS", 5, 1, 3_600),
            jobs_backoff_max_seconds=_integer(values, "JOBS_BACKOFF_MAX_SECONDS", 3_600, 1, 86_400),
            jobs_disabled_defer_seconds=_integer(values, "JOBS_DISABLED_DEFER_SECONDS", 300, 30, 3_600),
            yonerai_enabled=_boolean(values, "YONERAI_ENABLED"),
            yonerai_allow_remote=_boolean(values, "YONERAI_ALLOW_REMOTE"),
            yonerai_remote_status_opt_in=_boolean(values, "YONERAI_REMOTE_STATUS_OPT_IN"),
            yonerai_auth_token=values.get("YONERAI_AUTH_TOKEN", "").strip(),
            yonerai_core_origin=_optional_core_origin(values, "YONERAI_CORE_ORIGIN"),
            yonerai_timeout_seconds=_floating(values, "YONERAI_TIMEOUT_SECONDS", 5.0, 0.25, 15.0),
            yonerai_max_response_bytes=_integer(values, "YONERAI_MAX_RESPONSE_BYTES", 65_536, 1_024, 262_144),
            ai_execution_topology=_optional_choice(
                values,
                "AI_EXECUTION_TOPOLOGY",
                _AI_EXECUTION_TOPOLOGIES,
            ),
            ai_hosting_profile=_optional_choice(
                values,
                "AI_HOSTING_PROFILE",
                _AI_HOSTING_PROFILES,
            ),
            ai_packaging_candidate=_optional_choice(
                values,
                "AI_PACKAGING_CANDIDATE",
                _AI_PACKAGING_CANDIDATES,
            ),
        )
