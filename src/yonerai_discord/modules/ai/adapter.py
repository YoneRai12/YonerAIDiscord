from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import discord
from discord import app_commands

from yonerai_discord.ai_control import TaskComplexity
from yonerai_discord.capabilities import AI_WEB_SEARCH_CAPABILITY_ID, COMMAND_CAPABILITIES, COMMAND_RBAC_FLOORS
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.discord_policy import determine_rbac_level
from yonerai_discord.execution_gateway.core_contract import DiscordCoreFacts
from yonerai_discord.execution_gateway.local import LocalExecutionGateway
from yonerai_discord.execution_gateway.models import RunEvent
from yonerai_discord.execution_gateway.protocol import ExecutionGateway
from yonerai_discord.runtime_manifests.ai_memory import AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID
from yonerai_discord.search_fabric.contracts import SearchIntent
from yonerai_discord.search_fabric.audit import append_search_outcome_audit
from yonerai_discord.search_fabric.orchestrator import (
    SearchOrchestratorOutcome,
    SearchVerificationState,
    build_search_synthesis_prompt,
    classify_search_intent,
    search_synthesis_evidence,
    search_source_display_title,
    search_query_is_high_stakes,
)
from yonerai_discord.search_fabric.receipts import opaque_search_request_id
from yonerai_discord.v0_contracts import (
    FORMAL_PROVIDER_INPUT_DIRECTIVE,
    ContextBuildInput,
    ContextBuildResult,
    MemorySelectionInput,
    MemoryVisibility,
    Scope,
)
from yonerai_discord.v0_runtime.context_builder import RuntimeContextBuilder
from yonerai_discord.v0_runtime.memory_repository import V0ExplicitMemoryRepository
from yonerai_discord.v0_runtime.memory_selector import RuntimeMemorySelector

from .bounded_tools import (
    BoundedToolSet,
    EMPTY_CAPABILITY_SNAPSHOT,
    EMPTY_CATALOG_REVISION,
    StaticCapabilitySnapshot,
    ToolScopeBinding,
    capability_metadata_transport,
)
from .capability_rag import authorized_capability_ids_for_discord_actor
from .admission import AIAdmissionController, AdmissionRejection
from .consent_view import RemoteConsentScope, RemoteConsentTerminalState, RemoteConsentView
from .core_artifact_delivery import (
    CoreArtifactDeliveryError,
    CoreArtifactDeliveryPreparer,
)
from .conversation import (
    ConversationIndexConflictError,
    ConversationSessionError,
    ConversationSnapshot,
    ConversationStore,
)
from .discord_inputs import (
    AttachmentLimits,
    DiscordInputError,
    collect_discord_attachments,
    contains_secret_like_text,
)
from .display_preferences import DisplayPreferenceStore, display_mode_label, effective_display_mode
from .discord_renderer import DiscordAIResponseRenderer
from .gateway_execution import DuplicateDiscordRun, build_discord_core_facts, execute_ai_run
from .mention import _with_search_verification_notice, _with_verified_search_sources
from .models import AISource, AIRequest, DataBoundary, provider_facing_envelope_digest
from .remote_consent import RemoteConsentStore
from .service import AIService, AIUnavailableError, PrivacyBoundaryError
from .task_routing import AIIntent, UNKNOWN_OPERATION_REPLY, AITaskRoute, classify_ai_task
from .task_progress import (
    DiscordAITaskProgressRenderer,
    DiscordAITaskProgressSession,
    build_ai_progress_plan,
)
from yonerai_discord.v0_runtime.command_service import (
    AICommand,
    AICommandInput,
    CommandActor,
    CommandResult,
    CommandScope,
    V0CommandService,
)
from yonerai_discord.v0_runtime.renderer import render_command_result


_REMOTE_CONSENT_DISCLOSURE = (
    "外部AIへ質問内容を送る前に、本人の初回同意が必要です。"
    "同意はDiscord user ID単位で保存され、取消またはpolicy/disclosure版変更まで有効です。"
)
_ATTACHMENT_UNAVAILABLE_REPLY = "添付の理解は現在利用できません。設定と権限を確認してください。"
_POLICY_CHANGED_REPLY = "待機中に権限または機能設定が変わったため、AIへ送信しませんでした。"
_AI_ASK_BUSY_REPLY = "AI機能は現在混雑しています。少し待ってから再送してください。"
_DEFAULT_MAX_PENDING_CONSENT_PROMPTS = 256
_MAX_PENDING_CONSENT_PROMPTS = 4_096
_SEARCH_FABRIC_UNAVAILABLE_REPLY = "検索基盤を利用できません。外部の有料検索へは自動で切り替えません。"


class _SearchFabricGateway(Protocol):
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


@dataclass(frozen=True, slots=True)
class _PendingSlashAsk:
    """同意カードが表示されている間だけ保持するslash入力。raw bytesはまだ読まない。"""

    interaction: Any = field(repr=False)
    prompt: str = field(repr=False)
    mode: Literal["normal", "complex"]
    attachment: Any | None = field(repr=False)


