"""PostgreSQL connection helper — pooled.

A pooled ``connection()`` so every agent opens connections the same cheap way.
Under at-least-once redelivery the agents open a connection per event; a
per-call ``psycopg.connect`` (the previous behavior) meant a full TCP + auth
round-trip each time and unbounded connection churn under load. A process-wide
``ConnectionPool`` bounds concurrency and reuses connections.

The pool is created lazily on first use and configured from settings, so
importing this module never opens a socket. ``connection()`` keeps the same
transaction semantics as before: commit on clean exit, rollback on exception.
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Iterator

from shared.settings import get_settings

if TYPE_CHECKING:  # avoid importing psycopg at module load (keeps import light)
    import psycopg

_pool = None


def _get_pool():
    """Lazily create the process-wide connection pool."""
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool

        settings = get_settings()
        _pool = ConnectionPool(
            settings.postgres_dsn,
            min_size=settings.postgres_pool_min_size,
            max_size=settings.postgres_pool_max_size,
            open=True,
            name="cavi-erp",
        )
    return _pool


@contextlib.contextmanager
def connection() -> Iterator["psycopg.Connection"]:
    """Yield a pooled Postgres connection, committing on success.

    ``ConnectionPool.connection()`` already commits on clean exit, rolls back on
    exception, and returns the connection to the pool — so this wrapper just
    preserves the original call site (`with connection() as conn:`).
    """
    with _get_pool().connection() as conn:
        yield conn
