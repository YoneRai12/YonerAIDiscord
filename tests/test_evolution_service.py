from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from yonerai_discord.capability_metadata_contract import capability_metadata_content_revision
from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.db import Database
from yonerai_discord.modules.ai import AIReply, AIRequest, AIService, ProviderAuthorizationError
from yonerai_discord.modules.ai.bounded_tools import (
    StaticCapabilityMetadata,
    StaticCapabilitySnapshot,
)
from yonerai_discord.modules.ai.models import TaskModelRequirement
from yonerai_discord.modules.ai.ports import _verify_service_sink
from yonerai_discord.modules.evolution import EvolutionPlugin
from yonerai_discord.modules.evolution.service import (
    EvolutionArtifactError,
    EvolutionDisabledError,
    EvolutionService,
    EvolutionServiceError,
)


class FakeSolProvider:
    def __init__(self, *, local: bool, model: str = "gpt-5.6-sol") -> None:
        self._local = local
        self.model = model
        self.requests: list[AIRequest] = []

    @property
    def is_local(self) -> bool:
        return self._local

    @property
    def runtime_model_bindings(self) -> dict[str, str]:
        return {
            "ai.fast": self.model,
            "ai.balanced": self.model,
            "ai.quality": self.model,
        }

    async def complete(self, request: AIRequest) -> AIReply:
        self.requests.append(request)
        return AIReply(
            text="安全性、test、rollback、完了条件を含む審査用計画",
            model=self.model,
            provider="fake",
        )

    async def complete_authorized(
        self,
        request: AIRequest,
        provider_sink_verifier: object,
    ) -> AIReply:
        if not _verify_service_sink(
            provider_sink_verifier,
            request=request,
            provider=self,
        ):
            raise ProviderAuthorizationError("authorization changed")
        return await self.complete(request)


class BlockingSolProvider(FakeSolProvider):
    def __init__(self, *, local: bool = True) -> None:
        super().__init__(local=local)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: AIRequest) -> AIReply:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return AIReply(
            text="safe review plan",
            model=self.model,
            provider="fake",
        )

    async def complete_authorized(
        self,
        request: AIRequest,
        provider_sink_verifier: object,
    ) -> AIReply:
        self.started.set()
        await self.release.wait()
        if not _verify_service_sink(
            provider_sink_verifier,
            request=request,
            provider=self,
        ):
            raise ProviderAuthorizationError("authorization changed at evolution sink")
        self.requests.append(request)
        return AIReply(
            text="safe review plan",
            model=self.model,
            provider="fake",
        )


def _evolution_capability_snapshot() -> StaticCapabilitySnapshot:
    content = {
        "bindings": [],
        "capability_id": "cap-run-evolution-propose",
        "intent_tags": ["self_evolution"],
        "minimum_rbac": "bot_owner",
        "module_id": "intelligence.self-evolution",
        "name": "Evolution propose",
        "primary_intent": "self_evolution",
        "risk": "high",
        "source_provenance": "runtime_manifest",
        "surface_bindings": ["command:evolution.propose"],
    }
    return StaticCapabilitySnapshot(
        (
            StaticCapabilityMetadata(
                capability_id=content["capability_id"],
                module_id=content["module_id"],
                name=content["name"],
                primary_intent="self_evolution",
                intent_tags=("self_evolution",),
                risk="high",
                minimum_rbac="bot_owner",
                source_provenance="runtime_manifest",
                content_revision=capability_metadata_content_revision(content),
                surface_bindings=("command:evolution.propose",),
            ),
        )
    )


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "suite.sqlite3")
    value.open()
    value.migrate()
    try:
        yield value
    finally:
        value.close()


