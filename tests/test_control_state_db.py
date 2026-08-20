from __future__ import annotations

import sqlite3

import pytest

from yonerai_discord.db import Database, Migration
from yonerai_discord.control_plane import CapabilitySpec, ModuleSpec, RbacLevel, Registry


@pytest.fixture
def database(tmp_path):
    instance = Database(tmp_path / "control.sqlite3")
    instance.open()
    instance.migrate()
    try:
        yield instance
    finally:
        instance.close()


def test_overrides_preserve_exact_scope_and_resolve_guild_before_global(database: Database) -> None:
    database.set_module_override("Core.Tools", True, updated_by=100, reason="global default")
    database.set_module_override("core.tools", False, 200, updated_by=101, reason="guild pause")
    database.set_capability_override("tools.ping", False, updated_by=100)
    database.set_capability_override("tools.ping", True, "200", updated_by=101)
    database.set_level_override("tools.ping", "moderator", updated_by=100)
    database.set_level_override("tools.ping", "guild_admin", 200, updated_by=101)

    assert database.get_module_override("core.tools") is True
    assert database.get_module_override("core.tools", 200) is False
    assert database.resolve_module_override("core.tools", 200) is False
    assert database.resolve_module_override("core.tools", 201) is True
    assert database.resolve_capability_override("tools.ping", 200) is True
    assert database.resolve_capability_override("tools.ping", 201) is False
    assert database.resolve_level_override("tools.ping", 200) == 30
    assert database.resolve_level_override("tools.ping", 201) == 20

    effective = {(item.kind, item.subject_id): item for item in database.list_effective_overrides(200)}
    assert effective[("module", "core.tools")].enabled is False
    assert effective[("module", "core.tools")].guild_id == 200
    assert effective[("capability", "tools.ping")].enabled is True
    assert effective[("permission", "tools.ping")].required_level == 30
    assert {(item.kind, item.subject_id) for item in database.list_enabled_overrides(200)} == {
        ("capability", "tools.ping")
    }

    database.set_module_override("core.tools", None, 200, updated_by=102, reason="inherit again")
    assert database.get_module_override("core.tools", 200) is None
    assert database.resolve_module_override("core.tools", 200) is True


def test_database_is_compatible_with_control_plane_state_store(database: Database) -> None:
    registry = Registry(database)
    registry.register_module(ModuleSpec("core", default_enabled=True))
    registry.register_capability(CapabilitySpec("core.ping", "core", required_level=RbacLevel.TRUSTED))

    database.set_module_override("core", False, 77, updated_by=5)
    database.set_level_override("core.ping", "moderator", updated_by=5)

    assert registry.configured_module_enabled("core", 77) is False
    assert registry.configured_module_enabled("core", 78) is True
    assert registry.required_level("core.ping", 77) is RbacLevel.MODERATOR


def test_owner_managed_capability_and_delegated_actor_grants_are_separate(database: Database) -> None:
    capability_id = "cap-run-site-auto-publish"
    database.set_owner_managed_capability_enabled(
        capability_id,
        True,
        200,
        updated_by=100,
        reason="owner-only baseline",
    )

    assert database.get_capability_override(capability_id, 200) is True
    assert database.get_level_override(capability_id, 200) == int(RbacLevel.BOT_OWNER)
    assert database.list_capability_actor_grants(200, capability_id=capability_id) == ()

    database.set_capability_actor_grant(
        capability_id,
        101,
        True,
        200,
        granted_by=100,
        reason="explicit owner delegation",
    )
    grant = database.get_capability_actor_grant(capability_id, 101, 200)
    assert grant is not None
    assert grant.grant_kind == "owner_delegated"
    assert grant.granted_by == 100
    assert [item.subject_user_id for item in database.list_capability_actor_grants(200)] == [101]
    database.close()
    database.open()
    assert database.get_capability_actor_grant(capability_id, 101, 200) == grant

    with pytest.raises(ValueError, match="owner self"):
        database.set_capability_actor_grant(capability_id, 100, True, 200, granted_by=100)
    with pytest.raises(ValueError, match="Discord guild"):
        database.set_capability_actor_grant(capability_id, 101, True, 0, granted_by=100)

    database.set_capability_actor_grant(capability_id, 101, False, 200, granted_by=100)
    assert database.get_capability_actor_grant(capability_id, 101, 200) is None
    events = [record.event for record in database.list_audit()]
    assert "owner_managed_capability.activated" in events
    assert "capability_actor_grant.set" in events
    assert "capability_actor_grant.cleared" in events


