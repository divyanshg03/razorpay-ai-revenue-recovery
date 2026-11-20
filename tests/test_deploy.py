"""The hosted pieces: the webhook receiver's HTTP front, and the public results page.

The receiver tests run a real server on an ephemeral port and talk to it over a socket, so
what they exercise is what the hosting platform runs, not a handler called in-process.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import pathlib
import sqlite3
import subprocess
import sys
import threading

import pytest

from recovery.ingest.server import MAX_BODY_BYTES, build_server, config_from_env

REPO = pathlib.Path(__file__).resolve().parents[1]
SECRET = "test-secret-not-a-real-one"


@pytest.fixture
def server(tmp_path):
    srv = build_server(SECRET, 0, str(tmp_path / "events.sqlite3"), host="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _request(srv, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    out = resp.status, resp.read().decode()
    conn.close()
    return out


def _event(payment_id, status):
    return json.dumps({"entity": "event", "event": f"payment.{status}",
                       "payload": {"payment": {"entity": {"id": payment_id,
                                                          "status": status}}}}).encode()


def _signed(body, event_id):
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": event_id,
            "Content-Type": "application/json"}


def test_healthz_creates_no_event(server):
    assert _request(server, "GET", "/healthz") == (200, "ok")


def test_signed_delivery_is_accepted_then_deduplicated(server):
    body = _event("pay_1", "failed")
    assert _request(server, "POST", "/webhook", body, _signed(body, "evt_1")) == (200, "queued")
    assert _request(server, "POST", "/webhook", body, _signed(body, "evt_1")) == \
        (200, "duplicate ignored")


def test_unsigned_delivery_is_rejected(server):
    body = _event("pay_1", "failed")
    code, detail = _request(server, "POST", "/webhook", body,
                            {"X-Razorpay-Signature": "0" * 64, "X-Razorpay-Event-Id": "evt_x"})
    assert (code, detail) == (400, "invalid signature")


def test_late_failed_does_not_unpay_over_http(server, tmp_path):
    """The resurrection problem, end to end through the socket: a `payment.failed` that
    arrives after `payment.captured` must leave the payment captured in the store."""
    captured, failed = _event("pay_2", "captured"), _event("pay_2", "failed")
    assert _request(server, "POST", "/webhook", captured, _signed(captured, "evt_c"))[0] == 200
    assert _request(server, "POST", "/webhook", failed, _signed(failed, "evt_f"))[0] == 200
    with sqlite3.connect(tmp_path / "events.sqlite3") as db:
        row = db.execute("SELECT status FROM payment_state WHERE payment_id = 'pay_2'").fetchone()
    assert row == ("captured",)


def test_oversized_body_is_refused_unread(server):
    code, _ = _request(server, "POST", "/webhook", b"",
                       {"Content-Length": str(MAX_BODY_BYTES + 1)})
    assert code == 413


def test_unknown_paths_are_404(server):
    assert _request(server, "GET", "/")[0] == 404
    assert _request(server, "POST", "/other", b"{}")[0] == 404


@pytest.mark.parametrize("secret", ["", " leading", "trailing ", "trailing\n"])
def test_bad_secrets_refuse_to_start(secret):
    with pytest.raises(SystemExit):
        config_from_env({"RAZORPAY_WEBHOOK_SECRET": secret})


def test_config_defaults():
    assert config_from_env({"RAZORPAY_WEBHOOK_SECRET": "s"}) == \
        ("s", 8080, "runtime/events.sqlite3")
    assert config_from_env({"RAZORPAY_WEBHOOK_SECRET": "s", "PORT": "9000",
                            "EVENT_STORE": "/data/e.db"}) == ("s", 9000, "/data/e.db")


def test_results_page_matches_readme():
    """`site/index.html` is the README converted. If it is stale, the hosted page is showing
    figures the repository no longer stands behind."""
    r = subprocess.run([sys.executable, "scripts/render_site.py", "--check"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, ("site/index.html is stale - run `python scripts/render_site.py`\n"
                               + r.stderr)
