"""Tests for the shared auth helpers — constant-time secret + HMAC signatures.

Both mechanisms must fail closed (unconfigured/missing → reject) and accept only
exact matches.
"""
from __future__ import annotations

from shared.auth import (
    compute_signature,
    constant_time_compare,
    verify_shared_secret,
    verify_signature,
)


def test_constant_time_compare():
    assert constant_time_compare("abc", "abc") is True
    assert constant_time_compare("abc", "abd") is False
    assert constant_time_compare("abc", "ab") is False


def test_verify_shared_secret_accepts_exact_match():
    assert verify_shared_secret("s3cret", "s3cret") is True


def test_verify_shared_secret_fails_closed():
    assert verify_shared_secret("s3cret", "") is False       # unconfigured
    assert verify_shared_secret(None, "s3cret") is False     # missing header
    assert verify_shared_secret("", "s3cret") is False       # empty header
    assert verify_shared_secret("wrong", "s3cret") is False  # mismatch


def test_hmac_signature_roundtrip():
    body = b'{"tenant_id":"t1","event":"x"}'
    sig = compute_signature(body, "signing-secret")
    assert verify_signature(body, sig, "signing-secret") is True


def test_hmac_signature_rejects_tampered_body_or_secret():
    body = b'{"amount":100}'
    sig = compute_signature(body, "signing-secret")
    assert verify_signature(b'{"amount":9999}', sig, "signing-secret") is False  # tampered body
    assert verify_signature(body, sig, "other-secret") is False                  # wrong secret


def test_hmac_signature_fails_closed():
    body = b"{}"
    assert verify_signature(body, "anything", "") is False       # unconfigured
    assert verify_signature(body, None, "signing-secret") is False  # missing signature
