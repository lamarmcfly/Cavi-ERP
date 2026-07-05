"""Vault HTTP service — the only network surface that touches credentials.

Exposes two endpoints so other components (n8n workflows, agents) can use ERP
credentials **without ever reading them**:

  * ``POST /vend``  -> non-secret capability metadata (scopes, expiry, realm,
                       consumer/token *ids*). Confirms a credential exists and is
                       usable. Never returns the consumer/token *secrets*.
  * ``POST /sign``  -> a request-specific OAuth 1.0a ``Authorization`` header.
                       Vault signs in-process; the secrets stay here.
  * ``GET  /healthz`` -> liveness probe.

The request handlers are pure functions on ``VaultService`` (returning
``(status, dict)``) so they unit-test without opening a socket. A thin stdlib
``http.server`` wraps them — fine for an internal sidecar; swap for
FastAPI/uvicorn if you need ASGI features.
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agents.vault.vault import (
    CredentialNotFound,
    InMemoryCredentialStore,
    TokenExpired,
    UnsupportedPlatform,
    Vault,
    VaultError,
)

log = logging.getLogger("cavi.vault.service")


class VaultService:
    """Pure request logic over a Vault instance. No HTTP here — testable directly."""

    def __init__(self, vault: Vault) -> None:
        self._vault = vault

    def vend(self, body: dict) -> tuple[int, dict[str, Any]]:
        tenant_id = body.get("tenant_id")
        erp_platform = body.get("erp_platform")
        if not (isinstance(tenant_id, str) and tenant_id
                and isinstance(erp_platform, str) and erp_platform):
            return 400, {"error": "tenant_id and erp_platform are required"}
        try:
            token = self._vault.vend_token(tenant_id, erp_platform)
        except CredentialNotFound as exc:
            return 404, {"error": str(exc)}
        except UnsupportedPlatform as exc:
            return 400, {"error": str(exc)}
        except VaultError as exc:  # pragma: no cover - defensive
            return 500, {"error": str(exc)}
        # Non-secret surface only — mirrors VendedToken's public attributes.
        return 200, {
            "tenant_id": token.tenant_id,
            "erp_platform": token.erp_platform,
            "realm": token.realm,
            "consumer_key": token.consumer_key,
            "token_id": token.token_id,
            "scopes": list(token.scopes),
            "signature_method": token.signature_method,
            "expires_at": token.expires_at,
        }

    def sign(self, body: dict) -> tuple[int, dict[str, Any]]:
        tenant_id = body.get("tenant_id")
        erp_platform = body.get("erp_platform")
        method = body.get("method")
        url = body.get("url")
        params = body.get("params")  # optional query params dict
        if not (isinstance(tenant_id, str) and tenant_id
                and isinstance(erp_platform, str) and erp_platform
                and isinstance(method, str) and method
                and isinstance(url, str) and url):
            return 400, {"error": "tenant_id, erp_platform, method, url are required"}
        try:
            token = self._vault.vend_token(tenant_id, erp_platform)
            header = token.authorization_header(method, url, params)
        except CredentialNotFound as exc:
            return 404, {"error": str(exc)}
        except (UnsupportedPlatform, TokenExpired) as exc:
            return 400, {"error": str(exc)}
        except VaultError as exc:  # pragma: no cover - defensive
            return 500, {"error": str(exc)}
        return 200, {"authorization_header": header, "expires_at": token.expires_at}


def make_handler(service: VaultService) -> type[BaseHTTPRequestHandler]:
    class VaultHandler(BaseHTTPRequestHandler):
        server_version = "CaviVault/1.0"

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid JSON body"})
                return
            if self.path == "/vend":
                status, payload = service.vend(body)
            elif self.path == "/sign":
                status, payload = service.sign(body)
            else:
                status, payload = 404, {"error": "not found"}
            self._send(status, payload)

        def log_message(self, *args) -> None:  # keep stdout clean; we log ourselves
            return

    return VaultHandler


def serve(service: VaultService, host: str | None = None, port: int | None = None) -> None:
    host = host or os.environ.get("CAVI_VAULT_HOST", "0.0.0.0")
    port = port or int(os.environ.get("CAVI_VAULT_PORT", "8080"))
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    log.info("Vault service listening on %s:%d", host, port)
    httpd.serve_forever()


def _seed_from_env(vault: Vault) -> None:
    """Seed one tenant's NetSuite credentials from environment variables.

    Used by the containerized service, where there is no OS keyring. Reads
    CAVI_VAULT_SEED_TENANT + NETSUITE_* and stores them in the in-memory store.
    """
    tenant_id = os.environ.get("CAVI_VAULT_SEED_TENANT")
    account_id = os.environ.get("NETSUITE_ACCOUNT_ID")
    if not (tenant_id and account_id):
        return
    vault.store_credentials(
        tenant_id,
        "netsuite",
        {
            "account_id": account_id,
            "consumer_key": os.environ.get("NETSUITE_CONSUMER_KEY", ""),
            "consumer_secret": os.environ.get("NETSUITE_CONSUMER_SECRET", ""),
            "token_id": os.environ.get("NETSUITE_TOKEN_ID", ""),
            "token_secret": os.environ.get("NETSUITE_TOKEN_SECRET", ""),
        },
    )
    log.info("seeded netsuite credentials for tenant %s", tenant_id)


def build_vault_from_env() -> Vault:
    """Construct a Vault whose store is chosen by CAVI_VAULT_STORE.

    * ``keyring`` (default) — OS keyring; for host/desktop use.
    * ``memory``            — in-memory store seeded from env; for containers and
                              CI, where no OS keyring exists. Secrets never hit disk.
    """
    if os.environ.get("CAVI_VAULT_STORE", "keyring").lower() == "memory":
        vault = Vault(store=InMemoryCredentialStore())
        _seed_from_env(vault)
        return vault
    return Vault()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve(VaultService(build_vault_from_env()))
