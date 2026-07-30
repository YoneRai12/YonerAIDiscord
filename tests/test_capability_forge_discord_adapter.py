from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")

import yonerai_discord.modules.capability_forge as capability_forge_plugin  # noqa: E402
from yonerai_discord.capability_forge.discord_adapter import (  # noqa: E402
    DiscordForgeAdapter,
    DiscordForgeAuthorizer,
    DiscordOwnerDmPort,
    ForgeDecisionButton,
)
from yonerai_discord.capability_forge.lifecycle import CandidateKind, TemplateIdentity  # noqa: E402
from yonerai_discord.capability_forge.owner_notification import (  # noqa: E402
    OwnerDecisionAction,
    OwnerDecisionButton,
    OwnerDecisionStatus,
    OwnerNotificationCard,
)
from yonerai_discord.capability_forge.sandbox_contract import SandboxCandidate, SandboxScope  # noqa: E402
from yonerai_discord.capability_forge.sandbox_service import SandboxRunStatus  # noqa: E402
from yonerai_discord.control_plane import RbacLevel  # noqa: E402
from yonerai_discord.modules.operations import (  # noqa: E402
    InteractionFailureDelivery,
    InteractionFailureTerminal,
)
from yonerai_discord.modules.capability_forge import CapabilityForgePlugin  # noqa: E402
from yonerai_discord.runtime_manifests.capability_forge import FORGE_OWNER_NOTIFICATION_CAPABILITY_ID  # noqa: E402


DIGEST = "a" * 64


class _Guard:
    def __init__(self, allowed: object = True) -> None:
        self.allowed = allowed
        self.calls: list[dict[str, object]] = []

    def currently_allowed(self, capability_id: str, **values: object) -> object:
        self.calls.append({"capability_id": capability_id, **values})
        return self.allowed


class _User:
    def __init__(self, user_id: int = 42) -> None:
        self.id = user_id
        self.sent: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def send(self, *args: object, **kwargs: object) -> None:
        self.sent.append((args, kwargs))


class _Database:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, object]]] = []

    def append_audit(self, event: str, **kwargs: object) -> None:
        self.rows.append((event, kwargs))


class _Bot:
    def __init__(self, *, owner_ids: frozenset[int] = frozenset({42}), allowed: object = True) -> None:
        self.settings = SimpleNamespace(bot_owner_ids=owner_ids, database_path=Path("unused.sqlite3"))
        self.user = _User()
        self.capability_guard = _Guard(allowed)
        self.is_closing = False
        self.is_owner_result = True
        self.owner_calls = 0
        self.database = _Database()
        self.interaction_failure_terminal = InteractionFailureTerminal()

    def get_user(self, user_id: int) -> _User | None:
        return self.user if user_id == self.user.id else None

    async def is_owner(self, user: _User) -> bool:
        self.owner_calls += 1
        return self.is_owner_result and user.id == 42


def _card(
    candidate_kind: CandidateKind = CandidateKind.SEALED_RECIPE,
) -> OwnerNotificationCard:
    templates = {
        CandidateKind.SANDBOX_PYTHON_PURE: (TemplateIdentity("python_pure", "1"),),
        CandidateKind.SANDBOX_BROWSER_READONLY: (TemplateIdentity("browser_readonly", "1"),),
    }.get(candidate_kind, ())
    return OwnerNotificationCard(
        recipe_digest=DIGEST,
        expected_revision=2,
        code_owned_description="Code-owned safe recipe description",
        templates=templates,
        actions=(
            OwnerDecisionButton(OwnerDecisionAction.KEEP, f"cf1:k:{DIGEST}:2"),
            OwnerDecisionButton(OwnerDecisionAction.REJECT, f"cf1:r:{DIGEST}:2"),
            OwnerDecisionButton(OwnerDecisionAction.PROMOTE_REQUESTED, f"cf1:p:{DIGEST}:2"),
        ),
        candidate_kind=candidate_kind,
    )