def test_override_inputs_are_validated_and_queries_are_parameterized(database: Database) -> None:
    with pytest.raises(ValueError, match="invalid character"):
        database.set_module_override("x'); DROP TABLE audit_log; --", True, updated_by=1)
    with pytest.raises(TypeError, match="bool"):
        database.set_capability_override("safe.id", 1, updated_by=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown RBAC"):
        database.set_level_override("safe.id", 999, updated_by=1)
    with pytest.raises(ValueError, match="guild_id"):
        database.set_module_override("safe.id", True, -1, updated_by=1)

    database.append_audit("integrity.checked", actor_id=1, details={"table": "audit_log"})
    assert database.list_audit()[-1].details == {"table": "audit_log"}


def test_audit_is_canonical_bounded_metadata_and_database_append_only(database: Database) -> None:
    audit_id = database.append_audit(
        "control.changed",
        actor_id=55,
        guild_id=99,
        details={"z": [2, 1], "a": {"enabled": True}},
    )
    event = next(item for item in database.list_audit() if item.id == audit_id)
    assert event.event == "control.changed"
    assert event.guild_id == 99
    assert event.details == {"a": {"enabled": True}, "z": [2, 1]}

    with pytest.raises(ValueError, match="secrets or body text"):
        database.append_audit("unsafe", actor_id=1, details={"access_token": "do-not-store"})
    with pytest.raises(TypeError, match="mapping"):
        database.append_audit("unsafe", actor_id=1, details=["not", "a", "mapping"])  # type: ignore[arg-type]

    connection = database._require_connection()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE audit_log SET event = 'tampered' WHERE id = ?", (audit_id,))
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM audit_log WHERE id = ?", (audit_id,))
    connection.rollback()


def test_recent_audit_returns_newest_first_with_bounded_limit(database: Database) -> None:
    ids = [database.append_audit(f"recent.{index}", actor_id=1) for index in range(3)]
    records = database.list_recent_audit(limit=2)
    assert [record.id for record in records] == [ids[2], ids[1]]

    with pytest.raises(ValueError, match="between 1 and 100"):
        database.list_recent_audit(limit=0)
    with pytest.raises(ValueError, match="between 1 and 100"):
        database.list_recent_audit(limit=True)  # type: ignore[arg-type]


def test_guild_audit_summary_is_exactly_scoped_and_never_reads_details(database: Database) -> None:
    guild_ids = [
        database.append_audit(
            "guild.first",
            actor_id=11,
            guild_id=42,
            plugin="admin_ui",
            details={"safe": "not returned"},
        ),
        database.append_audit("other.guild", actor_id=12, guild_id=99, details={"safe": "other"}),
        database.append_audit("guild.second", actor_id=13, guild_id=42, details={"safe": "newer"}),
    ]
    connection = database._require_connection()
    statements: list[str] = []

    def deny_details(
        action: int,
        table: str | None,
        column: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and table == "audit_log" and column == "details_json":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_details)
    connection.set_trace_callback(statements.append)
    try:
        rows = database.list_guild_audit_summary(42, limit=50)
    finally:
        connection.set_trace_callback(None)
        connection.set_authorizer(None)

    assert [row.id for row in rows] == [guild_ids[2], guild_ids[0]]
    assert [row.event for row in rows] == ["guild.second", "guild.first"]
    assert all(not hasattr(row, "details") and not hasattr(row, "guild_id") for row in rows)
    select = next(statement for statement in statements if "FROM audit_log" in statement)
    assert (
        " ".join(select.split()) == "SELECT id, event, plugin, actor_id, created_at FROM audit_log "
        "WHERE guild_id = 42 ORDER BY id DESC LIMIT 50"
    )
    with pytest.raises(ValueError, match="Discord guild"):
        database.list_guild_audit_summary(None)
    with pytest.raises(ValueError, match="Discord guild"):
        database.list_guild_audit_summary(0)
    with pytest.raises(ValueError, match="between 1 and 50"):
        database.list_guild_audit_summary(42, limit=51)


def test_agent_audit_projection_query_is_scope_bound_ordered_and_never_reads_details(database: Database) -> None:
    first_id = database.append_audit(
        "agent.first",
        actor_id=11,
        guild_id=42,
        plugin="ai",
        details={"private": "matching-first"},
    )
    database.append_audit(
        "agent.other-actor",
        actor_id=12,
        guild_id=42,
        details={"private": "other-actor"},
    )
    database.append_audit(
        "agent.other-guild",
        actor_id=11,
        guild_id=99,
        details={"private": "other-guild"},
    )
    connection = database._require_connection()
    invalid_json_id = int(
        connection.execute(
            "INSERT INTO audit_log(event, plugin, guild_id, actor_id, details_json) VALUES (?, ?, ?, ?, ?)",
            ("agent.invalid-json", None, 42, 11, "private-not-json"),
        ).lastrowid
    )
    final_id = database.append_audit(
        "agent.final",
        actor_id=11,
        guild_id=42,
        details={"private": "matching-final"},
    )
    statements: list[str] = []

    def deny_details(
        action: int,
        table: str | None,
        column: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and table == "audit_log" and column == "details_json":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_details)
    connection.set_trace_callback(statements.append)
    try:
        rows = database.list_agent_audit_projection(
            guild_id=42,
            actor_id=11,
            after_id=first_id,
            limit=2,
        )
    finally:
        connection.set_trace_callback(None)
        connection.set_authorizer(None)

    assert [row.id for row in rows] == [invalid_json_id, final_id]
    assert [row.event for row in rows] == ["agent.invalid-json", "agent.final"]
    assert all(row.guild_id == 42 and row.actor_id == 11 for row in rows)
    assert all(not hasattr(row, "details") for row in rows)
    select = next(statement for statement in statements if "FROM audit_log" in statement)
    assert "details_json" not in select
    assert " ".join(select.split()) == (
        "SELECT id, event, plugin, guild_id, actor_id, created_at FROM audit_log "
        f"WHERE guild_id = 42 AND actor_id = 11 AND id > {first_id} ORDER BY id LIMIT 2"
    )

    with pytest.raises(ValueError, match="Discord guild"):
        database.list_agent_audit_projection(guild_id=0, actor_id=11)
    with pytest.raises(ValueError, match="actor ID"):
        database.list_agent_audit_projection(guild_id=42, actor_id=0)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        database.list_agent_audit_projection(guild_id=42, actor_id=11, limit=1_001)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        database.list_agent_audit_projection(guild_id=42, actor_id=11, limit=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative"):
        database.list_agent_audit_projection(guild_id=42, actor_id=11, after_id=-1)
    with pytest.raises(ValueError, match="non-negative"):
        database.list_agent_audit_projection(guild_id=42, actor_id=11, after_id=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative"):
        database.list_agent_audit_projection(guild_id=42, actor_id=11, after_id=9_223_372_036_854_775_808)


def test_proposal_ledger_stores_metadata_only_and_enforces_transitions(database: Database) -> None:
    proposal = database.create_evolution_proposal(
        "evo-abc123",
        "a" * 64,
        proposer_id=10,
        reason="awaiting review",
    )
    assert proposal.status == "proposed"
    assert proposal.proposal_hash == "a" * 64

    reviewing = database.update_evolution_proposal_status(
        proposal.proposal_id,
        "in_review",
        updated_by=20,
        reason="review started",
    )
    approved = database.update_evolution_proposal_status(
        proposal.proposal_id,
        "approved",
        updated_by=20,
        reason="scope and tests verified",
    )
    assert reviewing.status == "in_review"
    assert approved.status == "approved"
    with pytest.raises(ValueError, match="invalid proposal transition"):
        database.update_evolution_proposal_status(proposal.proposal_id, "rejected", updated_by=20)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        database.create_evolution_proposal(
            "evo-child",
            "b" * 64,
            proposer_id=10,
            parent_proposal_id="evo-missing",
        )

    columns = {row[1] for row in database._require_connection().execute("PRAGMA table_info(evolution_proposal)")}
    assert "patch" not in columns
    assert "prompt" not in columns
    assert "body" not in columns


def test_failed_migration_rolls_back_schema_and_version(database: Database) -> None:
    broken = Migration(
        6,
        """
        CREATE TABLE should_roll_back (id INTEGER PRIMARY KEY);
        THIS IS NOT SQL;
        """,
    )
    with pytest.raises(sqlite3.Error):
        database.migrate((broken,))

    connection = database._require_connection()
    assert (
        connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'should_roll_back'").fetchone()
        is None
    )
    assert connection.execute("SELECT 1 FROM schema_migrations WHERE version = 6").fetchone() is None
