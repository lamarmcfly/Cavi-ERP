"""Tests for the Vault HTTP service.

Most tests exercise the pure handlers (no socket). One end-to-end test starts a
real stdlib server on an ephemeral port to prove the HTTP wrapper works. All use
an in-memory credential store, so no OS keyring is touched.
"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agents.vault.service import VaultService, make_handler
from agents.vault.vault import InMemoryCredentialStore, Vault

NETSUITE_CREDS = {
    "account_id": "1234567_SB1",
    "consumer_key": "ck_value",
    "consumer_secret": "cs_SECRET_value",
    "token_id": "ti_value",
    "token_secret": "ts_SECRET_value",
}
SECRETS = (NETSUITE_CREDS["consumer_secret"], NETSUITE_CREDS["token_secret"])


@pytest.fixture
def service() -> VaultService:
    vault = Vault(store=InMemoryCredentialStore())
    vault.store_credentials("tenant-acme", "netsuite", NETSUITE_CREDS)
    return VaultService(vault)


def test_vend_returns_metadata_and_no_secrets(service: VaultService):
    status, body = service.vend({"tenant_id": "tenant-acme", "erp_platform": "netsuite"})
    assert status == 200
    assert body["realm"] == "1234567_SB1"
    assert body["scopes"] == ["netsuite:rest:read", "netsuite:rest:write"]
    assert "expires_at" in body
    blob = json.dumps(body)
    for secret in SECRETS:
        assert secret not in blob  # secrets never cross the wire


def test_vend_missing_fields_is_400(service: VaultService):
    status, body = service.vend({"tenant_id": "tenant-acme"})
    assert status == 400
    assert "error" in body


def test_vend_unknown_tenant_is_404(service: VaultService):
    status, _ = service.vend({"tenant_id": "ghost", "erp_platform": "netsuite"})
    assert status == 404


def test_vend_unsupported_platform_is_400(service: VaultService):
    status, _ = service.vend({"tenant_id": "tenant-acme", "erp_platform": "sap"})
    assert status == 400


def test_sign_returns_authorization_header_and_no_secrets(service: VaultService):
    status, body = service.sign({
        "tenant_id": "tenant-acme",
        "erp_platform": "netsuite",
        "method": "POST",
        "url": "https://example.suitetalk.api.netsuite.com/record/v1/journalEntry",
    })
    assert status == 200
    assert body["authorization_header"].startswith("OAuth ")
    assert 'oauth_signature_method="HMAC-SHA256"' in body["authorization_header"]
    for secret in SECRETS:
        assert secret not in json.dumps(body)


def test_sign_missing_fields_is_400(service: VaultService):
    status, _ = service.sign({"tenant_id": "tenant-acme", "erp_platform": "netsuite"})
    assert status == 400


def test_http_roundtrip_sign(service: VaultService):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps({
            "tenant_id": "tenant-acme",
            "erp_platform": "netsuite",
            "method": "POST",
            "url": "https://example.suitetalk.api.netsuite.com/record/v1/journalEntry",
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/sign",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
        assert body["authorization_header"].startswith("OAuth ")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_http_healthz(service: VaultService):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            assert json.loads(resp.read())["status"] == "ok"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
