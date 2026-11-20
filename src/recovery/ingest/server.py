"""HTTP front for `WebhookIngest` — the hosted receiver.

    python -m recovery.ingest.server            # reads its configuration from the environment

`webhook.py` turns untrusted bytes into trusted facts and owns every decision about a delivery.
This module only moves bytes between a socket and that class, so the hosted endpoint and the
in-process demo run the same verification, dedup and precedence code. Stdlib only, like the
rest of the runtime.

Configuration, all from the environment so no value is ever written into the repo:

    RAZORPAY_WEBHOOK_SECRET   required. Must match Dashboard -> Webhooks exactly.
    PORT                      default 8080. Hosting platforms inject this.
    EVENT_STORE               default runtime/events.sqlite3. Ignored when DATABASE_URL is set.
    DATABASE_URL              optional. A postgres:// URL; when set, dedup and payment state live
                              in Postgres (`pg_store.py`) and survive the container being
                              replaced, which the SQLite file does not.

## Two refusals at startup, each a failure this repo has already had

- **An empty secret** rejects every delivery while the endpoint looks healthy. `WebhookIngest`
  already refuses it.
- **A secret with surrounding whitespace** did the same to 18 real deliveries in phase 0
  (`results/phase0/0.4d-razorpay-originated-event.json`). Stripping it silently would make a
  dashboard-side typo invisible, so the server refuses to start and says why instead.

## Routes

    POST /webhook   Razorpay deliveries. 200 on accept or duplicate, 400 on a bad signature.
    GET  /healthz   200 "ok". Touches nothing, so health probes never create an event.

Anything else is 404. Bodies over `MAX_BODY_BYTES` get 413 before they are read: a Razorpay
payment event is a few kilobytes, and an unauthenticated endpoint should not buffer whatever
it is sent.
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .webhook import EVENT_ID_HEADER, SIGNATURE_HEADER, EventStore, WebhookIngest

#: Razorpay events are a few KB. 1 MB is generous and still bounds what a stranger can send.
MAX_BODY_BYTES = 1_000_000

#: A client that opens a socket and sends nothing must not hold a thread forever.
SOCKET_TIMEOUT_SECONDS = 10


def make_handler(ingest: WebhookIngest) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = SOCKET_TIMEOUT_SECONDS
        server_version = "recovery-webhook"
        sys_version = ""

        def _reply(self, code: int, text: str) -> None:
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            if self.path == "/healthz":
                self._reply(200, "ok")
            else:
                self._reply(404, "not found")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/webhook":
                self._reply(404, "not found")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._reply(411, "content-length required")
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._reply(413, "body too large")
                return
            raw = self.rfile.read(length)
            code, detail = ingest.handle(
                raw,
                self.headers.get(SIGNATURE_HEADER, ""),
                self.headers.get(EVENT_ID_HEADER, ""),
                user_agent=self.headers.get("User-Agent", ""),
            )
            self._reply(code, detail)

        def log_message(self, fmt: str, *args) -> None:
            # Method, path and status only. Never headers or bodies: the signature header
            # and the payment payload do not belong in a hosting platform's log stream.
            sys.stderr.write(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")

    return Handler


def config_from_env(env: dict[str, str]) -> tuple[str, int, str, str | None]:
    secret = env.get("RAZORPAY_WEBHOOK_SECRET", "")
    if secret != secret.strip():
        raise SystemExit("RAZORPAY_WEBHOOK_SECRET has leading or trailing whitespace; "
                         "fix it where it is set rather than here - refusing to start")
    if not secret:
        raise SystemExit("RAZORPAY_WEBHOOK_SECRET is not set - refusing to start")
    port = int(env.get("PORT", "8080"))
    store = env.get("EVENT_STORE", "runtime/events.sqlite3")
    database_url = env.get("DATABASE_URL") or None
    return secret, port, store, database_url


def build_server(secret: str, port: int, store_path: str, host: str = "0.0.0.0",
                 database_url: str | None = None) -> ThreadingHTTPServer:
    if database_url:
        from .pg_store import PgEventStore  # only the hosted image has the driver
        store = PgEventStore(database_url)
    else:
        store = EventStore(store_path)
    ingest = WebhookIngest(secret, store)
    return ThreadingHTTPServer((host, port), make_handler(ingest))


def describe_store(store_path: str, database_url: str | None) -> str:
    """For the startup line. Host and database name only, never the credentials."""
    if not database_url:
        return f"sqlite {store_path}"
    u = urlparse(database_url)
    return f"postgres {u.hostname}:{u.port or 5432}/{u.path.lstrip('/')}"


def main() -> int:
    secret, port, store, database_url = config_from_env(dict(os.environ))
    server = build_server(secret, port, store, database_url=database_url)
    sys.stderr.write(f"webhook receiver listening on :{port}, store "
                     f"{describe_store(store, database_url)}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
