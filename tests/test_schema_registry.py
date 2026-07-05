"""Tests for the finalized centralized schema registry.

Scope: the 13 *canonical* event contracts this task defines (the ERP-integration
bus — vault / ledger.query / forge.write / ticker.event / mapper.transform /
beacon.report, plus the deadletter envelope). Legacy domain contracts
(ledger.entry, forge.completed, ticker.price, …) keep their own tests and are
intentionally not held to the canonical house rules here.

Two layers of guarantee:

1. Meta-structure — each canonical schema is a valid JSON Schema and obeys the
   house rules: $schema/title/description present, root object with
   additionalProperties:false, required lists *every* root property (no optional
   root fields), tenant_id is a non-empty string wherever it appears, and every
   ``*_at`` field is an ISO-8601 date-time string.
2. Behavior — each canonical schema accepts a valid fixture and rejects an
   invalid one, so the contracts actually discriminate.

The suite also exercises bootstrap's filename->(subject, version) parsing and
discovery so a mis-named contract file fails here rather than at deploy time.
It runs with no infrastructure (no Postgres/Redis), consistent with the rest of
the scaffold.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "schema_registry" / "schemas"

# Make `scripts/bootstrap.py` importable (scripts/ is not a package on sys.path).
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import bootstrap  # noqa: E402

# Enforce `format` (e.g. date-time) where the runtime supports it; structural
# rules below are always enforced regardless of optional format libs.
FORMAT_CHECKER = FormatChecker()

# The 13 canonical contracts this task finalizes. deadletter.envelope is the
# strict *envelope* (id/source/error), so it carries no tenant_id / *_at fields;
# the meta checks below apply the tenant_id / date-time rules only where present.
CANONICAL_SUBJECTS = [
    "vault.secret.granted",
    "vault.secret.denied",
    "ledger.query.completed",
    "ledger.query.failed",
    "forge.write.requested",
    "forge.write.approved",
    "forge.write.rejected",
    "forge.write.completed",
    "ticker.event.received",
    "mapper.transform.completed",
    "mapper.transform.failed",
    "beacon.report.generated",
    "deadletter.envelope",
]


def _schema_path(subject: str) -> Path:
    return SCHEMAS_DIR / f"{subject}.v1.json"


def _load(subject: str) -> dict:
    return json.loads(_schema_path(subject).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Fixtures: one valid + one invalid instance per canonical contract.
# Each `invalid` violates a structural rule (missing-required / wrong-type /
# additionalProperties / minLength) enforced in every environment.
# --------------------------------------------------------------------------- #
_ISO = "2026-07-04T12:00:00Z"

FIXTURES: dict[str, dict] = {
    "vault.secret.granted": {
        "valid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "token_id": "tok_abc123",
            "scopes": ["netsuite:rest:read", "netsuite:rest:write"],
            "expires_at": _ISO,
            "issued_at": _ISO,
        },
        # missing required token_id
        "invalid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "scopes": ["netsuite:rest:read"],
            "expires_at": _ISO,
            "issued_at": _ISO,
        },
    },
    "vault.secret.denied": {
        "valid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "sap",
            "reason": "no active grant for platform",
            "requested_at": _ISO,
        },
        # empty tenant_id violates minLength:1
        "invalid": {
            "tenant_id": "",
            "erp_platform": "sap",
            "reason": "no active grant for platform",
            "requested_at": _ISO,
        },
    },
    "ledger.query.completed": {
        "valid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "subject": "invoices",
            "filters": {"status": "open"},
            "result_count": 2,
            "payload": [{"id": "INV-1"}, {"id": "INV-2"}],
            "schema_version": "cavi.invoice.v1",
            "queried_at": _ISO,
        },
        # result_count must be an integer, not a string
        "invalid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "subject": "invoices",
            "filters": {"status": "open"},
            "result_count": "2",
            "payload": [],
            "schema_version": "cavi.invoice.v1",
            "queried_at": _ISO,
        },
    },
    "ledger.query.failed": {
        "valid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "subject": "invoices",
            "filters": {},
            "reason": "upstream timeout",
            "failed_at": _ISO,
        },
        # extra root property rejected by additionalProperties:false
        "invalid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "subject": "invoices",
            "filters": {},
            "reason": "upstream timeout",
            "failed_at": _ISO,
            "unexpected": True,
        },
    },
    "forge.write.requested": {
        "valid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "operation": "create",
            "target_module": "SalesOrder",
            "payload": {"customer": "ACME", "total": 100},
            "requested_by": "agent:forge",
            "requested_at": _ISO,
            "diff_preview": "+ SalesOrder ACME $100",
        },
        # missing required diff_preview
        "invalid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "operation": "create",
            "target_module": "SalesOrder",
            "payload": {"customer": "ACME"},
            "requested_by": "agent:forge",
            "requested_at": _ISO,
        },
    },
    "forge.write.approved": {
        "valid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "operation": "create",
            "approved_by": "user:owner",
            "approved_at": _ISO,
            "write_id": "w_001",
        },
        # missing required write_id
        "invalid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "operation": "create",
            "approved_by": "user:owner",
            "approved_at": _ISO,
        },
    },
    "forge.write.rejected": {
        "valid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "operation": "create",
            "rejected_by": "user:owner",
            "rejected_at": _ISO,
            "reason": "duplicate order",
        },
        # empty reason violates minLength:1
        "invalid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "operation": "create",
            "rejected_by": "user:owner",
            "rejected_at": _ISO,
            "reason": "",
        },
    },
    "forge.write.completed": {
        "valid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "operation": "create",
            "write_id": "w_001",
            "erp_confirmation": {"id": "SO-123", "revision": 1},
            "completed_at": _ISO,
        },
        # erp_confirmation must be an object, not a string
        "invalid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "operation": "create",
            "write_id": "w_001",
            "erp_confirmation": "SO-123",
            "completed_at": _ISO,
        },
    },
    "ticker.event.received": {
        "valid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "event_type": "invoice.paid",
            "raw_payload": {"id": "INV-1", "amount": 100},
            "received_at": _ISO,
            "routed_to": "ledger.query",
        },
        # missing required routed_to
        "invalid": {
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "event_type": "invoice.paid",
            "raw_payload": {"id": "INV-1"},
            "received_at": _ISO,
        },
    },
    "mapper.transform.completed": {
        "valid": {
            "tenant_id": "tenant-acme",
            "source_erp": "sap",
            "source_schema": "sap.invoice.v2",
            "target_schema": "cavi.invoice.v1",
            "input_hash": "sha256:deadbeef",
            "output": {"id": "INV-1", "total": 100},
            "transformed_at": _ISO,
        },
        # output must be an object, not an array
        "invalid": {
            "tenant_id": "tenant-acme",
            "source_erp": "sap",
            "source_schema": "sap.invoice.v2",
            "target_schema": "cavi.invoice.v1",
            "input_hash": "sha256:deadbeef",
            "output": [],
            "transformed_at": _ISO,
        },
    },
    "mapper.transform.failed": {
        "valid": {
            "tenant_id": "tenant-acme",
            "source_erp": "sap",
            "source_schema": "sap.invoice.v2",
            "reason": "unmappable field 'x'",
            "failed_at": _ISO,
        },
        # extra root property rejected by additionalProperties:false
        "invalid": {
            "tenant_id": "tenant-acme",
            "source_erp": "sap",
            "source_schema": "sap.invoice.v2",
            "reason": "unmappable field 'x'",
            "failed_at": _ISO,
            "target_schema": "cavi.invoice.v1",
        },
    },
    "beacon.report.generated": {
        "valid": {
            "tenant_id": "tenant-acme",
            "report_type": "weekly_ops",
            "kpis": {"throughput": 42, "error_rate": 0.01},
            "period_start": _ISO,
            "period_end": _ISO,
            "generated_at": _ISO,
            "delivery_targets": ["telegram:1370595013"],
        },
        # delivery_targets must be an array, not a string
        "invalid": {
            "tenant_id": "tenant-acme",
            "report_type": "weekly_ops",
            "kpis": {"throughput": 42},
            "period_start": _ISO,
            "period_end": _ISO,
            "generated_at": _ISO,
            "delivery_targets": "telegram:1370595013",
        },
    },
    "deadletter.envelope": {
        "valid": {
            "id": "00000000-0000-0000-0000-000000000000",
            "subject": "ledger.entry",
            "schema_version": 1,
            "source": "forge",
            "correlation_id": None,
            "tenant_id": "tenant-acme",
            "payload": {"entry_id": "e1", "not": "valid against its own contract"},
            "error": "payload violated ledger.entry.v1",
        },
        # missing required error
        "invalid": {
            "id": "00000000-0000-0000-0000-000000000000",
            "subject": "ledger.entry",
            "schema_version": 1,
            "source": "forge",
            "correlation_id": None,
            "tenant_id": "tenant-acme",
            "payload": {"entry_id": "e1"},
        },
    },
}


# --------------------------------------------------------------------------- #
# Discovery sanity
# --------------------------------------------------------------------------- #
def test_all_canonical_schema_files_exist():
    missing = [s for s in CANONICAL_SUBJECTS if not _schema_path(s).exists()]
    assert not missing, f"canonical schema files missing: {missing}"


def test_every_canonical_subject_has_fixtures():
    assert set(FIXTURES) == set(CANONICAL_SUBJECTS)


# --------------------------------------------------------------------------- #
# Meta-structure (canonical contracts only)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("subject", CANONICAL_SUBJECTS)
def test_schema_meta_structure(subject: str):
    schema = _load(subject)

    # Self-consistent JSON Schema.
    Draft202012Validator.check_schema(schema)

    # House rules.
    assert schema.get("$schema"), f"{subject}: missing $schema"
    assert schema.get("title"), f"{subject}: missing title"
    assert schema.get("description"), f"{subject}: missing description"
    assert schema.get("type") == "object", f"{subject}: root type must be object"
    assert schema.get("additionalProperties") is False, (
        f"{subject}: root must set additionalProperties:false"
    )

    required = schema.get("required")
    properties = schema.get("properties")
    assert isinstance(required, list) and required, f"{subject}: required must be non-empty"
    assert isinstance(properties, dict) and properties, f"{subject}: properties must be non-empty"

    # No optional root fields: required lists every property.
    assert set(required) == set(properties), (
        f"{subject}: required {sorted(required)} != properties {sorted(properties)}"
    )

    # tenant_id, where present, is a non-empty string — except on the dead-letter
    # envelope, where it is envelope metadata and nullable (like correlation_id).
    tenant = properties.get("tenant_id")
    if tenant is not None and subject != "deadletter.envelope":
        assert tenant.get("type") == "string" and tenant.get("minLength") == 1, (
            f"{subject}: tenant_id must be a non-empty string"
        )

    # Every *_at field is an ISO-8601 date-time string.
    for name, spec in properties.items():
        if name.endswith("_at"):
            assert spec.get("type") == "string" and spec.get("format") == "date-time", (
                f"{subject}: {name} must be a date-time string"
            )


# --------------------------------------------------------------------------- #
# Behavior: valid accepted, invalid rejected
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("subject", CANONICAL_SUBJECTS)
def test_valid_fixture_accepted(subject: str):
    validator = Draft202012Validator(_load(subject), format_checker=FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(FIXTURES[subject]["valid"]), key=str)
    assert not errors, f"{subject}: valid fixture rejected: {[e.message for e in errors]}"


@pytest.mark.parametrize("subject", CANONICAL_SUBJECTS)
def test_invalid_fixture_rejected(subject: str):
    validator = Draft202012Validator(_load(subject), format_checker=FORMAT_CHECKER)
    assert not validator.is_valid(FIXTURES[subject]["invalid"]), (
        f"{subject}: invalid fixture unexpectedly passed validation"
    )


# --------------------------------------------------------------------------- #
# bootstrap parsing + discovery
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "filename,expected",
    [
        ("vault.secret.granted.v1.json", ("vault.secret.granted", 1)),
        ("deadletter.envelope.v1.json", ("deadletter.envelope", 1)),
        ("forge.write.completed.v2.json", ("forge.write.completed", 2)),
    ],
)
def test_parse_subject_version(filename, expected):
    assert bootstrap.parse_subject_version(filename) == expected


def test_parse_subject_version_rejects_unversioned():
    with pytest.raises(ValueError):
        bootstrap.parse_subject_version("vault.secret.granted.json")


def test_discover_schemas_includes_every_canonical_subject():
    records = bootstrap.discover_schemas(SCHEMAS_DIR)
    found = {r["subject"] for r in records}
    missing = set(CANONICAL_SUBJECTS) - found
    assert not missing, f"discover_schemas missed: {sorted(missing)}"
    # Every discovered record carries an integer version and a parsed schema doc.
    for rec in records:
        assert isinstance(rec["version"], int)
        assert isinstance(rec["schema"], dict) and rec["schema"].get("$schema")
