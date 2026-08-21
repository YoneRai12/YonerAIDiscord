from __future__ import annotations

import argparse
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


SCHEMA = "yonerai.sandbox-operator-cli.v1"
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DEFAULT_BLOCKERS = (
    "trusted_broker_unconfigured",
    "actual_vm_absent",
    "canary_not_run",
)
_COMPONENT_KEYS = (
    "transport",
    "signed_job",
    "signed_receipt",
    "durable_replay",
    "trusted_broker",
    "guest_worker",
    "vm_lifecycle",
)
_CHECK_KEYS = (
    "typed_status",
    "exact_binding",
    "receipt_integrity",
    "artifact_ownership",
    "audit",
    "timeout_cleanup",
    "cancel_cleanup",
)


class SandboxCommand(StrEnum):
    STATUS = "status"
    DOCTOR = "doctor"
    PLAN = "plan"
    RUN_TEMPLATE = "run-template"
    JOBS = "jobs"
    RECEIPT = "receipt"
    CANCEL = "cancel"


class SandboxCode(StrEnum):
    OK = "OK"
    SANDBOX_NOT_READY = "SANDBOX_NOT_READY"
    OWNER_AUTH_UNAVAILABLE = "OWNER_AUTH_UNAVAILABLE"
    OWNER_DENIED = "OWNER_DENIED"
    DATABASE_NOT_FOUND = "DATABASE_NOT_FOUND"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_CANCELLABLE = "JOB_NOT_CANCELLABLE"
    RECEIPT_NOT_AVAILABLE = "RECEIPT_NOT_AVAILABLE"
    READ_FAILED = "READ_FAILED"
    AUDIT_UNAVAILABLE = "AUDIT_UNAVAILABLE"
    MUTATION_FAILED = "MUTATION_FAILED"


class SandboxTemplate(StrEnum):
    PYTHON_SMOKE = "python-smoke"


def _require_token(value: str, field: str) -> None:
    if not _SAFE_TOKEN.fullmatch(value):
        raise ValueError(f"{field} must be a bounded code token")


def _require_job_id(value: str) -> None:
    if not _SAFE_JOB_ID.fullmatch(value):
        raise ValueError("job_id must be a bounded opaque identifier")


@dataclass(frozen=True, slots=True)
class SandboxStatusSnapshot:
    ready: bool = False
    contract: str = "implemented_offline"
    vm: str = "absent"
    broker: str = "unconfigured"
    worker: str = "unconfigured"
    execution_count: int = 0
    blockers: tuple[str, ...] = _DEFAULT_BLOCKERS

    def __post_init__(self) -> None:
        for field, value in (
            ("contract", self.contract),
            ("vm", self.vm),
            ("broker", self.broker),
            ("worker", self.worker),
        ):
            _require_token(value, field)
        if isinstance(self.execution_count, bool) or self.execution_count < 0:
            raise ValueError("execution_count must be a non-negative integer")
        for blocker in self.blockers:
            _require_token(blocker, "blocker")
        if self.ready and self.blockers:
            raise ValueError("a ready snapshot cannot contain blockers")


@dataclass(frozen=True, slots=True)
class SandboxJobView:
    job_id: str
    template: str
    state: str

    def __post_init__(self) -> None:
        _require_job_id(self.job_id)
        _require_token(self.template, "template")
        _require_token(self.state, "state")


@dataclass(frozen=True, slots=True)
class SandboxReceiptView:
    job_id: str
    state: str
    signed: bool
    cleanup_confirmed: bool
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _require_job_id(self.job_id)
        _require_token(self.state, "state")
        if self.failure_code is not None:
            _require_token(self.failure_code, "failure_code")


@dataclass(frozen=True, slots=True)
class SandboxMutationOutcome:
    code: SandboxCode
    changed: bool
    job_id: str | None = None
    state: str | None = None
    audit_recorded: bool = False

    def __post_init__(self) -> None:
        if self.job_id is not None:
            _require_job_id(self.job_id)
        if self.state is not None:
            _require_token(self.state, "state")
        if self.changed and self.code is not SandboxCode.OK:
            raise ValueError("changed outcomes must use OK")
        if self.changed and (self.job_id is None or self.state is None or not self.audit_recorded):
            raise ValueError("changed outcomes require a bound job, state, and audit")


