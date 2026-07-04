"""PostgreSQL connection helper.

Thin wrapper around psycopg so every agent opens connections the same way.
Kept deliberately small — agents own their own queries; this just hands
them a configured connection.
"""
from __future__ import annotations

import contextlib
from typing import Iterator

import psycopg

from shared.settings import get_settings


@contextlib.contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Yield a short-lived Postgres connection, committing on success."""
    settings = get_settings()
    conn = psycopg.connect(settings.postgres_dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
