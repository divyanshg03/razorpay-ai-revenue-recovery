"""The same cohort, measured INSIDE NPCI's attempt cap. Writes results/npci-cap-rerun.json.

    python scripts/npci_cap_rerun.py

## Why this exists

`results/metrics.json` measures a six-attempt schedule. NPCI's UPI circular **OC-215-A**
allows **one attempt and three retries** per mandate per execution cycle, so the published
number is measured outside the rule it would have to ship under. That is a real objection and
the honest answer is a measurement, not a paragraph.

This is a SEPARATE artifact rather than a replacement, because the frozen metric definition
(`docs/metric-definition.md`, frozen at `8d14dbe` before any engine code existed) fixes the
configuration the headline is computed under. Quietly re-running the headline under different
rules and publishing the result as the headline is precisely the move the freeze exists to
prevent. So the frozen number stays where it is, and this sits beside it.

## What changes, and why each change is right

1. **Three retries, not six.** The failed charge IS the attempt under OC-215-A, so only three
   retries may follow it. They are spread across the same declared horizon - the spreading is
   the finding, and it is being tested under a tighter budget, not abandoned.
2. **No retry on day 0.** The derived schedule opens with a same-day retry. Against a debit
   that failed that morning for want of funds, a second debit hours later is an artifact of a
   simulator that does not condition funds on the failure, and no real ladder makes it.
3. **The incumbent retries T+1..T+3.** Razorpay's own documentation says Subscriptions retries
   "on the following day", excluding the date of the charge - four debits in total, exactly at
   NPCI's cap. The shipped arm B also re-debits on T+0, giving it five.
4. **Each debt's own 21-day window.** `metric-definition.md` §3 says "21 days from the initial
   mandate failure, per debt". The shipped runners anchor every arm on the cohort start date
   instead, and failures land on three different days, so the baselines can retry a debt that
   has not failed yet. Fixed here; the fix in the batch itself is a separate job.
5. **Self-cure is counted identically in every arm**, every day, before any retry - as
   `engine_arm.py` already does it. The shipped controls never check it and the shipped
   incumbent checks it only after T+3.

## What is still NOT modelled, and would move this number

- **Peak hours.** OC-215-A also confines retries to non-peak windows (outside 10:00-13:00 and
  17:00-21:30). Every action here is timed at 10:00, which is inside the morning peak. The
  simulator has no time-of-day effect, so moving them changes nothing in these figures - but
  it is a scheduling constraint a production run would have to satisfy, not a free pass.
- **Pre-debit notification.** The RBI e-mandate framework requires notice ahead of a debit.
  Not modelled. Retries a week apart leave room for one; daily retries barely do.
- **eMandate confirmation lag.** Results can take days to come back. Charges settle
  synchronously here.
- Every limitation of the simulator itself, which `cohort/PARAMETERS.md` lists and this run
  inherits unchanged. The intervals below describe sampling noise in a model we wrote.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import random
import subprocess
import sys
import tempfile
from dataclasses import replace

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from recovery.cohort.simulator import SimulatedCohort                # noqa: E402
from recovery.diagnosis.taxonomy import diagnose                     # noqa: E402
from recovery.engine.policy import Policy                            # noqa: E402
from recovery.evaluation.assignment import SEED, assign              # noqa: E402
from recovery.evaluation.engine_arm import run_engine                # noqa: E402
from recovery.ledger.audit import AuditLedger                        # noqa: E402
from recovery.models import Actionability, Arm                       # noqa: E402

OUT = REPO / "results" / "npci-cap-rerun.json"
START = dt.date(2026, 9, 3)
N_CUSTOMERS = 5_000
WINDOW_DAYS = 21
RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260920

#: NPCI OC-215-A: one attempt plus three retries per mandate per execution cycle.
RETRIES_ALLOWED = 3
#: Razorpay Subscriptions, from their docs: T+1, T+2, T+3, "excluding the date of the charge".
INCUMBENT_DAYS = (1, 2, 3)

RETRYABLE = (Actionability.RETRY_LATER, Actionability.NEEDS_FUNDS)


def capped_schedule(horizon: int = WINDOW_DAYS, retries: int = RETRIES_ALLOWED) -> tuple[int, ...]:
    """The engine's own spreading rule, applied to the retries that follow the failed charge.

    Not a hand-picked list: the same "cover the horizon" logic as `retry_schedule`, with the
    difference that day 0 is the charge rather than the first retry.
    """
    return tuple(round(horizon * k / retries) for k in range(1, retries + 1))


def calendar_arm(refs: set[str], days: tuple[int, ...], respect_diagnosis: bool) -> dict[str, int]:
    """Retries only, on `days` after each debt's OWN failure, plus self-cure every day."""
    cohort = SimulatedCohort(seed=SEED, n_customers=N_CUSTOMERS, start=START)
    debts = sorted((d for d in cohort.debts() if d.customer_ref in refs), key=lambda d: d.debt_id)
    recovered: dict[str, int] = {}
    for debt in debts:
        action = diagnose(debt.failure).actionability
        may_retry = not (respect_diagnosis and action not in RETRYABLE)
        recovered[debt.debt_id] = 0
        for offset in range(WINDOW_DAYS + 1):
            day = debt.failed_at.date() + dt.timedelta(days=offset)
            if cohort.organic_settle(debt, day) or (
                    may_retry and offset in days and cohort.attempt_charge(debt, day, action)):
                recovered[debt.debt_id] = debt.amount_paise
                break
    return recovered


