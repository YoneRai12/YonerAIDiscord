from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from yonerai_discord.ai_control import RiskLevel, TaskComplexity, TaskKind
from yonerai_discord.db import Database, EvolutionProposalRecord
from yonerai_discord.modules.ai.models import (
    AIRequest,
    DataBoundary,
    TaskModelRequirement,
    provider_facing_envelope_digest,
)
from yonerai_discord.modules.ai.bounded_tools import (
    EMPTY_CAPABILITY_SNAPSHOT,
    StaticCapabilitySnapshot,
    BoundedToolSet,
    ToolScopeBinding,
    capability_metadata_transport,
)
from yonerai_discord.modules.ai.service import (
    AIService,
    AIUnavailableError,
    PrivacyBoundaryError,
    ProviderSelectionError,
)
from yonerai_discord.v0_contracts import (
    FORMAL_PROVIDER_INPUT_DIRECTIVE,
    ContextBuildInput,
    MemoryVisibility,
    Scope,
)
from yonerai_discord.v0_runtime.context_builder import RuntimeContextBuilder


class EvolutionServiceError(RuntimeError):
    pass


class EvolutionDisabledError(EvolutionServiceError):
    pass


class EvolutionArtifactError(EvolutionServiceError):
    pass


_SECRET_PATTERNS = (
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b[MNO][A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_PROTECTED_STEMS = frozenset(
    {"secret", "secrets", "credential", "credentials", "rbac", "role", "roles", "permission", "permissions"}
)


@dataclass(frozen=True, slots=True)
class CreatedProposal:
    record: EvolutionProposalRecord
    artifact_path: Path
    plan: str
    model: str | None


@dataclass(frozen=True, slots=True)
class VerifiedProposal:
    record: EvolutionProposalRecord
    artifact_path: Path
    content: str
    integrity_ok: bool


class EvolutionService:
    """quality modelのproposalを永続化するが、source変更・適用・push APIは持たない。"""

    def __init__(
        self,
        database: Database,
        artifact_dir: Path,
        *,
        ai_service: AIService | None,
        enabled: bool,
        current_policy: Callable[[str, int, int], bool] | None = None,
        context_builder: RuntimeContextBuilder | None = None,
        provider_is_local: bool | None = None,
        remote_consent_active: Callable[[int], bool] | None = None,
        model_requirement: TaskModelRequirement | None = None,
        capability_snapshot: StaticCapabilitySnapshot = EMPTY_CAPABILITY_SNAPSHOT,
        provider_catalog_revision: str | None = None,
        tool_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.artifact_dir = artifact_dir
        self.ai_service = ai_service
        self.enabled = enabled
        self._current_policy = current_policy or (lambda _operation, _guild_id, _actor_id: True)
        self._context_builder = context_builder or RuntimeContextBuilder()
        known_locality = (
            provider_is_local if provider_is_local is not None else getattr(ai_service, "provider_locality", None)
        )
        self._provider_is_local = known_locality is True
        self._remote_consent_active = remote_consent_active or (lambda _actor_id: False)
        self._capability_snapshot = capability_snapshot
        self._provider_catalog_revision = provider_catalog_revision or getattr(
            ai_service,
            "provider_catalog_revision",
            EMPTY_CAPABILITY_SNAPSHOT.content_revision,
        )
        if not callable(tool_clock):
            raise TypeError("tool_clock must be callable")
        self._tool_clock = tool_clock
        if model_requirement is not None and model_requirement.logical_alias != "ai.quality":
            raise ValueError("self-evolution requires the ai.quality logical alias")
        if (
            ai_service is not None
            and known_locality is not True
            and (model_requirement is None or model_requirement.provider_model_id is None)
        ):
            raise ValueError("remote or unknown self-evolution provider requires a configured quality model binding")
        self._model_requirement = model_requirement or TaskModelRequirement("ai.quality")
        self._closed = False

    def close(self) -> None:
        self._closed = True

    async def propose(
        self,
        *,
        actor_id: int,
        guild_id: int,
        title: str,
        rationale: str,
        target_paths: tuple[str, ...],
        generate_with_ai: bool,
        allow_remote: bool,
    ) -> CreatedProposal:
        # Kept only for command compatibility. Persistent user consent is the
        # authorization source and this per-call flag never grants access.
        _ = allow_remote
        actor_id = _snowflake(actor_id, "actor_id")
        guild_id = _snowflake(guild_id, "guild_id")
        self._require_action_allowed("propose", guild_id, actor_id)
        normalized_title = _text(title, "title", maximum=160)
        normalized_rationale = _text(rationale, "rationale", maximum=1_000)
        normalized_targets = _target_paths(target_paths)
        _reject_secret_like(normalized_title, normalized_rationale, *normalized_targets)

        plan: str
        model: str | None = None
        if generate_with_ai:
            service = self.ai_service
            if service is None or not service.available:
                raise EvolutionServiceError("AI provider is unavailable")
            if not self._provider_is_local:
                try:
                    consent_active = self._remote_consent_active(actor_id) is True
                except Exception:
                    consent_active = False
                if not consent_active:
                    raise EvolutionServiceError("persistent remote AI consent is required")
            prompt = (
                f"改善提案: {normalized_title}\n"
                f"理由: {normalized_rationale}\n"
                f"対象候補: {', '.join(normalized_targets)}\n"
                "安全性、互換性、必要test、rollback、完了条件を含む実装計画だけを日本語で作成してください。"
            )
            toolset = BoundedToolSet.issue(
                scope=ToolScopeBinding(guild_id, None, actor_id),
                intent="self_evolution",
                complexity=TaskComplexity.COMPLEX.value,
                snapshot=self._capability_snapshot,
                provider_catalog_revision=self._provider_catalog_revision,
                web_search=False,
                issued_at=self._tool_clock(),
            )
            provider_boundary = DataBoundary.REMOTE_OPT_IN if not self._provider_is_local else DataBoundary.LOCAL_ONLY
            context = self._context_builder.build(
                ContextBuildInput(
                    Scope(guild_id, actor_id, visibility=MemoryVisibility.USER_PRIVATE),
                    prompt,
                    (),
                    task_instructions=(
                        "改善proposal planだけを返し、コード適用、秘密値、merge、push、restartを実行しないでください。",
                    ),
                    allowed_typed_tools=toolset.effective_tools,
                    intent="self_evolution",
                    capability_metadata=capability_metadata_transport(toolset),
                    complexity=TaskComplexity.COMPLEX.value,
                    bounded_toolset_digest=toolset.digest,
                    capability_catalog_revision=toolset.capability_catalog_revision,
                    provider_catalog_revision=toolset.provider_catalog_revision,
                    provider_envelope_sha256=provider_facing_envelope_digest(
                        prompt=prompt,
                        provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
                        history=(),
                        attachments=(),
                        metadata={},
                        task_kind=TaskKind.SELF_EVOLUTION,
                        complexity=TaskComplexity.COMPLEX,
                        risk=RiskLevel.HIGH,
                        uses_tools=False,
                        web_search=False,
                        has_side_effects=False,
                        boundary=provider_boundary,
                        required_model_alias=self._model_requirement.logical_alias,
                        required_model_id=self._model_requirement.provider_model_id,
                    ),
                )
            )
            try:
                reply = await service.ask(
                    AIRequest(
                        prompt=prompt,
                        guild_id=guild_id,
                        user_id=actor_id,
                        provider_input=FORMAL_PROVIDER_INPUT_DIRECTIVE,
                        context_authorization=context.context_authorization,
                        boundary=provider_boundary,
                        system_prompt=context.prompt,
                        task_kind=TaskKind.SELF_EVOLUTION,
                        complexity=TaskComplexity.COMPLEX,
                        risk=RiskLevel.HIGH,
                        required_model_alias=self._model_requirement.logical_alias,
                        required_model_id=self._model_requirement.provider_model_id,
                        intent="self_evolution",
                        bounded_toolset=toolset,
                        allowed_model_tools=toolset.effective_tools,
                        max_tool_calls=toolset.max_tool_calls,
                    ),
                    provider_call_allowed=lambda: (
                        self._action_allowed("propose", guild_id, actor_id)
                        and (self._provider_is_local or self._remote_consent_active(actor_id) is True)
                    ),
                )
            except ProviderSelectionError as exc:
                if exc.reason == "required_model_unavailable":
                    raise EvolutionServiceError(
                        "the configured required quality model is unavailable before self-evolution dispatch"
                    ) from exc
                raise EvolutionServiceError("AI proposal route is unavailable") from exc
            except (AIUnavailableError, PrivacyBoundaryError) as exc:
                if not self._action_allowed("propose", guild_id, actor_id):
                    raise EvolutionDisabledError("self-evolution operation changed before the AI sink") from exc
                raise EvolutionServiceError("AI proposal generation failed") from exc
            if (
                self._model_requirement.provider_model_id is not None
                and reply.model != self._model_requirement.provider_model_id
            ):
                raise EvolutionServiceError("self-evolution proposal did not use the configured required quality model")
            plan = _text(reply.text, "plan", maximum=40_000)
            _reject_secret_like(plan)
            model = reply.model
        else:
            plan = "AI生成なし。所有者が記載した理由をproposalとして審査します。"

        created_at = datetime.now(UTC)
        content = _render_artifact(
            title=normalized_title,
            rationale=normalized_rationale,
            target_paths=normalized_targets,
            plan=plan,
            model=model,
            created_at=created_at,
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        proposal_id = f"evo-{digest[:20]}"
        self._require_action_allowed("propose", guild_id, actor_id)
        artifact_path = self._write_artifact(
            proposal_id,
            content,
            commit_check=lambda: self._require_action_allowed("propose", guild_id, actor_id),
        )
        try:
            self._require_action_allowed("propose", guild_id, actor_id)
            record = self.database.create_evolution_proposal(
                proposal_id,
                digest,
                proposer_id=actor_id,
                reason=f"{normalized_title}: {normalized_rationale}"[:1_000],
            )
        except EvolutionDisabledError:
            self._remove_partial_artifact(artifact_path)
            raise
        except Exception as exc:
            try:
                existing = self.database.get_evolution_proposal(proposal_id)
            except Exception as verify_exc:
                raise EvolutionArtifactError("proposal persistence state is uncertain") from verify_exc
            if existing is not None:
                if existing.proposal_hash == digest:
                    return CreatedProposal(record=existing, artifact_path=artifact_path, plan=plan, model=model)
                raise EvolutionArtifactError("proposal persistence state is uncertain") from exc
            self._remove_partial_artifact(artifact_path)
            raise EvolutionServiceError("proposal persistence failed") from exc
        return CreatedProposal(record=record, artifact_path=artifact_path, plan=plan, model=model)

    def begin_review(
        self,
        proposal_id: str,
        *,
        actor_id: int,
        guild_id: int,
        note: str,
    ) -> EvolutionProposalRecord:
        actor_id = _snowflake(actor_id, "actor_id")
        guild_id = _snowflake(guild_id, "guild_id")
        self._require_action_allowed("review", guild_id, actor_id)
        normalized_note = _text(note, "note", maximum=1_000)
        self._require_action_allowed("review", guild_id, actor_id)
        return self.database.update_evolution_proposal_status(
            proposal_id,
            "in_review",
            updated_by=actor_id,
            reason=normalized_note,
        )

    def approve(
        self,
        proposal_id: str,
        *,
        actor_id: int,
        guild_id: int,
        note: str,
    ) -> EvolutionProposalRecord:
        actor_id = _snowflake(actor_id, "actor_id")
        guild_id = _snowflake(guild_id, "guild_id")
        self._require_action_allowed("approve", guild_id, actor_id)
        normalized_note = _text(note, "note", maximum=1_000)
        verified = self.show(proposal_id)
        if not verified.integrity_ok:
            raise EvolutionArtifactError("proposal artifact integrity check failed")
        self._require_action_allowed("approve", guild_id, actor_id)
        return self.database.update_evolution_proposal_status(
            proposal_id,
            "approved",
            updated_by=actor_id,
            reason=normalized_note,
        )

    def reject(
        self,
        proposal_id: str,
        *,
        actor_id: int,
        guild_id: int,
        note: str,
    ) -> EvolutionProposalRecord:
        actor_id = _snowflake(actor_id, "actor_id")
        guild_id = _snowflake(guild_id, "guild_id")
        self._require_action_allowed("reject", guild_id, actor_id)
        normalized_note = _text(note, "note", maximum=1_000)
        self._require_action_allowed("reject", guild_id, actor_id)
        return self.database.update_evolution_proposal_status(
            proposal_id,
            "rejected",
            updated_by=actor_id,
            reason=normalized_note,
        )

    def show(self, proposal_id: str) -> VerifiedProposal:
        record = self.database.get_evolution_proposal(proposal_id)
        if record is None:
            raise KeyError(proposal_id)
        path = self._artifact_path(record.proposal_id)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise EvolutionArtifactError("proposal artifact cannot be read") from exc
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return VerifiedProposal(
            record=record,
            artifact_path=path,
            content=content,
            integrity_ok=secrets.compare_digest(digest, record.proposal_hash),
        )

    def list(self, *, limit: int = 10) -> tuple[EvolutionProposalRecord, ...]:
        return self.database.list_evolution_proposals(limit=limit)

    def _write_artifact(
        self,
        proposal_id: str,
        content: str,
        *,
        commit_check: Callable[[], None] | None = None,
    ) -> Path:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        if self.artifact_dir.is_symlink():
            raise EvolutionArtifactError("artifact directory must not be a symlink")
        target = self._artifact_path(proposal_id)
        if target.exists() or target.is_symlink():
            raise EvolutionArtifactError("proposal artifact already exists")
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if commit_check is not None:
                commit_check()
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return target

    def _artifact_path(self, proposal_id: str) -> Path:
        if not re.fullmatch(r"evo-[0-9a-f]{20}", proposal_id):
            raise ValueError("invalid proposal_id")
        return self.artifact_dir / f"{proposal_id}.md"

    def _require_enabled(self) -> None:
        if self._closed or not self.enabled:
            raise EvolutionDisabledError("self-evolution proposal creation and review are disabled")

    def _require_action_allowed(self, operation: str, guild_id: int, actor_id: int) -> None:
        self._require_enabled()
        try:
            allowed = self._current_policy(operation, guild_id, actor_id)
        except Exception:
            allowed = False
        if self._closed or allowed is not True:
            raise EvolutionDisabledError("self-evolution operation is currently disabled")

    def _action_allowed(self, operation: str, guild_id: int, actor_id: int) -> bool:
        try:
            return not self._closed and self.enabled and self._current_policy(operation, guild_id, actor_id) is True
        except Exception:
            return False

    @staticmethod
    def _remove_partial_artifact(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            raise EvolutionArtifactError("proposal artifact cleanup state is uncertain") from cleanup_exc


def _text(value: str, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip()
    if not 1 <= len(normalized) <= maximum:
        raise ValueError(f"{label} must contain 1 to {maximum} characters")
    return normalized


def _snowflake(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
        raise ValueError(f"{label} must be a valid Discord ID")
    return value


def _target_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    if not 1 <= len(values) <= 10:
        raise ValueError("target_paths must contain 1 to 10 paths")
    normalized: list[str] = []
    for raw in values:
        value = raw.strip().replace("\\", "/")
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError("target path must be workspace-relative")
        lowered = tuple(part.lower() for part in path.parts)
        if any(
            part == ".env" or part.startswith(".env.") or part.split(".", 1)[0] in _PROTECTED_STEMS for part in lowered
        ):
            raise ValueError("self-evolution cannot target secrets or RBAC paths")
        normalized.append(path.as_posix())
    if len(set(normalized)) != len(normalized):
        raise ValueError("target_paths must be unique")
    return tuple(normalized)


def _reject_secret_like(*values: str) -> None:
    if any(pattern.search(value) for value in values for pattern in _SECRET_PATTERNS):
        raise ValueError("proposal text must not contain credentials")


def _render_artifact(
    *,
    title: str,
    rationale: str,
    target_paths: tuple[str, ...],
    plan: str,
    model: str | None,
    created_at: datetime,
) -> str:
    targets = "\n".join(f"- `{path}`" for path in target_paths)
    return (
        f"# {title}\n\n"
        f"- Created: {created_at.isoformat()}\n"
        f"- Model: {model or 'none'}\n"
        "- Application: disabled by design\n\n"
        f"## Rationale\n\n{rationale}\n\n"
        f"## Target paths\n\n{targets}\n\n"
        f"## Proposal plan\n\n{plan}\n"
    )
