"""Vault — credential lifecycle manager.

Vault is the *only* component in Cavi ERP that holds raw ERP credentials. Every
other agent obtains access by asking Vault to **vend** a short-lived, scoped
token; no agent ever reads a secret directly.

Storage model
-------------
Per-tenant credentials live in the OS keyring (Windows Credential Locker, macOS
Keychain, Secret Service on Linux) via `keyring`. `python-dotenv` is used only
to *seed* the keyring from a tenant's `.env` during onboarding — secrets never
stay on disk in the running system.

Vending model (NetSuite OAuth 1.0a TBA)
---------------------------------------
NetSuite Token-Based Auth secrets are long-lived, so Vault does not hand them
out. Instead `vend_token()` returns a `VendedToken` — a time-boxed capability
that can sign requests for a few minutes and then must be re-vended. The raw
`consumer_secret`/`token_secret` are captured inside a signing closure and are
never exposed as attributes of the token (see `VendedToken`).

    vault.store_credentials(tenant, "netsuite", {...})   # onboarding
    token = vault.vend_token(tenant, "netsuite")          # per-use
    header = token.authorization_header("GET", url)       # signs in-process
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol
from urllib.parse import quote

DEFAULT_TTL_SECONDS = 300  # vended tokens live 5 minutes, then must be re-vended

# Per-platform required credential fields and default vended scopes.
_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "netsuite": frozenset(
        {"account_id", "consumer_key", "consumer_secret", "token_id", "token_secret"}
    ),
}
_DEFAULT_SCOPES: dict[str, tuple[str, ...]] = {
    "netsuite": ("netsuite:rest:read", "netsuite:rest:write"),
}
_PLATFORM_ALIASES = {"netsuite": "netsuite", "ns": "netsuite", "netsuite_tba": "netsuite"}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class VaultError(Exception):
    """Base class for Vault failures."""


class UnsupportedPlatform(VaultError):
    pass


class CredentialNotFound(VaultError):
    pass


class TokenExpired(VaultError):
    pass


# --------------------------------------------------------------------------- #
# Credential storage backends
# --------------------------------------------------------------------------- #
class CredentialStore(Protocol):
    """Pluggable secret store. The default is keyring-backed; tests inject an
    in-memory implementation so they never touch the real OS keyring."""

    def get(self, service: str, tenant_id: str) -> dict | None: ...
    def put(self, service: str, tenant_id: str, secret: Mapping) -> None: ...
    def delete(self, service: str, tenant_id: str) -> None: ...


class InMemoryCredentialStore:
    """Volatile store for tests and local experiments. Never use in production."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict] = {}

    def get(self, service: str, tenant_id: str) -> dict | None:
        value = self._data.get((service, tenant_id))
        return dict(value) if value is not None else None

    def put(self, service: str, tenant_id: str, secret: Mapping) -> None:
        self._data[(service, tenant_id)] = dict(secret)

    def delete(self, service: str, tenant_id: str) -> None:
        self._data.pop((service, tenant_id), None)


class KeyringCredentialStore:
    """Production store: persists the per-tenant credential blob in the OS
    keyring. `keyring` is imported lazily so the module loads even where the
    package isn't installed (e.g. test-only environments)."""

    def __init__(self, namespace: str = "cavi-vault") -> None:
        self._namespace = namespace

    def _service(self, service: str) -> str:
        return f"{self._namespace}:{service}"

    def get(self, service: str, tenant_id: str) -> dict | None:
        import keyring

        raw = keyring.get_password(self._service(service), tenant_id)
        return json.loads(raw) if raw else None

    def put(self, service: str, tenant_id: str, secret: Mapping) -> None:
        import keyring

        keyring.set_password(self._service(service), tenant_id, json.dumps(dict(secret)))

    def delete(self, service: str, tenant_id: str) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(self._service(service), tenant_id)
        except PasswordDeleteError:
            pass


# --------------------------------------------------------------------------- #
# OAuth 1.0a TBA signing (NetSuite)
# --------------------------------------------------------------------------- #
def _percent(value: object) -> str:
    """RFC 5849 percent-encoding: encode everything except unreserved chars."""
    return quote(str(value), safe="-._~")


def _oauth1a_authorization_header(
    creds: Mapping[str, str],
    http_method: str,
    url: str,
    query_params: Mapping[str, str] | None,
    *,
    nonce: str,
    timestamp: str,
) -> str:
    """Build a NetSuite OAuth 1.0a TBA `Authorization` header (HMAC-SHA256).

    Pure function: given the same inputs (including nonce + timestamp) it always
    produces the same header, which is what makes it testable.
    """
    oauth_params = {
        "oauth_consumer_key": creds["consumer_key"],
        "oauth_token": creds["token_id"],
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": timestamp,
        "oauth_nonce": nonce,
        "oauth_version": "1.0",
    }

    # Signature base string: METHOD & encode(url) & encode(sorted params).
    # realm and oauth_signature are excluded from the base string.
    signed_params = {**(query_params or {}), **oauth_params}
    normalized = "&".join(
        f"{_percent(k)}={_percent(v)}" for k, v in sorted(signed_params.items())
    )
    base_string = "&".join([http_method.upper(), _percent(url), _percent(normalized)])

    signing_key = f"{_percent(creds['consumer_secret'])}&{_percent(creds['token_secret'])}"
    digest = hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()

    # Authorization header: realm first, then oauth params + signature.
    header_params = {**oauth_params, "oauth_signature": signature}
    parts = [f'realm="{_percent(str(creds["account_id"]).upper())}"']
    parts += [f'{_percent(k)}="{_percent(v)}"' for k, v in sorted(header_params.items())]
    return "OAuth " + ", ".join(parts)


