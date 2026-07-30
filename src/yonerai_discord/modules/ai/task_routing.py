"""Discord AI入力を、モデル経路と進捗表示へ一度だけ分類する。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

from yonerai_discord.ai_control import RiskLevel, TaskComplexity, TaskKind


_COMPLEX_MARKERS = (
    "詳しく分析",
    "設計して",
    "原因を調査",
    "コードを書",
    "実装して",
    "リファクタ",
    "設計書",
    "web作って",
    "webを作",
    "ウェブを作",
    "サイトを作",
    "html",
    "全機能",
    "総合",
    "一式",
    "自己進化",
    "比較して",
    "手順を考え",
    "deep analysis",
    "design",
    "implement",
    "debug",
)
_CODE_MARKERS = (
    "コードを書",
    "実装して",
    "リファクタ",
    "web作って",
    "webを作",
    "ウェブを作",
    "サイトを作",
    "html",
    "write code",
    "implement",
    "refactor",
    "create a website",
)
_SELF_EVOLUTION_MARKERS = (
    "自己進化",
    "self-evolution",
    "self evolution",
)
_TINY_FORMATTING_MARKERS = (
    "整形だけ",
    "箇条書きにして",
    "jsonに整形",
    "誤字を直して",
    "表記を統一",
    "format only",
)
_TINY_CLASSIFICATION_MARKERS = (
    "分類だけ",
    "カテゴリを一つ",
    "yes/noだけ",
    "真偽だけ",
    "classify only",
)
_ARTIFACT_MARKERS = (
    "html",
    "webを作",
    "web作って",
    "ウェブを作",
    "サイトを作",
    "コードを書",
    "ファイルを作",
    "画像を生成",
    "画像生成",
    "動画を生成",
    "動画生成",
    "音楽を生成",
    "音楽生成",
    "create a website",
    "generate an image",
    "generate a video",
    "generate music",
)

_SITE_MARKERS = (
    "web作って",
    "webを作",
    "ウェブを作",
    "サイトを作",
    "ホームページを作",
    "create a website",
    "build a website",
)
_MEDIA_MARKERS = (
    "画像を生成",
    "画像生成",
    "動画を生成",
    "動画生成",
    "音楽を生成",
    "音楽生成",
    "generate an image",
    "generate a video",
    "generate music",
)
_MUSIC_MARKERS = ("流して", "再生して", "プレイリスト", "play music")
_MEMORY_MARKERS = ("覚えて", "記憶", "メモして", "remember this")
_MODERATION_MARKERS = ("ban", "kick", "timeout", "追放", "投稿を削除", "モデレーション")
_KNOWLEDGE_MARKERS = ("教えて", "説明して", "とは", "なぜ", "how ", "what ", "why ")
_UNKNOWN_OPERATION_MARKERS = (
    "設定を変更して",
    "pcを操作して",
    "パソコンを操作して",
    "ブラウザを操作して",
    "shellを実行して",
    "シェルを実行して",
    "コマンドを実行して",
    "再起動して",
    "停止して再開して",
    "削除して復元して",
)
_WEB_TARGET_RE = re.compile(
    r"(?:https?://[^\s]+|(?<![@\w])(?:[a-z0-9](?:[a-z0-9-]{0,62})?\.)+[a-z]{2,63}(?:/[^\s]*)?)",
    re.IGNORECASE,
)
_HTTPS_TARGET_RE = re.compile(r"https://[^\s]+", re.IGNORECASE)
_BROWSER_EXECUTION_SUFFIXES = (
    "スクショして",
    "スクショ撮って",
    "スクショを撮って",
    "スクリーンショットして",
    "スクリーンショット撮って",
    "スクリーンショットを撮って",
    "画面を撮って",
    "ページを撮って",
    "開いて",
    "アクセスして",
)
_MEDIA_INSPECTION_EVIDENCE_MARKERS = (
    "字幕データ",
    "字幕を取得",
    "文字起こし",
    "画像認識",
    "代表フレーム",
    "動画を見",
    "動画見て",
    "映像を見",
)
_MEDIA_INSPECTION_EXECUTION_SUFFIXES = (
    "把握して",
    "分析して",
    "解析して",
    "要約して",
    "確認して",
    "見て",
)
_MEDIA_INSPECTION_NATURAL_MARKERS = (
    "これ何",
    "これは何",
    "これなに",
    "これはなに",
    "どういう",
    "何の動画",
    "どんな動画",
    "内容を教えて",
    "内容教えて",
    "説明して",
    "要約して",
    "まとめて",
    "分析して",
    "解析して",
    "確認して",
    "見て",
    "把握して",
    "what is this",
    "explain",
    "summarize",
    "analyze",
)
_MEDIA_PLAYBACK_SUFFIXES = (
    "再生",
    "再生して",
    "を再生して",
    "流して",
    "を流して",
    "ながして",
    "かけて",
    "リピート",
    "リピートして",
    "プレイリスト",
    "プレイリストに追加して",
    "キュー",
    "キューに追加して",
)
_ENGLISH_MEDIA_PLAYBACK_RE = re.compile(
    r"^(?:please\s+)?(?:play|queue|loop)(?:\s+.+)?$",
    re.IGNORECASE,
)
_ENGLISH_BROWSER_EXECUTION_RE = re.compile(
    r"^(?:please\s+)?(?:open\s+.+|(?:.+\s+and\s+)?take\s+(?:a\s+)?(?:4k\s+)?screenshot(?:\s+of\s+.+)?)$",
    re.IGNORECASE,
)
_AMBIGUOUS_OPERATION_PREFIXES = ("それを", "これを", "あれを")
_AMBIGUOUS_OPERATION_SUFFIXES = ("止めて", "変更して", "実行して", "削除して", "公開して")
_OPERATION_EXPLANATION_MARKERS = (
    "とは",
    "どういう意味",
    "説明して",
    "翻訳して",
    "訳して",
    "英語にして",
    "引用",
)
_REPETITION_RE = re.compile(r"(?:[2-9]|[1-9][0-9]{1,2})\s*(?:回|かい|times?(?![a-z0-9_]))|複数回|繰り返")
_SEQUENCE_RE = re.compile(
    r"(?:そのあと|それから|次に|続けて|"
    r"(?<!\w)after\s+that(?!\w)|(?<!\w)then(?!\w)|(?<!\w)next(?!\w))"
)
_EXECUTION_CLAUSE_END_RE = re.compile(
    r"(?:して|やって|作って|つくって|選んで|振って|振る|流して|ながして|再生して|かけて|"
    r"見せて|撮って|並べて|つないで|繋げて|実行して)(?:[。.!！])?\Z"
)
_ENGLISH_IMPERATIVE_RE = re.compile(
    r"(?:please\s+)?(?:roll|play|create|make|generate|run|execute|show|search|list|check|hash|encode|compose)\b"
)

UNKNOWN_OPERATION_REPLY = "操作の対象と、行いたい操作を1つに絞って言い直してください。"
BROWSER_OPERATION_UNAVAILABLE_REPLY = (
    "このWeb操作を登録済みの安全な実行形式へ一致させられなかったため、"
    "通常AIへ切り替えず中止しました。URLは `https://` から指定してください。"
    "4K指定は現在のスクリーンショット経路では未対応です。"
)
MEDIA_INSPECTION_UNAVAILABLE_REPLY = (
    "このURLの内容理解には、字幕・音声・代表フレーム等の根拠を取得できる"
    "許可済みメディア解析Providerが必要です。現在は未接続のため実行せず、"
    "ページタイトルやサムネイルだけから推測回答することも中止しました。"
    "本人所有または処理許諾済みの動画ファイルを添付するか、対応Providerを有効にしてください。"
)


def requests_multi_step_execution(prompt: str) -> bool:
    """回数指定または順序接続を含む実行依頼を、atomic actionから分離する。"""

    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", prompt).casefold().split())
    if _REPETITION_RE.search(normalized) is not None:
        return True
    for match in _SEQUENCE_RE.finditer(normalized):
        before = normalized[: match.start()].strip(" \t\r\n,、。.!?！？;；:")
        after = normalized[match.end() :].strip(" \t\r\n,、。.!?！？;；:")
        if (
            before
            and after
            and (
                _has_sequence_delimiter(normalized, match.start())
                or (_looks_like_execution_clause(before) and _looks_like_execution_clause(after))
            )
        ):
            return True
    return False


def _has_sequence_delimiter(value: str, connector_start: int) -> bool:
    prefix = value[:connector_start].rstrip()
    return bool(prefix) and prefix[-1] in ",、。.!?！？;；:"


def _looks_like_execution_clause(value: str) -> bool:
    return bool(value) and (
        _EXECUTION_CLAUSE_END_RE.search(value) is not None or _ENGLISH_IMPERATIVE_RE.match(value) is not None
    )


class AIIntent(StrEnum):
    """入力分類。実行権限そのものではなく、候補を狭めるためだけに使う。"""

    CONVERSATION = "conversation"
    KNOWLEDGE = "knowledge"
    WEB_RESEARCH = "web_research"
    CODE = "code"
    SITE = "site"
    MEDIA = "media"
    MUSIC = "music"
    MEMORY = "memory"
    MODERATION = "moderation"
    SELF_EVOLUTION = "self_evolution"
    UNKNOWN = "unknown"


class AIExecutionMode(StrEnum):
    DIRECT = "direct"
    TASK = "task"


class RetrievalSource(StrEnum):
    PERSONAL_MEMORY = "personal_memory"
    WEB = "web"


class AIModelTool(StrEnum):
    """外部modelへ実際に公開できるtool。未列挙のtoolは常に拒否する。"""

    WEB_SEARCH = "web_search"


@dataclass(frozen=True, slots=True)
class AIExecutionBudget:
    """request単位の上限。現行Discord adapterは無制限agent loopを許可しない。"""

    max_provider_calls: int = 1
    max_tool_calls: int = 0
    time_budget_seconds: int = 30

    def __post_init__(self) -> None:
        values = (self.max_provider_calls, self.max_tool_calls, self.time_budget_seconds)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("execution budget values must be integers")
        if self.max_provider_calls != 1:
            raise ValueError("Discord AI requests currently allow exactly one provider call")
        if not 0 <= self.max_tool_calls <= 1:
            raise ValueError("max_tool_calls must be zero or one")
        if not 5 <= self.time_budget_seconds <= 120:
            raise ValueError("time_budget_seconds must be between 5 and 120")


@dataclass(frozen=True, slots=True)
class AITaskRoute:
    """カード表示とprovider選択が共有する、外部へ安全な分類結果。"""

    kind: TaskKind
    complexity: TaskComplexity
    risk: RiskLevel
    uses_tools: bool
    web_search: bool
    expects_artifact: bool
    show_progress: bool
    intent: AIIntent = AIIntent.CONVERSATION
    execution_mode: AIExecutionMode = AIExecutionMode.DIRECT
    retrieval_sources: tuple[RetrievalSource, ...] = (RetrievalSource.PERSONAL_MEMORY,)
    allowed_model_tools: tuple[AIModelTool, ...] = ()
    budget: AIExecutionBudget = AIExecutionBudget()
    repetition_requested: bool = False
    reason_codes: tuple[str, ...] = ("standard_conversation",)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TaskKind):
            raise TypeError("kind must be a TaskKind")
        if not isinstance(self.complexity, TaskComplexity):
            raise TypeError("complexity must be a TaskComplexity")
        if not isinstance(self.risk, RiskLevel):
            raise TypeError("risk must be a RiskLevel")
        for name in ("uses_tools", "web_search", "expects_artifact", "show_progress", "repetition_requested"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")
        if self.web_search and not self.uses_tools:
            raise ValueError("web_search requires uses_tools")
        if not isinstance(self.intent, AIIntent):
            raise TypeError("intent must be an AIIntent")
        if not isinstance(self.execution_mode, AIExecutionMode):
            raise TypeError("execution_mode must be an AIExecutionMode")
        retrieval_sources = tuple(self.retrieval_sources)
        allowed_model_tools = tuple(self.allowed_model_tools)
        if any(not isinstance(source, RetrievalSource) for source in retrieval_sources):
            raise TypeError("retrieval_sources must contain RetrievalSource values")
        if any(not isinstance(tool, AIModelTool) for tool in allowed_model_tools):
            raise TypeError("allowed_model_tools must contain AIModelTool values")
        if len(retrieval_sources) != len(set(retrieval_sources)):
            raise ValueError("retrieval_sources must be unique")
        if len(allowed_model_tools) != len(set(allowed_model_tools)) or len(allowed_model_tools) > 1:
            raise ValueError("allowed_model_tools must be empty or contain only web_search")
        if allowed_model_tools and not self.uses_tools:
            raise ValueError("allowed_model_tools require uses_tools")
        if self.web_search and allowed_model_tools != (AIModelTool.WEB_SEARCH,):
            raise ValueError("web_search route may expose only the web_search model tool")
        if not self.web_search and allowed_model_tools:
            raise ValueError("model tools require an explicit web_search route")
        if not isinstance(self.budget, AIExecutionBudget):
            raise TypeError("budget must be an AIExecutionBudget")
        if self.budget.max_tool_calls != len(allowed_model_tools):
            raise ValueError("tool budget must exactly match the model tool allowlist")
        if self.intent is AIIntent.UNKNOWN and (
            self.uses_tools or self.web_search or retrieval_sources or allowed_model_tools or self.budget.max_tool_calls
        ):
            raise ValueError("unknown routes cannot use retrieval, context, or tools")
        reason_codes = tuple(self.reason_codes)
        if (
            not reason_codes
            or len(reason_codes) > 12
            or any(not code or len(code) > 64 or not code.replace("_", "").isalnum() for code in reason_codes)
        ):
            raise ValueError("reason_codes must be a short machine-readable tuple")
        object.__setattr__(self, "retrieval_sources", retrieval_sources)
        object.__setattr__(self, "allowed_model_tools", allowed_model_tools)
        object.__setattr__(self, "reason_codes", reason_codes)


def classify_ai_task(
    prompt: str,
    *,
    web_search: bool = False,
    attachment_count: int = 0,
    uses_tools: bool = False,
) -> AITaskRoute:
    """追加のAI呼出しをせず、同じ入力からモデル経路と表示経路を決める。

    通常会話はTerraへ送り、コード・自己進化・長文・ツール・添付など
    明確に重い依頼だけをcomplexとしてSolへ上げる。Lunaの狭い経路は、
    内部の明示的な分類・整形タスクだけに残し、通常会話を自動降格しない。
    """

    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    if type(web_search) is not bool or type(uses_tools) is not bool:
        raise TypeError("web_search and uses_tools must be booleans")
    if isinstance(attachment_count, bool) or not isinstance(attachment_count, int) or attachment_count < 0:
        raise ValueError("attachment_count must be a non-negative integer")

    normalized = " ".join(prompt.casefold().split())
    execution_normalized = unicodedata.normalize("NFKC", normalized)
    effective_uses_tools = uses_tools or web_search
    expects_artifact = any(marker in normalized for marker in _ARTIFACT_MARKERS)

    tiny_formatting = (
        len(prompt) <= 240
        and not effective_uses_tools
        and attachment_count == 0
        and not expects_artifact
        and any(marker in normalized for marker in _TINY_FORMATTING_MARKERS)
    )
    tiny_classification = (
        len(prompt) <= 240
        and not effective_uses_tools
        and attachment_count == 0
        and not expects_artifact
        and any(marker in normalized for marker in _TINY_CLASSIFICATION_MARKERS)
    )

    site_request = any(marker in normalized for marker in _SITE_MARKERS)
    media_request = any(marker in normalized for marker in _MEDIA_MARKERS)
    browser_operation = _looks_like_browser_operation(execution_normalized)
    media_inspection_operation = parse_media_inspection_request(execution_normalized) is not None
    unknown_operation = (
        media_inspection_operation
        or browser_operation
        or _looks_like_unknown_operation(
            normalized,
            uses_tools=uses_tools,
            site_request=site_request,
            media_request=media_request,
        )
    )
    if any(marker in normalized for marker in _SELF_EVOLUTION_MARKERS):
        kind = TaskKind.SELF_EVOLUTION
    elif any(marker in normalized for marker in _CODE_MARKERS):
        kind = TaskKind.CODE_GENERATION
    elif tiny_formatting:
        kind = TaskKind.FORMATTING
    elif tiny_classification:
        kind = TaskKind.CLASSIFICATION
    else:
        kind = TaskKind.GENERAL

    multi_step_requested = requests_multi_step_execution(execution_normalized)
    complex_request = (
        len(prompt) >= 600
        or any(marker in normalized for marker in _COMPLEX_MARKERS)
        or multi_step_requested
        or effective_uses_tools
        or attachment_count > 0
    )
    tiny_request = (tiny_formatting or tiny_classification) and not complex_request
    complexity = (
        TaskComplexity.TINY if tiny_request else TaskComplexity.COMPLEX if complex_request else TaskComplexity.STANDARD
    )
    risk = RiskLevel.LOW if tiny_request else RiskLevel.NORMAL
    show_progress = complex_request or expects_artifact

    if unknown_operation:
        intent = AIIntent.UNKNOWN
        kind = TaskKind.GENERAL
        complexity = TaskComplexity.STANDARD
        risk = RiskLevel.NORMAL
        effective_uses_tools = False
        web_search = False
        expects_artifact = False
        show_progress = False
    elif web_search:
        intent = AIIntent.WEB_RESEARCH
    elif any(marker in normalized for marker in _SELF_EVOLUTION_MARKERS):
        intent = AIIntent.SELF_EVOLUTION
    elif site_request:
        intent = AIIntent.SITE
    elif media_request:
        intent = AIIntent.MEDIA
    elif any(marker in normalized for marker in _MODERATION_MARKERS):
        intent = AIIntent.MODERATION
        risk = RiskLevel.HIGH
    elif any(marker in normalized for marker in _MUSIC_MARKERS):
        intent = AIIntent.MUSIC
    elif any(marker in normalized for marker in _MEMORY_MARKERS):
        intent = AIIntent.MEMORY
    elif kind is TaskKind.CODE_GENERATION:
        intent = AIIntent.CODE
    elif any(marker in normalized for marker in _KNOWLEDGE_MARKERS):
        intent = AIIntent.KNOWLEDGE
    else:
        intent = AIIntent.CONVERSATION

    execution_mode = AIExecutionMode.TASK if show_progress else AIExecutionMode.DIRECT
    retrieval_sources: tuple[RetrievalSource, ...]
    if intent is AIIntent.UNKNOWN or tiny_request or intent in {AIIntent.MODERATION, AIIntent.MUSIC}:
        retrieval_sources = ()
    elif web_search:
        retrieval_sources = (RetrievalSource.PERSONAL_MEMORY, RetrievalSource.WEB)
    else:
        retrieval_sources = (RetrievalSource.PERSONAL_MEMORY,)
    allowed_model_tools = (AIModelTool.WEB_SEARCH,) if web_search and intent is not AIIntent.UNKNOWN else ()
    budget = AIExecutionBudget(
        max_tool_calls=len(allowed_model_tools),
        time_budget_seconds=120 if execution_mode is AIExecutionMode.TASK else 30,
    )
    reason_codes: list[str] = [f"intent_{intent.value}", f"complexity_{complexity.value}"]
    if attachment_count:
        reason_codes.append("has_attachments")
    if web_search:
        reason_codes.append("explicit_web_search")
    if expects_artifact:
        reason_codes.append("expects_artifact")
    if risk is RiskLevel.HIGH:
        reason_codes.append("high_risk")
    repetition_requested = _REPETITION_RE.search(execution_normalized) is not None
    if repetition_requested:
        reason_codes.append("repetition_requested")
    elif multi_step_requested:
        reason_codes.append("multi_step_requested")
    if intent is AIIntent.CONVERSATION and complexity is TaskComplexity.STANDARD:
        reason_codes = ["standard_conversation"]
    elif intent is AIIntent.UNKNOWN:
        reason_codes = [
            (
                "media_inspection_unavailable"
                if media_inspection_operation
                else "browser_operation_unavailable"
                if browser_operation
                else "unknown_operation"
            )
        ]
    return AITaskRoute(
        kind=kind,
        complexity=complexity,
        risk=risk,
        uses_tools=effective_uses_tools,
        web_search=web_search,
        expects_artifact=expects_artifact,
        show_progress=show_progress,
        intent=intent,
        execution_mode=execution_mode,
        retrieval_sources=retrieval_sources,
        allowed_model_tools=allowed_model_tools,
        budget=budget,
        repetition_requested=repetition_requested,
        reason_codes=tuple(reason_codes),
    )


def _looks_like_unknown_operation(
    normalized: str,
    *,
    uses_tools: bool,
    site_request: bool,
    media_request: bool,
) -> bool:
    if any(marker in normalized for marker in _OPERATION_EXPLANATION_MARKERS):
        return False
    if uses_tools:
        return True
    if any(marker in normalized for marker in _UNKNOWN_OPERATION_MARKERS):
        return True
    if any(normalized.startswith(prefix) for prefix in _AMBIGUOUS_OPERATION_PREFIXES) and any(
        normalized.endswith(suffix) for suffix in _AMBIGUOUS_OPERATION_SUFFIXES
    ):
        return True
    operation_families = sum(
        (
            site_request,
            media_request,
            any(marker in normalized for marker in _MODERATION_MARKERS),
            any(marker in normalized for marker in _MUSIC_MARKERS),
            any(marker in normalized for marker in _MEMORY_MARKERS),
            any(marker in normalized for marker in _SELF_EVOLUTION_MARKERS),
        )
    )
    return operation_families > 1


def _looks_like_browser_operation(normalized: str) -> bool:
    """Web実行らしい入力を、未登録時に通常の文章生成へ落とさない。"""

    if any(marker in normalized for marker in _OPERATION_EXPLANATION_MARKERS):
        return False
    if normalized.rstrip().endswith(("?", "？")) or _WEB_TARGET_RE.search(normalized) is None:
        return False
    imperative = normalized.strip().rstrip("。.!！").strip()
    return (
        imperative.endswith(_BROWSER_EXECUTION_SUFFIXES)
        or _ENGLISH_BROWSER_EXECUTION_RE.fullmatch(imperative) is not None
    )


def parse_media_inspection_request(prompt: str) -> tuple[str, str] | None:
    """YouTube URLの自然な内容質問または明示的なメディア理解要求を抽出する。"""

    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", prompt).strip().split())
    matches = tuple(_HTTPS_TARGET_RE.finditer(normalized))
    if len(matches) != 1:
        return None
    match = matches[0]
    url = match.group(0).rstrip("。.!！,，、")
    if not 8 <= len(url) <= 2_048:
        return None
    youtube_video = _looks_like_youtube_video_url(url)
    instruction = f"{normalized[: match.start()]} {normalized[match.end() :]}".strip()
    instruction = " ".join(instruction.split())
    if not instruction:
        return (url, "この動画の内容を説明して") if youtube_video else None
    if len(instruction) > 1_000:
        return None
    folded_instruction = f" {instruction.casefold()} "
    if _looks_like_media_playback_instruction(instruction):
        return None
    imperative = instruction.rstrip("。.!！").strip()
    explicit_request = any(
        marker in instruction for marker in _MEDIA_INSPECTION_EVIDENCE_MARKERS
    ) and imperative.endswith(_MEDIA_INSPECTION_EXECUTION_SUFFIXES)
    natural_youtube_request = youtube_video and (
        instruction.rstrip().endswith(("?", "？"))
        or any(marker in folded_instruction for marker in _MEDIA_INSPECTION_NATURAL_MARKERS)
    )
    if not explicit_request and not natural_youtube_request:
        return None
    return url, instruction


def _looks_like_youtube_video_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.casefold() != "https" or parsed.username or parsed.password or port is not None:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path.strip("/")
    if host == "youtu.be":
        return bool(path and "/" not in path)
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return False
    if path.startswith("shorts/"):
        return bool(path.removeprefix("shorts/")) and "/" not in path.removeprefix("shorts/")
    if path == "watch":
        video_ids = parse_qs(parsed.query).get("v", ())
        return len(video_ids) == 1 and bool(video_ids[0])
    return False


def _looks_like_media_playback_instruction(instruction: str) -> bool:
    if instruction.rstrip().endswith(("?", "？")):
        return False
    imperative = instruction.casefold().rstrip(" \t\r\n。.!！?？").strip()
    return imperative.endswith(_MEDIA_PLAYBACK_SUFFIXES) or _ENGLISH_MEDIA_PLAYBACK_RE.fullmatch(imperative) is not None


__all__ = [
    "AIExecutionMode",
    "AIExecutionBudget",
    "AIIntent",
    "AIModelTool",
    "AITaskRoute",
    "BROWSER_OPERATION_UNAVAILABLE_REPLY",
    "MEDIA_INSPECTION_UNAVAILABLE_REPLY",
    "RetrievalSource",
    "UNKNOWN_OPERATION_REPLY",
    "classify_ai_task",
    "parse_media_inspection_request",
]
