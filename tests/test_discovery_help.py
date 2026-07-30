from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace

import pytest

from yonerai_discord.capabilities import COMMAND_CAPABILITIES, COMMAND_PLUGIN_BY_ROOT, SURFACE_RATE_LIMITS
from yonerai_discord.config import SAFE_DEFAULT_PLUGINS
from yonerai_discord.control_plane import (
    ActorContext,
    CapabilitySpec,
    InMemoryStateStore,
    ModuleSpec,
    RbacLevel,
    Registry,
)
from yonerai_discord.modules.discovery.adapter import DiscoveryPlugin, _send_ephemeral
from yonerai_discord.modules.discovery.domain import MAX_QUERY_LENGTH, DiscoveryInputError
from yonerai_discord.modules.discovery.service import DiscoveryService, render_command_page


MODULE_ID = "interaction.discord-surface"


def _registry(
    rows: Iterable[tuple[str, str, RbacLevel]],
) -> tuple[InMemoryStateStore, Registry]:
    store = InMemoryStateStore()
    registry = Registry(store)
    registry.register_module(ModuleSpec(MODULE_ID))
    for capability_id, description, level in rows:
        registry.register_capability(
            CapabilitySpec(
                capability_id,
                MODULE_ID,
                name=description,
                required_level=level,
                minimum_level=level,
            )
        )
    return store, registry


def _service(
    registry: Registry,
    mapping: dict[str, str],
    *,
    running: frozenset[str] = frozenset({"sample"}),
    floors: dict[str, RbacLevel] | None = None,
) -> DiscoveryService:
    roots = {path.split(" ", 1)[0] for path in mapping}
    return DiscoveryService(
        registry,
        command_capabilities=mapping,
        command_plugins={root: "sample" for root in roots},
        command_floors=floors,
        plugin_is_running=lambda plugin: plugin in running,
    )


def _paths(result: object) -> set[str]:
    return {entry.path for entry in result.entries}  # type: ignore[attr-defined]


def test_everyone_and_admin_receive_only_commands_allowed_by_effective_rbac() -> None:
    _, registry = _registry(
        (
            ("cap-public", "Public command", RbacLevel.EVERYONE),
            ("cap-admin", "Admin command", RbacLevel.GUILD_ADMIN),
        )
    )
    mapping = {"sample public": "cap-public", "sample admin": "cap-admin"}
    service = _service(registry, mapping)
    live = frozenset(mapping)

    everyone = service.search(ActorContext(1, 10, RbacLevel.EVERYONE), live_command_paths=live)
    admin = service.search(ActorContext(2, 10, RbacLevel.GUILD_ADMIN), live_command_paths=live)

    assert _paths(everyone) == {"sample public"}
    assert _paths(admin) == set(mapping)
    assert (
        next(entry for entry in admin.entries if entry.path == "sample admin").required_level is RbacLevel.GUILD_ADMIN
    )


def test_discord_surface_floor_is_part_of_effective_rbac_filter() -> None:
    _, registry = _registry((("cap-public", "Public in registry", RbacLevel.EVERYONE),))
    mapping = {"sample guarded": "cap-public"}
    service = _service(registry, mapping, floors={"sample guarded": RbacLevel.GUILD_ADMIN})
    live = frozenset(mapping)

    everyone = service.search(ActorContext(1, 10), live_command_paths=live)
    admin = service.search(ActorContext(2, 10, RbacLevel.GUILD_ADMIN), live_command_paths=live)

    assert everyone.total_entries == 0
    assert admin.entries[0].required_level is RbacLevel.GUILD_ADMIN


def test_module_and_capability_off_are_hidden_for_current_guild() -> None:
    store, registry = _registry(
        (
            ("cap-one", "One", RbacLevel.EVERYONE),
            ("cap-two", "Two", RbacLevel.EVERYONE),
        )
    )
    mapping = {"sample one": "cap-one", "sample two": "cap-two"}
    service = _service(registry, mapping)
    actor = ActorContext(1, 10, RbacLevel.EVERYONE)

    store.set_capability_override("cap-one", False, 10)
    capability_off = service.search(actor, live_command_paths=frozenset(mapping))
    assert _paths(capability_off) == {"sample two"}

    store.set_module_override(MODULE_ID, False, 10)
    module_off = service.search(actor, live_command_paths=frozenset(mapping))
    assert module_off.total_entries == 0


def test_stopped_plugin_missing_live_surface_and_runtime_unavailable_are_hidden() -> None:
    _, registry = _registry((("cap-one", "One", RbacLevel.EVERYONE),))
    mapping = {"sample one": "cap-one"}
    actor = ActorContext(1, 10)

    stopped = _service(registry, mapping, running=frozenset()).search(
        actor,
        live_command_paths=frozenset(mapping),
    )
    missing_surface = _service(registry, mapping).search(actor, live_command_paths=frozenset())
    registry.set_runtime_availability("cap-one", False)
    unavailable = _service(registry, mapping).search(actor, live_command_paths=frozenset(mapping))

    assert stopped.total_entries == 0
    assert missing_surface.total_entries == 0
    assert unavailable.total_entries == 0


def test_casefold_search_covers_path_description_module_and_rbac() -> None:
    _, registry = _registry((("cap-admin", "Straße Status", RbacLevel.GUILD_ADMIN),))
    service = _service(registry, {"sample status": "cap-admin"})
    actor = ActorContext(1, 10, RbacLevel.GUILD_ADMIN)
    live = frozenset({"sample status"})

    assert service.search(actor, live_command_paths=live, query="STRASSE").total_entries == 1
    assert service.search(actor, live_command_paths=live, query="discord-surface").total_entries == 1
    assert service.search(actor, live_command_paths=live, query="guild_admin").total_entries == 1
    assert service.search(actor, live_command_paths=live, query="missing").total_entries == 0


