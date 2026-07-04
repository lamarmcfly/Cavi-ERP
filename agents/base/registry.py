"""Schema registry client.

Every event flowing between agents is validated against a versioned JSON
Schema stored in `schema_registry/schemas/`. Centralizing the contract here
means a producing agent and a consuming agent can evolve independently as
long as they both honor the registered schema.

Schemas are named `<subject>.v<version>.json`, e.g. `ledger.entry.v1.json`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from shared.settings import get_settings


class SchemaNotFound(Exception):
    pass


class SchemaRegistry:
    def __init__(self, schemas_dir: str | None = None) -> None:
        self._dir = Path(schemas_dir or get_settings().schema_registry_dir)
        self._cache: dict[str, dict[str, Any]] = {}

    def _path(self, subject: str, version: int) -> Path:
        return self._dir / f"{subject}.v{version}.json"

    def get(self, subject: str, version: int) -> dict[str, Any]:
        key = f"{subject}.v{version}"
        if key not in self._cache:
            path = self._path(subject, version)
            if not path.exists():
                raise SchemaNotFound(f"No schema registered for {key} at {path}")
            self._cache[key] = json.loads(path.read_text(encoding="utf-8"))
        return self._cache[key]

    def validate(self, subject: str, version: int, payload: dict[str, Any]) -> None:
        """Raise jsonschema.ValidationError if payload violates the contract."""
        jsonschema.validate(instance=payload, schema=self.get(subject, version))