@pytest.mark.asyncio
async def test_plugin_current_policy_rechecks_exact_owner_and_rbac(
    database: Database,
    tmp_path: Path,
) -> None:
    class RecordingGuard:
        def __init__(self) -> None:
            self.allowed = True
            self.calls: list[tuple[str, int, int, RbacLevel, RbacLevel]] = []

        def currently_allowed(
            self,
            capability_id: str,
            *,
            guild_id: int,
            user_id: int,
            actor_level: RbacLevel,
            floor: RbacLevel,
        ) -> bool:
            self.calls.append((capability_id, guild_id, user_id, actor_level, floor))
            return self.allowed

    class Tree:
        def add_command(self, _command: object) -> None:
            return None

        def remove_command(self, _name: str) -> None:
            return None

    guard = RecordingGuard()
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            database_path=tmp_path / "suite.sqlite3",
            self_evolution_enabled=True,
            bot_owner_ids=(10,),
            ai_model_quality="gpt-5.6-sol",
        ),
        database=database,
        ai_service=None,
        capability_guard=guard,
        tree=Tree(),
        owner_id=None,
        owner_ids=None,
    )
    plugin = EvolutionPlugin()
    await plugin.start(bot)
    try:
        assert plugin.service is not None
        created = await plugin.service.propose(
            actor_id=10,
            guild_id=20,
            title="owner reauthorization",
            rationale="the production callback must include current actor RBAC",
            target_paths=("src/example.py",),
            generate_with_ai=False,
            allow_remote=False,
        )
        assert guard.calls
        assert all(
            call
            == (
                "cap-run-evolution-propose",
                20,
                10,
                RbacLevel.BOT_OWNER,
                RbacLevel.BOT_OWNER,
            )
            for call in guard.calls
        )

        calls_before_non_owner = len(guard.calls)
        with pytest.raises(EvolutionDisabledError):
            plugin.service.begin_review(
                created.record.proposal_id,
                actor_id=99,
                guild_id=20,
                note="not an owner",
            )
        assert len(guard.calls) == calls_before_non_owner

        guard.allowed = False
        with pytest.raises(EvolutionDisabledError):
            plugin.service.begin_review(
                created.record.proposal_id,
                actor_id=10,
                guild_id=20,
                note="capability disabled",
            )
        assert guard.calls[-1] == (
            "cap-run-evolution-review",
            20,
            10,
            RbacLevel.BOT_OWNER,
            RbacLevel.BOT_OWNER,
        )
        persisted = database.get_evolution_proposal(created.record.proposal_id)
        assert persisted is not None
        assert persisted.status == "proposed"
    finally:
        await plugin.stop()


@pytest.mark.asyncio
async def test_disabled_evolution_cannot_create_artifacts(database: Database, tmp_path: Path) -> None:
    service = EvolutionService(
        database,
        tmp_path / "proposals",
        ai_service=None,
        enabled=False,
    )

    with pytest.raises(EvolutionDisabledError):
        await service.propose(
            actor_id=1,
            guild_id=2,
            title="改善",
            rationale="安全に改善する",
            target_paths=("src/example.py",),
            generate_with_ai=False,
            allow_remote=False,
        )

    assert not (tmp_path / "proposals").exists()


@pytest.mark.asyncio
async def test_proposal_lifecycle_is_review_only_and_integrity_checked(
    database: Database,
    tmp_path: Path,
) -> None:
    service = EvolutionService(
        database,
        tmp_path / "proposals",
        ai_service=None,
        enabled=True,
    )
    created = await service.propose(
        actor_id=10,
        guild_id=20,
        title="診断を改善",
        rationale="秘密値を扱わず診断項目を増やす",
        target_paths=("src/diagnostics.py", "tests/test_diagnostics.py"),
        generate_with_ai=False,
        allow_remote=False,
    )

    assert created.artifact_path.is_file()
    assert created.record.status == "proposed"
    assert service.show(created.record.proposal_id).integrity_ok
    reviewed = service.begin_review(created.record.proposal_id, actor_id=11, guild_id=20, note="内容を確認")
    assert reviewed.status == "in_review"
    approved = service.approve(
        created.record.proposal_id,
        actor_id=11,
        guild_id=20,
        note="テスト後に手動適用",
    )
    assert approved.status == "approved"
    assert not hasattr(service, "apply")
    assert not hasattr(service, "merge")
    assert not hasattr(service, "push")
    assert not hasattr(service, "restart")


@pytest.mark.asyncio
async def test_tampered_artifact_cannot_be_approved(database: Database, tmp_path: Path) -> None:
    service = EvolutionService(database, tmp_path / "proposals", ai_service=None, enabled=True)
    created = await service.propose(
        actor_id=10,
        guild_id=20,
        title="安全な提案",
        rationale="artifact整合性を検証する",
        target_paths=("src/example.py",),
        generate_with_ai=False,
        allow_remote=False,
    )
    service.begin_review(created.record.proposal_id, actor_id=10, guild_id=20, note="review")
    created.artifact_path.write_text("tampered", encoding="utf-8")

    with pytest.raises(EvolutionArtifactError, match="integrity"):
        service.approve(created.record.proposal_id, actor_id=10, guild_id=20, note="approve")
    assert database.get_evolution_proposal(created.record.proposal_id).status == "in_review"  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["review", "reject"])