async def test_owner_dm_rechecks_exact_settings_discord_owner_and_current_capability_before_send() -> None:
    bot = _Bot()
    authorizer = DiscordForgeAuthorizer(bot)
    port = DiscordOwnerDmPort(authorizer)

    await port.send_private_owner_card(
        owner_user_id=42,
        card=_card(),
        idempotency_key=f"forge-owner-notification:v1:{DIGEST}",
        allowed_mentions=(),
    )

    assert len(bot.user.sent) == 1
    bot.capability_guard.allowed = True
    bot.is_owner_result = False
    with pytest.raises(PermissionError):
        await port.send_private_owner_card(
            owner_user_id=42,
            card=_card(),
            idempotency_key=f"forge-owner-notification:v1:{DIGEST}",
            allowed_mentions=(),
        )
    assert len(bot.user.sent) == 1
    _, kwargs = bot.user.sent[0]
    assert kwargs["allowed_mentions"].everyone is False
    assert bot.owner_calls == 2
    assert bot.capability_guard.calls == [
        {
            "capability_id": FORGE_OWNER_NOTIFICATION_CAPABILITY_ID,
            "guild_id": None,
            "user_id": 42,
            "actor_level": RbacLevel.BOT_OWNER,
            "floor": RbacLevel.BOT_OWNER,
        }
    ]

    bot.settings.bot_owner_ids = frozenset({42, 43})
    with pytest.raises(PermissionError):
        await port.send_private_owner_card(
            owner_user_id=42,
            card=_card(),
            idempotency_key=f"forge-owner-notification:v1:{DIGEST}",
            allowed_mentions=(),
        )
    assert len(bot.user.sent) == 1

    bot.settings.bot_owner_ids = frozenset({42})
    bot.is_owner_result = True
    bot.capability_guard.allowed = False
    with pytest.raises(PermissionError):
        await port.send_private_owner_card(
            owner_user_id=42,
            card=_card(),
            idempotency_key=f"forge-owner-notification:v1:{DIGEST}",
            allowed_mentions=(),
        )
    assert len(bot.user.sent) == 1
    with pytest.raises(ValueError, match="contract"):
        await port.send_private_owner_card(
            owner_user_id=42,
            card=_card(),
            idempotency_key="forge-owner-notification:v1:wrong",
            allowed_mentions=(),
        )


async def test_sandbox_owner_dm_renders_only_fixed_candidate_kind_label() -> None:
    bot = _Bot()
    port = DiscordOwnerDmPort(DiscordForgeAuthorizer(bot))

    await port.send_private_owner_card(
        owner_user_id=42,
        card=_card(CandidateKind.SANDBOX_PYTHON_PURE),
        idempotency_key=f"forge-owner-notification:v1:{DIGEST}",
        allowed_mentions=(),
    )

    rendered = bot.user.sent[0][0][0]
    assert isinstance(rendered, str)
    assert "Kind: Sandbox Python pure" in rendered
    assert "Templates: python_pure@1" in rendered
    assert CandidateKind.SANDBOX_PYTHON_PURE.value not in rendered

    await port.send_private_owner_card(
        owner_user_id=42,
        card=_card(CandidateKind.SANDBOX_BROWSER_READONLY),
        idempotency_key=f"forge-owner-notification:v1:{DIGEST}",
        allowed_mentions=(),
    )
    browser_rendered = bot.user.sent[1][0][0]
    assert "Kind: Sandbox browser read-only" in browser_rendered
    assert "Templates: browser_readonly@1" in browser_rendered
    assert CandidateKind.SANDBOX_BROWSER_READONLY.value not in browser_rendered


class _Interaction:
    def __init__(self, user: _User, *, client: object | None = None, interaction_id: int = 501) -> None:
        self.id = interaction_id
        self.guild_id = None
        self.user = user
        self.client = client
        self.response = SimpleNamespace(is_done=lambda: False, send_message=self._send)
        self.followup = SimpleNamespace(send=self._send)
        self.responses: list[tuple[str, dict[str, object]]] = []

    async def _send(self, message: str, **kwargs: object) -> None:
        self.responses.append((message, kwargs))


class _Service:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def apply_owner_decision(self, request: object) -> object:
        self.requests.append(request)
        return SimpleNamespace(status=OwnerDecisionStatus.APPLIED)


class _FailingService:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def apply_owner_decision(self, request: object) -> object:
        del request
        self.calls += 1
        raise self.error


async def test_button_side_effect_requires_fresh_owner_and_policy_and_remains_proposal_only() -> None:
    bot = _Bot()
    service = _Service()
    adapter = DiscordForgeAdapter(authorizer=DiscordForgeAuthorizer(bot), service=service)
    interaction = _Interaction(bot.user)

    await adapter.apply_interaction(interaction, f"cf1:p:{DIGEST}:2")

    assert len(service.requests) == 1
    assert service.requests[0].action is OwnerDecisionAction.PROMOTE_REQUESTED
    assert "proposal" in interaction.responses[0][0]
    assert interaction.responses[0][1]["ephemeral"] is True
    assert bot.owner_calls == 1

    bot.is_closing = True
    await adapter.apply_interaction(interaction, f"cf1:k:{DIGEST}:2")
    assert len(service.requests) == 1

    bot.is_closing = False
    bot.capability_guard.allowed = False
    await adapter.apply_interaction(interaction, f"cf1:k:{DIGEST}:2")
    assert len(service.requests) == 1

    bot.capability_guard.allowed = True
    bot.is_owner_result = False
    await adapter.apply_interaction(interaction, f"cf1:k:{DIGEST}:2")
    assert len(service.requests) == 1


