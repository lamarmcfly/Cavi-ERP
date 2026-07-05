"""Centralized configuration for Cavi ERP.

All services (the six agents, the schema registry, and the bootstrap
scripts) read their connection details from here so that there is a single
source of truth. Values come from environment variables (see .env.example),
which keeps secrets out of the codebase and lets us swap configs per
environment (local / staging / prod) without code changes.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CAVI_", extra="ignore")

    # --- PostgreSQL (system of record + schema registry) ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "cavi_erp"
    postgres_user: str = "cavi"
    postgres_password: str = "change-me"
    # Connection-pool bounds (see shared/db.py).
    postgres_pool_min_size: int = 1
    postgres_pool_max_size: int = 10

    # --- Redis (cache + event bus) ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    # Bus delivery tuning (Redis Streams consumer groups — see
    # docs/adr/0003-bus-durability.md). BaseAgent consumes with XREADGROUP and
    # only XACKs after handle() succeeds, so a crashed consumer's in-flight
    # message is redelivered instead of lost (at-least-once processing).
    # How long XREADGROUP blocks waiting for new messages, in ms.
    stream_block_ms: int = 5000
    # Max messages pulled per XREADGROUP / reclaim pass (COUNT).
    stream_batch_size: int = 10
    # A message whose handle() has failed this many times is treated as poison:
    # dead-lettered and acked so it can't wedge the group behind it.
    stream_max_deliveries: int = 5
    # Only reclaim a pending message once it has been idle (unacked) this long,
    # in ms — the retry backoff, and the guard against stealing a message a live
    # sibling is still working.
    stream_reclaim_idle_ms: int = 30000
    # Approximate cap on entries kept per stream (XADD MAXLEN ~). The durable
    # replay source is event_log, not the stream, so the stream only needs enough
    # history to cover redelivery/reclaim.
    stream_maxlen: int = 10000

    # --- n8n middleware ---
    n8n_base_url: str = "http://localhost:5678"

    # --- Schema registry ---
    schema_registry_dir: str = "schema_registry/schemas"

    # --- Observability ---
    # Health/metrics HTTP server bind. Port 0 = disabled (an agent starts it only
    # when a port is set). See shared/health.py.
    health_host: str = "0.0.0.0"
    health_port: int = 0
    # Root log level for the JSON logger (see shared/logging.py).
    log_level: str = "INFO"

    # --- Security ---
    # Shared secret required on the Vault HTTP service (/vend, /sign). Empty =
    # the service refuses to sign (fail closed). Set per environment.
    vault_api_secret: str = ""
    # Optional comma-separated tenant allowlist for the Vault service. Empty =
    # any tenant with stored credentials (default-allow); set = default-deny to
    # the named tenants only (mirrors the inventory adapter's allowlist gate).
    vault_tenant_allowlist: str = ""
    # HMAC signing secret shared with n8n for inbound webhook verification.
    webhook_signing_secret: str = ""

    # --- Hermes bridge (Beacon alert delivery) ---
    # The gateway's POST /notify endpoint. Empty string = no real delivery, so
    # Beacon degrades to log-only (safe default for local/dev + tests).
    # Accepts the CAVI_-prefixed name or the bare HERMES_WEBHOOK_URL already used
    # by the n8n container in docker-compose.yml, so both stay in sync.
    hermes_webhook_url: str = Field(
        default="",
        validation_alias=AliasChoices("CAVI_HERMES_WEBHOOK_URL", "HERMES_WEBHOOK_URL"),
    )
    # Where Beacon sends human alerts through Hermes. Owner's Telegram chat.
    beacon_target: str = "telegram:1370595013"

    @property
    def vault_tenant_allowset(self) -> frozenset[str]:
        """Parsed allowlist; empty set means default-allow."""
        return frozenset(t.strip() for t in self.vault_tenant_allowlist.split(",") if t.strip())

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so config is parsed once per process."""
    return Settings()