def engine_arm(refs: set[str], policy: Policy) -> tuple[dict[str, int], dict[str, int], int]:
    """Arm C under the capped policy, scored on each debt's own window."""
    cohort = SimulatedCohort(seed=SEED, n_customers=N_CUSTOMERS, start=START)
    debts = [d for d in cohort.debts() if d.customer_ref in refs]
    customers = [c for c in cohort.customers() if c.ref in refs]
    ledger = AuditLedger(pathlib.Path(tempfile.mkdtemp()) / "npci.jsonl", policy.version,
                         fresh=True)
    # +2 days so a debt that failed on the cohort's third day still reaches its own day 21.
    outcomes = run_engine(cohort, debts, customers, START, WINDOW_DAYS + 2, policy, ledger)
    failed_on = {d.debt_id: (d.failed_at.date() - START).days for d in debts}
    recovered, cost = {}, {}
    for o in outcomes:
        in_window = o.recovered and 0 <= o.settled_on_day - failed_on[o.debt_id] <= WINDOW_DAYS
        recovered[o.debt_id] = o.recovered_paise if in_window else 0
        cost[o.debt_id] = o.contact_cost_paise
    return recovered, cost, len(outcomes)


def _mean(values: list[int]) -> float:
    return sum(values) / len(values)


def compare(treat: list[int], control: list[int], paired: bool, scale: int,
            rng: random.Random) -> dict:
    """Percentile bootstrap. Paired when the control ran on the treated arm's own customers."""
    if paired:
        diffs = [t - c for t, c in zip(treat, control)]
        point = _mean(diffs)
        draws = sorted(_mean(rng.choices(diffs, k=len(diffs))) for _ in range(RESAMPLES))
    else:
        point = _mean(treat) - _mean(control)
        draws = sorted(_mean(rng.choices(treat, k=len(treat)))
                       - _mean(rng.choices(control, k=len(control))) for _ in range(RESAMPLES))
    lo, hi = draws[int(0.025 * RESAMPLES)], draws[int(0.975 * RESAMPLES) - 1]
    return {
        "paired": paired,
        "net_incremental_per_customer_rupees": round(point / 100, 2),
        "net_incremental_total_rupees": round(point * scale / 100, 2),
        "ci95_total_rupees": [round(lo * scale / 100, 2), round(hi * scale / 100, 2)],
        "excludes_zero": (lo > 0) == (hi > 0),
    }


