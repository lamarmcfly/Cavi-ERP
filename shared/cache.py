"""Redis client factory.

Redis serves two roles in Cavi ERP:
  1. A read-through cache for hot lookups (e.g. Ticker price snapshots).
  2. A lightweight pub/sub channel agents use to emit events that n8n
     workflows subscribe to.
"""
from __future__ import annotations

from shared.settings import get_settings


def get_client():
    # Imported lazily so modules that only need the domain logic (e.g. tests of
    # the Vault library) don't require the redis driver to be installed.
    import redis

    settings = get_settings()
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        decode_responses=True,
    )