async def test_review_and_reject_recheck_exact_policy_before_database_write(
    database: Database,
    tmp_path: Path,
    operation: str,
) -> None:
    guarded_operation: str | None = None
    guarded_checks = 0
    observed: list[tuple[str, int, int]] = []

    def current_policy(current_operation: str, guild_id: int, actor_id: int) -> bool:
        nonlocal guarded_checks
        observed.append((current_operation, guild_id, actor_id))
        if current_operation != guarded_operation:
            return True
        guarded_checks += 1
        return guarded_checks == 1

    service = EvolutionService(
        database,
        tmp_path / "proposals",
        ai_service=None,
        enabled=True,
        current_policy=current_policy,
    )
    created = await service.propose(
        actor_id=10,
        guild_id=20,
        title="transition race",
        rationale="policy must be current at the database boundary",
        target_paths=("src/example.py",),
        generate_with_ai=False,
        allow_remote=False,
    )
    observed.clear()
    guarded_operation = operation

    with pytest.raises(EvolutionDisabledError):
        if operation == "review":
            service.begin_review(created.record.proposal_id, actor_id=99, guild_id=20, note="review")
        else:
            service.reject(created.record.proposal_id, actor_id=99, guild_id=20, note="reject")

    assert observed == [(operation, 20, 99), (operation, 20, 99)]
    persisted = database.get_evolution_proposal(created.record.proposal_id)
    assert persisted is not None
    assert persisted.status == "proposed"


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_approve_rechecks_after_artifact_verification_when_actor_loses_access_or_shutdown_starts(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shutdown: bool,
) -> None:
    permitted_approvers = {10}
    observed: list[tuple[str, int, int]] = []

    def current_policy(operation: str, guild_id: int, actor_id: int) -> bool:
        observed.append((operation, guild_id, actor_id))
        return operation != "approve" or actor_id in permitted_approvers

    service = EvolutionService(
        database,
        tmp_path / "proposals",
        ai_service=None,
        enabled=True,
        current_policy=current_policy,
    )
    created = await service.propose(
        actor_id=10,
        guild_id=20,
        title="approval race",
        rationale="integrity verification must not preserve stale authority",
        target_paths=("src/example.py",),
        generate_with_ai=False,
        allow_remote=False,
    )
    service.begin_review(created.record.proposal_id, actor_id=10, guild_id=20, note="review")
    original_show = service.show

    def show_then_revoke(proposal_id: str):
        verified = original_show(proposal_id)
        if shutdown:
            service.close()
        else:
            permitted_approvers.clear()
        return verified

    monkeypatch.setattr(service, "show", show_then_revoke)
    observed.clear()

    with pytest.raises(EvolutionDisabledError):
        service.approve(created.record.proposal_id, actor_id=10, guild_id=20, note="approve")

    expected_checks = 1 if shutdown else 2
    assert observed == [("approve", 20, 10)] * expected_checks
    persisted = database.get_evolution_proposal(created.record.proposal_id)
    assert persisted is not None
    assert persisted.status == "in_review"


@pytest.mark.asyncio
async def test_remote_sol_generation_uses_persistent_user_consent_not_per_request_flag(
    database: Database,
    tmp_path: Path,
) -> None:
    provider = FakeSolProvider(local=False)
    consent = {"active": False}
    service = EvolutionService(
        database,
        tmp_path / "proposals",
        ai_service=AIService(provider),
        enabled=True,
        provider_is_local=False,
        remote_consent_active=lambda actor_id: actor_id == 10 and consent["active"],
        model_requirement=TaskModelRequirement("ai.quality", "gpt-5.6-sol"),
    )
    arguments = {
        "actor_id": 10,
        "guild_id": 20,
        "title": "Sol proposal",
        "rationale": "審査可能な計画を作る",
        "target_paths": ("src/example.py",),
        "generate_with_ai": True,
    }

    with pytest.raises(EvolutionServiceError, match="persistent remote AI consent"):
        await service.propose(**arguments, allow_remote=True)
    assert provider.requests == []

    consent["active"] = True
    created = await service.propose(**arguments, allow_remote=False)

    assert created.model == "gpt-5.6-sol"
    assert provider.requests[-1].guild_id == 20
    assert provider.requests[-1].user_id == 10
    assert "gpt-5.6-sol" in created.artifact_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_local_quality_adapter_can_replace_the_physical_sol_model(
    database: Database,
    tmp_path: Path,
) -> None:
    provider = FakeSolProvider(local=True, model="gpt-5.6-terra")
    service = EvolutionService(
        database,
        tmp_path / "proposals",
        ai_service=AIService(provider),
        enabled=True,
    )

    created = await service.propose(
        actor_id=10,
        guild_id=20,
        title="local quality adapter",
        rationale="local quality bindingは物理modelを差し替え可能にする",
        target_paths=("src/example.py",),
        generate_with_ai=True,
        allow_remote=False,
    )
    assert created.model == "gpt-5.6-terra"
    assert provider.requests[0].required_model_alias == "ai.quality"
    assert provider.requests[0].required_model_id is None


