"""Tests for Beacon — severity policy, routing, and dedup.

No infrastructure: channels are in-memory collectors and dedup is driven by a
fake clock.
"""
from agents.beacon.beacon import (
    Beacon,
    CollectingChannel,
    Severity,
    severity_for,
)


class FakeClock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _beacon(clock=None):
    logs, hermes = CollectingChannel(), CollectingChannel()
    beacon = Beacon(
        {"log": logs, "hermes": hermes},
        clock=clock or FakeClock(),
        dedup_window_seconds=300,
    )
    return beacon, logs, hermes


def test_dead_lettered_financial_event_is_critical():
    assert severity_for("deadletter.ledger.entry") is Severity.CRITICAL


def test_dead_lettered_nonfinancial_event_is_error():
    assert severity_for("deadletter.ticker.price") is Severity.ERROR


def test_business_rejection_is_warning():
    assert severity_for("ledger.rejected") is Severity.WARNING


def test_critical_routes_to_log_and_hermes():
    beacon, logs, hermes = _beacon()
    alert = beacon.notify("deadletter.ledger.entry", {"entry_id": "e1"})
    assert alert.severity is Severity.CRITICAL
    assert len(logs.alerts) == 1
    assert len(hermes.alerts) == 1  # critical pages a human


def test_info_event_only_logs():
    beacon, logs, hermes = _beacon()
    beacon.notify("ledger.posted", {"entry_id": "e1"})
    assert len(logs.alerts) == 1
    assert len(hermes.alerts) == 0  # routine facts don't page anyone


def test_duplicate_within_window_is_suppressed():
    clock = FakeClock()
    beacon, logs, hermes = _beacon(clock=clock)
    first = beacon.notify("deadletter.ledger.entry", {"entry_id": "e1"})
    second = beacon.notify("deadletter.ledger.entry", {"entry_id": "e1"})
    assert first is not None and second is None  # storm collapsed
    assert len(hermes.alerts) == 1

    clock.advance(301)  # past the dedup window
    third = beacon.notify("deadletter.ledger.entry", {"entry_id": "e1"})
    assert third is not None
    assert len(hermes.alerts) == 2


def test_distinct_identifiers_are_not_deduped():
    beacon, _, hermes = _beacon()
    beacon.notify("deadletter.ledger.entry", {"entry_id": "e1"})
    beacon.notify("deadletter.ledger.entry", {"entry_id": "e2"})
    assert len(hermes.alerts) == 2  # different entries, both alert
