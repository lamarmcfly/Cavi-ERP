"""Tests for the Vault agent's canonical event payloads.

The agent runtime needs redis to construct, so — like the rest of the suite —
these test the *pure* payload builders (`granted_payload` / `denied_payload`)
rather than the agent class, and validate their output against the real
`vault.secret.*` contracts through the SchemaRegistry. No infrastructure.

This is the emitter half of the canonical migration (issue #2): the schemas
landed in #1; here Vault is brought up to emit them.
"""
from __future__ import annotations

import jsonschema
import pytest

from agents.base.registry import SchemaRegistry
from agents.vault.agent import _iso, denied_payload, granted_payload
from agents.vault.vault import InMemoryCredentialStore, Vault

NETSUITE_CREDS = {
    "account_id": "1234567_SB1",
    "consumer_key": "ck_consumer_key_value",
    "consumer_secret": "cs_consumer_secret_value",
    "token_id": "ti_token_id_value",
    "token_secret": "ts_token_secret_value",
}
SECRET_VALUES = (NETSUITE_CREDS["consumer_secret"], NETSUITE_CREDS["token_secret"])


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry()


@pytest.fixture
def vault() -> Vault:
    v = Vault(store=InMemoryCredentialStore(), default_ttl=300)
    v.store_credentials("tenant-acme", "netsuite", NETSUITE_CREDS)
    return v


def test_granted_payload_matches_canonical_contract(vault: Vault, registry: SchemaRegistry):
    token = vault.vend_token("tenant-acme", "netsuite", now=1_000.0)
    payload = granted_payload(token)

    # Validates against vault.secret.granted.v1 (also enforces additionalProperties).
    registry.validate("vault.secret.granted", 1, payload)

    # Exactly the canonical field set — the migration adds token_id + issued_at,
    # which the legacy payload lacked.
    assert set(payload) == {
        "tenant_id", "erp_platform", "token_id", "scopes", "expires_at", "issued_at",
    }
    assert payload["token_id"] == "ti_token_id_value"
    assert payload["scopes"] == ["netsuite:rest:read", "netsuite:rest:write"]
    # Epoch floats are converted to ISO-8601 strings at the boundary.
    assert payload["issued_at"] == _iso(1_000.0)
    assert payload["expires_at"] == _iso(1_300.0)
    assert isinstance(payload["issued_at"], str) and isinstance(payload["expires_at"], str)


def test_granted_payload_leaks_no_secret_material(vault: Vault):
    token = vault.vend_token("tenant-acme", "netsuite", now=1_000.0)
    blob = repr(granted_payload(token))
    for secret in SECRET_VALUES:
        assert secret not in blob


def test_denied_payload_matches_canonical_contract(registry: SchemaRegistry):
    payload = denied_payload(
        "tenant-acme", "sap", "no active grant", requested_at="2026-07-04T12:00:00Z"
    )
    registry.validate("vault.secret.denied", 1, payload)
    assert set(payload) == {"tenant_id", "erp_platform", "reason", "requested_at"}
    assert payload["requested_at"] == "2026-07-04T12:00:00Z"


def test_denied_payload_stamps_requested_at_when_omitted(registry: SchemaRegistry):
    payload = denied_payload("tenant-acme", "sap", "no active grant")
    # A timestamp is always present (stamped at denial time) so the contract holds.
    assert payload["requested_at"]
    registry.validate("vault.secret.denied", 1, payload)


def test_regression_legacy_granted_payload_is_now_rejected(registry: SchemaRegistry):
    """The pre-migration payload (no token_id/issued_at, epoch expires_at) must
    fail the canonical contract — this is exactly what the migration fixes."""
    legacy = {
        "tenant_id": "tenant-acme",
        "erp_platform": "netsuite",
        "scopes": ["netsuite:rest:read"],
        "expires_at": 1_300.0,  # epoch float, not ISO — and token_id/issued_at missing
    }
    with pytest.raises(jsonschema.ValidationError):
        registry.validate("vault.secret.granted", 1, legacy)
