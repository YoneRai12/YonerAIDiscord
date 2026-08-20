from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")

from yonerai_discord.control_plane import RbacLevel  # noqa: E402
from yonerai_discord.modules.capability_forge import CapabilityForgePlugin  # noqa: E402
from yonerai_discord.modules.capability_forge.sandbox_adapter import SandboxGroup  # noqa: E402
from yonerai_discord.runtime_manifests.capability_forge import (  # noqa: E402
    SANDBOX_COMMAND_CAPABILITY_IDS,
)
from yonerai_discord.runtime_readiness import refresh_runtime_readiness  # noqa: E402
from yonerai_discord.sandbox_operator_cli import (  # noqa: E402
    SandboxCliDependencies,
    SandboxJobView,
    SandboxMutationOutcome,
    SandboxReceiptView,
    SandboxStatusSnapshot,
)


class _Guard:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.registry: object | None = None
        self.calls: list[tuple[str, dict[str, object]]] = []

    def currently_allowed(self, capability_id: str, **values: object) -> bool:
        self.calls.append((capability_id, values))
        return self.allowed


class _Tree:
    def __init__(self, *, fail_after_add: bool = False) -> None:
        self.commands: dict[str, object] = {}
        self.fail_after_add = fail_after_add
        self.adds = 0
        self.removes = 0

    def add_command(self, command: object) -> None:
        self.adds += 1
        self.commands[command.name] = command  # type: ignore[attr-defined]
        if self.fail_after_add:
            raise RuntimeError("fixed add failure")

    def get_command(self, name: str, *, type: object) -> object | None:
        assert type is discord.AppCommandType.chat_input
        return self.commands.get(name)

    def remove_command(self, name: str, *, type: object) -> object | None:
        assert type is discord.AppCommandType.chat_input
        self.removes += 1
        return self.commands.pop(name, None)


class _Bot:
    def __init__(self, *, owner_ids: frozenset[int] = frozenset({42}), tree: _Tree | None = None) -> None:
        self.settings = SimpleNamespace(bot_owner_ids=owner_ids, database_path=Path("unused.sqlite3"))
        self.capability_registry = object()
        self.capability_guard = _Guard()
        self.capability_guard.registry = self.capability_registry
        self.tree = tree or _Tree()
        self.is_closing = False
        self.owner_allowed = True
        self.owner_calls = 0
        self.dynamic: list[object] = []

    async def is_owner(self, user: object) -> bool:
        self.owner_calls += 1
        return self.owner_allowed and getattr(user, "id", None) == 42

    def is_closed(self) -> bool:
        return False

    def add_dynamic_items(self, *items: object) -> None:
        self.dynamic.extend(items)

    def remove_dynamic_items(self, *items: object) -> None:
        for item in items:
            self.dynamic.remove(item)


class _Interaction:
    def __init__(self, bot: _Bot, *, user_id: int = 42, guild_id: int = 9001, interaction_id: int = 1234) -> None:
        self.client = bot
        self.user = SimpleNamespace(id=user_id)
        self.id = interaction_id
        self.guild_id = guild_id
        self.channel_id = 7001
        self._response_done = False
        self.deferred: list[dict[str, object]] = []
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.response = SimpleNamespace(
            is_done=lambda: self._response_done,
            send_message=self._send,
            defer=self._defer,
        )
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))

    async def _defer(self, **kwargs: object) -> None:
        self._response_done = True
        self.deferred.append(kwargs)


class _DoctorReport:
    def to_mapping(self) -> dict[str, object]:
        return {
            "state": "contract_ready",
            "scope": {"actual_vm_contacted": False, "live_ready": False},
            "components": {
                "transport": "implemented_offline",
                "signed_job": "implemented_offline",
                "signed_receipt": "implemented_offline",
                "durable_replay": "implemented_offline",
                "trusted_broker": "unconfigured",
                "guest_worker": "unconfigured",
                "vm_lifecycle": "unconfigured",
            },
            "checks": {
                "typed_status": True,
                "exact_binding": True,
                "receipt_integrity": True,
                "artifact_ownership": True,
                "audit": True,
                "timeout_cleanup": True,
                "cancel_cleanup": True,
            },
            "blockers": ["actual_vm_absent"],
        }


async def _doctor() -> _DoctorReport:
    return _DoctorReport()


