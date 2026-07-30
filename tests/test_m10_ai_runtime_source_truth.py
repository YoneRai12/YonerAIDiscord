from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from yonerai_discord.bot import _m10_truth_snapshot
from yonerai_discord.db import Database
from yonerai_discord.modules.ai import AIPlugin, AIService
from yonerai_discord.modules.admin_ui.web_adapter import (
    _deployment_current_truth as _admin_deployment_current_truth,
)


class _Guard:
    def event_allowed(self, _capability_id: str, **_kwargs: object) -> bool:
        return True

    def currently_allowed(self, _capability_id: str, **_kwargs: object) -> bool:
        return True


def _settings(database_path: Path, *, durable: bool) -> SimpleNamespace:
    return SimpleNamespace(
        database_path=database_path,
        ai_orchestration_durable_enabled=durable,
        ai_conversation_ttl_seconds=7_200,
        ai_conversation_max_turns=12,
        ai_conversation_max_sessions=128,
        ai_conversation_max_total_binary_bytes=64 * 1024 * 1024,
        ai_attachment_max_file_bytes=8 * 1024 * 1024,
        ai_attachment_max_total_bytes=16 * 1024 * 1024,
        ai_attachment_max_files=4,
        ai_base_url="",
        ai_mention_enabled=True,
        ai_mention_guild_ids=frozenset({10}),
        ai_mention_allow_all_guilds=False,
        ai_reply_continuation_enabled=False,
        ai_attachments_enabled=False,
        ai_timeout_seconds=30.0,
        ai_admission_global_concurrency=4,
        ai_admission_max_waiters=32,
        ai_admission_wait_timeout_seconds=0.1,
        ai_admission_drain_timeout_seconds=0.1,
    )


def _bot(database_path: Path, *, durable: bool, database: object | None) -> SimpleNamespace:
    values: dict[str, object] = {
        "user": SimpleNamespace(id=99),
        "settings": _settings(database_path, durable=durable),
        "tree": SimpleNamespace(add_command=lambda _command: None, remove_command=lambda _name: None),
        "capability_guard": _Guard(),
        "add_listener": lambda _listener, _name: None,
        "remove_listener": lambda _listener, _name: None,
        "is_closing": False,
    }
    if database is not None:
        values["database"] = database
    return SimpleNamespace(**values)


def _open_database(path: Path) -> Database:
    database = Database(path)
    database.open()
    database.migrate()
    return database


@pytest.mark.asyncio
async def test_final_truth_marks_only_started_durable_runtime_and_open_audit_ready(tmp_path: Path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    database = _open_database(database_path)
    bot = _bot(database_path, durable=True, database=database)
    plugin = AIPlugin()

    try:
        await plugin.start(bot)

        truth = bot.deployment_current_truth
        assert plugin._deployment_current_truth is truth
        assert truth.jobs_source.configured is True
        assert truth.jobs_source.ready is True
        assert truth.jobs_source.live_success is None
        assert truth.jobs_source.blocker is None
        assert truth.audit_source.configured is True
        assert truth.audit_source.ready is True
        assert truth.audit_source.live_success is None
        assert truth.audit_source.blocker is None
        assert plugin._orchestration_consumer is not None
        assert plugin._orchestration_consumer.ready is True
        assert callable(bot.deployment_current_truth_current)
        system_truth, system_injected = _m10_truth_snapshot(bot)
        admin_truth, admin_identity = _admin_deployment_current_truth(bot)
        assert system_truth is truth
        assert system_injected is True
        assert admin_truth is truth
        assert admin_identity is truth
    finally:
        await plugin.stop()
        database.close()

    assert not hasattr(bot, "deployment_current_truth")
    assert not hasattr(bot, "deployment_current_truth_current")


@pytest.mark.asyncio
async def test_disabled_durable_runtime_stays_typed_unconfigured(tmp_path: Path) -> None:
    database_path = tmp_path / "bot.sqlite3"
    database = _open_database(database_path)
    bot = _bot(database_path, durable=False, database=database)
    plugin = AIPlugin()

    try:
        await plugin.start(bot)

        source = bot.deployment_current_truth.jobs_source
        assert source.configured is False
        assert source.ready is False
        assert source.live_success is None
        assert source.blocker == "durable_orchestration_not_configured"
        assert plugin._orchestration_runtime is None
        assert plugin._orchestration_consumer is None
    finally:
        await plugin.stop()
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_state", "configured", "blocker"),
    [
        ("missing", False, "audit_source_not_configured"),
        ("closed", True, "audit_source_not_ready"),
    ],
)
async def test_audit_truth_requires_exact_open_database_contract(
    tmp_path: Path,
    database_state: str,
    configured: bool,
    blocker: str,
) -> None:
    database_path = tmp_path / database_state / "bot.sqlite3"
    database = Database(database_path) if database_state == "closed" else None
    bot = _bot(database_path, durable=False, database=database)
    plugin = AIPlugin()

    await plugin.start(bot)
    source = bot.deployment_current_truth.audit_source

    assert source.configured is configured
    assert source.ready is False
    assert source.live_success is None
    assert source.blocker == blocker

    await plugin.stop()