def test_remote_evolution_composition_requires_a_physical_quality_binding(
    database: Database,
    tmp_path: Path,
) -> None:
    provider = FakeSolProvider(local=False)

    with pytest.raises(ValueError, match="configured quality model binding"):
        EvolutionService(
            database,
            tmp_path / "proposals",
            ai_service=AIService(provider),
            enabled=True,
            provider_is_local=False,
        )


@pytest.mark.asyncio
async def test_plugin_uses_configured_remote_quality_model_binding(
    database: Database,
    tmp_path: Path,
) -> None:
    class Guard:
        def currently_allowed(self, *_args, **_kwargs) -> bool:
            return True

    class Tree:
        def add_command(self, _command: object) -> None:
            return None

        def remove_command(self, _name: str) -> None:
            return None

    configured_model = "configured-remote-quality-model"
    provider = FakeSolProvider(local=False, model=configured_model)
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            database_path=tmp_path / "suite.sqlite3",
            self_evolution_enabled=True,
            bot_owner_ids=(10,),
            ai_model_quality=configured_model,
        ),
        database=database,
        ai_service=AIService(provider),
        ai_provider_is_local=False,
        ai_remote_consent_store=SimpleNamespace(active_user=lambda user_id: user_id == 10),
        capability_guard=Guard(),
        tree=Tree(),
        owner_id=None,
        owner_ids=None,
    )
    plugin = EvolutionPlugin()
    await plugin.start(bot)
    try:
        assert plugin.service is not None
        created = await plugin.service.propose(
            actor_id=10,
            guild_id=20,
            title="configured quality binding",
            rationale="composition rootは設定済みquality modelを実行要件へ渡す",
            target_paths=("src/example.py",),
            generate_with_ai=True,
            allow_remote=False,
        )
    finally:
        await plugin.stop()

    assert created.model == configured_model
    assert provider.requests[0].required_model_alias == "ai.quality"
    assert provider.requests[0].required_model_id == configured_model


@pytest.mark.asyncio
@pytest.mark.parametrize("live_revision_matches", [True, False])
async def test_plugin_wires_the_same_capability_snapshot_used_by_ai_service(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_revision_matches: bool,
) -> None:
    class Guard:
        def currently_allowed(self, *_args, **_kwargs) -> bool:
            return True

    class Tree:
        def add_command(self, _command: object) -> None:
            return None

        def remove_command(self, _name: str) -> None:
            return None

    snapshot = _evolution_capability_snapshot()
    live_revision = snapshot.content_revision if live_revision_matches else "f" * 64
    provider = FakeSolProvider(local=True)
    ai_service = AIService(
        provider,
        require_prepared_context=True,
        require_authorization=True,
        capability_catalog_revision=lambda: live_revision,
    )
    monkeypatch.setattr(
        "yonerai_discord.modules.evolution._bounded_capability_snapshot",
        lambda _bot: snapshot,
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            database_path=tmp_path / "suite.sqlite3",
            self_evolution_enabled=True,
            bot_owner_ids=(10,),
            ai_model_quality="unused-local-quality",
        ),
        database=database,
        ai_service=ai_service,
        ai_provider_is_local=True,
        ai_remote_consent_store=SimpleNamespace(active_user=lambda _user_id: False),
        capability_registry=object(),
        capability_guard=Guard(),
        tree=Tree(),
        owner_id=None,
        owner_ids=None,
    )
    plugin = EvolutionPlugin()
    await plugin.start(bot)
    try:
        assert plugin.service is not None
        if live_revision_matches:
            created = await plugin.service.propose(
                actor_id=10,
                guild_id=20,
                title="capability snapshot wiring",
                rationale="AI runtimeと同じ静的catalog revisionをproposal経路へ渡す",
                target_paths=("src/example.py",),
                generate_with_ai=True,
                allow_remote=False,
            )
            assert created.model == "gpt-5.6-sol"
            assert len(provider.requests) == 1
            assert provider.requests[0].bounded_toolset is not None
            assert provider.requests[0].bounded_toolset.capability_catalog_revision == snapshot.content_revision
        else:
            with pytest.raises(EvolutionServiceError, match="generation failed"):
                await plugin.service.propose(
                    actor_id=10,
                    guild_id=20,
                    title="stale capability snapshot",
                    rationale="catalog revision不一致をprovider前に拒否する",
                    target_paths=("src/example.py",),
                    generate_with_ai=True,
                    allow_remote=False,
                )
            assert provider.requests == []
    finally:
        await plugin.stop()