class _Projection:
    def __init__(self, *, ready: bool = False, revoke: _Guard | None = None) -> None:
        self.ready = ready
        self.revoke = revoke
        self.reads = 0

    async def read_status(self) -> SandboxStatusSnapshot:
        self.reads += 1
        if self.revoke is not None:
            self.revoke.allowed = False
        return SandboxStatusSnapshot(
            ready=self.ready,
            contract="live_verified" if self.ready else "implemented_offline",
            vm="running" if self.ready else "absent",
            broker="ready" if self.ready else "unconfigured",
            worker="ready" if self.ready else "unconfigured",
            execution_count=0,
            blockers=() if self.ready else ("actual_vm_absent",),
        )

    async def list_jobs(self, *, limit: int) -> tuple[SandboxJobView, ...]:
        del limit
        return (SandboxJobView("job-1", "python-smoke", "pending"),)

    async def read_receipt(self, job_id: str) -> SandboxReceiptView | None:
        return SandboxReceiptView(job_id, "succeeded", True, True)


class _Mutations:
    def __init__(self) -> None:
        self.run_calls = 0
        self.cancel_calls = 0

    async def run_template(self, template: object) -> SandboxMutationOutcome:
        del template
        self.run_calls += 1
        raise AssertionError("unready surface must not mutate")

    async def cancel(self, job_id: str) -> SandboxMutationOutcome:
        del job_id
        self.cancel_calls += 1
        raise AssertionError("unready surface must not mutate")


def _group(bot: _Bot, dependencies: SandboxCliDependencies | None = None) -> SandboxGroup:
    group: SandboxGroup
    group = SandboxGroup(
        bot=bot,
        dependencies=dependencies or SandboxCliDependencies(doctor=_doctor),
        current=lambda: not group._closing,
    )
    return group


async def _invoke(group: SandboxGroup, command: str, interaction: _Interaction) -> None:
    if command == "run-template":
        await group.run_template.callback(group, interaction, "python-smoke")
    elif command == "jobs":
        await group.jobs.callback(group, interaction, 10)
    elif command in {"receipt", "cancel"}:
        await getattr(group, command).callback(group, interaction, "job-1")
    else:
        await getattr(group, command).callback(group, interaction)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", tuple(SANDBOX_COMMAND_CAPABILITY_IDS))
async def test_all_seven_routes_are_owner_only_ephemeral_and_use_exact_capability(command: str) -> None:
    bot = _Bot()
    interaction = _Interaction(bot)
    await _invoke(_group(bot), command, interaction)

    assert len(interaction.messages) == 1
    content, kwargs = interaction.messages[0]
    assert f"command: {command}" in content
    assert len(content) <= 1_900
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].everyone is False
    assert {capability_id for capability_id, _ in bot.capability_guard.calls} == {
        SANDBOX_COMMAND_CAPABILITY_IDS[command]
    }
    assert all(
        values
        == {
            "guild_id": 9001,
            "user_id": 42,
            "actor_level": RbacLevel.BOT_OWNER,
            "floor": RbacLevel.BOT_OWNER,
        }
        for _, values in bot.capability_guard.calls
    )


@pytest.mark.asyncio
async def test_non_owner_revoke_close_and_identity_drift_fail_closed() -> None:
    bot = _Bot()
    group = _group(bot)
    non_owner = _Interaction(bot, user_id=41)
    await group.status.callback(group, non_owner)
    assert "OWNER_DENIED" in non_owner.messages[0][0]

    bot.owner_allowed = False
    denied = _Interaction(bot)
    await group.plan.callback(group, denied)
    assert "OWNER_DENIED" in denied.messages[0][0]

    bot.owner_allowed = True
    bot.is_closing = True
    closing = _Interaction(bot)
    await group.status.callback(group, closing)
    assert "OWNER_DENIED" in closing.messages[0][0]

    bot.is_closing = False
    mismatched = _Interaction(_Bot())
    await group.status.callback(group, mismatched)
    assert "OWNER_DENIED" in mismatched.messages[0][0]


@pytest.mark.asyncio
async def test_capability_revoke_after_projection_hides_stale_result() -> None:
    bot = _Bot()
    projection = _Projection(revoke=bot.capability_guard)
    group = _group(bot, SandboxCliDependencies(read_projection=projection, doctor=_doctor))
    interaction = _Interaction(bot)
    await group.status.callback(group, interaction)

    assert projection.reads == 1
    content = interaction.messages[0][0]
    assert "OWNER_DENIED" in content
    assert "contract:" not in content
    assert "vm:" not in content