class SandboxReadProjection(Protocol):
    """Read-only, redacted projection. It must never open a writable repository."""

    async def read_status(self) -> SandboxStatusSnapshot: ...

    async def list_jobs(self, *, limit: int) -> tuple[SandboxJobView, ...]: ...

    async def read_receipt(self, job_id: str) -> SandboxReceiptView | None: ...


class SandboxOwnerAuthorizer(Protocol):
    """Resolve the current surface actor without accepting a caller-supplied identity."""

    async def current_actor_is_owner(self) -> bool: ...


class SandboxMutationPort(Protocol):
    async def run_template(self, template: SandboxTemplate) -> SandboxMutationOutcome: ...

    async def cancel(self, job_id: str) -> SandboxMutationOutcome: ...


class SandboxDoctorReport(Protocol):
    def to_mapping(self) -> Mapping[str, object]: ...


DoctorRunner = Callable[[], Awaitable[SandboxDoctorReport]]


@dataclass(frozen=True, slots=True)
class SandboxCliDependencies:
    read_projection: SandboxReadProjection | None = None
    doctor: DoctorRunner | None = None
    owner_authorizer: SandboxOwnerAuthorizer | None = None
    mutations: SandboxMutationPort | None = None


@dataclass(frozen=True, slots=True)
class SandboxCliResult:
    command: SandboxCommand
    ok: bool
    code: SandboxCode
    ready: bool
    changed: bool
    data: Mapping[str, object]
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for blocker in self.blockers:
            _require_token(blocker, "blocker")
        if self.ok != (self.code is SandboxCode.OK):
            raise ValueError("ok and code disagree")
        if self.changed and not self.ok:
            raise ValueError("failed results cannot report changes")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "command": self.command.value,
            "ok": self.ok,
            "code": self.code.value,
            "ready": self.ready,
            "changed": self.changed,
            "data": dict(self.data),
            "blockers": list(self.blockers),
        }


def build_sandbox_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yonerai-discord sandbox",
        description="YonerAI execution sandbox operator surface",
    )
    parser.add_argument("--json", dest="json_mode", action="store_true", help="render stable JSON")
    commands = parser.add_subparsers(dest="sandbox_command", required=True)
    for command in SandboxCommand:
        child = commands.add_parser(command.value)
        child.add_argument(
            "--json",
            dest="json_mode",
            action="store_true",
            default=argparse.SUPPRESS,
            help="render stable JSON",
        )
        if command is SandboxCommand.RUN_TEMPLATE:
            child.add_argument(
                "template",
                nargs="?",
                choices=[item.value for item in SandboxTemplate],
                default=SandboxTemplate.PYTHON_SMOKE.value,
            )
        elif command is SandboxCommand.JOBS:
            child.add_argument("--limit", type=_bounded_limit, default=25)
        elif command in {SandboxCommand.RECEIPT, SandboxCommand.CANCEL}:
            child.add_argument("job_id", type=_job_id_argument)
    return parser


def parse_sandbox_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_sandbox_parser().parse_args(argv)


def _bounded_limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return parsed


def _job_id_argument(value: str) -> str:
    try:
        _require_job_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("job_id is invalid") from exc
    return value


