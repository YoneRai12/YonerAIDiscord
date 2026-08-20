from __future__ import annotations

import json

import pytest

from yonerai_discord.sandbox_operator_cli import (
    SCHEMA,
    SandboxCliDependencies,
    SandboxCode,
    SandboxCommand,
    SandboxJobView,
    SandboxMutationOutcome,
    SandboxReceiptView,
    SandboxStatusSnapshot,
    build_sandbox_parser,
    dispatch_sandbox_command,
    parse_sandbox_args,
    render_sandbox_result,
    sandbox_exit_code,
)


class _ReadProjection:
    def __init__(self, *, ready: bool = False) -> None:
        self.snapshot = SandboxStatusSnapshot(
            ready=ready,
            contract="implemented_offline" if not ready else "live_verified",
            vm="absent" if not ready else "running",
            broker="unconfigured" if not ready else "ready",
            worker="unconfigured" if not ready else "ready",
            execution_count=0,
            blockers=() if ready else ("actual_vm_absent",),
        )
        self.jobs = (SandboxJobView("job-1", "python-smoke", "pending"),)
        self.receipt: SandboxReceiptView | None = SandboxReceiptView(
            "job-1",
            "succeeded",
            signed=True,
            cleanup_confirmed=True,
        )
        self.limits: list[int] = []
        self.receipt_ids: list[str] = []
        self.status_calls = 0

    async def read_status(self) -> SandboxStatusSnapshot:
        self.status_calls += 1
        return self.snapshot

    async def list_jobs(self, *, limit: int) -> tuple[SandboxJobView, ...]:
        self.limits.append(limit)
        return self.jobs

    async def read_receipt(self, job_id: str) -> SandboxReceiptView | None:
        self.receipt_ids.append(job_id)
        return self.receipt


class _Owner:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls = 0

    async def current_actor_is_owner(self) -> bool:
        self.calls += 1
        return self.allowed


class _Mutations:
    def __init__(self) -> None:
        self.templates: list[str] = []
        self.cancelled: list[str] = []

    async def run_template(self, template: object) -> SandboxMutationOutcome:
        value = str(template)
        self.templates.append(value)
        return SandboxMutationOutcome(SandboxCode.OK, True, "job-2", "pending", audit_recorded=True)

    async def cancel(self, job_id: str) -> SandboxMutationOutcome:
        self.cancelled.append(job_id)
        return SandboxMutationOutcome(SandboxCode.OK, True, job_id, "cancel_requested", audit_recorded=True)


class _DoctorReport:
    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": "yonerai.sandbox-doctor.v1",
            "state": "contract_ready",
            "scope": {"backend": "injected_hyperv_contract", "actual_vm_contacted": False, "live_ready": False},
            "components": {
                "transport": "implemented_offline",
                "signed_job": "implemented_offline",
                "signed_receipt": "implemented_offline",
                "durable_replay": "implemented_offline",
                "trusted_broker": "unconfigured",
                "guest_worker": "unconfigured",
                "vm_lifecycle": "unconfigured",
            },
            "blockers": ["actual_vm_absent", "canary_not_run"],
            "checks": {
                "typed_status": True,
                "exact_binding": True,
                "receipt_integrity": True,
                "artifact_ownership": True,
                "audit": True,
                "timeout_cleanup": True,
                "cancel_cleanup": True,
            },
            "error_code": None,
        }


async def _doctor() -> _DoctorReport:
    return _DoctorReport()


def _synthetic_host_path() -> str:
    return "".join(("C", ":/", "host/private/sk-secret"))


def _assert_envelope(result: object, command: SandboxCommand) -> None:
    mapping = result.to_mapping()  # type: ignore[union-attr]
    assert tuple(mapping) == ("schema", "command", "ok", "code", "ready", "changed", "data", "blockers")
    assert mapping["schema"] == SCHEMA
    assert mapping["command"] == command.value


@pytest.mark.parametrize(
    "argv, command",
    [
        (["status"], "status"),
        (["doctor"], "doctor"),
        (["plan"], "plan"),
        (["run-template"], "run-template"),
        (["jobs"], "jobs"),
        (["receipt", "job-1"], "receipt"),
        (["cancel", "job-1"], "cancel"),
    ],
)
def test_parser_exposes_only_bounded_surface(argv: list[str], command: str) -> None:
    args = parse_sandbox_args(argv)
    assert args.sandbox_command == command
    assert not hasattr(args, "owner_id")
    assert not hasattr(args, "code")
    assert not hasattr(args, "path")