@pytest.mark.asyncio
async def test_runtime_truth_recheck_and_cleanup_require_current_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AIService, "available", property(lambda _self: True))
    database_path = tmp_path / "bot.sqlite3"
    database = _open_database(database_path)
    bot = _bot(database_path, durable=True, database=database)
    plugin = AIPlugin()

    await plugin.start(bot)
    selection = bot.ai_execution_profile_selection
    repository = bot.ai_orchestration_repository
    original_truth = bot.deployment_current_truth
    current_callback = bot.deployment_current_truth_current

    stable_truth, stable_injected = _m10_truth_snapshot(bot)
    assert stable_truth is original_truth
    assert stable_injected is True

    database.close()
    closed_truth, closed_injected = _m10_truth_snapshot(bot)
    admin_closed_truth, admin_closed_identity = _admin_deployment_current_truth(bot)
    assert closed_truth is not original_truth
    assert closed_injected is True
    assert closed_truth.audit_source.ready is False
    assert closed_truth.audit_source.blocker == "audit_source_not_ready"
    assert admin_closed_truth is closed_truth
    assert admin_closed_identity is closed_truth

    bot.ai_orchestration_repository = object()
    changed_truth, changed_injected = _m10_truth_snapshot(bot)
    changed_jobs = changed_truth.jobs_source
    assert changed_injected is True
    assert changed_jobs.configured is True
    assert changed_jobs.ready is False
    assert changed_jobs.blocker == "durable_orchestration_consumer_not_ready"
    bot.ai_orchestration_repository = repository

    bot.database = object()
    changed_audit = plugin._build_deployment_current_truth(selection, bot=bot).audit_source
    assert changed_audit.configured is True
    assert changed_audit.ready is False
    assert changed_audit.blocker == "audit_source_identity_changed"
    bot.database = database

    service = bot.ai_service
    bot.ai_service = object()
    changed_provider, _changed_provider_injected = _m10_truth_snapshot(bot)
    assert changed_provider.provider_source.ready is False
    assert changed_provider.provider_source.blocker == "provider_source_identity_changed"
    bot.ai_service = service

    foreign_truth = object()

    def foreign_callback() -> object:
        return foreign_truth

    bot.deployment_current_truth = foreign_truth
    bot.deployment_current_truth_current = foreign_callback
    await plugin.stop()

    assert bot.deployment_current_truth is foreign_truth
    assert bot.deployment_current_truth_current is foreign_callback
    assert current_callback is not foreign_callback


def test_public_truth_readers_fail_closed_when_current_callback_fails() -> None:
    def broken_current() -> object:
        raise RuntimeError("fixed callback failure")

    bot = SimpleNamespace(
        deployment_current_truth=object(),
        deployment_current_truth_current=broken_current,
    )

    system_truth, system_injected = _m10_truth_snapshot(bot)
    admin_truth, admin_identity = _admin_deployment_current_truth(bot)

    assert system_injected is False
    assert system_truth.jobs_source.ready is False
    assert system_truth.audit_source.ready is False
    assert admin_identity is None
    assert admin_truth.jobs_source.ready is False
    assert admin_truth.audit_source.ready is False
