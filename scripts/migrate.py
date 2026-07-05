"""Apply / track / roll back Cavi ERP database migrations.

The compose stack runs the SQL in ``schema_registry/migrations/`` once, at first
DB init (``docker-entrypoint-initdb.d``) — so schema changes to an *existing*
database have no supported path and nothing records what's applied. This runner
fixes that: it tracks applied migrations in a ``schema_migrations`` table and
applies only the pending ones, in order, **each in its own transaction**.
Optional ``<version>_<name>.down.sql`` files enable rolling back the most recent
migration.

    python -m scripts.migrate             # apply all pending
    python -m scripts.migrate --status    # show applied vs pending
    python -m scripts.migrate --rollback  # revert the most recently applied

Idempotent: re-running applies nothing if up to date. The discovery/ordering
helpers are import-safe (no DB at import) so they unit-test without a database.
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("cavi.migrate")

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "schema_registry" / "migrations"

# "<version>_<name>.sql" — a forward migration. ".down.sql" files are excluded.
_UP = re.compile(r"^(?P<version>\d+)_(?P<name>.+)\.sql$")

CREATE_TRACKING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT        PRIMARY KEY,
    name       TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path

    @property
    def down_path(self) -> Path:
        return self.path.with_name(f"{self.version}_{self.name}.down.sql")

    def has_down(self) -> bool:
        return self.down_path.exists()


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Forward migrations in version order (``*.down.sql`` and stray files skipped)."""
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        if path.name.endswith(".down.sql"):
            continue
        match = _UP.match(path.name)
        if not match:
            log.warning("skipping unrecognized migration file %s", path.name)
            continue
        migrations.append(Migration(version=match["version"], name=match["name"], path=path))
    return migrations


def pending(all_migrations: list[Migration], applied_versions: set[str]) -> list[Migration]:
    """Migrations not yet recorded as applied, preserving order."""
    return [m for m in all_migrations if m.version not in applied_versions]


def _applied_versions(conn) -> set[str]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def apply_pending(directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration (each in its own transaction). Returns the
    versions applied this run."""
    from shared.db import connection

    with connection() as conn:
        conn.execute(CREATE_TRACKING)
        done = _applied_versions(conn)

    applied: list[str] = []
    for migration in discover_migrations(directory):
        if migration.version in done:
            continue
        with connection() as conn:  # own transaction — partial progress is safe
            conn.execute(migration.path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
        log.info("applied %s_%s", migration.version, migration.name)
        applied.append(migration.version)
    return applied


def status(directory: Path = MIGRATIONS_DIR) -> tuple[list[str], list[str]]:
    """Return (applied_versions, pending_versions)."""
    from shared.db import connection

    with connection() as conn:
        conn.execute(CREATE_TRACKING)
        done = _applied_versions(conn)
    all_migrations = discover_migrations(directory)
    applied = sorted(m.version for m in all_migrations if m.version in done)
    pend = [m.version for m in pending(all_migrations, done)]
    return applied, pend


def rollback_last(directory: Path = MIGRATIONS_DIR) -> str | None:
    """Roll back the most recently applied migration via its ``.down.sql``.
    Returns the version rolled back, or None if nothing is applied."""
    from shared.db import connection

    with connection() as conn:
        conn.execute(CREATE_TRACKING)
        row = conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    version = row[0]
    migration = next((m for m in discover_migrations(directory) if m.version == version), None)
    if migration is None or not migration.has_down():
        raise SystemExit(f"no down migration for {version}; cannot roll back")
    with connection() as conn:
        conn.execute(migration.down_path.read_text(encoding="utf-8"))
        conn.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
    log.info("rolled back %s", version)
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cavi ERP migration runner")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="show applied vs pending")
    group.add_argument("--rollback", action="store_true", help="revert the most recent migration")
    args = parser.parse_args(argv)

    if args.status:
        applied, pend = status()
        print(f"applied ({len(applied)}): {', '.join(applied) or '-'}")
        print(f"pending ({len(pend)}): {', '.join(pend) or '-'}")
        return 0
    if args.rollback:
        version = rollback_last()
        print(f"rolled back {version}" if version else "nothing to roll back")
        return 0

    applied = apply_pending()
    print(f"applied {len(applied)} migration(s): {', '.join(applied) or '-'}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
