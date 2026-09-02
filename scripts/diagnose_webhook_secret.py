"""Prove — not guess — whether the Dashboard webhook secret matches ours.

Razorpay was delivering to our endpoint all along; every delivery was rejected with
"invalid signature". That is a secret mismatch, but "the signature failed" alone does not
prove which side is wrong. This script takes a rejected delivery's FULL body and signature
and tests candidate secrets against it, which settles it.

It also watches for the fix landing: Razorpay retries a failed webhook with exponential
backoff for 24 hours, so correcting the secret in the Dashboard should make an in-flight
retry verify without anyone touching the payment again.

Run: python scripts/diagnose_webhook_secret.py [--watch-minutes 20] [--candidate SECRET]...
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
REJECT_LOG = REPO / "runtime" / "webhook-rejected.jsonl"
EVENT_LOG = REPO / "results" / "phase0" / "0.4c-received-events.jsonl"

SELF_TEST_PREFIXES = ("evt_daemon", "evt_recovery", "evt_health", "evt_verify", "evt_chk",
                      "evt_pin", "evt_rsrv", "evt_zrok", "evt_restart", "evt_wrongsig")


def load_webhook_secret() -> str | None:
    """Read from .env, never hardcode. This repo is destined to be public and git history
    is permanent, so a live signing secret must not enter it."""
    env = REPO / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == "RAZORPAY_WEBHOOK_SECRET":
                return v.strip()  # .strip() matters: trailing space broke this once
    return None


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def razorpay_rejections() -> list[dict]:
    # zrok proxies to localhost, so peer is ALWAYS 127.0.0.1 for tunnelled traffic.
    # The User-Agent is what identifies the true sender.
    return [r for r in read_jsonl(REJECT_LOG) if "Razorpay" in r.get("user_agent", "")]


def razorpay_events() -> list[dict]:
    return [e for e in read_jsonl(EVENT_LOG)
            if not any(e.get("event_id", "").startswith(p) for p in SELF_TEST_PREFIXES)]


def test_candidates(rejection: dict, candidates: list[str]) -> dict | None:
    body = rejection.get("body")
    sig = rejection.get("signature")
    if not body or not sig:
        return None
    raw = body.encode("utf-8")
    results = {}
    for cand in candidates:
        digest = hmac.new(cand.encode(), raw, hashlib.sha256).hexdigest()
        results[cand] = hmac.compare_digest(digest, sig)
    return {"event_id": rejection.get("event_id"), "rejected_at": rejection.get("rejected_at"),
            "signature_received": sig, "candidates": results,
            "any_match": any(results.values())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch-minutes", type=int, default=20)
    parser.add_argument("--candidate", action="append", default=[],
                        help="extra secret to test (repeatable)")
    args = parser.parse_args()

    candidates = ([load_webhook_secret()] if load_webhook_secret() else []) + args.candidate
    if not candidates:
        sys.exit("no secret to test: set RAZORPAY_WEBHOOK_SECRET in .env or pass --candidate")

    already = razorpay_events()
    if already:
        print(f"gate 0.4 ALREADY CLOSED: {len(already)} Razorpay-originated event(s) verified")
        for e in already[-3:]:
            print(f"  {e['received_at']}  {e['event_id']}  {e['event']}")
        return 0

    # Test whatever full-signature rejection we already hold.
    testable = [r for r in razorpay_rejections() if r.get("signature") and r.get("body")]
    if testable:
        verdict = test_candidates(testable[-1], candidates)
        print(json.dumps(verdict, indent=2))
        if verdict and not verdict["any_match"]:
            print("\nCONFIRMED: the Dashboard secret does not match any candidate above.")
    else:
        print("no full-signature rejection captured yet (older entries predate the fix)")

    print(f"\nwatching {args.watch_minutes}m for a verified Razorpay event...", flush=True)
    deadline = time.time() + args.watch_minutes * 60
    seen_rejects = len(razorpay_rejections())
    while time.time() < deadline:
        events = razorpay_events()
        if events:
            print("\nGATE 0.4 CLOSED - Razorpay-originated event verified and logged:")
            print(json.dumps({k: events[-1][k] for k in ("received_at", "event_id", "event")},
                             indent=2))
            return 0
        now_rejects = razorpay_rejections()
        if len(now_rejects) > seen_rejects:
            newest = now_rejects[-1]
            seen_rejects = len(now_rejects)
            print(f"  [{newest['rejected_at']}] Razorpay retried, still rejected "
                  f"(event {newest['event_id']})", flush=True)
            verdict = test_candidates(newest, candidates)
            if verdict and verdict["any_match"]:
                print("  ...but a candidate secret MATCHES - the receiver is running the "
                      "wrong secret, not the Dashboard.")
        time.sleep(10)

    print("\nno verified Razorpay event within the window.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
