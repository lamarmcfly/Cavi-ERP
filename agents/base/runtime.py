"""Agent entrypoint helper — configure observability, then run.

Wraps the boilerplate every agent's ``__main__`` needs: JSON logging, an optional
health/metrics server, then the blocking run loop. Each agent's ``__main__``
becomes ``run_agent(TheAgent())`` and they all get the same operational surface
(structured logs + ``/healthz`` ``/readyz`` ``/metrics`` when a health port is set).
"""
from __future__ import annotations

import logging
from typing import Callable, Mapping

from agents.base.agent import BaseAgent
from shared.health import serve_health
from shared.logging import configure_logging
from shared.settings import get_settings

log = logging.getLogger("cavi.runtime")


def run_agent(
    agent: BaseAgent,
    *,
    readiness: Mapping[str, Callable[[], bool]] | None = None,
) -> None:
    """Configure logging + (optional) health server, then run the agent forever."""
    settings = get_settings()
    configure_logging(getattr(logging, settings.log_level.upper(), logging.INFO))
    if settings.health_port:
        serve_health(settings.health_host, settings.health_port, readiness=readiness)
        log.info(
            "%s health server on %s:%d",
            agent.name, settings.health_host, settings.health_port,
            extra={"agent": agent.name, "health_port": settings.health_port},
        )
    agent.run()
