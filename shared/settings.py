"""Centralized configuration for Cavi ERP.

All services (the six agents, the schema registry, and the bootstrap
scripts) read their connection details from here so that there is a single
source of truth. Values come from environment variables (see .env.example),
which keeps secrets out of the codebase and lets us swap configs per
environment (local / staging / prod) without code changes.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CAVI_", extra="ignore")

    # --- PostgreSQL (system of record + schema registry) ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "cavi_erp"
    postgres_user: str = "cavi"
    postgres_password: str = "change-me"

    # --- Redis (cache + lightweight event bus) ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # --- n8n middleware ---
    n8n_base_url: str = "http://localhost:5678"

    # --- Schema registry ---
    schema_registry_dir: str = "schema_registry/schemas"

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
