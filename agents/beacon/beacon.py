"""Beacon — notifications & alerting (domain logic).

Beacon is the terminal sink for everything the other agents flag: dead-lettered
events, business rejections, denied secret requests. It turns a raw event into a
classified `Alert`, routes it to channels by severity, and suppresses duplicate
alerts so a storm of identical failures doesn't page a human 500 times.

Three seams, all injectable for testing:
  * **Severity policy** (`severity_for`) — the "what's urgent" decision.
  * **Routing** (`routes`) — severity -> which channels fire.
  * **Channels** (`NotificationChannel`) — how an alert actually reaches a human.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Mapping, Protocol

log = logging.getLogger("cavi.beacon")

DEFAULT_DEDUP_WINDOW_SECONDS = 300  # collapse identical alerts within 5 minutes


class Severity(IntEnum):
    INFO = 10
    WARNING = 20
    ERROR = 30
    CRITICAL = 40


@dataclass(frozen=True)
class Alert:
    severity: Severity
    title: str
    subject: str             # originating event subject
    body: str
    dedup_key: str
    correlation_id: str | None = None


# --------------------------------------------------------------------------- #
# Severity policy (the business seam — tune this to taste)
# --------------------------------------------------------------------------- #
def severity_for(subject: str) -> Severity:
    """Map an event subject to an alert severity.

    A dead-lettered *financial* event is the worst case (money couldn't be
    parsed), so it is CRITICAL; other dead-letters are ERROR. Business
    rejections are WARNING; routine facts are INFO.
    """
    if subject.startswith("deadletter."):
        inner = subject[len("deadletter."):]
        return Severity.CRITICAL if inner.startswith("ledger.") else Severity.ERROR
    return {
        "ledger.rejected": Severity.WARNING,
        "vault.secret.denied": Severity.WARNING,
        "ledger.posted": Severity.INFO,
        "forge.completed": Severity.INFO,
    }.get(subject, Severity.INFO)


def classify(subject: str, payload: Mapping, correlation_id: str | None = None) -> Alert:
    """Build a classified Alert from an inbound event."""
    severity = severity_for(subject)
    # A stable identifier keeps repeats of the *same* failure deduped while
    # distinct failures stay separate.
    ident = (
        payload.get("entry_id")
        or payload.get("work_order_id")
        or payload.get("id")
        or ""
    )
    dedup_key = f"{subject}:{ident}"
    body = json.dumps(dict(payload), default=str)[:500]
    return Alert(
        severity=severity,
        title=f"{severity.name}: {subject}",
        subject=subject,
        body=body,
        dedup_key=dedup_key,
        correlation_id=correlation_id,
    )


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #
class NotificationChannel(Protocol):
    def send(self, alert: Alert) -> None: ...


class LogChannel:
    """Always-on channel that writes alerts to the application log."""

    def send(self, alert: Alert) -> None:
        log.log(
            logging.ERROR if alert.severity >= Severity.ERROR else logging.INFO,
            "ALERT %s — %s", alert.title, alert.body,
        )


class CollectingChannel:
    """Captures alerts in memory — used by tests and dashboards."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


class HermesChannel:
    """Delivers alerts to humans via the Hermes bridge (Telegram/Slack/etc.).

    The transport is injected as `send_fn(target, text)` so Beacon stays
    decoupled from *how* Hermes is reached — a gateway webhook, the CLI, or the
    MCP messages_send tool in an orchestration layer.
    """

    def __init__(self, target: str, send_fn: Callable[[str, str], None]) -> None:
        self._target = target
        self._send_fn = send_fn

    def send(self, alert: Alert) -> None:
        self._send_fn(self._target, f"[{alert.severity.name}] {alert.title}\n{alert.body}")


# severity -> channel names that should fire. The routing seam.
DEFAULT_ROUTES: dict[Severity, tuple[str, ...]] = {
    Severity.INFO: ("log",),
    Severity.WARNING: ("log",),
    Severity.ERROR: ("log", "hermes"),
    Severity.CRITICAL: ("log", "hermes"),
}


# --------------------------------------------------------------------------- #
# Dedup store (the "have I paged about this already?" state)
# --------------------------------------------------------------------------- #
class DedupStore(Protocol):
    def is_duplicate(self, key: str, window_seconds: int) -> bool: ...


class InMemoryDedupStore:
    """Process-local dedup. Fine for a single process and tests, but the state is
    lost on restart and not shared across replicas — so a restarting/scaled-out
    Beacon would re-page humans. `RedisDedupStore` fixes that in production."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    def is_duplicate(self, key: str, window_seconds: int) -> bool:
        now = self._clock()
        last = self._last_sent.get(key)
        if last is not None and now - last < window_seconds:
            return True
        self._last_sent[key] = now
        return False


class RedisDedupStore:
    """Durable, fleet-wide dedup via Redis ``SET key NX EX``.

    The key survives restarts and is shared across Beacon replicas, so an alert
    storm is suppressed exactly once fleet-wide instead of once per process. The
    client is imported lazily so importing this module needs no redis.
    """

    def __init__(self, client=None, *, namespace: str = "beacon:dedup") -> None:
        self._client = client
        self._namespace = namespace

    def _conn(self):
        if self._client is None:
            from shared.cache import get_client

            self._client = get_client()
        return self._client

    def is_duplicate(self, key: str, window_seconds: int) -> bool:
        # SET NX EX is atomic: it sets the key only if absent, with a TTL equal to
        # the dedup window. A successful set (truthy) => first sighting in the
        # window => NOT a duplicate. A failed set (falsy) => key already present
        # => duplicate.
        was_set = self._conn().set(
            f"{self._namespace}:{key}", "1", nx=True, ex=window_seconds
        )
        return not was_set


# --------------------------------------------------------------------------- #
# Beacon
# --------------------------------------------------------------------------- #
class Beacon:
    def __init__(
        self,
        channels: Mapping[str, NotificationChannel],
        *,
        routes: Mapping[Severity, tuple[str, ...]] = DEFAULT_ROUTES,
        dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
        dedup: DedupStore | None = None,
    ) -> None:
        self._channels = dict(channels)
        self._routes = dict(routes)
        self._dedup_window = dedup_window_seconds
        # Injectable so production can use a durable, fleet-wide RedisDedupStore;
        # defaults to process-local (with the injected clock) for tests/local.
        self._dedup: DedupStore = dedup if dedup is not None else InMemoryDedupStore(clock=clock)

    def notify(
        self, subject: str, payload: Mapping, correlation_id: str | None = None
    ) -> Alert | None:
        """Classify, dedup, and dispatch. Returns the Alert sent, or None if it
        was suppressed as a duplicate within the dedup window."""
        alert = classify(subject, payload, correlation_id)
        if self._is_duplicate(alert):
            return None
        self._dispatch(alert)
        return alert

    def _is_duplicate(self, alert: Alert) -> bool:
        return self._dedup.is_duplicate(alert.dedup_key, self._dedup_window)

    def _dispatch(self, alert: Alert) -> None:
        for name in self._routes.get(alert.severity, ()):
            channel = self._channels.get(name)
            if channel is not None:
                channel.send(alert)
