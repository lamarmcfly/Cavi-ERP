"""Concurrency test for Ledger idempotency (the C4 double-post fix).

`Ledger.post` now routes through the store's atomic `insert_if_absent`, so a
storm of concurrent redeliveries of the same entry_id has exactly one winner —
no double-post, no crash. The in-memory store's lock is the test-time analog of
the Postgres ON CONFLICT guard.
"""
from __future__ import annotations

import threading

from agents.ledger.ledger import InMemoryLedgerStore, JournalEntry, Ledger


def _balanced_entry() -> JournalEntry:
    return JournalEntry.from_payload({
        "entry_id": "00000000-0000-0000-0000-0000000000aa",
        "currency": "USD",
        "lines": [
            {"account": "1000-cash", "direction": "debit", "amount_minor": 2500},
            {"account": "4000-revenue", "direction": "credit", "amount_minor": 2500},
        ],
    })


def test_concurrent_double_post_has_exactly_one_winner():
    store = InMemoryLedgerStore()
    ledger = Ledger(store=store)
    entry = _balanced_entry()

    n = 8
    barrier = threading.Barrier(n)   # maximize contention: release all at once
    results: list[str] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        status = ledger.post(entry).status
        with results_lock:
            results.append(status)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("posted") == 1        # exactly one write won
    assert results.count("duplicate") == n - 1  # the rest deduped, none crashed
    assert store.get(entry.entry_id) is not None
