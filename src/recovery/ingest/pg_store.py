"""Postgres-backed event store for the hosted receiver.

`EventStore` in `webhook.py` keeps dedup and payment state in SQLite, which is right on a
laptop and wrong on a host that can replace the container: the file goes with it, and a
Razorpay retry that straddles the restart is accepted twice. This class keeps the same
public methods against a database that outlives the container, so `WebhookIngest` cannot
tell which one it was given.

The driver is `pg8000`, pure Python, imported only here and installed only in the Docker
image. Nothing else in the repository needs it: the batch, the demo and the test suite run
on the standard library and SQLite exactly as before, and a test that needs Postgres skips
itself when no database is configured.

## The precedence rule is enforced by the database, not by a read-then-write

SQLite's version reads the stored precedence, compares, then writes, under a process lock.
That lock means nothing across connections, so here the comparison lives inside the upsert:
`ON CONFLICT ... DO UPDATE ... WHERE payment_state.precedence < EXCLUDED.precedence`. Two
deliveries racing for the same payment cannot leave the lower-precedence one standing.
"""

from __future__ import annotations

import datetime as dt
import ssl
import threading
from urllib.parse import parse_qs, unquote, urlparse

from ..models import IST
from .webhook import _STATE_PRECEDENCE

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS seen_events (
        event_id    TEXT PRIMARY KEY,
        event_type  TEXT NOT NULL,
        received_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS payment_state (
        payment_id TEXT PRIMARY KEY,
        status     TEXT NOT NULL,
        precedence INTEGER NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS rejections (
        id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        rejected_at TEXT NOT NULL,
        reason      TEXT NOT NULL,
        event_id    TEXT,
        user_agent  TEXT,
        body        TEXT,
        signature   TEXT)""",
)


def connect_kwargs(url: str) -> dict:
    """`postgres://user:pass@host:port/db?sslmode=require` -> pg8000 keyword arguments.

    TLS is used when `sslmode` asks for it. `require` encrypts without checking the
    certificate, as libpq's `require` does; `verify-ca` and `verify-full` check it.
    """
    u = urlparse(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"not a postgres URL (scheme {u.scheme!r})")
    kw = {
        "user": unquote(u.username or ""),
        "password": unquote(u.password or "") or None,
        "host": u.hostname or "localhost",
        "port": u.port or 5432,
        "database": unquote(u.path.lstrip("/")) or None,
    }
    mode = parse_qs(u.query).get("sslmode", ["disable"])[0]
    if mode in ("verify-ca", "verify-full"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = mode == "verify-full"
        kw["ssl_context"] = ctx
    elif mode == "require":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kw["ssl_context"] = ctx
    elif mode not in ("disable", "allow", "prefer"):
        raise ValueError(f"unsupported sslmode {mode!r}")
    return kw


class PgEventStore:
    """Same public surface as `webhook.EventStore`, backed by Postgres."""

    def __init__(self, url: str):
        import pg8000.native  # deferred: only the hosted image installs it

        self._pg = pg8000.native
        self._kwargs = connect_kwargs(url)
        self._lock = threading.Lock()
        self._conn = self._pg.Connection(**self._kwargs)
        for stmt in _SCHEMA:
            self._conn.run(stmt)

    def _run(self, sql: str, **params):
        """One connection, serialised by a lock, reconnected once if the server dropped it.

        A managed database closes idle connections; without the retry the first delivery
        after a quiet night would fail and rely on Razorpay's retry to land.
        """
        with self._lock:
            try:
                return self._conn.run(sql, **params)
            except (self._pg.InterfaceError, OSError):
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001 - the connection is already broken
                    pass
                self._conn = self._pg.Connection(**self._kwargs)
                return self._conn.run(sql, **params)

    def _now(self) -> str:
        return dt.datetime.now(IST).isoformat()

    def is_duplicate(self, event_id: str) -> bool:
        return bool(self._run("SELECT 1 FROM seen_events WHERE event_id = :e", e=event_id))

    def mark_seen(self, event_id: str, event_type: str) -> None:
        self._run("INSERT INTO seen_events (event_id, event_type, received_at) "
                  "VALUES (:e, :t, :n) ON CONFLICT (event_id) DO NOTHING",
                  e=event_id, t=event_type, n=self._now())

    def observe_payment(self, payment_id: str, status: str) -> str:
        """Apply precedence inside the upsert, then return the status that stands."""
        self._run(
            "INSERT INTO payment_state (payment_id, status, precedence, updated_at) "
            "VALUES (:p, :s, :r, :n) ON CONFLICT (payment_id) DO UPDATE SET "
            "status = EXCLUDED.status, precedence = EXCLUDED.precedence, "
            "updated_at = EXCLUDED.updated_at "
            "WHERE payment_state.precedence < EXCLUDED.precedence",
            p=payment_id, s=status, r=_STATE_PRECEDENCE.get(status, 0), n=self._now())
        return self.payment_status(payment_id)

    def payment_status(self, payment_id: str) -> str | None:
        rows = self._run("SELECT status FROM payment_state WHERE payment_id = :p",
                         p=payment_id)
        return rows[0][0] if rows else None

    def is_settled(self, payment_id: str) -> bool:
        return self.payment_status(payment_id) in ("captured", "authorized")

    def log_rejection(self, reason: str, event_id: str, user_agent: str,
                      body: bytes, signature: str) -> None:
        self._run("INSERT INTO rejections (rejected_at, reason, event_id, user_agent, body, "
                  "signature) VALUES (:n, :r, :e, :u, :b, :s)",
                  n=self._now(), r=reason, e=event_id, u=user_agent,
                  b=body.decode("utf-8", errors="replace"), s=signature)

    def rejection_count(self, sender_contains: str | None = None) -> int:
        if sender_contains:
            rows = self._run("SELECT COUNT(*) FROM rejections WHERE user_agent LIKE :u",
                             u=f"%{sender_contains}%")
        else:
            rows = self._run("SELECT COUNT(*) FROM rejections")
        return int(rows[0][0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