class _InteractionResponseSource:
    """既存message rendererをInteraction followupへ接続する薄いreply互換層。"""

    def __init__(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction

    async def reply(self, **kwargs: Any) -> Any:
        payload = dict(kwargs)
        payload.pop("mention_author", None)
        payload["ephemeral"] = True
        payload["wait"] = True
        return await self._interaction.followup.send(**payload)


@dataclass(frozen=True, slots=True)
class _AttachmentSource:
    attachment: Any

    @property
    def attachments(self) -> tuple[Any, ...]:
        return (self.attachment,)


class AIGroup(app_commands.Group):
    model_group = app_commands.Group(name="model", description="会話を変えずにmodel preferenceを管理")
    provider_group = app_commands.Group(name="provider", description="会話を変えずにprovider preferenceを管理")

    def __init__(
        self,
        service: AIService,
        display_preferences: DisplayPreferenceStore | None = None,
        response_renderer: DiscordAIResponseRenderer | None = None,
        v0_commands: V0CommandService | None = None,
        execution_gateway: ExecutionGateway | None = None,
        context_builder: RuntimeContextBuilder | None = None,
        explicit_memory_repository: V0ExplicitMemoryRepository | None = None,
        memory_recall_allowed: Callable[[discord.Interaction], bool] | None = None,
        provider_is_local: bool | None = None,
        provider_available: bool | None = None,
        remote_consent_active: Callable[[int], bool] | None = None,
        remote_consent_store: RemoteConsentStore | None = None,
        conversation_store: ConversationStore | None = None,
        task_progress_renderer: DiscordAITaskProgressRenderer | None = None,
        attachments_available: bool = False,
        web_search_available: bool = False,
        admission: AIAdmissionController | None = None,
        max_pending_consent_prompts: int = _DEFAULT_MAX_PENDING_CONSENT_PROMPTS,
        capability_snapshot: StaticCapabilitySnapshot = EMPTY_CAPABILITY_SNAPSHOT,
        provider_catalog_revision: str | None = None,
        tool_clock: Callable[[], float] = time.monotonic,
        core_artifact_delivery: CoreArtifactDeliveryPreparer | None = None,
        search_gateway: _SearchFabricGateway | None = None,
        search_gateway_current: Callable[[], object | None] | None = None,
        search_audit_database: object | None = None,
        search_audit_database_current: Callable[[], object | None] | None = None,
        search_audit_required: bool = False,
        search_readiness_changed: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(name="ai", description="プライバシー境界付きAI機能")
        self.service = service
        self.display_preferences = display_preferences or DisplayPreferenceStore()
        self.response_renderer = response_renderer or DiscordAIResponseRenderer()
        self.v0_commands = v0_commands
        self._execution_gateway = execution_gateway
        self._context_builder = context_builder or RuntimeContextBuilder()
        self._explicit_memory_repository = explicit_memory_repository
        self._memory_selector = RuntimeMemorySelector()
        self._memory_recall_allowed = memory_recall_allowed or (lambda _interaction: False)
        known_locality = (
            provider_is_local if provider_is_local is not None else getattr(service, "provider_locality", None)
        )
        self._provider_is_local = known_locality is True
        self._provider_available = (
            bool(getattr(service, "available", False)) if provider_available is None else provider_available is True
        )
        self._remote_consent_active = remote_consent_active or (lambda _user_id: False)
        self._remote_consent_store = remote_consent_store
        self._conversation_store = conversation_store
        self._task_progress_renderer = task_progress_renderer
        self._attachments_available = attachments_available is True
        self._search_gateway = search_gateway
        self._search_gateway_current = search_gateway_current or (lambda: self._search_gateway)
        if not callable(self._search_gateway_current):
            raise TypeError("search_gateway_current must be callable")
        if type(search_audit_required) is not bool:
            raise TypeError("search_audit_required must be a boolean")
        self._search_audit_database = search_audit_database
        self._search_audit_database_current = search_audit_database_current or (lambda: self._search_audit_database)
        if not callable(self._search_audit_database_current):
            raise TypeError("search_audit_database_current must be callable")
        self._search_audit_required = search_audit_required
        self._search_readiness_changed = search_readiness_changed or (lambda _ready: None)
        if not callable(self._search_readiness_changed):
            raise TypeError("search_readiness_changed must be callable")
        self._web_search_available = (
            web_search_available is True
            and search_gateway is not None
            and (
                not search_audit_required
                or (
                    search_audit_database is not None and callable(getattr(search_audit_database, "append_audit", None))
                )
            )
        )
        if core_artifact_delivery is not None and not isinstance(
            core_artifact_delivery,
            CoreArtifactDeliveryPreparer,
        ):
            raise TypeError("core_artifact_delivery must be a CoreArtifactDeliveryPreparer")
        self._core_artifact_delivery = core_artifact_delivery
        self._admission = admission or AIAdmissionController()
        if (
            isinstance(max_pending_consent_prompts, bool)
            or not isinstance(max_pending_consent_prompts, int)
            or not 1 <= max_pending_consent_prompts <= _MAX_PENDING_CONSENT_PROMPTS
        ):
            raise ValueError("max_pending_consent_prompts must be between 1 and 4096")
        self._max_pending_consent_prompts = max_pending_consent_prompts
        self._closing = False
        self._active_run_tasks: set[asyncio.Task[object]] = set()
        self._pending_remote_consents: dict[int, tuple[RemoteConsentView, _PendingSlashAsk]] = {}
        self._capability_snapshot = capability_snapshot
        self._provider_catalog_revision = provider_catalog_revision or getattr(
            service,
            "provider_catalog_revision",
            EMPTY_CATALOG_REVISION,
        )
        if not callable(tool_clock):
            raise TypeError("tool_clock must be callable")
        self._tool_clock = tool_clock

    @model_group.command(name="list", description="利用可能なlogical model aliasを表示します")
    async def model_list(self, interaction: discord.Interaction) -> None:
        await self._run_v0(interaction, AICommand.MODEL_LIST, path="ai model list")

    @model_group.command(name="set", description="logical model aliasを自分用に保存します")
    async def model_set(self, interaction: discord.Interaction, alias: str) -> None:
        await self._run_v0(interaction, AICommand.MODEL_SET, alias, path="ai model set")

    @model_group.command(name="auto", description="model preferenceを自動選択へ戻します")
    async def model_auto(self, interaction: discord.Interaction) -> None:
        await self._run_v0(interaction, AICommand.MODEL_AUTO, path="ai model auto")

    @provider_group.command(name="list", description="canonical registryのprovider候補を表示します")
    async def provider_list(self, interaction: discord.Interaction) -> None:
        await self._run_v0(interaction, AICommand.PROVIDER_LIST, path="ai provider list")

    @provider_group.command(name="set", description="provider IDを自分用に保存します")
    async def provider_set(self, interaction: discord.Interaction, provider_id: str) -> None:
        await self._run_v0(interaction, AICommand.PROVIDER_SET, provider_id, path="ai provider set")

    @app_commands.command(name="route", description="preferred/effective routeとreasonを表示します")
    async def route(self, interaction: discord.Interaction) -> None:
        await self._run_v0(interaction, AICommand.ROUTE, path="ai route")

    @app_commands.command(name="reset", description="この会話の短期履歴だけをリセットします")
    async def reset(self, interaction: discord.Interaction) -> None:
        await self._run_v0(interaction, AICommand.RESET, path="ai reset")

    @app_commands.command(name="status", description="AI接続が利用可能か確認します")
    async def status(self, interaction: discord.Interaction) -> None:
        mode = self.display_preferences.get(int(interaction.user.id))
        await interaction.response.send_message(
            ("AI: 利用可能" if self._provider_available else "AI: 未設定（安全に休止中）")
            + f"\n表示モード: {display_mode_label(mode)}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="ask", description="AIへ質問します")
    @app_commands.describe(
        prompt="質問",
        mode="通常はTerra、複雑な分析はSol",
        allow_remote="互換用（永続同意の代替にはなりません）",
        attachment="任意の画像または許可済みファイル（1件まで）",
    )
    async def ask(
        self,
        interaction: discord.Interaction,
        prompt: str,
        mode: Literal["normal", "complex"] = "normal",
        allow_remote: bool = False,
        attachment: discord.Attachment | None = None,
    ) -> None:
        if self._closing:
            await interaction.response.send_message(
                _POLICY_CHANGED_REPLY,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message("サーバー内でのみ利用できます。", ephemeral=True)
            return
        if contains_secret_like_text(prompt):
            await interaction.response.send_message(
                "秘密情報らしい文字列を含む質問はAIへ送信できません。秘密を除いてから再送してください。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        route = classify_ai_task(prompt)
        if route.intent is AIIntent.UNKNOWN:
            await interaction.response.send_message(
                UNKNOWN_OPERATION_REPLY,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await _fresh_ai_ask_capability_allowed(interaction, COMMAND_CAPABILITIES["ai ask"]):
            await interaction.response.send_message(
                _POLICY_CHANGED_REPLY,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if attachment is not None and (
            not self._attachments_enabled(interaction)
            or not await _fresh_ai_ask_capability_allowed(interaction, AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID)
        ):
            await interaction.response.send_message(
                _ATTACHMENT_UNAVAILABLE_REPLY,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not self._provider_available:
            await interaction.response.send_message(
                "AIは未設定か、一時的に利用できません。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not self._provider_is_local and not self._remote_consent_is_active(interaction):
            await self._request_remote_consent(
                interaction,
                prompt=prompt,
                mode=mode,
                attachment=attachment,
            )
            return
        await _defer_interaction(interaction)
        await self._run_ask(interaction, prompt=prompt, mode=mode, attachment=attachment, web_search=False)

    @app_commands.command(name="search", description="明示した検索だけを Web 検索で調べます")
    @app_commands.describe(query="検索したい内容")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        """予備の slash 専用入口。通常会話を暗黙に検索へ昇格させない。"""

        if self._closing:
            await self._send_initial_reply(interaction, _POLICY_CHANGED_REPLY)
            return
        if interaction.guild_id is None:
            await self._send_initial_reply(interaction, "このコマンドはサーバー内でのみ利用できます。")
            return
        if contains_secret_like_text(query):
            await self._send_initial_reply(interaction, "秘密情報らしい内容は検索に送信できません。")
            return
        if not self._web_search_available or not await self._refresh_search_gateway_readiness():
            await self._send_initial_reply(interaction, "Web 検索は現在利用できません。")
            return
        route = classify_ai_task(query, web_search=True)
        if route.intent is AIIntent.UNKNOWN:
            await self._send_initial_reply(interaction, UNKNOWN_OPERATION_REPLY)
            return
        if not await self._fresh_search_allowed(interaction):
            await self._send_initial_reply(interaction, _POLICY_CHANGED_REPLY)
            return
        if not self._provider_available:
            await self._send_initial_reply(interaction, "AI は未設定か、一時的に利用できません。")
            return
        if not self._provider_is_local and not self._remote_consent_is_active(interaction):
            await self._request_remote_consent(
                interaction,
                prompt=query,
                mode="normal",
                attachment=None,
                web_search=True,
            )
            return
        await _defer_interaction(interaction)
        await self._run_ask(interaction, prompt=query, mode="normal", attachment=None, web_search=True)

    async def _run_ask(
        self,
        interaction: discord.Interaction,
        *,
        prompt: str,
        mode: Literal["normal", "complex"],
        attachment: Any | None,
        web_search: bool = False,
    ) -> bool:
        """同意済みslash入力を既存gateway・renderer・ConversationStoreで一度だけ実行する。"""

        if self._closing:
            await self._send_followup(interaction, _POLICY_CHANGED_REPLY)
            return False
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_run_tasks.add(current_task)
        try:
            decision = await self._admission.acquire(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                user_id=int(interaction.user.id),
            )
            if not decision.admitted:
                reply = (
                    _POLICY_CHANGED_REPLY if decision.rejection is AdmissionRejection.CLOSING else _AI_ASK_BUSY_REPLY
                )
                await self._send_followup(interaction, reply)
                return False
            assert decision.lease is not None
            async with decision.lease:
                return await self._run_admitted_ask(
                    interaction,
                    prompt=prompt,
                    mode=mode,
                    attachment=attachment,
                    web_search=web_search,
                )
        finally:
            if current_task is not None:
                self._active_run_tasks.discard(current_task)

    async def _run_admitted_ask(
        self,
        interaction: discord.Interaction,
        *,
        prompt: str,
        mode: Literal["normal", "complex"],
        attachment: Any | None,
        web_search: bool = False,
    ) -> bool:

        command_path = "ai search" if web_search else "ai ask"
        provider_prompt = prompt
        verified_sources: tuple[AISource, ...] = ()
        search_verification: SearchVerificationState | None = None
        if not await _fresh_ai_command_capability_allowed(
            interaction, COMMAND_CAPABILITIES[command_path], command_path
        ):
            await self._send_followup(interaction, _POLICY_CHANGED_REPLY)
            return False
        if web_search and (not self._web_search_available or not await self._fresh_search_allowed(interaction)):
            await self._send_followup(interaction, _POLICY_CHANGED_REPLY)
            return False
        if attachment is not None and (
            not self._attachments_enabled(interaction)
            or not await _fresh_ai_ask_capability_allowed(interaction, AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID)
        ):
            await self._send_followup(interaction, _ATTACHMENT_UNAVAILABLE_REPLY)
            return False
        if not self._provider_available:
            await self._send_followup(interaction, "AIは未設定か、一時的に利用できません。")
            return False
        if not self._provider_is_local and not self._remote_consent_is_active(interaction):
            await self._send_followup(interaction, _POLICY_CHANGED_REPLY)
            return False
        if web_search:
            evidence = await self._search_evidence(interaction, prompt)
            if evidence is None:
                await self._send_followup(interaction, _SEARCH_FABRIC_UNAVAILABLE_REPLY)
                return False
            provider_prompt, verified_sources, search_verification = evidence

        attachment_bundle = None
        if attachment is not None:
            try:

                async def attachment_read_allowed() -> bool:
                    return (
                        self._attachments_enabled(interaction)
                        and await _fresh_ai_ask_capability_allowed(interaction, COMMAND_CAPABILITIES["ai ask"])
                        and await _fresh_ai_ask_capability_allowed(
                            interaction,
                            AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID,
                        )
                    )

                attachment_bundle = await collect_discord_attachments(
                    (_AttachmentSource(attachment),),
                    enabled=self._attachments_enabled(interaction),
                    limits=self._attachment_limits(interaction),
                    read_allowed=attachment_read_allowed,
                )
            except DiscordInputError as exc:
                await self._send_followup(interaction, exc.user_message)
                return False
            if not await attachment_read_allowed():
                await self._send_followup(interaction, _POLICY_CHANGED_REPLY)
                return False

        attachments = () if attachment_bundle is None else attachment_bundle.attachments
        route = classify_ai_task(prompt, attachment_count=len(attachments), web_search=web_search)
        if route.intent is AIIntent.UNKNOWN:
            await self._send_followup(interaction, UNKNOWN_OPERATION_REPLY)
            return False
        effective_complexity = TaskComplexity.COMPLEX if mode == "complex" else route.complexity
        try:
            snapshot = await self._conversation_snapshot(interaction)
            provider_history = () if web_search else (() if snapshot is None else snapshot.history)
            provider_attachments = () if web_search else attachments
            toolset = await self._issue_toolset(
                interaction,
                route=route,
                complexity=effective_complexity,
                web_search=False,
                query=prompt,
            )
            context_result = self._build_context(
                interaction,
                provider_prompt,
                route=route,
                toolset=toolset,
                history=provider_history,
                attachments=provider_attachments,
                web_search=False,
                include_private_context=not web_search,
            )
        except asyncio.CancelledError:
            raise
        except (AIUnavailableError, TypeError, ValueError):
            await self._send_followup(interaction, "AIは未設定か、一時的に利用できません。")
            return False
        except Exception:
            await self._send_followup(interaction, "AIは未設定か、一時的に利用できません。")
            return False

        # 添付readやConversationStore待機の間にrole/module/policyが変わっても、
        # staleなInteraction memberだけでprovider直前を通さない。
        if not await _fresh_ai_command_capability_allowed(
            interaction, COMMAND_CAPABILITIES[command_path], command_path
        ):
            await self._send_followup(interaction, _POLICY_CHANGED_REPLY)
            return False
        if web_search and not await self._fresh_search_allowed(interaction):
            await self._send_followup(interaction, _POLICY_CHANGED_REPLY)
            return False
        if attachments and not await _fresh_ai_ask_capability_allowed(
            interaction,
            AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID,
        ):
            await self._send_followup(interaction, _POLICY_CHANGED_REPLY)
            return False

        memory_authorization = context_result.memory_authorization

        def provider_still_allowed() -> bool:
            if not _ai_command_provider_call_allowed(interaction, command_path):
                return False
            if attachments and not self._attachment_currently_allowed(interaction):
                return False
            if not self._provider_is_local and not self._remote_consent_is_active(interaction):
                return False
            if web_search and not _ai_ask_capability_currently_allowed(
                interaction,
                AI_WEB_SEARCH_CAPABILITY_ID,
                floor=RbacLevel.EVERYONE,
            ):
                return False
            if memory_authorization is None:
                return True
            try:
                repository = self._explicit_memory_repository
                return (
                    self._memory_recall_allowed(interaction) is True
                    and repository is not None
                    and repository.authorization_current(memory_authorization)
                )
            except Exception:
                return False

        async def provider_sink_allowed() -> bool:
            if not await _fresh_ai_command_capability_allowed(
                interaction, COMMAND_CAPABILITIES[command_path], command_path
            ):
                return False
            if attachments and not await _fresh_ai_ask_capability_allowed(
                interaction,
                AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID,
            ):
                return False
            if web_search and not await self._fresh_search_allowed(interaction):
                return False
            if not provider_still_allowed():
                return False
            candidate_ids = tuple(candidate.capability_id for candidate in toolset.candidates)
            if not candidate_ids:
                return True
            allowed = await authorized_capability_ids_for_discord_actor(
                guard=getattr(interaction.client, "capability_guard", None),
                guild=getattr(interaction, "guild", None),
                channel=getattr(interaction, "channel", None),
                user_id=int(interaction.user.id),
                capability_ids=candidate_ids,
            )
            return allowed == frozenset(candidate_ids) and provider_still_allowed()

        source = _InteractionResponseSource(interaction)
        progress_session: DiscordAITaskProgressSession | None = None
        progress_renderer = self._task_progress_renderer
        progress_plan = build_ai_progress_plan(
            route=route,
            instruction=prompt,
            attachment_count=len(attachments),
            has_reference=False,
        )
        if progress_renderer is not None and progress_plan is not None:
            try:
                progress_session = await progress_renderer.start(source, progress_plan)
            except Exception:
                progress_session = None

        async def apply_gateway_progress(event: RunEvent) -> None:
            if progress_session is not None:
                await progress_session.apply_gateway_event(event)

        try:
            core_facts = _discord_interaction_core_facts(
                interaction,
                route_mode=route.kind.value,
            )
            reply = await execute_ai_run(
                self._gateway(),
                AIRequest(
                    prompt=provider_prompt,
                    guild_id=interaction.guild_id,
                    channel_id=int(interaction.channel_id),
                    user_id=interaction.user.id,
                    provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
                    context_authorization=context_result.context_authorization,
                    boundary=(DataBoundary.REMOTE_OPT_IN if not self._provider_is_local else DataBoundary.LOCAL_ONLY),
                    system_prompt=context_result.prompt,
                    history=provider_history,
                    attachments=provider_attachments,
                    task_kind=route.kind,
                    complexity=effective_complexity,
                    risk=route.risk,
                    uses_tools=False,
                    web_search=False,
                    has_side_effects=False,
                    contains_durable_memory=memory_authorization is not None,
                    memory_authorization=memory_authorization,
                    intent=route.intent.value,
                    bounded_toolset=toolset,
                    allowed_model_tools=(),
                    max_tool_calls=0,
                ),
                idempotency_key=_discord_interaction_idempotency_key(interaction),
                conversation_key=_discord_interaction_conversation_key(interaction),
                authorization_check=provider_still_allowed,
                fresh_authorization_check=provider_sink_allowed,
                tool_capability_check=None,
                discord_core_facts=core_facts,
                accept_core_artifact_references=self._core_artifact_delivery is not None,
                on_event=apply_gateway_progress,
            )
        except DuplicateDiscordRun:
            return False
        except PrivacyBoundaryError:
            await self._fail_progress_or_followup(interaction, progress_session, _POLICY_CHANGED_REPLY)
            return False
        except (AIUnavailableError, ValueError):
            await self._fail_progress_or_followup(
                interaction,
                progress_session,
                "AIは未設定か、一時的に利用できません。",
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail_progress_or_followup(
                interaction,
                progress_session,
                "AIは未設定か、一時的に利用できません。",
            )
            return False

        if not await provider_sink_allowed():
            await self._fail_progress_or_followup(interaction, progress_session, _POLICY_CHANGED_REPLY)
            return False
        media_attachments = ()
        fresh_delivery_allowed = provider_sink_allowed
        if reply.artifact_references:
            preparer = self._core_artifact_delivery
            if preparer is None:
                await self._fail_progress_or_followup(
                    interaction,
                    progress_session,
                    "AIは未設定か、一時的に利用できません。",
                )
                return False

            async def core_delivery_allowed() -> bool:
                if (
                    not self._provider_available
                    or getattr(interaction.client, "ai_service", None) is not self.service
                    or getattr(interaction.client, "ai_execution_gateway", None) is not self._execution_gateway
                    or not await provider_sink_allowed()
                ):
                    return False
                return (
                    self._provider_available
                    and getattr(interaction.client, "ai_service", None) is self.service
                    and getattr(interaction.client, "ai_execution_gateway", None) is self._execution_gateway
                )

            try:
                media_attachments = await preparer.prepare(
                    reply.artifact_references,
                    facts=core_facts,
                    authorization_current=core_delivery_allowed,
                )
            except asyncio.CancelledError:
                raise
            except CoreArtifactDeliveryError:
                await self._fail_progress_or_followup(
                    interaction,
                    progress_session,
                    "AIは未設定か、一時的に利用できません。",
                )
                return False

            async def core_final_delivery_allowed() -> bool:
                return await preparer.currently_available(core_delivery_allowed)

            fresh_delivery_allowed = core_final_delivery_allowed
        task_summary = ""
        existing_message = None
        if progress_session is not None:
            task_summary = await progress_session.begin_terminal_success()
            existing_message = progress_session.message
        search_reply_text = (
            _with_search_verification_notice(reply.text, search_verification) if web_search else reply.text
        )
        rendered_reply_text = (
            _with_verified_search_sources(search_reply_text, verified_sources) if web_search else reply.text
        )
        display_mode = effective_display_mode(
            self.display_preferences.get(int(interaction.user.id)),
            route=route,
            content=rendered_reply_text,
        )
        try:
            rendered = await self.response_renderer.reply(
                source,
                rendered_reply_text,
                model=reply.model,
                prompt=prompt,
                artifact_scope=f"guild-{interaction.guild_id}",
                existing_message=existing_message,
                task_summary=task_summary,
                display_mode=display_mode,
                media_attachments=media_attachments,
                send_allowed=provider_still_allowed,
                fresh_send_allowed=fresh_delivery_allowed,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail_progress_or_followup(
                interaction,
                progress_session,
                "AIは未設定か、一時的に利用できません。",
            )
            return False
        response_message = rendered.primary_message
        if response_message is None:
            if progress_session is not None:
                await progress_session.final_delivery_failed("最終回答の送信に失敗")
            return False
        if progress_session is not None and not rendered.reused_message:
            await progress_session.mark_final_fallback()
        await self._append_conversation_exchange(
            snapshot,
            interaction=interaction,
            prompt=prompt,
            assistant_text=rendered.full_text,
            attachments=attachments,
            response_message=response_message,
            authorization_current=provider_sink_allowed,
        )
        return True

    async def begin_close(self) -> None:
        """slashの新規実行と未確定同意カードを閉じる。"""

        self._closing = True
        pending = tuple(view for view, _pending in self._pending_remote_consents.values())
        self._pending_remote_consents.clear()
        for view in pending:
            try:
                await view.close()
            except Exception:
                continue

    async def cancel_active_runs(self, *, timeout_seconds: float) -> bool:
        if not 0.1 <= float(timeout_seconds) <= 60.0:
            raise ValueError("timeout_seconds must be between 0.1 and 60")
        current_task = asyncio.current_task()
        active = tuple(task for task in self._active_run_tasks if task is not current_task and not task.done())
        for task in active:
            task.cancel()
        if not active:
            return True
        done, pending = await asyncio.wait(active, timeout=float(timeout_seconds))
        del done
        return not pending

    async def _request_remote_consent(
        self,
        interaction: discord.Interaction,
        *,
        prompt: str,
        mode: Literal["normal", "complex"],
        attachment: Any | None,
        web_search: bool = False,
    ) -> None:
        """既存RemoteConsentViewでslash入力を一度だけ再開する。"""

        store = self._remote_consent_store
        if self._closing:
            await interaction.response.send_message(
                _POLICY_CHANGED_REPLY,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        channel_id = getattr(interaction, "channel_id", None)
        source_id = getattr(interaction, "id", None)
        guild_id = getattr(interaction, "guild_id", None)
        invalid_ids = any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (user_id, channel_id, source_id)
        )
        if store is None or invalid_ids or isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
            await interaction.response.send_message(
                "外部AIへの初回同意が未完了です。BOTへのメンションまたはDMで同意を完了してから再実行してください。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        scope = RemoteConsentScope(guild_id, channel_id, user_id, source_id)
        pending = _PendingSlashAsk(interaction, prompt, mode, attachment)
        view: RemoteConsentView

        async def confirm(
            _confirmation_interaction: discord.Interaction,
            confirmed_scope: RemoteConsentScope,
        ) -> bool:
            current = self._pending_remote_consents.get(user_id)
            if self._closing or current is None or current[0] is not view or confirmed_scope != scope:
                return False
            command_path = "ai search" if web_search else "ai ask"
            if not await _fresh_ai_command_capability_allowed(
                interaction, COMMAND_CAPABILITIES[command_path], command_path
            ):
                return False
            if web_search and not await self._fresh_search_allowed(interaction):
                return False
            if attachment is not None and not await _fresh_ai_ask_capability_allowed(
                interaction,
                AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID,
            ):
                return False
            if not _ai_command_provider_call_allowed(interaction, command_path):
                return False
            # 再評価後に古いカードからgrantしない。ここからgrantまでawaitを置かない。
            current = self._pending_remote_consents.get(user_id)
            if self._closing or current is None or current[0] is not view:
                return False
            store.grant(guild_id=guild_id, channel_id=channel_id, user_id=user_id)
            return await self._run_ask(
                interaction,
                prompt=prompt,
                mode=mode,
                attachment=attachment,
                web_search=web_search,
            )

        def terminal(terminal_view: RemoteConsentView, _state: RemoteConsentTerminalState) -> None:
            current = self._pending_remote_consents.get(user_id)
            if current is not None and current[0] is terminal_view:
                self._pending_remote_consents.pop(user_id, None)

        view = RemoteConsentView(scope, confirm, on_terminal=terminal)
        previous = self._pending_remote_consents.get(user_id)
        views_to_close: list[RemoteConsentView] = []
        if previous is None:
            while len(self._pending_remote_consents) >= self._max_pending_consent_prompts:
                _evicted_user_id, evicted = next(iter(self._pending_remote_consents.items()))
                self._pending_remote_consents.pop(_evicted_user_id, None)
                views_to_close.append(evicted[0])
        self._pending_remote_consents[user_id] = (view, pending)
        if previous is not None:
            views_to_close.append(previous[0])
        for previous_view in views_to_close:
            await previous_view.close()
        await _defer_interaction(interaction)
        try:
            prompt_message = await interaction.followup.send(
                _REMOTE_CONSENT_DISCLOSURE,
                ephemeral=True,
                view=view,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            view.bind_prompt_message(prompt_message)
        except Exception:
            if self._pending_remote_consents.get(user_id, (None, None))[0] is view:
                self._pending_remote_consents.pop(user_id, None)
            await view.close()
            await self._send_followup(
                interaction, "同意画面を安全に表示できなかったため、外部AIには送信しませんでした。"
            )

    async def _conversation_snapshot(self, interaction: discord.Interaction) -> ConversationSnapshot | None:
        store = self._conversation_store
        if store is None:
            return None
        try:
            return await store.get_or_start(
                guild_id=int(interaction.guild_id),
                channel_id=int(interaction.channel_id),
                user_id=int(interaction.user.id),
            )
        except (ConversationSessionError, TypeError, ValueError):
            return None

    async def _append_conversation_exchange(
        self,
        snapshot: ConversationSnapshot | None,
        *,
        interaction: discord.Interaction,
        prompt: str,
        assistant_text: str,
        attachments: tuple[Any, ...],
        response_message: Any,
        authorization_current: Callable[[], bool | Awaitable[bool]],
    ) -> None:
        store = self._conversation_store
        bot_message_id = getattr(response_message, "id", None)
        if store is None or snapshot is None or isinstance(bot_message_id, bool) or not isinstance(bot_message_id, int):
            return
        try:
            await store.append_exchange(
                session_id=snapshot.session_id,
                guild_id=int(interaction.guild_id),
                channel_id=int(interaction.channel_id),
                user_id=int(interaction.user.id),
                user_text=prompt,
                assistant_text=assistant_text,
                attachments=attachments,
                bot_message_id=bot_message_id,
                authorization_current=authorization_current,
            )
        except (ConversationIndexConflictError, ConversationSessionError, TypeError, ValueError):
            return

    async def _fail_progress_or_followup(
        self,
        interaction: discord.Interaction,
        progress_session: DiscordAITaskProgressSession | None,
        message: str,
    ) -> None:
        if progress_session is not None and await progress_session.fail(message):
            return
        await self._send_followup(interaction, message)

    async def _send_followup(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.followup.send(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_initial_reply(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _search_evidence(
        self,
        interaction: discord.Interaction,
        query: str,
    ) -> tuple[str, tuple[AISource, ...], SearchVerificationState] | None:
        gateway = self._search_gateway
        try:
            gateway_current = self._search_gateway_current()
        except Exception:
            return None
        if gateway is None or gateway_current is not gateway:
            return None
        if not await self._refresh_search_gateway_readiness():
            return None

        async def authorization_current() -> bool:
            try:
                current = self._search_gateway_current()
            except Exception:
                return False
            return current is gateway and await self._fresh_search_allowed(interaction)

        if not await authorization_current():
            return None
        interaction_id = getattr(interaction, "id", None)
        if isinstance(interaction_id, bool) or not isinstance(interaction_id, int) or interaction_id <= 0:
            return None
        try:
            outcome = await gateway.search(
                query,
                request_id=opaque_search_request_id(f"slash:{interaction_id}"),
                intent=classify_search_intent(query),
                language="ja-JP",
                high_stakes=search_query_is_high_stakes(query),
                authorization_current=authorization_current,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._set_search_readiness(False)
            return None
        if not isinstance(outcome, SearchOrchestratorOutcome) or not await authorization_current():
            return None
        if self._search_audit_required:
            guild_id = getattr(interaction, "guild_id", None)
            actor_id = getattr(getattr(interaction, "user", None), "id", None)
            database = self._search_audit_database
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
        self._set_search_readiness(True)
        evidence = search_synthesis_evidence(outcome)
        if not evidence:
            return None
        sources = tuple(AISource(search_source_display_title(item), item.source.url) for item in evidence)
        try:
            provider_prompt = build_search_synthesis_prompt(query, outcome)
        except (TypeError, ValueError):
            return None
        return provider_prompt, sources, outcome.verification_state

    def _set_search_readiness(self, ready: bool) -> None:
        try:
            self._search_readiness_changed(ready is True)
        except Exception:
            return

    async def _refresh_search_gateway_readiness(self) -> bool:
        gateway = self._search_gateway
        try:
            current = self._search_gateway_current()
        except Exception:
            current = None
        probe = getattr(gateway, "probe", None)
        if gateway is None or current is not gateway or not callable(probe):
            self._set_search_readiness(False)
            return False
        try:
            ready = await probe() is True
        except asyncio.CancelledError:
            raise
        except Exception:
            ready = False
        self._set_search_readiness(ready)
        return ready

    async def _fresh_search_allowed(self, interaction: discord.Interaction) -> bool:
        return await _fresh_ai_command_capability_allowed(
            interaction,
            COMMAND_CAPABILITIES["ai ask"],
            "ai ask",
        ) and await _fresh_ai_command_capability_allowed(
            interaction,
            AI_WEB_SEARCH_CAPABILITY_ID,
            "ai search",
        )

    def _remote_consent_is_active(self, interaction: discord.Interaction) -> bool:
        try:
            return self._remote_consent_active(int(interaction.user.id)) is True
        except Exception:
            return False

    def _attachments_enabled(self, interaction: discord.Interaction) -> bool:
        settings = getattr(getattr(interaction, "client", None), "settings", None)
        return self._attachments_available and bool(getattr(settings, "ai_attachments_enabled", False))

    def _attachment_limits(self, interaction: discord.Interaction) -> AttachmentLimits:
        settings = getattr(getattr(interaction, "client", None), "settings", None)
        return AttachmentLimits(
            max_files=1,
            max_file_bytes=int(getattr(settings, "ai_attachment_max_file_bytes", 8 * 1024 * 1024)),
            max_total_bytes=int(getattr(settings, "ai_attachment_max_total_bytes", 16 * 1024 * 1024)),
            read_timeout_seconds=min(15.0, max(1.0, float(getattr(settings, "ai_timeout_seconds", 30.0)))),
        )

    def _attachment_currently_allowed(self, interaction: discord.Interaction) -> bool:
        return self._attachments_enabled(interaction) and _ai_ask_capability_currently_allowed(
            interaction,
            AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID,
            floor=RbacLevel.EVERYONE,
        )

    def _build_context(
        self,
        interaction: discord.Interaction,
        prompt: str,
        *,
        route: AITaskRoute,
        toolset: BoundedToolSet,
        history: tuple[Any, ...],
        attachments: tuple[Any, ...],
        web_search: bool = False,
        include_private_context: bool = True,
    ) -> ContextBuildResult:
        guild_id = interaction.guild_id
        channel_id = int(interaction.channel_id)
        user_id = int(interaction.user.id)
        if guild_id is None:
            scope = Scope(
                None,
                user_id,
                dm_channel_id=channel_id,
                visibility=MemoryVisibility.DIRECT_MESSAGE,
            )
        else:
            scope = Scope(guild_id, user_id, visibility=MemoryVisibility.USER_PRIVATE)
        memories = ()
        memory_authorization = None
        repository = self._explicit_memory_repository
        try:
            recall_allowed = self._memory_recall_allowed(interaction) is True
        except Exception:
            recall_allowed = False
        if include_private_context and repository is not None and recall_allowed:
            try:
                recall_scope = repository.recall_scope(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )
                if recall_scope is not None:
                    scope = recall_scope
                    candidates = repository.list(scope, limit=20)
                    memories = self._memory_selector.select(
                        MemorySelectionInput(scope, candidates, limit=6, query=prompt)
                    ).records
                    memory_authorization = repository.authorization_token(
                        scope,
                        memories,
                        request_channel_id=channel_id,
                    )
            except (PermissionError, RuntimeError, TypeError, ValueError):
                memories = ()
                memory_authorization = None
        return self._context_builder.build(
            ContextBuildInput(
                scope,
                prompt,
                memories,
                history=tuple(
                    json.dumps({"role": turn.role.value, "text": turn.text}, ensure_ascii=False) for turn in history
                ),
                attachment_refs=tuple(
                    json.dumps(
                        {"filename": item.filename, "mime_type": item.mime_type, "bytes": item.byte_length},
                        ensure_ascii=False,
                    )
                    for item in attachments
                ),
                allowed_typed_tools=toolset.effective_tools,
                memory_authorization=memory_authorization,
                request_channel_id=channel_id,
                intent=route.intent.value,
                capability_metadata=capability_metadata_transport(toolset),
                complexity=toolset.complexity.value,
                bounded_toolset_digest=toolset.digest,
                capability_catalog_revision=toolset.capability_catalog_revision,
                provider_catalog_revision=toolset.provider_catalog_revision,
                provider_envelope_sha256=provider_facing_envelope_digest(
                    prompt=prompt,
                    provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
                    history=history,
                    attachments=attachments,
                    metadata={},
                    task_kind=route.kind,
                    complexity=TaskComplexity(toolset.complexity.value),
                    risk=route.risk,
                    uses_tools=web_search,
                    web_search=web_search,
                    has_side_effects=False,
                    boundary=(DataBoundary.REMOTE_OPT_IN if not self._provider_is_local else DataBoundary.LOCAL_ONLY),
                ),
            )
        )

    async def _issue_toolset(
        self,
        interaction: discord.Interaction,
        *,
        route: AITaskRoute,
        complexity: TaskComplexity,
        web_search: bool = False,
        query: str,
    ) -> BoundedToolSet:
        candidate_ids = tuple(
            entry.capability_id
            for entry in self._capability_snapshot.entries
            if any(tag.value == route.intent.value for tag in entry.intent_tags)
        )
        eligible_capability_ids = await authorized_capability_ids_for_discord_actor(
            guard=getattr(interaction.client, "capability_guard", None),
            guild=getattr(interaction, "guild", None),
            channel=getattr(interaction, "channel", None),
            user_id=int(interaction.user.id),
            capability_ids=candidate_ids,
        )
        return BoundedToolSet.issue(
            scope=ToolScopeBinding(
                interaction.guild_id,
                int(interaction.channel_id),
                int(interaction.user.id),
            ),
            intent=route.intent.value,
            complexity=complexity.value,
            snapshot=self._capability_snapshot,
            provider_catalog_revision=self._provider_catalog_revision,
            web_search=web_search,
            issued_at=self._tool_clock(),
            query=query,
            eligible_capability_ids=eligible_capability_ids,
        )

    def _gateway(self) -> ExecutionGateway:
        gateway = self._execution_gateway
        if gateway is None:
            gateway = LocalExecutionGateway.from_ai_service(self.service)
            self._execution_gateway = gateway
        return gateway

    async def _run_v0(
        self,
        interaction: discord.Interaction,
        command: AICommand,
        value: str | None = None,
        *,
        path: str,
    ) -> None:
        service = self.v0_commands
        actor = _v0_actor(interaction)
        if service is None or actor is None or not _v0_command_currently_allowed(interaction, path):
            result = CommandResult(False, "actor_not_authorized", {})
        else:
            mutation = command in {
                AICommand.MODEL_SET,
                AICommand.MODEL_AUTO,
                AICommand.PROVIDER_SET,
                AICommand.RESET,
            }
            if mutation:
                actor = await _fresh_v0_mutation_actor(interaction, path)
            if actor is None:
                result = CommandResult(False, "authorization_changed", {})
                await _v0_reply(interaction, render_command_result(result))
                return
            result = await service.execute_ai(
                AICommandInput(actor, command, value),
                commit_check=((lambda checked_actor, _operation: checked_actor == actor) if mutation else None),
            )
            if not _v0_command_currently_allowed(interaction, path):
                result = CommandResult(False, "authorization_changed", {})
        await _v0_reply(interaction, render_command_result(result))


def _v0_actor(interaction: discord.Interaction) -> CommandActor | None:
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    channel_id = getattr(interaction, "channel_id", None)
    guild_id = getattr(interaction, "guild_id", None)
    if not isinstance(user_id, int) or not isinstance(channel_id, int):
        return None
    scope = (
        CommandScope(None, dm_channel_id=channel_id)
        if guild_id is None
        else CommandScope(int(guild_id), channel_id=channel_id)
    )
    permissions = getattr(getattr(interaction, "user", None), "guild_permissions", None)
    can_share = bool(getattr(permissions, "manage_guild", False))
    return CommandActor(user_id, scope, True, can_share)


def _v0_command_currently_allowed(interaction: discord.Interaction, path: str) -> bool:
    client = getattr(interaction, "client", None)
    if client is None or bool(getattr(client, "is_closing", False)):
        return False
    guild_id = getattr(interaction, "guild_id", None)
    capability_id = COMMAND_CAPABILITIES.get(path)
    checker = getattr(getattr(client, "capability_guard", None), "currently_allowed", None)
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if capability_id is None or not callable(checker) or not isinstance(user_id, int):
        return False
    try:
        return bool(
            checker(
                capability_id,
                guild_id=guild_id,
                user_id=user_id,
                actor_level=_v0_actor_level(interaction),
                floor=COMMAND_RBAC_FLOORS.get(path, RbacLevel.EVERYONE),
            )
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _v0_actor_level(interaction: discord.Interaction) -> RbacLevel:
    user = getattr(interaction, "user", None)
    guild = getattr(interaction, "guild", None)
    settings = getattr(getattr(interaction, "client", None), "settings", None)
    if user is None or settings is None:
        return RbacLevel.EVERYONE
    roles = getattr(user, "roles", ()) or ()
    role_ids = frozenset(
        int(role.id)
        for role in roles
        if isinstance(getattr(role, "id", None), int) and not isinstance(role.id, bool) and role.id > 0
    )
    try:
        return determine_rbac_level(
            user_id=int(user.id),
            guild_owner_id=(
                int(guild.owner_id) if guild is not None and isinstance(getattr(guild, "owner_id", None), int) else None
            ),
            permissions=getattr(user, "guild_permissions", None),
            role_ids=role_ids,
            settings=settings,
        )
    except (AttributeError, TypeError, ValueError):
        return RbacLevel.EVERYONE


async def _v0_reply(interaction: discord.Interaction, text: str) -> None:
    kwargs = {"ephemeral": True, "allowed_mentions": discord.AllowedMentions.none()}
    if interaction.response.is_done():
        await interaction.followup.send(text, **kwargs)
    else:
        await interaction.response.send_message(text, **kwargs)


async def _fresh_v0_mutation_actor(
    interaction: discord.Interaction,
    path: str,
) -> CommandActor | None:
    guild = getattr(interaction, "guild", None)
    if guild is None:
        actor = _v0_actor(interaction)
        return actor if actor is not None and _v0_command_currently_allowed(interaction, path) else None
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    channel_id = getattr(interaction, "channel_id", None)
    fetch_member = getattr(guild, "fetch_member", None)
    guard = getattr(getattr(interaction, "client", None), "capability_guard", None)
    evaluate = getattr(guard, "evaluate_fresh_member", None)
    capability_id = COMMAND_CAPABILITIES.get(path)
    if (
        not isinstance(user_id, int)
        or not isinstance(channel_id, int)
        or capability_id is None
        or not callable(fetch_member)
        or not callable(evaluate)
    ):
        return None
    try:
        member = await fetch_member(user_id)
        if getattr(member, "id", None) != user_id:
            return None
        decision = await evaluate(capability_id, guild=guild, member=member)
        actor_level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
        if not bool(getattr(decision, "allowed", False)) or actor_level < COMMAND_RBAC_FLOORS.get(
            path, RbacLevel.EVERYONE
        ):
            return None
        permissions = getattr(member, "guild_permissions", None)
        return CommandActor(
            user_id,
            CommandScope(int(guild.id), channel_id=channel_id),
            True,
            bool(getattr(permissions, "manage_guild", False)),
        )
    except (discord.HTTPException, AttributeError, KeyError, TypeError, ValueError):
        return None


def _discord_interaction_idempotency_key(interaction: discord.Interaction) -> str:
    interaction_id = getattr(interaction, "id", None)
    if isinstance(interaction_id, bool) or not isinstance(interaction_id, int) or interaction_id <= 0:
        raise AIUnavailableError("Discord interaction id is unavailable")
    return f"discord-interaction:{interaction_id}"


def _discord_interaction_conversation_key(interaction: discord.Interaction) -> str:
    guild_id = getattr(interaction, "guild_id", None)
    channel_id = getattr(interaction, "channel_id", None)
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (guild_id, channel_id, user_id)
    ):
        raise AIUnavailableError("Discord interaction scope is unavailable")
    return f"guild:{guild_id}:channel:{channel_id}:user:{user_id}"


def _discord_interaction_core_facts(
    interaction: discord.Interaction,
    *,
    route_mode: str,
) -> DiscordCoreFacts:
    guild_id = getattr(interaction, "guild_id", None)
    surface_channel_id = getattr(interaction, "channel_id", None)
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    message_id = getattr(interaction, "id", None)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (guild_id, surface_channel_id, user_id, message_id)
    ):
        raise AIUnavailableError("Discord interaction scope is unavailable")
    channel_id = surface_channel_id
    thread_id: int | None = None
    channel = getattr(interaction, "channel", None)
    if isinstance(channel, discord.Thread):
        parent_id = getattr(channel, "parent_id", None)
        if isinstance(parent_id, bool) or not isinstance(parent_id, int) or parent_id <= 0:
            raise AIUnavailableError("Discord interaction thread scope is unavailable")
        channel_id = parent_id
        thread_id = surface_channel_id
    request_id = _discord_interaction_idempotency_key(interaction)
    return build_discord_core_facts(
        user_id=user_id,
        guild_id=guild_id,
        channel_id=channel_id,
        thread_id=thread_id,
        message_id=message_id,
        request_id=request_id,
        route_mode=route_mode,
        trigger="slash",
    )


def _ai_ask_provider_call_allowed(interaction: discord.Interaction) -> bool:
    """Re-evaluate shutdown, capability policy and RBAC at the provider sink."""

    return _ai_command_provider_call_allowed(interaction, "ai ask")


def _ai_command_provider_call_allowed(interaction: discord.Interaction, path: str) -> bool:
    capability_id = COMMAND_CAPABILITIES.get(path)
    if capability_id is None:
        return False
    return _ai_ask_capability_currently_allowed(
        interaction,
        capability_id,
        floor=COMMAND_RBAC_FLOORS.get(path, RbacLevel.EVERYONE),
    )


def _ai_ask_capability_currently_allowed(
    interaction: discord.Interaction,
    capability_id: str,
    *,
    floor: RbacLevel,
) -> bool:
    """既存のcurrently_allowedをprovider/read/delivery境界でfail-closedに使う。"""

    client = getattr(interaction, "client", None)
    guild = getattr(interaction, "guild", None)
    user = getattr(interaction, "user", None)
    settings = getattr(client, "settings", None)
    guard = getattr(client, "capability_guard", None)
    checker = getattr(guard, "currently_allowed", None)
    guild_id = getattr(interaction, "guild_id", None)
    user_id = getattr(user, "id", None)
    if (
        client is None
        or bool(getattr(client, "is_closing", False))
        or settings is None
        or guild is None
        or not callable(checker)
        or isinstance(guild_id, bool)
        or not isinstance(guild_id, int)
        or guild_id <= 0
        or isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
    ):
        return False
    roles = getattr(user, "roles", ()) or ()
    role_ids = frozenset(
        int(role.id)
        for role in roles
        if isinstance(getattr(role, "id", None), int) and not isinstance(role.id, bool) and role.id > 0
    )
    try:
        actor_level = determine_rbac_level(
            user_id=user_id,
            guild_owner_id=(int(guild.owner_id) if isinstance(getattr(guild, "owner_id", None), int) else None),
            permissions=getattr(user, "guild_permissions", None),
            role_ids=role_ids,
            settings=settings,
        )
        return bool(
            checker(
                capability_id,
                guild_id=guild_id,
                user_id=user_id,
                actor_level=actor_level,
                floor=floor,
            )
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


async def _fresh_ai_ask_capability_allowed(
    interaction: discord.Interaction,
    capability_id: str,
) -> bool:
    return await _fresh_ai_command_capability_allowed(interaction, capability_id, "ai ask")


async def _fresh_ai_command_capability_allowed(
    interaction: discord.Interaction,
    capability_id: str,
    path: str,
) -> bool:
    """添付読取・request作成前にREST memberで中央policyを再評価する。"""

    client = getattr(interaction, "client", None)
    guild = getattr(interaction, "guild", None)
    user_id = getattr(getattr(interaction, "user", None), "id", None)
    guild_id = getattr(interaction, "guild_id", None)
    guard = getattr(client, "capability_guard", None)
    fetch_member = getattr(guild, "fetch_member", None)
    evaluate = getattr(guard, "evaluate_fresh_member", None)
    if (
        client is None
        or bool(getattr(client, "is_closing", False))
        or guild is None
        or getattr(guild, "id", None) != guild_id
        or isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or isinstance(guild_id, bool)
        or not isinstance(guild_id, int)
        or guild_id <= 0
        or not callable(fetch_member)
        or not callable(evaluate)
    ):
        return False
    try:
        member = await fetch_member(user_id)
        if getattr(member, "id", None) != user_id:
            return False
        decision = await evaluate(capability_id, guild=guild, member=member)
        actor_level = RbacLevel.parse(getattr(decision, "actor_level", RbacLevel.EVERYONE))
        floor = COMMAND_RBAC_FLOORS.get(path, RbacLevel.EVERYONE)
        return (
            getattr(decision, "allowed", False) is True
            and actor_level >= floor
            and _ai_ask_capability_currently_allowed(interaction, capability_id, floor=floor)
        )
    except (discord.HTTPException, AttributeError, KeyError, TypeError, ValueError):
        return False


async def _defer_interaction(interaction: discord.Interaction) -> None:
    response = getattr(interaction, "response", None)
    is_done = getattr(response, "is_done", None)
    if response is None or not callable(is_done):
        raise AIUnavailableError("Discord interaction response is unavailable")
    try:
        done = is_done()
    except Exception as exc:
        raise AIUnavailableError("Discord interaction response is unavailable") from exc
    if not done:
        await response.defer(ephemeral=True, thinking=True)
