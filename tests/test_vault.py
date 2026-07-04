"""Tests for the Vault credential lifecycle manager.

These use an in-memory credential store, so they never touch the real OS keyring
and need no infrastructure. NetSuite signing is exercised with a fixed nonce +
timestamp to make the OAuth 1.0a output deterministic.
"""
import base64

import pytest

from agents.vault.vault import (
    CredentialNotFound,
    InMemoryCredentialStore,
    TokenExpired,
    UnsupportedPlatform,
    Vault,
)

# Fake-but-shaped NetSuite TBA credentials.
NETSUITE_CREDS = {
    "account_id": "1234567_SB1",
    "consumer_key": "ck_consumer_key_value",
    "consumer_secret": "cs_consumer_secret_value",
    "token_id": "ti_token_id_value",
    "token_secret": "ts_token_secret_value",
}
SECRET_VALUES = (NETSUITE_CREDS["consumer_secret"], NETSUITE_CREDS["token_secret"])


@pytest.fixture
def vault() -> Vault:
    v = Vault(store=InMemoryCredentialStore(), default_ttl=300)
    v.store_credentials("tenant-acme", "netsuite", NETSUITE_CREDS)
    return v


def test_vend_token_is_scoped_and_short_lived(vault: Vault):
    token = vault.vend_token("tenant-acme", "netsuite", now=1_000.0)

    assert token.tenant_id == "tenant-acme"
    assert token.erp_platform == "netsuite"
    assert token.realm == "1234567_SB1"
    assert token.consumer_key == NETSUITE_CREDS["consumer_key"]
    assert token.scopes == ("netsuite:rest:read", "netsuite:rest:write")
    # 5-minute lifetime relative to the injected issue time.
    assert token.issued_at == 1_000.0
    assert token.expires_at == 1_300.0
    assert token.is_expired(now=1_299.0) is False
    assert token.is_expired(now=1_300.0) is True


def test_vend_token_unknown_tenant_raises(vault: Vault):
    with pytest.raises(CredentialNotFound):
        vault.vend_token("tenant-nobody", "netsuite")


def test_unsupported_platform_rejected(vault: Vault):
    with pytest.raises(UnsupportedPlatform):
        vault.vend_token("tenant-acme", "sap")


def test_authorization_header_is_valid_oauth1a(vault: Vault):
    token = vault.vend_token("tenant-acme", "netsuite", now=1_000.0)
    url = "https://1234567-sb1.suitetalk.api.netsuite.com/services/rest/record/v1/customer"

    header = token.authorization_header(
        "GET", url, params={"limit": "10"}, nonce="fixed-nonce", timestamp=1700000000,
        now=1_000.0,
    )

    # Structure: realm + the required OAuth 1.0a TBA fields.
    assert header.startswith("OAuth ")
    assert 'realm="1234567_SB1"' in header
    assert 'oauth_signature_method="HMAC-SHA256"' in header
    assert 'oauth_consumer_key="ck_consumer_key_value"' in header
    assert 'oauth_nonce="fixed-nonce"' in header
    assert 'oauth_version="1.0"' in header

    # Deterministic for fixed nonce+timestamp...
    again = token.authorization_header(
        "GET", url, params={"limit": "10"}, nonce="fixed-nonce", timestamp=1700000000,
        now=1_000.0,
    )
    assert header == again
    # ...and sensitive to the request (changing the URL changes the signature).
    other = token.authorization_header(
        "GET", url + "X", params={"limit": "10"}, nonce="fixed-nonce",
        timestamp=1700000000, now=1_000.0,
    )
    assert other != header

    # The signature is a real HMAC-SHA256 digest: 32 bytes, base64-encoded.
    sig = header.split('oauth_signature="', 1)[1].split('"', 1)[0]
    # header values are percent-encoded; '%2F'->'/', '%2B'->'+', '%3D'->'='
    from urllib.parse import unquote

    assert len(base64.b64decode(unquote(sig))) == 32


def test_expired_token_refuses_to_sign(vault: Vault):
    token = vault.vend_token("tenant-acme", "netsuite", ttl=60, now=1_000.0)
    with pytest.raises(TokenExpired):
        token.authorization_header("GET", "https://example.com", now=2_000.0)


def test_secrets_are_never_exposed_on_the_token(vault: Vault):
    token = vault.vend_token("tenant-acme", "netsuite", now=1_000.0)

    # Not reachable as attributes...
    assert not hasattr(token, "consumer_secret")
    assert not hasattr(token, "token_secret")
    # ...not in repr or the dataclass' public field values.
    blob = repr(token) + repr(vars(token).get("_signer"))
    for secret in SECRET_VALUES:
        assert secret not in blob
        assert secret not in repr(token)


def test_rotate_and_revoke(vault: Vault):
    # Rotate swaps the access token; a freshly vended token reflects the new id.
    vault.rotate_token("tenant-acme", "netsuite", token_id="ti_new", token_secret="ts_new")
    assert vault.vend_token("tenant-acme", "netsuite").token_id == "ti_new"

    # Revoke removes the credentials entirely.
    vault.revoke("tenant-acme", "netsuite")
    with pytest.raises(CredentialNotFound):
        vault.vend_token("tenant-acme", "netsuite")
