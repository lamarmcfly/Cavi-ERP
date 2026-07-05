"""Authentication helpers for Cavi ERP network surfaces.

Two constant-time mechanisms:

  * **Shared secret (bearer).** A caller proves it is a trusted internal
    component by presenting a pre-shared secret header. Used by the Vault HTTP
    service to gate `/vend` and `/sign` — the endpoints that use credentials.
  * **HMAC signature.** A caller proves it holds the signing secret *and* that
    the body is intact by sending `X-Signature = hex(HMAC-SHA256(body, secret))`.
    Used to verify inbound webhooks relayed through n8n.

Every comparison uses `hmac.compare_digest` to avoid timing side channels, and
every check **fails closed**: an unconfigured secret rejects all callers rather
than silently allowing them.
"""
from __future__ import annotations

import hashlib
import hmac


def constant_time_compare(a: str, b: str) -> bool:
    """Timing-safe string equality."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_shared_secret(provided: str | None, expected: str) -> bool:
    """True iff ``provided`` matches the configured ``expected`` secret.

    Fails closed: an empty ``expected`` (auth not configured) or a missing
    ``provided`` header always returns False.
    """
    if not expected or not provided:
        return False
    return constant_time_compare(provided, expected)


def compute_signature(body: bytes, secret: str) -> str:
    """Hex HMAC-SHA256 of ``body`` under ``secret`` — the value senders put in
    the signature header."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, provided_sig: str | None, secret: str) -> bool:
    """True iff ``provided_sig`` is a valid HMAC-SHA256 of ``body`` under
    ``secret``. Fails closed on a missing secret or signature."""
    if not secret or not provided_sig:
        return False
    return hmac.compare_digest(provided_sig, compute_signature(body, secret))