@pytest.mark.asyncio
async def test_remote_non_sol_binding_is_rejected_before_self_evolution_dispatch(
    database: Database,
    tmp_path: Path,
) -> None:
    provider = FakeSolProvider(local=False, model="gpt-5.6-terra")
    service = EvolutionService(
        database,
        tmp_path / "proposals",
        ai_service=AIService(provider),
        enabled=True,
        provider_is_local=False,
        remote_consent_active=lambda actor_id: actor_id == 10,
        model_requirement=TaskModelRequirement("ai.quality", "gpt-5.6-sol"),
    )

    with pytest.raises(EvolutionServiceError, match="required quality model"):
        await service.propose(
            actor_id=10,
            guild_id=20,
            title="wrong remote model",
            rationale="remoteはmanifest policyのSol bindingだけを許可する",
            target_paths=("src/example.py",),
            generate_with_ai=True,
            allow_remote=False,
        )
    assert provider.requests == []


@pytest.mark.asyncio
async def test_remote_consent_revoked_while_waiting_blocks_evolution_provider_sink(
    database: Database,
    tmp_path: Path,
) -> None:
    provider = BlockingSolProvider(local=False)
    consent = {"active": True}
    service = EvolutionService(
        database,
        tmp_path / "proposals",
        ai_service=AIService(provider),
        enabled=True,
        provider_is_local=False,
        remote_consent_active=lambda actor_id: actor_id == 10 and consent["active"],
        model_requirement=TaskModelRequirement("ai.quality", "gpt-5.6-sol"),
    )

    task = asyncio.create_task(
        service.propose(
            actor_id=10,
            guild_id=20,
            title="remote consent race",
            rationale="provider sink直前の永続同意を再検査する",
            target_paths=("src/example.py",),
            generate_with_ai=True,
            allow_remote=False,
        )
    )
    await provider.started.wait()
    consent["active"] = False
    provider.release.set()

    with pytest.raises(EvolutionServiceError, match="generation failed"):
        await task
    assert provider.requests == []
    assert database.list_evolution_proposals(limit=10) == ()
    assert not (tmp_path / "proposals").exists()


@pytest.mark.asyncio
async def test_generated_plan_with_secret_like_value_is_never_persisted(
    database: Database,
    tmp_path: Path,
) -> None:
    provider = FakeSolProvider(local=True)

    async def secret_plan(request: AIRequest):
        provider.requests.append(request)
        return AIReply(
            text="token sk-proj-" + "abcdefghijklmnopqrstuvwxyz012345",
            model="gpt-5.6-sol",
            provider="fake",
        )

    provider.complete = secret_plan  # type: ignore[method-assign]
    artifact_dir = tmp_path / "proposals"
    service = EvolutionService(
        database,
        artifact_dir,
        ai_service=AIService(provider),
        enabled=True,
    )

    with pytest.raises(ValueError, match="credentials"):
        await service.propose(
            actor_id=10,
            guild_id=20,
            title="provider output validation",
            rationale="generated text is untrusted",
            target_paths=("src/example.py",),
            generate_with_ai=True,
            allow_remote=False,
        )

    assert not artifact_dir.exists()
    assert database.list_evolution_proposals(limit=10) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_service", [False, True])