@pytest.mark.asyncio
async def test_unready_run_and_cancel_have_exact_zero_mutation_and_no_raw_code() -> None:
    bot = _Bot()
    mutations = _Mutations()
    dependencies = SandboxCliDependencies(read_projection=_Projection(), doctor=_doctor, mutations=mutations)
    group = _group(bot, dependencies)

    run = _Interaction(bot)
    await group.run_template.callback(group, run, "python-smoke")
    cancel = _Interaction(bot)
    await group.cancel.callback(group, cancel, "job-1")

    assert mutations.run_calls == mutations.cancel_calls == 0
    assert "SANDBOX_NOT_READY" in run.messages[0][0]
    assert "SANDBOX_NOT_READY" in cancel.messages[0][0]
    assert run.deferred == cancel.deferred == [{"ephemeral": True, "thinking": True}]
    assert "raw" not in (run.messages[0][0] + cancel.messages[0][0]).lower()


@pytest.mark.asyncio
async def test_mutation_never_starts_when_discord_defer_fails() -> None:
    bot = _Bot()
    mutations = _Mutations()
    group = _group(
        bot,
        SandboxCliDependencies(
            read_projection=_Projection(ready=True),
            doctor=_doctor,
            mutations=mutations,
        ),
    )
    interaction = _Interaction(bot)

    async def fail_defer(**_kwargs: object) -> None:
        raise RuntimeError("Discord acknowledgement failed")

    interaction.response.defer = fail_defer
    await group.run_template.callback(group, interaction, "python-smoke")

    assert mutations.run_calls == 0
    assert interaction.messages == []


@pytest.mark.asyncio
async def test_dependency_binder_receives_each_exact_interaction_and_cannot_leak_failures() -> None:
    bot = _Bot()
    seen: list[tuple[int, int]] = []

    def bind(interaction: _Interaction, dependencies: SandboxCliDependencies) -> SandboxCliDependencies:
        seen.append((interaction.id, interaction.guild_id))
        return SandboxCliDependencies(read_projection=_Projection(ready=True), doctor=dependencies.doctor)

    group: SandboxGroup
    group = SandboxGroup(
        bot=bot,
        dependencies=SandboxCliDependencies(doctor=_doctor),
        current=lambda: not group._closing,
        bind_dependencies=bind,
    )
    first = _Interaction(bot, guild_id=9001, interaction_id=1001)
    second = _Interaction(bot, guild_id=9002, interaction_id=2001)
    await group.status.callback(group, first)
    await group.status.callback(group, second)

    assert seen == [(1001, 9001), (2001, 9002)]
    assert all("ready: true" in interaction.messages[0][0] for interaction in (first, second))

    def broken(_interaction: _Interaction, _dependencies: SandboxCliDependencies) -> SandboxCliDependencies:
        raise RuntimeError("private/.env sk-secret")

    blocked: SandboxGroup
    blocked = SandboxGroup(
        bot=bot,
        dependencies=SandboxCliDependencies(doctor=_doctor),
        current=lambda: not blocked._closing,
        bind_dependencies=broken,
    )
    failed = _Interaction(bot)
    await blocked.status.callback(blocked, failed)
    assert "READ_FAILED" in failed.messages[0][0]
    assert "private" not in failed.messages[0][0] and "secret" not in failed.messages[0][0]


@pytest.mark.asyncio
async def test_errors_and_invalid_identifiers_never_expose_source_path_secret_or_exception() -> None:
    # Keep the public source free of a host-path literal while preserving the runtime leak regression.
    source_path = "".join(("C", ":/", "private/source/.env"))

    async def broken_doctor() -> _DoctorReport:
        raise RuntimeError(f"{source_path} sk-secret")

    bot = _Bot()
    group = _group(bot, SandboxCliDependencies(doctor=broken_doctor))
    doctor = _Interaction(bot)
    await group.doctor.callback(group, doctor)
    invalid = _Interaction(bot)
    await group.receipt.callback(group, invalid, "../.env")
    visible = repr(doctor.messages + invalid.messages).lower()
    assert source_path.lower() not in visible
    assert "sk-secret" not in visible
    assert ".env" not in visible
    assert "runtimeerror" not in visible