def test_parser_help_and_validation(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as help_exit:
        build_sandbox_parser().parse_args(["--help"])
    assert help_exit.value.code == 0
    assert "run-template" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        parse_sandbox_args(["receipt", "../secret"])
    with pytest.raises(SystemExit):
        parse_sandbox_args(["jobs", "--limit", "101"])
    with pytest.raises(SystemExit):
        parse_sandbox_args(["run-template", "raw-code"])


@pytest.mark.asyncio
async def test_status_default_is_honest_read_only_and_unready() -> None:
    result = await dispatch_sandbox_command(parse_sandbox_args(["status"]))
    _assert_envelope(result, SandboxCommand.STATUS)
    assert result.code is SandboxCode.OK
    assert result.ready is False
    assert result.changed is False
    assert result.data == {
        "contract": "implemented_offline",
        "vm": "absent",
        "broker": "unconfigured",
        "worker": "unconfigured",
        "execution_count": 0,
        "raw_inline_code": False,
    }


@pytest.mark.asyncio
async def test_doctor_projects_existing_report_without_claiming_live_readiness() -> None:
    result = await dispatch_sandbox_command(
        parse_sandbox_args(["doctor"]),
        SandboxCliDependencies(doctor=_doctor),
    )
    _assert_envelope(result, SandboxCommand.DOCTOR)
    assert result.code is SandboxCode.OK
    assert result.ready is False
    assert result.data["contract_state"] == "contract_ready"
    assert result.data["actual_vm_contacted"] is False
    serialized = render_sandbox_result(result, json_mode=True)
    assert "injected_hyperv_contract" not in serialized
    assert "error_code" not in serialized


@pytest.mark.asyncio
async def test_missing_or_broken_doctor_is_content_free() -> None:
    missing = await dispatch_sandbox_command(parse_sandbox_args(["doctor"]))
    source_path = _synthetic_host_path()

    async def broken() -> _DoctorReport:
        raise RuntimeError(source_path)

    failed = await dispatch_sandbox_command(
        parse_sandbox_args(["doctor"]),
        SandboxCliDependencies(doctor=broken),
    )
    for result in (missing, failed):
        assert result.code is SandboxCode.READ_FAILED
        assert result.data == {}
        serialized = render_sandbox_result(result, json_mode=True)
        assert source_path not in serialized
        assert "private" not in serialized


@pytest.mark.asyncio
async def test_plan_is_non_mutating_and_raw_code_stays_disabled() -> None:
    result = await dispatch_sandbox_command(parse_sandbox_args(["plan"]))
    assert result.code is SandboxCode.OK
    assert result.changed is False
    assert result.data == {
        "mutation": False,
        "owner_local_only": True,
        "raw_inline_code": False,
        "external_bind": False,
        "vm_network": False,
        "templates": ["python-smoke"],
    }


@pytest.mark.asyncio
async def test_unready_run_template_has_exact_zero_side_effect_evidence() -> None:
    owner = _Owner(True)
    mutations = _Mutations()
    result = await dispatch_sandbox_command(
        parse_sandbox_args(["run-template"]),
        SandboxCliDependencies(owner_authorizer=owner, mutations=mutations),
    )
    _assert_envelope(result, SandboxCommand.RUN_TEMPLATE)
    assert result.code is SandboxCode.SANDBOX_NOT_READY
    assert result.changed is False
    assert result.data == {
        "template": "python-smoke",
        "enqueue_count": 0,
        "audit_count": 0,
        "pipe_exchange_count": 0,
        "vm_contact_count": 0,
        "raw_inline_code": False,
    }
    assert owner.calls == 0
    assert mutations.templates == []


@pytest.mark.asyncio
async def test_ready_run_requires_process_bound_owner_and_explicit_mutation_port() -> None:
    read = _ReadProjection(ready=True)
    no_auth = await dispatch_sandbox_command(
        parse_sandbox_args(["run-template"]),
        SandboxCliDependencies(read_projection=read, mutations=_Mutations()),
    )
    assert no_auth.code is SandboxCode.OWNER_AUTH_UNAVAILABLE
    assert no_auth.changed is False

    denied_owner = _Owner(False)
    mutations = _Mutations()
    denied = await dispatch_sandbox_command(
        parse_sandbox_args(["run-template"]),
        SandboxCliDependencies(read_projection=read, owner_authorizer=denied_owner, mutations=mutations),
    )
    assert denied.code is SandboxCode.OWNER_DENIED
    assert mutations.templates == []


@pytest.mark.asyncio
async def test_ready_owner_can_invoke_only_fixed_template() -> None:
    read = _ReadProjection(ready=True)
    owner = _Owner(True)
    mutations = _Mutations()
    result = await dispatch_sandbox_command(
        parse_sandbox_args(["run-template", "python-smoke"]),
        SandboxCliDependencies(read_projection=read, owner_authorizer=owner, mutations=mutations),
    )
    assert result.code is SandboxCode.OK
    assert result.changed is True
    assert result.data == {
        "job_id": "job-2",
        "state": "pending",
        "audit_recorded": True,
        "template": "python-smoke",
    }
    assert mutations.templates == ["python-smoke"]


@pytest.mark.asyncio
async def test_jobs_and_receipt_use_only_redacted_read_projection() -> None:
    read = _ReadProjection()
    owner = _Owner(True)
    jobs = await dispatch_sandbox_command(
        parse_sandbox_args(["jobs", "--limit", "7"]),
        SandboxCliDependencies(read_projection=read, owner_authorizer=owner),
    )
    receipt = await dispatch_sandbox_command(
        parse_sandbox_args(["receipt", "job-1"]),
        SandboxCliDependencies(read_projection=read, owner_authorizer=owner),
    )
    assert jobs.code is SandboxCode.OK
    assert jobs.data == {
        "jobs": [{"job_id": "job-1", "template": "python-smoke", "state": "pending"}],
        "count": 1,
    }
    assert read.limits == [7]
    assert receipt.code is SandboxCode.OK
    assert receipt.data == {
        "job_id": "job-1",
        "state": "succeeded",
        "signed": True,
        "cleanup_confirmed": True,
        "failure_code": None,
    }
    assert read.receipt_ids == ["job-1"]
    assert owner.calls == 2


@pytest.mark.asyncio
async def test_read_surfaces_fail_closed_without_database_or_receipt() -> None:
    owner = _Owner(True)
    jobs = await dispatch_sandbox_command(
        parse_sandbox_args(["jobs"]),
        SandboxCliDependencies(owner_authorizer=owner),
    )
    receipt = await dispatch_sandbox_command(
        parse_sandbox_args(["receipt", "job-404"]),
        SandboxCliDependencies(owner_authorizer=owner),
    )
    assert jobs.code is SandboxCode.DATABASE_NOT_FOUND
    assert receipt.code is SandboxCode.DATABASE_NOT_FOUND

    read = _ReadProjection()
    read.receipt = None
    missing = await dispatch_sandbox_command(
        parse_sandbox_args(["receipt", "job-404"]),
        SandboxCliDependencies(read_projection=read, owner_authorizer=owner),
    )
    assert missing.code is SandboxCode.RECEIPT_NOT_AVAILABLE
    assert missing.changed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, _Owner(False)])
