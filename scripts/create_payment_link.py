"""Create a test-mode Payment Link and print its checkout URL.

Gates 0.2 and 0.4 are both blocked on the same thing: this account has never had a payment,
so no error fields exist to inspect and no webhook event has ever had cause to fire. Driving
one link to a FAILURE through the hosted checkout unblocks both at once.

Server-side payment APIs are gated on this account (same as Subscriptions, see
docs/phase-0-findings.md), so the failure has to be produced through the checkout UI. Test
card 4100 2800 0008 0001 maps to `insufficient_fund` — and you must select "failure" on the
success/failure screen, otherwise the card alone will not do it.

NOTE: test mode caps Payment Links at 30 per business. Check results/phase0/ before
creating them in bulk.

Run: python scripts/create_payment_link.py [--amount-paise 49900]
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import urllib.error
import urllib.request

API = "https://api.razorpay.com/v1"
REPO = pathlib.Path(__file__).resolve().parents[1]


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
    if not key_id.startswith("rzp_test_"):
        sys.exit(f"refusing to run: key is not test mode ({key_id[:12]}...)")
    return key_id, secret


def call(method: str, path: str, key_id: str, secret: str, body: dict | None = None):
    auth = base64.b64encode(f"{key_id}:{secret}".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--amount-paise", type=int, default=49900,
                        help="amount in paise (default 49900 = Rs 499)")
    args = parser.parse_args()

    key_id, secret = load_keys()

    payload = {
        "amount": args.amount_paise,
        "currency": "INR",
        "description": "Gate 0.2/0.4 - recovery retry probe",
        "customer": {
            "name": "Gate Probe",
            # Repeating digits are rejected by the API; learned in gate 0.5.
            "contact": "+919812345670",
            "email": "gate-probe@example.com",
        },
        # No notification: this link exists to be driven manually, not to dun anyone.
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"gate": "0.2+0.4", "purpose": "observe error fields and fire a webhook"},
    }

    status, resp = call("POST", "/payment_links", key_id, secret, payload)
    out = {"request": {"method": "POST", "path": "/payment_links", "body": payload},
           "status": status, "response": resp}

    dest = REPO / "results" / "phase0" / "0.2b-payment-link-for-failure.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")

    if status != 200:
        print(json.dumps(out, indent=2))
        return 1

    print(f"payment link id : {resp['id']}")
    print(f"amount          : Rs {resp['amount'] / 100:.2f}")
    print(f"CHECKOUT URL    : {resp['short_url']}")
    print(f"evidence        : {dest.relative_to(REPO)}")
    print()
    print("Drive it to FAILURE:")
    print("  card    4100 2800 0008 0001   (maps to insufficient_fund)")
    print("  expiry  any future date       cvv any 3 digits")
    print("  then SELECT 'failure' on the success/failure screen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