def render_sandbox_result(result: SandboxCliResult, *, json_mode: bool) -> str:
    mapping = result.to_mapping()
    if json_mode:
        return json.dumps(
            mapping,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    blockers = ", ".join(result.blockers) if result.blockers else "none"
    data = json.dumps(dict(result.data), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return "\n".join(
        (
            f"command: {result.command.value}",
            f"code: {result.code.value}",
            f"ready: {str(result.ready).lower()}",
            f"changed: {str(result.changed).lower()}",
            f"blockers: {blockers}",
            f"data: {data}",
        )
    )


def sandbox_exit_code(result: SandboxCliResult) -> int:
    if result.ok:
        return 0
    if result.code is SandboxCode.OWNER_DENIED:
        return 3
    return 2


async def dispatch_sandbox_command(
    args: argparse.Namespace,
    dependencies: SandboxCliDependencies | None = None,
) -> SandboxCliResult:
    deps = dependencies or SandboxCliDependencies()
    command = SandboxCommand(args.sandbox_command)
    if command is SandboxCommand.STATUS:
        return await _status(command, deps)
    if command is SandboxCommand.DOCTOR:
        return await _doctor(command, deps)
    if command is SandboxCommand.PLAN:
        return await _plan(command, deps)
    if command is SandboxCommand.RUN_TEMPLATE:
        return await _run_template(command, SandboxTemplate(args.template), deps)
    if command is SandboxCommand.JOBS:
        return await _jobs(command, args.limit, deps)
    if command is SandboxCommand.RECEIPT:
        return await _receipt(command, args.job_id, deps)
    return await _cancel(command, args.job_id, deps)


def _result(
    command: SandboxCommand,
    code: SandboxCode,
    *,
    ready: bool,
    changed: bool = False,
    data: Mapping[str, object] | None = None,
    blockers: tuple[str, ...] = (),
) -> SandboxCliResult:
    return SandboxCliResult(
        command=command,
        ok=code is SandboxCode.OK,
        code=code,
        ready=ready,
        changed=changed,
        data=data or {},
        blockers=blockers,
    )


async def _read_status(deps: SandboxCliDependencies) -> SandboxStatusSnapshot:
    if deps.read_projection is None:
        return SandboxStatusSnapshot()
    snapshot = await deps.read_projection.read_status()
    if type(snapshot) is not SandboxStatusSnapshot:
        raise TypeError("invalid status projection")
    return snapshot


def _status_data(snapshot: SandboxStatusSnapshot) -> dict[str, object]:
    return {
        "contract": snapshot.contract,
        "vm": snapshot.vm,
        "broker": snapshot.broker,
        "worker": snapshot.worker,
        "execution_count": snapshot.execution_count,
        "raw_inline_code": False,
    }


async def _status(command: SandboxCommand, deps: SandboxCliDependencies) -> SandboxCliResult:
    try:
        snapshot = await _read_status(deps)
    except Exception:
        return _result(command, SandboxCode.READ_FAILED, ready=False)
    return _result(
        command, SandboxCode.OK, ready=snapshot.ready, data=_status_data(snapshot), blockers=snapshot.blockers
    )


async def _doctor(command: SandboxCommand, deps: SandboxCliDependencies) -> SandboxCliResult:
    if deps.doctor is None:
        return _result(command, SandboxCode.READ_FAILED, ready=False)
    try:
        report = await deps.doctor()
        data, ready, blockers = _project_doctor(report.to_mapping())
    except Exception:
        return _result(command, SandboxCode.READ_FAILED, ready=False)
    return _result(command, SandboxCode.OK, ready=ready, data=data, blockers=blockers)


def _project_doctor(mapping: Mapping[str, object]) -> tuple[dict[str, object], bool, tuple[str, ...]]:
    state = mapping.get("state")
    if state not in {"contract_ready", "failed"}:
        raise ValueError("unsupported doctor state")
    scope = mapping.get("scope")
    components = mapping.get("components")
    checks = mapping.get("checks")
    blockers_value = mapping.get("blockers")
    if not isinstance(scope, Mapping) or not isinstance(components, Mapping) or not isinstance(checks, Mapping):
        raise TypeError("invalid doctor mapping")
    if not isinstance(blockers_value, list):
        raise TypeError("invalid doctor blockers")
    blockers = tuple(blocker for blocker in blockers_value if isinstance(blocker, str))
    if len(blockers) != len(blockers_value):
        raise TypeError("invalid doctor blocker")
    for blocker in blockers:
        _require_token(blocker, "blocker")
    component_data: dict[str, str] = {}
    for key in _COMPONENT_KEYS:
        value = components.get(key)
        if not isinstance(value, str):
            raise TypeError("invalid doctor component")
        _require_token(value, "component")
        component_data[key] = value
    check_data = {key: checks.get(key) is True for key in _CHECK_KEYS}
    actual_vm_contacted = scope.get("actual_vm_contacted") is True
    live_ready = scope.get("live_ready") is True
    data: dict[str, object] = {
        "contract_state": state,
        "actual_vm_contacted": actual_vm_contacted,
        "components": component_data,
        "checks": check_data,
        "raw_inline_code": False,
    }
    ready = state == "contract_ready" and actual_vm_contacted and live_ready and not blockers
    return data, ready, blockers


async def _plan(command: SandboxCommand, deps: SandboxCliDependencies) -> SandboxCliResult:
    try:
        snapshot = await _read_status(deps)
    except Exception:
        return _result(command, SandboxCode.READ_FAILED, ready=False)
    data = {
        "mutation": False,
        "owner_local_only": True,
        "raw_inline_code": False,
        "external_bind": False,
        "vm_network": False,
        "templates": [item.value for item in SandboxTemplate],
    }
    return _result(command, SandboxCode.OK, ready=snapshot.ready, data=data, blockers=snapshot.blockers)


def _zero_run_data(template: SandboxTemplate) -> dict[str, object]:
    return {
        "template": template.value,
        "enqueue_count": 0,
        "audit_count": 0,
        "pipe_exchange_count": 0,
        "vm_contact_count": 0,
        "raw_inline_code": False,
    }


async def _run_template(
    command: SandboxCommand,
    template: SandboxTemplate,
    deps: SandboxCliDependencies,
) -> SandboxCliResult:
    try:
        snapshot = await _read_status(deps)
    except Exception:
        return _result(command, SandboxCode.READ_FAILED, ready=False, data=_zero_run_data(template))
    if not snapshot.ready or deps.mutations is None:
        blockers = snapshot.blockers or ("mutation_port_unconfigured",)
        return _result(
            command,
            SandboxCode.SANDBOX_NOT_READY,
            ready=False,
            data=_zero_run_data(template),
            blockers=blockers,
        )
    owner_result = await _authorize_owner(command, snapshot, deps)
    if owner_result is not None:
        return owner_result
    try:
        outcome = await deps.mutations.run_template(template)
    except Exception:
        return _result(command, SandboxCode.MUTATION_FAILED, ready=True, data=_zero_run_data(template))
    return _mutation_result(command, snapshot, outcome, template=template)


async def _jobs(command: SandboxCommand, limit: int, deps: SandboxCliDependencies) -> SandboxCliResult:
    owner_result = await _authorize_owner(command, SandboxStatusSnapshot(), deps)
    if owner_result is not None:
        return owner_result
    if deps.read_projection is None:
        return _result(command, SandboxCode.DATABASE_NOT_FOUND, ready=False, data={"jobs": [], "count": 0})
    try:
        snapshot = await _read_status(deps)
        jobs = await deps.read_projection.list_jobs(limit=limit)
        if not isinstance(jobs, tuple) or not all(type(item) is SandboxJobView for item in jobs):
            raise TypeError("invalid jobs projection")
    except Exception:
        return _result(command, SandboxCode.READ_FAILED, ready=False, data={"jobs": [], "count": 0})
    data = {
        "jobs": [{"job_id": item.job_id, "template": item.template, "state": item.state} for item in jobs],
        "count": len(jobs),
    }
    return _result(command, SandboxCode.OK, ready=snapshot.ready, data=data, blockers=snapshot.blockers)


async def _receipt(command: SandboxCommand, job_id: str, deps: SandboxCliDependencies) -> SandboxCliResult:
    owner_result = await _authorize_owner(command, SandboxStatusSnapshot(), deps)
    if owner_result is not None:
        return owner_result
    if deps.read_projection is None:
        return _result(command, SandboxCode.DATABASE_NOT_FOUND, ready=False, data={"job_id": job_id})
    try:
        snapshot = await _read_status(deps)
        receipt = await deps.read_projection.read_receipt(job_id)
        if receipt is not None and type(receipt) is not SandboxReceiptView:
            raise TypeError("invalid receipt projection")
    except Exception:
        return _result(command, SandboxCode.READ_FAILED, ready=False, data={"job_id": job_id})
    if receipt is None:
        return _result(
            command,
            SandboxCode.RECEIPT_NOT_AVAILABLE,
            ready=snapshot.ready,
            data={"job_id": job_id},
            blockers=snapshot.blockers,
        )
    data = {
        "job_id": receipt.job_id,
        "state": receipt.state,
        "signed": receipt.signed,
        "cleanup_confirmed": receipt.cleanup_confirmed,
        "failure_code": receipt.failure_code,
    }
    return _result(command, SandboxCode.OK, ready=snapshot.ready, data=data, blockers=snapshot.blockers)


async def _cancel(command: SandboxCommand, job_id: str, deps: SandboxCliDependencies) -> SandboxCliResult:
    try:
        snapshot = await _read_status(deps)
    except Exception:
        return _result(command, SandboxCode.READ_FAILED, ready=False, data={"job_id": job_id})
    if deps.mutations is None:
        blockers = snapshot.blockers or ("mutation_port_unconfigured",)
        return _result(
            command,
            SandboxCode.SANDBOX_NOT_READY,
            ready=False,
            data={"job_id": job_id},
            blockers=blockers,
        )
    owner_result = await _authorize_owner(command, snapshot, deps, job_id=job_id)
    if owner_result is not None:
        return owner_result
    try:
        outcome = await deps.mutations.cancel(job_id)
    except Exception:
        return _result(command, SandboxCode.MUTATION_FAILED, ready=snapshot.ready, data={"job_id": job_id})
    return _mutation_result(command, snapshot, outcome)


async def _authorize_owner(
    command: SandboxCommand,
    snapshot: SandboxStatusSnapshot,
    deps: SandboxCliDependencies,
    *,
    job_id: str | None = None,
) -> SandboxCliResult | None:
    data: dict[str, object] = {"job_id": job_id} if job_id is not None else {}
    if deps.owner_authorizer is None:
        return _result(command, SandboxCode.OWNER_AUTH_UNAVAILABLE, ready=snapshot.ready, data=data)
    try:
        allowed = await deps.owner_authorizer.current_actor_is_owner()
    except Exception:
        return _result(command, SandboxCode.OWNER_AUTH_UNAVAILABLE, ready=snapshot.ready, data=data)
    if allowed is not True:
        return _result(command, SandboxCode.OWNER_DENIED, ready=snapshot.ready, data=data)
    return None


def _mutation_result(
    command: SandboxCommand,
    snapshot: SandboxStatusSnapshot,
    outcome: SandboxMutationOutcome,
    *,
    template: SandboxTemplate | None = None,
) -> SandboxCliResult:
    if type(outcome) is not SandboxMutationOutcome:
        return _result(command, SandboxCode.MUTATION_FAILED, ready=snapshot.ready)
    data: dict[str, object] = {
        "job_id": outcome.job_id,
        "state": outcome.state,
        "audit_recorded": outcome.audit_recorded,
    }
    if template is not None:
        data["template"] = template.value
    return _result(
        command,
        outcome.code,
        ready=snapshot.ready,
        changed=outcome.changed,
        data=data,
        blockers=snapshot.blockers,
    )


__all__ = [
    "DoctorRunner",
    "SandboxCliDependencies",
    "SandboxCliResult",
    "SandboxCode",
    "SandboxCommand",
    "SandboxDoctorReport",
    "SandboxJobView",
    "SandboxMutationOutcome",
    "SandboxMutationPort",
    "SandboxOwnerAuthorizer",
    "SandboxReadProjection",
    "SandboxReceiptView",
    "SandboxStatusSnapshot",
    "SandboxTemplate",
    "build_sandbox_parser",
    "dispatch_sandbox_command",
    "parse_sandbox_args",
    "render_sandbox_result",
    "sandbox_exit_code",
]
