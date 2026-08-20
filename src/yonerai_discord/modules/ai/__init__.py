"""プライバシー境界を持つAIプラグイン。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yonerai_discord.agent_audit_projection import (
    AgentAuditCursor,
    AgentAuditPage,
    AuditProjectionError,
    AuditProjectionFailureCode,
)
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.db import Database
from yonerai_discord.discord_policy import determine_rbac_level
from yonerai_discord.execution_gateway import RunInput
from yonerai_discord.execution_gateway.core_files import CoreFilesReadPortV01
from yonerai_discord.execution_gateway.local import LocalExecutionGateway
from yonerai_discord.execution_gateway.protocol import ExecutionGateway
from yonerai_discord.provider_registry import DEFAULT_CATALOG
from yonerai_discord.runtime_manifests.ai_memory import (
    AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID,
    MEMORY_CONTEXT_RECALL_CAPABILITY_ID,
)
from yonerai_discord.runtime_readiness import publish_runtime_readiness, withdraw_runtime_readiness
from yonerai_discord.search_fabric.composition import (
    SearchCompositionError,
    build_local_search_fabric_runtime,
)
from yonerai_discord.v0_runtime.context_builder import RuntimeContextBuilder
from yonerai_discord.v0_runtime.memory_repository import V0ExplicitMemoryRepository
from yonerai_discord.v0_runtime.command_service import V0CommandService
from yonerai_discord.v0_runtime.integration import (
    ConversationResetAdapter,
    ExplicitMemoryCommandAdapter,
    RuntimeRouterAvailabilityPort,
    SQLiteCommandPreferencePort,
)
from yonerai_discord.v0_runtime.provider_router import (
    ProviderPreferenceRepository,
    ProviderPreferenceRouter,
)

from .admission import AIAdmissionController
from .action_router import NaturalActionRouter
from .adapter import AIGroup
from .agent_audit_port import AgentAuditReadBinding, BoundAgentAuditReadPort
from .artifacts import ArtifactStore
from .bounded_tools import (
    EMPTY_CAPABILITY_SNAPSHOT,
    StaticCapabilitySnapshot,
    build_static_capability_snapshot,
)
from .conversation import ConversationStore, ConversationStoreStats
from .core_artifact_delivery import CoreArtifactDeliveryPreparer
from .core_runtime_composition import (
    DirectCoreRuntimeCompositionError,
    build_direct_core_gateway_factory,
)
from .core_surface import DiscordCoreSurfaceGateway, with_discord_core_facts
from .discord_renderer import DiscordAIResponseRenderer
from .display_preferences import DisplayMode, DisplayPreferenceStore
from .execution_profiles import (
    ExecutionProfileError,
    ExecutionTopology,
    HostingProfile,
    HybridExecutionGateway,
    PackagingDependencyClass,
    PackagingCandidate,
    RuntimeExecutionProfileSelection,
    compose_execution_gateway,
    compose_profiled_execution_gateway,
    profile_contract,
    resolve_runtime_execution_profile,
    validate_profile_dependencies,
)
from .mention import WEB_SEARCH_CAPABILITY_ID, AIMentionListener
from .models import (
    PROVIDER_METADATA_ALLOWED_KEYS,
    AISource,
    AIReply,
    AIRequest,
    DataBoundary,
)
from .orchestration import OrchestrationEngine
from .orchestration_composition import (
    DurableOrchestrationConfiguration,
    DurableOrchestrationConsumer,
    DurableOrchestrationRuntime,
    compose_durable_orchestration,
)
from .orchestration_planner import OrchestrationPlanner, PlannerDispatchContext
from .orchestration_planner_adapter import AIServicePlannerPort
from .provider import (
    OpenAICompatibleProvider,
    ProviderConfigurationError,
    ai_provider_endpoint_is_local,
)
from .provider_dispatch import (
    PreferenceAwareAIProviderSelector,
    ProviderReadinessTracker,
    build_runtime_ai_catalog,
)
from .ports import ProviderAuthorizationError
from .remote_consent import RemoteConsentStore
from .service import AIService, AIUnavailableError, PrivacyBoundaryError
from .site_delivery import DiscordAISiteDelivery
from .state_repository import AIStateRepository
from .task_progress import DiscordAITaskProgressRenderer, TaskStatusEmojis
from .web_adapter import WebGroup


logger = logging.getLogger(__name__)

_LOCAL_GATEWAY_DEPENDENCIES = (
    PackagingDependencyClass.PUBLIC_CODE,
    PackagingDependencyClass.PURE_RUNTIME,
)
_DIRECT_CORE_RUNTIME_DEPENDENCIES = (
    PackagingDependencyClass.OFFICIAL_SECRET,
    PackagingDependencyClass.PRIVATE_ENDPOINT,
)

if TYPE_CHECKING:
    from yonerai_discord.deployment_current_truth import M10CurrentTruthV1


async def _search_gateway_ready(gateway: object | None) -> bool:
    probe = getattr(gateway, "probe", None)
    if not callable(probe):
        return False
    try:
        return await probe() is True
    except Exception:
        return False


class AIPlugin:
    """設定がない場合は安全に休止する、AIサービスのライフサイクル境界。"""

    def __init__(
        self,
        *,
        execution_gateway_factory: Callable[[AIService], ExecutionGateway] | None = None,
        direct_core_gateway_factory: Callable[[AIService], ExecutionGateway] | None = None,
        discord_processing_gateway_factory: Callable[[AIService], ExecutionGateway] | None = None,
        hybrid_core_gateway_factory: Callable[[AIService], ExecutionGateway] | None = None,
        execution_gateway_dependency_classes: tuple[PackagingDependencyClass, ...] | None = None,
        direct_core_gateway_dependency_classes: tuple[PackagingDependencyClass, ...] | None = None,
        discord_processing_gateway_dependency_classes: tuple[PackagingDependencyClass, ...] | None = None,
        hybrid_core_gateway_dependency_classes: tuple[PackagingDependencyClass, ...] | None = None,
        hybrid_selector: Callable[[RunInput], bool] | None = None,
        core_files_read_port: CoreFilesReadPortV01 | None = None,
        search_gateway: Any | None = None,
    ) -> None:
        self._execution_gateway_factory = execution_gateway_factory
        self._direct_core_gateway_factory = direct_core_gateway_factory
        self._discord_processing_gateway_factory = discord_processing_gateway_factory
        self._hybrid_core_gateway_factory = hybrid_core_gateway_factory
        self._execution_gateway_dependency_classes = execution_gateway_dependency_classes
        self._direct_core_gateway_dependency_classes = direct_core_gateway_dependency_classes
        self._discord_processing_gateway_dependency_classes = discord_processing_gateway_dependency_classes
        self._hybrid_core_gateway_dependency_classes = hybrid_core_gateway_dependency_classes
        self._hybrid_selector = hybrid_selector
        if core_files_read_port is not None and not callable(getattr(core_files_read_port, "read_for_delivery", None)):
            raise TypeError("core_files_read_port must expose read_for_delivery()")
        self._core_files_read_port = core_files_read_port
        self._injected_search_gateway = search_gateway
        self._search_gateway = search_gateway
        self._search_gateway_owned = False
        self._active_core_files_read_port: CoreFilesReadPortV01 | None = None
        self._core_artifact_delivery: CoreArtifactDeliveryPreparer | None = None
        self._execution_profile_selection: RuntimeExecutionProfileSelection | None = None
        self._deployment_current_truth: M10CurrentTruthV1 | None = None
        self._deployment_current_truth_current: Callable[[], M10CurrentTruthV1] | None = None
        self._audit_database: Database | None = None
        self._agent_audit_port: BoundAgentAuditReadPort | None = None
        self._active_agent_audit_port: BoundAgentAuditReadPort | None = None
        self._closing = True
        self.service: AIService | None = None
        self._session_owner: OpenAICompatibleProvider | None = None
        self._bot: Any | None = None
        self._mention_listener: AIMentionListener | None = None
        self._ai_group: AIGroup | None = None
        self._web_group: WebGroup | None = None
        self._action_router: NaturalActionRouter | None = None
        self._orchestration_engine: OrchestrationEngine | None = None
        self._orchestration_runtime: DurableOrchestrationRuntime | None = None
        self._orchestration_consumer: DurableOrchestrationConsumer | None = None
        self._orchestration_planner: OrchestrationPlanner | None = None
        self._orchestration_planner_port: AIServicePlannerPort | None = None
        self._admission: AIAdmissionController | None = None
        self._remote_consent: RemoteConsentStore | None = None
        self._state_repository: AIStateRepository | None = None
        self._display_preferences: DisplayPreferenceStore | None = None
        self.conversation_store: ConversationStore | None = None
        self._explicit_memory_repository: V0ExplicitMemoryRepository | None = None
        self._v0_commands: V0CommandService | None = None
        self._execution_gateway: ExecutionGateway | None = None
        self._provider_selector: PreferenceAwareAIProviderSelector | None = None
        self._provider_readiness: ProviderReadinessTracker | None = None
        self._context_builder: RuntimeContextBuilder | None = None

    async def start(self, bot: Any) -> None:
        # A failed or repeated start must not leave a previously issued identity usable.
        self._closing = True
        self._active_agent_audit_port = None
        self._agent_audit_port = None
        settings = bot.settings
        if self._search_gateway is None:
            try:
                self._search_gateway = build_local_search_fabric_runtime(settings)
            except SearchCompositionError as exc:
                logger.warning(
                    "search_fabric_composition_unavailable",
                    extra={"error_type": type(exc).__name__},
                )
                self._search_gateway = None
            else:
                self._search_gateway_owned = self._search_gateway is not None
        selection = resolve_runtime_execution_profile(settings)
        runtime_direct_core_gateway_factory: Callable[[AIService], ExecutionGateway] | None = None
        if selection.topology is ExecutionTopology.DIRECT_CORE and self._direct_core_gateway_factory is None:
            try:
                runtime_direct_core_gateway_factory = build_direct_core_gateway_factory(settings)
            except DirectCoreRuntimeCompositionError:
                raise ExecutionProfileError("direct Core runtime configuration is unavailable") from None
        profile_dependency_classes = self._validate_execution_profile_injections(
            selection,
            settings=settings,
            runtime_direct_core_gateway_factory=runtime_direct_core_gateway_factory,
        )
        self._execution_profile_selection = selection
        self._bot = bot
        injected_database = getattr(bot, "database", None)
        self._audit_database = injected_database if isinstance(injected_database, Database) else None
        if self._audit_database is not None and self._audit_database.is_open is True:
            self._agent_audit_port = BoundAgentAuditReadPort(
                self._audit_database,
                database_current=lambda: getattr(bot, "database", None),
                port_current=lambda: self._active_agent_audit_port,
                closing_current=lambda: bool(
                    self._closing is True or self._bot is not bot or getattr(bot, "is_closing", False) is not False
                ),
            )
        database_path = getattr(settings, "database_path", None)
        if database_path is not None:
            self._state_repository = AIStateRepository(database_path)
        self._display_preferences = DisplayPreferenceStore(self._state_repository)
        setattr(bot, "ai_display_preferences", self._display_preferences)
        self.conversation_store = ConversationStore(
            ttl_seconds=settings.ai_conversation_ttl_seconds,
            max_turns=settings.ai_conversation_max_turns,
            max_text_chars=24_000,
            max_attachment_bytes=settings.ai_attachment_max_file_bytes,
            max_binary_bytes=settings.ai_attachment_max_total_bytes,
            max_attachments_per_turn=settings.ai_attachment_max_files,
            max_sessions=settings.ai_conversation_max_sessions,
            max_total_binary_bytes=settings.ai_conversation_max_total_binary_bytes,
            repository=self._state_repository,
        )
        setattr(bot, "ai_conversation_store", self.conversation_store)
        self._context_builder = RuntimeContextBuilder(history_limit=settings.ai_conversation_max_turns * 2)
        if self._state_repository is not None:
            self._explicit_memory_repository = V0ExplicitMemoryRepository(self._state_repository)
        endpoint = settings.ai_base_url
        provider: OpenAICompatibleProvider | None = None
        provider_is_local: bool | None = None
        if endpoint and selection.topology in {ExecutionTopology.LOCAL_STANDALONE, ExecutionTopology.HYBRID}:
            try:
                provider = OpenAICompatibleProvider(
                    base_url=endpoint,
                    openai_api_key=settings.openai_api_key,
                    compatible_api_key=settings.ai_api_key,
                    allow_remote=settings.ai_allow_remote,
                    allow_luna=settings.ai_allow_luna,
                    enable_web_search=bool(getattr(settings, "openai_web_search_tool_enabled", False)),
                    enable_attachments=bool(getattr(settings, "ai_attachments_enabled", False)),
                    fast_model=getattr(settings, "ai_model_fast", "gpt-5.6-luna"),
                    balanced_model=getattr(settings, "ai_model_balanced", "gpt-5.6-terra"),
                    quality_model=getattr(settings, "ai_model_quality", "gpt-5.6-sol"),
                    safety_identifier_secret=settings.ai_safety_identifier_secret,
                    timeout_seconds=settings.ai_timeout_seconds,
                    max_output_tokens=settings.ai_max_output_tokens,
                    max_response_bytes=settings.ai_max_response_bytes,
                )
            except ProviderConfigurationError as exc:
                # Key未入力などのprovider不備でBot全体を落とさず、/ai statusを残す。
                logger.warning("ai_provider_unavailable", extra={"error_type": type(exc).__name__})
            else:
                self._session_owner = provider
                provider_is_local = provider.is_local
        runtime_catalog = DEFAULT_CATALOG
        capability_snapshot = _bounded_capability_snapshot(bot)

        def capability_catalog_revision() -> str:
            current = _bounded_capability_snapshot(bot)
            if current is EMPTY_CAPABILITY_SNAPSHOT and capability_snapshot is not EMPTY_CAPABILITY_SNAPSHOT:
                return ""
            return current.content_revision

        preference_repository: ProviderPreferenceRepository | None = None
        preference_router: ProviderPreferenceRouter | None = None
        if self._state_repository is not None:
            preference_repository = ProviderPreferenceRepository(self._state_repository.v0_connection())
        if provider is not None:
            runtime_catalog = build_runtime_ai_catalog(
                provider_id=provider.runtime_provider_id,
                is_local=provider.is_local,
                model_bindings=provider.runtime_model_bindings,
                supports_web_search=provider.supports_web_search,
                supports_attachments=provider.supports_attachments,
            )
            if preference_repository is not None:
                preference_router = ProviderPreferenceRouter(preference_repository, runtime_catalog)
                # Adapter construction alone is UNKNOWN. A successful authorized
                # auto/default call promotes the same state used by dispatch and
                # `/ai route`; failed calls mark it unavailable.
                self._provider_readiness = ProviderReadinessTracker((provider.runtime_provider_id,))
                self._provider_selector = PreferenceAwareAIProviderSelector(
                    preference_router,
                    providers={provider.runtime_provider_id: provider},
                    readiness=self._provider_readiness.snapshot,
                    readiness_updates=self._provider_readiness,
                    default_provider_id=provider.runtime_provider_id,
                )
        self.service = AIService(
            provider,
            provider_selector=self._provider_selector,
            require_prepared_context=True,
            require_authorization=True,
            provider_catalog_revision=runtime_catalog.content_revision,
            capability_catalog_revision=capability_catalog_revision,
        )
        execution_gateway = self._compose_execution_gateway(
            self.service,
            selection,
            dependencies=profile_dependency_classes,
            runtime_direct_core_gateway_factory=runtime_direct_core_gateway_factory,
        )
        for method_name in ("start", "events", "submit_result", "cancel"):
            if not callable(getattr(execution_gateway, method_name, None)):
                raise TypeError(f"execution gateway must provide {method_name}()")
        self._execution_gateway = execution_gateway
        if selection.topology is ExecutionTopology.DIRECT_CORE and self._core_files_read_port is not None:
            self._active_core_files_read_port = self._core_files_read_port
            self._core_artifact_delivery = CoreArtifactDeliveryPreparer(
                self._core_files_read_port,
                port_current=lambda: self._active_core_files_read_port,
            )
        local_execution = selection.topology is ExecutionTopology.LOCAL_STANDALONE
        execution_available = self.service.available if local_execution else True
        execution_provider_is_local = provider_is_local if local_execution else False
        search_gateway_ready = await _search_gateway_ready(self._search_gateway)
        search_audit_ready = (
            isinstance(self._audit_database, Database)
            and getattr(bot, "database", None) is self._audit_database
            and self._audit_database.is_open is True
            and callable(getattr(self._audit_database, "append_audit", None))
        )
        search_surface_configured = bool(
            getattr(settings, "web_search_enabled", False)
            and getattr(settings, "web_search_backend", "") == "yonerai_search_gateway"
            and self._search_gateway is not None
            and search_audit_ready
        )
        search_configured_available = search_surface_configured and execution_available
        execution_web_search_available = search_surface_configured and search_gateway_ready

        def search_readiness_changed(ready: bool) -> None:
            current = bool(
                ready is True
                and search_surface_configured
                and self._bot is bot
                and self.service is not None
                and getattr(bot, "ai_service", None) is self.service
                and self._search_gateway is not None
                and getattr(bot, "ai_search_gateway", None) is self._search_gateway
                and self._audit_database is not None
                and getattr(bot, "database", None) is self._audit_database
                and self._audit_database.is_open is True
                and getattr(bot, "is_closing", False) is not True
            )
            setattr(bot, "ai_web_search_available", current)
            publish_runtime_readiness(bot, {WEB_SEARCH_CAPABILITY_ID: current})

        execution_attachments_available = (
            bool(provider is not None and provider.supports_attachments)
            if local_execution
            else bool(
                selection.topology is ExecutionTopology.DIRECT_CORE
                and isinstance(execution_gateway, DiscordCoreSurfaceGateway)
                and execution_gateway.files_registration_available
            )
        )
        setattr(bot, "ai_service", self.service)
        setattr(bot, "ai_execution_gateway", self._execution_gateway)
        setattr(bot, "ai_execution_profile_selection", selection)
        setattr(bot, "ai_provider_is_local", execution_provider_is_local)
        if self._search_gateway is not None:
            setattr(bot, "ai_search_gateway", self._search_gateway)
        setattr(bot, "ai_web_search_available", execution_web_search_available)
        setattr(
            bot,
            "ai_attachments_available",
            execution_attachments_available,
        )
        publish_runtime_readiness(
            bot,
            {
                "cap-can-0161": execution_available,
                # mention surfaceは外部providerなしでも本人同意案内とdeterministic local actionを処理できる。
                "cap-run-ai-mention-chat": settings.ai_mention_enabled,
                WEB_SEARCH_CAPABILITY_ID: execution_web_search_available,
                AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID: execution_attachments_available,
                MEMORY_CONTEXT_RECALL_CAPABILITY_ID: self._explicit_memory_repository is not None,
            },
        )
        self._remote_consent = RemoteConsentStore(
            ttl_seconds=getattr(settings, "ai_remote_consent_ttl_seconds", None),
            max_grants=int(getattr(settings, "ai_remote_consent_max_grants", 1_024)),
            repository=self._state_repository,
        )
        setattr(bot, "ai_remote_consent_store", self._remote_consent)
        if (
            preference_repository is not None
            and preference_router is not None
            and self._provider_selector is not None
            and self._explicit_memory_repository is not None
        ):

            def route_consent(scope: Any, user_id: int) -> bool:
                if provider_is_local is True:
                    return True
                channel_id = scope.dm_channel_id if scope.guild_id is None else scope.channel_id
                if channel_id is None or self._remote_consent is None:
                    return False
                return self._remote_consent.active(
                    guild_id=scope.guild_id,
                    channel_id=channel_id,
                    user_id=user_id,
                )

            self._v0_commands = V0CommandService(
                preferences=SQLiteCommandPreferencePort(preference_repository),
                conversation_reset=ConversationResetAdapter(self.conversation_store),
                route_availability=RuntimeRouterAvailabilityPort(
                    self._provider_selector,
                    consent_verified=route_consent,
                ),
                memory=ExplicitMemoryCommandAdapter(self._explicit_memory_repository),
                catalog=runtime_catalog,
            )
            setattr(bot, "v0_ai_command_service", self._v0_commands)

        def memory_recall_allowed(subject: Any) -> bool:
            return _memory_context_recall_allowed(
                bot,
                subject,
                repository=self._explicit_memory_repository,
            )

        task_progress_renderer = DiscordAITaskProgressRenderer(
            emojis=TaskStatusEmojis(
                processing=str(getattr(settings, "ai_status_emoji_processing", "🔄")),
                done=str(getattr(settings, "ai_status_emoji_done", "✅")),
                pending=str(getattr(settings, "ai_status_emoji_pending", "▫️")),
                failed=str(getattr(settings, "ai_status_emoji_failed", "❌")),
            )
        )
        self._admission = AIAdmissionController(
            max_global=int(getattr(settings, "ai_admission_global_concurrency", 4)),
            max_waiters=int(getattr(settings, "ai_admission_max_waiters", 32)),
            wait_timeout_seconds=float(getattr(settings, "ai_admission_wait_timeout_seconds", 2.0)),
        )
        setattr(bot, "ai_admission", self._admission)
        self._ai_group = AIGroup(
            self.service,
            self._display_preferences,
            v0_commands=self._v0_commands,
            execution_gateway=self._execution_gateway,
            context_builder=self._context_builder,
            explicit_memory_repository=self._explicit_memory_repository,
            memory_recall_allowed=memory_recall_allowed,
            provider_is_local=execution_provider_is_local,
            provider_available=execution_available,
            remote_consent_active=(
                lambda user_id: self._remote_consent is not None and self._remote_consent.active_user(user_id)
            ),
            remote_consent_store=self._remote_consent,
            conversation_store=self.conversation_store,
            task_progress_renderer=task_progress_renderer,
            attachments_available=execution_attachments_available,
            web_search_available=search_configured_available,
            admission=self._admission,
            max_pending_consent_prompts=int(getattr(settings, "ai_remote_consent_max_pending_prompts", 256)),
            capability_snapshot=capability_snapshot,
            provider_catalog_revision=runtime_catalog.content_revision,
            core_artifact_delivery=self._core_artifact_delivery,
            search_gateway=self._search_gateway,
            search_gateway_current=lambda: getattr(bot, "ai_search_gateway", None),
            search_audit_database=self._audit_database,
            search_audit_database_current=lambda: getattr(bot, "database", None),
            search_audit_required=True,
            search_readiness_changed=search_readiness_changed,
        )
        self._web_group = WebGroup(
            search_gateway=self._search_gateway,
            search_gateway_current=lambda: getattr(bot, "ai_search_gateway", None),
            search_audit_database=self._audit_database,
            search_audit_database_current=lambda: getattr(bot, "database", None),
            search_available=search_surface_configured,
            search_readiness_changed=search_readiness_changed,
        )
        bot.tree.add_command(self._ai_group)
        bot.tree.add_command(self._web_group)
        if settings.ai_mention_enabled:
            artifact_store = None
            if database_path is not None:
                artifact_store = ArtifactStore(Path(database_path).parent / "ai-artifacts")
            response_renderer = DiscordAIResponseRenderer(artifact_store=artifact_store)
            self._action_router = NaturalActionRouter(bot)
            setattr(bot, "ai_action_router", self._action_router)
            if getattr(settings, "ai_orchestration_durable_enabled", False) is True:
                if database_path is None:
                    raise RuntimeError("durable orchestration requires a database path")
                orchestration_path = (
                    Path(database_path).expanduser().resolve(strict=False).parent / "ai-orchestration.sqlite3"
                )
                runtime = compose_durable_orchestration(
                    self._action_router,
                    DurableOrchestrationConfiguration(
                        enabled=True,
                        repository_path=orchestration_path,
                    ),
                )
                if runtime is None:
                    raise RuntimeError("durable orchestration composition failed")
                consumer = DurableOrchestrationConsumer(bot)
                try:
                    await consumer.start(runtime)
                except BaseException:
                    await runtime.close()
                    raise
                self._orchestration_runtime = runtime
                self._orchestration_consumer = consumer
                self._orchestration_engine = runtime.engine
            else:
                self._orchestration_engine = OrchestrationEngine(self._action_router)
                setattr(bot, "ai_orchestration_engine", self._orchestration_engine)
            planner_port = (
                AIServicePlannerPort(
                    self.service,
                    capability_catalog_revision=capability_catalog_revision,
                )
                if self.service.available
                else None
            )
            self._orchestration_planner_port = planner_port
            self._orchestration_planner = OrchestrationPlanner(
                self._action_router.registry,
                planner_port,
            )
            if planner_port is not None:
                setattr(bot, "ai_orchestration_planner_port", planner_port)
            setattr(bot, "ai_orchestration_planner", self._orchestration_planner)
            setattr(bot, "ai_orchestration_planner_ready", planner_port is not None)
            self._mention_listener = AIMentionListener(
                self.service,
                bot,
                self.conversation_store,
                pre_ai_hook=self._action_router,
                admission=self._admission,
                remote_consent_store=self._remote_consent,
                response_renderer=response_renderer,
                task_progress_renderer=task_progress_renderer,
                site_delivery=DiscordAISiteDelivery(bot),
                provider_is_local=execution_provider_is_local,
                provider_available=execution_available,
                web_search_available=search_configured_available,
                attachments_available=execution_attachments_available,
                display_preferences=self._display_preferences,
                context_builder=self._context_builder,
                explicit_memory_repository=self._explicit_memory_repository,
                execution_gateway=self._execution_gateway,
                memory_recall_allowed=memory_recall_allowed,
                capability_snapshot=capability_snapshot,
                provider_catalog_revision=runtime_catalog.content_revision,
                orchestration_planner=self._orchestration_planner,
                orchestration_engine=self._orchestration_engine,
                core_artifact_delivery=self._core_artifact_delivery,
                search_gateway=self._search_gateway,
                search_gateway_current=lambda: getattr(bot, "ai_search_gateway", None),
                search_audit_database=self._audit_database,
                search_audit_database_current=lambda: getattr(bot, "database", None),
                search_audit_required=True,
                search_readiness_changed=search_readiness_changed,
            )
            bot.add_listener(self._mention_listener.on_message, "on_message")
        deployment_current_truth = self._build_deployment_current_truth(selection, bot=bot)

        def deployment_current_truth_current() -> M10CurrentTruthV1:
            from yonerai_discord.deployment_current_truth import build_m10_current_truth

            if (
                self._bot is not bot
                or self._deployment_current_truth_current is not deployment_current_truth_current
                or getattr(bot, "deployment_current_truth_current", None) is not deployment_current_truth_current
                or getattr(bot, "deployment_current_truth", None) is not self._deployment_current_truth
            ):
                return build_m10_current_truth()
            try:
                current = self._build_deployment_current_truth(selection, bot=bot)
            except Exception:
                current = build_m10_current_truth()
            previous = self._deployment_current_truth
            if current == previous and previous is not None:
                return previous
            self._deployment_current_truth = current
            setattr(bot, "deployment_current_truth", current)
            return current

        self._deployment_current_truth = deployment_current_truth
        self._deployment_current_truth_current = deployment_current_truth_current
        setattr(bot, "deployment_current_truth", deployment_current_truth)
        setattr(bot, "deployment_current_truth_current", deployment_current_truth_current)

        if getattr(bot, "is_closing", False) is False:
            self._closing = False
            port = self._agent_audit_port
            if (
                port is not None
                and self._bot is bot
                and self._audit_database is not None
                and getattr(bot, "database", None) is self._audit_database
                and self._audit_database.is_open is True
            ):
                self._active_agent_audit_port = port
        else:
            self._closing = True

    async def read_agent_audit_page(
        self,
        *,
        binding: AgentAuditReadBinding,
        cursor: AgentAuditCursor,
    ) -> AgentAuditPage:
        """Read the redacted audit projection through the active internal port only."""

        port = self._active_agent_audit_port
        if port is None or self._closing is True:
            raise AuditProjectionError(AuditProjectionFailureCode.AUTHORIZATION_DENIED)
        return await port.read_page(binding=binding, cursor=cursor)

    async def begin_close(self) -> None:
        """新規mention/actionを止め、stop()のdrainより先に受付を閉じる。"""

        self._closing = True
        self._active_agent_audit_port = None
        self._active_core_files_read_port = None
        listener = self._mention_listener
        ai_group = self._ai_group
        web_group = self._web_group
        admission = self._admission
        if admission is not None:
            await admission.begin_close()
        if ai_group is not None:
            await ai_group.begin_close()
        if web_group is not None:
            await web_group.begin_close()
        if listener is not None:
            await listener.begin_close()
        if self._action_router is not None:
            self._action_router.begin_close()
        if self._orchestration_consumer is not None:
            await self._orchestration_consumer.begin_close()

    async def stop(self) -> None:
        bot = self._bot
        listener = self._mention_listener
        ai_group = self._ai_group
        web_group = self._web_group
        router = self._action_router
        admission = self._admission
        remote_consent = self._remote_consent
        state_repository = self._state_repository
        session_owner = self._session_owner
        orchestration_consumer = self._orchestration_consumer
        deployment_current_truth_current = self._deployment_current_truth_current
        pending_error: BaseException | None = None

        # quiesce_all()を経由しない個別disableでも、先に全入口を閉じる。
        try:
            await self.begin_close()
        except BaseException as exc:
            pending_error = exc
        if bot is not None:
            if listener is not None:
                try:
                    bot.remove_listener(listener.on_message, "on_message")
                except Exception as exc:
                    pending_error = exc
            try:
                withdraw_runtime_readiness(
                    bot,
                    (
                        "cap-can-0161",
                        "cap-run-ai-mention-chat",
                        WEB_SEARCH_CAPABILITY_ID,
                        AI_ATTACHMENT_UNDERSTANDING_CAPABILITY_ID,
                        MEMORY_CONTEXT_RECALL_CAPABILITY_ID,
                    ),
                )
            except Exception as exc:
                pending_error = pending_error or exc
            try:
                bot.tree.remove_command("ai")
            except Exception as exc:
                pending_error = pending_error or exc
            if web_group is not None:
                try:
                    get_command = getattr(bot.tree, "get_command", None)
                    current_web = get_command("web") if callable(get_command) else web_group
                    if current_web is web_group:
                        bot.tree.remove_command("web")
                except Exception as exc:
                    pending_error = pending_error or exc

        if admission is not None:
            try:
                drain_timeout = float(
                    getattr(getattr(bot, "settings", None), "ai_admission_drain_timeout_seconds", 5.0)
                )
                drained = await admission.drain(timeout_seconds=drain_timeout)
                if not drained:
                    stats = admission.stats()
                    logger.warning(
                        "ai_admission_drain_timeout",
                        extra={"active_count": stats.active, "waiting_count": stats.waiting},
                    )
                    cancelled = (
                        await listener.cancel_active(timeout_seconds=min(1.0, max(0.1, drain_timeout)))
                        if listener is not None
                        else False
                    )
                    slash_cancelled = (
                        await ai_group.cancel_active_runs(timeout_seconds=min(1.0, max(0.1, drain_timeout)))
                        if ai_group is not None
                        else True
                    )
                    post_cancel_drained = await admission.drain(timeout_seconds=0.1)
                    if not cancelled or not slash_cancelled or not post_cancel_drained:
                        logger.warning(
                            "ai_admission_cancel_timeout",
                            extra={
                                "active_count": admission.stats().active,
                                "listener_task_count": listener.active_task_count if listener is not None else 0,
                            },
                        )
            except BaseException as exc:
                pending_error = exc if not isinstance(exc, Exception) else pending_error or exc

        if orchestration_consumer is not None:
            try:
                await orchestration_consumer.stop()
            except BaseException as exc:
                pending_error = exc if not isinstance(exc, Exception) else pending_error or exc

        try:
            if session_owner is not None:
                await session_owner.close()
        except BaseException as exc:
            pending_error = exc if not isinstance(exc, Exception) else pending_error or exc
        finally:
            if bot is not None and getattr(bot, "ai_service", None) is self.service:
                delattr(bot, "ai_service")
            if (
                bot is not None
                and hasattr(bot, "ai_execution_gateway")
                and getattr(bot, "ai_execution_gateway") is self._execution_gateway
            ):
                delattr(bot, "ai_execution_gateway")
            if (
                bot is not None
                and self._execution_profile_selection is not None
                and getattr(bot, "ai_execution_profile_selection", None) is self._execution_profile_selection
            ):
                delattr(bot, "ai_execution_profile_selection")
            if (
                bot is not None
                and self._deployment_current_truth is not None
                and getattr(bot, "deployment_current_truth", None) is self._deployment_current_truth
            ):
                delattr(bot, "deployment_current_truth")
            if (
                bot is not None
                and deployment_current_truth_current is not None
                and getattr(bot, "deployment_current_truth_current", None) is deployment_current_truth_current
            ):
                delattr(bot, "deployment_current_truth_current")
            if bot is not None and getattr(bot, "ai_conversation_store", None) is self.conversation_store:
                delattr(bot, "ai_conversation_store")
            if bot is not None and router is not None and getattr(bot, "ai_action_router", None) is router:
                delattr(bot, "ai_action_router")
            if (
                bot is not None
                and self._orchestration_engine is not None
                and getattr(bot, "ai_orchestration_engine", None) is self._orchestration_engine
            ):
                delattr(bot, "ai_orchestration_engine")
            if (
                bot is not None
                and self._orchestration_planner is not None
                and getattr(bot, "ai_orchestration_planner", None) is self._orchestration_planner
            ):
                delattr(bot, "ai_orchestration_planner")
            if (
                bot is not None
                and self._orchestration_planner_port is not None
                and getattr(bot, "ai_orchestration_planner_port", None) is self._orchestration_planner_port
            ):
                delattr(bot, "ai_orchestration_planner_port")
            if bot is not None and hasattr(bot, "ai_orchestration_planner_ready"):
                delattr(bot, "ai_orchestration_planner_ready")
            if bot is not None and admission is not None and getattr(bot, "ai_admission", None) is admission:
                delattr(bot, "ai_admission")
            if (
                bot is not None
                and remote_consent is not None
                and getattr(bot, "ai_remote_consent_store", None) is remote_consent
            ):
                delattr(bot, "ai_remote_consent_store")
            if bot is not None and hasattr(bot, "ai_provider_is_local"):
                delattr(bot, "ai_provider_is_local")
            if bot is not None and hasattr(bot, "ai_web_search_available"):
                delattr(bot, "ai_web_search_available")
            if (
                bot is not None
                and self._search_gateway is not None
                and getattr(bot, "ai_search_gateway", None) is self._search_gateway
            ):
                delattr(bot, "ai_search_gateway")
            if bot is not None and hasattr(bot, "ai_attachments_available"):
                delattr(bot, "ai_attachments_available")
            if bot is not None and getattr(bot, "ai_display_preferences", None) is self._display_preferences:
                delattr(bot, "ai_display_preferences")
            if (
                bot is not None
                and self._v0_commands is not None
                and getattr(bot, "v0_ai_command_service", None) is self._v0_commands
            ):
                delattr(bot, "v0_ai_command_service")
            self._session_owner = None
            self.service = None
            self._bot = None
            self._mention_listener = None
            self._ai_group = None
            self._web_group = None
            self._action_router = None
            self._orchestration_engine = None
            self._orchestration_runtime = None
            self._orchestration_consumer = None
            self._orchestration_planner = None
            self._orchestration_planner_port = None
            self._admission = None
            self._remote_consent = None
            self.conversation_store = None
            self._state_repository = None
            self._display_preferences = None
            self._explicit_memory_repository = None
            self._v0_commands = None
            self._execution_gateway = None
            self._execution_profile_selection = None
            self._deployment_current_truth = None
            self._deployment_current_truth_current = None
            self._closing = True
            self._active_agent_audit_port = None
            self._agent_audit_port = None
            self._audit_database = None
            if self._search_gateway_owned:
                self._search_gateway = self._injected_search_gateway
                self._search_gateway_owned = False
            self._provider_selector = None
            self._provider_readiness = None
            self._context_builder = None
            self._active_core_files_read_port = None
            self._core_artifact_delivery = None
            if state_repository is not None:
                try:
                    state_repository.close()
                except Exception as exc:
                    pending_error = pending_error or exc

        if pending_error is not None:
            raise pending_error

    def _validate_execution_profile_injections(
        self,
        selection: RuntimeExecutionProfileSelection,
        *,
        settings: Any,
        runtime_direct_core_gateway_factory: Callable[[AIService], ExecutionGateway] | None = None,
    ) -> tuple[PackagingDependencyClass, ...]:
        topology = selection.topology
        if (
            topology is ExecutionTopology.DIRECT_CORE
            and self._direct_core_gateway_factory is None
            and runtime_direct_core_gateway_factory is None
        ):
            raise ExecutionProfileError("direct Core topology requires an injected gateway")
        if topology is ExecutionTopology.DISCORD_PROCESSING and self._discord_processing_gateway_factory is None:
            raise ExecutionProfileError("Discord processing topology requires an injected gateway")
        if topology is ExecutionTopology.HYBRID and (
            self._hybrid_core_gateway_factory is None or self._hybrid_selector is None
        ):
            raise ExecutionProfileError("hybrid topology requires an injected Core gateway and selector")
        return self._selected_gateway_dependency_classes(
            selection,
            settings=settings,
            runtime_direct_core_gateway_factory=runtime_direct_core_gateway_factory,
        )

    def _selected_gateway_dependency_classes(
        self,
        selection: RuntimeExecutionProfileSelection,
        *,
        settings: Any,
        runtime_direct_core_gateway_factory: Callable[[AIService], ExecutionGateway] | None = None,
    ) -> tuple[PackagingDependencyClass, ...]:
        contract = profile_contract(
            selection.topology,
            selection.hosting_profile,
            selection.packaging,
        )
        topology = selection.topology
        groups: tuple[tuple[PackagingDependencyClass, ...], ...]
        if topology is ExecutionTopology.LOCAL_STANDALONE:
            groups = (
                self._required_factory_dependency_classes(
                    factory=self._execution_gateway_factory,
                    dependencies=self._execution_gateway_dependency_classes,
                    default=_LOCAL_GATEWAY_DEPENDENCIES,
                    label="local",
                ),
            )
        elif topology is ExecutionTopology.DIRECT_CORE:
            groups = (
                self._required_factory_dependency_classes(
                    factory=self._direct_core_gateway_factory,
                    dependencies=self._direct_core_gateway_dependency_classes,
                    default=(
                        _DIRECT_CORE_RUNTIME_DEPENDENCIES if runtime_direct_core_gateway_factory is not None else None
                    ),
                    label="direct Core",
                ),
            )
        elif topology is ExecutionTopology.DISCORD_PROCESSING:
            groups = (
                self._required_factory_dependency_classes(
                    factory=self._discord_processing_gateway_factory,
                    dependencies=self._discord_processing_gateway_dependency_classes,
                    default=None,
                    label="Discord processing",
                ),
            )
        else:
            groups = (
                self._required_factory_dependency_classes(
                    factory=self._execution_gateway_factory,
                    dependencies=self._execution_gateway_dependency_classes,
                    default=_LOCAL_GATEWAY_DEPENDENCIES,
                    label="local",
                ),
                self._required_factory_dependency_classes(
                    factory=self._hybrid_core_gateway_factory,
                    dependencies=self._hybrid_core_gateway_dependency_classes,
                    default=None,
                    label="hybrid Core",
                ),
            )
        provider_dependencies = self._configured_local_provider_dependency_classes(
            selection,
            settings=settings,
        )
        if provider_dependencies:
            groups += (provider_dependencies,)

        merged: list[PackagingDependencyClass] = []
        for dependencies in groups:
            validate_profile_dependencies(contract, dependencies)
            for dependency in dependencies:
                if dependency not in merged:
                    merged.append(dependency)
        declared = tuple(merged)
        validate_profile_dependencies(contract, declared)
        return declared

    @staticmethod
    def _configured_local_provider_dependency_classes(
        selection: RuntimeExecutionProfileSelection,
        *,
        settings: Any,
    ) -> tuple[PackagingDependencyClass, ...]:
        if selection.topology not in {
            ExecutionTopology.LOCAL_STANDALONE,
            ExecutionTopology.HYBRID,
        }:
            return ()
        endpoint = getattr(settings, "ai_base_url", "")
        if not isinstance(endpoint, str) or not endpoint:
            return ()
        try:
            endpoint_is_local = ai_provider_endpoint_is_local(endpoint)
        except ProviderConfigurationError:
            # Provider constructor keeps the existing unavailable-with-warning path.
            return ()
        if endpoint_is_local:
            return (PackagingDependencyClass.HOST_LOCAL_RESOURCE,)
        remote_enabled = getattr(settings, "ai_allow_remote", False) is True
        api_key = getattr(settings, "openai_api_key", "")
        if remote_enabled and isinstance(api_key, str) and bool(api_key):
            return (
                PackagingDependencyClass.OFFICIAL_SECRET,
                PackagingDependencyClass.PRIVATE_ENDPOINT,
            )
        return ()

    @staticmethod
    def _required_factory_dependency_classes(
        *,
        factory: Callable[[AIService], ExecutionGateway] | None,
        dependencies: tuple[PackagingDependencyClass, ...] | None,
        default: tuple[PackagingDependencyClass, ...] | None,
        label: str,
    ) -> tuple[PackagingDependencyClass, ...]:
        if factory is None:
            if default is None:
                raise ExecutionProfileError(f"{label} gateway dependency evidence is unavailable")
            return default
        if dependencies is None:
            raise ExecutionProfileError(f"{label} gateway factory requires dependency evidence")
        return dependencies

    def _compose_execution_gateway(
        self,
        service: AIService,
        selection: RuntimeExecutionProfileSelection,
        *,
        dependencies: tuple[PackagingDependencyClass, ...],
        runtime_direct_core_gateway_factory: Callable[[AIService], ExecutionGateway] | None = None,
    ) -> ExecutionGateway:
        local_factory = self._execution_gateway_factory or LocalExecutionGateway.from_ai_service
        topology = selection.topology
        if topology is ExecutionTopology.LOCAL_STANDALONE:
            return compose_profiled_execution_gateway(
                topology,
                selection.hosting_profile,
                selection.packaging,
                dependencies=dependencies,
                local=local_factory(service),
            )
        if topology is ExecutionTopology.DIRECT_CORE:
            factory = self._direct_core_gateway_factory or runtime_direct_core_gateway_factory
            if factory is None:  # pragma: no cover - start preflight owns this boundary
                raise ExecutionProfileError("direct Core topology requires an injected gateway")
            return compose_profiled_execution_gateway(
                topology,
                selection.hosting_profile,
                selection.packaging,
                dependencies=dependencies,
                direct_core=factory(service),
            )
        if topology is ExecutionTopology.DISCORD_PROCESSING:
            factory = self._discord_processing_gateway_factory
            if factory is None:  # pragma: no cover - start preflight owns this boundary
                raise ExecutionProfileError("Discord processing topology requires an injected gateway")
            return compose_profiled_execution_gateway(
                topology,
                selection.hosting_profile,
                selection.packaging,
                dependencies=dependencies,
                discord_processing=factory(service),
            )
        factory = self._hybrid_core_gateway_factory
        if factory is None or self._hybrid_selector is None:  # pragma: no cover - start preflight owns this boundary
            raise ExecutionProfileError("hybrid topology requires an injected Core gateway and selector")
        return compose_profiled_execution_gateway(
            topology,
            selection.hosting_profile,
            selection.packaging,
            dependencies=dependencies,
            local=local_factory(service),
            hybrid_core=factory(service),
            hybrid_selector=self._hybrid_selector,
        )

    def _build_deployment_current_truth(
        self,
        selection: RuntimeExecutionProfileSelection,
        *,
        bot: Any,
    ) -> M10CurrentTruthV1:
        from yonerai_discord.deployment_current_truth import M10SourceTruthV1, build_m10_current_truth

        topology = selection.topology
        available_ports = {
            ExecutionTopology.LOCAL_STANDALONE: ("local",),
            ExecutionTopology.DIRECT_CORE: ("direct_core",),
            ExecutionTopology.DISCORD_PROCESSING: ("discord_processing",),
            ExecutionTopology.HYBRID: ("hybrid_core", "hybrid_selector", "local"),
        }[topology]
        provider_declared = topology is not ExecutionTopology.LOCAL_STANDALONE or bool(
            self.service is not None and self.service.available
        )
        provider_identity_current = (
            self._bot is bot
            and self._execution_profile_selection is selection
            and self.service is not None
            and getattr(bot, "ai_service", None) is self.service
            and self._execution_gateway is not None
            and getattr(bot, "ai_execution_gateway", None) is self._execution_gateway
            and getattr(bot, "ai_execution_profile_selection", None) is selection
        )
        provider_configured = provider_declared and provider_identity_current
        provider_source = M10SourceTruthV1(
            configured=provider_configured,
            ready=False,
            live_success=None,
            blocker=(
                "provider_live_readiness_unverified"
                if provider_configured
                else ("provider_source_identity_changed" if provider_declared else "provider_source_not_configured")
            ),
        )
        durable_enabled = getattr(getattr(bot, "settings", None), "ai_orchestration_durable_enabled", False) is True
        if not durable_enabled:
            jobs_source = M10SourceTruthV1(
                configured=False,
                ready=False,
                live_success=None,
                blocker="durable_orchestration_not_configured",
            )
        else:
            runtime = self._orchestration_runtime
            consumer = self._orchestration_consumer
            jobs_ready = (
                isinstance(runtime, DurableOrchestrationRuntime)
                and isinstance(consumer, DurableOrchestrationConsumer)
                and consumer.runtime is runtime
                and consumer.ready is True
                and runtime.ready is True
                and getattr(bot, "ai_orchestration_runtime", None) is runtime
                and getattr(bot, "ai_orchestration_engine", None) is runtime.engine
                and getattr(bot, "ai_orchestration_repository", None) is runtime.repository
            )
            jobs_source = M10SourceTruthV1(
                configured=True,
                ready=jobs_ready,
                live_success=None,
                blocker=None if jobs_ready else "durable_orchestration_consumer_not_ready",
            )

        database = self._audit_database
        audit_configured = isinstance(database, Database)
        audit_identity_current = audit_configured and getattr(bot, "database", None) is database
        audit_ready = (
            audit_identity_current
            and database.is_open is True
            and not bool(getattr(bot, "is_closing", False))
            and callable(getattr(database, "append_audit", None))
            and callable(getattr(database, "list_audit", None))
            and callable(getattr(database, "list_guild_audit_summary", None))
        )
        audit_source = M10SourceTruthV1(
            configured=audit_configured,
            ready=audit_ready,
            live_success=None,
            blocker=(
                None
                if audit_ready
                else (
                    "audit_source_identity_changed"
                    if audit_configured and not audit_identity_current
                    else ("audit_source_not_ready" if audit_configured else "audit_source_not_configured")
                )
            ),
        )
        return build_m10_current_truth(
            selected_topology=selection.topology if selection.explicit else None,
            selected_hosting_profile=selection.hosting_profile if selection.explicit else None,
            selected_packaging=selection.packaging if selection.explicit else None,
            available_ports=available_ports,
            provider_source=provider_source,
            jobs_source=jobs_source,
            audit_source=audit_source,
        )


def _bounded_capability_snapshot(bot: Any) -> StaticCapabilitySnapshot:
    registry = getattr(bot, "capability_registry", None)
    if registry is None:
        return EMPTY_CAPABILITY_SNAPSHOT
    try:
        return build_static_capability_snapshot(registry)
    except Exception:
        # A missing or changing registry cannot widen the model-tool surface.
        return EMPTY_CAPABILITY_SNAPSHOT


def _memory_context_recall_allowed(
    bot: Any,
    subject: Any,
    *,
    repository: V0ExplicitMemoryRepository | None,
) -> bool:
    """previewとは独立したambient memory capabilityをguild/DMで再評価する。"""

    if repository is None or bool(getattr(bot, "is_closing", False)):
        return False
    service = getattr(bot, "personal_memory_service", None)
    if service is None:
        return False
    user = getattr(subject, "user", None) or getattr(subject, "author", None)
    guild = getattr(subject, "guild", None)
    guild_id = getattr(subject, "guild_id", None)
    if guild_id is None and guild is not None:
        guild_id = getattr(guild, "id", None)
    user_id = getattr(user, "id", None)
    if (
        (guild_id is not None and (not isinstance(guild_id, int) or guild_id <= 0))
        or not isinstance(user_id, int)
        or user_id <= 0
        or bool(getattr(user, "bot", False))
    ):
        return False
    if guild_id is None and not bool(getattr(getattr(bot, "settings", None), "ai_dm_enabled", False)):
        return False
    if guild_id is not None:
        enabled = getattr(service, "is_enabled", None)
        if not callable(enabled):
            return False
        try:
            if not bool(enabled(guild_id, user_id)):
                return False
        except Exception:
            return False
    guard = getattr(bot, "capability_guard", None)
    currently_allowed = getattr(guard, "currently_allowed", None)
    if not callable(currently_allowed):
        return False
    try:
        return bool(
            currently_allowed(
                MEMORY_CONTEXT_RECALL_CAPABILITY_ID,
                guild_id=guild_id,
                user_id=user_id,
                actor_level=_subject_actor_level(bot, subject, user=user, guild=guild),
                floor=RbacLevel.EVERYONE,
            )
        )
    except Exception:
        return False


def _subject_actor_level(bot: Any, subject: Any, *, user: Any, guild: Any) -> RbacLevel:
    settings = getattr(bot, "settings", None)
    if settings is None:
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


def setup(registry: Any) -> None:
    register = getattr(registry, "register_plugin", None) or getattr(registry, "register", None)
    if register is None:
        raise TypeError("registry must provide register_plugin() or register()")
    register("ai", AIPlugin)


__all__ = [
    "AISource",
    "AIReply",
    "AIAdmissionController",
    "AIRequest",
    "AIService",
    "AIUnavailableError",
    "NaturalActionRouter",
    "ConversationStore",
    "ConversationStoreStats",
    "CoreArtifactDeliveryPreparer",
    "DataBoundary",
    "OpenAICompatibleProvider",
    "OrchestrationEngine",
    "OrchestrationPlanner",
    "PlannerDispatchContext",
    "AIServicePlannerPort",
    "PrivacyBoundaryError",
    "ProviderAuthorizationError",
    "ProviderConfigurationError",
    "PROVIDER_METADATA_ALLOWED_KEYS",
    "RemoteConsentStore",
    "AIStateRepository",
    "DisplayMode",
    "DisplayPreferenceStore",
    "DiscordCoreSurfaceGateway",
    "ExecutionProfileError",
    "ExecutionTopology",
    "HostingProfile",
    "HybridExecutionGateway",
    "PackagingDependencyClass",
    "PackagingCandidate",
    "RuntimeExecutionProfileSelection",
    "compose_execution_gateway",
    "compose_profiled_execution_gateway",
    "resolve_runtime_execution_profile",
    "with_discord_core_facts",
    "setup",
]
