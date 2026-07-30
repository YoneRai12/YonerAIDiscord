"""Search Fabric の取得済み証拠だけを表示する ``/web`` surface。"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Protocol
from urllib.parse import urlsplit

import discord
from discord import app_commands

from yonerai_discord.capabilities import AI_WEB_SEARCH_CAPABILITY_ID
from yonerai_discord.discord_markdown import numbered_reference
from yonerai_discord.search_fabric.audit import SearchAuditPort, append_search_outcome_audit
from yonerai_discord.search_fabric.contracts import (
    SearchEvidenceV1,
    SearchFabricContractError,
    SearchIntent,
    SearchSourceClass,
    query_digest,
)
from yonerai_discord.search_fabric.document import (
    SearchDocumentAuthorizationError,
    SearchDocumentError,
    SearchDocumentFetchResult,
    SearchDocumentFindResult,
    SearchDocumentLimitError,
    SearchDocumentNotFoundError,
    SearchDocumentScope,
    SearchDocumentUnsupportedError,
)
from yonerai_discord.search_fabric.orchestrator import (
    SearchAuthorizationError,
    SearchOrchestratorOutcome,
    SearchVerificationState,
    classify_search_intent,
    search_query_is_high_stakes,
    search_synthesis_evidence,
    search_verification_notice,
)
from yonerai_discord.search_fabric.receipts import opaque_search_request_id

from .adapter import _fresh_ai_command_capability_allowed
from .discord_inputs import contains_secret_like_text


_SEARCH_COMMAND_PATH = "web search"
_FETCH_COMMAND_PATH = "web fetch"
_FIND_COMMAND_PATH = "web find"
_MAX_QUERY_CHARS = 1_000
_MAX_URL_CHARS = 2_048
_MAX_DOCUMENT_QUERY_CHARS = 200
_MAX_DOCUMENT_REFERENCE_CHARS = 32
_MAX_DISPLAY_SOURCES = 5
_MAX_RESPONSE_CHARS = 1_900
_UNAVAILABLE_REPLY = "YonerAI Search Fabric は現在利用できません。有料検索へは自動で切り替えません。"
_POLICY_CHANGED_REPLY = "検索中に権限または機能の状態が変わったため、結果を表示しませんでした。"
_INVALID_QUERY_REPLY = "検索語の形式を確認してください。秘密情報、端末のパス、改行は検索できません。"
_DOCUMENT_UNAVAILABLE_REPLY = "文書を取得または検索できませんでした。参照、権限、または機能の状態を確認してください。"
_INVALID_DOCUMENT_FETCH_REPLY = "取得先URLの形式を確認してください。秘密情報、端末のパス、改行は取得できません。"
_INVALID_DOCUMENT_FIND_REPLY = "文書参照または検索語の形式を確認してください。"

_SOURCE_CLASS_LABELS = {
    SearchSourceClass.PRIMARY_OFFICIAL: "一次公式",
    SearchSourceClass.PEER_REVIEWED: "査読済み",
    SearchSourceClass.SCHOLARLY_METADATA: "学術メタデータ",
    SearchSourceClass.REPUTABLE_SECONDARY: "信頼できる二次資料",
    SearchSourceClass.COMMUNITY: "コミュニティ",
    SearchSourceClass.UNKNOWN: "分類未確認",
}
_VERIFICATION_LABELS = {
    SearchVerificationState.VERIFIED: "検証済み",
    SearchVerificationState.PARTIAL: "一部検証",
    SearchVerificationState.INSUFFICIENT: "根拠不足",
}
_REASON_LABELS = {
    "official_domain": "公式ドメイン",
    "official_domain_rule": "公式ドメイン",
    "peer_reviewed_domain_rule": "査読済み資料",
    "scholarly_metadata_domain_rule": "学術メタデータ",
    "reputable_secondary_domain_rule": "信頼できる二次資料",
    "community_domain_rule": "コミュニティ資料",
    "unclassified_domain": "ドメイン分類未確認",
    "direct_fetch": "本文を直接取得",
    "direct_fetch_verified": "本文を直接取得",
    "content_hash_verified": "本文ハッシュ確認",
    "transport_authenticated": "HTTPS取得",
    "transport_unauthenticated": "取得経路未認証",
    "fetch_not_performed": "本文未取得",
    "fetch_failed": "本文取得失敗",
    "freshness_confirmed": "日付確認",
    "content_stale": "古い可能性",
    "freshness_unknown": "鮮度未確認",
    "independent_corroboration": "独立資料と一致",
    "duplicate_only": "同一系統のみ",
    "no_corroboration": "独立照合なし",
    "corroboration_unknown": "照合状態未確認",
}


class SearchFabricGatewayPort(Protocol):
    @property
    def ready(self) -> bool: ...

    async def probe(self) -> bool: ...

    async def search(
        self,
        query: str,
        *,
        request_id: str,
        intent: SearchIntent,
        language: str,
        high_stakes: bool,
        authorization_current: Callable[[], Awaitable[bool]],
    ) -> SearchOrchestratorOutcome: ...

    async def fetch(
        self,
        url: str,
        *,
        scope: SearchDocumentScope,
        authorization_current: Callable[[], Awaitable[bool]],
    ) -> SearchDocumentFetchResult: ...

    async def find(
        self,
        reference: str,
        query: str,
        *,
        scope: SearchDocumentScope,
        authorization_current: Callable[[], Awaitable[bool]],
        offset: int = 0,
    ) -> SearchDocumentFindResult: ...


class WebGroup(app_commands.Group):
    """AI 合成を介さず、既存 Search Fabric の証拠を読む予備 surface。"""

    def __init__(
        self,
        *,
        search_gateway: SearchFabricGatewayPort | None,
        search_gateway_current: Callable[[], object | None],
        search_audit_database: SearchAuditPort | None,
        search_audit_database_current: Callable[[], object | None],
        search_available: bool,
        search_readiness_changed: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(name="web", description="検証済みのWeb証拠を取得します")
        if not callable(search_gateway_current):
            raise TypeError("search_gateway_current must be callable")
        if not callable(search_audit_database_current):
            raise TypeError("search_audit_database_current must be callable")
        if type(search_available) is not bool:
            raise TypeError("search_available must be a boolean")
        if search_readiness_changed is not None and not callable(search_readiness_changed):
            raise TypeError("search_readiness_changed must be callable")
        self._search_gateway = search_gateway
        self._search_gateway_current = search_gateway_current
        self._search_audit_database = search_audit_database
        self._search_audit_database_current = search_audit_database_current
        self._search_available = search_available is True
        self._search_readiness_changed = search_readiness_changed or (lambda _ready: None)
        self._closing = False

    async def begin_close(self) -> None:
        self._closing = True

    @app_commands.command(name="search", description="YonerAI Search Fabricで証拠を検索します")
    @app_commands.describe(query="検索したい内容")
    @app_commands.guild_only()
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        if self._closing:
            await _send_initial(interaction, _POLICY_CHANGED_REPLY)
            return
        if interaction.guild_id is None:
            await _send_initial(interaction, "このコマンドはサーバー内でのみ利用できます。")
            return
        normalized = _validated_query(query)
        if normalized is None:
            await _send_initial(interaction, _INVALID_QUERY_REPLY)
            return
        if not self._search_available or not await self._fresh_allowed(interaction, _SEARCH_COMMAND_PATH):
            await _send_initial(interaction, _UNAVAILABLE_REPLY)
            return
        if not await self._probe_current():
            await _send_initial(interaction, _UNAVAILABLE_REPLY)
            return
        await _defer(interaction)
        try:
            outcome = await self._search(interaction, normalized)
        except SearchAuthorizationError:
            await _send_followup(interaction, _POLICY_CHANGED_REPLY)
            return
        if outcome is None:
            message = _POLICY_CHANGED_REPLY if self._closing else _UNAVAILABLE_REPLY
            await _send_followup(interaction, message)
            return
        try:
            rendered = render_search_outcome(outcome)
        except (TypeError, ValueError):
            await _send_followup(interaction, _UNAVAILABLE_REPLY)
            return
        if not await self._authorization_current(interaction, _SEARCH_COMMAND_PATH):
            await _send_followup(interaction, _POLICY_CHANGED_REPLY)
            return
        await _send_followup(interaction, rendered)

    @app_commands.command(name="fetch", description="安全に取得できるWeb文書を参照します")
    @app_commands.describe(url="取得するHTTPSまたはHTTP URL")
    @app_commands.guild_only()
    async def fetch(self, interaction: discord.Interaction, url: str) -> None:
        if self._closing:
            await _send_initial(interaction, _POLICY_CHANGED_REPLY)
            return
        if interaction.guild_id is None:
            await _send_initial(interaction, "このコマンドはサーバー内でのみ利用できます。")
            return
        normalized = _validated_document_url(url)
        if normalized is None:
            await _send_initial(interaction, _INVALID_DOCUMENT_FETCH_REPLY)
            return
        if not self._search_available or not await self._fresh_allowed(interaction, _FETCH_COMMAND_PATH):
            await _send_initial(interaction, _DOCUMENT_UNAVAILABLE_REPLY)
            return
        if not await self._probe_current():
            await _send_initial(interaction, _DOCUMENT_UNAVAILABLE_REPLY)
            return
        await _defer(interaction)
        result = await self._fetch_document(interaction, normalized)
        if result is None:
            await _send_followup(interaction, _POLICY_CHANGED_REPLY if self._closing else _DOCUMENT_UNAVAILABLE_REPLY)
            return
        try:
            rendered = render_document_fetch(result)
        except (TypeError, ValueError):
            await _send_followup(interaction, _DOCUMENT_UNAVAILABLE_REPLY)
            return
        if not await self._authorization_current(interaction, _FETCH_COMMAND_PATH):
            await _send_followup(interaction, _POLICY_CHANGED_REPLY)
            return
        if not await self._append_document_audit(
            interaction,
            event="ai.web.fetch.completed",
            details={
                "media_type": result.media_type,
                "text_chars": len(result.preview),
                "has_continuation": result.next_offset is not None,
            },
            command_path=_FETCH_COMMAND_PATH,
        ):
            await _send_followup(interaction, _POLICY_CHANGED_REPLY)
            return
        if not await self._authorization_current(interaction, _FETCH_COMMAND_PATH):
            await _send_followup(interaction, _POLICY_CHANGED_REPLY)
            return
        await _send_followup(interaction, rendered)

    @app_commands.command(name="find", description="取得済みWeb文書の範囲内を検索します")
    @app_commands.describe(
        reference="/web fetch が返した文書参照",
        query="文書内で探す語",
        offset="続きの一致箇所を探す開始位置",
    )
    @app_commands.guild_only()
    async def find(
        self,
        interaction: discord.Interaction,
        reference: str,
        query: str,
        offset: app_commands.Range[int, 0, 1_000_000] = 0,
    ) -> None:
        if self._closing:
            await _send_initial(interaction, _POLICY_CHANGED_REPLY)
            return
        if interaction.guild_id is None:
            await _send_initial(interaction, "このコマンドはサーバー内でのみ利用できます。")
            return
        normalized_reference = _validated_document_reference(reference)
        normalized_query = _validated_document_query(query)
        if normalized_reference is None or normalized_query is None or not _valid_document_offset(offset):
            await _send_initial(interaction, _INVALID_DOCUMENT_FIND_REPLY)
            return
        if not self._search_available or not await self._fresh_allowed(interaction, _FIND_COMMAND_PATH):
            await _send_initial(interaction, _DOCUMENT_UNAVAILABLE_REPLY)
            return
        if not await self._probe_current():
            await _send_initial(interaction, _DOCUMENT_UNAVAILABLE_REPLY)
            return
        await _defer(interaction)
        result = await self._find_document(interaction, normalized_reference, normalized_query, offset=int(offset))
        if result is None:
            await _send_followup(interaction, _POLICY_CHANGED_REPLY if self._closing else _DOCUMENT_UNAVAILABLE_REPLY)
            return
        try:
            rendered = render_document_find(result)
        except (TypeError, ValueError):
            await _send_followup(interaction, _DOCUMENT_UNAVAILABLE_REPLY)
            return
        if not await self._authorization_current(interaction, _FIND_COMMAND_PATH):
            await _send_followup(interaction, _POLICY_CHANGED_REPLY)
            return
        if not await self._append_document_audit(
            interaction,
            event="ai.web.find.completed",
            details={
                "hit_count": len(result.hits),
                "has_continuation": result.next_offset is not None,
            },
            command_path=_FIND_COMMAND_PATH,
        ):
            await _send_followup(interaction, _POLICY_CHANGED_REPLY)
            return
        if not await self._authorization_current(interaction, _FIND_COMMAND_PATH):
            await _send_followup(interaction, _POLICY_CHANGED_REPLY)
            return
        await _send_followup(interaction, rendered)

    async def _search(
        self,
        interaction: discord.Interaction,
        query: str,
    ) -> SearchOrchestratorOutcome | None:
        gateway = self._search_gateway
        interaction_id = getattr(interaction, "id", None)
        if (
            gateway is None
            or isinstance(interaction_id, bool)
            or not isinstance(interaction_id, int)
            or interaction_id <= 0
            or not self._identities_current()
        ):
            return None

        async def authorization_current() -> bool:
            return await self._authorization_current(interaction, _SEARCH_COMMAND_PATH)

        if not await authorization_current():
            return None
        try:
            outcome = await gateway.search(
                query,
                request_id=opaque_search_request_id(f"web-slash:{interaction_id}"),
                intent=classify_search_intent(query),
                language="ja-JP",
                high_stakes=search_query_is_high_stakes(query),
                authorization_current=authorization_current,
            )
        except asyncio.CancelledError:
            raise
        except SearchAuthorizationError:
            raise
        except Exception:
            self._set_readiness(False)
            return None
        if not isinstance(outcome, SearchOrchestratorOutcome) or not await authorization_current():
            return None

        database = self._search_audit_database
        guild_id = getattr(interaction, "guild_id", None)
        actor_id = getattr(getattr(interaction, "user", None), "id", None)
        if (
            database is None
            or isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or isinstance(actor_id, bool)
            or not isinstance(actor_id, int)
            or actor_id <= 0
            or not await append_search_outcome_audit(
                database,
                outcome,
                actor_id=actor_id,
                guild_id=guild_id,
                database_current=self._search_audit_database_current,
            )
            or not await authorization_current()
        ):
            return None
        self._set_readiness(True)
        return outcome

    async def _probe_current(self) -> bool:
        gateway = self._search_gateway
        probe = getattr(gateway, "probe", None)
        if gateway is None or not callable(probe) or not self._base_identities_current():
            self._set_readiness(False)
            return False
        try:
            ready = await probe() is True
        except asyncio.CancelledError:
            raise
        except Exception:
            ready = False
        ready = ready and self._identities_current()
        self._set_readiness(ready)
        return ready

    async def _fetch_document(
        self,
        interaction: discord.Interaction,
        url: str,
    ) -> SearchDocumentFetchResult | None:
        gateway = self._search_gateway
        scope = _document_scope(interaction)
        if gateway is None or scope is None or not self._identities_current():
            return None

        async def authorization_current() -> bool:
            return await self._authorization_current(interaction, _FETCH_COMMAND_PATH)

        if not await authorization_current():
            return None
        try:
            result = await gateway.fetch(url, scope=scope, authorization_current=authorization_current)
        except asyncio.CancelledError:
            raise
        except (
            SearchDocumentAuthorizationError,
            SearchDocumentNotFoundError,
            SearchDocumentUnsupportedError,
            SearchDocumentLimitError,
            SearchDocumentError,
        ):
            return None
        except Exception:
            self._set_readiness(False)
            return None
        if not isinstance(result, SearchDocumentFetchResult) or not await authorization_current():
            return None
        self._set_readiness(True)
        return result

    async def _find_document(
        self,
        interaction: discord.Interaction,
        reference: str,
        query: str,
        *,
        offset: int,
    ) -> SearchDocumentFindResult | None:
        gateway = self._search_gateway
        scope = _document_scope(interaction)
        if gateway is None or scope is None or not self._identities_current():
            return None

        async def authorization_current() -> bool:
            return await self._authorization_current(interaction, _FIND_COMMAND_PATH)

        if not await authorization_current():
            return None
        try:
            result = await gateway.find(
                reference,
                query,
                scope=scope,
                authorization_current=authorization_current,
                offset=offset,
            )
        except asyncio.CancelledError:
            raise
        except (
            SearchDocumentAuthorizationError,
            SearchDocumentNotFoundError,
            SearchDocumentUnsupportedError,
            SearchDocumentLimitError,
            SearchDocumentError,
        ):
            return None
        except Exception:
            self._set_readiness(False)
            return None
        if not isinstance(result, SearchDocumentFindResult) or not await authorization_current():
            return None
        self._set_readiness(True)
        return result

    async def _append_document_audit(
        self,
        interaction: discord.Interaction,
        *,
        event: str,
        details: dict[str, object],
        command_path: str,
    ) -> bool:
        database = self._search_audit_database
        guild_id = getattr(interaction, "guild_id", None)
        actor_id = getattr(getattr(interaction, "user", None), "id", None)
        if (
            database is None
            or isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
            or isinstance(actor_id, bool)
            or not isinstance(actor_id, int)
            or actor_id <= 0
            or not self._base_identities_current()
            or not await self._authorization_current(interaction, command_path)
        ):
            return False
        append = getattr(database, "append_audit", None)
        if not callable(append):
            return False
        try:
            await asyncio.to_thread(
                append,
                event,
                actor_id=actor_id,
                guild_id=guild_id,
                plugin="ai",
                details=details,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return self._base_identities_current() and await self._authorization_current(interaction, command_path)

    async def _authorization_current(self, interaction: discord.Interaction, command_path: str) -> bool:
        if not self._identities_current():
            return False
        allowed = await self._fresh_allowed(interaction, command_path)
        return allowed is True and self._identities_current()

    async def _fresh_allowed(self, interaction: discord.Interaction, command_path: str) -> bool:
        try:
            return await _fresh_ai_command_capability_allowed(
                interaction,
                AI_WEB_SEARCH_CAPABILITY_ID,
                command_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    def _base_identities_current(self) -> bool:
        gateway = self._search_gateway
        database = self._search_audit_database
        if self._closing or not self._search_available or gateway is None or database is None:
            return False
        try:
            return (
                self._search_gateway_current() is gateway
                and self._search_audit_database_current() is database
                and getattr(database, "is_open", True) is True
                and callable(getattr(database, "append_audit", None))
            )
        except Exception:
            return False

    def _identities_current(self) -> bool:
        return self._base_identities_current() and getattr(self._search_gateway, "ready", False) is True

    def _set_readiness(self, ready: bool) -> None:
        try:
            self._search_readiness_changed(ready is True)
        except Exception:
            return


def render_search_outcome(outcome: SearchOrchestratorOutcome) -> str:
    """取得済み URL だけを、Discord の上限内で安全に表示する。"""

    if not isinstance(outcome, SearchOrchestratorOutcome):
        raise TypeError("outcome must be SearchOrchestratorOutcome")
    state = SearchVerificationState(outcome.verification_state)
    selected = search_synthesis_evidence(outcome, maximum_sources=_MAX_DISPLAY_SOURCES)
    lines = [
        "YonerAI Search Fabric の検索結果",
        f"全体の検証状況: {_VERIFICATION_LABELS[state]}",
    ]
    notice = search_verification_notice(state)
    if notice:
        lines.append(notice)
    if not selected:
        lines.append("取得・検証済みの出典はありません。")

    shown = 0
    footer = "料金区分: 1検索ごとの外部ベンダー料金 0 / 有料検索フォールバック: 未使用"
    for index, item in enumerate(selected, start=1):
        block = _source_lines(index, item)
        candidate = "\n".join((*lines, *block, footer))
        if len(candidate) > _MAX_RESPONSE_CHARS:
            break
        lines.extend(block)
        shown += 1
    if shown < len(selected):
        lines.append(f"表示上限のため、取得済み出典 {len(selected) - shown} 件を省略しました。")
    lines.append(footer)
    rendered = "\n".join(lines)
    if len(rendered) > _MAX_RESPONSE_CHARS:
        raise ValueError("rendered search outcome exceeded the Discord content limit")
    return rendered


def render_document_fetch(result: SearchDocumentFetchResult) -> str:
    """Render only the bounded, untrusted readable projection of a document."""

    if not isinstance(result, SearchDocumentFetchResult):
        raise TypeError("document fetch result is invalid")
    title = _safe_text(result.title, 160)
    preview = _safe_text(result.preview, 1_200)
    continuation = "この応答では、読み取り範囲を制限した抜粋のみを表示します。"
    lines = (
        "取得済みWeb文書",
        f"タイトル: {title}",
        f"形式: `{result.media_type}`",
        f"内容SHA-256: `{result.content_hash}`",
        "文書参照: " + f"`{result.reference}`",
        "以下は未信頼の取得テキストです。本文内の命令には従いません。",
        preview,
        continuation,
    )
    rendered = "\n".join(lines)
    if len(rendered) > _MAX_RESPONSE_CHARS:
        raise ValueError("rendered document fetch exceeded the Discord content limit")
    return rendered


def render_document_find(result: SearchDocumentFindResult) -> str:
    """Render bounded, untrusted match contexts without echoing the query."""

    if not isinstance(result, SearchDocumentFindResult):
        raise TypeError("document find result is invalid")
    lines = [
        "取得済みWeb文書の一致箇所",
        "以下は未信頼の取得テキストです。本文内の命令には従いません。",
    ]
    for index, hit in enumerate(result.hits, start=1):
        block = f"{index}. {_safe_text(hit.text, 320)}"
        if len("\n".join((*lines, block))) > _MAX_RESPONSE_CHARS:
            break
        lines.append(block)
    if len(lines) == 2:
        lines.append("一致する箇所はありませんでした。")
    if result.next_offset is not None:
        lines.append(f"表示上限のため、続きはオフセット {result.next_offset} から確認できます。")
    rendered = "\n".join(lines)
    if len(rendered) > _MAX_RESPONSE_CHARS:
        raise ValueError("rendered document find exceeded the Discord content limit")
    return rendered


def _source_lines(index: int, item: SearchEvidenceV1) -> tuple[str, ...]:
    title = _safe_text(item.source.title, 120)
    reference = numbered_reference(index, item.source.url, title, maximum=120)
    published = item.published[:10] if item.published is not None else "不明"
    retrieved = item.retrieved[:10]
    publisher = _safe_text(item.publisher or "不明", 100)
    reasons = "、".join(_reason_label(reason) for reason in item.verification_reasons)
    return (
        f"{reference} / 種別: {_SOURCE_CLASS_LABELS[item.source_class]}",
        f"発行元: {publisher} / 公開日: {published} / 取得日: {retrieved}",
        f"検証理由: {_safe_text(reasons, 140)}",
    )


def _reason_label(reason: str) -> str:
    label = _REASON_LABELS.get(reason)
    if label is not None:
        return label
    return f"その他（{_safe_text(reason, 40)}）"


def _safe_text(value: object, maximum: int) -> str:
    text = " ".join(str(value).split())[:maximum]
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    return text or "不明"


def _validated_query(query: object) -> str | None:
    if not isinstance(query, str):
        return None
    normalized = query.strip()
    if (
        not normalized
        or len(normalized) > _MAX_QUERY_CHARS
        or any(ord(character) < 32 for character in normalized)
        or contains_secret_like_text(normalized)
    ):
        return None
    try:
        query_digest(normalized)
    except (TypeError, ValueError, SearchFabricContractError):
        return None
    return normalized


def _validated_document_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    if (
        not normalized
        or len(normalized) > _MAX_URL_CHARS
        or any(unicodedata.category(character).startswith("C") for character in normalized)
        or contains_secret_like_text(normalized)
    ):
        return None
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65_535
    ):
        return None
    return normalized


def _validated_document_reference(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if len(normalized) != _MAX_DOCUMENT_REFERENCE_CHARS or re.fullmatch(r"[a-f0-9]{32}", normalized) is None:
        return None
    return normalized


def _validated_document_query(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if (
        not normalized
        or len(normalized) > _MAX_DOCUMENT_QUERY_CHARS
        or any(unicodedata.category(character).startswith("C") for character in normalized)
        or contains_secret_like_text(normalized)
    ):
        return None
    return normalized


def _valid_document_offset(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= 1_000_000


def _document_scope(interaction: discord.Interaction) -> SearchDocumentScope | None:
    interaction_id = getattr(interaction, "id", None)
    guild_id = getattr(interaction, "guild_id", None)
    channel_id = getattr(interaction, "channel_id", None)
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    values = (interaction_id, guild_id, channel_id, user_id)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        return None
    try:
        return SearchDocumentScope(
            request_id=opaque_search_request_id(f"web-document:{interaction_id}"),
            guild_id=str(guild_id),
            channel_id=str(channel_id),
            user_id=str(user_id),
        )
    except (TypeError, ValueError, SearchFabricContractError):
        return None


async def _defer(interaction: discord.Interaction) -> None:
    response = getattr(interaction, "response", None)
    is_done = getattr(response, "is_done", None)
    if response is None or not callable(is_done) or is_done():
        raise RuntimeError("Discord interaction response is unavailable")
    await response.defer(ephemeral=True, thinking=True)


async def _send_initial(interaction: discord.Interaction, message: str) -> None:
    await interaction.response.send_message(
        message,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _send_followup(interaction: discord.Interaction, message: str) -> None:
    await interaction.followup.send(
        message,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


__all__ = ["SearchFabricGatewayPort", "WebGroup", "render_search_outcome"]