def main() -> int:
    policy = replace(Policy(), retry_days=capped_schedule(),
                     max_retries_per_debt=RETRIES_ALLOWED)
    schedule = capped_schedule()
    assignment = assign(SimulatedCohort(seed=SEED, n_customers=N_CUSTOMERS,
                                        start=START).customers())
    refs_c = assignment.refs_in(Arm.ENGINE)
    refs_b = assignment.refs_in(Arm.INCUMBENT_LADDER)

    print(f"  schedule: charge on day 0, retries on {schedule};  incumbent {INCUMBENT_DAYS}")
    b = calendar_arm(refs_b, INCUMBENT_DAYS, respect_diagnosis=False)
    d = calendar_arm(refs_c, schedule, respect_diagnosis=False)
    d_diag = calendar_arm(refs_c, schedule, respect_diagnosis=True)
    c_recovered, c_cost, n_c = engine_arm(refs_c, policy)

    ids = sorted(c_recovered)
    c_net = [c_recovered[i] - c_cost[i] for i in ids]
    rng = random.Random(BOOTSTRAP_SEED)

    def arm_block(recovered: dict[str, int], cost: dict[str, int] | None = None) -> dict:
        n = len(recovered)
        got = sum(1 for v in recovered.values() if v)
        return {
            "n": n,
            "recovery_rate": round(got / n, 4),
            "recovered_rupees": round(sum(recovered.values()) / 100, 2),
            "contact_cost_rupees": round(sum((cost or {}).values()) / 100, 2),
        }

    artifact = {
        "what_this_is": (
            "The seeded cohort measured inside NPCI OC-215-A: one attempt plus three retries "
            "per mandate execution. A companion to results/metrics.json, NOT a replacement "
            "for it - the headline stays under the frozen metric definition, which fixes the "
            "configuration it is computed in."),
        "generated_by": "scripts/npci_cap_rerun.py",
        "head_commit": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                                      capture_output=True, text=True).stdout.strip(),
        "working_tree_dirty": bool(subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                                                  capture_output=True, text=True).stdout.strip()),
        "seed": SEED,
        "n_customers": N_CUSTOMERS,
        "window_days": WINDOW_DAYS,
        "window_anchored_on": "each debt's own failure date",
        "rule": "NPCI UPI circular OC-215-A: 1 attempt + 3 retries per mandate per cycle",
        "schedule": {
            "engine_and_controls": list(schedule),
            "incumbent": list(INCUMBENT_DAYS),
            "note": ("Day 0 is the charge that failed, in every arm. Razorpay's documented "
                     "ladder is T+1..T+3, excluding the date of the charge."),
        },
        "policy_overrides": {"retry_days": list(schedule),
                             "max_retries_per_debt": RETRIES_ALLOWED},
        "arms": {
            "B_incumbent": arm_block(b),
            "C_engine": arm_block(c_recovered, c_cost),
            "D_calendar_blind": arm_block(d),
            "D_diagnosed_calendar": arm_block(d_diag),
        },
        "comparisons": {
            "headline__C_vs_B": {
                "label": "engine vs Razorpay's T+1..T+3 ladder, both inside the cap",
                **compare(c_net, list(b.values()), paired=False, scale=n_c, rng=rng)},
            "spacing_is_worth__D_diagnosed_vs_B": {
                "label": "the calendar alone vs the incumbent, same three retries",
                **compare([d_diag[i] for i in ids], list(b.values()), paired=False,
                          scale=n_c, rng=rng)},
            "decisioning_is_worth__C_vs_D_diagnosed": {
                "label": "the engine vs a diagnosis-respecting calendar on its own customers",
                **compare(c_net, [d_diag[i] for i in ids], paired=True, scale=n_c, rng=rng)},
            "decisioning_is_worth__C_vs_D": {
                "label": "the engine vs a calendar blind to the cause (flatters the control: "
                         "the simulator lets a silent retry fix causes that need the customer)",
                **compare(c_net, [d[i] for i in ids], paired=True, scale=n_c, rng=rng)},
        },
        "bootstrap": {"resamples": RESAMPLES, "seed": BOOTSTRAP_SEED,
                      "method": "percentile; paired where the control runs on arm C's own "
                                "customers, independent where it does not"},
        "not_modelled": [
            "Peak hours. OC-215-A confines retries to non-peak windows; every action here is "
            "timed at 10:00, inside the 10:00-13:00 peak. The simulator has no time-of-day "
            "effect, so this changes none of the figures above and all of the deployment.",
            "Pre-debit notification under the RBI e-mandate framework.",
            "eMandate confirmation lag: charges settle synchronously here.",
            "Every simulator limitation in src/recovery/cohort/PARAMETERS.md, inherited "
            "unchanged. These intervals are sampling noise in a model we wrote.",
        ],
    }
    OUT.write_text(json.dumps(artifact, indent=1, sort_keys=False) + "\n", encoding="utf-8")

    for name, arm in artifact["arms"].items():
        print(f"  {name:<24} {arm['recovery_rate']:>7.2%}   n={arm['n']}")
    for name, cmp_ in artifact["comparisons"].items():
        print(f"  {name:<40} Rs {cmp_['net_incremental_total_rupees']:>12,.0f}   "
              f"CI [{cmp_['ci95_total_rupees'][0]:,.0f}, {cmp_['ci95_total_rupees'][1]:,.0f}]"
              f"{'' if cmp_['excludes_zero'] else '   <- crosses zero'}")
    print(f"  written: {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
