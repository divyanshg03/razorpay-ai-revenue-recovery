"""Regenerate Phase 2's exit evidence. No figure in the docs is typed by hand.

Run: python scripts/phase2_evidence.py
Out: results/phase2/2-engine-and-gate.json

Requires Ollama on 127.0.0.1:11434 with llama3.1:8b for the two live-LLM sections; if it is
unreachable those sections record that fact rather than a fabricated result.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import socket
import sys
import tempfile
from collections import Counter

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from recovery.cohort.simulator import SimulatedCohort  # noqa: E402
from recovery.engine.policy import Policy  # noqa: E402
from recovery.evaluation.baselines import recovery_rate, run_incumbent_ladder  # noqa: E402
from recovery.evaluation.engine_arm import run_engine  # noqa: E402
from recovery.evaluation.invariants import check_ledger  # noqa: E402
from recovery.ledger.audit import AuditLedger  # noqa: E402
from recovery.llm import composer  # noqa: E402
from recovery.llm.copy_gate import Facts, check  # noqa: E402
from recovery.llm.parser import parse_reply  # noqa: E402
from recovery.models import Actionability  # noqa: E402

SEED, N, WINDOW = 20260905, 1500, 21
START = dt.date(2026, 9, 3)

#: ministral-3's actual output from the bake-off (results/phase0/0.8-model-bakeoff.json).
#: A genuine model-generated violation, not a hand-written strawman.
MINISTRAL_REAL_OUTPUT = (
    "Your Rs 999 autopay failed. Update payment details now to avoid service disruption. "
    "Limited-time bonus: 10% extra data on next recharge. Offer valid until 31st July."
)


def ollama_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1):
            return True
    except OSError:
        return False


def main() -> int:
    policy = Policy()
    ledger = AuditLedger(pathlib.Path(tempfile.mkdtemp()) / "audit.jsonl", policy.version)

    cohort_c = SimulatedCohort(seed=SEED, n_customers=N, start=START)
    out_c = run_engine(cohort_c, cohort_c.debts(), cohort_c.customers(), START, WINDOW,
                       policy, ledger)
    cohort_b = SimulatedCohort(seed=SEED, n_customers=N, start=START)
    out_b = run_incumbent_ladder(cohort_b, cohort_b.debts(), START, WINDOW)

    invariants = check_ledger(ledger, policy.max_contacts_per_debt_7d)
    summary = ledger.summary()
    inbound = Counter(e["body"]["intent"] for e in ledger.read() if e["type"] == "inbound")

    facts = Facts(amount_paise=49900, link="https://rzp.io/rzp/demo0001", merchant="Acme Fitness")
    fixture = check(MINISTRAL_REAL_OUTPUT, facts)

    if ollama_up():
        warm_s = composer.warm()
        live = composer.compose(facts, Actionability.NEEDS_FUNDS, use_llm=True)
        parsed = parse_reply("cant pay till salary comes on the 5th", dt.date(2026, 8, 31),
                             use_llm=True)
        live_compose = {
            "model": live.model, "warm_seconds": round(warm_s, 2),
            "gen_seconds": round(live.llm_seconds or 0, 2),
            "llm_wrote": live.llm_output, "sent": live.text, "source": live.source,
            "gate": live.gate.verdict.value, "gate_categories": live.gate.categories,
            "chars": len(live.text),
            "link_written_by_model": "http" in (live.llm_output or ""),
        }
        live_parse = {
            "reply": "cant pay till salary comes on the 5th", "intent": parsed.intent.value,
            "date_phrase_from_model": parsed.date_phrase,
            "date_resolved_by_code": str(parsed.promised_date), "source": parsed.source,
        }
    else:
        live_compose = {"skipped": "Ollama unreachable on 11434 - not fabricated"}
        live_parse = {"skipped": "Ollama unreachable on 11434 - not fabricated"}

    report = {
        "phase": 2,
        "policy_version": policy.version,
        "generated_by": "scripts/phase2_evidence.py; every number regenerates",
        "engine_run": {
            "seed": SEED, "n_customers": N, "window_days": WINDOW, "llm_in_batch": False,
            "note": "a batch composes with templates and parses with code overrides; the LLM "
                    "path is exercised live below and in tests/test_phase2.py",
        },
        "guardrail_invariants_replayed_from_ledger": invariants,
        "all_zero": all(v == 0 for v in invariants.values()),
        "ledger": summary,
        "inbound_replies_parsed": dict(inbound),
        "sanity_not_headline": {
            "arm_B_incumbent_recovery_rate": recovery_rate(out_b),
            "arm_C_engine_recovery_rate": recovery_rate(out_c),
            "arm_C_contacts": sum(o.contacts for o in out_c),
            "arm_C_retries": sum(o.retries for o in out_c),
            "arm_C_contact_cost_rupees": round(sum(o.contact_cost_paise for o in out_c) / 100, 2),
            "note": "same seed, no holdout, no confidence interval. Phase 3 measures this "
                    "against the definition frozen in docs/metric-definition.md.",
        },
        "copy_gate_on_real_ministral_output": {
            "text": MINISTRAL_REAL_OUTPUT, "verdict": fixture.verdict.value,
            "categories": fixture.categories, "reasons": fixture.reasons,
        },
        "live_llama31_composition": live_compose,
        "live_llama31_reply_parse": live_parse,
    }
    out = REPO / "results" / "phase2" / "2-engine-and-gate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "ledger"}, indent=2))
    print("ledger:", {k: v for k, v in summary.items() if k != "stop_reasons"})
    print("stop reasons:", summary["stop_reasons"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
