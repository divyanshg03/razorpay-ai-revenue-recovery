"""Produce a small, committable extract of the Phase 3 audit ledger.

The full ledgers are ~27 MB each (40k records) and are gitignored. They do not need to be
committed, because the batch is deterministic: `python scripts/run_batch.py` regenerates
them byte-for-byte from the seed. What the repo carries instead is:

  1. the CHAIN HEAD HASH of each ledger, so a regenerated ledger can be proved identical
  2. a full REPLAY of three representative debts, chosen by outcome rather than by
     position, so the "every decision is reconstructable" claim can be checked by hand
     without downloading 82 MB

Choosing the examples by outcome matters. An arbitrary head-of-file slice would show
whatever happened to sort first; these show a recovery, a statutory stop, and a debt the
engine gave up on — including the refusals, which are the compliance evidence.

Run: python scripts/ledger_extract.py
Out: results/phase3/ledger-extract.json
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from recovery.ledger.audit import AuditLedger  # noqa: E402

LEDGERS = {
    "primary_21d": REPO / "results" / "phase3" / "arm-c-audit.jsonl",
    "secondary_14d": REPO / "results" / "phase3" / "arm-c-audit-14d.jsonl",
    "shifted": REPO / "results" / "phase3" / "arm-c-audit-shifted.jsonl",
}


def pick_examples(entries: list[dict]) -> dict[str, str]:
    """One debt per interesting outcome, by outcome not by position."""
    recovered, opted_out, exhausted = None, None, None
    for e in entries:
        b, t = e["body"], e["type"]
        if t == "outcome" and b.get("recovered_paise", 0) > 0 and recovered is None:
            recovered = b["debt_id"]
        if t == "decision" and b.get("stop_reason") == "opt_out" and opted_out is None:
            opted_out = b["debt_id"]
        if t == "decision" and b.get("stop_reason") == "escalation_ladder_exhausted" \
                and exhausted is None:
            exhausted = b["debt_id"]
        if recovered and opted_out and exhausted:
            break
    return {k: v for k, v in
            {"recovered": recovered, "stopped_on_opt_out": opted_out,
             "ladder_exhausted": exhausted}.items() if v}


def main() -> int:
    out: dict = {
        "why_this_exists":
            "The full ledgers are ~27MB each and gitignored. They are reproducible: "
            "`python scripts/run_batch.py` regenerates them byte-for-byte from the seed. "
            "This file carries each ledger's chain-head hash so a regenerated copy can be "
            "proved identical, plus a full replay of three representative debts.",
        "ledgers": {},
    }
    missing = []
    for name, path in LEDGERS.items():
        if not path.exists():
            missing.append(name)
            continue
        led = AuditLedger(path, policy_version="(read-only)")
        entries = led.read()
        intact, broken_at = led.verify_chain()
        examples = pick_examples(entries)
        out["ledgers"][name] = {
            "source_file": str(path.relative_to(REPO)).replace("\\", "/"),
            "records": len(entries),
            "chain_intact": intact,
            "chain_broken_at": broken_at,
            "chain_head_hash": entries[-1]["record_hash"] if entries else None,
            "summary": led.summary(),
            "replayed_examples": {
                label: led.replay_debt(debt_id) for label, debt_id in examples.items()
            },
        }
    if missing:
        out["missing_ledgers"] = missing
        print(f"WARNING: not found (run scripts/run_batch.py first): {missing}",
              file=sys.stderr)

    dest = REPO / "results" / "phase3" / "ledger-extract.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")

    for name, block in out["ledgers"].items():
        print(f"{name}: {block['records']} records, chain_intact={block['chain_intact']}, "
              f"head={(block['chain_head_hash'] or '')[:16]}..., "
              f"examples={list(block['replayed_examples'])}")
    size = dest.stat().st_size / 1024
    print(f"\nwritten -> {dest.relative_to(REPO)} ({size:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