@pytest.mark.asyncio
async def test_plugin_registers_once_binds_identity_and_removes_exact_group(tmp_path: Path) -> None:
    bot = _Bot()
    bot.settings.database_path = tmp_path / "forge.sqlite3"
    plugin = CapabilityForgePlugin()
    await plugin.start(bot)
    group = plugin.sandbox_group
    assert group is not None
    assert bot.tree.adds == 1
    assert bot.tree.get_command("sandbox", type=discord.AppCommandType.chat_input) is group
    assert bot.runtime_capability_readiness[SANDBOX_COMMAND_CAPABILITY_IDS["status"]] is True
    assert bot.runtime_capability_readiness[SANDBOX_COMMAND_CAPABILITY_IDS["jobs"]] is True
    assert bot.runtime_capability_readiness[SANDBOX_COMMAND_CAPABILITY_IDS["receipt"]] is True
    assert bot.runtime_capability_readiness[SANDBOX_COMMAND_CAPABILITY_IDS["run-template"]] is False
    assert bot.runtime_capability_readiness[SANDBOX_COMMAND_CAPABILITY_IDS["cancel"]] is False

    bot.capability_guard.registry = object()
    drifted = _Interaction(bot)
    await group.status.callback(group, drifted)
    assert "OWNER_DENIED" in drifted.messages[0][0]

    await plugin.stop()
    assert bot.tree.commands == {}
    assert bot.tree.removes == 1
    assert not hasattr(bot, "capability_forge_sandbox_group")


@pytest.mark.asyncio
async def test_backend_routes_refresh_from_current_runtime_probe_and_withdraw_on_stop(tmp_path: Path) -> None:
    bot = _Bot()
    bot.settings.database_path = tmp_path / "forge.sqlite3"
    plugin = CapabilityForgePlugin()
    await plugin.start(bot)
    runtime = plugin.sandbox_runtime
    assert runtime is not None
    capability_id = SANDBOX_COMMAND_CAPABILITY_IDS["run-template"]
    assert refresh_runtime_readiness(bot, capability_id) is False

    runtime._backend_ready = lambda: True
    assert refresh_runtime_readiness(bot, capability_id) is True
    runtime._backend_ready = lambda: False
    assert refresh_runtime_readiness(bot, capability_id) is False

    await plugin.stop()
    assert refresh_runtime_readiness(bot, capability_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ("guard", "registry"))
async def test_incomplete_control_plane_keeps_read_routes_unready(tmp_path: Path, missing: str) -> None:
    bot = _Bot()
    bot.settings.database_path = tmp_path / "forge.sqlite3"
    if missing == "guard":
        bot.capability_guard = None
    else:
        bot.capability_registry = None
    plugin = CapabilityForgePlugin()
    await plugin.start(bot)
    assert all(
        bot.runtime_capability_readiness[capability_id] is False
        for capability_id in SANDBOX_COMMAND_CAPABILITY_IDS.values()
    )
    await plugin.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_ids", (frozenset(), frozenset({42, 43})))
async def test_invalid_owner_configuration_keeps_every_sandbox_route_unready(
    tmp_path: Path,
    owner_ids: frozenset[int],
) -> None:
    bot = _Bot(owner_ids=owner_ids)
    bot.settings.database_path = tmp_path / "forge.sqlite3"
    plugin = CapabilityForgePlugin()
    await plugin.start(bot)
    assert all(
        bot.runtime_capability_readiness[capability_id] is False
        for capability_id in SANDBOX_COMMAND_CAPABILITY_IDS.values()
    )
    await plugin.stop()


@pytest.mark.asyncio
async def test_partial_tree_registration_failure_rolls_back_every_resource(tmp_path: Path) -> None:
    tree = _Tree(fail_after_add=True)
    bot = _Bot(tree=tree)
    bot.settings.database_path = tmp_path / "forge.sqlite3"
    plugin = CapabilityForgePlugin()
    with pytest.raises(RuntimeError, match="fixed add failure"):
        await plugin.start(bot)
    assert tree.commands == {}
    assert plugin.repository is None
    assert plugin.sandbox_group is None
    assert bot.runtime_capability_readiness == {}


def test_command_capability_map_is_exact_and_immutable() -> None:
    assert tuple(SANDBOX_COMMAND_CAPABILITY_IDS) == (
        "status",
        "doctor",
        "plan",
        "run-template",
        "jobs",
        "receipt",
        "cancel",
    )
    with pytest.raises(TypeError):
        SANDBOX_COMMAND_CAPABILITY_IDS["status"] = "changed"  # type: ignore[index]
