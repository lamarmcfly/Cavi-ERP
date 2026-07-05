"""Tests for the observability layer — metrics, JSON logging, health surface."""
from __future__ import annotations

import io
import json
import logging
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from shared.health import health_response, make_health_handler, readiness_response
from shared.logging import JsonFormatter, configure_logging
from shared.metrics import DEADLETTERS, EVENTS_EMITTED, Metrics


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def test_counter_inc_and_value():
    m = Metrics()
    m.inc(EVENTS_EMITTED, agent="ledger", subject="ledger.posted")
    m.inc(EVENTS_EMITTED, agent="ledger", subject="ledger.posted")
    assert m.value(EVENTS_EMITTED, agent="ledger", subject="ledger.posted") == 2


def test_labels_are_distinct_series():
    m = Metrics()
    m.inc("c", a="1")
    m.inc("c", a="2")
    assert m.value("c", a="1") == 1 and m.value("c", a="2") == 1


def test_prometheus_exposition_format():
    m = Metrics()
    m.inc(DEADLETTERS, agent="forge", subject="ledger.entry")
    text = m.prometheus()
    assert 'cavi_deadletters_total{agent="forge",subject="ledger.entry"} 1' in text
    assert text.endswith("\n")


# --------------------------------------------------------------------------- #
# JSON logging
# --------------------------------------------------------------------------- #
def test_json_formatter_includes_extra_fields():
    record = logging.LogRecord(
        "cavi.test", logging.INFO, __file__, 1, "hello %s", ("world",), None
    )
    record.correlation_id = "corr-1"
    record.tenant_id = "tenant-acme"
    out = json.loads(JsonFormatter().format(record))
    assert out["level"] == "INFO" and out["msg"] == "hello world"
    assert out["logger"] == "cavi.test" and "ts" in out
    assert out["correlation_id"] == "corr-1" and out["tenant_id"] == "tenant-acme"


def test_configure_logging_emits_json_lines():
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    buf = io.StringIO()
    try:
        configure_logging(stream=buf)
        logging.getLogger("cavi.x").info("hi", extra={"agent": "ledger"})
        obj = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert obj["msg"] == "hi" and obj["agent"] == "ledger"
    finally:
        root.handlers[:], root.level = saved_handlers, saved_level


# --------------------------------------------------------------------------- #
# Health surface
# --------------------------------------------------------------------------- #
def test_healthz_is_ok():
    assert health_response() == (200, {"status": "ok"})


def test_readyz_200_when_all_checks_pass():
    status, body = readiness_response({"redis": lambda: True, "pg": lambda: True})
    assert status == 200 and body == {"ready": True, "checks": {"redis": True, "pg": True}}


def test_readyz_503_when_a_check_fails_or_raises():
    def boom() -> bool:
        raise RuntimeError("down")

    status, body = readiness_response({"redis": lambda: True, "pg": boom})
    assert status == 503 and body["ready"] is False and body["checks"]["pg"] is False


def test_metrics_and_health_over_http():
    registry = Metrics()
    registry.inc(EVENTS_EMITTED, agent="ledger", subject="ledger.posted")
    handler = make_health_handler(readiness={"ok": lambda: True}, registry=registry)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as resp:
            assert resp.status == 200
            assert 'cavi_events_emitted_total{agent="ledger",subject="ledger.posted"} 1' in resp.read().decode()
        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as resp:
            assert json.loads(resp.read())["status"] == "ok"
        with urllib.request.urlopen(f"{base}/readyz", timeout=5) as resp:
            assert json.loads(resp.read())["ready"] is True
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
