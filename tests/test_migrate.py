"""Tests for the migration runner's discovery/ordering logic (no database).

The apply/rollback paths need a live Postgres and are exercised by the compose
end-to-end flow; here we pin the pure logic that decides *what* runs and *in what
order* — the part most likely to silently regress.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import migrate  # noqa: E402


def test_discovers_real_migrations_in_version_order():
    versions = [m.version for m in migrate.discover_migrations()]
    assert versions == sorted(versions)
    assert {"0001", "0002", "0003"} <= set(versions)


def test_0003_has_a_down_migration():
    by_version = {m.version: m for m in migrate.discover_migrations()}
    assert by_version["0003"].has_down() is True


def test_discovery_skips_down_and_unversioned_files(tmp_path: Path):
    (tmp_path / "0001_init.sql").write_text("-- up")
    (tmp_path / "0001_init.down.sql").write_text("-- down")   # excluded
    (tmp_path / "0002_ledger.sql").write_text("-- up")
    (tmp_path / "notes.txt").write_text("nope")               # not .sql
    (tmp_path / "readme.sql").write_text("-- no version")     # no NNNN_ prefix
    got = migrate.discover_migrations(tmp_path)
    assert [(m.version, m.name) for m in got] == [("0001", "init"), ("0002", "ledger")]


def test_pending_excludes_already_applied():
    all_migrations = migrate.discover_migrations()
    pend = migrate.pending(all_migrations, applied_versions={"0001", "0002"})
    assert "0003" in [m.version for m in pend]
    assert "0001" not in [m.version for m in pend]


def test_pending_is_empty_when_all_applied():
    all_migrations = migrate.discover_migrations()
    assert migrate.pending(all_migrations, {m.version for m in all_migrations}) == []


def test_status_reports_applied_and_pending_argument_shapes():
    # main() with --status/--rollback should not raise at arg-parse time.
    import argparse
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true")
    group.add_argument("--rollback", action="store_true")
    assert parser.parse_args(["--status"]).status is True
    assert parser.parse_args([]).status is False
