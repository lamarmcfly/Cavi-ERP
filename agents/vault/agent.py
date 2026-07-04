"""Vault — custodian of sensitive master data and secrets.

The event runtime is a thin shell around `vault.Vault`: it receives credential
requests over the bus and vends scoped tokens. Note that the live token (and its
signing capability) is used **in-process** by the requester via the `Vault`
library — only non-secret acknowledgements travel over the bus, so secrets never
leave this agent.
"""
from __future__ import annotations

import logging

from agents.base import BaseAgent, Event
from agents.vault.vault import CredentialNotFound, Vault, VaultError

log = logging.getLogger("cavi.vault")


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
                    payload={"tenant_id": tenant_id, "erp_platform": platform,
                             "reason": str(exc)},
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
                payload={
                    "tenant_id": tenant_id,
                    "erp_platform": platform,
                    "scopes": list(token.scopes),
                    "expires_at": token.expires_at,
                },
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    VaultAgent().run()
