"""Hash-chained audit log — tamper-evidence for the event record (W7 / FR7).

``event_log`` is already durable and append-only, which makes it *auditable*;
this module makes it **tamper-evident**: every recorded event carries a
``chain_hash`` computed over the previous event's hash plus its own canonical
content, so editing, deleting, or reordering any historical row breaks every
hash after it. Verification recomputes the chain from genesis and reports the
first divergence — an exported log can be checked by anyone holding it,
without trusting the database it came from.

Scope: the chain covers events recorded after migration 0004 lands (pre-chain
rows keep ``chain_hash NULL`` — the chain era starts at genesis then, rather
than pretending to notarize history that predates it). One global chain,
ordered by the append sequence: cross-tenant ordering is part of what is being
notarized.

Pure functions only — the stores in ``shared/events.py`` call `chain_hash` at
write time, and `scripts/audit_export.py` uses `verify_chain` on an export.
"""
from __future__ import annotations

import hashlib
import json
from typing import Mapping, Sequence

#: The chain's starting point. Not a hash of anything — a fixed, public anchor.
GENESIS_HASH = "sha256:" + "0" * 64


def canonical_event(event: Mapping) -> str:
    """Deterministic serialization of an event envelope (sorted keys, no
    whitespace, `default=str` for total coverage) so logically-equal events
    fingerprint identically regardless of dict ordering."""
    return json.dumps(dict(event), sort_keys=True, separators=(",", ":"), default=str)


def chain_hash(prev_hash: str, event: Mapping) -> str:
    """Hash of this event chained onto its predecessor.

    ``H(n) = sha256(H(n-1) + "\\n" + canonical(event_n))`` — so a change to any
    historical event (or its position) changes every subsequent hash.
    """
    material = prev_hash + "\n" + canonical_event(event)
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def verify_chain(
    entries: Sequence[tuple[Mapping, str]],
    *,
    genesis: str = GENESIS_HASH,
) -> list[str]:
    """Recompute the chain over ``(event, recorded_chain_hash)`` pairs in log
    order and report every divergence, human-readably. An empty list means the
    log is intact; any entry means the log was altered at (or before) the
    named position."""
    problems: list[str] = []
    prev = genesis
    for position, (event, recorded) in enumerate(entries):
        expected = chain_hash(prev, event)
        if recorded != expected:
            problems.append(
                f"chain break at position {position} "
                f"(event {event.get('id', '?')}): recorded {recorded!r}, "
                f"expected {expected!r}"
            )
        # Continue from the *recorded* hash so one break is reported once, not
        # cascaded into a report for every subsequent row.
        prev = recorded
    return problems
