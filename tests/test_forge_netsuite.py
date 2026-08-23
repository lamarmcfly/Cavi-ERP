"""Tests for the production NetSuite ERP client (Vault-signed writer + reader).

No live Vault or NetSuite: the HTTP transport is an injected stub returning
canned `httpx.Response` objects, so these prove the governance behavior of the
client itself —

  * every ERP request is signed via Vault's /sign first (secrets never here),
  * writes are external-id upserts keyed by ``op.idempotency_key``,
  * every refusal (missing config, Vault denial, ERP rejection, transport
    failure, unsupported operation) fails closed as `ErpWriteError`, and
  * the dry-run reader maps 404 to "does not exist" instead of guessing.
"""
from __future__ import annotations

import httpx
import pytest

from agents.forge.netsuite import VAULT_AUTH_HEADER, NetSuiteClient
from agents.forge.write import ErpWriteError, WriteOperation, WriteState

VAULT = "http://vault:8080"
BASE = "https://acct-sb1.suitetalk.api.netsuite.com/services/rest"
SECRET = "test-vault-secret"
SIGNED = "OAuth realm=\"ACCT\", oauth_signature=\"abc\""


def _op(
    operation: str = "create",
    state: WriteState = WriteState.APPROVED,
    target_external_id: str | None = None,
) -> WriteOperation:
    return WriteOperation(
        write_id="w1",
        tenant_id="tenant-acme",
        erp_platform="netsuite",
        operation=operation,
        target_module="journalEntry",
        payload={"memo": "posting", "currency": "USD"},
        requested_by="agent:forge",
        diff_preview="",
        state=state,
        target_external_id=target_external_id,
    )


RECORD_URL = f"{BASE}/record/v1/journalEntry/eid:cavi-w1"


class StubTransport:
    """Plays back canned responses (or raises canned exceptions), recording
    every call as (method, url, kwargs)."""

    def __init__(self, *responses) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._responses = list(responses)

    def __call__(self, method: str, url: str, **kwargs) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(transport: StubTransport, **overrides) -> NetSuiteClient:
    config = dict(
        vault_url=VAULT, vault_api_secret=SECRET, netsuite_rest_url=BASE,
        request=transport,
    )
    config.update(overrides)
    return NetSuiteClient(**config)


def _sign_ok() -> httpx.Response:
    return httpx.Response(200, json={"authorization_header": SIGNED})


# --------------------------------------------------------------------------- #
# apply() — Vault-signed, idempotency-keyed upsert
# --------------------------------------------------------------------------- #
def test_apply_signs_via_vault_then_upserts_by_external_id():
    transport = StubTransport(
        _sign_ok(),
        httpx.Response(204, headers={"Location": f"{BASE}/record/v1/journalEntry/123"}),
    )
    confirmation = _client(transport).apply(_op())

    # 1st call: Vault /sign, authenticated with the shared-secret header, asking
    # for exactly the request that will be sent to NetSuite.
    method, url, kwargs = transport.calls[0]
    assert (method, url) == ("POST", f"{VAULT}/sign")
    assert kwargs["headers"][VAULT_AUTH_HEADER] == SECRET
    assert kwargs["json"] == {
        "tenant_id": "tenant-acme", "erp_platform": "netsuite",
        "method": "PUT", "url": RECORD_URL,
    }

    # 2nd call: the upsert itself — PUT to the external-id endpoint so NetSuite
    # dedupes on the idempotency key, carrying Vault's Authorization header.
    method, url, kwargs = transport.calls[1]
    assert (method, url) == ("PUT", RECORD_URL)
    assert kwargs["headers"]["Authorization"] == SIGNED
    assert kwargs["json"] == {"memo": "posting", "currency": "USD"}

    assert confirmation["external_id"] == "cavi-w1"
    assert confirmation["status_code"] == 204
    assert confirmation["location"].endswith("/journalEntry/123")


