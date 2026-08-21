"""明示的な自然言語を既存の型付きサービスへ安全に接続する。

このモジュールは LLM の出力を操作命令として扱わない。Discord の生本文にある
Bot への明示メンションと、登録済みの完全一致パターンだけを入口にする。
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

import discord

from yonerai_discord.ai_control.routing import RiskLevel
from yonerai_discord.capabilities import (
    ACTION_CAPABILITIES,
    COMMAND_CAPABILITIES,
    COMMAND_PLUGIN_BY_ROOT,
    COMMAND_RBAC_FLOORS,
    EVENT_CAPABILITIES,
    SITE_AUTO_PUBLISH_CAPABILITY_ID,
)
from yonerai_discord.control_plane import ActorContext, RbacLevel, Registry
from yonerai_discord.discord_markdown import numbered_link
from yonerai_discord.discord_policy import determine_rbac_level
from yonerai_discord.modules.discovery.domain import MAX_QUERY_LENGTH
from yonerai_discord.modules.discovery.service import DiscoveryService
from yonerai_discord.modules.media_pipeline.domain import ArtifactKind, ArtifactRef, ArtifactScope
from yonerai_discord.voice_contract import VOICEVOX_SPEAKER_ID
from yonerai_discord.modules.media_pipeline.discord_asset_inspection import (
    DiscordAssetInspectionError,
    DiscordAssetKind,
    EmojiAssetInspectionRequest,
    StickerAssetFacts,
    StickerAssetInspectionRequest,
    StickerFormat,
    inspect_custom_emoji,
    inspect_sticker,
)
from yonerai_discord.modules.media_pipeline.plugin import MediaPipelinePlugin
from yonerai_discord.modules.jp_information.errors import JpInformationError, PublishedRangeError, UnknownRegionError
from yonerai_discord.modules.audio_core import LoopMode
from yonerai_discord.modules.utility import (
    color_from_hex,
    discord_timestamp,
    parse_choices,
    parse_dice,
    sha256_text,
    snowflake_created_at,
)
from yonerai_discord.modules.music.authorization import build_music_commit_check
from yonerai_discord.modules.music.models import (
    MusicActor,
    MusicAuthorizationError,
    MusicError,
    MusicSeekUnsupportedError,
    MusicSessionError,
    MusicUnavailableError,
)
from yonerai_discord.modules.music.links import youtube_search_url
from yonerai_discord.modules.nasa_apod import (
    ApodDateError,
    NasaApodError,
    render_apod_text,
)
from yonerai_discord.modules.scheduling.domain import Meeting, RSVP, RSVPStatus, parse_aware_datetime
from yonerai_discord.modules.voice.models import SpeechRequest
from yonerai_discord.modules.voice.service import SpeechUnavailableError
from yonerai_discord.modules.personal_memory.service import (
    CONVERSATION_RETENTION_SECONDS,
    FACT_RETENTION_SECONDS,
    MemoryDisabledError,
    SensitiveMemoryError,
)
from yonerai_discord.modules.personal_memory.domain import MemoryKind
from yonerai_discord.runtime_readiness import refresh_runtime_readiness
from yonerai_discord.surface_inventory import command_paths_from_tree

from .models import AIReply, AIRequest
from .discord_inputs import contains_secret_like_text
from .task_routing import parse_media_inspection_request, requests_multi_step_execution


logger = logging.getLogger(__name__)

_PROVIDER = "local-action-router"
_MODEL = "deterministic-v1"
_MAX_RESULT_CHARS = 1_900
_MAX_MODEL_EVIDENCE_CHARS = 8_000
_MAX_QUERY_CHARS = 200
_MAX_MEMORY_CHARS = 1_000
_SITE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,95}\Z")
_BOT_MENTION = re.compile(r"<@!?(?P<id>[1-9][0-9]*)>")
_ANY_DISCORD_MENTION = re.compile(r"<(?:(?:@!?|@&|#)[1-9][0-9]*)>")
_JST = timezone(timedelta(hours=9))
_MENTION_CAPABILITY_ID = EVENT_CAPABILITIES["ai_mention_message"]
DISCORD_TRIGGER_METADATA_KEY = "discord_trigger"
DISCORD_ACTIVE_REPLY_TRIGGER = "active_bot_reply"


class ActionMode(StrEnum):
    EXECUTE = "execute"
    DEFER_TO_SLASH = "defer_to_slash"


class ActionEffect(StrEnum):
    READ_ONLY = "read_only"
    SIDE_EFFECT = "side_effect"


class ActionOutputMode(StrEnum):
    DIRECT_REPLY = "direct_reply"
    MODEL_SYNTHESIS = "model_synthesis"


class ActionStatus(StrEnum):
    COMPLETED = "completed"
    DENIED = "denied"
    DEFERRED = "deferred"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ActionIntent:
    action_id: str
    parameters: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}), repr=False)

    def __post_init__(self) -> None:
        action_id = self.action_id.strip().lower()
        if not action_id:
            raise ValueError("action_id must not be empty")
        parameters = {str(key): str(value) for key, value in self.parameters.items()}
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "parameters", MappingProxyType(parameters))


@dataclass(frozen=True, slots=True)
class ActionResult:
    status: ActionStatus
    text: str = field(repr=False)
    artifact: ArtifactRef | None = field(default=None, repr=False)
    model_evidence: str | None = field(default=None, repr=False, compare=False)
    delivery_handled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, ActionStatus):
            raise TypeError("status must be an ActionStatus")
        if self.artifact is not None and not isinstance(self.artifact, ArtifactRef):
            raise TypeError("action result artifact must be an ArtifactRef")
        if type(self.delivery_handled) is not bool:
            raise TypeError("delivery_handled must be a bool")
        text = self.text.strip()
        if not text:
            raise ValueError("action result must not be empty")
        object.__setattr__(self, "text", text[:_MAX_RESULT_CHARS])
        evidence = self.model_evidence
        if evidence is not None:
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError("model evidence must be a non-empty string")
            object.__setattr__(self, "model_evidence", evidence.strip()[:_MAX_MODEL_EVIDENCE_CHARS])

    def to_ai_reply(self, *, synthesis_action_id: str | None = None) -> AIReply:
        return AIReply(
            text=self.model_evidence
            if synthesis_action_id is not None and self.model_evidence is not None
            else self.text,
            model=_MODEL,
            provider=_PROVIDER,
            synthesis_action_id=synthesis_action_id,
            delivery_handled=self.delivery_handled,
        )


@dataclass(frozen=True, slots=True)
class ActionContext:
    bot: Any
    message: discord.Message
    request: AIRequest
    actor_level: RbacLevel
    artifact_scope: ArtifactScope | None = field(default=None, repr=False)
    binding_guard: Callable[[], bool] | None = field(default=None, repr=False, compare=False)

    def bindings_are_current(self) -> bool:
        """モデルへ公開しない実行時binding guardをfail-closedで評価する。"""

        if self.binding_guard is None:
            return True
        try:
            return self.binding_guard() is True
        except Exception:
            return False


class ActionParser(Protocol):
    def __call__(self, text: str) -> Mapping[str, str] | None: ...


class ActionExecutor(Protocol):
    def __call__(self, context: ActionContext, parameters: Mapping[str, Any]) -> Awaitable[ActionResult]: ...


def _freeze_planner_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        raise ValueError("planner schema is too deeply nested")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_planner_json(item, depth=depth + 1) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_planner_json(item, depth=depth + 1) for item in value)
    raise TypeError("planner schema must contain JSON-compatible values")


@dataclass(frozen=True, slots=True)
class PlannerActionContract:
    """LLMへexecutorを渡さず公開できる、固定action metadata。"""

    description: str
    input_schema: Mapping[str, Any] = field(repr=False)
    tags: tuple[str, ...] = ()
    intent_hints: tuple[str, ...] = ()
    risk: RiskLevel = RiskLevel.NORMAL
    output_artifact_schema: Mapping[str, Any] | None = field(default=None, repr=False)
    grounded_parameters: tuple[str, ...] = ()
    repetition_parameter_extractor: Callable[[str], Mapping[str, str] | None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    retry_safe: bool = False

    def __post_init__(self) -> None:
        description = " ".join(self.description.strip().split())
        if not 1 <= len(description) <= 160:
            raise ValueError("planner action description must be between 1 and 160 characters")
        if not isinstance(self.risk, RiskLevel):
            raise TypeError("planner action risk must be a RiskLevel")
        if type(self.retry_safe) is not bool:
            raise TypeError("planner action retry_safe must be a bool")
        schema = _freeze_planner_json(self.input_schema)
        if not isinstance(schema, Mapping):
            raise TypeError("planner input_schema must be an object")
        tags = tuple(dict.fromkeys(_planner_hint(value, "tag") for value in self.tags))
        intents = tuple(dict.fromkeys(_planner_hint(value, "intent hint") for value in self.intent_hints))
        if not tags or len(tags) > 16 or len(intents) > 8:
            raise ValueError("planner action hints are outside the bounded contract")
        output_schema = (
            None if self.output_artifact_schema is None else _freeze_planner_json(self.output_artifact_schema)
        )
        if output_schema is not None and not isinstance(output_schema, Mapping):
            raise TypeError("planner output_artifact_schema must be an object")
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for rule in properties.values():
                if isinstance(rule, Mapping) and rule.get("type") in {"artifact_ref", "artifact_ref_list"}:
                    artifact_kinds_from_schema(rule)
        grounded_parameters = self.grounded_parameters
        if (
            not isinstance(grounded_parameters, tuple)
            or len(grounded_parameters) > 8
            or any(
                type(name) is not str or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) is None
                for name in grounded_parameters
            )
            or len(grounded_parameters) != len(set(grounded_parameters))
        ):
            raise ValueError("planner grounded parameters are outside the bounded contract")
        if grounded_parameters and (
            not isinstance(properties, Mapping)
            or any(
                not isinstance(properties.get(name), Mapping) or properties[name].get("type") != "string"
                for name in grounded_parameters
            )
        ):
            raise ValueError("planner grounded parameter must name a string schema property")
        if self.repetition_parameter_extractor is not None and not callable(self.repetition_parameter_extractor):
            raise TypeError("planner repetition parameter extractor must be callable")
        if output_schema is not None:
            artifact_kinds_from_schema(output_schema, output=True)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "input_schema", schema)
        object.__setattr__(self, "tags", tags)
        object.__setattr__(self, "intent_hints", intents)
        object.__setattr__(self, "output_artifact_schema", output_schema)
        object.__setattr__(self, "grounded_parameters", grounded_parameters)


def artifact_kinds_from_schema(
    schema: Mapping[str, Any],
    *,
    output: bool = False,
) -> frozenset[ArtifactKind]:
    """code-owned artifact slot schemaをbounded kind集合へ変換する。"""

    if not isinstance(schema, Mapping):
        raise ValueError("artifact schema must be an object")
    keys = set(schema)
    schema_type = schema.get("type")
    if schema_type == "artifact_ref_list":
        if output:
            raise ValueError("artifact output schema cannot be artifact_ref_list")
        if keys != {"type", "accepted_kinds", "minItems", "maxItems"}:
            raise ValueError("artifact list schema must declare exact kinds and bounds")
        minimum = schema["minItems"]
        maximum = schema["maxItems"]
        if (
            isinstance(minimum, bool)
            or isinstance(maximum, bool)
            or not isinstance(minimum, int)
            or not isinstance(maximum, int)
            or not 1 <= minimum <= maximum <= 8
        ):
            raise ValueError("artifact list bounds are outside the 1..8 contract")
        values = schema["accepted_kinds"]
        if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= len(ArtifactKind):
            raise ValueError("accepted artifact kinds are outside the bounded contract")
        raw_kinds = tuple(values)
    elif schema_type != "artifact_ref":
        raise ValueError("artifact schema type must be artifact_ref or artifact_ref_list")
    elif output:
        if keys != {"type", "kind"}:
            raise ValueError("artifact output schema must declare exactly one kind")
        raw_kinds: tuple[Any, ...] = (schema["kind"],)
    elif keys == {"type", "kind"}:
        raw_kinds = (schema["kind"],)
    elif keys == {"type", "accepted_kinds"}:
        values = schema["accepted_kinds"]
        if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= len(ArtifactKind):
            raise ValueError("accepted artifact kinds are outside the bounded contract")
        raw_kinds = tuple(values)
    else:
        raise ValueError("artifact input schema must declare kind or accepted_kinds")
    try:
        kinds = tuple(ArtifactKind(value) for value in raw_kinds)
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact schema declares an unknown kind") from exc
    if len(kinds) != len(set(kinds)):
        raise ValueError("artifact kinds must be unique")
    return frozenset(kinds)


def artifact_list_bounds_from_schema(schema: Mapping[str, Any]) -> tuple[int, int]:
    """artifact_ref_list schemaを検証し、code-owned item boundsを返す。"""

    if not isinstance(schema, Mapping) or schema.get("type") != "artifact_ref_list":
        raise ValueError("artifact list schema type must be artifact_ref_list")
    artifact_kinds_from_schema(schema)
    return int(schema["minItems"]), int(schema["maxItems"])


def _planner_hint(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"planner {label} must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not 1 <= len(normalized) <= 48 or "\n" in normalized:
        raise ValueError(f"planner {label} is outside the bounded contract")
    return normalized


def _planner_string_schema(
    properties: Mapping[str, Mapping[str, Any]],
    *,
    required: tuple[str, ...],
) -> Mapping[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(required),
    }


def _planner_utility_contracts() -> Mapping[str, PlannerActionContract]:
    """状態共有を要しないpure utilityだけをplanner候補へ公開する。"""

    return MappingProxyType(
        {
            "tools.dice": PlannerActionContract(
                "指定したダイス式を振り、出目と合計を返す。",
                _planner_string_schema(
                    {"expression": {"type": "string", "minLength": 3, "maxLength": 32}},
                    required=("expression",),
                ),
                ("dice", "roll", "ダイス", "サイコロ", "振る"),
                ("random", "utility"),
                RiskLevel.LOW,
            ),
            "tools.random": PlannerActionContract(
                "指定した整数の閉区間から一つをランダムに選ぶ。",
                _planner_string_schema(
                    {
                        "minimum": {"type": "string", "pattern": r"^[+-]?[0-9]+$", "maxLength": 20},
                        "maximum": {"type": "string", "pattern": r"^[+-]?[0-9]+$", "maxLength": 20},
                    },
                    required=("minimum", "maximum"),
                ),
                ("random", "range", "乱数", "ランダム", "範囲"),
                ("random", "utility"),
                RiskLevel.LOW,
            ),
            "tools.choose": PlannerActionContract(
                "ASCIIカンマで区切った候補から一つを選ぶ。",
                _planner_string_schema(
                    {"choices": {"type": "string", "minLength": 3, "maxLength": 2_000}},
                    required=("choices",),
                ),
                ("choose", "choice", "選ぶ", "選択"),
                ("候補から選んで", "random", "utility"),
                RiskLevel.LOW,
            ),
            "tools.timestamp": PlannerActionContract(
                "offset付きISO-8601日時をDiscord timestampへ変換する。",
                _planner_string_schema(
                    {"datetime": {"type": "string", "minLength": 10, "maxLength": 64}},
                    required=("datetime",),
                ),
                ("timestamp", "datetime", "discord", "日時", "時刻"),
                ("formatting", "utility"),
                RiskLevel.LOW,
            ),
            "tools.snowflake": PlannerActionContract(
                "Discord Snowflake IDから作成日時を算出する。",
                _planner_string_schema(
                    {"discord_id": {"type": "string", "pattern": r"^[0-9]{17,20}$"}},
                    required=("discord_id",),
                ),
                ("snowflake", "discord id", "作成日時", "時刻"),
                ("formatting", "utility"),
                RiskLevel.LOW,
            ),
            "tools.sha256": PlannerActionContract(
                "短い文字列のSHA-256 digestを計算する。",
                _planner_string_schema(
                    {"text": {"type": "string", "minLength": 1, "maxLength": 512}},
                    required=("text",),
                ),
                ("sha-256", "sha256", "hash", "digest", "ハッシュ"),
                ("formatting", "utility"),
                RiskLevel.LOW,
                retry_safe=True,
            ),
            "tools.color": PlannerActionContract(
                "6桁HEXカラーを正規化して表示する。",
                _planner_string_schema(
                    {"hex_color": {"type": "string", "pattern": r"^#[0-9A-Fa-f]{6}$"}},
                    required=("hex_color",),
                ),
                ("color", "hex", "カラー", "色"),
                ("formatting", "utility"),
                RiskLevel.LOW,
                retry_safe=True,
            ),
        }
    )


_MUSIC_REPETITION_PARAMETER_RE = re.compile(
    rf"(?P<query>.{{1,{_MAX_QUERY_CHARS}}}?)(?:\s*を)?\s*"
    r"[1-9][0-9]{0,2}\s*(?:回|かい|times?(?![a-z0-9_]))\s*"
    r"(?:続けて\s*)?"
    r"(?:流して|ながして|再生して|かけて)\Z"
)
_MUSIC_ORDERED_PARAMETER_RE = re.compile(
    rf"(?P<query>.{{1,{_MAX_QUERY_CHARS}}}?)(?:\s*を)?\s*"
    r"(?:続けて\s*)?"
    r"(?:流して|ながして|再生して|かけて)\Z"
)
_MUSIC_EXPLICIT_REPETITION_RE = re.compile(r"(?<![0-9])[1-9][0-9]{0,2}\s*(?:回|かい|times?(?![a-z0-9_]))")


def _music_repetition_parameter_extractor(source_span: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", source_span).strip().split())
    pattern = (
        _MUSIC_REPETITION_PARAMETER_RE
        if _MUSIC_EXPLICIT_REPETITION_RE.search(normalized) is not None
        else _MUSIC_ORDERED_PARAMETER_RE
    )
    match = pattern.fullmatch(normalized)
    if match is None:
        return None
    query = " ".join(match.group("query").strip().split())
    if not query or _ANY_DISCORD_MENTION.search(query):
        return None
    return MappingProxyType({"query": query})


def _planner_domain_contracts() -> Mapping[str, PlannerActionContract]:
    """既存executorのうちfresh境界を満たすdomain actionだけをplannerへ公開する。"""

    empty_schema = _planner_string_schema({}, required=())
    local_music_search_schema = _planner_string_schema(
        {"query": {"type": "string", "minLength": 1, "maxLength": _MAX_QUERY_CHARS}},
        required=("query",),
    )
    return MappingProxyType(
        {
            "earthquake.latest": PlannerActionContract(
                "現在取得できる最新の地震情報を確認し、震度・震源・規模を表示する。",
                empty_schema,
                ("地震", "震度", "震源", "earthquake"),
                ("最新の地震情報", "地震を確認"),
                RiskLevel.LOW,
            ),
            "nasa.apod": PlannerActionContract(
                "NASA Astronomy Picture of the Dayを指定日または今日について取得する。",
                _planner_string_schema(
                    {
                        "date": {
                            "type": "string",
                            "pattern": r"^(?:|[0-9]{4}-[0-9]{2}-[0-9]{2})$",
                            "maxLength": 10,
                        }
                    },
                    required=("date",),
                ),
                ("nasa", "apod", "nasa apod"),
                ("nasaの今日の画像", "指定日のapod"),
                RiskLevel.LOW,
            ),
            "music.status": PlannerActionContract(
                "音楽runtimeの利用可否、ローカル曲数、接続中サーバー数を表示する。",
                empty_schema,
                ("音楽状態", "music runtime", "ミュージック状態"),
                ("音楽機能の状態", "音楽runtimeを確認"),
                RiskLevel.LOW,
            ),
            "music.search-local": PlannerActionContract(
                "許可済みローカル音楽ライブラリを曲名で検索し、候補名だけを表示する。",
                local_music_search_schema,
                ("ローカル曲検索", "音楽ライブラリ検索", "search local music"),
                ("ローカル曲を検索", "ローカル曲名を検索"),
                RiskLevel.LOW,
            ),
            "music.enqueue": PlannerActionContract(
                "許可済みローカル音楽ライブラリで一意に決まる1曲を、現在のVCキューへ追加する。",
                local_music_search_schema,
                ("流して", "再生して", "キュー追加", "music enqueue"),
                ("ローカル曲を流す", "順番に曲を再生"),
                RiskLevel.NORMAL,
                grounded_parameters=("query",),
                repetition_parameter_extractor=_music_repetition_parameter_extractor,
            ),
            "browser.screenshot": PlannerActionContract(
                "明示された公開HTTP(S) URLをremote browserで開き、画面のスクリーンショットを返信する。",
                _planner_string_schema(
                    {"url": {"type": "string", "minLength": 8, "maxLength": 2_048}},
                    required=("url",),
                ),
                ("url screenshot", "web screenshot", "スクショ", "スクリーンショット"),
                ("URLの画面を撮って", "Webページをスクショ"),
                RiskLevel.HIGH,
                grounded_parameters=("url",),
            ),
            "media.url-inspect": PlannerActionContract(
                "明示された公開動画URLを、許可済みの字幕・音声・画像理解Providerで解析して返信する。",
                _planner_string_schema(
                    {
                        "url": {"type": "string", "minLength": 8, "maxLength": 2_048},
                        "instruction": {"type": "string", "minLength": 1, "maxLength": 1_000},
                    },
                    required=("url", "instruction"),
                ),
                ("video url analysis", "media inspection", "字幕データ", "画像認識", "動画解析"),
                ("動画URLを字幕と画像で分析", "URLの動画を文字起こしして把握"),
                RiskLevel.HIGH,
                grounded_parameters=("url", "instruction"),
            ),
            "music.youtube-preview": PlannerActionContract(
                "曲名からYouTube公式検索URLを組み立て、その検索画面のスクリーンショットを返信する。",
                local_music_search_schema,
                ("youtube screenshot", "曲の検索画面", "youtube画面"),
                ("曲名のYouTube画面をスクショ", "複数曲の検索画面を撮って"),
                RiskLevel.HIGH,
                grounded_parameters=("query",),
            ),
            "site.status": PlannerActionContract(
                "静的サイト公開基盤のローカル構成とreadinessだけを表示する。",
                empty_schema,
                ("サイト公開状態", "site publish readiness", "サイト基盤状態"),
                ("サイト公開基盤を確認", "site readiness"),
                RiskLevel.LOW,
            ),
            "discovery.list": PlannerActionContract(
                "現在利用可能なBot機能一覧を表示する。",
                empty_schema,
                ("機能一覧", "list bot capabilities"),
                ("利用可能な機能", "available bot features"),
                RiskLevel.LOW,
            ),
            "discovery.search": PlannerActionContract(
                "Bot機能カタログを指定語で検索する。",
                _planner_string_schema(
                    {"query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_LENGTH}},
                    required=("query",),
                ),
                ("機能カタログ検索", "search feature catalog"),
                ("bot feature search", "機能を検索"),
                RiskLevel.LOW,
            ),
            "poll.results": PlannerActionContract(
                "指定した投票IDの結果を現在のチャンネル範囲で確認する。",
                _planner_string_schema(
                    {"poll_id": {"type": "string", "pattern": r"^[0-9a-f]{32}$"}},
                    required=("poll_id",),
                ),
                ("投票結果", "poll results"),
                ("poll results", "投票の結果"),
                RiskLevel.LOW,
                grounded_parameters=("poll_id",),
            ),
            "schedule.list": PlannerActionContract(
                "現在のサーバーの今後の予定一覧を確認する。",
                empty_schema,
                ("予定一覧", "schedule list"),
                ("upcoming schedule", "今後の予定"),
                RiskLevel.LOW,
            ),
            "schedule.show": PlannerActionContract(
                "指定した予定IDの詳細を現在のサーバー範囲で確認する。",
                _planner_string_schema(
                    {"meeting_id": {"type": "string", "pattern": r"^MEET-[A-F0-9]{8}$"}},
                    required=("meeting_id",),
                ),
                ("予定ID詳細", "schedule meeting detail", "meet-"),
                ("予定IDを確認",),
                RiskLevel.LOW,
                grounded_parameters=("meeting_id",),
            ),
        }
    )


def _planner_action_registrations() -> Mapping[str, tuple[PlannerActionContract, ActionEffect]]:
    """planner metadataと実際の副作用種別を同じcode-owned登録へ束縛する。"""

    domain_contracts = _planner_domain_contracts()
    registrations = {
        action_id: (contract, ActionEffect.READ_ONLY) for action_id, contract in _planner_utility_contracts().items()
    }
    registrations.update(
        {
            "earthquake.latest": (domain_contracts["earthquake.latest"], ActionEffect.READ_ONLY),
            "nasa.apod": (domain_contracts["nasa.apod"], ActionEffect.READ_ONLY),
            "music.status": (domain_contracts["music.status"], ActionEffect.READ_ONLY),
            "music.search-local": (domain_contracts["music.search-local"], ActionEffect.READ_ONLY),
            "music.enqueue": (domain_contracts["music.enqueue"], ActionEffect.SIDE_EFFECT),
            "browser.screenshot": (domain_contracts["browser.screenshot"], ActionEffect.SIDE_EFFECT),
            "media.url-inspect": (domain_contracts["media.url-inspect"], ActionEffect.SIDE_EFFECT),
            "music.youtube-preview": (domain_contracts["music.youtube-preview"], ActionEffect.SIDE_EFFECT),
            "site.status": (domain_contracts["site.status"], ActionEffect.READ_ONLY),
            "discovery.list": (domain_contracts["discovery.list"], ActionEffect.READ_ONLY),
            "discovery.search": (domain_contracts["discovery.search"], ActionEffect.READ_ONLY),
            "poll.results": (domain_contracts["poll.results"], ActionEffect.READ_ONLY),
            "schedule.list": (domain_contracts["schedule.list"], ActionEffect.READ_ONLY),
            "schedule.show": (domain_contracts["schedule.show"], ActionEffect.READ_ONLY),
        }
    )
    return MappingProxyType(registrations)


_ACTION_ADDITIONAL_COMMAND_PATHS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "music.enqueue": ("music search",),
        "music.youtube-preview": ("music search-youtube",),
    }
)
_ACTION_RBAC_FLOORS: Mapping[str, RbacLevel] = MappingProxyType(
    {
        "browser.screenshot": RbacLevel.BOT_OWNER,
        "browser.youtube-playback-evidence": RbacLevel.BOT_OWNER,
        "discord.asset-inspect": RbacLevel.TRUSTED,
        "discord.asset-inspect-invalid": RbacLevel.TRUSTED,
        "image.edit": RbacLevel.TRUSTED,
        "media.url-inspect": RbacLevel.BOT_OWNER,
        "music.youtube-preview": RbacLevel.BOT_OWNER,
        "site.permission-grant": RbacLevel.BOT_OWNER,
        "site.permission-revoke": RbacLevel.BOT_OWNER,
    }
)
_ACTION_TIMEOUT_SECONDS: Mapping[str, float] = MappingProxyType(
    {
        "browser.screenshot": 65.0,
        "browser.youtube-playback-evidence": 65.0,
        "media.url-inspect": 65.0,
        "music.youtube-preview": 65.0,
    }
)
_ACTION_OUTPUT_MODES: Mapping[str, ActionOutputMode] = MappingProxyType(
    {
        "media.url-inspect": ActionOutputMode.MODEL_SYNTHESIS,
    }
)


@dataclass(frozen=True, slots=True)
class ActionSpec:
    action_id: str
    command_path: str
    capability_id: str
    rbac_floor: RbacLevel
    parser: ActionParser = field(repr=False, compare=False)
    executor: ActionExecutor = field(repr=False, compare=False)
    mode: ActionMode = ActionMode.EXECUTE
    effect: ActionEffect = ActionEffect.SIDE_EFFECT
    planner_contract: PlannerActionContract | None = None
    additional_command_paths: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    output_mode: ActionOutputMode = ActionOutputMode.DIRECT_REPLY

    def __post_init__(self) -> None:
        action_id = self.action_id.strip().lower()
        command_path = " ".join(self.command_path.strip().lower().split())
        if not action_id or not command_path:
            raise ValueError("action_id and command_path are required")
        expected = COMMAND_CAPABILITIES.get(command_path, ACTION_CAPABILITIES.get(command_path))
        if expected is None or expected != self.capability_id:
            raise ValueError("action capability must match its code-owned capability mapping")
        floor = RbacLevel.parse(self.rbac_floor)
        declared_floor = COMMAND_RBAC_FLOORS.get(command_path, RbacLevel.EVERYONE)
        if floor < declared_floor:
            raise ValueError("action RBAC floor is below the command floor")
        if not callable(self.parser) or not callable(self.executor):
            raise TypeError("parser and executor must be callable")
        if not isinstance(self.mode, ActionMode):
            raise TypeError("mode must be an ActionMode")
        if not isinstance(self.effect, ActionEffect):
            raise TypeError("effect must be an ActionEffect")
        if not isinstance(self.output_mode, ActionOutputMode):
            raise TypeError("output_mode must be an ActionOutputMode")
        if self.planner_contract is not None and not isinstance(self.planner_contract, PlannerActionContract):
            raise TypeError("planner_contract must be a PlannerActionContract")
        if (
            self.planner_contract is not None
            and self.planner_contract.retry_safe
            and self.effect is not ActionEffect.READ_ONLY
        ):
            raise ValueError("only read-only actions may be declared retry-safe")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= float(self.timeout_seconds) <= 65.0
        ):
            raise ValueError("action timeout_seconds must be between 0.1 and 65")
        if not isinstance(self.additional_command_paths, tuple) or any(
            not isinstance(path, str) for path in self.additional_command_paths
        ):
            raise TypeError("additional_command_paths must be a tuple of strings")
        additional_paths = tuple(" ".join(path.strip().lower().split()) for path in self.additional_command_paths)
        if (
            any(not path for path in additional_paths)
            or command_path in additional_paths
            or len(additional_paths) != len(set(additional_paths))
        ):
            raise ValueError("additional command paths must be non-empty, unique, and exclude the primary path")
        additional_capability_ids: list[str] = []
        for path in additional_paths:
            capability_id = COMMAND_CAPABILITIES.get(path)
            if capability_id is None:
                raise ValueError("additional command path must have a code-owned capability mapping")
            additional_capability_ids.append(capability_id)
        if self.capability_id in additional_capability_ids or len(additional_capability_ids) != len(
            set(additional_capability_ids)
        ):
            raise ValueError("additional command capabilities must be unique and exclude the primary capability")
        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "command_path", command_path)
        object.__setattr__(self, "rbac_floor", floor)
        object.__setattr__(self, "additional_command_paths", additional_paths)
        object.__setattr__(
            self,
            "timeout_seconds",
            None if self.timeout_seconds is None else float(self.timeout_seconds),
        )

    @property
    def capability_requirements(self) -> tuple[tuple[str, str, RbacLevel], ...]:
        """このactionを公開・実行するために同時に必要なcommand capability。"""

        return (
            (self.command_path, self.capability_id, self.rbac_floor),
            *(
                (
                    path,
                    COMMAND_CAPABILITIES[path],
                    COMMAND_RBAC_FLOORS.get(path, RbacLevel.EVERYONE),
                )
                for path in self.additional_command_paths
            ),
        )


class ActionRegistry:
    """順序付きの明示パターン registry。最初の一意な一致だけを返す。"""

    def __init__(self, specs: Sequence[ActionSpec]) -> None:
        values = tuple(specs)
        ids = [spec.action_id for spec in values]
        if len(ids) != len(set(ids)):
            raise ValueError("action IDs must be unique")
        self._specs = values
        self._by_id = MappingProxyType({spec.action_id: spec for spec in values})

    @property
    def specs(self) -> tuple[ActionSpec, ...]:
        return self._specs

    def get(self, action_id: str) -> ActionSpec:
        return self._by_id[action_id]

    def parse(self, text: str) -> ActionIntent | None:
        matches: list[ActionIntent] = []
        for spec in self._specs:
            parameters = spec.parser(text)
            if parameters is not None:
                matches.append(ActionIntent(spec.action_id, parameters))
        return matches[0] if len(matches) == 1 else None


class NaturalActionRouter:
    """Discord 生本文から安全なローカル action だけを実行する pre-AI hook。"""

    # AIMentionListener はこの明示マーカーを持つ hook だけを、外部 AI 本人同意の
    # 判定前に実行する。任意の hook を早期実行して境界を迂回させない。
    runs_before_remote_consent = True

    def __init__(
        self,
        bot: Any,
        *,
        registry: ActionRegistry | None = None,
        timeout_seconds: float = 15.0,
        max_concurrency: int = 4,
    ) -> None:
        if not 0.1 <= float(timeout_seconds) <= 60.0:
            raise ValueError("timeout_seconds must be between 0.1 and 60")
        if isinstance(max_concurrency, bool) or not 1 <= int(max_concurrency) <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        self.bot = bot
        self.timeout_seconds = float(timeout_seconds)
        self._semaphore = asyncio.Semaphore(int(max_concurrency))
        self.registry = registry or self._default_registry()
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def _closing_now(self) -> bool:
        return self._closing or bool(getattr(self.bot, "is_closing", False))

    def begin_close(self) -> None:
        """新規 route と semaphore 待機中の route を fail-closed にする。"""

        self._closing = True

    async def __call__(self, message: discord.Message, request: AIRequest) -> AIReply | None:
        if self._closing_now:
            return _closing_result().to_ai_reply()
        text = _explicit_action_text(
            message,
            self.bot,
            allow_active_reply=(request.metadata.get(DISCORD_TRIGGER_METADATA_KEY) == DISCORD_ACTIVE_REPLY_TRIGGER),
        )
        if text is None:
            return None
        intent = self.registry.parse(text)
        if intent is None:
            return None
        spec = self.registry.get(intent.action_id)
        context = self._context(message, request)
        if context is None:
            return ActionResult(
                ActionStatus.DENIED, "操作の実行元を安全に確認できなかったため、中止しました。"
            ).to_ai_reply()
        if context.actor_level < spec.rbac_floor or not self._allowed(spec, context):
            return ActionResult(
                ActionStatus.DENIED,
                "この操作は現在の権限またはサーバーポリシーでは利用できません。",
            ).to_ai_reply()
        if spec.mode is ActionMode.DEFER_TO_SLASH:
            return ActionResult(
                ActionStatus.DEFERRED,
                f"この操作は影響が大きいため自動実行しません。`/{spec.command_path}` を使い、内容を確認して実行してください。",
            ).to_ai_reply()
        try:
            result = await asyncio.wait_for(
                self._execute(spec, context, intent.parameters),
                timeout=spec.timeout_seconds or self.timeout_seconds,
            )
        except TimeoutError:
            result = ActionResult(
                ActionStatus.FAILED,
                "処理結果を時間内に確認できませんでした。内容を短くして、もう一度送ってください。",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "natural_action_failed",
                extra={"action_id": spec.action_id, "error_type": type(exc).__name__},
            )
            result = ActionResult(ActionStatus.FAILED, "処理に失敗しました。詳細な入力内容はログへ保存していません。")
        synthesis_action_id = (
            spec.action_id
            if result.status is ActionStatus.COMPLETED and spec.output_mode is ActionOutputMode.MODEL_SYNTHESIS
            else None
        )
        return result.to_ai_reply(synthesis_action_id=synthesis_action_id)

    def requires_model_synthesis(self, text: str) -> bool:
        """Return code-owned output policy without executing an action."""

        if not isinstance(text, str) or self._closing_now:
            return False
        intent = self.registry.parse(text)
        if intent is None:
            return False
        try:
            spec = self.registry.get(intent.action_id)
        except KeyError:
            return False
        return spec.mode is ActionMode.EXECUTE and spec.output_mode is ActionOutputMode.MODEL_SYNTHESIS

    async def _execute(
        self,
        spec: ActionSpec,
        context: ActionContext,
        parameters: Mapping[str, str],
    ) -> ActionResult:
        if self._closing_now:
            return _closing_result()
        async with self._semaphore:
            if self._closing_now:
                return _closing_result()
            if not self._currently_allowed(spec, context) or not self._mention_currently_allowed(context):
                return ActionResult(
                    ActionStatus.DENIED,
                    "待機中に権限または機能設定が変更されたため、操作を実行しませんでした。",
                )
            return await spec.executor(context, parameters)

    async def fresh_context_for_spec(
        self,
        spec: ActionSpec,
        message: discord.Message,
        request: AIRequest,
    ) -> ActionContext | None:
        """plan実行用にREST fresh memberと現在policyからcontextを作り直す。"""

        if self._closing_now:
            return None
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        guard = getattr(self.bot, "capability_guard", None)
        fetch_member = getattr(guild, "fetch_member", None)
        evaluate = getattr(guard, "evaluate_fresh_member", None)
        currently_allowed = getattr(guard, "currently_allowed", None)
        permissions_for = getattr(channel, "permissions_for", None)
        ids = (
            getattr(guild, "id", None),
            getattr(channel, "id", None),
            getattr(author, "id", None),
        )
        if (
            not all(isinstance(value, int) and value > 0 for value in ids)
            or request.guild_id != ids[0]
            or request.channel_id != ids[1]
            or request.user_id != ids[2]
            or not self.runtime_requirements_current(spec, ids[0])
            or not callable(fetch_member)
            or not callable(evaluate)
            or not callable(currently_allowed)
            or not callable(permissions_for)
        ):
            return None
        try:
            member = await fetch_member(ids[2])
            if getattr(member, "id", None) != ids[2]:
                return None
            permissions = permissions_for(member)
            if (
                getattr(permissions, "view_channel", False) is not True
                or getattr(permissions, "read_message_history", False) is not True
            ):
                return None
            levels: list[RbacLevel] = []
            requirements = (
                *((capability_id, floor) for _, capability_id, floor in spec.capability_requirements),
                (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
            )
            for capability_id, floor in requirements:
                decision = await evaluate(capability_id, guild=guild, member=member)
                level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
                if (
                    getattr(decision, "allowed", False) is not True
                    or level < floor
                    or currently_allowed(
                        capability_id,
                        guild_id=ids[0],
                        user_id=ids[2],
                        actor_level=level,
                        floor=floor,
                    )
                    is not True
                ):
                    return None
                levels.append(level)
            if not self.runtime_requirements_current(spec, ids[0]):
                return None
            return ActionContext(self.bot, message, request, min(levels))
        except Exception:
            return None

    def runtime_requirements_current(self, spec: ActionSpec, guild_id: int) -> bool:
        """code-owned requirementsのregistry/runtime/readinessを全件fail-closedで確認する。"""

        registry = getattr(self.bot, "capability_registry", None)
        guard = getattr(self.bot, "capability_guard", None)
        if (
            isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or registry is None
            or registry is not getattr(guard, "registry", None)
        ):
            return False
        for _, capability_id, _ in spec.capability_requirements:
            refresh_runtime_readiness(self.bot, capability_id)
        readiness = getattr(self.bot, "runtime_capability_readiness", None)
        if not isinstance(readiness, dict):
            return False
        try:
            return all(
                registry.capability_status(capability_id, guild_id).executable is True
                and registry.runtime_available(capability_id) is True
                and readiness.get(capability_id) is True
                for _, capability_id, _ in spec.capability_requirements
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _currently_allowed(spec: ActionSpec, context: ActionContext) -> bool:
        checker = getattr(getattr(context.bot, "capability_guard", None), "currently_allowed", None)
        if not callable(checker):
            return False
        try:
            return all(
                checker(
                    capability_id,
                    guild_id=int(context.message.guild.id),
                    user_id=int(context.message.author.id),
                    actor_level=context.actor_level,
                    floor=floor,
                )
                is True
                for _, capability_id, floor in spec.capability_requirements
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _mention_currently_allowed(context: ActionContext) -> bool:
        checker = getattr(getattr(context.bot, "capability_guard", None), "currently_allowed", None)
        if not callable(checker):
            return False
        try:
            return bool(
                checker(
                    _MENTION_CAPABILITY_ID,
                    guild_id=int(context.message.guild.id),
                    user_id=int(context.message.author.id),
                    actor_level=context.actor_level,
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def _context(self, message: discord.Message, request: AIRequest) -> ActionContext | None:
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        settings = getattr(self.bot, "settings", None)
        if guild is None or channel is None or author is None or settings is None:
            return None
        ids = (getattr(guild, "id", None), getattr(channel, "id", None), getattr(author, "id", None))
        if not all(isinstance(value, int) and value > 0 for value in ids):
            return None
        if request.guild_id != guild.id or request.user_id != author.id:
            return None
        roles = getattr(author, "roles", ())
        role_ids = frozenset(
            int(role.id) for role in roles if isinstance(getattr(role, "id", None), int) and role.id > 0
        )
        try:
            actor_level = determine_rbac_level(
                user_id=int(author.id),
                guild_owner_id=(int(guild.owner_id) if isinstance(getattr(guild, "owner_id", None), int) else None),
                permissions=getattr(author, "guild_permissions", None),
                role_ids=role_ids,
                settings=settings,
            )
        except (AttributeError, TypeError, ValueError):
            return None
        return ActionContext(self.bot, message, request, actor_level)

    @staticmethod
    def _allowed(spec: ActionSpec, context: ActionContext) -> bool:
        guard = getattr(context.bot, "capability_guard", None)
        checker = getattr(guard, "event_allowed", None)
        if not callable(checker):
            return False
        message = context.message
        try:
            return all(
                checker(
                    capability_id,
                    surface=path,
                    guild_id=int(message.guild.id),
                    channel_id=int(message.channel.id),
                    event_id=int(message.id),
                    user_id=int(message.author.id),
                    author_is_bot=bool(getattr(message.author, "bot", False)),
                    actor_level=context.actor_level,
                )
                is True
                for path, capability_id, _ in spec.capability_requirements
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def _default_registry(self) -> ActionRegistry:
        planner_actions = _planner_action_registrations()
        execute: tuple[tuple[str, str, ActionParser, ActionExecutor], ...] = (
            ("earthquake.latest", "earthquake latest", _earthquake_parser, self._earthquake_latest),
            ("weather", "weather", _weather_parser, self._weather),
            ("warning", "warning", _warning_parser, self._warning),
            ("holiday.next", "holiday next", _holiday_parser, self._holiday_next),
            ("nasa.apod", "nasa apod", _nasa_apod_parser, self._nasa_apod),
            ("memory.remember", "memory remember", _memory_remember_parser, self._memory_remember),
            ("memory.list", "memory list", _memory_list_parser, self._memory_list),
            ("memory.search", "memory search", _memory_search_parser, self._memory_search),
            ("memory.status", "memory status", _memory_status_parser, self._memory_status),
            ("music.status", "music status", _music_status_parser, self._music_status),
            ("music.queue", "music queue", _music_queue_parser, self._music_queue),
            ("music.pause", "music pause", _music_pause_parser, self._music_pause),
            ("music.resume", "music resume", _music_resume_parser, self._music_resume),
            ("music.skip", "music skip", _music_skip_parser, self._music_skip),
            ("music.stop", "music stop", _music_stop_parser, self._music_stop),
            ("music.radio", "music radio", _music_radio_parser, self._music_radio),
            ("music.radio-invalid", "music radio", _music_radio_invalid_parser, self._music_radio_invalid),
            ("music.seek", "music seek", _music_seek_parser, self._music_seek),
            ("music.volume", "music volume", _music_volume_parser, self._music_volume),
            ("music.loop", "music loop", _music_loop_parser, self._music_loop),
            ("music.shuffle", "music shuffle", _music_shuffle_parser, self._music_shuffle),
            ("music.remove", "music remove", _music_remove_parser, self._music_remove),
            ("music.seek-invalid", "music seek", _music_seek_invalid_parser, self._music_control_invalid),
            ("music.volume-invalid", "music volume", _music_volume_invalid_parser, self._music_control_invalid),
            ("music.loop-invalid", "music loop", _music_loop_invalid_parser, self._music_control_invalid),
            ("music.shuffle-invalid", "music shuffle", _music_shuffle_invalid_parser, self._music_control_invalid),
            ("music.remove-invalid", "music remove", _music_remove_invalid_parser, self._music_control_invalid),
            ("music.leave", "music leave", _music_leave_parser, self._music_leave),
            ("music.playlist-list", "music playlist list", _music_playlist_list_parser, self._music_playlist_list),
            ("music.playlist-save", "music playlist save", _music_playlist_save_parser, self._music_playlist_save),
            ("music.playlist-load", "music playlist load", _music_playlist_load_parser, self._music_playlist_load),
            (
                "music.playlist-delete",
                "music playlist delete",
                _music_playlist_delete_parser,
                self._music_playlist_delete,
            ),
            ("music.enqueue", "music play", _planner_only_parser, self._music_enqueue),
            ("music.request", "music search-youtube", _music_request_parser, self._music_request),
            ("music.search-local", "music search", _music_local_search_parser, self._music_local_search),
            (
                "music.youtube-preview",
                "browser screenshot",
                _music_youtube_preview_parser,
                self._music_youtube_preview,
            ),
            (
                "browser.screenshot",
                "browser screenshot",
                _browser_screenshot_parser,
                self._browser_screenshot,
            ),
            (
                "browser.youtube-playback-evidence",
                "browser interact",
                _browser_youtube_playback_evidence_parser,
                self._browser_youtube_playback_evidence,
            ),
            (
                "media.url-inspect",
                "media url-inspect",
                _media_url_inspection_parser,
                self._media_url_inspect,
            ),
            (
                "discord.asset-inspect",
                "media discord-asset-inspect",
                _discord_asset_inspection_parser,
                self._discord_asset_inspect,
            ),
            (
                "discord.asset-inspect-invalid",
                "media discord-asset-inspect",
                _discord_asset_inspection_invalid_parser,
                self._discord_asset_inspect_invalid,
            ),
            ("music.speak", "music speak", _music_speak_parser, self._music_speak),
            ("music.speak-invalid", "music speak", _music_speak_invalid_parser, self._music_speak_invalid),
            ("tools.dice", "tools dice", _tools_dice_parser, self._tools_dice),
            ("tools.random", "tools random", _tools_random_parser, self._tools_random),
            ("tools.choose", "tools choose", _tools_choose_parser, self._tools_choose),
            ("tools.timestamp", "tools timestamp", _tools_timestamp_parser, self._tools_timestamp),
            ("tools.snowflake", "tools snowflake", _tools_snowflake_parser, self._tools_snowflake),
            (
                "tools.snowflake-invalid",
                "tools snowflake",
                _tools_snowflake_invalid_parser,
                self._tools_snowflake_invalid,
            ),
            ("tools.sha256", "tools sha256", _tools_sha256_parser, self._tools_sha256),
            ("tools.sha256-invalid", "tools sha256", _tools_sha256_invalid_parser, self._tools_sha256_invalid),
            ("tools.color", "tools color", _tools_color_parser, self._tools_color),
            ("tools.color-invalid", "tools color", _tools_color_invalid_parser, self._tools_color_invalid),
            ("poll.results", "poll results", _poll_results_parser, self._poll_results),
            ("poll.results-invalid", "poll results", _poll_results_invalid_parser, self._poll_results_invalid),
            ("discovery.list", "help", _feature_list_parser, self._feature_discovery),
            ("discovery.search", "help", _feature_search_parser, self._feature_discovery),
            (
                "discovery.search-invalid",
                "help",
                _feature_search_invalid_parser,
                self._feature_search_invalid,
            ),
            ("site.list", "site list", _site_list_parser, self._site_list),
            ("site.show", "site show", _site_show_parser, self._site_show),
            ("site.status", "site status", _site_status_parser, self._site_status),
            (
                "site.permission-grant",
                "system capability-set",
                _site_permission_grant_parser,
                self._site_permission_grant,
            ),
            (
                "site.permission-revoke",
                "system capability-set",
                _site_permission_revoke_parser,
                self._site_permission_revoke,
            ),
            ("mod.warnings", "mod warnings", _mod_warnings_parser, self._mod_warnings),
            ("mod.case", "mod case", _mod_case_parser, self._mod_case),
            ("schedule.list", "schedule list", _schedule_list_parser, self._schedule_list),
            ("schedule.show", "schedule show", _schedule_show_parser, self._schedule_show),
            ("schedule.create", "schedule create", _schedule_create_parser, self._schedule_create),
            ("schedule.rsvp", "schedule rsvp", _schedule_rsvp_parser, self._schedule_rsvp),
            ("image.generate", "image generate", _image_generation_parser, self._image_generate),
            ("image.edit", "image edit", _image_editing_parser, self._image_edit),
            ("video.generate", "video generate", _video_generation_parser, self._video_generate),
            ("music.generate", "musicgen generate", _confirmed_music_generation_parser, self._music_generate),
            (
                "music.generate-rights-required",
                "musicgen generate",
                _music_generation_parser,
                self._music_generation_rights_required,
            ),
        )
        unknown_planner_actions = set(planner_actions) - {action_id for action_id, _, _, _ in execute}
        if unknown_planner_actions:
            raise ValueError("planner action registration references an unknown action")
        unknown_additional_requirements = set(_ACTION_ADDITIONAL_COMMAND_PATHS) - {
            action_id for action_id, _, _, _ in execute
        }
        if unknown_additional_requirements:
            raise ValueError("additional command requirements reference an unknown action")
        specs = []
        for action_id, path, parser, executor in execute:
            planner_registration = planner_actions.get(action_id)
            planner_contract = None if planner_registration is None else planner_registration[0]
            specs.append(
                ActionSpec(
                    action_id,
                    path,
                    COMMAND_CAPABILITIES.get(path) or ACTION_CAPABILITIES[path],
                    _ACTION_RBAC_FLOORS.get(action_id, COMMAND_RBAC_FLOORS.get(path, RbacLevel.EVERYONE)),
                    parser,
                    executor,
                    effect=(ActionEffect.SIDE_EFFECT if planner_registration is None else planner_registration[1]),
                    planner_contract=planner_contract,
                    additional_command_paths=_ACTION_ADDITIONAL_COMMAND_PATHS.get(action_id, ()),
                    timeout_seconds=_ACTION_TIMEOUT_SECONDS.get(action_id),
                    output_mode=_ACTION_OUTPUT_MODES.get(action_id, ActionOutputMode.DIRECT_REPLY),
                )
            )
        specs.extend(
            (
                ActionSpec(
                    "music.rights-allow",
                    "music play",
                    COMMAND_CAPABILITIES["music play"],
                    RbacLevel.GUILD_ADMIN,
                    _music_rights_allow_parser,
                    self._music_rights_allow,
                ),
                ActionSpec(
                    "music.rights-revoke",
                    "music play",
                    COMMAND_CAPABILITIES["music play"],
                    RbacLevel.GUILD_ADMIN,
                    _music_rights_revoke_parser,
                    self._music_rights_revoke,
                ),
            )
        )
        from yonerai_discord.modules.media_pipeline.actions import build_media_action_registration

        media_registration = build_media_action_registration(self)
        specs.extend(media_registration.specs)
        defer_actions: tuple[tuple[str, str, ActionParser], ...] = (
            ("defer.mod-ban", "mod ban", _high_impact_parser(r"(?:.+を)?(?:ban|BAN|禁止|追放)して")),
            ("defer.mod-kick", "mod kick", _high_impact_parser(r"(?:.+を)?(?:kick|KICK|キック)して")),
            ("defer.ticket-close", "ticket close", _high_impact_parser(r"(?:この)?チケット(?:を)?閉じて")),
            ("defer.role-add", "server role-add", _high_impact_parser(r"(?:.+に)?.+ロール(?:を)?付けて")),
            ("defer.config", "system module-set", _high_impact_parser(r"(?:設定|モジュール)(?:を)?.+変更して")),
            (
                "defer.evolution",
                "evolution status",
                _high_impact_parser(r"(?:自己進化|自動改善)(?:を)?(?:実行|開始)して"),
            ),
        )
        specs.extend(
            ActionSpec(
                action_id,
                path,
                COMMAND_CAPABILITIES[path],
                _required_floor(path),
                parser,
                _never_execute,
                ActionMode.DEFER_TO_SLASH,
            )
            for action_id, path, parser in defer_actions
        )
        registry = ActionRegistry(specs)
        media_registration.bind_registry(registry)
        return registry

    async def _earthquake_latest(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        service = getattr(context.bot, "earthquake_service", None)
        fetch_latest = getattr(service, "fetch_latest", None)
        if not callable(fetch_latest):
            return _unavailable(
                "地震情報モジュールは現在利用できません。`/earthquake latest` で状態を確認してください。"
            )
        event = await fetch_latest()
        guard = getattr(context.bot, "capability_guard", None)
        current = getattr(guard, "currently_allowed", None)
        try:
            still_allowed = callable(current) and (
                current(
                    COMMAND_CAPABILITIES["earthquake latest"],
                    guild_id=int(context.message.guild.id),
                    user_id=int(context.message.author.id),
                    actor_level=context.actor_level,
                )
                is True
            )
            still_allowed = still_allowed and self._mention_currently_allowed(context)
        except Exception:
            still_allowed = False
        if not still_allowed:
            return ActionResult(
                ActionStatus.DENIED,
                "取得待機中に権限または機能設定が変更されたため、結果を表示しません。",
            )
        if event is None:
            return ActionResult(ActionStatus.COMPLETED, "現在取得できる地震・緊急地震速報はありません。")
        scale = _safe_text(getattr(event, "scale_label", "不明"), 20)
        location = _safe_text(getattr(event, "hypocenter_name", None) or "不明", 160)
        magnitude = getattr(event, "magnitude", None)
        depth = getattr(event, "depth_km", None)
        details = [f"最新の地震情報: 最大震度 {scale}", f"震源: {location}"]
        if magnitude is not None:
            details.append(f"マグニチュード: {float(magnitude):g}")
        if depth is not None:
            details.append(f"深さ: {int(depth)}km")
        details.append("出典: P2P地震情報API。気象庁の公式防災情報も確認してください。")
        return ActionResult(ActionStatus.COMPLETED, "\n".join(details))

    async def _weather(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        service = getattr(context.bot, "jp_information_service", None)
        getter = getattr(service, "get_weather", None)
        if not callable(getter):
            return _unavailable("天気情報モジュールは現在利用できません。`/weather` で確認してください。")
        try:
            forecast = await getter(parameters["region"])
        except UnknownRegionError:
            return ActionResult(
                ActionStatus.FAILED, "地域を一意に特定できません。都道府県名か気象庁の地域コードで指定してください。"
            )
        except JpInformationError:
            return ActionResult(
                ActionStatus.FAILED, "気象庁の天気情報を安全に取得できませんでした。少し待って再試行してください。"
            )
        lines = [f"{_safe_text(forecast.region.name, 120)}の天気予報"]
        if forecast.headline:
            lines.append(_safe_text(forecast.headline, 500))
        for period in forecast.periods[:5]:
            lines.append(f"- {_safe_text(period.area_name, 100)}: {_safe_text(period.weather, 260)}")
        lines.append(f"出典: 気象庁 {numbered_link(1, forecast.source_url)}")
        return ActionResult(ActionStatus.COMPLETED, "\n".join(lines))

    async def _warning(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        service = getattr(context.bot, "jp_information_service", None)
        getter = getattr(service, "get_warning", None)
        if not callable(getter):
            return _unavailable("警報・注意報モジュールは現在利用できません。`/warning` で確認してください。")
        try:
            report = await getter(parameters["region"])
        except UnknownRegionError:
            return ActionResult(
                ActionStatus.FAILED, "地域を一意に特定できません。都道府県名か気象庁の地域コードで指定してください。"
            )
        except JpInformationError:
            return ActionResult(
                ActionStatus.FAILED, "気象庁の警報・注意報を安全に取得できませんでした。少し待って再試行してください。"
            )
        lines = [f"{_safe_text(report.region.name, 120)}の警報・注意報"]
        if report.headline:
            lines.append(_safe_text(report.headline, 500))
        if report.warnings:
            lines.extend(
                f"- {_safe_text(item.area_name, 100)}: {_safe_text(item.name, 140)} ({_safe_text(item.status, 60)})"
                for item in report.warnings[:10]
            )
        else:
            lines.append("公式データ上、発表中の警報・注意報は見つかりませんでした。")
        lines.append(f"出典: 気象庁 {numbered_link(1, report.source_url)}")
        return ActionResult(ActionStatus.COMPLETED, "\n".join(lines))

    async def _holiday_next(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        service = getattr(context.bot, "jp_information_service", None)
        getter = getattr(service, "get_holiday_calendar", None)
        if not callable(getter):
            return _unavailable("祝日情報モジュールは現在利用できません。`/holiday next` で確認してください。")
        try:
            calendar = await getter()
            item = calendar.next_on_or_after(datetime.now(UTC).astimezone(_JST).date())
        except PublishedRangeError:
            return ActionResult(ActionStatus.FAILED, "内閣府CSVの公表範囲外です。将来の祝日は推測しません。")
        except JpInformationError:
            return ActionResult(
                ActionStatus.FAILED, "内閣府の祝日情報を安全に取得できませんでした。少し待って再試行してください。"
            )
        if item is None:
            return ActionResult(ActionStatus.COMPLETED, "内閣府CSVの公表範囲内に次の祝日はありません。")
        return ActionResult(
            ActionStatus.COMPLETED,
            f"次の祝日は {item.day.isoformat()} の「{_safe_text(item.name, 120)}」です。"
            f"\n出典: 内閣府 {numbered_link(1, calendar.source_url)}",
        )

    async def _nasa_apod(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        service = getattr(context.bot, "nasa_apod_service", None)
        getter = getattr(service, "get", None)
        if not callable(getter):
            return _unavailable("NASA APODモジュールは現在利用できません。`/nasa apod` で確認してください。")
        try:
            item = await getter(parameters.get("date") or None)
        except ApodDateError:
            return ActionResult(
                ActionStatus.FAILED,
                "日付は1995-06-16から今日までの `YYYY-MM-DD` で指定してください。",
            )
        except NasaApodError:
            return ActionResult(
                ActionStatus.FAILED,
                "NASA APODを安全に取得できませんでした。詳細な応答や認証情報は表示しません。",
            )
        guard = getattr(context.bot, "capability_guard", None)
        current = getattr(guard, "currently_allowed", None)
        if not callable(current):
            return ActionResult(ActionStatus.DENIED, "送信前に機能の現在状態を確認できませんでした。")
        try:
            still_allowed = bool(
                current(
                    COMMAND_CAPABILITIES["nasa apod"],
                    guild_id=int(context.message.guild.id),
                    user_id=int(context.message.author.id),
                    actor_level=context.actor_level,
                )
            ) and self._mention_currently_allowed(context)
        except (AttributeError, KeyError, TypeError, ValueError):
            still_allowed = False
        if not still_allowed:
            return ActionResult(
                ActionStatus.DENIED,
                "取得待機中に権限または機能設定が変更されたため、結果を表示しません。",
            )
        return ActionResult(ActionStatus.COMPLETED, render_apod_text(item))

    async def _image_generate(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        """明示メンションの画像生成を既存の型付き adapter へだけ橋渡しする。"""

        prompt = str(parameters.get("prompt", "")).strip()
        if not prompt or len(prompt) > 1_100:
            return ActionResult(
                ActionStatus.DEFERRED,
                "画像の内容を指定してください。例: `@BOT 画像を作って: 青い猫`",
            )
        if not await self._image_generation_current(context):
            return ActionResult(
                ActionStatus.DENIED,
                "この画像生成は現在の権限またはサーバーポリシーでは利用できません。",
            )
        adapter = getattr(context.bot, "image_generation_adapter", None)
        generate = getattr(adapter, "generate_for_message", None)
        if not callable(generate):
            return _unavailable("画像生成は現在利用できません。`/image generate` で状態を確認してください。")

        async def authorization_current() -> bool:
            return await self._image_generation_current(context)

        try:
            delivered = await generate(
                context.message,
                prompt=prompt,
                authorization_current=authorization_current,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "image_generation_mention_failed",
                extra={"error_type": type(exc).__name__},
            )
            delivered = False
        if delivered is not True:
            return _unavailable("画像生成は現在利用できません。`/image generate` で状態を確認してください。")
        return ActionResult(ActionStatus.COMPLETED, "画像をこのメッセージへの返信に添付しました。")

    async def _image_generation_current(self, context: ActionContext) -> bool:
        """開始・provider・配信境界でREST memberを使って二capabilityを再確認する。"""

        if self._closing_now:
            return False
        message = context.message
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        guard = getattr(context.bot, "capability_guard", None)
        fetch_member = getattr(guild, "fetch_member", None)
        evaluate = getattr(guard, "evaluate_fresh_member", None)
        current = getattr(guard, "currently_allowed", None)
        user_id = getattr(author, "id", None)
        guild_id = getattr(guild, "id", None)
        if (
            not isinstance(user_id, int)
            or user_id <= 0
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or not callable(fetch_member)
            or not callable(evaluate)
            or not callable(current)
        ):
            return False
        try:
            member = await fetch_member(user_id)
            if getattr(member, "id", None) != user_id:
                return False
            for capability_id, floor in (
                (COMMAND_CAPABILITIES["image generate"], RbacLevel.TRUSTED),
                (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
            ):
                decision = await evaluate(capability_id, guild=guild, member=member)
                actor_level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
                if (
                    getattr(decision, "allowed", False) is not True
                    or actor_level < floor
                    or current(
                        capability_id,
                        guild_id=guild_id,
                        user_id=user_id,
                        actor_level=actor_level,
                        floor=floor,
                    )
                    is not True
                ):
                    return False
            return not self._closing_now
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    async def _image_edit(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        instruction = str(parameters.get("instruction", "")).strip()
        if not instruction:
            return ActionResult(ActionStatus.DEFERRED, "編集指示を添えて、PNG を1枚だけ付けて再試行してください。")
        if not await self._image_editing_current(context):
            return ActionResult(ActionStatus.DENIED, "画像編集は現在の権限またはポリシーでは利用できません。")
        adapter = getattr(context.bot, "image_editing_adapter", None)
        edit = getattr(adapter, "edit_for_message", None)
        if not callable(edit):
            return _unavailable("画像編集は現在利用できません。設定と有効化を確認してから再試行してください。")

        async def authorization_current() -> bool:
            return await self._image_editing_current(context)

        try:
            delivered = await edit(
                context.message,
                instruction=instruction,
                authorization_current=authorization_current,
                settings=getattr(context.bot, "settings", None),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("image_editing_mention_failed", extra={"error_type": type(exc).__name__})
            delivered = False
        if delivered is not True:
            return _unavailable(
                "画像編集を完了できませんでした。PNG を1枚だけ付け、設定と同意を確認して再試行してください。"
            )
        return ActionResult(ActionStatus.COMPLETED, "編集済み画像をこのメッセージへの返信として送信しました。")

    async def _image_editing_current(self, context: ActionContext) -> bool:
        if self._closing_now:
            return False
        try:
            spec = self.registry.get("image.edit")
            if not self.runtime_requirements_current(spec, int(context.message.guild.id)):
                return False
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        message = context.message
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        guard = getattr(context.bot, "capability_guard", None)
        fetch_member = getattr(guild, "fetch_member", None)
        permissions_for = getattr(channel, "permissions_for", None)
        evaluate = getattr(guard, "evaluate_fresh_member", None)
        current = getattr(guard, "currently_allowed", None)
        user_id, guild_id = getattr(author, "id", None), getattr(guild, "id", None)
        if not all(isinstance(value, int) and value > 0 for value in (user_id, guild_id)) or not all(
            callable(value) for value in (fetch_member, permissions_for, evaluate, current)
        ):
            return False
        try:
            member = await fetch_member(user_id)
            if getattr(member, "id", None) != user_id:
                return False
            permissions = permissions_for(member)
            if (
                getattr(permissions, "view_channel", False) is not True
                or getattr(permissions, "read_message_history", False) is not True
            ):
                return False
            for capability_id, floor in (
                (ACTION_CAPABILITIES["image edit"], RbacLevel.TRUSTED),
                (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
            ):
                decision = await evaluate(capability_id, guild=guild, member=member)
                level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
                if (
                    getattr(decision, "allowed", False) is not True
                    or level < floor
                    or current(capability_id, guild_id=guild_id, user_id=user_id, actor_level=level, floor=floor)
                    is not True
                ):
                    return False
            return not self._closing_now
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    async def _video_generate(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        """明示メンションの動画生成を既存の型付き adapter へだけ橋渡しする。"""

        prompt = str(parameters.get("prompt", "")).strip()
        if not prompt or len(prompt) > 1_100:
            return ActionResult(
                ActionStatus.DEFERRED,
                "動画の内容を指定してください。例: `@BOT 動画を作って: 青い海`",
            )
        if not await self._video_generation_current(context):
            return ActionResult(
                ActionStatus.DENIED,
                "この動画生成は現在の権限またはサーバーポリシーでは利用できません。",
            )
        adapter = getattr(context.bot, "video_generation_adapter", None)
        generate = getattr(adapter, "generate_for_message", None)
        if not callable(generate):
            return _unavailable("動画生成は現在利用できません。`/video generate` で状態を確認してください。")

        async def authorization_current() -> bool:
            return await self._video_generation_current(context)

        try:
            delivered = await generate(
                context.message,
                prompt=prompt,
                authorization_current=authorization_current,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "video_generation_mention_failed",
                extra={"error_type": type(exc).__name__},
            )
            delivered = False
        if delivered is not True:
            return _unavailable("動画生成は現在利用できません。`/video generate` で状態を確認してください。")
        return ActionResult(ActionStatus.COMPLETED, "動画をこのメッセージへの返信に添付しました。")

    async def _video_generation_current(self, context: ActionContext) -> bool:
        """開始・provider・配信境界でREST memberを使って二capabilityを再確認する。"""

        if self._closing_now:
            return False
        message = context.message
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        guard = getattr(context.bot, "capability_guard", None)
        fetch_member = getattr(guild, "fetch_member", None)
        evaluate = getattr(guard, "evaluate_fresh_member", None)
        current = getattr(guard, "currently_allowed", None)
        user_id = getattr(author, "id", None)
        guild_id = getattr(guild, "id", None)
        if (
            not isinstance(user_id, int)
            or user_id <= 0
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or not callable(fetch_member)
            or not callable(evaluate)
            or not callable(current)
        ):
            return False
        try:
            member = await fetch_member(user_id)
            if getattr(member, "id", None) != user_id:
                return False
            for capability_id, floor in (
                (COMMAND_CAPABILITIES["video generate"], RbacLevel.TRUSTED),
                (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
            ):
                decision = await evaluate(capability_id, guild=guild, member=member)
                actor_level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
                if (
                    getattr(decision, "allowed", False) is not True
                    or actor_level < floor
                    or current(
                        capability_id,
                        guild_id=guild_id,
                        user_id=user_id,
                        actor_level=actor_level,
                        floor=floor,
                    )
                    is not True
                ):
                    return False
            return not self._closing_now
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    async def _music_generation_rights_required(self, _: ActionContext, __: Mapping[str, str]) -> ActionResult:
        return ActionResult(
            ActionStatus.DEFERRED,
            "権利を確認済みなら `@BOT 権利確認済みで音楽を作って: 内容` と再送してください。",
        )

    async def _music_generate(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        """権利確認済みの明示メンションだけを既存音楽生成adapterへ橋渡しする。"""

        prompt = str(parameters.get("prompt", "")).strip()
        if not prompt or len(prompt) > 1_100:
            return ActionResult(
                ActionStatus.DEFERRED,
                "音楽の内容を指定してください。権利を確認済みなら `@BOT 権利確認済みで音楽を作って: 内容` を使ってください。",
            )
        if not await self._music_generation_current(context):
            return ActionResult(
                ActionStatus.DENIED,
                "この音楽生成は現在の権限またはサーバーポリシーでは利用できません。",
            )
        adapter = getattr(context.bot, "music_generation_adapter", None)
        generate = getattr(adapter, "generate_for_message", None)
        if not callable(generate):
            return _unavailable("音楽生成は現在利用できません。`/musicgen generate` で状態を確認してください。")

        async def authorization_current() -> bool:
            return await self._music_generation_current(context)

        try:
            delivered = await generate(
                context.message,
                prompt=prompt,
                authorization_current=authorization_current,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "music_generation_mention_failed",
                extra={"error_type": type(exc).__name__},
            )
            delivered = False
        if delivered is not True:
            return _unavailable("音楽生成は現在利用できません。`/musicgen generate` で状態を確認してください。")
        return ActionResult(ActionStatus.COMPLETED, "音楽をこのメッセージへの返信に添付しました。")

    async def _music_generation_current(self, context: ActionContext) -> bool:
        """開始・provider・配信境界でREST memberを使って二capabilityを再確認する。"""

        if self._closing_now:
            return False
        message = context.message
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        guard = getattr(context.bot, "capability_guard", None)
        fetch_member = getattr(guild, "fetch_member", None)
        evaluate = getattr(guard, "evaluate_fresh_member", None)
        current = getattr(guard, "currently_allowed", None)
        user_id = getattr(author, "id", None)
        guild_id = getattr(guild, "id", None)
        if (
            not isinstance(user_id, int)
            or user_id <= 0
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or not callable(fetch_member)
            or not callable(evaluate)
            or not callable(current)
        ):
            return False
        try:
            member = await fetch_member(user_id)
            if getattr(member, "id", None) != user_id:
                return False
            for capability_id, floor in (
                (COMMAND_CAPABILITIES["musicgen generate"], RbacLevel.TRUSTED),
                (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
            ):
                decision = await evaluate(capability_id, guild=guild, member=member)
                actor_level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
                if (
                    getattr(decision, "allowed", False) is not True
                    or actor_level < floor
                    or current(
                        capability_id,
                        guild_id=guild_id,
                        user_id=user_id,
                        actor_level=actor_level,
                        floor=floor,
                    )
                    is not True
                ):
                    return False
            return not self._closing_now
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    async def _memory_remember(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        service = getattr(context.bot, "personal_memory_service", None)
        remember = getattr(service, "remember", None)
        if not callable(remember):
            return _unavailable("個人メモリモジュールは現在利用できません。")
        try:
            item = await asyncio.to_thread(
                remember,
                context.request.guild_id,
                context.request.user_id,
                parameters["text"],
            )
        except MemoryDisabledError:
            return ActionResult(
                ActionStatus.DENIED, "個人メモリはOFFです。先に `/memory enable` で本人同意を行ってください。"
            )
        except SensitiveMemoryError:
            return ActionResult(
                ActionStatus.DENIED, "トークン、APIキー、パスワードなどの秘密らしい内容は保存しません。"
            )
        except ValueError:
            return ActionResult(ActionStatus.FAILED, "保存内容は1〜1000文字で指定してください。")
        return ActionResult(ActionStatus.COMPLETED, f"本人専用メモ `{int(item.id)}` として保存しました。")

    async def _memory_list(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        service = getattr(context.bot, "personal_memory_service", None)
        list_items = getattr(service, "list_items", None)
        is_enabled = getattr(service, "is_enabled", None)
        if not callable(list_items) or not callable(is_enabled):
            return _unavailable("個人メモリモジュールは現在利用できません。")
        enabled = await asyncio.to_thread(is_enabled, context.request.guild_id, context.request.user_id)
        if not enabled:
            return ActionResult(
                ActionStatus.DENIED, "個人メモリはOFFです。先に `/memory enable` で本人同意を行ってください。"
            )
        items = await asyncio.to_thread(
            list_items,
            context.request.guild_id,
            context.request.user_id,
            limit=10,
        )
        return _memory_items_result(items)

    async def _memory_search(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        service = getattr(context.bot, "personal_memory_service", None)
        search = getattr(service, "search", None)
        is_enabled = getattr(service, "is_enabled", None)
        if not callable(search) or not callable(is_enabled):
            return _unavailable("個人メモリモジュールは現在利用できません。")
        enabled = await asyncio.to_thread(is_enabled, context.request.guild_id, context.request.user_id)
        if not enabled:
            return ActionResult(
                ActionStatus.DENIED, "個人メモリはOFFです。先に `/memory enable` で本人同意を行ってください。"
            )
        items = await asyncio.to_thread(
            search,
            context.request.guild_id,
            context.request.user_id,
            parameters["query"],
            limit=5,
        )
        return _memory_items_result(items)

    async def _memory_status(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        service = await _current_personal_memory_service(context)
        if service is None:
            return ActionResult(ActionStatus.DENIED, "個人メモリの状態を安全に確認できないため、表示しませんでした。")
        try:
            enabled = await asyncio.to_thread(service.is_enabled, context.request.guild_id, context.request.user_id)
            items = await asyncio.to_thread(
                service.list_items,
                context.request.guild_id,
                context.request.user_id,
                limit=100,
            )
        except Exception:
            return ActionResult(ActionStatus.FAILED, "個人メモリの状態を安全に取得できませんでした。")
        if not await asyncio.to_thread(service.is_enabled, context.request.guild_id, context.request.user_id):
            enabled, items = False, ()
        if await _current_personal_memory_service(context) is not service:
            return ActionResult(ActionStatus.DENIED, "個人メモリの状態を安全に確認できないため、表示しませんでした。")
        counts = {kind: sum(getattr(item, "kind", None) is kind for item in items) for kind in MemoryKind}
        return ActionResult(
            ActionStatus.COMPLETED,
            f"個人メモリ: {'ON' if enabled else 'OFF'}\n保存中: {len(items)}件"
            f"（明示メモ {counts[MemoryKind.FACT]} / 会話 {counts[MemoryKind.CONVERSATION]}）\n"
            f"会話保持: {CONVERSATION_RETENTION_SECONDS // 86400}日 / 明示メモ: {FACT_RETENTION_SECONDS // 86400}日",
        )

    async def _music_status(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        service = getattr(context.bot, "music_service", None)
        status = getattr(service, "status", None)
        if not callable(status):
            return _unavailable("音楽モジュールは現在利用できません。")
        speech_queue = getattr(context.bot, "speech_queue", None)
        snapshot = status(speech_available=bool(getattr(speech_queue, "available", False)))
        state = "利用可能" if snapshot.available else f"利用不可 ({_safe_text(snapshot.reason, 100)})"
        return ActionResult(
            ActionStatus.COMPLETED,
            f"音楽: {state}\nローカル曲: {snapshot.indexed_tracks}曲\n接続中: {snapshot.active_sessions}サーバー",
        )

    async def _music_queue(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, _ = _music_context(context)
            snapshot = await service.snapshot(guild_id)
        except MusicError as exc:
            return _music_error(exc)
        lines = [f"再生中: {_safe_text(snapshot.current.title, 200)}" if snapshot.current else "再生中: なし"]
        lines.extend(
            f"{index}. {_safe_text(track.title, 200)}" for index, track in enumerate(snapshot.upcoming[:15], 1)
        )
        return ActionResult(ActionStatus.COMPLETED, "\n".join(lines))

    async def _music_enqueue(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        raw_query = parameters.get("query")
        if not isinstance(raw_query, str):
            return ActionResult(ActionStatus.DENIED, "曲名を1件指定してください。")
        query = " ".join(unicodedata.normalize("NFKC", raw_query).strip().split())
        if (
            not 1 <= len(query) <= _MAX_QUERY_CHARS
            or _ANY_DISCORD_MENTION.search(query)
            or any(unicodedata.category(character).startswith("C") for character in query)
        ):
            return ActionResult(ActionStatus.DENIED, "曲名を1件、200文字以内の通常テキストで指定してください。")
        try:
            spec = self.registry.get("music.enqueue")
            service, guild_id, _ = _music_context(context)
            if getattr(service, "available", False) is not True or not self.runtime_requirements_current(
                spec, guild_id
            ):
                return _unavailable("音楽モジュールは現在利用できません。")
            base_search_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music search",
                extra_capability_ids=(
                    _MENTION_CAPABILITY_ID,
                    COMMAND_CAPABILITIES["music play"],
                ),
            )
            if base_search_check is None:
                return ActionResult(
                    ActionStatus.DENIED,
                    "ローカル曲の検索前に権限を再確認できないため、追加しませんでした。",
                )

            async def search_check() -> MusicActor | None:
                if not self.runtime_requirements_current(spec, guild_id):
                    return None
                return await base_search_check()

            actor = await search_check()
            if actor is None:
                return ActionResult(
                    ActionStatus.DENIED,
                    "ローカル曲の検索前に権限を再確認できないため、追加しませんでした。",
                )
            candidates = await service.search(query, actor, guild_id=guild_id, limit=25)
            if (
                await search_check() is None
                or getattr(context.bot, "music_service", None) is not service
                or getattr(service, "available", False) is not True
            ):
                return ActionResult(
                    ActionStatus.DENIED,
                    "検索後に権限または音楽機能の設定が変更されたため、追加しませんでした。",
                )
        except MusicError as exc:
            return _music_error(exc)
        except (AttributeError, KeyError, TypeError, ValueError):
            return ActionResult(ActionStatus.DENIED, "ローカル曲を安全に特定できないため、追加しませんでした。")
        selected = _unique_music_match(query, candidates)
        if selected is None:
            return ActionResult(
                ActionStatus.DEFERRED,
                "許可済みローカル曲を1件に特定できませんでした。曲名をより正確に指定してください。",
            )
        return await self._enqueue_selected_music(
            context,
            service,
            guild_id,
            selected,
            runtime_spec=spec,
        )

    async def _music_request(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        query = parameters["query"]
        if not query:
            return ActionResult(
                ActionStatus.DEFERRED,
                "曲名を指定してください。例: `@BOT 混沌ブギながして`",
            )
        try:
            service = getattr(context.bot, "music_service", None)
            guild_id = int(context.message.guild.id)
            candidates = ()
            searched_local = False
            if bool(getattr(service, "available", False)):
                service, guild_id, actor = _music_context(context)
                search_check = await build_music_commit_check(
                    context.bot,
                    context.message.guild,
                    int(context.message.author.id),
                    "music search",
                    extra_capability_ids=(_MENTION_CAPABILITY_ID,),
                )
                fresh_actor = await search_check() if search_check is not None else None
                if fresh_actor is not None:
                    actor = fresh_actor
                    candidates = await service.search(query, actor, guild_id=guild_id, limit=4)
                    searched_local = True
            if searched_local:
                search_check = await build_music_commit_check(
                    context.bot,
                    context.message.guild,
                    int(context.message.author.id),
                    "music search",
                    extra_capability_ids=(_MENTION_CAPABILITY_ID,),
                )
                if search_check is None or await search_check() is None:
                    candidates = ()
        except MusicError as exc:
            return _music_error(exc)
        selected = _unique_music_match(query, candidates)
        if selected is None:
            if not candidates:
                link_check = await build_music_commit_check(
                    context.bot,
                    context.message.guild,
                    int(context.message.author.id),
                    "music search-youtube",
                    extra_capability_ids=(_MENTION_CAPABILITY_ID,),
                )
                if link_check is None or await link_check() is None:
                    return ActionResult(
                        ActionStatus.DENIED,
                        "権限または音楽機能の設定が変更されたため、YouTube検索リンクを生成しませんでした。",
                    )
                url = youtube_search_url(query)
                return ActionResult(
                    ActionStatus.DEFERRED,
                    "許可済みローカルライブラリに一致する曲がありません。\n"
                    f"YouTube公式検索（リンクのみ）: {numbered_link(1, url)}\n"
                    "音声抽出・ダウンロード・VC中継は行いません。",
                )
            candidate_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music search",
                extra_capability_ids=(_MENTION_CAPABILITY_ID,),
            )
            if candidate_check is None or await candidate_check() is None:
                return ActionResult(
                    ActionStatus.DENIED,
                    "権限または音楽機能の設定が変更されたため、ローカル曲の候補を表示しませんでした。",
                )
            names = "、".join(_safe_text(track.title, 100) for track in candidates[:4])
            return ActionResult(
                ActionStatus.DEFERRED,
                f"複数の候補があります: {names}\n曲名をもう少し正確に指定してください。",
            )
        return await self._enqueue_selected_music(context, service, guild_id, selected)

    async def _browser_screenshot(
        self,
        context: ActionContext,
        parameters: Mapping[str, str],
    ) -> ActionResult:
        return await self._capture_remote_browser_screenshot(
            context,
            action_id="browser.screenshot",
            url=parameters["url"],
            target_kind="url",
        )

    async def _browser_youtube_playback_evidence(
        self,
        context: ActionContext,
        parameters: Mapping[str, str],
    ) -> ActionResult:
        if parameters.get("invalid") == "1":
            return ActionResult(
                ActionStatus.DEFERRED,
                "YouTube検索語を1〜200文字で指定し、"
                "`YouTubeで 検索語 を検索して、先頭候補を開いて再生し、"
                "途中と再生後をスクショして` の完全な書式で送ってください。",
            )
        try:
            spec = self.registry.get("browser.youtube-playback-evidence")
        except KeyError:
            return _unavailable("YouTube画面操作は現在利用できません。")
        plugin = getattr(context.bot, "browser_rendering_plugin", None)
        adapter = getattr(context.bot, "browser_run_adapter", None)
        runner = getattr(context.bot, "remote_browser_run_service", None)
        store = getattr(context.bot, "media_pipeline_store", None)
        run = getattr(adapter, "run_youtube_for_message", None)
        if not callable(run):
            return _unavailable(
                "YouTube画面操作は現在利用できません。Browser RunとMedia Artifact Storeの設定を確認してください。"
            )
        fresh_context: ActionContext | None = None

        def identities_current(current: ActionContext | None) -> bool:
            status = getattr(context.bot, "remote_browser_interactive_status", None)
            return (
                current is not None
                and plugin is not None
                and adapter is not None
                and runner is not None
                and store is not None
                and not self._closing_now
                and getattr(context.bot, "browser_rendering_plugin", None) is plugin
                and getattr(plugin, "interactive_adapter", None) is adapter
                and getattr(plugin, "interactive_runner", None) is runner
                and getattr(context.bot, "browser_run_adapter", None) is adapter
                and getattr(context.bot, "remote_browser_run_service", None) is runner
                and getattr(context.bot, "media_pipeline_store", None) is store
                and getattr(adapter, "closing", True) is False
                and getattr(status, "ready", False) is True
                and self.runtime_requirements_current(spec, int(context.message.guild.id))
                and self._currently_allowed(spec, current)
                and self._mention_currently_allowed(current)
            )

        async def authorization_current() -> bool:
            nonlocal fresh_context
            try:
                refreshed = await self.fresh_context_for_spec(spec, context.message, context.request)
            except asyncio.CancelledError:
                raise
            except Exception:
                refreshed = None
            fresh_context = refreshed
            return identities_current(refreshed)

        def authorization_current_sync() -> bool:
            return identities_current(fresh_context)

        if not await authorization_current():
            return ActionResult(
                ActionStatus.DENIED,
                "YouTube画面操作は現在の権限またはサーバーポリシーでは利用できません。",
            )
        try:
            delivered = await run(
                context.message,
                query=parameters["query"],
                authorization_current=authorization_current,
                authorization_current_sync=authorization_current_sync,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "browser_interactive_mention_failed",
                extra={"action_id": spec.action_id, "error_type": type(exc).__name__},
            )
            delivered = False
        if delivered is not True:
            return _unavailable(
                "YouTubeの検索・再生画面を安全に取得できませんでした。設定・権限を確認して再試行してください。"
            )
        return ActionResult(
            ActionStatus.COMPLETED,
            "YouTubeの検索結果画面と再生後画面を同じ進捗メッセージへ添付しました。",
            delivery_handled=True,
        )

    async def _media_url_inspect(
        self,
        context: ActionContext,
        parameters: Mapping[str, str],
    ) -> ActionResult:
        if not await self._media_url_inspection_current(context):
            return ActionResult(
                ActionStatus.DENIED,
                "動画URL解析は現在の権限またはサーバーポリシーでは利用できません。",
            )
        adapter = getattr(context.bot, "media_url_inspection_adapter", None)
        if _media_inspection_requires_external_ai_consent(adapter) and not self._remote_ai_consent_current(context):
            return ActionResult(
                ActionStatus.DENIED,
                "外部AIへ動画URLを送信する初回同意が必要です。同じ依頼をもう一度送って同意してください。",
            )
        inspect = getattr(adapter, "inspect_for_message", None)
        if not callable(inspect):
            return _unavailable("動画URL解析Providerは現在利用できません。module設定とcredentialを確認してください。")

        async def authorization_current() -> bool:
            if not await self._media_url_inspection_current(context):
                return False
            return not _media_inspection_requires_external_ai_consent(adapter) or self._remote_ai_consent_current(
                context
            )

        try:
            text = await inspect(
                context.message,
                url=parameters["url"],
                instruction=parameters["instruction"],
                authorization_current=authorization_current,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "media_url_inspection_mention_failed",
                extra={"error_type": type(exc).__name__},
            )
            text = None
        if not isinstance(text, str) or not text.strip():
            return _unavailable("動画URLを根拠付きで解析できませんでした。推測回答には切り替えていません。")
        if not await authorization_current():
            return ActionResult(
                ActionStatus.DENIED,
                "解析中に権限または外部AI送信の同意状態が変わったため、結果を表示しません。",
            )
        return ActionResult(
            ActionStatus.COMPLETED,
            "動画解析の根拠を取得しました。",
            model_evidence=text,
        )

    async def _music_youtube_preview(
        self,
        context: ActionContext,
        parameters: Mapping[str, str],
    ) -> ActionResult:
        query = parameters["query"]
        try:
            url = youtube_search_url(query)
        except (TypeError, ValueError):
            return ActionResult(ActionStatus.DEFERRED, "曲名を200文字以内で指定してください。")
        return await self._capture_remote_browser_screenshot(
            context,
            action_id="music.youtube-preview",
            url=url,
            target_kind="youtube_search",
        )

    async def _capture_remote_browser_screenshot(
        self,
        context: ActionContext,
        *,
        action_id: str,
        url: str,
        target_kind: str,
    ) -> ActionResult:
        if not await self._remote_browser_screenshot_current(context, action_id):
            return ActionResult(
                ActionStatus.DENIED,
                "Webスクリーンショットは現在の権限またはサーバーポリシーでは利用できません。",
            )
        adapter = getattr(context.bot, "browser_rendering_adapter", None)
        capture = getattr(adapter, "capture_for_message", None)
        if not callable(capture):
            return _unavailable(
                "Webスクリーンショットは現在利用できません。remote browserの設定と有効化を確認してください。"
            )

        async def authorization_current() -> bool:
            return await self._remote_browser_screenshot_current(context, action_id)

        try:
            delivered = await capture(
                context.message,
                url=url,
                authorization_current=authorization_current,
                target_kind=target_kind,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "browser_rendering_mention_failed",
                extra={"action_id": action_id, "error_type": type(exc).__name__},
            )
            delivered = False
        if delivered is not True:
            return _unavailable(
                "Webスクリーンショットを完了できませんでした。URL・設定・権限を確認して再試行してください。"
            )
        return ActionResult(
            ActionStatus.COMPLETED,
            "Webスクリーンショットをこのメッセージへの返信として送信しました。",
        )

    async def _remote_browser_screenshot_current(self, context: ActionContext, action_id: str) -> bool:
        if self._closing_now:
            return False
        try:
            spec = self.registry.get(action_id)
            fresh = await self.fresh_context_for_spec(spec, context.message, context.request)
            return fresh is not None and not self._closing_now
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    async def _media_url_inspection_current(self, context: ActionContext) -> bool:
        if self._closing_now:
            return False
        try:
            spec = self.registry.get("media.url-inspect")
            fresh = await self.fresh_context_for_spec(spec, context.message, context.request)
            return fresh is not None and not self._closing_now
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    async def _discord_asset_inspect(
        self,
        context: ActionContext,
        parameters: Mapping[str, str],
    ) -> ActionResult:
        plugin = getattr(context.bot, "media_pipeline_plugin", None)
        if type(plugin) is not MediaPipelinePlugin or not MediaPipelinePlugin.is_current_for(plugin, context.bot):
            return ActionResult(
                ActionStatus.DENIED,
                "絵文字またはスタンプを安全に確認できないため、表示しませんでした。",
            )
        try:
            spec = self.registry.get("discord.asset-inspect")
            current = await self.fresh_context_for_spec(spec, context.message, context.request)
        except (AttributeError, KeyError, TypeError, ValueError):
            current = None
        if current is None or self._closing_now:
            return ActionResult(
                ActionStatus.DENIED,
                "絵文字またはスタンプを安全に確認できないため、表示しませんでした。",
            )
        guild_id = int(current.message.guild.id)
        try:
            if parameters.get("kind") == DiscordAssetKind.CUSTOM_EMOJI.value:
                result = inspect_custom_emoji(
                    EmojiAssetInspectionRequest(
                        guild_id=guild_id,
                        mention=parameters["mention"],
                    )
                )
            elif parameters.get("kind") == DiscordAssetKind.STICKER.value:
                stickers = tuple(getattr(current.message, "stickers", ()))
                if len(stickers) != 1:
                    raise DiscordAssetInspectionError("exactly one sticker is required")
                sticker = stickers[0]
                result = inspect_sticker(
                    StickerAssetInspectionRequest(
                        StickerAssetFacts(
                            guild_id=guild_id,
                            asset_id=int(sticker.id),
                            name=str(sticker.name),
                            format=_discord_sticker_format(getattr(sticker, "format", None)),
                        )
                    )
                )
            else:
                raise DiscordAssetInspectionError("asset kind is invalid")
        except (AttributeError, KeyError, TypeError, ValueError, DiscordAssetInspectionError):
            return ActionResult(
                ActionStatus.DENIED,
                "絵文字またはスタンプを1件だけ、対応する形式で指定してください。",
            )
        if (
            not MediaPipelinePlugin.is_current_for(plugin, context.bot)
            or not self.runtime_requirements_current(spec, guild_id)
            or not self._currently_allowed(spec, current)
            or not self._mention_currently_allowed(current)
        ):
            return ActionResult(
                ActionStatus.DENIED,
                "確認中に権限または機能設定が変更されたため、結果を表示しませんでした。",
            )
        if result.kind is DiscordAssetKind.CUSTOM_EMOJI:
            animation = "アニメーション" if result.animated else "静止画"
            return ActionResult(
                ActionStatus.COMPLETED,
                f"カスタム絵文字: {_safe_text(result.display_name, 100)}\n形式: {animation}\nID: `{result.asset_id}`",
            )
        return ActionResult(
            ActionStatus.COMPLETED,
            f"スタンプ: {_safe_text(result.display_name, 100)}\n"
            f"形式: {result.format.value.upper()}\nID: `{result.asset_id}`",
        )

    async def _discord_asset_inspect_invalid(
        self,
        _: ActionContext,
        __: Mapping[str, str],
    ) -> ActionResult:
        return ActionResult(
            ActionStatus.DENIED,
            "絵文字は `この絵文字を調べて: <:name:ID>`、"
            "スタンプは1件だけ添えて `このスタンプを調べて` と送ってください。",
        )

    @staticmethod
    def _remote_ai_consent_current(context: ActionContext) -> bool:
        store = getattr(context.bot, "ai_remote_consent_store", None)
        active = getattr(store, "active", None)
        guild = getattr(context.message, "guild", None)
        channel = getattr(context.message, "channel", None)
        author = getattr(context.message, "author", None)
        if not callable(active) or guild is None or channel is None or author is None:
            return False
        try:
            return bool(
                active(
                    guild_id=int(guild.id),
                    channel_id=int(channel.id),
                    user_id=int(author.id),
                )
            )
        except (AttributeError, TypeError, ValueError):
            return False

    async def _enqueue_selected_music(
        self,
        context: ActionContext,
        service: Any,
        guild_id: int,
        selected: Any,
        *,
        runtime_spec: ActionSpec | None = None,
    ) -> ActionResult:
        additional_capability_ids = (
            ()
            if runtime_spec is None
            else tuple(
                capability_id
                for _, capability_id, _ in runtime_spec.capability_requirements
                if capability_id != COMMAND_CAPABILITIES["music play"]
            )
        )

        async def make_play_check() -> Callable[[], Awaitable[MusicActor | None]] | None:
            base_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music play",
                extra_capability_ids=(
                    _MENTION_CAPABILITY_ID,
                    *additional_capability_ids,
                ),
            )
            if base_check is None:
                return None

            async def current() -> MusicActor | None:
                if (
                    getattr(context.bot, "music_service", None) is not service
                    or getattr(service, "available", False) is not True
                    or self._closing_now
                    or (runtime_spec is not None and not self.runtime_requirements_current(runtime_spec, guild_id))
                ):
                    return None
                fresh_actor = await base_check()
                if (
                    getattr(context.bot, "music_service", None) is not service
                    or getattr(service, "available", False) is not True
                    or self._closing_now
                    or (runtime_spec is not None and not self.runtime_requirements_current(runtime_spec, guild_id))
                ):
                    return None
                return fresh_actor

            return current

        play_check = await make_play_check()
        actor = await play_check() if play_check is not None else None
        channel = _voice_channel(context.message.author)
        if (
            actor is None
            or (channel is None and actor.voice_channel_id is not None)
            or (channel is not None and actor.voice_channel_id != int(channel.id))
        ):
            return ActionResult(
                ActionStatus.DENIED,
                "再生開始前に権限、音楽機能、VC参加状態を再確認できなかったため、接続しませんでした。",
            )
        queued_without_voice = channel is None
        connected_here = False
        voice_client = getattr(context.message.guild, "voice_client", None)
        try:
            if service.session_channel_id(guild_id) is None:
                if voice_client is not None:
                    return ActionResult(ActionStatus.UNAVAILABLE, "別の音声機能がこのサーバーのVC接続を使用中です。")
                if channel is not None:
                    voice_client = await channel.connect(self_deaf=True, reconnect=False)
                    connected_here = True
                    commit_check = await make_play_check()
                    if commit_check is None or await commit_check() is None:
                        await _rollback_music_join(service, guild_id, voice_client)
                        return ActionResult(
                            ActionStatus.DENIED,
                            "接続待機中に権限または音楽機能の設定が変更されたため、接続を取り消しました。",
                        )
                    await service.join(
                        guild_id,
                        voice_client,
                        actor,
                        voice_channel_id=int(channel.id),
                        commit_check=commit_check,
                    )
            commit_check = await make_play_check()
            if commit_check is None:
                if connected_here and voice_client is not None:
                    await _rollback_music_join(service, guild_id, voice_client)
                return ActionResult(
                    ActionStatus.DENIED,
                    "再生開始前に権限を再確認できなかったため、曲を追加しませんでした。",
                )
            track, position = await service.play(
                guild_id,
                selected.title,
                actor,
                commit_check=commit_check,
            )
            if await commit_check() is None:
                return ActionResult(
                    ActionStatus.DENIED,
                    "曲の追加後に権限または音楽機能の設定が変更されたため、追加結果を表示しませんでした。",
                )
        except asyncio.CancelledError:
            if connected_here and voice_client is not None:
                await _rollback_music_join(service, guild_id, voice_client)
            raise
        except MusicError as exc:
            if connected_here and voice_client is not None:
                await _rollback_music_join(service, guild_id, voice_client)
            return _music_error(exc)
        except Exception:
            if connected_here and voice_client is not None:
                await _rollback_music_join(service, guild_id, voice_client)
            return ActionResult(ActionStatus.FAILED, "VCへ安全に接続できなかったため、再生しませんでした。")
        if queued_without_voice:
            return ActionResult(
                ActionStatus.COMPLETED,
                f"許可済みローカル曲「{_safe_text(track.title, 200)}」を待機キュー "
                f"{position} 番へ追加しました。VCへ参加すると開始します。"
                "再起動後は /music join が必要です。",
            )
        return ActionResult(
            ActionStatus.COMPLETED,
            f"許可済みローカル曲「{_safe_text(track.title, 200)}」をキュー {position} 番へ追加しました。",
        )

    async def _music_local_search(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, _ = _music_context(context)
            if getattr(service, "available", False) is not True:
                return _unavailable("音楽モジュールは現在利用できません。")
            search_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music search",
                extra_capability_ids=(_MENTION_CAPABILITY_ID,),
            )
            actor = await search_check() if search_check is not None else None
            if actor is None:
                return ActionResult(
                    ActionStatus.DENIED, "検索前に権限を再確認できないため、ローカル曲を表示しませんでした。"
                )
            tracks = await service.search(parameters["query"], actor, guild_id=guild_id, limit=10)
            if (
                search_check is None
                or await search_check() is None
                or getattr(context.bot, "music_service", None) is not service
                or getattr(service, "available", False) is not True
            ):
                return ActionResult(
                    ActionStatus.DENIED,
                    "検索後に権限または音楽機能の設定が変更されたため、ローカル曲を表示しませんでした。",
                )
        except MusicError as exc:
            return _music_error(exc)
        if not tracks:
            return ActionResult(ActionStatus.COMPLETED, "一致するローカル曲はありません。")
        lines = [f"{index}. {_safe_text(track.title, 100)}" for index, track in enumerate(tracks[:10], 1)]
        return ActionResult(ActionStatus.COMPLETED, "ローカル曲の検索結果:\n" + "\n".join(lines))

    async def _music_speak(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, _ = _music_context(context)
            speech_queue = getattr(context.bot, "speech_queue", None)
            if (
                getattr(service, "available", False) is not True
                or getattr(speech_queue, "available", False) is not True
            ):
                return _unavailable("音楽または音声機能は現在利用できません。")
            commit_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music speak",
                extra_capability_ids=(_MENTION_CAPABILITY_ID,),
            )
            if commit_check is None:
                return ActionResult(ActionStatus.DENIED, "読み上げ前に権限を再確認できないため、追加しませんでした。")

            async def current_actor() -> MusicActor | None:
                actor = await commit_check()
                if (
                    actor is None
                    or getattr(context.bot, "music_service", None) is not service
                    or getattr(context.bot, "speech_queue", None) is not speech_queue
                    or getattr(service, "available", False) is not True
                    or getattr(speech_queue, "available", False) is not True
                ):
                    return None
                return actor

            async def current_policy() -> bool:
                return await current_actor() is not None

            actor = await current_actor()
            if actor is None:
                return ActionResult(ActionStatus.DENIED, "読み上げ前に権限を再確認できないため、追加しませんでした。")
            speech = await speech_queue.synthesize(
                SpeechRequest(
                    text=parameters["text"],
                    guild_id=guild_id,
                    channel_id=int(context.message.channel.id),
                    speaker_id=VOICEVOX_SPEAKER_ID,
                ),
                current_policy=current_policy,
            )
            actor = await current_actor()
            if actor is None:
                return ActionResult(
                    ActionStatus.DENIED, "合成後に権限または音声機能の設定が変更されたため、追加しませんでした。"
                )
            position = await service.add_speech_wav(guild_id, actor, speech.wav, commit_check=current_actor)
        except (MusicError, SpeechUnavailableError, ValueError):
            return ActionResult(ActionStatus.FAILED, "読み上げを安全に追加できませんでした。")
        return ActionResult(
            ActionStatus.COMPLETED, f"TTS queue {position}番へ追加しました。曲は停止せず自動duckingします。"
        )

    async def _music_speak_invalid(self, _: ActionContext, __: Mapping[str, str]) -> ActionResult:
        return ActionResult(ActionStatus.DENIED, "読み上げる本文は制御文字なしの1〜500文字で指定してください。")

    async def _music_rights_allow(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        return await self._music_rights_change(context, parameters["query"], allow=True)

    async def _music_rights_revoke(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        return await self._music_rights_change(context, parameters["query"], allow=False)

    async def _music_rights_change(
        self,
        context: ActionContext,
        query: str,
        *,
        allow: bool,
    ) -> ActionResult:
        try:
            service, guild_id, _ = _music_context(context)
            commit_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music play",
                extra_capability_ids=(_MENTION_CAPABILITY_ID,),
            )
            fresh_actor = await commit_check() if commit_check is not None else None
            if fresh_actor is None or not fresh_actor.manage_guild:
                return ActionResult(ActionStatus.DENIED, "この操作にはサーバー管理権限が必要です。")
            if allow:
                await service.grant_track_rights(guild_id, query, fresh_actor, commit_check=commit_check)
                return ActionResult(ActionStatus.COMPLETED, "このサーバーで曲の再生を許可しました。")
            removed = await service.revoke_track_rights(guild_id, query, fresh_actor, commit_check=commit_check)
            return ActionResult(
                ActionStatus.COMPLETED,
                "このサーバーで曲の許可を取り消しました。" if removed else "この曲に有効な許可はありません。",
            )
        except MusicError as exc:
            return _music_error(exc)

    async def _music_pause(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        return await self._music_control(context, "pause")

    async def _music_resume(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        return await self._music_control(context, "resume")

    async def _music_skip(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        return await self._music_control(context, "skip")

    async def _music_stop(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        return await self._music_control(context, "stop")

    async def _music_radio(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, _ = _music_context(context)
            commit_check = await _music_current_commit_check(context, service, "music radio")
            if commit_check is None:
                return ActionResult(
                    ActionStatus.DENIED,
                    "音楽機能の現在状態を再確認できないため、操作しませんでした。",
                )
            fresh_actor = await commit_check()
            if fresh_actor is None:
                return ActionResult(
                    ActionStatus.DENIED,
                    "音楽機能の現在状態を再確認できないため、操作しませんでした。",
                )
            enabled = parameters.get("mode") == "on"
            await service.set_local_radio(
                guild_id,
                fresh_actor,
                enabled,
                commit_check=commit_check,
            )
        except MusicError as exc:
            return _music_error(exc)
        return ActionResult(
            ActionStatus.COMPLETED,
            (
                "許可済みローカル曲だけを使うラジオを開始しました。手動の曲追加を優先します。"
                if enabled
                else "ローカルラジオの自動補充を停止しました。現在の曲はそのまま再生します。"
            ),
        )

    async def _music_radio_invalid(self, _: ActionContext, __: Mapping[str, str]) -> ActionResult:
        return ActionResult(
            ActionStatus.DENIED,
            "ローカルラジオは「ローカルラジオを開始して」または「ローカルラジオを停止して」で指定してください。",
        )

    async def _music_seek(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, _ = _music_context(context)
            commit_check = await _music_current_commit_check(context, service, "music seek")
            if commit_check is None:
                return ActionResult(ActionStatus.DENIED, "音楽機能の現在状態を再確認できないため、操作しませんでした。")
            fresh_actor = await commit_check()
            if fresh_actor is None:
                return ActionResult(ActionStatus.DENIED, "音楽機能の現在状態を再確認できないため、操作しませんでした。")
            seconds = int(parameters["seconds"])
            track = await service.seek(guild_id, fresh_actor, seconds, commit_check=commit_check)
        except (MusicError, KeyError, ValueError) as exc:
            if isinstance(exc, MusicError):
                return _music_error(exc)
            return ActionResult(ActionStatus.DENIED, "再生位置の秒数が不正です。")
        return ActionResult(
            ActionStatus.COMPLETED,
            f"「{_safe_text(track.title, 200)}」の再生位置を{seconds}秒へ移動しました。",
        )

    async def _music_volume(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        return await self._music_control(
            context,
            "volume",
            value=int(parameters["percent"]),
            volume_bus=parameters.get("bus", "music"),
        )

    async def _music_loop(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        return await self._music_control(context, "loop", value=LoopMode(parameters["mode"]))

    async def _music_shuffle(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        return await self._music_control(context, "shuffle")

    async def _music_remove(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, _ = _music_context(context)
            commit_check = await _music_current_commit_check(context, service, "music remove")
            if commit_check is None:
                return ActionResult(ActionStatus.DENIED, "音楽機能の現在状態を再確認できないため、操作しませんでした。")
            fresh_actor = await commit_check()
            if fresh_actor is None:
                return ActionResult(ActionStatus.DENIED, "音楽機能の現在状態を再確認できないため、操作しませんでした。")
            removed = await service.remove(
                guild_id, fresh_actor, int(parameters["position"]), commit_check=commit_check
            )
        except (MusicError, ValueError) as exc:
            return (
                _music_error(exc)
                if isinstance(exc, MusicError)
                else ActionResult(ActionStatus.DENIED, "キュー位置が不正です。")
            )
        return ActionResult(ActionStatus.COMPLETED, f"キューから「{_safe_text(removed.title, 200)}」を削除しました。")

    async def _music_control_invalid(self, _: ActionContext, __: Mapping[str, str]) -> ActionResult:
        return ActionResult(ActionStatus.DENIED, "音楽操作の入力が不正です。値またはキュー位置を確認してください。")

    async def _tools_dice(self, _: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            parsed = parse_dice(parameters["expression"])
        except (KeyError, ValueError):
            return ActionResult(ActionStatus.DENIED, "ダイスの形式が不正です。例: `2d6+1を振って`")
        rolls = [secrets.randbelow(parsed.sides) + 1 for _ in range(parsed.count)]
        suffix = f" {parsed.modifier:+d}" if parsed.modifier else ""
        return ActionResult(ActionStatus.COMPLETED, f"{rolls}{suffix} = **{sum(rolls) + parsed.modifier}**")

    async def _tools_random(self, _: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        minimum_text = parameters.get("minimum", "")
        maximum_text = parameters.get("maximum", "")
        if not all(re.fullmatch(r"[+-]?[0-9]+", value) for value in (minimum_text, maximum_text)):
            return ActionResult(ActionStatus.DENIED, "整数の範囲を指定してください。例: `1から100でランダムに選んで`")
        try:
            minimum = int(minimum_text)
            maximum = int(maximum_text)
        except (KeyError, ValueError):
            return ActionResult(ActionStatus.DENIED, "整数の範囲を指定してください。例: `1から100でランダムに選んで`")
        if not (-(2**63) <= minimum <= 2**63 - 1 and -(2**63) <= maximum <= 2**63 - 1):
            return ActionResult(ActionStatus.DENIED, "整数の範囲が不正です。")
        if minimum > maximum or maximum - minimum > 10_000_000:
            return ActionResult(ActionStatus.DENIED, "範囲が不正または広すぎます。")
        return ActionResult(ActionStatus.COMPLETED, str(minimum + secrets.randbelow(maximum - minimum + 1)))

    async def _tools_choose(self, _: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            choice = secrets.choice(parse_choices(parameters["choices"]))
        except (KeyError, ValueError):
            return ActionResult(ActionStatus.DENIED, "候補をASCIIカンマ区切りで2〜20個指定してください。")
        return ActionResult(ActionStatus.COMPLETED, f"選択結果: {_safe_text(choice, 100)}")

    async def _tools_timestamp(self, _: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            return ActionResult(ActionStatus.COMPLETED, discord_timestamp(parameters["datetime"], "F"))
        except (KeyError, ValueError):
            return ActionResult(
                ActionStatus.DENIED,
                "日時が不正です。例: `2026-07-25T20:00+09:00をDiscord時刻にして`",
            )

    async def _tools_snowflake(self, _: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            created_at = snowflake_created_at(int(parameters["discord_id"]))
        except (KeyError, ValueError):
            return await self._tools_snowflake_invalid(_, parameters)
        return ActionResult(
            ActionStatus.COMPLETED,
            f"作成日時 (UTC): {created_at.isoformat(timespec='milliseconds')}\n"
            f"{discord_timestamp(created_at.isoformat(), 'F')}",
        )

    async def _tools_snowflake_invalid(self, _: ActionContext, __: Mapping[str, str]) -> ActionResult:
        return ActionResult(
            ActionStatus.DENIED,
            "Discord IDは17〜20桁の正しいSnowflakeで指定してください。",
        )

    async def _tools_sha256(self, _: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            digest = sha256_text(parameters["text"])
        except (KeyError, ValueError):
            return await self._tools_sha256_invalid(_, parameters)
        return ActionResult(ActionStatus.COMPLETED, f"SHA-256: `{digest}`")

    async def _tools_sha256_invalid(self, _: ActionContext, __: Mapping[str, str]) -> ActionResult:
        return ActionResult(ActionStatus.DENIED, "SHA-256を計算する文字列を1〜512文字で指定してください。")

    async def _tools_color(self, _: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            color = color_from_hex(parameters["hex_color"])
        except (KeyError, ValueError):
            return await self._tools_color_invalid(_, parameters)
        return ActionResult(ActionStatus.COMPLETED, f"HEXカラー: `#{color:06X}`")

    async def _tools_color_invalid(self, _: ActionContext, __: Mapping[str, str]) -> ActionResult:
        return ActionResult(ActionStatus.DENIED, "HEXカラーは `#5865F2` の形式で指定してください。")

    async def _site_list(self, _: ActionContext, __: Mapping[str, str]) -> ActionResult:
        return ActionResult(
            ActionStatus.COMPLETED,
            "サイト一覧には非公開情報が含まれるため、ephemeral表示の `/site list` を使用してください。",
        )

    async def _site_show(self, _: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        site_id = parameters.get("site_id", "")
        if _SITE_ID_RE.fullmatch(site_id) is None:
            return ActionResult(ActionStatus.DENIED, "site_idの形式が不正です。")
        return ActionResult(
            ActionStatus.COMPLETED,
            "サイト詳細には非公開情報が含まれるため、ephemeral表示の `/site show` を使用してください。",
        )

    async def _site_status(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        if not await _site_status_current(context):
            return _site_unavailable_result()
        status = getattr(context.bot, "site_publish_status", None)
        if status is None:
            return _site_unavailable_result()
        if not await _site_status_current(context) or getattr(context.bot, "site_publish_status", None) is not status:
            return _site_unavailable_result()
        configured = getattr(status, "configured", None)
        ready = getattr(status, "ready", None)
        if not isinstance(configured, bool) or not isinstance(ready, bool):
            return _site_unavailable_result()
        return ActionResult(
            ActionStatus.COMPLETED,
            "サイト公開基盤のローカルreadiness:\n"
            f"- 構成: {'済み' if configured else '未構成'}\n"
            f"- 準備状態: {'ready' if ready else 'not ready'}\n"
            f"- 詳細: {_safe_text(getattr(status, 'detail', ''), 240)}\n"
            "この表示は外部公開やlive接続の成功を意味しません。",
        )

    async def _site_permission_grant(
        self,
        context: ActionContext,
        parameters: Mapping[str, str],
    ) -> ActionResult:
        return await self._set_site_delegated_permission(context, parameters, enabled=True)

    async def _site_permission_revoke(
        self,
        context: ActionContext,
        parameters: Mapping[str, str],
    ) -> ActionResult:
        return await self._set_site_delegated_permission(context, parameters, enabled=False)

    async def _set_site_delegated_permission(
        self,
        context: ActionContext,
        parameters: Mapping[str, str],
        *,
        enabled: bool,
    ) -> ActionResult:
        message = context.message
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        actor = getattr(message, "author", None)
        settings = getattr(context.bot, "settings", None)
        database = getattr(context.bot, "database", None)
        try:
            target_id = int(parameters["target_id"])
            actor_id = int(actor.id)
            guild_id = int(guild.id)
        except (AttributeError, KeyError, TypeError, ValueError):
            return ActionResult(ActionStatus.DENIED, "対象ユーザーとguildを確認できなかったため変更しませんでした。")
        if (
            target_id == actor_id
            or settings is None
            or actor_id not in getattr(settings, "bot_owner_ids", frozenset())
            or target_id in getattr(settings, "bot_owner_ids", frozenset())
            or database is None
            or not context.bindings_are_current()
        ):
            return ActionResult(ActionStatus.DENIED, "この委任変更はBOT所有者本人だけが実行できます。")

        fetch_member = getattr(guild, "fetch_member", None)
        permissions_for = getattr(channel, "permissions_for", None)
        guard = getattr(context.bot, "capability_guard", None)
        currently_allowed = getattr(guard, "currently_allowed", None)
        if not callable(fetch_member) or not callable(permissions_for) or not callable(currently_allowed):
            return ActionResult(ActionStatus.DENIED, "現在のDiscord権限を再確認できなかったため変更しませんでした。")
        try:
            fresh_actor = await fetch_member(actor_id)
            target = await fetch_member(target_id)
            actor_permissions = permissions_for(fresh_actor)
            same_actor = int(fresh_actor.id) == actor_id
            same_target = int(target.id) == target_id
            native_admin = (
                int(getattr(guild, "owner_id", 0) or 0) == actor_id
                or bool(getattr(getattr(fresh_actor, "guild_permissions", None), "administrator", False))
                or bool(getattr(getattr(fresh_actor, "guild_permissions", None), "manage_guild", False))
            )
            channel_current = bool(getattr(actor_permissions, "view_channel", False)) and bool(
                getattr(actor_permissions, "read_message_history", False)
            )
            control_current = (
                currently_allowed(
                    COMMAND_CAPABILITIES["system capability-set"],
                    guild_id=guild_id,
                    user_id=actor_id,
                    actor_level=RbacLevel.BOT_OWNER,
                    floor=RbacLevel.BOT_OWNER,
                )
                is True
            )
        except Exception:
            return ActionResult(ActionStatus.DENIED, "現在のDiscord権限を再確認できなかったため変更しませんでした。")
        mentioned_targets = tuple(
            member
            for member in getattr(message, "mentions", ())
            if getattr(member, "id", None) == target_id and getattr(member, "bot", False) is False
        )
        if (
            not same_actor
            or not same_target
            or bool(getattr(target, "bot", False))
            or len(mentioned_targets) != 1
            or not native_admin
            or not channel_current
            or not control_current
            or not context.bindings_are_current()
        ):
            return ActionResult(ActionStatus.DENIED, "対象または現在の権限を確認できなかったため変更しませんでした。")

        if enabled and (
            database.get_capability_override(SITE_AUTO_PUBLISH_CAPABILITY_ID, guild_id) is not True
            or database.get_level_override(SITE_AUTO_PUBLISH_CAPABILITY_ID, guild_id) != int(RbacLevel.BOT_OWNER)
        ):
            return ActionResult(
                ActionStatus.DENIED,
                "サイト公開のowner専用基準が有効ではないため、委任を追加しませんでした。",
            )
        try:
            database.set_capability_actor_grant(
                SITE_AUTO_PUBLISH_CAPABILITY_ID,
                target_id,
                enabled,
                guild_id,
                granted_by=actor_id,
                reason="Discord owner delegated site auto-publish",
            )
        except (RuntimeError, TypeError, ValueError):
            return ActionResult(ActionStatus.FAILED, "委任許可の保存に失敗したため変更しませんでした。")

        action = "追加" if enabled else "解除"
        return ActionResult(
            ActionStatus.COMPLETED,
            f"<@{target_id}> のサイト自動公開を委任許可リストで{action}しました。"
            "\nBOT所有者権限や他の管理権限は付与していません。",
        )

    async def _music_leave(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, actor = _music_context(context)
            commit_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music leave",
                extra_capability_ids=(_MENTION_CAPABILITY_ID,),
            )
            if commit_check is None:
                return ActionResult(ActionStatus.DENIED, "退出前に権限を再確認できないため、操作しませんでした。")
            await service.leave(guild_id, actor, commit_check=commit_check)
        except MusicError as exc:
            return _music_error(exc)
        return ActionResult(ActionStatus.COMPLETED, "VCから退出しました。")

    async def _music_playlist_list(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, actor = _music_context(context)
            commit_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music playlist list",
                extra_capability_ids=(_MENTION_CAPABILITY_ID,),
            )
            fresh_actor = await commit_check() if commit_check is not None else None
            if fresh_actor is None:
                return ActionResult(ActionStatus.DENIED, "一覧表示前に権限を再確認できないため、操作しませんでした。")
            records = await service.list_playlists(guild_id, fresh_actor)
        except MusicError as exc:
            return _music_error(exc)
        if not records:
            return ActionResult(ActionStatus.COMPLETED, "保存済みのプレイリストはありません。")
        lines = [f"{index}. {_safe_text(record.name, 64)}" for index, record in enumerate(records[:10], 1)]
        return ActionResult(ActionStatus.COMPLETED, "保存済みプレイリスト:\n" + "\n".join(lines))

    async def _music_playlist_save(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, actor = _music_context(context)
            commit_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music playlist save",
                extra_capability_ids=(_MENTION_CAPABILITY_ID,),
            )
            if commit_check is None:
                return ActionResult(ActionStatus.DENIED, "保存前に権限を再確認できないため、操作しませんでした。")
            await service.save_playlist(guild_id, actor, parameters["name"], commit_check=commit_check)
        except MusicError as exc:
            return _music_error(exc)
        return ActionResult(ActionStatus.COMPLETED, "現在のキューをプレイリストへ保存しました。")

    async def _music_playlist_load(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, actor = _music_context(context)
            commit_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music playlist load",
                extra_capability_ids=(_MENTION_CAPABILITY_ID,),
            )
            if commit_check is None:
                return ActionResult(ActionStatus.DENIED, "読み込み前に権限を再確認できないため、操作しませんでした。")
            loaded, missing = await service.load_playlist(
                guild_id, actor, parameters["name"], commit_check=commit_check
            )
        except MusicError as exc:
            return _music_error(exc)
        suffix = f"（見つからない曲: {missing}）" if missing else ""
        return ActionResult(ActionStatus.COMPLETED, f"プレイリストから{loaded}曲を追加しました。{suffix}")

    async def _music_playlist_delete(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        try:
            service, guild_id, actor = _music_context(context)
            commit_check = await build_music_commit_check(
                context.bot,
                context.message.guild,
                int(context.message.author.id),
                "music playlist delete",
                extra_capability_ids=(_MENTION_CAPABILITY_ID,),
            )
            if commit_check is None:
                return ActionResult(ActionStatus.DENIED, "削除前に権限を再確認できないため、操作しませんでした。")
            removed = await service.delete_playlist(guild_id, actor, parameters["name"], commit_check=commit_check)
        except MusicError as exc:
            return _music_error(exc)
        return ActionResult(
            ActionStatus.COMPLETED,
            "プレイリストを完全削除しました。" if removed else "指定したプレイリストはありません。",
        )

    async def _poll_results(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        repository = await _current_community_repository(context, "poll results")
        if repository is None:
            return _poll_results_denied()
        guild_id = int(context.message.guild.id)
        channel_id = int(context.message.channel.id)
        poll_id = parameters["poll_id"]
        try:
            poll = await asyncio.to_thread(repository.get_poll, guild_id, poll_id)
        except Exception:
            return ActionResult(ActionStatus.FAILED, "投票結果を安全に取得できませんでした。")
        if await _current_community_repository(context, "poll results") is not repository:
            return _poll_results_denied()
        if (
            poll is None
            or int(getattr(poll, "guild_id", 0)) != guild_id
            or int(getattr(poll, "channel_id", 0) or 0) != channel_id
        ):
            return _poll_results_denied()
        try:
            results = await asyncio.to_thread(repository.poll_results, guild_id, poll_id)
            current_poll = await asyncio.to_thread(repository.get_poll, guild_id, poll_id)
        except Exception:
            return ActionResult(ActionStatus.FAILED, "投票結果を安全に取得できませんでした。")
        if await _current_community_repository(context, "poll results") is not repository:
            return _poll_results_denied()
        if (
            current_poll is None
            or int(getattr(current_poll, "guild_id", 0)) != guild_id
            or int(getattr(current_poll, "channel_id", 0) or 0) != channel_id
        ):
            return _poll_results_denied()
        status = getattr(getattr(current_poll, "status", None), "value", getattr(current_poll, "status", None))
        options = getattr(current_poll, "options", ())
        question = getattr(current_poll, "question", None)
        if (
            status not in {"open", "closed"}
            or not isinstance(question, str)
            or not 1 <= len(question) <= 300
            or not isinstance(options, tuple)
            or not 2 <= len(options) <= 5
            or any(not isinstance(option, str) or not 1 <= len(option) <= 100 for option in options)
            or not isinstance(results, tuple)
            or len(results) != len(options)
        ):
            return ActionResult(ActionStatus.FAILED, "投票結果を安全に取得できませんでした。")
        votes: list[int] = []
        for index, result in enumerate(results):
            count = getattr(result, "votes", None)
            if (
                getattr(result, "option_index", None) != index
                or getattr(result, "option", None) != options[index]
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                return ActionResult(ActionStatus.FAILED, "投票結果を安全に取得できませんでした。")
            votes.append(count)
        label = "受付中" if status == "open" else "終了"
        lines = [
            f"投票結果（{label}）",
            f"質問: {_safe_text(question, 300)}",
            *(f"{index}. {_safe_text(option, 100)}: {votes[index - 1]}票" for index, option in enumerate(options, 1)),
        ]
        text = "\n".join(lines)
        if len(text) > 1_900:
            return ActionResult(ActionStatus.FAILED, "投票結果を安全に表示できませんでした。")
        return ActionResult(ActionStatus.COMPLETED, text)

    async def _poll_results_invalid(self, _: ActionContext, __: Mapping[str, str]) -> ActionResult:
        return ActionResult(ActionStatus.DENIED, "投票IDは32桁の小文字16進数で指定してください。")

    async def _feature_discovery(
        self,
        context: ActionContext,
        parameters: Mapping[str, str],
    ) -> ActionResult:
        bot = context.bot
        require_registry = getattr(bot, "require_registry", None)
        tree = getattr(bot, "tree", None)
        plugins = getattr(bot, "plugins", None)
        plugin_is_running = getattr(plugins, "is_running", None)
        if not callable(require_registry) or tree is None or not callable(plugin_is_running):
            return _unavailable("機能一覧を安全に確認できないため、表示しませんでした。")
        try:
            actor = await self._feature_discovery_actor(context)
            if actor is None:
                return ActionResult(ActionStatus.DENIED, "機能一覧を表示する現在の権限を確認できませんでした。")
            registry = require_registry()
            if not isinstance(registry, Registry):
                raise TypeError("registry is unavailable")
            live_paths = command_paths_from_tree(tree)
            runtime_ready_paths = frozenset(
                path
                for path in live_paths
                if (capability_id := COMMAND_CAPABILITIES.get(path)) is not None
                and registry.runtime_available(capability_id) is True
            )
            service = DiscoveryService(
                registry,
                command_capabilities=COMMAND_CAPABILITIES,
                command_plugins=COMMAND_PLUGIN_BY_ROOT,
                command_floors=COMMAND_RBAC_FLOORS,
                plugin_is_running=plugin_is_running,
            )
            page = service.search(
                actor,
                live_command_paths=runtime_ready_paths,
                query=parameters.get("query"),
            )
            final_actor = await self._feature_discovery_actor(context)
            if (
                final_actor is None
                or require_registry() is not registry
                or getattr(bot, "tree", None) is not tree
                or getattr(bot, "plugins", None) is not plugins
            ):
                return ActionResult(ActionStatus.DENIED, "機能一覧を表示する現在の権限を確認できませんでした。")
            live_paths = command_paths_from_tree(tree)
            runtime_ready_paths = frozenset(
                path
                for path in live_paths
                if (capability_id := COMMAND_CAPABILITIES.get(path)) is not None
                and registry.runtime_available(capability_id) is True
            )
            page = service.search(
                final_actor,
                live_command_paths=runtime_ready_paths,
                query=parameters.get("query"),
            )
        except Exception:
            return _unavailable("機能一覧を安全に確認できないため、表示しませんでした。")
        if not page.entries:
            return ActionResult(ActionStatus.COMPLETED, "現在の条件で一致する利用候補はありません。")
        footer = "※runtime readinessがtrueの候補だけです。対象・Bot権限は実行時に再確認されます。"
        continuation = "続きは検索語を絞るか、ephemeralな `/help` を利用してください。"
        entry_lines: list[str] = []
        for entry in page.entries:
            line = (
                f"`/{_safe_text(entry.path, 100)}`"
                f" ｜ {_safe_text(entry.module_id, 64)}"
                f" ｜ {_safe_text(entry.description, 120)}"
            )
            trial = "\n".join(
                (
                    f"現在の利用候補: {page.total_entries}件（先頭{len(entry_lines) + 1}件）",
                    *entry_lines,
                    line,
                    continuation,
                    footer,
                )
            )
            if len(trial) > _MAX_RESULT_CHARS:
                break
            entry_lines.append(line)
        if not entry_lines:
            return _unavailable("機能一覧を安全な長さで表示できませんでした。")
        lines = [f"現在の利用候補: {page.total_entries}件（先頭{len(entry_lines)}件）", *entry_lines]
        if page.total_pages > 1 or len(entry_lines) < len(page.entries):
            lines.append(continuation)
        lines.append(footer)
        return ActionResult(ActionStatus.COMPLETED, "\n".join(lines))

    async def _feature_discovery_actor(self, context: ActionContext) -> ActorContext | None:
        bot = context.bot
        guild = context.message.guild
        channel = context.message.channel
        user_id = getattr(context.message.author, "id", None)
        guild_id = getattr(guild, "id", None)
        guard = getattr(bot, "capability_guard", None)
        fetch_member = getattr(guild, "fetch_member", None)
        evaluate = getattr(guard, "evaluate_fresh_member", None)
        currently_allowed = getattr(guard, "currently_allowed", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if (
            self._closing_now
            or not isinstance(user_id, int)
            or user_id <= 0
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or not callable(fetch_member)
            or not callable(evaluate)
            or not callable(currently_allowed)
            or not callable(permissions_for)
        ):
            return None
        try:
            member = await fetch_member(user_id)
            if getattr(member, "id", None) != user_id:
                return None
            permissions = permissions_for(member)
            if (
                getattr(permissions, "view_channel", False) is not True
                or getattr(permissions, "read_message_history", False) is not True
            ):
                return None
            levels: list[RbacLevel] = []
            for capability_id, floor in (
                (COMMAND_CAPABILITIES["help"], COMMAND_RBAC_FLOORS.get("help", RbacLevel.EVERYONE)),
                (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
            ):
                decision = await evaluate(capability_id, guild=guild, member=member)
                level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
                if (
                    getattr(decision, "allowed", False) is not True
                    or level < floor
                    or currently_allowed(
                        capability_id,
                        guild_id=guild_id,
                        user_id=user_id,
                        actor_level=level,
                        floor=floor,
                    )
                    is not True
                ):
                    return None
                levels.append(level)
            return ActorContext(user_id, guild_id, min(levels))
        except Exception:
            return None

    async def _feature_search_invalid(self, _: ActionContext, __: Mapping[str, str]) -> ActionResult:
        return ActionResult(ActionStatus.DENIED, f"検索語は1〜{MAX_QUERY_LENGTH}文字で指定してください。")

    async def _mod_warnings(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        repository = await _current_modtools_repository(context, "mod warnings")
        if repository is None:
            return ActionResult(ActionStatus.DENIED, "警告履歴を安全に確認できないため、表示しませんでした。")
        target_id = int(parameters["target_id"])
        try:
            member = await context.message.guild.fetch_member(target_id)
            if int(member.id) != target_id:
                return ActionResult(ActionStatus.DENIED, "対象メンバーを確認できないため、表示しませんでした。")
        except Exception:
            return ActionResult(ActionStatus.DENIED, "対象メンバーを確認できないため、表示しませんでした。")
        if await _current_modtools_repository(context, "mod warnings") is not repository:
            return ActionResult(ActionStatus.DENIED, "警告履歴を安全に確認できないため、表示しませんでした。")
        try:
            warnings = await asyncio.to_thread(repository.warnings_for, int(context.message.guild.id), target_id)
        except Exception:
            return ActionResult(ActionStatus.FAILED, "警告履歴を安全に取得できませんでした。")
        if await _current_modtools_repository(context, "mod warnings") is not repository:
            return ActionResult(ActionStatus.DENIED, "警告履歴を安全に確認できないため、表示しませんでした。")
        if not warnings:
            return ActionResult(ActionStatus.COMPLETED, "有効な警告はありません。")
        lines = [
            f"Case #{int(getattr(item, 'case_id', 0))}: {_safe_text(getattr(item, 'reason', ''), 240)}"
            for item in warnings[:20]
        ]
        return ActionResult(ActionStatus.COMPLETED, "警告履歴:\n" + "\n".join(lines))

    async def _mod_case(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        repository = await _current_modtools_repository(context, "mod case")
        if repository is None:
            return ActionResult(ActionStatus.DENIED, "Caseを安全に確認できないため、表示しませんでした。")
        case_id = int(parameters["case_id"])
        try:
            item = await asyncio.to_thread(repository.get_case, int(context.message.guild.id), case_id)
        except Exception:
            return ActionResult(ActionStatus.FAILED, "Caseを安全に取得できませんでした。")
        if await _current_modtools_repository(context, "mod case") is not repository:
            return ActionResult(ActionStatus.DENIED, "Caseを安全に確認できないため、表示しませんでした。")
        if item is None or int(getattr(item, "guild_id", 0)) != int(context.message.guild.id):
            return ActionResult(ActionStatus.COMPLETED, "Caseが見つかりません。")
        action = _safe_text(getattr(getattr(item, "action", None), "value", ""), 40)
        status = _safe_text(getattr(item, "status", ""), 40)
        reason = _safe_text(getattr(item, "reason", ""), 240)
        return ActionResult(ActionStatus.COMPLETED, f"Case {case_id}: {action} / {status}\n理由: {reason}")

    async def _schedule_list(self, context: ActionContext, _: Mapping[str, str]) -> ActionResult:
        repository = await _current_scheduling_repository(context, "schedule list")
        if repository is None:
            return ActionResult(ActionStatus.DENIED, "予定を安全に確認できないため、表示しませんでした。")
        try:
            meetings = await asyncio.to_thread(
                repository.list_meetings, int(context.message.guild.id), datetime.now(UTC), 25
            )
        except Exception:
            return ActionResult(ActionStatus.FAILED, "予定を安全に取得できませんでした。")
        if await _current_scheduling_repository(context, "schedule list") is not repository:
            return ActionResult(ActionStatus.DENIED, "予定を安全に確認できないため、表示しませんでした。")
        if not meetings:
            return ActionResult(ActionStatus.COMPLETED, "今後の予定はありません。")
        lines = [
            f"`{_safe_text(getattr(meeting, 'id', ''), 20)}` **{_safe_text(getattr(meeting, 'title', ''), 40)}** "
            f"<t:{int(meeting.starts_at.timestamp())}:F>"
            for meeting in meetings[:25]
        ]
        return ActionResult(ActionStatus.COMPLETED, "今後の予定:\n" + "\n".join(lines))

    async def _schedule_show(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        repository = await _current_scheduling_repository(context, "schedule show")
        if repository is None:
            return ActionResult(ActionStatus.DENIED, "予定を安全に確認できないため、表示しませんでした。")
        try:
            meeting = await asyncio.to_thread(repository.get_meeting, parameters["meeting_id"])
        except Exception:
            return ActionResult(ActionStatus.FAILED, "予定を安全に取得できませんでした。")
        if await _current_scheduling_repository(context, "schedule show") is not repository:
            return ActionResult(ActionStatus.DENIED, "予定を安全に確認できないため、表示しませんでした。")
        if meeting is None or int(getattr(meeting, "guild_id", 0)) != int(context.message.guild.id):
            return ActionResult(ActionStatus.DENIED, "予定を安全に確認できないため、表示しませんでした。")
        return ActionResult(
            ActionStatus.COMPLETED,
            f"`{_safe_text(meeting.id, 20)}` **{_safe_text(meeting.title, 120)}**\n"
            f"開始: <t:{int(meeting.starts_at.timestamp())}:F>\n"
            f"終了: <t:{int(meeting.ends_at.timestamp())}:F>\n"
            f"Timezone: `{_safe_text(meeting.timezone, 64)}`",
        )

    async def _schedule_create(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        repository = await _current_scheduling_repository(context, "schedule create")
        if repository is None:
            return ActionResult(ActionStatus.DENIED, "予定を安全に作成できないため、登録しませんでした。")
        try:
            meeting = Meeting(
                id=f"MEET-{secrets.token_hex(4).upper()}",
                guild_id=int(context.message.guild.id),
                channel_id=int(context.message.channel.id),
                creator_id=int(context.message.author.id),
                title=parameters["title"],
                starts_at=parse_aware_datetime(parameters["starts_at"]),
                ends_at=parse_aware_datetime(parameters["ends_at"]),
                timezone=parameters["timezone"],
            )
        except (KeyError, TypeError, ValueError):
            return ActionResult(
                ActionStatus.DENIED,
                "日時またはtimezoneが不正です。日時には `+09:00` のような時差を含めてください。",
            )
        if await _current_scheduling_repository(context, "schedule create") is not repository:
            return ActionResult(ActionStatus.DENIED, "予定を安全に作成できないため、登録しませんでした。")
        try:
            created = await asyncio.to_thread(repository.save_meeting, meeting)
        except Exception:
            return ActionResult(ActionStatus.FAILED, "予定を安全に登録できませんでした。")
        if await _current_scheduling_repository(context, "schedule create") is not repository:
            return ActionResult(ActionStatus.DENIED, "予定を安全に確認できないため、登録結果を表示しませんでした。")
        if not created:
            return ActionResult(
                ActionStatus.FAILED, "予定IDが競合したため登録しませんでした。もう一度実行してください。"
            )
        return ActionResult(
            ActionStatus.COMPLETED,
            f"登録しました: `{meeting.id}` **{_safe_text(meeting.title, 80)}**\n開始: <t:{int(meeting.starts_at.timestamp())}:F>",
        )

    async def _schedule_rsvp(self, context: ActionContext, parameters: Mapping[str, str]) -> ActionResult:
        repository = await _current_scheduling_repository(context, "schedule rsvp")
        if repository is None:
            return ActionResult(ActionStatus.DENIED, "出欠を安全に確認できないため、保存しませんでした。")
        try:
            meeting = await asyncio.to_thread(repository.get_meeting, parameters["meeting_id"])
        except Exception:
            return ActionResult(ActionStatus.FAILED, "予定を安全に取得できませんでした。")
        if meeting is None or int(getattr(meeting, "guild_id", 0)) != int(context.message.guild.id):
            return ActionResult(ActionStatus.COMPLETED, "予定が見つかりません。")
        try:
            rsvp = RSVP(
                meeting_id=meeting.id,
                user_id=int(context.message.author.id),
                status=RSVPStatus(parameters["status"]),
                responded_at=datetime.now(UTC),
            )
        except (KeyError, TypeError, ValueError):
            return ActionResult(ActionStatus.DENIED, "出欠の入力が不正です。")
        if await _current_scheduling_repository(context, "schedule rsvp") is not repository:
            return ActionResult(ActionStatus.DENIED, "出欠を安全に確認できないため、保存しませんでした。")
        try:
            await asyncio.to_thread(repository.save_rsvp, rsvp)
        except Exception:
            return ActionResult(ActionStatus.FAILED, "出欠を安全に保存できませんでした。")
        if await _current_scheduling_repository(context, "schedule rsvp") is not repository:
            return ActionResult(ActionStatus.DENIED, "出欠を安全に確認できないため、保存結果を表示しませんでした。")
        labels = {RSVPStatus.ATTENDING: "参加", RSVPStatus.TENTATIVE: "未定", RSVPStatus.DECLINED: "欠席"}
        return ActionResult(ActionStatus.COMPLETED, f"{labels[rsvp.status]}で回答しました。")

    async def _music_control(
        self,
        context: ActionContext,
        action: str,
        *,
        value: int | LoopMode | None = None,
        volume_bus: str = "music",
    ) -> ActionResult:
        try:
            service, guild_id, _ = _music_context(context)
            commit_check = await _music_current_commit_check(context, service, f"music {action}")
            if commit_check is None:
                return ActionResult(
                    ActionStatus.DENIED,
                    "権限または音楽機能の現在状態を再確認できないため、操作しませんでした。",
                )
            fresh_actor = await commit_check()
            if fresh_actor is None:
                return ActionResult(
                    ActionStatus.DENIED,
                    "権限または音楽機能の現在状態を再確認できないため、操作しませんでした。",
                )
            if action == "pause":
                await service.pause(guild_id, fresh_actor, commit_check=commit_check)
                text = "音楽を一時停止しました。TTSキューは継続できます。"
            elif action == "resume":
                await service.resume(guild_id, fresh_actor, commit_check=commit_check)
                text = "音楽を再開しました。"
            elif action == "skip":
                track = await service.skip(guild_id, fresh_actor, commit_check=commit_check)
                text = f"「{_safe_text(track.title, 200)}」をスキップしました。"
            elif action == "stop":
                removed = await service.stop_music(guild_id, fresh_actor, commit_check=commit_check)
                text = f"音楽を停止し、待機中の{removed}曲を取り除きました。TTSキューは停止していません。"
            elif action == "volume":
                assert isinstance(value, int)
                if volume_bus == "speech":
                    await service.set_speech_volume(
                        guild_id,
                        fresh_actor,
                        value / 100.0,
                        commit_check=commit_check,
                    )
                    text = f"読み上げ音量を{value}%に設定しました。"
                else:
                    await service.set_volume(guild_id, fresh_actor, value / 100.0, commit_check=commit_check)
                    text = f"音量を{value}%に設定しました。"
            elif action == "loop":
                assert isinstance(value, LoopMode)
                await service.set_loop(guild_id, fresh_actor, value, commit_check=commit_check)
                labels = {LoopMode.OFF: "オフ", LoopMode.TRACK: "この曲", LoopMode.QUEUE: "キュー"}
                text = f"ループを{labels[value]}に設定しました。"
            elif action == "shuffle":
                shuffled = await service.shuffle(guild_id, fresh_actor, commit_check=commit_check)
                text = f"キューをシャッフルしました（{shuffled}曲）。"
            else:
                raise ValueError("unknown music action")
        except MusicError as exc:
            return _music_error(exc)
        return ActionResult(ActionStatus.COMPLETED, text)


def _explicit_action_text(
    message: discord.Message,
    bot: Any,
    *,
    allow_active_reply: bool = False,
) -> str | None:
    content = getattr(message, "content", None)
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    if not isinstance(content, str) or not isinstance(bot_id, int) or bot_id <= 0:
        return None
    matches = tuple(_BOT_MENTION.finditer(content))
    if matches:
        if not any(int(match.group("id")) == bot_id for match in matches):
            return None
        without_bot = _BOT_MENTION.sub(
            lambda match: " " if int(match.group("id")) == bot_id else match.group(0),
            content,
        )
        if (
            any(int(match.group("id")) != bot_id for match in matches)
            and _mod_warnings_parser(without_bot) is None
            and not _is_site_permission_action(without_bot)
        ):
            return None
    else:
        reference_id = getattr(getattr(message, "reference", None), "message_id", None)
        if (
            not allow_active_reply
            or isinstance(reference_id, bool)
            or not isinstance(reference_id, int)
            or reference_id <= 0
        ):
            return None
        # ``allow_active_reply`` is set only after AIMentionListener resolved the
        # referenced BOT response against guild/channel/user ownership.  Keep a
        # reference check here as defence in depth, and continue to reject every
        # unrelated Discord mention below.
        without_bot = content
    if (
        _ANY_DISCORD_MENTION.search(without_bot)
        and _mod_warnings_parser(without_bot) is None
        and not _is_site_permission_action(without_bot)
    ):
        return None
    if any(unicodedata.category(char).startswith("C") for char in without_bot):
        raw_normalized = unicodedata.normalize("NFKC", without_bot).lstrip()
        if raw_normalized.startswith("読み上げて:"):
            return without_bot
    normalized = " ".join(unicodedata.normalize("NFKC", without_bot).strip().split())
    if not normalized or any(unicodedata.category(char).startswith("C") for char in normalized):
        return None
    if len(normalized) > 1_100:
        # 明示された生成要求はprompt長超過でも通常AIへ落とさず、本文を保持しない
        # typed slash案内へ縮退する。その他の長大入力は従来どおり対象外。
        for prefix in (
            "画像を生成して",
            "画像を作って",
            "音楽を生成して",
            "音楽を作って",
            "曲を生成して",
            "曲を作って",
            "権利確認済みで音楽を生成して",
            "権利確認済みで音楽を作って",
            "権利確認済みで曲を生成して",
            "権利確認済みで曲を作って",
            "動画を生成して",
            "動画を作って",
        ):
            if normalized.startswith(prefix) and (
                len(normalized) == len(prefix) or normalized[len(prefix) : len(prefix) + 1] in {":", " "}
            ):
                return prefix
        return None
    return normalized.rstrip("。！？!? ")


def _fullmatch(pattern: str, *, names: tuple[str, ...] = ()) -> ActionParser:
    compiled = re.compile(rf"^(?:{pattern})$")

    def parse(text: str) -> Mapping[str, str] | None:
        match = compiled.fullmatch(text)
        if match is None:
            return None
        values: dict[str, str] = {}
        for name in names:
            value = " ".join((match.groupdict().get(name) or "").strip().split())
            if not value:
                return None
            values[name] = value
        return MappingProxyType(values)

    return parse


_earthquake_parser = _fullmatch(r"(?:最新の)?(?:地震|地震情報)(?:を)?(?:教えて|見せて|確認して)?")
_weather_parser = _fullmatch(
    rf"(?:(?:今日|本日|明日|あす|現在)(?:の)?)?"
    rf"(?P<region>.{{1,{_MAX_QUERY_CHARS}}}?)(?:の)?(?:天気|天気予報)"
    rf"(?:は|を(?:教えて|見せて|確認して)?|教えて|見せて|確認して)?",
    names=("region",),
)
_warning_parser = _fullmatch(
    rf"(?P<region>.{{1,{_MAX_QUERY_CHARS}}}?)(?:の)?(?:警報|注意報|警報注意報)(?:を)?(?:教えて|見せて|確認して)?",
    names=("region",),
)
_holiday_parser = _fullmatch(r"(?:次の|今度の)(?:祝日|休日)(?:を)?(?:教えて|見せて|確認して)?")
_memory_list_parser = _fullmatch(r"(?:私の|自分の)?(?:メモ|記憶)(?:一覧)?(?:を)?(?:見せて|教えて|確認して)")
_memory_status_parser = _fullmatch(r"(?:私のメモリ状態を教えて|メモリ状態)")
_memory_search_parser = _fullmatch(
    rf"(?:私の|自分の)?(?:メモ|記憶)(?:から|で)(?P<query>.{{1,{_MAX_QUERY_CHARS}}}?)(?:を)?(?:検索して|探して)",
    names=("query",),
)
_music_status_parser = _fullmatch(r"(?:音楽|ミュージック)(?:の)?(?:状態|ステータス)(?:を)?(?:教えて|見せて|確認して)?")
_music_queue_parser = _fullmatch(r"(?:音楽の)?(?:キュー|再生待ち)(?:を)?(?:教えて|見せて|確認して)")
_music_pause_parser = _fullmatch(r"(?:音楽を)?(?:一時停止して|ポーズして)")
_music_resume_parser = _fullmatch(r"(?:音楽を)?(?:再開して|再生再開して)")
_music_skip_parser = _fullmatch(r"(?:今の)?(?:曲を)?(?:スキップして|飛ばして)")
_music_stop_parser = _fullmatch(r"音楽(?:を)?(?:停止して|止めて)")


def _music_radio_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    modes = {
        "ローカルラジオを開始して": "on",
        "ローカルラジオを停止して": "off",
    }
    mode = modes.get(normalized)
    return MappingProxyType({"mode": mode}) if mode is not None else None


def _music_radio_invalid_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized.startswith("ローカルラジオ"):
        return None
    return MappingProxyType({}) if _music_radio_parser(text) is None else None


def _nasa_apod_parser(text: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    if normalized == "今日のNASAの写真" or normalized.casefold() == "nasa apod":
        return MappingProxyType({})
    match = re.fullmatch(r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})のAPOD", normalized, re.IGNORECASE)
    if match is None:
        return None
    return MappingProxyType({"date": match.group("date")})


def _image_generation_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    for prefix in ("画像を生成して", "画像を作って"):
        if normalized == prefix:
            return MappingProxyType({})
        if not normalized.startswith(prefix):
            continue
        remainder = normalized[len(prefix) :]
        if remainder[:1] not in {":", " ", "\t", "\r", "\n"}:
            continue
        prompt = remainder.lstrip(": \t\r\n").strip()
        return MappingProxyType({"prompt": prompt} if prompt else {})
    return None


def _image_editing_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    for prefix in ("この画像を編集して", "画像を編集して", "画像編集して", "画像を編集"):
        if normalized == prefix:
            return MappingProxyType({})
        if not normalized.startswith(prefix):
            continue
        remainder = normalized[len(prefix) :]
        if remainder[:1] not in {":", " ", "\t", "\r", "\n"}:
            continue
        instruction = remainder.lstrip(": \t\r\n").strip()
        return MappingProxyType({"instruction": instruction} if instruction else {})
    return None


def _video_generation_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    for prefix in ("動画を生成して", "動画を作って"):
        if normalized == prefix:
            return MappingProxyType({})
        if not normalized.startswith(prefix):
            continue
        remainder = normalized[len(prefix) :]
        if remainder[:1] not in {":", " ", "\t", "\r", "\n"}:
            continue
        prompt = remainder.lstrip(": \t\r\n").strip()
        return MappingProxyType({"prompt": prompt} if prompt else {})
    return None


def _music_generation_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    for prefix in ("音楽を生成して", "音楽を作って", "曲を生成して", "曲を作って"):
        if normalized == prefix:
            return MappingProxyType({})
        if normalized.startswith(prefix) and normalized[len(prefix) : len(prefix) + 1] in {":", " ", "\t", "\r", "\n"}:
            return MappingProxyType({})
    return None


def _confirmed_music_generation_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    for prefix in (
        "権利確認済みで音楽を生成して",
        "権利確認済みで音楽を作って",
        "権利確認済みで曲を生成して",
        "権利確認済みで曲を作って",
    ):
        if normalized == prefix:
            return MappingProxyType({})
        if not normalized.startswith(prefix):
            continue
        remainder = normalized[len(prefix) :]
        if remainder[:1] not in {":", " ", "\t", "\r", "\n"}:
            continue
        prompt = remainder.lstrip(": \t\r\n").strip()
        return MappingProxyType({"prompt": prompt} if prompt else {})
    return None


def _music_request_parser(text: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    if requests_multi_step_execution(normalized):
        return None
    match = re.fullmatch(
        rf"(?P<query>.{{0,{_MAX_QUERY_CHARS}}}?)(?:\s*を)?\s*(?:流して|ながして|再生して|かけて)",
        normalized,
    )
    if match is None:
        return None
    query = " ".join((match.group("query") or "").strip().split())
    return MappingProxyType({"query": query})


def _music_youtube_preview_parser(text: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    if requests_multi_step_execution(normalized):
        return None
    match = re.fullmatch(
        rf"(?P<query>.{{1,{_MAX_QUERY_CHARS}}}?)(?:の)?youtube(?:検索)?(?:の)?"
        r"(?:画面|検索画面)(?:を)?(?:スクショして|スクリーンショット(?:を)?撮って)",
        normalized,
        re.IGNORECASE,
    )
    if match is None:
        return None
    query = " ".join(match.group("query").strip().split())
    if not query or _ANY_DISCORD_MENTION.search(query):
        return None
    return MappingProxyType({"query": query})


def _browser_screenshot_parser(text: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    if requests_multi_step_execution(normalized):
        return None
    suffixes = (
        "の画面のスクリーンショットを撮って",
        "の画面をスクリーンショット撮って",
        "の画面をスクリーンショットして",
        "の画面をスクショして",
        "のスクリーンショットを撮って",
        "のスクショを撮って",
        "をスクリーンショットして",
        "をスクリーンショット撮って",
        "をスクショして",
    )
    for suffix in suffixes:
        if not normalized.endswith(suffix):
            continue
        url = normalized[: -len(suffix)].strip()
        if (
            8 <= len(url) <= 2_048
            and url.casefold().startswith(("https://", "http://"))
            and not any(unicodedata.category(character).startswith("C") for character in url)
        ):
            return MappingProxyType({"url": url})
    return None


def _browser_youtube_playback_evidence_parser(text: str) -> Mapping[str, str] | None:
    normalized_source = unicodedata.normalize("NFKC", text).strip()
    explicit = normalized_source.casefold().startswith("youtubeで")
    if any(unicodedata.category(character).startswith("C") for character in normalized_source):
        return MappingProxyType({"invalid": "1"}) if explicit else None
    normalized = " ".join(normalized_source.split())
    match = re.fullmatch(
        r"YouTubeで (?P<query>.{1,200}?) を検索して、"
        r"先頭候補を開いて再生し、途中と再生後をスクショして",
        normalized,
        re.IGNORECASE,
    )
    if match is not None:
        query = match.group("query").strip()
        if query and not _ANY_DISCORD_MENTION.search(query) and not contains_secret_like_text(query):
            return MappingProxyType({"query": query})
        return MappingProxyType({"invalid": "1"})
    if explicit and ("検索" in normalized or "スクショ" in normalized or "スクリーンショット" in normalized):
        return MappingProxyType({"invalid": "1"})
    return None


def _media_inspection_requires_external_ai_consent(adapter: object) -> bool:
    """Provider種別が不明な場合は、外部AI送信として安全側へ倒す。"""

    try:
        value = getattr(adapter, "requires_external_ai_consent")
    except Exception:
        return True
    return value if isinstance(value, bool) else True


def _media_url_inspection_parser(text: str) -> Mapping[str, str] | None:
    parsed = parse_media_inspection_request(text)
    if parsed is None:
        return None
    url, instruction = parsed
    return MappingProxyType({"url": url, "instruction": instruction})


def _discord_asset_inspection_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    emoji = re.fullmatch(
        r"この絵文字を調べて:\s*(?P<mention><a?:[A-Za-z0-9_]{2,32}:[0-9]{17,20}>)",
        normalized,
    )
    if emoji is not None:
        return MappingProxyType(
            {
                "kind": DiscordAssetKind.CUSTOM_EMOJI.value,
                "mention": emoji.group("mention"),
            }
        )
    if normalized == "このスタンプを調べて":
        return MappingProxyType({"kind": DiscordAssetKind.STICKER.value})
    return None


def _discord_asset_inspection_invalid_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if _discord_asset_inspection_parser(normalized) is not None:
        return None
    if normalized.startswith(
        (
            "この絵文字を調べて",
            "このスタンプを調べて",
            "この絵文字をプレビューして",
            "このスタンプをプレビューして",
        )
    ):
        return MappingProxyType({})
    return None


def _planner_only_parser(_: str) -> Mapping[str, str] | None:
    return None


def _music_local_search_parser(text: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    match = re.fullmatch(r"ローカル曲で (?P<query>.{1,200}?) を検索して", normalized)
    if match is None:
        return None
    query = match.group("query").strip()
    if not query or _ANY_DISCORD_MENTION.search(query):
        return None
    return MappingProxyType({"query": query})


def _music_speak_parser(text: str) -> Mapping[str, str] | None:
    if any(unicodedata.category(character).startswith("C") for character in text):
        return None
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(r"読み上げて:\s*(?P<text>.{1,500})", normalized)
    if match is None:
        return None
    value = match.group("text").strip()
    return MappingProxyType({"text": value}) if value else None


def _music_speak_invalid_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized.startswith("読み上げて:"):
        return None
    return MappingProxyType({}) if _music_speak_parser(text) is None else None


def _tools_dice_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(r"(?P<expression>.*)を振って", normalized)
    return MappingProxyType({"expression": match.group("expression").strip()}) if match is not None else None


def _tools_random_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(r"(?P<minimum>.*)から(?P<maximum>.*)でランダムに選んで", normalized)
    if match is None:
        return None
    return MappingProxyType({"minimum": match.group("minimum").strip(), "maximum": match.group("maximum").strip()})


def _tools_choose_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    prefix = "候補から選んで:"
    if not normalized.startswith(prefix):
        return None
    return MappingProxyType({"choices": normalized.removeprefix(prefix).strip()})


def _tools_timestamp_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(r"(?P<datetime>.*)をDiscord時刻にして", normalized)
    return MappingProxyType({"datetime": match.group("datetime").strip()}) if match is not None else None


def _tools_snowflake_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(
        r"(?:Discord ID (?P<created_id>[0-9]{17,20}) の作成日時を教えて|"
        r"Snowflake (?P<timestamp_id>[0-9]{17,20}) を時刻にして)",
        normalized,
    )
    if match is None:
        return None
    return MappingProxyType({"discord_id": match.group("created_id") or match.group("timestamp_id")})


def _tools_snowflake_invalid_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(
        r"(?:Discord ID|Snowflake)\s+\S{1,64}\s+(?:の作成日時を教えて|を時刻にして)",
        normalized,
    )
    return MappingProxyType({}) if match is not None and _tools_snowflake_parser(text) is None else None


def _tools_sha256_parser(text: str) -> Mapping[str, str] | None:
    prefix = "SHA-256を計算:"
    if not text.startswith(prefix):
        return None
    value = text.removeprefix(prefix).strip()
    return MappingProxyType({"text": value}) if 1 <= len(value) <= 512 else None


def _tools_sha256_invalid_parser(text: str) -> Mapping[str, str] | None:
    prefix = "SHA-256を計算:"
    if not text.startswith(prefix):
        return None
    value = text.removeprefix(prefix).strip()
    return MappingProxyType({}) if len(value) <= 512 and _tools_sha256_parser(text) is None else None


def _tools_color_parser(text: str) -> Mapping[str, str] | None:
    match = re.fullmatch(r"カラー (?P<hex_color>#[0-9A-Fa-f]{6})を確認して", text)
    return MappingProxyType({"hex_color": match.group("hex_color")}) if match is not None else None


def _tools_color_invalid_parser(text: str) -> Mapping[str, str] | None:
    prefix = "カラー "
    suffix = "を確認して"
    if not text.startswith(prefix) or not text.endswith(suffix):
        return None
    value = text.removeprefix(prefix).removesuffix(suffix).strip()
    return MappingProxyType({}) if 1 <= len(value) <= 64 and _tools_color_parser(text) is None else None


def _poll_results_parser(text: str) -> Mapping[str, str] | None:
    match = re.fullmatch(r"投票 (?P<poll_id>[0-9a-f]{32}) の結果を見せて", text)
    return MappingProxyType({"poll_id": match.group("poll_id")}) if match is not None else None


def _poll_results_invalid_parser(text: str) -> Mapping[str, str] | None:
    return (
        MappingProxyType({})
        if text.startswith("投票 ") and text.endswith(" の結果を見せて") and _poll_results_parser(text) is None
        else None
    )


def _feature_list_parser(text: str) -> Mapping[str, str] | None:
    return MappingProxyType({}) if text == "使える機能を見せて" else None


def _feature_search_parser(text: str) -> Mapping[str, str] | None:
    match = re.fullmatch(r"機能を (?P<query>.+) で検索して", text)
    if match is None:
        return None
    query = match.group("query").strip()
    if (
        not 1 <= len(query) <= MAX_QUERY_LENGTH
        or _ANY_DISCORD_MENTION.search(query)
        or any(unicodedata.category(character).startswith("C") for character in query)
    ):
        return None
    return MappingProxyType({"query": query})


def _feature_search_invalid_parser(text: str) -> Mapping[str, str] | None:
    return (
        MappingProxyType({})
        if text.startswith("機能を ") and text.endswith(" で検索して") and _feature_search_parser(text) is None
        else None
    )


def _site_list_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    return MappingProxyType({}) if normalized in {"サイト一覧を見せて", "私のサイト一覧を見せて"} else None


def _site_show_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    prefix = "サイト詳細:"
    if not normalized.startswith(prefix):
        return None
    return MappingProxyType({"site_id": normalized.removeprefix(prefix).strip().lower()})


def _site_status_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    return MappingProxyType({}) if normalized in {"サイト公開基盤の状態を教えて", "サイト状態を見せて"} else None


def _site_permission_grant_parser(text: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    match = re.fullmatch(
        r"<@!?(?P<target_id>[1-9][0-9]{0,18})>\s*(?:に|にも|の)?\s*"
        r"(?:Web)?サイト(?:の)?公開(?:権限)?(?:を)?(?:許可|ONに)(?:して)?",
        normalized,
        re.IGNORECASE,
    )
    return MappingProxyType({"target_id": match.group("target_id")}) if match is not None else None


def _site_permission_revoke_parser(text: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    match = re.fullmatch(
        r"<@!?(?P<target_id>[1-9][0-9]{0,18})>\s*(?:に|の)?\s*"
        r"(?:Web)?サイト(?:の)?公開(?:権限|許可)?(?:を)?(?:解除|取り消し|OFFに)(?:して)?",
        normalized,
        re.IGNORECASE,
    )
    return MappingProxyType({"target_id": match.group("target_id")}) if match is not None else None


def _is_site_permission_action(text: str) -> bool:
    return _site_permission_grant_parser(text) is not None or _site_permission_revoke_parser(text) is not None


def _music_volume_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(r"(?P<speech>読み上げ)?音量を(?P<percent>[0-9]{1,3})%にして", normalized)
    if match is None:
        return None
    percent = int(match.group("percent"))
    if not 0 <= percent <= 200:
        return None
    return MappingProxyType(
        {
            "percent": str(percent),
            "bus": "speech" if match.group("speech") is not None else "music",
        }
    )


def _music_seek_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(r"再生位置を(?P<seconds>[0-9]{1,5})秒にして", normalized)
    if match is None:
        return None
    seconds = int(match.group("seconds"))
    return MappingProxyType({"seconds": str(seconds)}) if 0 <= seconds <= 86_400 else None


def _music_seek_invalid_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not (normalized.startswith("再生位置を") and normalized.endswith("秒にして")):
        return None
    return MappingProxyType({}) if _music_seek_parser(text) is None else None


def _music_volume_invalid_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not (
        (normalized.startswith("音量を") or normalized.startswith("読み上げ音量を")) and normalized.endswith("%にして")
    ):
        return None
    return MappingProxyType({}) if _music_volume_parser(text) is None else None


def _music_loop_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    modes = {
        "ループをオフにして": LoopMode.OFF.value,
        "ループをこの曲にして": LoopMode.TRACK.value,
        "ループをキューにして": LoopMode.QUEUE.value,
    }
    mode = modes.get(normalized)
    return MappingProxyType({"mode": mode}) if mode is not None else None


def _music_loop_invalid_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not (normalized.startswith("ループを") and normalized.endswith("にして")):
        return None
    return MappingProxyType({}) if _music_loop_parser(text) is None else None


def _music_shuffle_parser(text: str) -> Mapping[str, str] | None:
    return MappingProxyType({}) if unicodedata.normalize("NFKC", text).strip() == "キューをシャッフルして" else None


def _music_shuffle_invalid_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized.startswith("キューをシャッフル"):
        return None
    return MappingProxyType({}) if _music_shuffle_parser(text) is None else None


def _music_remove_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(r"キューの(?P<position>[0-9]{1,4})番を削除して", normalized)
    if match is None:
        return None
    position = int(match.group("position"))
    return MappingProxyType({"position": str(position)}) if 1 <= position <= 1_000 else None


def _music_remove_invalid_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not (normalized.startswith("キューの") and normalized.endswith("番を削除して")):
        return None
    return MappingProxyType({}) if _music_remove_parser(text) is None else None


def _mod_warnings_parser(text: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    match = re.fullmatch(r"<@!?(?P<target_id>[1-9]\d{0,18})> の警告履歴", normalized)
    return MappingProxyType({"target_id": match.group("target_id")}) if match is not None else None


def _mod_case_parser(text: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    match = re.fullmatch(r"Case (?P<case_id>[1-9]\d{0,18})を表示", normalized)
    return MappingProxyType({"case_id": match.group("case_id")}) if match is not None else None


def _schedule_list_parser(text: str) -> Mapping[str, str] | None:
    return MappingProxyType({}) if unicodedata.normalize("NFKC", text).strip() == "予定一覧" else None


def _schedule_show_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    match = re.fullmatch(r"(?P<meeting>MEET-[A-F0-9]{8}) の予定を見せて", normalized)
    if match is None:
        match = re.fullmatch(r"予定 (?P<meeting>MEET-[A-F0-9]{8}) の詳細を見せて", normalized)
    if match is None:
        match = re.fullmatch(r"予定詳細: (?P<meeting>MEET-[A-F0-9]{8})", normalized)
    return MappingProxyType({"meeting_id": match.group("meeting")}) if match is not None else None


def _schedule_create_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    prefix = "予定を作成:"
    if not normalized.startswith(prefix):
        return None
    fields = tuple(field.strip() for field in normalized.removeprefix(prefix).split("|"))
    if len(fields) not in {3, 4}:
        return None
    title, starts_at, ends_at = fields[:3]
    timezone_key = fields[3] if len(fields) == 4 else "Asia/Tokyo"
    if not 1 <= len(title) <= 120 or not starts_at or not ends_at or not timezone_key:
        return None
    return MappingProxyType({"title": title, "starts_at": starts_at, "ends_at": ends_at, "timezone": timezone_key})


def _schedule_rsvp_parser(text: str) -> Mapping[str, str] | None:
    normalized = unicodedata.normalize("NFKC", text).strip()
    prefix = "出欠:"
    if not normalized.startswith(prefix):
        return None
    fields = tuple(field.strip() for field in normalized.removeprefix(prefix).split("|"))
    if len(fields) != 2:
        return None
    meeting_id, status = fields
    if re.fullmatch(r"MEET-[A-F0-9]{8}", meeting_id) is None or status not in {item.value for item in RSVPStatus}:
        return None
    return MappingProxyType({"meeting_id": meeting_id, "status": status})


def _music_leave_parser(text: str) -> Mapping[str, str] | None:
    return MappingProxyType({}) if unicodedata.normalize("NFKC", text).strip() == "VCから退出して" else None


def _music_playlist_list_parser(text: str) -> Mapping[str, str] | None:
    return MappingProxyType({}) if unicodedata.normalize("NFKC", text).strip() == "プレイリスト一覧" else None


def _music_playlist_save_parser(text: str) -> Mapping[str, str] | None:
    return _music_playlist_name_parser(text, "プレイリストを保存")


def _music_playlist_load_parser(text: str) -> Mapping[str, str] | None:
    return _music_playlist_name_parser(text, "プレイリストを読み込んで")


def _music_playlist_delete_parser(text: str) -> Mapping[str, str] | None:
    return _music_playlist_name_parser(text, "プレイリストを完全削除")


def _music_playlist_name_parser(text: str, prefix: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    expected = f"{prefix}:"
    if not normalized.startswith(expected):
        return None
    name = normalized.removeprefix(expected).strip()
    if not 1 <= len(name) <= 64:
        return None
    return MappingProxyType({"name": name})


def _music_rights_allow_parser(text: str) -> Mapping[str, str] | None:
    return _music_rights_parser(text, "権利確認済みで曲を許可")


def _music_rights_revoke_parser(text: str) -> Mapping[str, str] | None:
    return _music_rights_parser(text, "曲の許可を取り消して")


def _music_rights_parser(text: str, prefix: str) -> Mapping[str, str] | None:
    normalized = " ".join(unicodedata.normalize("NFKC", text).strip().split())
    expected = f"{prefix}:"
    if not normalized.startswith(expected):
        return None
    query = normalized.removeprefix(expected).strip()
    if not 1 <= len(query) <= _MAX_QUERY_CHARS:
        return None
    return MappingProxyType({"query": query})


def _memory_remember_parser(text: str) -> Mapping[str, str] | None:
    match = re.fullmatch(
        rf"(?:(?P<before>.{{1,{_MAX_MEMORY_CHARS}}}?)を覚えて(?:おいて)?|覚えて[：:]\s*(?P<after>.{{1,{_MAX_MEMORY_CHARS}}}))",
        text,
    )
    if match is None:
        return None
    value = " ".join((match.group("before") or match.group("after") or "").strip().split())
    return MappingProxyType({"text": value}) if value else None


def _high_impact_parser(pattern: str) -> ActionParser:
    return _fullmatch(pattern)


async def _never_execute(_: ActionContext, __: Mapping[str, str]) -> ActionResult:
    raise RuntimeError("deferred actions must never execute")


def _required_floor(path: str) -> RbacLevel:
    configured = COMMAND_RBAC_FLOORS.get(path, RbacLevel.EVERYONE)
    defaults = {
        "mod ban": RbacLevel.GUILD_ADMIN,
        "mod kick": RbacLevel.GUILD_ADMIN,
        "ticket close": RbacLevel.EVERYONE,
        "server role-add": RbacLevel.GUILD_ADMIN,
        "system module-set": RbacLevel.GUILD_ADMIN,
        "evolution status": RbacLevel.BOT_OWNER,
        "image generate": RbacLevel.TRUSTED,
        "musicgen generate": RbacLevel.TRUSTED,
        "video generate": RbacLevel.TRUSTED,
    }
    return max(configured, defaults[path])


def _music_context(context: ActionContext) -> tuple[Any, int, MusicActor]:
    service = getattr(context.bot, "music_service", None)
    if service is None:
        raise MusicUnavailableError("music plugin is unavailable")
    guild = context.message.guild
    author = context.message.author
    channel = _voice_channel(author)
    permissions = getattr(author, "guild_permissions", None)
    manage = bool(
        permissions is not None
        and (bool(getattr(permissions, "administrator", False)) or bool(getattr(permissions, "manage_guild", False)))
    )
    actor = MusicActor(
        int(author.id),
        int(channel.id) if channel is not None else None,
        manage,
    )
    return service, int(guild.id), actor


async def _music_current_commit_check(context: ActionContext, service: Any, command_path: str) -> Any | None:
    check = await build_music_commit_check(
        context.bot,
        context.message.guild,
        int(context.message.author.id),
        command_path,
        extra_capability_ids=(_MENTION_CAPABILITY_ID,),
    )
    if check is None:
        return None

    async def current() -> MusicActor | None:
        actor = await check()
        if actor is None or getattr(context.bot, "music_service", None) is not service:
            return None
        return actor if getattr(service, "available", False) is True else None

    return current


async def _current_community_repository(context: ActionContext, command_path: str) -> Any | None:
    bot = context.bot
    plugin = getattr(bot, "community_plugin", None)
    repository = getattr(bot, "community_repository", None)
    guild = context.message.guild
    channel = context.message.channel
    user_id = getattr(context.message.author, "id", None)
    guard = getattr(bot, "capability_guard", None)
    fetch_member = getattr(guild, "fetch_member", None)
    evaluate = getattr(guard, "evaluate_fresh_member", None)
    currently_allowed = getattr(guard, "currently_allowed", None)
    permissions_for = getattr(channel, "permissions_for", None)
    capability_id = COMMAND_CAPABILITIES.get(command_path)
    if (
        bool(getattr(bot, "is_closing", False))
        or plugin is None
        or bool(getattr(plugin, "closing", True))
        or getattr(plugin, "bot", None) is not bot
        or getattr(plugin, "repository", None) is not repository
        or repository is None
        or not isinstance(getattr(guild, "id", None), int)
        or not isinstance(getattr(channel, "id", None), int)
        or not isinstance(user_id, int)
        or user_id <= 0
        or not callable(fetch_member)
        or not callable(evaluate)
        or not callable(currently_allowed)
        or not callable(permissions_for)
        or capability_id is None
    ):
        return None
    try:
        member = await fetch_member(user_id)
        if int(member.id) != user_id:
            return None
        permissions = permissions_for(member)
        if (
            getattr(permissions, "view_channel", False) is not True
            or getattr(permissions, "read_message_history", False) is not True
        ):
            return None
        for identifier, floor in (
            (capability_id, COMMAND_RBAC_FLOORS.get(command_path, RbacLevel.EVERYONE)),
            (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
        ):
            decision = await evaluate(identifier, guild=guild, member=member)
            level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
            if (
                getattr(decision, "allowed", False) is not True
                or level < floor
                or currently_allowed(
                    identifier,
                    guild_id=int(guild.id),
                    user_id=user_id,
                    actor_level=level,
                    floor=floor,
                )
                is not True
            ):
                return None
    except Exception:
        return None
    return repository if getattr(bot, "community_repository", None) is repository else None


def _poll_results_denied() -> ActionResult:
    return ActionResult(ActionStatus.DENIED, "投票結果を安全に確認できないため、表示しませんでした。")


async def _current_modtools_repository(context: ActionContext, command_path: str) -> Any | None:
    bot = context.bot
    plugin = getattr(bot, "modtools_plugin", None)
    repository = getattr(bot, "modtools_repository", None)
    guild = context.message.guild
    user_id = getattr(context.message.author, "id", None)
    guard = getattr(bot, "capability_guard", None)
    fetch_member = getattr(guild, "fetch_member", None)
    evaluate = getattr(guard, "evaluate_fresh_member", None)
    currently_allowed = getattr(guard, "currently_allowed", None)
    capability_id = COMMAND_CAPABILITIES.get(command_path)
    if (
        bool(getattr(bot, "is_closing", False))
        or plugin is None
        or bool(getattr(plugin, "closing", True))
        or getattr(plugin, "bot", None) is not bot
        or getattr(plugin, "repository", None) is not repository
        or repository is None
        or not isinstance(user_id, int)
        or user_id <= 0
        or not callable(fetch_member)
        or not callable(evaluate)
        or not callable(currently_allowed)
        or capability_id is None
    ):
        return None
    try:
        member = await fetch_member(user_id)
        if int(member.id) != user_id:
            return None
        for identifier, floor in (
            (capability_id, COMMAND_RBAC_FLOORS.get(command_path, RbacLevel.EVERYONE)),
            (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
        ):
            decision = await evaluate(identifier, guild=guild, member=member)
            level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
            if (
                getattr(decision, "allowed", False) is not True
                or level < floor
                or currently_allowed(
                    identifier,
                    guild_id=int(guild.id),
                    user_id=user_id,
                    actor_level=level,
                    floor=floor,
                )
                is not True
            ):
                return None
    except Exception:
        return None
    return repository if getattr(bot, "modtools_repository", None) is repository else None


async def _current_scheduling_repository(context: ActionContext, command_path: str) -> Any | None:
    bot = context.bot
    plugin = getattr(bot, "scheduling_plugin", None)
    repository = getattr(bot, "scheduling_repository", None)
    guild = context.message.guild
    user_id = getattr(context.message.author, "id", None)
    guard = getattr(bot, "capability_guard", None)
    fetch_member = getattr(guild, "fetch_member", None)
    evaluate = getattr(guard, "evaluate_fresh_member", None)
    currently_allowed = getattr(guard, "currently_allowed", None)
    capability_id = COMMAND_CAPABILITIES.get(command_path)
    if (
        bool(getattr(bot, "is_closing", False))
        or plugin is None
        or bool(getattr(plugin, "closing", True))
        or getattr(plugin, "bot", None) is not bot
        or getattr(plugin, "repository", None) is not repository
        or repository is None
        or not isinstance(user_id, int)
        or user_id <= 0
        or not callable(fetch_member)
        or not callable(evaluate)
        or not callable(currently_allowed)
        or capability_id is None
    ):
        return None
    try:
        member = await fetch_member(user_id)
        if int(member.id) != user_id:
            return None
        for identifier, floor in (
            (capability_id, COMMAND_RBAC_FLOORS.get(command_path, RbacLevel.EVERYONE)),
            (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
        ):
            decision = await evaluate(identifier, guild=guild, member=member)
            level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
            if (
                getattr(decision, "allowed", False) is not True
                or level < floor
                or currently_allowed(
                    identifier,
                    guild_id=int(guild.id),
                    user_id=user_id,
                    actor_level=level,
                    floor=floor,
                )
                is not True
            ):
                return None
    except Exception:
        return None
    return repository if getattr(bot, "scheduling_repository", None) is repository else None


async def _current_personal_memory_service(context: ActionContext) -> Any | None:
    bot = context.bot
    service = getattr(bot, "personal_memory_service", None)
    guild = context.message.guild
    user_id = getattr(context.message.author, "id", None)
    guard = getattr(bot, "capability_guard", None)
    fetch_member = getattr(guild, "fetch_member", None)
    evaluate = getattr(guard, "evaluate_fresh_member", None)
    currently_allowed = getattr(guard, "currently_allowed", None)
    capability_id = COMMAND_CAPABILITIES.get("memory status")
    if (
        bool(getattr(bot, "is_closing", False))
        or service is None
        or not isinstance(user_id, int)
        or user_id <= 0
        or not callable(fetch_member)
        or not callable(evaluate)
        or not callable(currently_allowed)
        or capability_id is None
    ):
        return None
    try:
        member = await fetch_member(user_id)
        if int(member.id) != user_id:
            return None
        for identifier, floor in (
            (capability_id, COMMAND_RBAC_FLOORS.get("memory status", RbacLevel.EVERYONE)),
            (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
        ):
            decision = await evaluate(identifier, guild=guild, member=member)
            level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
            if (
                getattr(decision, "allowed", False) is not True
                or level < floor
                or currently_allowed(
                    identifier,
                    guild_id=int(guild.id),
                    user_id=user_id,
                    actor_level=level,
                    floor=floor,
                )
                is not True
            ):
                return None
    except Exception:
        return None
    return service if getattr(bot, "personal_memory_service", None) is service else None


async def _site_status_current(context: ActionContext) -> bool:
    bot = context.bot
    guild = context.message.guild
    user_id = getattr(context.message.author, "id", None)
    guard = getattr(bot, "capability_guard", None)
    fetch_member = getattr(guild, "fetch_member", None)
    evaluate = getattr(guard, "evaluate_fresh_member", None)
    currently_allowed = getattr(guard, "currently_allowed", None)
    capability_id = COMMAND_CAPABILITIES.get("site status")
    if (
        bool(getattr(bot, "is_closing", False))
        or not isinstance(user_id, int)
        or user_id <= 0
        or not callable(fetch_member)
        or not callable(evaluate)
        or not callable(currently_allowed)
        or capability_id is None
    ):
        return False
    try:
        member = await fetch_member(user_id)
        if int(member.id) != user_id:
            return False
        for identifier, floor in (
            (capability_id, COMMAND_RBAC_FLOORS.get("site status", RbacLevel.EVERYONE)),
            (_MENTION_CAPABILITY_ID, RbacLevel.EVERYONE),
        ):
            decision = await evaluate(identifier, guild=guild, member=member)
            level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
            if (
                getattr(decision, "allowed", False) is not True
                or level < floor
                or currently_allowed(
                    identifier,
                    guild_id=int(guild.id),
                    user_id=user_id,
                    actor_level=level,
                    floor=floor,
                )
                is not True
            ):
                return False
    except Exception:
        return False
    return True


def _voice_channel(author: Any) -> Any | None:
    channel = getattr(getattr(author, "voice", None), "channel", None)
    return channel if isinstance(getattr(channel, "id", None), int) and channel.id > 0 else None


def _discord_sticker_format(value: object) -> StickerFormat:
    name = str(getattr(value, "name", "")).casefold()
    if name in {"png", "apng", "gif", "lottie"}:
        return StickerFormat(name)
    raw = getattr(value, "value", value)
    mapping = {
        1: StickerFormat.PNG,
        2: StickerFormat.APNG,
        3: StickerFormat.LOTTIE,
        4: StickerFormat.GIF,
    }
    if isinstance(raw, bool) or not isinstance(raw, int) or raw not in mapping:
        raise DiscordAssetInspectionError("sticker format is invalid")
    return mapping[raw]


def _unique_music_match(query: str, candidates: Sequence[Any]) -> Any | None:
    if not candidates:
        return None
    normalized = query.casefold().strip()
    exact = [track for track in candidates if str(getattr(track, "title", "")).casefold() == normalized]
    if len(exact) == 1:
        return exact[0]
    return candidates[0] if len(candidates) == 1 else None


def _music_error(error: MusicError) -> ActionResult:
    if isinstance(error, MusicUnavailableError):
        return _unavailable("音楽モジュールは未設定または停止中です。`/music status` を確認してください。")
    if isinstance(error, MusicAuthorizationError):
        return ActionResult(ActionStatus.DENIED, "同じVCの曲依頼者か、サーバー管理権限を持つ利用者だけが操作できます。")
    if isinstance(error, MusicSeekUnsupportedError):
        return ActionResult(ActionStatus.UNAVAILABLE, "この音源は再生位置の変更に対応していません。")
    if isinstance(error, MusicSessionError):
        return ActionResult(ActionStatus.FAILED, "現在のVC接続または再生キューではその操作を実行できません。")
    return ActionResult(ActionStatus.FAILED, "音楽操作に失敗しました。")


async def _rollback_music_join(service: Any, guild_id: int, voice_client: Any) -> None:
    close_guild = getattr(service, "close_guild", None)
    if callable(close_guild):
        with suppress(Exception):
            await close_guild(guild_id)
    with suppress(Exception):
        await voice_client.disconnect(force=True)


def _memory_items_result(items: Sequence[Any]) -> ActionResult:
    if not items:
        return ActionResult(ActionStatus.COMPLETED, "本人専用メモは見つかりませんでした。")
    lines = [f"`{int(item.id)}` {_safe_text(item.content, 280)}" for item in items[:10]]
    return ActionResult(ActionStatus.COMPLETED, "本人専用メモ:\n" + "\n".join(lines))


def _unavailable(text: str) -> ActionResult:
    return ActionResult(ActionStatus.UNAVAILABLE, text)


def _closing_result() -> ActionResult:
    return ActionResult(ActionStatus.DENIED, "Botは再起動または停止処理中です。操作は実行しませんでした。")


def _site_unavailable_result() -> ActionResult:
    return ActionResult(ActionStatus.UNAVAILABLE, "サイト情報を安全に確認できないため、表示しませんでした。")


def _safe_text(value: object, maximum: int) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")[:maximum]
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text)) or "不明"


__all__ = [
    "ActionEffect",
    "ActionIntent",
    "ActionMode",
    "ActionOutputMode",
    "ActionRegistry",
    "ActionResult",
    "ActionSpec",
    "ActionStatus",
    "DISCORD_ACTIVE_REPLY_TRIGGER",
    "DISCORD_TRIGGER_METADATA_KEY",
    "NaturalActionRouter",
    "PlannerActionContract",
    "artifact_kinds_from_schema",
    "artifact_list_bounds_from_schema",
]
