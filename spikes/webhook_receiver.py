"""Gate 0.4 spike — Razorpay webhook receive path.

THIS IS A SPIKE, NOT PRODUCT CODE. Phase 0 answers feasibility questions and writes no
product code; this exists only to prove the receive path is correct before Phase 1 builds
the real one. Standard library only, so it runs with no install step.

It answers four questions the plan asks of gate 0.4:

  1. Can we verify Razorpay's HMAC-SHA256 signature over the *raw* body?
  2. Does dedup on `x-razorpay-event-id` work? (delivery is at-least-once)
  3. Can we ACK inside the 5 second timeout while processing takes longer?
  4. Does a tampered body get rejected without being enqueued?

Run standalone:  python spikes/webhook_receiver.py --port 8080 --secret <whsec>
Run the proof:   python spikes/test_webhook_receiver.py
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "X-Razorpay-Event-Id"

# Razorpay gives up on a webhook after 5s. We ACK far inside that and process off-thread.
ACK_BUDGET_SECONDS = 5.0

# Bounded so a long run cannot grow the dedup set without limit. Razorpay retries with
# exponential backoff for 24h, so the window only has to outlive that; in Phase 1 this
# becomes a persistent store, since an in-memory set dies with the process.
DEDUP_WINDOW = 10_000


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 over the RAW request body.

    Razorpay's docs are explicit: "Do not parse or cast the webhook request body." Parsing
    JSON and re-serialising it changes whitespace and key order, which changes the digest.
    test_webhook_receiver.py demonstrates that failure rather than taking it on trust.
    """
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # Constant-time: a naive == leaks the correct prefix length by timing.
    return hmac.compare_digest(expected, signature)


class Receiver:
    """Verify, dedup, enqueue. Deliberately does no business logic."""

    def __init__(self, secret: str, process_delay: float = 0.0, log_path: str | None = None,
                 reject_log_path: str | None = None):
        self.secret = secret
        self.queue: queue.Queue = queue.Queue()
        self._seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._lock = threading.Lock()
        self.process_delay = process_delay
        # Append-only record of every VERIFIED event. Without this, an event that
        # arrives while nobody is watching leaves no evidence it ever came.
        self.log_path = log_path
        # Durable record of anything rejected, so a wrong Dashboard secret is visible
        # rather than silent. See _log_rejection.
        self.reject_log_path = reject_log_path
        self.processed: list[dict] = []
        self.rejected: list[str] = []
        self.duplicates: list[str] = []
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _already_seen(self, event_id: str) -> bool:
        """At-least-once delivery means the same event id can arrive more than once."""
        with self._lock:
            if event_id in self._seen:
                return True
            self._seen[event_id] = None
            if len(self._seen) > DEDUP_WINDOW:
                self._seen.popitem(last=False)
            return False

    def _log_rejection(self, reason: str, event_id: str, peer: str, ua: str,
                       raw_body: bytes, signature: str = "") -> None:
        """Rejections MUST be durable, not just in memory.

        A wrong secret in the Dashboard makes every Razorpay delivery fail signature
        verification and vanish with a 400 — while the endpoint still reports healthy,
        because it is healthy. Without this file there is no way to tell "Razorpay never
        called" from "Razorpay called and we rejected it", which are opposite problems.
        """
        if not self.reject_log_path:
            return
        try:
            with open(self.reject_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "rejected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "reason": reason,
                    "event_id": event_id,
                    "peer": peer,
                    # zrok proxies to localhost, so `peer` is always 127.0.0.1 for tunnelled
                    # traffic — the User-Agent is what identifies the real sender.
                    "user_agent": ua,
                    # Full body AND signature, so a candidate secret can be TESTED offline.
                    # A truncated body makes a signature mismatch undiagnosable.
                    "signature": signature,
                    "body": raw_body.decode("utf-8", errors="replace"),
                }) + "\n")
        except OSError:
            pass

    def accept(self, raw_body: bytes, signature: str, event_id: str,
               peer: str = "", user_agent: str = "") -> tuple[int, str]:
        """Called on the request thread. Must stay far below ACK_BUDGET_SECONDS."""
        if not verify_signature(raw_body, signature, self.secret):
            self.rejected.append(event_id)
            self._log_rejection("invalid signature", event_id, peer, user_agent,
                                raw_body, signature)
            return 400, "invalid signature"

        # Dedup AFTER verifying, so an unsigned caller cannot poison the dedup set with a
        # guessed event id and suppress a real event.
        if not event_id:
            return 400, "missing event id"
        if self._already_seen(event_id):
            self.duplicates.append(event_id)
            return 200, "duplicate ignored"

        self.queue.put((event_id, raw_body))
        return 200, "queued"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event_id, raw_body = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            # Only NOW is it safe to parse: the signature is already verified.
            if self.process_delay:
                time.sleep(self.process_delay)
            payload = json.loads(raw_body)
            record = {
                "received_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event_id": event_id,
                "event": payload.get("event"),
                "payload": payload,
            }
            self.processed.append({"event_id": event_id, "event": payload.get("event")})
            if self.log_path:
                try:
                    with open(self.log_path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(record) + "\n")
                except OSError:
                    pass  # never let a logging failure kill the worker
            self.queue.task_done()

    def stop(self) -> None:
        self._stop.set()
        self._worker.join(timeout=2)


def make_handler(receiver: Receiver):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)  # raw bytes, never re-encoded
            status, message = receiver.accept(
                raw_body,
                self.headers.get(SIGNATURE_HEADER, ""),
                self.headers.get(EVENT_ID_HEADER, ""),
                peer=self.client_address[0] if self.client_address else "",
                user_agent=self.headers.get("User-Agent", ""),
            )
            body = json.dumps({"status": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass  # keep test output readable

    return Handler


def serve(secret: str, port: int, process_delay: float = 0.0, log_path: str | None = None,
          reject_log_path: str | None = None):
    receiver = Receiver(secret, process_delay=process_delay, log_path=log_path,
                        reject_log_path=reject_log_path)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(receiver))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, receiver


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--secret", required=True, help="webhook secret from the dashboard")
    parser.add_argument("--log", default=None,
                        help="append verified events to this JSONL file")
    parser.add_argument("--reject-log", default=None,
                        help="append rejected requests to this JSONL file")
    args = parser.parse_args()
    srv, _ = serve(args.secret, args.port, log_path=args.log,
                   reject_log_path=args.reject_log)
    print(f"listening on :{args.port} — expose with: zrok share public {args.port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()
