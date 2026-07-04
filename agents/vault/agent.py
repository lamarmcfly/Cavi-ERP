"""Vault — custodian of sensitive master data and secrets.

The event runtime is a thin shell around `vault.Vault`: it receives credential
requests over the bus and vends scoped tokens. Note that the live token (and its
signing capability) is used **in-process** by the requester via the `Vault`
library — only non-secret acknowledgements travel over the bus, so secrets never
leave this agent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from agents.base import BaseAgent, Event
from agents.vault.vault import CredentialNotFound, Vault, VaultError, VendedToken

log = logging.getLogger("cavi.vault")


def _iso(epoch: float) -> str:
    """Render an epoch-seconds instant as an ISO-8601 UTC string.

    Vended tokens track time as float epoch seconds (see VendedToken); the
    canonical vault.secret.* contracts require ISO-8601 timestamps, so the agent
    converts at the emit boundary.
    """
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def granted_payload(token: VendedToken) -> dict:
    """Build the canonical ``vault.secret.granted.v1`` payload from a vended token.

    Non-secret identifiers only — the raw NetSuite secrets live in the token's
    signing closure and are never surfaced here. ``token_id`` is the (non-secret)
    TBA token id already exposed on VendedToken.
    """
    return {
        "tenant_id": token.tenant_id,
        "erp_platform": token.erp_platform,
        "token_id": token.token_id,
        "scopes": list(token.scopes),
        "expires_at": _iso(token.expires_at),
        "issued_at": _iso(token.issued_at),
    }


def denied_payload(
    tenant_id: str, erp_platform: str, reason: str, *, requested_at: str | None = None
) -> dict:
    """Build the canonical ``vault.secret.denied.v1`` payload.

    The inbound request event carries no timestamp, so ``requested_at`` is stamped
    at denial time unless the caller supplies one (kept injectable for tests).
    """
    return {
        "tenant_id": tenant_id,
        "erp_platform": erp_platform,
        "reason": reason,
        "requested_at": requested_at or datetime.now(timezone.utc).isoformat(),
    }


class VaultAgent(BaseAgent):
    name = "vault"

    def __init__(self, vault: Vault | None = None) -> None:
        super().__init__()
        self.vault = vault or Vault()

    @property
    def subjects(self) -> list[str]:
        return ["vault.secret.request"]

    def vend_token(self, tenant_id: str, erp_platform: str):
        """Convenience passthrough so callers go through the agent or the lib."""
        return self.vault.vend_token(tenant_id, erp_platform)

    def handle(self, event: Event) -> None:
        tenant_id = event.payload["tenant_id"]
        platform = event.payload["erp_platform"]
        try:
            token = self.vault.vend_token(tenant_id, platform)
        except (CredentialNotFound, VaultError) as exc:
            log.warning("vault denied %s/%s: %s", tenant_id, platform, exc)
            self.emit(
                Event(
                    subject="vault.secret.denied",
                    schema_version=1,
                    source=self.name,
                    correlation_id=event.correlation_id,
                    payload=denied_payload(tenant_id, platform, str(exc)),
                )
            )
            return
        # Acknowledge with non-secret metadata only — never the key material.
        log.info("vault vended token for %s/%s (scopes=%s)",
                 tenant_id, platform, token.scopes)
        self.emit(
            Event(
                subject="vault.secret.granted",
                schema_version=1,
                source=self.name,
                correlation_id=event.correlation_id,
                payload=granted_payload(token),
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    VaultAgent().run()