async def test_dynamic_callback_unexpected_error_uses_bot_terminal_without_secret_or_success_claim() -> None:
    bot = _Bot()
    service = _FailingService(RuntimeError("secret decision body"))
    adapter = DiscordForgeAdapter(authorizer=DiscordForgeAuthorizer(bot), service=service)
    interaction = _Interaction(bot.user, client=bot)
    button = ForgeDecisionButton(f"cf1:p:{DIGEST}:2", "Promote request")
    previous = ForgeDecisionButton.adapter
    ForgeDecisionButton.adapter = adapter
    try:
        await button.callback(interaction)
    finally:
        ForgeDecisionButton.adapter = previous

    receipt = bot.interaction_failure_terminal.receipt_for(
        surface="capability_forge_decision",
        interaction_id=interaction.id,
    )
    assert receipt is not None
    assert receipt.delivery is InteractionFailureDelivery.DELIVERED
    assert service.calls == 1
    assert len(interaction.responses) == 1
    visible, kwargs = interaction.responses[0]
    assert receipt.reference_id in visible
    assert "secret decision body" not in visible
    assert "proposal" not in visible
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].everyone is False
    assert [event for event, _details in bot.database.rows] == ["operations.failure"]
    assert "secret decision body" not in repr(bot.database.rows)


async def test_duplicate_dynamic_callback_failure_has_one_terminal_delivery_and_audit() -> None:
    bot = _Bot()
    service = _FailingService(RuntimeError("private duplicate body"))
    adapter = DiscordForgeAdapter(authorizer=DiscordForgeAuthorizer(bot), service=service)
    interaction = _Interaction(bot.user, client=bot)
    button = ForgeDecisionButton(f"cf1:k:{DIGEST}:2", "Keep")
    previous = ForgeDecisionButton.adapter
    ForgeDecisionButton.adapter = adapter
    try:
        await button.callback(interaction)
        await button.callback(interaction)
    finally:
        ForgeDecisionButton.adapter = previous

    assert service.calls == 2
    assert len(interaction.responses) == 1
    assert len(bot.database.rows) == 1


async def test_dynamic_callback_propagates_external_cancellation_without_terminal_claim() -> None:
    bot = _Bot()
    service = _FailingService(asyncio.CancelledError())
    adapter = DiscordForgeAdapter(authorizer=DiscordForgeAuthorizer(bot), service=service)
    interaction = _Interaction(bot.user, client=bot)
    button = ForgeDecisionButton(f"cf1:r:{DIGEST}:2", "Reject")
    previous = ForgeDecisionButton.adapter
    ForgeDecisionButton.adapter = adapter
    try:
        with pytest.raises(asyncio.CancelledError):
            await button.callback(interaction)
    finally:
        ForgeDecisionButton.adapter = previous

    assert service.calls == 1
    assert interaction.responses == []
    assert bot.database.rows == []
    assert (
        bot.interaction_failure_terminal.receipt_for(
            surface="capability_forge_decision",
            interaction_id=interaction.id,
        )
        is None
    )


async def test_dynamic_callback_does_not_swallow_error_for_mismatched_interaction_client() -> None:
    bot = _Bot()
    other_bot = _Bot()
    service = _FailingService(RuntimeError("private mismatched client body"))
    adapter = DiscordForgeAdapter(authorizer=DiscordForgeAuthorizer(bot), service=service)
    interaction = _Interaction(bot.user, client=other_bot)
    button = ForgeDecisionButton(f"cf1:k:{DIGEST}:2", "Keep")
    previous = ForgeDecisionButton.adapter
    ForgeDecisionButton.adapter = adapter
    try:
        with pytest.raises(RuntimeError, match="private mismatched client body"):
            await button.callback(interaction)
    finally:
        ForgeDecisionButton.adapter = previous

    assert service.calls == 1
    assert interaction.responses == []
    assert other_bot.database.rows == []
    assert (
        other_bot.interaction_failure_terminal.receipt_for(
            surface="capability_forge_decision",
            interaction_id=interaction.id,
        )
        is None
    )


async def test_dynamic_item_rejects_non_strict_custom_id_before_routing() -> None:
    interaction = _Interaction(_User())
    item = discord.ui.Button(custom_id=f"cf1:x:{DIGEST}:2")
    with pytest.raises(ValueError, match="custom_id"):
        await ForgeDecisionButton.from_custom_id(interaction, item, None)


class _PluginBot(_Bot):
    def __init__(self, database_path: Path) -> None:
        super().__init__()
        self.settings.database_path = database_path
        self.dynamic: list[object] = []

    def add_dynamic_items(self, *items: object) -> None:
        self.dynamic.extend(items)

    def remove_dynamic_items(self, *items: object) -> None:
        for item in items:
            self.dynamic.remove(item)


