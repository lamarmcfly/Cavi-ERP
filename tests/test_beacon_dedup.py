"""Tests for Beacon dedup stores (H5 — durable, fleet-wide alert suppression).

The in-memory store keeps the previous behavior; the Redis store makes dedup
survive restarts and be shared across replicas — so a storm pages a human once
fleet-wide, not once per process. fakeredis stands in for Redis.
"""
from __future__ import annotations

import fakeredis

from agents.beacon.beacon import (
    Beacon,
    CollectingChannel,
    InMemoryDedupStore,
    RedisDedupStore,
)


# --------------------------------------------------------------------------- #
# Store-level behavior
# --------------------------------------------------------------------------- #
def test_inmemory_dedup_within_and_past_window():
    now = [1_000.0]
    store = InMemoryDedupStore(clock=lambda: now[0])
    assert store.is_duplicate("k", 300) is False   # first sighting
    assert store.is_duplicate("k", 300) is True     # within the window
    now[0] += 301
    assert store.is_duplicate("k", 300) is False    # window elapsed


def test_redis_dedup_suppresses_within_window():
    store = RedisDedupStore(client=fakeredis.FakeStrictRedis(decode_responses=True))
    assert store.is_duplicate("k", 300) is False
    assert store.is_duplicate("k", 300) is True


def test_redis_dedup_is_shared_across_instances():
    # The point of H5: a *fresh* store on the same Redis (a restart or a second
    # replica) still sees the key — in-memory dedup would not.
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    assert RedisDedupStore(client=redis).is_duplicate("k", 300) is False
    assert RedisDedupStore(client=redis).is_duplicate("k", 300) is True


# --------------------------------------------------------------------------- #
# Beacon wired to the Redis store
# --------------------------------------------------------------------------- #
def _beacon(redis) -> tuple[Beacon, CollectingChannel]:
    hermes = CollectingChannel()
    beacon = Beacon(
        {"log": CollectingChannel(), "hermes": hermes},
        dedup=RedisDedupStore(client=redis),
    )
    return beacon, hermes


def test_beacon_with_redis_dedup_collapses_a_storm():
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    beacon, hermes = _beacon(redis)
    first = beacon.notify("deadletter.ledger.entry", {"entry_id": "e1"})
    second = beacon.notify("deadletter.ledger.entry", {"entry_id": "e1"})
    assert first is not None and second is None
    assert len(hermes.alerts) == 1


def test_beacon_dedup_survives_a_restart():
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    b1, h1 = _beacon(redis)
    assert b1.notify("deadletter.ledger.entry", {"entry_id": "e1"}) is not None
    # Beacon "restarts": a brand-new instance on the same Redis. The alert is
    # still suppressed — a human is not re-paged.
    b2, h2 = _beacon(redis)
    assert b2.notify("deadletter.ledger.entry", {"entry_id": "e1"}) is None
    assert len(h1.alerts) == 1 and len(h2.alerts) == 0
