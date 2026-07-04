"""Load the file-based JSON Schemas into the Postgres schema_registry table.

Globs every ``schema_registry/schemas/*.json`` contract, derives the
``(subject, version)`` registry key from the filename, and upserts the schema
document into the ``schema_registry`` table with an explicit ``created_at``.

The table is what lets Postgres enforce the ``event_log`` foreign key
``(subject, schema_version) -> schema_registry (subject, version)``, so run this
after ``docker compose up`` before events start flowing.

    python -m scripts.bootstrap

Idempotent: re-running upserts (``ON CONFLICT (subject, version)``). The
parsing/discovery helpers are import-safe (no DB connection at import time) so
the test suite can exercise them without a live database.

Note on columns: the physical column is ``json_schema`` (see
``schema_registry/migrations/0001_init.sql``); it holds the "schema" document in
the ``(subject, version, schema, created_at)`` registry contract.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from shared.settings import get_settings

log = logging.getLogger("cavi.bootstrap")

# matches "<subject>.v<version>.json", e.g. "vault.secret.granted.v1.json"
_NAME = re.compile(r"^(?P<subject>.+)\.v(?P<version>\d+)\.json$")

UPSERT_SQL = """
    INSERT INTO schema_registry (subject, version, json_schema, created_at)
    VALUES (%(subject)s, %(version)s, %(json_schema)s, %(created_at)s)
    ON CONFLICT (subject, version)
    DO UPDATE SET json_schema = EXCLUDED.json_schema,
                  created_at  = EXCLUDED.created_at
"""


def parse_subject_version(filename: str) -> tuple[str, int]:
    """Derive ``(subject, version)`` from a schema filename.

    ``vault.secret.granted.v1.json`` -> ``("vault.secret.granted", 1)``.
    Raises ``ValueError`` on a name that does not carry a ``.v<n>.json`` suffix.
    """
    match = _NAME.match(filename)
    if not match:
        raise ValueError(
            f"schema filename {filename!r} does not match '<subject>.v<n>.json'"
        )
    return match["subject"], int(match["version"])


def discover_schemas(schemas_dir: str | Path | None = None) -> list[dict]:
    """Load every ``*.json`` contract and return upsert-ready records.

    Each record is ``{subject, version (int), schema (parsed dict), path}``.
    Files that do not match ``<subject>.v<n>.json`` are skipped with a warning
    so stray notes/fixtures in the directory don't abort the load.
    """
    directory = Path(schemas_dir or get_settings().schema_registry_dir)
    records: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            subject, version = parse_subject_version(path.name)
        except ValueError:
            log.warning("skipping unrecognized schema file %s", path.name)
            continue
        schema = json.loads(path.read_text(encoding="utf-8"))
        records.append(
            {"subject": subject, "version": version, "schema": schema, "path": path}
        )
    return records


def load_schemas(schemas_dir: str | Path | None = None) -> int:
    """Discover + upsert every contract into ``schema_registry``. Returns count."""
    # Imported lazily so the parse/discover helpers stay usable (and testable)
    # without psycopg / a live database configured.
    from shared.db import connection

    records = discover_schemas(schemas_dir)
    now = datetime.now(timezone.utc)
    with connection() as conn:
        for rec in records:
            conn.execute(
                UPSERT_SQL,
                {
                    "subject": rec["subject"],
                    "version": rec["version"],
                    "json_schema": json.dumps(rec["schema"]),
                    "created_at": now,
                },
            )
            log.info("registered %s v%s", rec["subject"], rec["version"])
    return len(records)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    count = load_schemas()
    log.info("bootstrap complete — %d schema(s) registered", count)
