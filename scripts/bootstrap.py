"""Load the file-based JSON Schemas into the Postgres schema_registry table.

Run after `docker compose up` so the durable event_log foreign key has the
contracts it references. Idempotent: re-running upserts.

    python -m scripts.bootstrap
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from shared.db import connection
from shared.settings import get_settings

log = logging.getLogger("cavi.bootstrap")

# matches "<subject>.v<version>.json"
_NAME = re.compile(r"^(?P<subject>.+)\.v(?P<version>\d+)\.json$")


def load_schemas() -> int:
    settings = get_settings()
    schema_dir = Path(settings.schema_registry_dir)
    loaded = 0
    with connection() as conn:
        for path in sorted(schema_dir.glob("*.v*.json")):
            m = _NAME.match(path.name)
            if not m:
                log.warning("skipping unrecognized schema file %s", path.name)
                continue
            subject, version = m["subject"], int(m["version"])
            body = json.loads(path.read_text(encoding="utf-8"))
            conn.execute(
                """
                INSERT INTO schema_registry (subject, version, json_schema)
                VALUES (%s, %s, %s)
                ON CONFLICT (subject, version)
                DO UPDATE SET json_schema = EXCLUDED.json_schema
                """,
                (subject, version, json.dumps(body)),
            )
            loaded += 1
            log.info("registered %s v%s", subject, version)
    return loaded


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    count = load_schemas()
    log.info("bootstrap complete — %d schema(s) registered", count)
