# Cavi ERP — image for the Python agents and the Vault HTTP service.
# psycopg[binary] bundles libpq, so no apt build deps are required.
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

EXPOSE 8080

# Default to the Vault service; override `command:` in compose to run an agent,
# e.g. ["python", "-m", "agents.ledger.agent"].
CMD ["python", "-m", "agents.vault.service"]
