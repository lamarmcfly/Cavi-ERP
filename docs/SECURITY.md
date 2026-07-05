# Security model

## Credentials never leave Vault
Vault is the only component that holds raw ERP secrets. Callers ask it to **vend**
a scoped token or **sign** a request; the `consumer_secret`/`token_secret` are
captured in an in-process signing closure and are never returned, logged, or
exposed on the token object (`repr`, `vars()`, and attribute access reveal
nothing). Consumers get a signed `Authorization` header, not the keys.

## The Vault HTTP surface is authenticated, fail-closed
`/vend` and `/sign` require the `X-Cavi-Vault-Secret` header, compared in constant
time (`shared/auth.py`).

- **Fail closed:** if `CAVI_VAULT_API_SECRET` is unset the service returns `503`
  and refuses to sign — it never serves credentials unauthenticated.
- `401` on a missing/invalid secret.
- Optional `CAVI_VAULT_TENANT_ALLOWLIST` (default-deny) restricts which tenants
  the service will sign for (`403` otherwise).
- `/healthz` is intentionally open for liveness probes.

Set a **strong** `CAVI_VAULT_API_SECRET` in every non-local environment (the
compose/k8s defaults are dev-only placeholders).

## Inbound webhook verification
`shared/auth.py` provides HMAC-SHA256 helpers (`compute_signature`/
`verify_signature`) to verify inbound webhooks relayed through n8n. Both the
shared-secret and HMAC checks fail closed (unconfigured ⇒ reject).

## Tenant isolation
`tenant_id` rides on the event envelope and is enforced where it matters — the
books: `journal_entry`/`journal_line` are `NOT NULL` on `tenant_id`, the ledger
store's reads are tenant-scoped (one tenant can't read another's entries), and a
ledger entry without a tenant is rejected, not commingled. `event_log` carries
`tenant_id` for tenant-scoped audit queries.

## Secrets handling
- `.env` is git-ignored; only `.env.example` (placeholders) is committed.
- Compose/k8s pass secrets via env/Secret objects, never baked into the image.
- The runtime image runs as a **non-root** user with only runtime deps + code.
- Never log secrets; the credential surface is server-only.

## Known gaps / follow-ups
- Fine-grained *per-caller → tenant* binding needs a real identity layer (mTLS or
  signed claims); today the shared secret authenticates the caller as
  trusted-internal and the allowlist scopes tenants.
- A hashed dependency lockfile (supply-chain pinning) is not yet generated.
- No SoC 2 controls / secret rotation automation yet.