async def test_jobs_and_receipt_authorize_before_any_read_projection(owner: _Owner | None) -> None:
    read = _ReadProjection(ready=True)
    dependencies = SandboxCliDependencies(read_projection=read, owner_authorizer=owner)
    jobs = await dispatch_sandbox_command(parse_sandbox_args(["jobs"]), dependencies)
    receipt = await dispatch_sandbox_command(parse_sandbox_args(["receipt", "job-1"]), dependencies)
    expected = SandboxCode.OWNER_AUTH_UNAVAILABLE if owner is None else SandboxCode.OWNER_DENIED
    for result in (jobs, receipt):
        assert result.code is expected
        assert result.changed is False
        assert result.data == {}
        assert result.blockers == ()
    assert read.status_calls == 0
    assert read.limits == []
    assert read.receipt_ids == []


@pytest.mark.asyncio
async def test_jobs_owner_auth_error_is_content_free_and_reads_nothing() -> None:
    source_path = _synthetic_host_path()

    class BrokenOwner:
        async def current_actor_is_owner(self) -> bool:
            raise RuntimeError(source_path)

    read = _ReadProjection(ready=True)
    result = await dispatch_sandbox_command(
        parse_sandbox_args(["jobs"]),
        SandboxCliDependencies(read_projection=read, owner_authorizer=BrokenOwner()),
    )
    assert result.code is SandboxCode.OWNER_AUTH_UNAVAILABLE
    assert result.data == {}
    assert read.status_calls == 0
    assert read.limits == []
    assert read.receipt_ids == []
    serialized = render_sandbox_result(result, json_mode=True)
    assert source_path not in serialized
    assert "secret" not in serialized


