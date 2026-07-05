"""Tests for the Vault HTTP service.

Most tests exercise the pure handlers (no socket). One end-to-end test starts a
real stdlib server on an ephemeral port to prove the HTTP wrapper works. All use
an in-memory credential store, so no OS keyring is touched.
"""
import contextlib
import json
import threading
import urllib.error
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


TEST_SECRET = "test-vault-secret"

SIGN_BODY = {
    "tenant_id": "tenant-acme",
    "erp_platform": "netsuite",
    "method": "POST",
    "url": "https://example.suitetalk.api.netsuite.com/record/v1/journalEntry",
}


@contextlib.contextmanager
def _running(handler_cls):
    """Run a handler on an ephemeral port; yields the base URL."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _post(url, body, headers=None):
    """POST JSON; return (status, parsed-body), tolerating 4xx/5xx."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_http_sign_with_valid_secret(service: VaultService):
    handler = make_handler(service, api_secret=TEST_SECRET)
    with _running(handler) as base:
        status, body = _post(f"{base}/sign", SIGN_BODY, {"X-Cavi-Vault-Secret": TEST_SECRET})
    assert status == 200
    assert body["authorization_header"].startswith("OAuth ")


def test_http_sign_without_secret_is_401(service: VaultService):
    handler = make_handler(service, api_secret=TEST_SECRET)
    with _running(handler) as base:
        status, body = _post(f"{base}/sign", SIGN_BODY)  # no auth header
    assert status == 401
    assert body["error"] == "unauthorized"


def test_http_sign_with_wrong_secret_is_401(service: VaultService):
    handler = make_handler(service, api_secret=TEST_SECRET)
    with _running(handler) as base:
        status, _ = _post(f"{base}/sign", SIGN_BODY, {"X-Cavi-Vault-Secret": "nope"})
    assert status == 401


def test_http_unconfigured_secret_fails_closed_503(service: VaultService):
    handler = make_handler(service, api_secret="")  # auth not configured
    with _running(handler) as base:
        status, body = _post(f"{base}/sign", SIGN_BODY, {"X-Cavi-Vault-Secret": "anything"})
    assert status == 503
    assert "not configured" in body["error"]


def test_http_tenant_allowlist_denies_403(service: VaultService):
    handler = make_handler(
        service, api_secret=TEST_SECRET, tenant_allowlist=frozenset({"other-tenant"})
    )
    with _running(handler) as base:
        status, body = _post(f"{base}/sign", SIGN_BODY, {"X-Cavi-Vault-Secret": TEST_SECRET})
    assert status == 403
    assert body["error"] == "tenant not permitted"


def test_http_healthz_is_open(service: VaultService):
    # Liveness must not require auth even when the service is secured.
    handler = make_handler(service, api_secret=TEST_SECRET)
    with _running(handler) as base:
        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as resp:
            assert json.loads(resp.read())["status"] == "ok"
