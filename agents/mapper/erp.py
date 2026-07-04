"""Mapper — ERP-schema transformation (domain logic).

The version-coercion side of Mapper (`mapper.py`) upgrades an event payload
between registered *schema versions* of the same subject. This side is the
cross-ERP anti-corruption layer: it transforms a record from a **source ERP
schema** into a **target schema**, emitting the canonical
`mapper.transform.completed` / `mapper.transform.failed` events.

Two things distinguish it from version coercion:
  * Transforms are keyed by ``(source_schema, target_schema)`` — a direct ERP-to-
    canonical mapping, not a version graph.
  * Every source record is fingerprinted with a stable ``input_hash`` so a
    replayed transform is idempotent/dedupable downstream.

An unregistered mapping or a transform that raises is a *business* failure
(`ErpTransformError`), surfaced as `mapper.transform.failed` — not a crash — so a
bad record is visible and replayable rather than lost.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable, Mapping

from agents.mapper.mapper import MapperError

TransformFn = Callable[[dict], dict]


class ErpTransformError(MapperError):
    """A source record could not be transformed to the target schema."""


def input_hash(record: Mapping) -> str:
    """Stable fingerprint of a source record, for idempotency/dedupe.

    Canonical JSON (sorted keys, no whitespace) so logically-equal records hash
    identically regardless of key order. `default=str` keeps it total for values
    like Decimal/datetime.
    """
    canonical = json.dumps(dict(record), sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def completed_payload(
    tenant_id: str,
    source_erp: str,
    source_schema: str,
    target_schema: str,
    record: Mapping,
    output: Mapping,
    *,
    transformed_at: str,
) -> dict:
    return {
        "tenant_id": tenant_id,
        "source_erp": source_erp,
        "source_schema": source_schema,
        "target_schema": target_schema,
        "input_hash": input_hash(record),
        "output": dict(output),
        "transformed_at": transformed_at,
    }


def failed_payload(
    tenant_id: str, source_erp: str, source_schema: str, reason: str, *, failed_at: str
) -> dict:
    return {
        "tenant_id": tenant_id,
        "source_erp": source_erp,
        "source_schema": source_schema,
        "reason": reason,
        "failed_at": failed_at,
    }


class ErpTransformer:
    """Registers ``(source_schema, target_schema)`` transforms and applies them,
    producing canonical completed/failed payloads. `clock` is injectable so
    timestamps are deterministic under test."""

    def __init__(self, *, clock: Callable[[], str] = _now_iso) -> None:
        self._transforms: dict[tuple[str, str], TransformFn] = {}
        self._clock = clock

    def register(
        self, source_schema: str, target_schema: str, fn: TransformFn
    ) -> TransformFn:
        self._transforms[(source_schema, target_schema)] = fn
        return fn

    def transform_for(self, source_schema: str, target_schema: str):
        """Decorator form of `register`."""

        def decorator(fn: TransformFn) -> TransformFn:
            return self.register(source_schema, target_schema, fn)

        return decorator

    def transform(
        self,
        tenant_id: str,
        source_erp: str,
        source_schema: str,
        target_schema: str,
        record: Mapping,
    ) -> dict:
        """Transform a source record and return the `mapper.transform.completed`
        payload. Raises `ErpTransformError` (with a human reason) if no transform
        is registered or the transform itself fails."""
        fn = self._transforms.get((source_schema, target_schema))
        if fn is None:
            raise ErpTransformError(
                f"no transform registered for {source_schema} -> {target_schema}"
            )
        try:
            output = fn(dict(record))
        except Exception as exc:  # a transform bug is a business failure, not a crash
            raise ErpTransformError(
                f"transform {source_schema} -> {target_schema} failed: {exc}"
            ) from exc
        return completed_payload(
            tenant_id, source_erp, source_schema, target_schema, record, output,
            transformed_at=self._clock(),
        )

    def failure(
        self, tenant_id: str, source_erp: str, source_schema: str, reason: str
    ) -> dict:
        """Build a `mapper.transform.failed` payload stamped now."""
        return failed_payload(
            tenant_id, source_erp, source_schema, reason, failed_at=self._clock()
        )