def test_pagination_boundaries_are_deterministic() -> None:
    rows = tuple((f"cap-{index}", f"Command {index}", RbacLevel.EVERYONE) for index in range(7))
    _, registry = _registry(rows)
    mapping = {f"sample command-{index}": f"cap-{index}" for index in range(7)}
    service = _service(registry, mapping)
    actor = ActorContext(1, 10)
    live = frozenset(mapping)

    first = service.search(actor, live_command_paths=live, page=1)
    second = service.search(actor, live_command_paths=live, page=2)
    outside = service.search(actor, live_command_paths=live, page=3)

    assert (len(first.entries), len(second.entries)) == (6, 1)
    assert (first.total_pages, second.total_pages) == (2, 2)
    assert outside.out_of_range is True
    assert "1～2" in render_command_page(outside)


def test_denied_secret_and_unmapped_live_surface_are_never_rendered() -> None:
    _, registry = _registry((("cap-secret", "SECRET_TOKEN=do-not-leak", RbacLevel.BOT_OWNER),))
    service = _service(registry, {"sample secret": "cap-secret"})
    result = service.search(
        ActorContext(1, 10, RbacLevel.EVERYONE),
        live_command_paths=frozenset({"sample secret", "rogue unmapped"}),
    )
    rendered = render_command_page(result)

    assert result.total_entries == 0
    assert "SECRET_TOKEN" not in rendered
    assert "rogue" not in rendered


def test_invalid_or_missing_registry_mapping_fails_closed_per_entry() -> None:
    _, registry = _registry((("cap-valid", "Valid", RbacLevel.EVERYONE),))
    mapping = {"sample missing": "cap-absent"}
    result = _service(registry, mapping).search(
        ActorContext(1, 10),
        live_command_paths=frozenset(mapping),
    )
    assert result.total_entries == 0


def test_query_limit_and_response_length_are_bounded() -> None:
    _, registry = _registry((("cap-one", "@everyone `unsafe` " + "x" * 300, RbacLevel.EVERYONE),))
    mapping = {"sample one": "cap-one"}
    service = _service(registry, mapping)

    with pytest.raises(DiscoveryInputError):
        service.search(
            ActorContext(1, 10),
            live_command_paths=frozenset(mapping),
            query="x" * (MAX_QUERY_LENGTH + 1),
        )

    rendered = render_command_page(service.search(ActorContext(1, 10), live_command_paths=frozenset(mapping)))
    assert len(rendered) < 2_000
    assert "@everyone" not in rendered
    assert "`unsafe`" not in rendered


@pytest.mark.asyncio
async def test_discord_response_is_ephemeral_and_disables_mentions() -> None:
    sent: dict[str, object] = {}

    class Response:
        def is_done(self) -> bool:
            return False

        async def send_message(self, content: str, **kwargs: object) -> None:
            sent.update(content=content, **kwargs)

    interaction = SimpleNamespace(response=Response())
    await _send_ephemeral(interaction, "safe")  # type: ignore[arg-type]

    assert sent["content"] == "safe"
    assert sent["ephemeral"] is True
    allowed_mentions = sent["allowed_mentions"]
    assert allowed_mentions.everyone is False  # type: ignore[union-attr]
    assert allowed_mentions.users is False  # type: ignore[union-attr]
    assert allowed_mentions.roles is False  # type: ignore[union-attr]
    assert allowed_mentions.replied_user is False  # type: ignore[union-attr]


def test_help_runtime_wiring_is_explicit_and_rate_limited() -> None:
    assert COMMAND_CAPABILITIES["help"] == "cap-run-discovery-help"
    assert COMMAND_PLUGIN_BY_ROOT["help"] == "discovery"
    assert "discovery" in SAFE_DEFAULT_PLUGINS
    assert SURFACE_RATE_LIMITS["help"] == 10


def test_unmapped_only_inventory_returns_zero_results() -> None:
    _, registry = _registry(())
    service = DiscoveryService(
        registry,
        command_capabilities={},
        command_plugins={},
        plugin_is_running=lambda _: True,
    )
    result = service.search(
        ActorContext(1, 10),
        live_command_paths=frozenset({"rogue unmapped"}),
    )
    assert result.total_entries == 0


def test_dm_context_returns_no_guild_command_results() -> None:
    _, registry = _registry((("cap-one", "One", RbacLevel.EVERYONE),))
    mapping = {"sample one": "cap-one"}
    result = _service(registry, mapping).search(
        ActorContext(1, None),
        live_command_paths=frozenset(mapping),
    )
    assert result.total_entries == 0


@pytest.mark.asyncio
async def test_help_command_is_guild_only_and_plugin_removes_it_on_stop() -> None:
    class Tree:
        def __init__(self) -> None:
            self.added: object | None = None
            self.removed: tuple[str, object] | None = None

        def add_command(self, command: object) -> None:
            self.added = command

        def remove_command(self, name: str, *, type: object) -> None:
            self.removed = (name, type)

    tree = Tree()
    plugin = DiscoveryPlugin()
    await plugin.start(SimpleNamespace(tree=tree))
    assert tree.added is not None
    assert tree.added.guild_only is True  # type: ignore[union-attr]

    await plugin.stop()
    assert tree.removed is not None
    assert tree.removed[0] == "help"
