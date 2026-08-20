from __future__ import annotations

import asyncio
import re
from argparse import Namespace
from dataclasses import replace
from typing import Any, Callable, Literal

import discord
from discord import app_commands

from yonerai_discord.control_plane import RbacLevel
from yonerai_discord.runtime_manifests.capability_forge import SANDBOX_COMMAND_CAPABILITY_IDS
from yonerai_discord.runtime_readiness import refresh_runtime_readiness
from yonerai_discord.sandbox_operator_cli import (
    SandboxCliDependencies,
    SandboxCliResult,
    SandboxCode,
    SandboxCommand,
    SandboxTemplate,
    dispatch_sandbox_command,
)


NO_MENTIONS = discord.AllowedMentions.none()
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_VISIBLE_JOBS = 5


class DiscordSandboxAuthorizer:
    """Bind one command invocation to the current bot, interaction, owner, and capability."""

    def __init__(
        self,
        *,
        bot: Any,
        interaction: discord.Interaction,
        capability_id: str,
        current: Callable[[], bool],
    ) -> None:
        self._bot = bot
        self._interaction = interaction
        self._capability_id = capability_id
        self._current = current

    async def current_actor_is_owner(self) -> bool:
        """Discord identity is resolved fresh and never caller supplied."""
        return await self.authorize_interaction()

    async def authorize_interaction(self) -> bool:
        if not self._is_current():
            return False
        owner_id = self._configured_owner_id()
        user = getattr(self._interaction, "user", None)
        if owner_id is None or getattr(user, "id", None) != owner_id:
            return False
        is_owner = getattr(self._bot, "is_owner", None)
        if not callable(is_owner):
            return False
        try:
            if (await is_owner(user)) is not True:
                return False
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        guard = getattr(self._bot, "capability_guard", None)
        currently_allowed = getattr(guard, "currently_allowed", None)
        if not callable(currently_allowed):
            return False
        try:
            # Re-evaluate production transport readiness for the exact route at
            # the moment an interaction is authorized.  A stale startup value
            # must never keep a VM mutation executable.
            refresh_runtime_readiness(self._bot, self._capability_id)
            return (
                currently_allowed(
                    self._capability_id,
                    guild_id=getattr(self._interaction, "guild_id", None),
                    user_id=owner_id,
                    actor_level=RbacLevel.BOT_OWNER,
                    floor=RbacLevel.BOT_OWNER,
                )
                is True
            )
        except Exception:
            return False

    def _is_current(self) -> bool:
        try:
            if self._current() is not True:
                return False
        except Exception:
            return False
        if getattr(self._interaction, "client", None) is not self._bot:
            return False
        if bool(getattr(self._bot, "is_closing", False)):
            return False
        is_closed = getattr(self._bot, "is_closed", None)
        try:
            return not (callable(is_closed) and bool(is_closed()))
        except Exception:
            return False

    def _configured_owner_id(self) -> int | None:
        owner_ids = getattr(getattr(self._bot, "settings", None), "bot_owner_ids", None)
        if not isinstance(owner_ids, (frozenset, set, tuple, list)) or len(owner_ids) != 1:
            return None
        owner_id = next(iter(owner_ids))
        if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0:
            return None
        return owner_id


