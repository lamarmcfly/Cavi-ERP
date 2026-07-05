# Cavi ERP — production image for the Python agents + the Vault HTTP service.
# Multi-stage: deps are built into a venv, then copied into a slim runtime that
# runs as a non-root user with only the runtime deps + app code (no tests, no
# lint/type tooling). psycopg[binary] bundles libpq, so no apt build deps needed.

# --- build stage: install runtime deps into an isolated venv ---
FROM python:3.11-slim AS build
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- runtime stage: slim, non-root ---
FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app

# Non-root runtime user.
RUN groupadd --system cavi && useradd --system --gid cavi --home-dir /app cavi

# Dependencies from the build stage.
COPY --from=build /opt/venv /opt/venv

# Only the code the runtime needs (tests/dev files are left out entirely).
COPY --chown=cavi:cavi agents ./agents
COPY --chown=cavi:cavi shared ./shared
COPY --chown=cavi:cavi scripts ./scripts
COPY --chown=cavi:cavi schema_registry ./schema_registry

USER cavi
EXPOSE 8080

# Default to the Vault service; compose/k8s override the command to run an agent,
# e.g. ["python", "-m", "agents.ledger.agent"]. Health/liveness is defined per
# service (compose healthcheck / k8s probe) since the port varies by process.
CMD ["python", "-m", "agents.vault.service"]