async def test_plugin_opens_one_repository_registers_routing_and_cleans_up(tmp_path: Path) -> None:
    bot = _PluginBot(tmp_path / "forge.sqlite3")
    plugin = CapabilityForgePlugin()

    await plugin.start(bot)
    assert plugin.repository is not None
    assert bot.capability_forge_repository is plugin.repository
    assert bot.capability_forge_sandbox_lifecycle is plugin.sandbox_lifecycle
    assert bot.dynamic
    assert bot.runtime_capability_readiness[FORGE_OWNER_NOTIFICATION_CAPABILITY_ID] is True
    # readiness は local composition 完了だけを意味し、Discord DM delivery/live 到達の証明ではない。

    await plugin.begin_close()
    await plugin.stop()
    assert plugin.repository is None
    assert not hasattr(bot, "capability_forge_repository")
    assert not hasattr(bot, "capability_forge_sandbox_lifecycle")
    assert bot.dynamic == []
    assert bot.runtime_capability_readiness == {}
    assert ForgeDecisionButton.adapter is None


async def test_plugin_sandbox_lifecycle_is_unavailable_without_trusted_backend_and_creates_no_proposal(
    tmp_path: Path,
) -> None:
    bot = _PluginBot(tmp_path / "forge.sqlite3")
    plugin = CapabilityForgePlugin()
    await plugin.start(bot)
    lifecycle = plugin.sandbox_lifecycle
    assert lifecycle is not None

    outcome = await lifecycle.run(
        owner_user_id=42,
        candidate=SandboxCandidate(source="result = {'value': 'safe'}", input_data={}),
        scope=SandboxScope(request_id="forge-plugin-test", guild_id=1, channel_id=2, user_id=42),
        backend_identity="unconfigured",
    )

    assert outcome.status is SandboxRunStatus.UNAVAILABLE
    with sqlite3.connect(bot.settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM forge_recipe_candidate").fetchone()[0] == 0
    assert bot.user.sent == []
    await plugin.stop()


async def test_plugin_failed_start_clears_the_class_level_dynamic_adapter(tmp_path: Path) -> None:
    bot = _PluginBot(tmp_path / "forge.sqlite3")
    bot.add_dynamic_items = None
    plugin = CapabilityForgePlugin()

    with pytest.raises(TypeError, match="add_dynamic_items"):
        await plugin.start(bot)

    assert ForgeDecisionButton.adapter is None
    assert plugin.repository is None
    assert bot.runtime_capability_readiness == {}


async def test_publish_failure_after_composition_still_cleans_every_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _PluginBot(tmp_path / "forge.sqlite3")
    plugin = CapabilityForgePlugin()
    original_publish = capability_forge_plugin.publish_runtime_readiness
    calls = 0
    captured: dict[str, object] = {}

    def publish_then_fail(current_bot: object, values: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            captured["repository"] = getattr(current_bot, "capability_forge_repository")
            raise RuntimeError("fixed publish failure")
        original_publish(current_bot, values)

    monkeypatch.setattr(capability_forge_plugin, "publish_runtime_readiness", publish_then_fail)

    with pytest.raises(RuntimeError, match="fixed publish failure"):
        await plugin.start(bot)

    repository = captured["repository"]
    assert repository._connection is None
    assert plugin.repository is plugin.service is plugin.adapter is None
    assert plugin._bot is plugin._task is None
    assert ForgeDecisionButton.adapter is None
    assert bot.dynamic == []
    assert not hasattr(bot, "capability_forge_repository")
    assert not hasattr(bot, "capability_forge_service")
    assert bot.runtime_capability_readiness == {}


async def test_withdraw_failure_is_reraised_only_after_full_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _PluginBot(tmp_path / "forge.sqlite3")
    plugin = CapabilityForgePlugin()
    await plugin.start(bot)
    repository = plugin.repository
    assert repository is not None

    def fail_withdraw(_bot: object, _capability_ids: object) -> None:
        raise RuntimeError("fixed withdraw failure")

    monkeypatch.setattr(capability_forge_plugin, "withdraw_runtime_readiness", fail_withdraw)

    with pytest.raises(RuntimeError, match="fixed withdraw failure"):
        await plugin.stop()

    assert repository._connection is None
    assert plugin.repository is plugin.service is plugin.adapter is None
    assert plugin._bot is plugin._task is None
    assert ForgeDecisionButton.adapter is None
    assert bot.dynamic == []
    assert not hasattr(bot, "capability_forge_repository")
    assert not hasattr(bot, "capability_forge_service")