class SandboxGroup(app_commands.Group):
    """Owner-only, content-free projection over the existing sandbox operator contract."""

    def __init__(
        self,
        *,
        bot: Any,
        dependencies: SandboxCliDependencies,
        current: Callable[[], bool],
        bind_dependencies: Callable[[discord.Interaction, SandboxCliDependencies], SandboxCliDependencies]
        | None = None,
    ) -> None:
        super().__init__(name="sandbox", description="Execution Sandbox operator controls")
        self._bot = bot
        self._dependencies = dependencies
        self._bind_dependencies = bind_dependencies
        self._current = current
        self._closing = False

    def begin_close(self) -> None:
        self._closing = True

    @app_commands.command(name="status", description="Show bounded sandbox readiness")
    async def status(self, interaction: discord.Interaction) -> None:
        await self._dispatch(interaction, SandboxCommand.STATUS)

    @app_commands.command(name="doctor", description="Run the offline sandbox contract doctor")
    async def doctor(self, interaction: discord.Interaction) -> None:
        await self._dispatch(interaction, SandboxCommand.DOCTOR)

    @app_commands.command(name="plan", description="Show the fixed no-raw-code execution plan")
    async def plan(self, interaction: discord.Interaction) -> None:
        await self._dispatch(interaction, SandboxCommand.PLAN)

    @app_commands.command(name="run-template", description="Request one fixed sandbox template")
    async def run_template(
        self,
        interaction: discord.Interaction,
        template: Literal["python-smoke"] = "python-smoke",
    ) -> None:
        await self._dispatch(interaction, SandboxCommand.RUN_TEMPLATE, template=template)

    @app_commands.command(name="jobs", description="List bounded sandbox job summaries")
    async def jobs(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        await self._dispatch(interaction, SandboxCommand.JOBS, limit=int(limit))

    @app_commands.command(name="receipt", description="Show one bounded sandbox receipt")
    async def receipt(self, interaction: discord.Interaction, job_id: str) -> None:
        await self._dispatch(interaction, SandboxCommand.RECEIPT, job_id=job_id)

    @app_commands.command(name="cancel", description="Request cancellation of one sandbox job")
    async def cancel(self, interaction: discord.Interaction, job_id: str) -> None:
        await self._dispatch(interaction, SandboxCommand.CANCEL, job_id=job_id)

    async def _dispatch(
        self,
        interaction: discord.Interaction,
        command: SandboxCommand,
        *,
        template: str | None = None,
        limit: int = 10,
        job_id: str | None = None,
    ) -> None:
        capability_id = SANDBOX_COMMAND_CAPABILITY_IDS[command.value]
        authorizer = DiscordSandboxAuthorizer(
            bot=self._bot,
            interaction=interaction,
            capability_id=capability_id,
            current=self._is_current,
        )
        if not await authorizer.authorize_interaction():
            await _reply(interaction, _fixed_failure(command, SandboxCode.OWNER_DENIED))
            return
        if command is SandboxCommand.RUN_TEMPLATE and template != SandboxTemplate.PYTHON_SMOKE.value:
            await _reply(interaction, _fixed_failure(command, SandboxCode.SANDBOX_NOT_READY))
            return
        if command in {SandboxCommand.RECEIPT, SandboxCommand.CANCEL}:
            if not isinstance(job_id, str) or _SAFE_JOB_ID.fullmatch(job_id) is None:
                await _reply(interaction, _fixed_failure(command, SandboxCode.JOB_NOT_FOUND))
                return
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            await _reply(interaction, _fixed_failure(command, SandboxCode.READ_FAILED))
            return
        if command in {SandboxCommand.RUN_TEMPLATE, SandboxCommand.CANCEL}:
            response = getattr(interaction, "response", None)
            defer = getattr(response, "defer", None)
            try:
                if response is None or response.is_done() or not callable(defer):
                    await _reply(interaction, _fixed_failure(command, SandboxCode.MUTATION_FAILED))
                    return
                await defer(ephemeral=True, thinking=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                # No VM mutation is allowed if Discord did not accept the
                # bounded long-running interaction acknowledgement.
                return
        args = Namespace(
            sandbox_command=command.value,
            template=template or SandboxTemplate.PYTHON_SMOKE.value,
            limit=limit,
            job_id=job_id,
        )
        dependencies = self._dependencies
        binder = self._bind_dependencies
        if binder is not None:
            try:
                dependencies = binder(interaction, dependencies)
            except asyncio.CancelledError:
                raise
            except Exception:
                await _reply(interaction, _fixed_failure(command, SandboxCode.READ_FAILED))
                return
            if type(dependencies) is not SandboxCliDependencies:
                await _reply(interaction, _fixed_failure(command, SandboxCode.READ_FAILED))
                return
        dependencies = replace(dependencies, owner_authorizer=authorizer)
        try:
            result = await dispatch_sandbox_command(args, dependencies)
        except asyncio.CancelledError:
            raise
        except Exception:
            await _reply(interaction, _fixed_failure(command, SandboxCode.READ_FAILED))
            return
        if not await authorizer.authorize_interaction():
            await _reply(interaction, _fixed_failure(command, SandboxCode.OWNER_DENIED))
            return
        await _reply(interaction, _render_result(result))

    def _is_current(self) -> bool:
        return self._closing is False and self._current() is True


def _fixed_failure(command: SandboxCommand, code: SandboxCode) -> str:
    return "\n".join(("Execution Sandbox", f"command: {command.value}", f"code: {code.value}", "changed: false"))


def _render_result(result: SandboxCliResult) -> str:
    lines = [
        "Execution Sandbox",
        f"command: {result.command.value}",
        f"code: {result.code.value}",
        f"ready: {str(result.ready).lower()}",
        f"changed: {str(result.changed).lower()}",
    ]
    data = result.data
    if result.command is SandboxCommand.STATUS:
        lines.extend(
            (
                f"contract: {data.get('contract', 'unavailable')}",
                f"vm: {data.get('vm', 'unavailable')}",
                f"broker: {data.get('broker', 'unavailable')}",
                f"worker: {data.get('worker', 'unavailable')}",
                f"execution_count: {data.get('execution_count', 0)}",
            )
        )
    elif result.command is SandboxCommand.DOCTOR:
        lines.extend(
            (
                f"contract_state: {data.get('contract_state', 'unavailable')}",
                f"actual_vm_contacted: {str(data.get('actual_vm_contacted') is True).lower()}",
            )
        )
    elif result.command is SandboxCommand.PLAN:
        lines.extend(("template: python-smoke", "raw_inline_code: false", "mutation: false"))
    elif result.command is SandboxCommand.JOBS:
        jobs = data.get("jobs")
        lines.append(f"count: {data.get('count', 0)}")
        if isinstance(jobs, list):
            for item in jobs[:_MAX_VISIBLE_JOBS]:
                if isinstance(item, dict):
                    lines.append(
                        f"job: {item.get('job_id', 'unavailable')} {item.get('template', 'unavailable')} "
                        f"{item.get('state', 'unavailable')}"
                    )
    else:
        if isinstance(data.get("job_id"), str):
            lines.append(f"job_id: {data['job_id']}")
        if isinstance(data.get("state"), str):
            lines.append(f"state: {data['state']}")
        if result.command is SandboxCommand.RUN_TEMPLATE:
            lines.append("template: python-smoke")
        if result.command is SandboxCommand.RECEIPT:
            lines.extend(
                (
                    f"signed: {str(data.get('signed') is True).lower()}",
                    f"cleanup_confirmed: {str(data.get('cleanup_confirmed') is True).lower()}",
                )
            )
    if result.blockers:
        lines.append("blockers: " + ",".join(result.blockers[:5]))
    return "\n".join(lines)[:1_900]


async def _reply(interaction: discord.Interaction, content: str) -> None:
    kwargs = {"ephemeral": True, "allowed_mentions": NO_MENTIONS}
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


__all__ = ["DiscordSandboxAuthorizer", "SandboxGroup"]
