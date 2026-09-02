"""Gate 0.4 proof. Drives the spike receiver over real HTTP and prints a JSON verdict.

Run: python spikes/test_webhook_receiver.py

Every assertion here corresponds to a documented Razorpay webhook behaviour, cited in
docs/razorpay-api-notes.md. Output is archived to results/phase0/.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import socket
import sys
import time

from webhook_receiver import ACK_BUDGET_SECONDS, serve

# Fake fixture, never registered anywhere. Deliberately not `whsec_`-prefixed so
# secret scanners on the public repo do not flag it.
SECRET = "test-fixture-not-a-real-secret"


def sign(raw: bytes) -> str:
    return hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def post(port: int, raw: bytes, signature: str, event_id: str) -> tuple[int, dict, float]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    start = time.perf_counter()
    conn.request(
        "POST",
        "/webhook",
        body=raw,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
        },
    )
    response = conn.getresponse()
    body = json.loads(response.read())
    elapsed = time.perf_counter() - start
    conn.close()
    return response.status, body, elapsed


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    port = free_port()
    # 1.5s of fake work per event: long enough to prove the ACK does not wait for it.
    server, receiver = serve(SECRET, port, process_delay=1.5)
    time.sleep(0.2)

    # Note the spacing and key order — this is what Razorpay would put on the wire, and the
    # signature is computed over exactly these bytes.
    raw = (
        b'{"entity":"event","event":"payment.failed","contains":["payment"],'
        b'"payload":{"payment":{"entity":{"id":"pay_SPIKE01","status":"failed",'
        b'"error_code":"BAD_REQUEST_ERROR","error_reason":"insufficient_funds"}}}}'
    )
    good = sign(raw)
    checks: dict[str, dict] = {}

    # 1. A correctly signed event is accepted, and the ACK lands well inside 5s despite the
    #    worker taking 1.5s. This is the queue-and-ACK requirement.
    status, body, elapsed = post(port, raw, good, "evt_001")
    checks["valid_event_accepted"] = {
        "http": status,
        "body": body,
        "ack_seconds": round(elapsed, 4),
        "ack_budget_seconds": ACK_BUDGET_SECONDS,
        "pass": status == 200 and body["status"] == "queued" and elapsed < ACK_BUDGET_SECONDS,
    }

    # 2. At-least-once delivery: the same event id again must not enqueue a second time.
    status, body, _ = post(port, raw, good, "evt_001")
    checks["duplicate_event_id_deduped"] = {
        "http": status,
        "body": body,
        "pass": status == 200 and body["status"] == "duplicate ignored",
    }

    # 3. Tampered body -> signature fails -> rejected and never enqueued.
    tampered = raw.replace(b'"status":"failed"', b'"status":"captured"')
    status, body, _ = post(port, tampered, good, "evt_002")
    checks["tampered_body_rejected"] = {
        "http": status,
        "body": body,
        "pass": status == 400 and body["status"] == "invalid signature",
    }

    # 4. THE ONE THAT MATTERS. Parse the JSON and re-serialise it — semantically identical,
    #    byte-wise different — and the original signature no longer verifies. This is why
    #    Razorpay says "Do not parse or cast the webhook request body", and why the handler
    #    reads raw bytes off the wire and verifies before json.loads.
    reserialised = json.dumps(json.loads(raw)).encode()
    status, body, _ = post(port, reserialised, good, "evt_003")
    checks["reserialised_body_fails_signature"] = {
        "http": status,
        "body": body,
        "raw_len": len(raw),
        "reserialised_len": len(reserialised),
        "bytes_differ": raw != reserialised,
        "pass": status == 400 and raw != reserialised,
    }

    # 5. Missing signature header is rejected outright.
    status, body, _ = post(port, raw, "", "evt_004")
    checks["missing_signature_rejected"] = {
        "http": status,
        "body": body,
        "pass": status == 400,
    }

    # 6. A distinct event id after a duplicate still gets through, i.e. dedup is keyed on the
    #    id and not just "have I seen this body".
    status, body, _ = post(port, raw, good, "evt_005")
    checks["distinct_event_id_accepted"] = {
        "http": status,
        "body": body,
        "pass": status == 200 and body["status"] == "queued",
    }

    # Let the worker drain so we can show ACK and processing are genuinely decoupled.
    time.sleep(4.0)
    checks["processing_decoupled_from_ack"] = {
        "enqueued_and_processed": len(receiver.processed),
        "duplicates_ignored": len(receiver.duplicates),
        "signature_rejections": len(receiver.rejected),
        "expected_processed": 2,
        "pass": len(receiver.processed) == 2 and len(receiver.duplicates) == 1,
    }

    receiver.stop()
    server.shutdown()

    verdict = {
        "gate": "0.4 — webhook receive path (local leg)",
        "scope": "receiver logic only; the zrok tunnel + dashboard registration leg is NOT covered here",
        "checks": checks,
        "all_passed": all(c["pass"] for c in checks.values()),
    }
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
