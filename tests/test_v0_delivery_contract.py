"""Phase 1 contract-only delivery tests; no Discord or Core runtime wiring."""

from yonerai_discord.v0_contracts import DeliveryContractHarness, DeliveryState, RunEvent


def test_v0_contract_only_idempotency_key_is_namespaced_and_duplicate_delivery_reuses_run() -> None:
    harness = DeliveryContractHarness()
    key, first, created = harness.claim(event_kind="message_create", event_id="987654321")
    replay_key, replay, replay_created = harness.claim(event_kind="message_create", event_id="987654321")
    assert key == "discord:message_create:987654321"
    assert replay_key == key
    assert created is True
    assert replay_created is False
    assert replay is first


def test_v0_contract_only_final_or_error_claims_terminal_exactly_once() -> None:
    harness = DeliveryContractHarness()
    state = DeliveryState()
    assert harness.reduce(state, RunEvent("final", 2, "final answer")) == "terminal"
    assert harness.reduce(state, RunEvent("error", 3, "late error")) == "ignored_after_terminal_or_stale"
    assert state.terminal_kind == "final"


def test_v0_contract_only_unknown_event_is_safely_ignored_without_state_change() -> None:
    harness = DeliveryContractHarness()
    state = DeliveryState(last_sequence=4, text="already rendered")
    assert harness.reduce(state, RunEvent("future_unrecognized_event", 5, "do something")) == "ignored_unknown"
    assert state == DeliveryState(last_sequence=4, text="already rendered")


def test_v0_contract_only_restart_reloads_idempotency_and_terminal_receipt() -> None:
    before_restart = DeliveryContractHarness()
    key, record, created = before_restart.claim(event_kind="interaction", event_id="444")
    state = DeliveryState()
    assert created is True
    assert before_restart.reduce(state, RunEvent("error", 7, "safe failure")) == "terminal"
    before_restart.persist_terminal(key, state)
    after_restart = DeliveryContractHarness(before_restart.store)
    replay_key, replay, replay_created = after_restart.claim(event_kind="interaction", event_id="444")
    assert replay_key == key
    assert replay_created is False
    assert replay.run_id == record.run_id
    assert replay.terminal is True
    assert replay.terminal_kind == "error"
