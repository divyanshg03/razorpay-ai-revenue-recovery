"""Webhook ingest — product version of the gate 0.4 spike.

Three things carried over from `spikes/webhook_receiver.py`, each learned expensively:

1. **Verify HMAC-SHA256 over the RAW body.** Parsing JSON and re-serialising changes the
   bytes and breaks the signature. Gate 0.4 demonstrated this rather than quoting it: the same
   payload went from 207 bytes to 223 and failed verification.

2. **Dedup must be PERSISTENT.** The spike used an in-memory set, which dies with the process.
   Razorpay retries with exponential backoff for 24 hours, so a restart mid-retry would
   reprocess events the old process had already handled. SQLite, not a set.

3. **Rejections must be logged durably.** A secret with trailing whitespace caused 18 real
   deliveries to be rejected while the endpoint reported perfectly healthy and the event log
   stayed empty. "Razorpay never called" and "Razorpay called and we rejected it" are opposite
   problems that look identical without this.

## The resurrection problem, and why last-write-wins is wrong

Razorpay states the webhook sequence "is not fixed", and `payment.failed` can be followed by
`payment.captured` for the same transaction. Naively storing the latest event would leave a
paid customer marked failed whenever the pair arrives out of order — and we would then dun
someone who has already paid.

So payment state is resolved by **precedence, not arrival order**: `captured` beats
`authorized` beats `failed`. Precedence is order-independent, which is the only property that
survives an at-least-once, unordered delivery guarantee.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import pathlib
import sqlite3
import threading

from ..models import IST

SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "X-Razorpay-Event-Id"

#: Razorpay abandons a delivery after 5 seconds. Verify, persist, ACK — never process inline.
ACK_BUDGET_SECONDS = 5.0

#: Higher wins, regardless of which event arrived last.
_STATE_PRECEDENCE = {
    "created": 0,
    "failed": 1,
    "authorized": 2,
    "captured": 3,
    "refunded": 4,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_events (
    event_id   TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_state (
    payment_id TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    precedence INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rejected_at TEXT NOT NULL,
    reason     TEXT NOT NULL,
    event_id   TEXT,
    user_agent TEXT,
    body       TEXT,
    signature  TEXT
);
"""


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 over the raw body, compared in constant time.

    `compare_digest` rather than `==`: a naive comparison leaks the length of the correct
    prefix through timing.
    """
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class EventStore:
    """SQLite-backed dedup and payment state. Survives restarts, unlike the spike."""

    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _now(self) -> str:
        return dt.datetime.now(IST).isoformat()

    def is_duplicate(self, event_id: str) -> bool:
        """At-least-once delivery means the same event id WILL arrive more than once.

        Gate 0.4 observed the same id redelivered four times with widening gaps, so this is
        load-bearing rather than defensive.
        """
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM seen_events WHERE event_id = ?", (event_id,))
            return cur.fetchone() is not None

    def mark_seen(self, event_id: str, event_type: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_events (event_id, event_type, received_at) "
                "VALUES (?, ?, ?)", (event_id, event_type, self._now()))
            self._conn.commit()

    def observe_payment(self, payment_id: str, status: str) -> str:
        """Apply precedence. Returns the resolved status after this observation.

        Deliberately NOT last-write-wins: an out-of-order `payment.failed` arriving after
        `payment.captured` must not un-pay the customer.
        """
        incoming = _STATE_PRECEDENCE.get(status, 0)
        with self._lock:
            cur = self._conn.execute(
                "SELECT status, precedence FROM payment_state WHERE payment_id = ?",
                (payment_id,))
            row = cur.fetchone()
            if row is None or incoming > row[1]:
                self._conn.execute(
                    "INSERT INTO payment_state (payment_id, status, precedence, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(payment_id) DO UPDATE SET "
                    "status=excluded.status, precedence=excluded.precedence, "
                    "updated_at=excluded.updated_at",
                    (payment_id, status, incoming, self._now()))
                self._conn.commit()
                return status
            return row[0]

    def payment_status(self, payment_id: str) -> str | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT status FROM payment_state WHERE payment_id = ?", (payment_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def is_settled(self, payment_id: str) -> bool:
        """Used by the pre-action state re-check. If this is True, do not contact."""
        return self.payment_status(payment_id) in ("captured", "authorized")

    def log_rejection(self, reason: str, event_id: str, user_agent: str,
                      body: bytes, signature: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rejections (rejected_at, reason, event_id, user_agent, body, "
                "signature) VALUES (?, ?, ?, ?, ?, ?)",
                (self._now(), reason, event_id, user_agent,
                 body.decode("utf-8", errors="replace"), signature))
            self._conn.commit()

    def rejection_count(self, sender_contains: str | None = None) -> int:
        """zrok proxies to localhost, so peer IP is always 127.0.0.1 for tunnelled traffic.
        The User-Agent is the only reliable way to tell who was rejected."""
        with self._lock:
            if sender_contains:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM rejections WHERE user_agent LIKE ?",
                    (f"%{sender_contains}%",))
            else:
                cur = self._conn.execute("SELECT COUNT(*) FROM rejections")
            return cur.fetchone()[0]

    def close(self) -> None:
        self._conn.close()


class WebhookIngest:
    """Verify, dedup, persist, ACK. Deliberately contains no business logic.

    Nothing here decides anything about a customer. It turns untrusted HTTP into trusted
    facts, and hands them on.
    """

    def __init__(self, secret: str, store: EventStore):
        if not secret:
            # An empty secret rejects every delivery while the endpoint looks perfectly
            # healthy — the exact silent failure gate 0.4 spent 40 minutes on.
            raise ValueError("webhook secret is empty; refusing to start")
        self.secret = secret
        self.store = store

    def handle(self, raw_body: bytes, signature: str, event_id: str,
               user_agent: str = "") -> tuple[int, str]:
        if not verify_signature(raw_body, signature, self.secret):
            self.store.log_rejection("invalid signature", event_id, user_agent,
                                     raw_body, signature)
            return 400, "invalid signature"
        if not event_id:
            self.store.log_rejection("missing event id", "", user_agent, raw_body, signature)
            return 400, "missing event id"

        # Dedup AFTER verifying: otherwise an unsigned caller could poison the dedup table
        # with a guessed event id and suppress a real delivery.
        if self.store.is_duplicate(event_id):
            return 200, "duplicate ignored"

        # Only now is it safe to parse.
        payload = json.loads(raw_body)
        event_type = payload.get("event", "unknown")
        self.store.mark_seen(event_id, event_type)

        entity = (payload.get("payload", {}).get("payment", {}).get("entity", {}))
        payment_id = entity.get("id")
        status = entity.get("status")
        if payment_id and status:
            self.store.observe_payment(payment_id, status)

        return 200, "queued"
