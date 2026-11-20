"""The hosted pieces: the webhook receiver's HTTP front, and the public results page.

The receiver tests run a real server on an ephemeral port and talk to it over a socket, so
what they exercise is what the hosting platform runs, not a handler called in-process.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import pathlib
import sqlite3
import ssl
import subprocess
import sys
import threading

import pytest

from recovery.ingest.pg_store import connect_kwargs
from recovery.ingest.server import (MAX_BODY_BYTES, build_server, config_from_env,
                                     describe_store)

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
        ("s", 8080, "runtime/events.sqlite3", None)
    assert config_from_env({"RAZORPAY_WEBHOOK_SECRET": "s", "PORT": "9000",
                            "EVENT_STORE": "/data/e.db", "DATABASE_URL": "postgres://h/d"}) == \
        ("s", 9000, "/data/e.db", "postgres://h/d")


def test_startup_line_never_prints_database_credentials():
    # A dotless host: with a dotted one, preflight's email detector reads the URL as an address.
    line = describe_store("x", "postgres://user:hunter2@dbhost:6543/events")
    assert line == "postgres dbhost:6543/events"
    assert "hunter2" not in line and "user" not in line


def test_connect_kwargs_parses_url_and_sslmode():
    kw = connect_kwargs("postgresql://u%40x:p%2Fw@h:6000/db?sslmode=require")
    assert (kw["user"], kw["password"], kw["host"], kw["port"], kw["database"]) == \
        ("u@x", "p/w", "h", 6000, "db")
    assert kw["ssl_context"].verify_mode == ssl.CERT_NONE
    assert "ssl_context" not in connect_kwargs("postgres://u@h/db")
    assert connect_kwargs("postgres://u@h/db?sslmode=verify-full")["ssl_context"].check_hostname
    with pytest.raises(ValueError):
        connect_kwargs("mysql://u@h/db")
    with pytest.raises(ValueError):
        connect_kwargs("postgres://u@h/db?sslmode=bogus")


# --- Postgres: the same behaviour, against a real database --------------------------------
# Runs only when TEST_DATABASE_URL points at a disposable database, e.g.
#   docker run --rm -d -p 55432:5432 -e POSTGRES_PASSWORD=test postgres:16
#   TEST_DATABASE_URL=postgres://postgres:test@127.0.0.1:55432/postgres pytest tests/test_deploy.py

PG_URL = os.environ.get("TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def pg_server():
    pytest.importorskip("pg8000")
    from recovery.ingest.pg_store import PgEventStore
    reset = PgEventStore(PG_URL)
    for table in ("seen_events", "payment_state", "rejections"):
        reset._run(f"TRUNCATE {table}")
    reset.close()
    srv = build_server(SECRET, 0, "unused", host="127.0.0.1", database_url=PG_URL)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@needs_pg
def test_pg_accepts_dedups_and_keeps_precedence(pg_server):
    from recovery.ingest.pg_store import PgEventStore
    captured, failed = _event("pay_pg", "captured"), _event("pay_pg", "failed")
    assert _request(pg_server, "POST", "/webhook", captured, _signed(captured, "evt_pc")) == \
        (200, "queued")
    assert _request(pg_server, "POST", "/webhook", captured, _signed(captured, "evt_pc")) == \
        (200, "duplicate ignored")
    assert _request(pg_server, "POST", "/webhook", failed, _signed(failed, "evt_pf"))[0] == 200
    assert _request(pg_server, "POST", "/webhook", failed,
                    {"X-Razorpay-Signature": "0" * 64, "X-Razorpay-Event-Id": "evt_bad"}) == \
        (400, "invalid signature")
    store = PgEventStore(PG_URL)
    try:
        assert store.payment_status("pay_pg") == "captured"
        assert store.is_settled("pay_pg")
        assert store.rejection_count() == 1
    finally:
        store.close()


@needs_pg
def test_pg_state_survives_a_new_process(pg_server):
    """The reason the store exists: a fresh connection (a replaced container) still knows the
    event id, so a retry that straddles the restart is not accepted twice."""
    body = _event("pay_restart", "failed")
    _request(pg_server, "POST", "/webhook", body, _signed(body, "evt_restart"))
    fresh = build_server(SECRET, 0, "unused", host="127.0.0.1", database_url=PG_URL)
    threading.Thread(target=fresh.serve_forever, daemon=True).start()
    try:
        assert _request(fresh, "POST", "/webhook", body, _signed(body, "evt_restart")) == \
            (200, "duplicate ignored")
    finally:
        fresh.shutdown()
        fresh.server_close()


@needs_pg
def test_pg_reconnects_after_the_server_drops_the_connection():
    """A managed database closes idle connections; the next delivery must still land."""
    pytest.importorskip("pg8000")
    from recovery.ingest.pg_store import PgEventStore
    store = PgEventStore(PG_URL)
    try:
        pid = store._run("SELECT pg_backend_pid()")[0][0]
        killer = PgEventStore(PG_URL)
        killer._run("SELECT pg_terminate_backend(:p)", p=pid)
        killer.close()
        store.mark_seen("evt_after_drop", "payment.failed")
        assert store.is_duplicate("evt_after_drop")
    finally:
        store.close()


def test_results_page_matches_readme():
    """`site/index.html` is the README converted. If it is stale, the hosted page is showing
    figures the repository no longer stands behind."""
    r = subprocess.run([sys.executable, "scripts/render_site.py", "--check"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, ("site/index.html is stale - run `python scripts/render_site.py`\n"
                               + r.stderr)