@pytest.mark.asyncio
async def test_cancel_never_calls_live_path_without_injected_mutation() -> None:
    read = _ReadProjection(ready=True)
    owner = _Owner(True)
    result = await dispatch_sandbox_command(
        parse_sandbox_args(["cancel", "job-1"]),
        SandboxCliDependencies(read_projection=read, owner_authorizer=owner),
    )
    assert result.code is SandboxCode.SANDBOX_NOT_READY
    assert result.changed is False
    assert owner.calls == 0


@pytest.mark.asyncio
async def test_cancel_is_owner_bound_and_returns_typed_audited_result() -> None:
    read = _ReadProjection(ready=True)
    owner = _Owner(True)
    mutations = _Mutations()
    result = await dispatch_sandbox_command(
        parse_sandbox_args(["cancel", "job-1"]),
        SandboxCliDependencies(read_projection=read, owner_authorizer=owner, mutations=mutations),
    )
    assert result.code is SandboxCode.OK
    assert result.changed is True
    assert result.data == {
        "job_id": "job-1",
        "state": "cancel_requested",
        "audit_recorded": True,
    }
    assert owner.calls == 1
    assert mutations.cancelled == ["job-1"]


@pytest.mark.asyncio
async def test_cancel_reaches_owner_bound_mutation_when_backend_readiness_drops() -> None:
    read = _ReadProjection(ready=False)
    owner = _Owner(True)
    mutations = _Mutations()

    result = await dispatch_sandbox_command(
        parse_sandbox_args(["cancel", "job-1"]),
        SandboxCliDependencies(read_projection=read, owner_authorizer=owner, mutations=mutations),
    )

    assert result.code is SandboxCode.OK
    assert result.ready is False and result.changed is True
    assert result.blockers == ("actual_vm_absent",)
    assert owner.calls == 1
    assert mutations.cancelled == ["job-1"]


@pytest.mark.asyncio
async def test_unready_cancel_still_rejects_non_owner_before_mutation() -> None:
    owner = _Owner(False)
    mutations = _Mutations()

    result = await dispatch_sandbox_command(
        parse_sandbox_args(["cancel", "job-1"]),
        SandboxCliDependencies(
            read_projection=_ReadProjection(ready=False),
            owner_authorizer=owner,
            mutations=mutations,
        ),
    )

    assert result.code is SandboxCode.OWNER_DENIED
    assert result.ready is False and result.changed is False
    assert owner.calls == 1
    assert mutations.cancelled == []


@pytest.mark.asyncio
async def test_mutation_errors_are_content_free_and_report_no_change() -> None:
    source_path = _synthetic_host_path()

    class BrokenMutations(_Mutations):
        async def cancel(self, job_id: str) -> SandboxMutationOutcome:
            del job_id
            raise RuntimeError(source_path)

    result = await dispatch_sandbox_command(
        parse_sandbox_args(["cancel", "job-1"]),
        SandboxCliDependencies(
            read_projection=_ReadProjection(ready=True),
            owner_authorizer=_Owner(True),
            mutations=BrokenMutations(),
        ),
    )
    assert result.code is SandboxCode.MUTATION_FAILED
    assert result.changed is False
    serialized = render_sandbox_result(result, json_mode=True)
    assert source_path not in serialized
    assert "secret" not in serialized


def test_changed_mutation_outcome_requires_job_state_and_audit_binding() -> None:
    with pytest.raises(ValueError, match="bound job, state, and audit"):
        SandboxMutationOutcome(SandboxCode.OK, True, "job-1", "pending", audit_recorded=False)


@pytest.mark.asyncio
async def test_json_and_human_renderers_keep_stable_public_fields() -> None:
    result = await dispatch_sandbox_command(parse_sandbox_args(["status", "--json"]))
    payload = json.loads(render_sandbox_result(result, json_mode=True))
    assert tuple(payload) == ("schema", "command", "ok", "code", "ready", "changed", "data", "blockers")
    human = render_sandbox_result(result, json_mode=False)
    assert "code: OK" in human
    assert "ready: false" in human
    assert "changed: false" in human
    assert sandbox_exit_code(result) == 0


@pytest.mark.asyncio
async def test_exit_codes_are_stable() -> None:
    unready = await dispatch_sandbox_command(parse_sandbox_args(["run-template"]))
    denied = await dispatch_sandbox_command(
        parse_sandbox_args(["run-template"]),
        SandboxCliDependencies(
            read_projection=_ReadProjection(ready=True),
            owner_authorizer=_Owner(False),
            mutations=_Mutations(),
        ),
    )
    assert sandbox_exit_code(unready) == 2
    assert sandbox_exit_code(denied) == 3
