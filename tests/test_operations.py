from __future__ import annotations

from datetime import UTC, datetime, timedelta

from yonerai_discord.modules.operations import (
    FailureKind,
    GateDecision,
    InputEnvelope,
    InputGate,
    InputPolicy,
    ProgressThrottle,
    ProgressUpdate,
    classify_failure,
)


NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def envelope(event_id: str, *, minute: int = 0, bot: bool = False) -> InputEnvelope:
    return InputEnvelope(event_id, 1, 2, 3, bot, NOW + timedelta(minutes=minute))


def test_input_gate_blocks_bots_duplicates_and_rate_limit() -> None:
    gate = InputGate(InputPolicy(events_per_user_per_minute=1))
    assert gate.evaluate(envelope("bot", bot=True)) is GateDecision.BOT_AUTHOR
    assert gate.evaluate(envelope("one")) is GateDecision.ALLOW
    assert gate.evaluate(envelope("one")) is GateDecision.DUPLICATE
    assert gate.evaluate(envelope("two")) is GateDecision.RATE_LIMITED
    assert gate.evaluate(envelope("three", minute=2)) is GateDecision.ALLOW


def test_gate_allowlists_are_fail_closed() -> None:
    gate = InputGate(InputPolicy(guild_allowlist=frozenset({99})))
    assert gate.evaluate(envelope("x")) is GateDecision.GUILD_BLOCKED


def test_dm_event_uses_dedupe_without_guild_channel_allowlists() -> None:
    gate = InputGate(
        InputPolicy(
            guild_allowlist=frozenset({10}),
            channel_allowlist=frozenset({20}),
        )
    )
    dm = InputEnvelope("dm-event", None, 30, 40, False, NOW)

    assert gate.evaluate(dm) is GateDecision.ALLOW
    assert gate.evaluate(dm) is GateDecision.DUPLICATE


def test_progress_throttle_deduplicates_and_limits() -> None:
    throttle = ProgressThrottle(minimum_interval=timedelta(seconds=1), max_updates=2)
    first = ProgressUpdate("scan", "開始")
    second = ProgressUpdate("scan", "半分")
    assert throttle.allow(first, NOW)
    assert not throttle.allow(first, NOW + timedelta(seconds=1))
    assert not throttle.allow(second, NOW + timedelta(milliseconds=500))
    assert throttle.allow(second, NOW + timedelta(seconds=1))
    assert not throttle.allow(ProgressUpdate("done", "完了"), NOW + timedelta(seconds=2))


def test_failure_classifier_never_returns_exception_text() -> None:
    failure = classify_failure(ValueError("secret raw content"))
    assert failure.kind is FailureKind.INVALID_INPUT
    assert "secret" not in failure.user_message
    assert failure.error_type == "ValueError"


def test_failure_classifier_unwraps_command_error_and_maps_discord_status() -> None:
    class DiscordForbidden(RuntimeError):
        status = 403

    wrapped = RuntimeError("wrapper")
    wrapped.original = DiscordForbidden("credential-bearing response")  # type: ignore[attr-defined]

    failure = classify_failure(wrapped)

    assert failure.kind is FailureKind.PERMISSION_DENIED
    assert failure.error_type == "DiscordForbidden"
    assert "credential" not in failure.user_message


def test_failure_classifier_rejects_cyclic_original_chain_without_recursion() -> None:
    first = RuntimeError("first private body")
    second = RuntimeError("second private body")
    first.original = second  # type: ignore[attr-defined]
    second.original = first  # type: ignore[attr-defined]

    failure = classify_failure(first)

    assert failure.kind is FailureKind.INTERNAL
    assert "private body" not in failure.user_message


def test_failure_classifier_ignores_original_property_failure() -> None:
    class BrokenWrapper(RuntimeError):
        @property
        def original(self) -> BaseException:
            raise RuntimeError("private property body")

    failure = classify_failure(BrokenWrapper("private wrapper body"))

    assert failure.kind is FailureKind.INTERNAL
    assert failure.error_type == "BrokenWrapper"
    assert "private" not in failure.user_message


def test_failure_classifier_ignores_status_property_failure() -> None:
    class BrokenStatus(RuntimeError):
        @property
        def status(self) -> int:
            raise RuntimeError("private status body")

    failure = classify_failure(BrokenStatus("private wrapper body"))

    assert failure.kind is FailureKind.INTERNAL
    assert failure.error_type == "BrokenStatus"
    assert "private" not in failure.user_message
