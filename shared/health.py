"""Health + metrics HTTP surface for the pub/sub agents.

The Ledger/Forge/Ticker/Mapper/Beacon agents are bare pub/sub loops with no
network surface, so an orchestrator can't tell if they're alive or ready. This
adds a small stdlib server exposing:

  * ``GET /healthz`` — liveness (the process is up).
  * ``GET /readyz``  — readiness: runs injected checks (e.g. redis/postgres
                       reachable); 200 if all pass, 503 otherwise.
  * ``GET /metrics`` — Prometheus text exposition from the metrics registry.

The response builders are pure ``() -> (status, body)`` functions so they
unit-test without a socket; ``serve_health()`` runs the server in a daemon thread
an agent starts alongside ``run()``.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping

from shared.metrics import REGISTRY, Metrics

ReadinessCheck = Callable[[], bool]


def health_response() -> tuple[int, dict]:
    return 200, {"status": "ok"}


def readiness_response(checks: Mapping[str, ReadinessCheck]) -> tuple[int, dict]:
    """Run each readiness check; 200 only if all pass. A check that raises is
    treated as failing (not ready) rather than crashing the probe."""
    results: dict[str, bool] = {}
    all_ok = True
    for name, check in checks.items():
        try:
            passed = bool(check())
        except Exception:
            passed = False
        results[name] = passed
        all_ok = all_ok and passed
    return (200 if all_ok else 503), {"ready": all_ok, "checks": results}


def make_health_handler(
    *,
    readiness: Mapping[str, ReadinessCheck] | None = None,
    registry: Metrics = REGISTRY,
) -> type[BaseHTTPRequestHandler]:
    checks = dict(readiness or {})

    class HealthHandler(BaseHTTPRequestHandler):
        server_version = "CaviHealth/1.0"

        def _send(self, status: int, body, content_type: str = "application/json") -> None:
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(*health_response())
            elif self.path == "/readyz":
                self._send(*readiness_response(checks))
            elif self.path == "/metrics":
                self._send(200, registry.prometheus(), "text/plain; version=0.0.4")
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *args) -> None:  # keep stdout clean; we log ourselves
            return

    return HealthHandler


def serve_health(
    host: str,
    port: int,
    *,
    readiness: Mapping[str, ReadinessCheck] | None = None,
) -> threading.Thread:
    """Start the health server in a daemon thread; returns the thread."""
    httpd = ThreadingHTTPServer((host, port), make_health_handler(readiness=readiness))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="cavi-health")
    thread.start()
    return thread
