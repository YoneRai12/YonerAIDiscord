from __future__ import annotations

from types import SimpleNamespace

from yonerai_discord.runtime_readiness import (
    publish_runtime_readiness,
    publish_runtime_readiness_probe,
    refresh_runtime_readiness,
    withdraw_runtime_readiness,
)


class FakeRegistry:
    def __init__(self) -> None:
        self.values: dict[str, bool] = {}

    def set_runtime_availability(self, capability_id: str, ready: bool) -> None:
        self.values[capability_id] = ready


def test_publish_updates_live_registry_after_surface_reconciliation() -> None:
    registry = FakeRegistry()
    bot = SimpleNamespace(capability_registry=registry)

    publish_runtime_readiness(bot, {"cap-run-example": True})

    assert bot.runtime_capability_readiness == {"cap-run-example": True}
    assert registry.values == {"cap-run-example": True}


def test_withdraw_marks_live_registry_unavailable() -> None:
    registry = FakeRegistry()
    bot = SimpleNamespace(
        capability_registry=registry,
        runtime_capability_readiness={"cap-run-example": True},
    )

    withdraw_runtime_readiness(bot, ("cap-run-example",))

    assert bot.runtime_capability_readiness == {}
    assert registry.values == {"cap-run-example": False}


def test_probe_refreshes_dynamic_readiness_and_is_removed_on_withdraw() -> None:
    registry = FakeRegistry()
    state = {"ready": False}
    bot = SimpleNamespace(capability_registry=registry)

    assert publish_runtime_readiness_probe(bot, "cap-run-example", lambda: state["ready"]) is False
    assert bot.runtime_capability_readiness == {"cap-run-example": False}

    state["ready"] = True
    assert refresh_runtime_readiness(bot, "cap-run-example") is True
    assert bot.runtime_capability_readiness == {"cap-run-example": True}
    assert registry.values == {"cap-run-example": True}

    withdraw_runtime_readiness(bot, ("cap-run-example",))
    assert not hasattr(bot, "runtime_capability_readiness_probes")
    assert refresh_runtime_readiness(bot, "cap-run-example") is None