async def test_policy_or_stop_during_sol_generation_writes_no_artifact_or_database(
    database: Database,
    tmp_path: Path,
    close_service: bool,
) -> None:
    provider = BlockingSolProvider()
    allowed = True
    artifact_dir = tmp_path / "proposals"
    service = EvolutionService(
        database,
        artifact_dir,
        ai_service=AIService(provider),
        enabled=True,
        current_policy=lambda _operation, _guild_id, _actor_id: allowed,
    )
    task = asyncio.create_task(
        service.propose(
            actor_id=10,
            guild_id=20,
            title="blocked proposal",
            rationale="must stop before persistence",
            target_paths=("src/example.py",),
            generate_with_ai=True,
            allow_remote=False,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    if close_service:
        service.close()
    else:
        allowed = False
    provider.release.set()

    with pytest.raises(EvolutionDisabledError):
        await task
    assert provider.requests == []
    assert not artifact_dir.exists()
    assert database.list_evolution_proposals(limit=10) == ()


@pytest.mark.asyncio
async def test_policy_off_after_artifact_stage_removes_partial_file_and_writes_no_database(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = True
    artifact_dir = tmp_path / "proposals"
    service = EvolutionService(
        database,
        artifact_dir,
        ai_service=None,
        enabled=True,
        current_policy=lambda _operation, _guild_id, _actor_id: allowed,
    )
    original_write = service._write_artifact

    def write_then_disable(proposal_id: str, content: str, **kwargs) -> Path:
        nonlocal allowed
        path = original_write(proposal_id, content, **kwargs)
        allowed = False
        return path

    monkeypatch.setattr(service, "_write_artifact", write_then_disable)

    with pytest.raises(EvolutionDisabledError):
        await service.propose(
            actor_id=10,
            guild_id=20,
            title="policy changes after artifact stage",
            rationale="database commit must not start",
            target_paths=("src/example.py",),
            generate_with_ai=False,
            allow_remote=False,
        )

    assert tuple(artifact_dir.glob("*")) == ()
    assert database.list_evolution_proposals(limit=10) == ()


@pytest.mark.asyncio
async def test_policy_is_rechecked_immediately_before_atomic_artifact_replace(
    database: Database,
    tmp_path: Path,
) -> None:
    checks = 0

    def current_policy(_operation: str, _guild_id: int, _actor_id: int) -> bool:
        nonlocal checks
        checks += 1
        return checks < 3

    artifact_dir = tmp_path / "proposals"
    service = EvolutionService(
        database,
        artifact_dir,
        ai_service=None,
        enabled=True,
        current_policy=current_policy,
    )

    with pytest.raises(EvolutionDisabledError):
        await service.propose(
            actor_id=10,
            guild_id=20,
            title="policy changes before atomic replace",
            rationale="temporary file must be removed",
            target_paths=("src/example.py",),
            generate_with_ai=False,
            allow_remote=False,
        )

    assert checks == 3
    assert tuple(artifact_dir.glob("*")) == ()
    assert database.list_evolution_proposals(limit=10) == ()


@pytest.mark.asyncio
async def test_database_commit_then_transport_error_returns_committed_receipt(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EvolutionService(database, tmp_path / "proposals", ai_service=None, enabled=True)
    original_create = database.create_evolution_proposal

    def commit_then_raise(*args, **kwargs):
        original_create(*args, **kwargs)
        raise RuntimeError("simulated post-commit transport failure")

    monkeypatch.setattr(database, "create_evolution_proposal", commit_then_raise)

    created = await service.propose(
        actor_id=10,
        guild_id=20,
        title="committed proposal",
        rationale="receipt must reflect the durable database state",
        target_paths=("src/example.py",),
        generate_with_ai=False,
        allow_remote=False,
    )

    assert created.artifact_path.is_file()
    persisted = database.get_evolution_proposal(created.record.proposal_id)
    assert persisted is not None
    assert persisted.proposal_hash == created.record.proposal_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "rationale"),
    [
        (".env", "安全な理由"),
        ("src/roles.py", "安全な理由"),
        ("src/example.py", "token sk-proj-" + "abcdefghijklmnopqrstuvwxyz012345"),
        ("../outside.py", "安全な理由"),
    ],
)
async def test_secret_rbac_and_escape_targets_are_rejected(
    database: Database,
    tmp_path: Path,
    target: str,
    rationale: str,
) -> None:
    service = EvolutionService(database, tmp_path / "proposals", ai_service=None, enabled=True)

    with pytest.raises(ValueError):
        await service.propose(
            actor_id=10,
            guild_id=20,
            title="拒否対象",
            rationale=rationale,
            target_paths=(target,),
            generate_with_ai=False,
            allow_remote=False,
        )
