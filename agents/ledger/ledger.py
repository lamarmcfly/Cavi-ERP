"""Ledger — double-entry accounting core (domain logic).

Ledger is the financial system of record. It consumes well-formed `ledger.entry`
events and posts **balanced** journal entries. Two invariants live here that the
schema registry cannot express:

  * **Balance** — total debits must equal total credits (and both be > 0).
    JSON Schema can require ≥2 positive lines, but not that they sum to zero.
  * **Idempotency** — the event bus is at-least-once, so the same `entry_id`
    may arrive twice. Posting it twice would double the books, so a re-post is
    a no-op that reports `duplicate`.

The persistence layer is abstracted (`LedgerStore`) so tests run against an
in-memory store with no database.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol


class Direction(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class LedgerError(Exception):
    """Base class for Ledger failures."""


class UnbalancedEntry(LedgerError):
    """Debits != credits — a business rejection, not a schema failure."""


# --------------------------------------------------------------------------- #
# Domain model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class JournalLine:
    account: str
    direction: Direction
    amount_minor: int  # positive integer, minor units (e.g. cents)


@dataclass(frozen=True)
class JournalEntry:
    entry_id: str
    currency: str
    lines: tuple[JournalLine, ...]
    memo: str | None = None

    @property
    def total_debits(self) -> int:
        return sum(ln.amount_minor for ln in self.lines if ln.direction is Direction.DEBIT)

    @property
    def total_credits(self) -> int:
        return sum(ln.amount_minor for ln in self.lines if ln.direction is Direction.CREDIT)

    def is_balanced(self) -> bool:
        """A valid double-entry posting: debits == credits, and money moved."""
        return self.total_debits == self.total_credits and self.total_debits > 0

    @classmethod
    def from_payload(cls, payload: Mapping) -> "JournalEntry":
        """Build from a `ledger.entry` event payload (already schema-validated)."""
        lines = tuple(
            JournalLine(
                account=line["account"],
                direction=Direction(line["direction"]),
                amount_minor=int(line["amount_minor"]),
            )
            for line in payload["lines"]
        )
        return cls(
            entry_id=payload["entry_id"],
            currency=payload["currency"],
            lines=lines,
            memo=payload.get("memo"),
        )


@dataclass(frozen=True)
class PostResult:
    entry_id: str
    currency: str
    status: str  # "posted" | "duplicate"
    total_minor: int
    line_count: int


# --------------------------------------------------------------------------- #
# Persistence backends
# --------------------------------------------------------------------------- #
class LedgerStore(Protocol):
    def get(self, entry_id: str) -> JournalEntry | None: ...
    def insert_if_absent(self, entry: JournalEntry) -> bool: ...


class InMemoryLedgerStore:
    """Volatile store for tests and local runs. `insert_if_absent` is guarded by
    a lock so a concurrent double-post has exactly one winner — the in-memory
    analog of the Postgres ON CONFLICT guard."""

    def __init__(self) -> None:
        self._entries: dict[str, JournalEntry] = {}
        self._lock = threading.Lock()

    def get(self, entry_id: str) -> JournalEntry | None:
        return self._entries.get(entry_id)

    def insert_if_absent(self, entry: JournalEntry) -> bool:
        """Insert atomically; return True if inserted, False if already present."""
        with self._lock:
            if entry.entry_id in self._entries:
                return False
            self._entries[entry.entry_id] = entry
            return True


class PostgresLedgerStore:
    """Durable store backed by the `journal_entry`/`journal_line` tables
    (see schema_registry/migrations/0002_ledger.sql). Imports the db helper
    lazily so the domain logic is testable without psycopg installed."""

    def get(self, entry_id: str) -> JournalEntry | None:
        from shared.db import connection

        with connection() as conn:
            head = conn.execute(
                "SELECT currency, memo FROM journal_entry WHERE entry_id = %s",
                (entry_id,),
            ).fetchone()
            if head is None:
                return None
            rows = conn.execute(
                "SELECT account, direction, amount_minor FROM journal_line "
                "WHERE entry_id = %s ORDER BY id",
                (entry_id,),
            ).fetchall()
        lines = tuple(
            JournalLine(account=a, direction=Direction(d), amount_minor=int(m))
            for (a, d, m) in rows
        )
        return JournalEntry(entry_id=entry_id, currency=head[0], lines=lines, memo=head[1])

    def insert_if_absent(self, entry: JournalEntry) -> bool:
        """Insert the entry + its lines in one transaction, atomically deduped.

        The head INSERT uses ``ON CONFLICT (entry_id) DO NOTHING``; if it wrote
        no row the entry already existed, so we return False *without* inserting
        lines. This closes the previous check-then-write race where two workers
        could both pass an ``exists()`` check and one would crash on the PK.
        """
        from shared.db import connection

        with connection() as conn:  # single transaction (commit on success)
            cur = conn.execute(
                "INSERT INTO journal_entry (entry_id, currency, memo, total_minor) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (entry_id) DO NOTHING",
                (entry.entry_id, entry.currency, entry.memo, entry.total_debits),
            )
            if cur.rowcount == 0:
                return False  # already posted — idempotent no-op
            for line in entry.lines:
                conn.execute(
                    "INSERT INTO journal_line (entry_id, account, direction, amount_minor) "
                    "VALUES (%s, %s, %s, %s)",
                    (entry.entry_id, line.account, line.direction.value, line.amount_minor),
                )
            return True


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
class Ledger:
    def __init__(self, store: LedgerStore | None = None) -> None:
        self._store: LedgerStore = store or PostgresLedgerStore()

    def post(self, entry: JournalEntry) -> PostResult:
        """Post a balanced journal entry. Idempotent on `entry_id`.

        Raises `UnbalancedEntry` if debits != credits — a business rejection the
        caller should surface (not silently drop), since the data was valid but
        the accounting was wrong.
        """
        if not entry.is_balanced():
            raise UnbalancedEntry(
                f"entry {entry.entry_id}: debits {entry.total_debits} "
                f"!= credits {entry.total_credits}"
            )
        # Atomic insert-or-skip removes the check-then-write race: a concurrent
        # redelivery of the same entry_id has exactly one winner; the loser is
        # reported as a duplicate rather than double-posting or crashing.
        inserted = self._store.insert_if_absent(entry)
        status = "posted" if inserted else "duplicate"
        return PostResult(
            entry_id=entry.entry_id,
            currency=entry.currency,
            status=status,
            total_minor=entry.total_debits,
            line_count=len(entry.lines),
        )
