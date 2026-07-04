"""The event envelope every agent speaks.

Agents never call each other directly. They emit `Event` envelopes onto the
bus (Redis pub/sub), and n8n workflows route those events to the agents that
care about them. The envelope carries routing + versioning metadata; the
domain-specific body lives in `payload` and is validated against the schema
registry using (`subject`, `schema_version`).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Event:
    subject: str                       # e.g. "ledger.entry"
    schema_version: int                # which registered schema `payload` follows
    source: str                        # emitting agent name, e.g. "forge"
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # correlation_id ties together every event in one business transaction so
    # a single sale can be traced across Forge -> Ledger -> Beacon.
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "schema_version": self.schema_version,
            "source": self.source,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            id=data["id"],
            subject=data["subject"],
            schema_version=data["schema_version"],
            source=data["source"],
            correlation_id=data.get("correlation_id"),
            payload=data["payload"],
        )
