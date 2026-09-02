"""Watch for the first payment on the account and capture gate 0.2 + 0.4 evidence.

Both gates are blocked on the same fact: this account has never had a payment. The moment
one exists, two questions get answered at once —

  0.2  do the five error fields populate in test mode as the docs claim, and which spelling
       actually arrives (`insufficient_fund` vs `insufficient_funds` — the docs use both)?
  0.4  does an event that RAZORPAY signed reach our webhook, as opposed to one we signed
       ourselves?

Run this, then drive the payment link to failure in a browser. It polls until a payment
appears, records everything, and exits.

Run: python scripts/capture_payment_evidence.py [--timeout-minutes 45]
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

API = "https://api.razorpay.com/v1"
REPO = pathlib.Path(__file__).resolve().parents[1]
EVENT_LOG = REPO / "results" / "phase0" / "0.4c-received-events.jsonl"
OUT = REPO / "results" / "phase0" / "0.2c-error-fields.json"

# Event ids we generated ourselves during gate 0.4 development. Anything NOT matching these
# prefixes came from Razorpay, which is the entire point of the check.
SELF_TEST_PREFIXES = ("evt_daemon", "evt_recovery", "evt_health", "evt_verify", "evt_chk",
                      "evt_pin", "evt_rsrv", "evt_zrok", "evt_restart")

ERROR_FIELDS = ("error_code", "error_description", "error_source", "error_step",
                "error_reason")


def load_keys() -> tuple[str, str]:
    env = {}
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    key_id, secret = env.get("RAZORPAY_KEY_ID"), env.get("RAZORPAY_KEY_SECRET")
    if not key_id or not secret:
        sys.exit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET missing from .env")
    return key_id, secret


def get(path: str, key_id: str, secret: str):
    auth = base64.b64encode(f"{key_id}:{secret}".encode()).decode()
    req = urllib.request.Request(API + path, headers={"Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def read_events() -> list[dict]:
    if not EVENT_LOG.exists():
        return []
    rows = []
    for line in EVENT_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def razorpay_originated(events: list[dict]) -> list[dict]:
    return [e for e in events
            if not any(e.get("event_id", "").startswith(p) for p in SELF_TEST_PREFIXES)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-minutes", type=int, default=45)
    parser.add_argument("--poll-seconds", type=int, default=10)
    args = parser.parse_args()

    key_id, secret = load_keys()
    baseline = len(read_events())
    deadline = time.time() + args.timeout_minutes * 60

    print(f"watching for a payment (timeout {args.timeout_minutes}m)...", flush=True)
    print(f"baseline: {baseline} event(s) already logged", flush=True)

    payment = None
    while time.time() < deadline:
        status, body = get("/payments?count=10", key_id, secret)
        if status == 200 and body.get("count", 0) > 0:
            payment = body["items"][0]
            print(f"payment appeared: {payment['id']} status={payment.get('status')}",
                  flush=True)
            break
        time.sleep(args.poll_seconds)

    if payment is None:
        print("timed out - no payment appeared. Nothing captured.", flush=True)
        return 2

    # Fetch the payment on its own endpoint: the list view does not always carry every
    # error field, and gate 0.2 is specifically about GET /payments/:id.
    status, detail = get(f"/payments/{payment['id']}", key_id, secret)

    # Give the webhook a moment; Razorpay delivers asynchronously and the tunnel adds ~2s.
    time.sleep(20)
    events = read_events()
    from_razorpay = razorpay_originated(events)

    present = {f: detail.get(f) for f in ERROR_FIELDS}
    populated = {f: v for f, v in present.items() if v not in (None, "")}

    report = {
        "gate": "0.2 (error field shape) + 0.4 (Razorpay-originated webhook event)",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "payment": {
            "id": detail.get("id"),
            "status": detail.get("status"),
            "method": detail.get("method"),
            "amount": detail.get("amount"),
        },
        "gate_0_2": {
            "http": status,
            "error_fields": present,
            "populated_count": len(populated),
            "all_five_populated": len(populated) == len(ERROR_FIELDS),
            # The docs use both spellings; this records which one the API actually returns.
            "error_reason_spelling_observed": detail.get("error_reason"),
            "verdict": ("PASS - fields populated" if populated else
                        "FAIL - no error fields populated (was the payment a success?)"),
        },
        "gate_0_4": {
            "events_before": baseline,
            "events_after": len(events),
            "razorpay_originated_count": len(from_razorpay),
            "razorpay_originated": [
                {"received_at": e["received_at"], "event_id": e["event_id"],
                 "event": e["event"]} for e in from_razorpay
            ],
            "verdict": ("PASS - a Razorpay-signed event was received, verified and logged"
                        if from_razorpay else
                        "INCOMPLETE - no Razorpay-originated event yet; check the webhook "
                        "is enabled in the Dashboard and subscribed to payment.failed"),
        },
        "raw_payment": detail,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    print(json.dumps({k: v for k, v in report.items() if k != "raw_payment"}, indent=2))
    print(f"\nwritten -> {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
