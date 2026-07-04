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
        return sum(l.amount_minor for l in self.lines if l.direction is Direction.DEBIT)

    @property
    def total_credits(self) -> int:
        return sum(l.amount_minor for l in self.lines if l.direction is Direction.CREDIT)

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
    def exists(self, entry_id: str) -> bool: ...
    def get(self, entry_id: str) -> JournalEntry | None: ...
    def save(self, entry: JournalEntry) -> None: ...


class InMemoryLedgerStore:
    """Volatile store for tests and local runs."""

    def __init__(self) -> None:
        self._entries: dict[str, JournalEntry] = {}

    def exists(self, entry_id: str) -> bool:
        return entry_id in self._entries

    def get(self, entry_id: str) -> JournalEntry | None:
        return self._entries.get(entry_id)

    def save(self, entry: JournalEntry) -> None:
        self._entries[entry.entry_id] = entry


class PostgresLedgerStore:
    """Durable store backed by the `journal_entry`/`journal_line` tables
    (see schema_registry/migrations/0002_ledger.sql). Imports the db helper
    lazily so the domain logic is testable without psycopg installed."""

    def exists(self, entry_id: str) -> bool:
        from shared.db import connection

        with connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM journal_entry WHERE entry_id = %s", (entry_id,)
            ).fetchone()
        return row is not None

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

    def save(self, entry: JournalEntry) -> None:
        from shared.db import connection

        with connection() as conn:  # single transaction (commit on success)
            conn.execute(
                "INSERT INTO journal_entry (entry_id, currency, memo, total_minor) "
                "VALUES (%s, %s, %s, %s)",
                (entry.entry_id, entry.currency, entry.memo, entry.total_debits),
            )
            for line in entry.lines:
                conn.execute(
                    "INSERT INTO journal_line (entry_id, account, direction, amount_minor) "
                    "VALUES (%s, %s, %s, %s)",
                    (entry.entry_id, line.account, line.direction.value, line.amount_minor),
                )


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
        if self._store.exists(entry.entry_id):
            return PostResult(
                entry_id=entry.entry_id,
                currency=entry.currency,
                status="duplicate",
                total_minor=entry.total_debits,
                line_count=len(entry.lines),
            )
        if not entry.is_balanced():
            raise UnbalancedEntry(
                f"entry {entry.entry_id}: debits {entry.total_debits} "
                f"!= credits {entry.total_credits}"
            )
        self._store.save(entry)
        return PostResult(
            entry_id=entry.entry_id,
            currency=entry.currency,
            status="posted",
            total_minor=entry.total_debits,
            line_count=len(entry.lines),
        )