# --------------------------------------------------------------------------- #
# Vended token
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VendedToken:
    """A short-lived, scoped capability handed to a requesting agent.

    Carries only *non-secret* identifiers as attributes. The raw NetSuite
    secrets live inside `_signer` (a closure created by Vault) and are never
    exposed — `repr`, `vars()`, and attribute access reveal no key material.
    """

    tenant_id: str
    erp_platform: str
    realm: str
    consumer_key: str
    token_id: str
    scopes: tuple[str, ...]
    issued_at: float
    expires_at: float
    signature_method: str = "HMAC-SHA256"
    oauth_version: str = "1.0"
    # Closure over the secrets; excluded from repr and equality.
    _signer: Callable[..., str] | None = field(default=None, repr=False, compare=False)

    def is_expired(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return now >= self.expires_at

    def authorization_header(
        self,
        http_method: str,
        url: str,
        params: Mapping[str, str] | None = None,
        *,
        nonce: str | None = None,
        timestamp: int | None = None,
        now: float | None = None,
    ) -> str:
        """Sign a request, producing the `Authorization` header value.

        Raises `TokenExpired` once the vended capability's TTL has passed — the
        caller must re-vend, even though the underlying NetSuite token is static.
        """
        if self.is_expired(now):
            raise TokenExpired(
                f"vended token for {self.tenant_id}/{self.erp_platform} expired"
            )
        if self._signer is None:  # pragma: no cover - defensive
            raise VaultError("token has no signer bound")
        nonce = nonce or secrets.token_hex(16)
        ts = str(timestamp if timestamp is not None else int(time.time()))
        return self._signer(http_method, url, params, nonce, ts)


# --------------------------------------------------------------------------- #
# Vault manager
# --------------------------------------------------------------------------- #
class Vault:
    """Credential lifecycle manager: store, vend, rotate, revoke."""

    def __init__(
        self,
        store: CredentialStore | None = None,
        *,
        default_ttl: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._store: CredentialStore = store or KeyringCredentialStore()
        self._default_ttl = default_ttl

    @staticmethod
    def _normalize_platform(erp_platform: str) -> str:
        key = _PLATFORM_ALIASES.get(erp_platform.strip().lower())
        if key is None:
            raise UnsupportedPlatform(f"unsupported ERP platform: {erp_platform!r}")
        return key

    # --- lifecycle: store ---------------------------------------------------
    def store_credentials(
        self, tenant_id: str, erp_platform: str, credentials: Mapping[str, str]
    ) -> None:
        platform = self._normalize_platform(erp_platform)
        missing = _REQUIRED_FIELDS[platform] - set(credentials)
        if missing:
            raise ValueError(
                f"{platform} credentials missing fields: {sorted(missing)}"
            )
        self._store.put(platform, tenant_id, dict(credentials))

    def load_from_dotenv(
        self, tenant_id: str, erp_platform: str, dotenv_path: str
    ) -> None:
        """Seed a tenant's credentials from a `.env` file into the keyring.

        Expects keys like NETSUITE_ACCOUNT_ID, NETSUITE_CONSUMER_KEY, ...
        The prefix is the platform name; values are mapped to lowercase fields.
        """
        from dotenv import dotenv_values

        platform = self._normalize_platform(erp_platform)
        prefix = f"{platform.upper()}_"
        raw = dotenv_values(dotenv_path)
        creds = {
            k[len(prefix):].lower(): v
            for k, v in raw.items()
            if k.startswith(prefix) and v is not None
        }
        self.store_credentials(tenant_id, platform, creds)

    # --- lifecycle: vend ----------------------------------------------------
    def vend_token(
        self,
        tenant_id: str,
        erp_platform: str,
        *,
        scopes: tuple[str, ...] | None = None,
        ttl: int | None = None,
        now: float | None = None,
    ) -> VendedToken:
        """Return a short-lived scoped token for the tenant on the platform.

        The raw secrets are read once, bound into a signing closure, and never
        returned to the caller.
        """
        platform = self._normalize_platform(erp_platform)
        creds = self._store.get(platform, tenant_id)
        if creds is None:
            raise CredentialNotFound(
                f"no {platform} credentials stored for tenant {tenant_id!r}"
            )

        issued_at = time.time() if now is None else now
        lifetime = self._default_ttl if ttl is None else ttl
        granted_scopes = scopes if scopes is not None else _DEFAULT_SCOPES[platform]

        def signer(
            http_method: str,
            url: str,
            params: Mapping[str, str] | None,
            nonce: str,
            timestamp: str,
        ) -> str:
            return _oauth1a_authorization_header(
                creds, http_method, url, params, nonce=nonce, timestamp=timestamp
            )

        return VendedToken(
            tenant_id=tenant_id,
            erp_platform=platform,
            realm=str(creds["account_id"]),
            consumer_key=creds["consumer_key"],
            token_id=creds["token_id"],
            scopes=tuple(granted_scopes),
            issued_at=issued_at,
            expires_at=issued_at + lifetime,
            _signer=signer,
        )

    # --- lifecycle: rotate / revoke ----------------------------------------
    def rotate_token(
        self, tenant_id: str, erp_platform: str, token_id: str, token_secret: str
    ) -> None:
        """Replace the TBA access token (e.g. after a NetSuite token reissue)."""
        platform = self._normalize_platform(erp_platform)
        creds = self._store.get(platform, tenant_id)
        if creds is None:
            raise CredentialNotFound(
                f"no {platform} credentials stored for tenant {tenant_id!r}"
            )
        creds.update(token_id=token_id, token_secret=token_secret)
        self._store.put(platform, tenant_id, creds)

    def revoke(self, tenant_id: str, erp_platform: str) -> None:
        platform = self._normalize_platform(erp_platform)
        self._store.delete(platform, tenant_id)
