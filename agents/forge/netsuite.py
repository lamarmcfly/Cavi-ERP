"""Forge — NetSuite ERP client (production `ErpWriter` + `ErpReader`).

This is the component that actually touches the system of record, so both of
its jobs are built around the write-back governance rules:

  * **Secrets never leave Vault.** Every NetSuite request is signed by the
    Vault service: the client POSTs `{tenant_id, erp_platform, method, url}`
    to Vault's ``/sign`` and receives a request-specific OAuth 1.0a
    ``Authorization`` header. This client never sees a consumer or token
    secret. Any Vault refusal (missing credential, expired token, auth
    failure, service down) raises `ErpWriteError` — fail closed, the write
    stays APPROVED and retryable upstream.

  * **Idempotent by construction.** All writes go to NetSuite's external-id
    upsert endpoint — ``PUT /record/v1/{module}/eid:{idempotency_key}`` — so
    the ERP itself dedupes on ``op.idempotency_key``: a retry after a lost
    response converges on the same single record instead of double-posting.
    Only ``create`` and ``update`` are supported (both are the same upsert);
    any other operation is refused rather than guessed at.

  * **Dry-run reads the same record the write will touch.** `fetch` GETs
    ``/record/v1/{module}/eid:{key}``; a 404 means "does not exist yet"
    (a create), anything else non-2xx raises — a proposal never gets a
    fabricated before-state.

The HTTP transport is injectable (mirroring Beacon's `Poster`), so the client
is fully testable with no live Vault or NetSuite.
"""
from __future__ import annotations

import logging
from typing import Callable, Mapping

import httpx

from agents.forge.write import ErpWriteError, WriteOperation
from shared.settings import get_settings

log = logging.getLogger("cavi.forge.netsuite")

#: Header a caller presents to authenticate to the Vault service. Kept in
#: lockstep with agents/vault/service.py::AUTH_HEADER (asserted in tests).
VAULT_AUTH_HEADER = "X-Cavi-Vault-Secret"

#: HTTP transport signature: (method, url, ...) -> httpx.Response.
Requester = Callable[..., httpx.Response]

_TIMEOUT = 30.0


class NetSuiteClient:
    """Vault-signed, idempotency-keyed NetSuite REST client.

    Refuses to operate (raises `ErpWriteError`) when any of its configuration
    is missing — a half-configured deployment must fail loudly before the
    first network call, never post unsigned or to a guessed host.
    """

    def __init__(
        self,
        *,
        vault_url: str,
        vault_api_secret: str,
        netsuite_rest_url: str,
        request: Requester | None = None,
    ) -> None:
        self._vault_url = vault_url.rstrip("/")
        self._vault_secret = vault_api_secret
        self._base = netsuite_rest_url.rstrip("/")
        self._request = request or httpx.request

    @classmethod
    def from_settings(cls, *, request: Requester | None = None) -> "NetSuiteClient":
        s = get_settings()
        return cls(
            vault_url=s.vault_url,
            vault_api_secret=s.vault_api_secret,
            netsuite_rest_url=s.netsuite_rest_url,
            request=request,
        )

    # --- configuration gate --------------------------------------------------
    def _require_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("VAULT_URL", self._vault_url),
                ("CAVI_VAULT_API_SECRET", self._vault_secret),
                ("NETSUITE_REST_URL", self._base),
            )
            if not value
        ]
        if missing:
            raise ErpWriteError(
                f"NetSuite client not configured (missing {', '.join(missing)}); "
                "refusing to touch the ERP"
            )

    # --- Vault signing -------------------------------------------------------
    def _sign(self, op: WriteOperation, method: str, url: str) -> str:
        """Get a request-specific Authorization header from Vault, fail closed."""
        try:
            response = self._request(
                "POST",
                f"{self._vault_url}/sign",
                headers={VAULT_AUTH_HEADER: self._vault_secret},
                json={
                    "tenant_id": op.tenant_id,
                    "erp_platform": op.erp_platform,
                    "method": method,
                    "url": url,
                },
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ErpWriteError(f"vault /sign unreachable: {exc}") from exc
        if response.status_code != 200:
            raise ErpWriteError(
                f"vault refused to sign {method} {url} for tenant {op.tenant_id}: "
                f"HTTP {response.status_code} {_error_of(response)}"
            )
        header = response.json().get("authorization_header")
        if not header:
            raise ErpWriteError("vault /sign returned no authorization_header")
        return header

    # --- record addressing ---------------------------------------------------
    def _record_url(self, op: WriteOperation) -> str:
        return f"{self._base}/record/v1/{op.target_module}/eid:{op.idempotency_key}"

    # --- ErpWriter -----------------------------------------------------------
    def apply(self, op: WriteOperation) -> dict:
        self._require_configured()
        if op.operation not in ("create", "update"):
            raise ErpWriteError(
                f"unsupported NetSuite operation {op.operation!r}; "
                "only create/update (external-id upsert) are implemented"
            )
        url = self._record_url(op)
        authorization = self._sign(op, "PUT", url)
        try:
            response = self._request(
                "PUT",
                url,
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                },
                json=dict(op.payload),
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ErpWriteError(f"netsuite upsert failed in transit: {exc}") from exc
        if not (200 <= response.status_code < 300):
            raise ErpWriteError(
                f"netsuite rejected {op.operation} {op.target_module} "
                f"eid:{op.idempotency_key}: HTTP {response.status_code} "
                f"{_error_of(response)}"
            )
        log.info(
            "netsuite upsert ok: %s eid:%s HTTP %d",
            op.target_module, op.idempotency_key, response.status_code,
        )
        confirmation: dict = {
            "external_id": op.idempotency_key,
            "status_code": response.status_code,
        }
        location = response.headers.get("Location")
        if location:
            confirmation["location"] = location
        body = _json_of(response)
        if body is not None:
            confirmation["record"] = body
        return confirmation

    # --- ErpReader -----------------------------------------------------------
    def fetch(self, op: WriteOperation) -> Mapping | None:
        self._require_configured()
        url = self._record_url(op)
        authorization = self._sign(op, "GET", url)
        try:
            response = self._request(
                "GET", url, headers={"Authorization": authorization}, timeout=_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise ErpWriteError(f"netsuite fetch failed in transit: {exc}") from exc
        if response.status_code == 404:
            return None  # does not exist yet — this write would create it
        if not (200 <= response.status_code < 300):
            raise ErpWriteError(
                f"netsuite fetch of {op.target_module} eid:{op.idempotency_key} "
                f"failed: HTTP {response.status_code} {_error_of(response)}"
            )
        body = _json_of(response)
        if body is None:
            raise ErpWriteError(
                f"netsuite fetch of {op.target_module} eid:{op.idempotency_key} "
                "returned a non-JSON body"
            )
        return body


def _json_of(response: httpx.Response) -> dict | None:
    """The response body as a dict, or None when absent/non-JSON/non-object."""
    try:
        body = response.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _error_of(response: httpx.Response) -> str:
    """Short human-readable error detail from a response body."""
    body = _json_of(response)
    if body:
        return str(body.get("error") or body.get("title") or body)[:200]
    return (response.text or "")[:200]
