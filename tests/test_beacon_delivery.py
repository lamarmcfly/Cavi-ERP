"""Tests for Beacon's real Hermes/Telegram delivery transport.

The transport POSTs ``{"target", "message"}`` to the Hermes gateway. These tests
drive the payload with an injected fake ``post`` (no real HTTP) and assert the
three behaviors that matter operationally:

  * a configured webhook delivers the exact Hermes contract,
  * an unconfigured webhook degrades to log-only (never raises, never posts),
  * a transport failure is swallowed so Beacon stays up.
"""
from __future__ import annotations

import httpx
import pytest

from agents.beacon.agent import build_hermes_sender


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


class RecordingPoster:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.calls: list[dict] = []
        self._response = response or FakeResponse()

    def __call__(self, url, *, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self._response


def test_configured_webhook_posts_hermes_contract():
    poster = RecordingPoster()
    send = build_hermes_sender("http://hermes.local:9000/notify", post=poster)

    send("telegram:1370595013", "[CRITICAL] dead-lettered ledger.entry\nentry_id: e1")

    assert len(poster.calls) == 1
    call = poster.calls[0]
    assert call["url"] == "http://hermes.local:9000/notify"
    # Exact Hermes /notify body: target + message (matches the n8n workflow).
    assert call["json"] == {
        "target": "telegram:1370595013",
        "message": "[CRITICAL] dead-lettered ledger.entry\nentry_id: e1",
    }


def test_empty_webhook_is_log_only_no_post():
    poster = RecordingPoster()
    send = build_hermes_sender("", post=poster)

    send("telegram:1370595013", "should not be delivered")

    assert poster.calls == []  # degraded to log-only, nothing posted


def test_transport_failure_is_swallowed():
    def exploding_post(url, *, json, timeout):
        raise httpx.ConnectError("connection refused")

    send = build_hermes_sender("http://hermes.local:9000/notify", post=exploding_post)

    # Must not raise — Beacon survives a downstream delivery failure.
    send("telegram:1370595013", "critical alert")


def test_http_error_status_is_swallowed():
    poster = RecordingPoster(FakeResponse(status_code=500))
    send = build_hermes_sender("http://hermes.local:9000/notify", post=poster)

    # raise_for_status() raises HTTPStatusError, which the sender must swallow.
    send("telegram:1370595013", "critical alert")
    assert len(poster.calls) == 1


@pytest.mark.parametrize("webhook", ["", "http://hermes.local:9000/notify"])
def test_sender_is_callable_regardless_of_config(webhook):
    send = build_hermes_sender(webhook, post=RecordingPoster())
    # Never raises for either configuration.
    send("telegram:1370595013", "ping")
