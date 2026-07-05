"""Structured JSON logging.

`configure_logging()` installs a JSON formatter on the root logger so every line
is one JSON object — timestamp, level, logger, message, plus any structured
fields passed via ``extra=`` (e.g. agent / subject / correlation_id / tenant_id).
Structured logs are what make an event traceable across agents in a log
aggregator; the plain string logs the agents emit today can't be queried by
correlation_id.

Fields passed via ``extra`` that the default formatter would ignore are merged
into the JSON object here, so existing `log.info("...", extra={...})` calls light
up without changing every call site.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import IO

# Standard LogRecord attributes — anything else on a record is a structured extra.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO, *, stream: IO[str] | None = None) -> None:
    """Route the root logger through the JSON formatter. Idempotent."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
