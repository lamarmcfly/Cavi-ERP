"""Export and verify the hash-chained audit log (W7 / FR7).

    python -m scripts.audit_export            # JSONL export of the chained log
    python -m scripts.audit_export --verify   # recompute the chain, report breaks

The export is self-verifying: each line carries the event envelope plus its
recorded ``chain_hash``, so anyone holding the file can re-run verification
with ``shared.audit.verify_chain`` — no access to (or trust in) the database
required. ``--verify`` exits non-zero on any break, so it can gate a backup
job or run as a scheduled integrity check.

Row shaping (`export_row`) and chain checking (`shared/audit.py`) are pure and
import-safe; only `_fetch_chained` touches the database.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable, Mapping

from shared.audit import verify_chain


def export_row(row: Mapping) -> dict:
    """One export line: the event envelope exactly as it was hashed, plus the
    chain fields (`seq`, `chain_hash`) and the storage timestamp."""
    return {
        "seq": row["seq"],
        "event": {
            "id": str(row["id"]),
            "subject": row["subject"],
            "schema_version": row["schema_version"],
            "source": row["source"],
            "correlation_id": (
                str(row["correlation_id"]) if row["correlation_id"] else None
            ),
            "tenant_id": row["tenant_id"],
            "payload": row["payload"],
        },
        "chain_hash": row["chain_hash"],
        "recorded_at": str(row["created_at"]),
    }


def verify_export(lines: Iterable[Mapping]) -> list[str]:
    """Verify an export (as produced by `export_row`) against the chain."""
    return verify_chain([(line["event"], line["chain_hash"]) for line in lines])


def _fetch_chained() -> list[dict]:
    from shared.db import connection

    with connection() as conn:
        cursor = conn.execute(
            "SELECT seq, id, subject, schema_version, source, correlation_id, "
            "       tenant_id, payload, chain_hash, created_at "
            "FROM event_log WHERE chain_hash IS NOT NULL ORDER BY seq"
        )
        description = cursor.description
        if description is None:  # a SELECT always has one; satisfies typing
            return []
        columns = [c.name for c in description]
        return [dict(zip(columns, values)) for values in cursor.fetchall()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify", action="store_true",
        help="recompute the chain and report breaks instead of exporting",
    )
    args = parser.parse_args(argv)

    lines = [export_row(row) for row in _fetch_chained()]
    if args.verify:
        problems = verify_export(lines)
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
        print(f"audit chain intact: {len(lines)} events verified")
        return 0

    for line in lines:
        print(json.dumps(line, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
