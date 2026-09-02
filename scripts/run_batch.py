"""Run the three-arm batch and write results/metrics.json.

    python scripts/run_batch.py

This is the ONLY thing that writes results/metrics.json, and the README's figures are
generated from that file — never typed. Per the frozen definition the batch is run ONCE for
the reported result; a re-run must be recorded in the Amendments section of
docs/metric-definition.md, because re-running until the number improves is fabrication.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from recovery.evaluation.batch import GuardrailViolation, run  # noqa: E402


def main() -> int:
    try:
        report = run(out_path=REPO / "results" / "metrics.json",
                     ledger_dir=REPO / "results" / "phase3")
    except GuardrailViolation as e:
        print(f"BATCH REFUSED TO WRITE: {e}", file=sys.stderr)
        return 1

    h = report["headline"]
    p = report["primary_cohort_21d"]
    print(json.dumps({
        "headline": h,
        "arms": {k: {"n": v["n"], "recovery_rate": v["recovery_rate"],
                     "recovered_rupees": round(v["recovered_paise"] / 100, 2)}
                 for k, v in p["arms"].items()},
        "assignment_shares": p["assignment"]["shares"],
        "guardrails_all_zero": p["guardrails_all_zero"],
        "metric_definition_is_ancestor": report["metric_definition"]["is_ancestor_of_head"],
        "shifted_cohort_headline":
            report["shifted_parameter_cohort"]["primary"]["net_incremental_total_rupees"],
    }, indent=2))
    print(f"\nwritten -> {REPO / 'results' / 'metrics.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