def test_update_is_addressed_to_the_existing_record_and_confirms_it():
    transport = StubTransport(
        _sign_ok(), httpx.Response(200, json={"id": "123", "externalId": "cavi-w0"})
    )
    confirmation = _client(transport).apply(
        _op(operation="update", target_external_id="cavi-w0")
    )
    # The PUT goes to the TARGET record's external id — never to the fresh
    # write's own key, which cannot identify a pre-existing record.
    method, url, _ = transport.calls[1]
    assert (method, url) == ("PUT", f"{BASE}/record/v1/journalEntry/eid:cavi-w0")
    assert confirmation["external_id"] == "cavi-w0"
    assert confirmation["record"] == {"id": "123", "externalId": "cavi-w0"}


def test_update_without_a_target_is_refused_before_any_network_io():
    transport = StubTransport()
    with pytest.raises(ErpWriteError, match="no target_external_id"):
        _client(transport).apply(_op(operation="update"))
    assert transport.calls == []


def test_create_with_a_target_is_refused_as_ambiguous():
    transport = StubTransport()
    with pytest.raises(ErpWriteError, match="create .* refused"):
        _client(transport).apply(_op(operation="create", target_external_id="cavi-w0"))
    assert transport.calls == []


def test_dry_run_fetch_reads_the_update_target():
    transport = StubTransport(_sign_ok(), httpx.Response(200, json={"memo": "old"}))
    record = _client(transport).fetch(_op(operation="update", target_external_id="cavi-w0"))
    assert record == {"memo": "old"}
    _, url, _ = transport.calls[1]
    assert url.endswith("/journalEntry/eid:cavi-w0")


def test_vault_refusal_fails_closed_before_any_erp_call():
    transport = StubTransport(httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(ErpWriteError, match="vault refused"):
        _client(transport).apply(_op())
    assert len(transport.calls) == 1   # never reached NetSuite


def test_vault_unreachable_fails_closed():
    transport = StubTransport(httpx.ConnectError("connection refused"))
    with pytest.raises(ErpWriteError, match="unreachable"):
        _client(transport).apply(_op())


def test_netsuite_rejection_raises_with_detail():
    transport = StubTransport(
        _sign_ok(), httpx.Response(400, json={"title": "Invalid record"})
    )
    with pytest.raises(ErpWriteError, match="HTTP 400.*Invalid record"):
        _client(transport).apply(_op())


def test_missing_configuration_refuses_before_any_network_io():
    transport = StubTransport()
    with pytest.raises(ErpWriteError, match="not configured"):
        _client(transport, netsuite_rest_url="").apply(_op())
    with pytest.raises(ErpWriteError, match="not configured"):
        _client(transport, vault_api_secret="").apply(_op())
    assert transport.calls == []


def test_unsupported_operation_is_refused_not_guessed():
    transport = StubTransport()
    with pytest.raises(ErpWriteError, match="unsupported"):
        _client(transport).apply(_op(operation="void"))
    assert transport.calls == []


def test_auth_header_name_matches_the_vault_service():
    from agents.vault.service import AUTH_HEADER
    assert VAULT_AUTH_HEADER == AUTH_HEADER


# --------------------------------------------------------------------------- #
# fetch() — dry-run reader for the same record the write will touch
# --------------------------------------------------------------------------- #
def test_fetch_returns_the_current_record():
    transport = StubTransport(
        _sign_ok(), httpx.Response(200, json={"memo": "old", "currency": "USD"})
    )
    record = _client(transport).fetch(_op())
    assert record == {"memo": "old", "currency": "USD"}
    method, url, _ = transport.calls[1]
    assert (method, url) == ("GET", RECORD_URL)


def test_fetch_maps_404_to_record_does_not_exist():
    transport = StubTransport(_sign_ok(), httpx.Response(404))
    assert _client(transport).fetch(_op()) is None


def test_fetch_failure_fails_closed_rather_than_guessing():
    transport = StubTransport(_sign_ok(), httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(ErpWriteError, match="HTTP 500"):
        _client(transport).fetch(_op())
