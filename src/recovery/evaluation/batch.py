"""The three-arm batch. Produces results/metrics.json and nothing else.

Executes the definition frozen in docs/metric-definition.md at commit 8d14dbe. Nothing in
this file decides anything about the experiment — the seed, split, window, unit, cost model
and baselines were all fixed before any engine code existed.

## Why each arm gets its own cohort object

The simulator's hidden per-customer state (payday, whether they have paid) is derived by
hashing the seed with the customer ref, so customer `cust_000123` has the SAME payday in
every instance built from the same seed. Each arm therefore runs against an identical world
and differs only in policy — which is what makes the difference between arms attributable to
the policy rather than to luck. Separate instances just stop one arm's "paid" flags leaking
into another's.

## The batch refuses to write on a guardrail violation

If any invariant replayed from the arm-C ledger is non-zero, `run()` raises instead of
writing metrics.json. A number produced by a run that broke its own stopping rules is worse
than no number, because it would be reported as though the rules held.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess

from ..cohort.simulator import SimulatedCohort
from ..engine.policy import Policy
from ..ledger.audit import AuditLedger
from ..models import Arm
from .assignment import SEED, assign
from .baselines import ArmOutcome, run_do_nothing, run_incumbent_ladder
from .engine_arm import run_engine
from .invariants import check_ledger
from .metrics import (BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, by_cause, by_debt_size_tercile,
                      compare, cost_per_incremental_rupee, failure_list, summarise)

#: All frozen. Changing any of these requires an amendment in docs/metric-definition.md.
N_CUSTOMERS = 5_000
WINDOW_DAYS = 21
SECONDARY_WINDOW_DAYS = 14
START = dt.date(2026, 9, 3)

#: The commit that froze the metric. Asserted to be an ancestor of HEAD at generation time —
#: that ancestry IS the proof the definition predates the result.
METRIC_DEFINITION_COMMIT = "8d14dbe"


class GuardrailViolation(RuntimeError):
    pass


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              cwd=pathlib.Path(__file__).resolve().parents[3]).stdout.strip()
    except OSError:
        return ""


def _metric_definition_is_ancestor() -> bool:
    """The freeze is only meaningful if the definition commit precedes this result."""
    repo = pathlib.Path(__file__).resolve().parents[3]
    try:
        r = subprocess.run(["git", "merge-base", "--is-ancestor",
                            METRIC_DEFINITION_COMMIT, "HEAD"],
                           capture_output=True, cwd=repo)
        return r.returncode == 0
    except OSError:
        return False


def _partition(cohort: SimulatedCohort, refs: set[str]):
    return [d for d in cohort.debts() if d.customer_ref in refs]


def run_arms(seed: int, n: int, start: dt.date, window: int, policy: Policy,
             ledger_path: pathlib.Path, shifted: bool = False
             ) -> tuple[dict[str, list[ArmOutcome]], AuditLedger, dict]:
    """Run all three arms over one cohort partitioned by the frozen assignment."""
    base = SimulatedCohort(seed=seed, n_customers=n, start=start, shifted=shifted)
    assignment = assign(base.customers(), seed=SEED)

    # Arm A - do nothing.
    ca = SimulatedCohort(seed=seed, n_customers=n, start=start, shifted=shifted)
    out_a = run_do_nothing(ca, _partition(ca, assignment.refs_in(Arm.DO_NOTHING)), start, window)

    # Arm B - Razorpay's ladder, reimplemented.
    cb = SimulatedCohort(seed=seed, n_customers=n, start=start, shifted=shifted)
    out_b = run_incumbent_ladder(cb, _partition(cb, assignment.refs_in(Arm.INCUMBENT_LADDER)),
                                 start, window)

    # Arm C - the engine. Only this arm writes a ledger; it is the only one that decides.
    cc = SimulatedCohort(seed=seed, n_customers=n, start=start, shifted=shifted)
    engine_refs = assignment.refs_in(Arm.ENGINE)
    # fresh=True: a batch run is a new experiment, not a continuation of the last one. See
    # AuditLedger.__init__ - appending one run onto another interleaves two chains over the
    # same debt ids and manufactures invariant violations out of nothing.
    ledger = AuditLedger(ledger_path, policy.version, fresh=True)
    out_c = run_engine(cc, _partition(cc, engine_refs),
                       [c for c in cc.customers() if c.ref in engine_refs],
                       start, window, policy, ledger)

    return ({"A": out_a, "B": out_b, "C": out_c}, ledger,
            {"counts": assignment.counts(), "shares": assignment.shares(),
             "excluded": len(assignment.excluded),
             "excluded_reasons": _count(assignment.excluded.values()),
             "provenance": base.provenance})


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _cohort_block(arms, assignment_info, ledger, policy, window, seed, shifted):
    inv = check_ledger(ledger, policy.max_contacts_per_debt_7d)
    a, b, c = arms["A"], arms["B"], arms["C"]
    primary = compare("engine vs incumbent ladder (PRIMARY)", c, b, "C", "B")
    secondary = compare("engine vs do-nothing (context only)", c, a, "C", "A")
    return {
        "seed": seed, "shifted_parameters": shifted, "window_days": window,
        "assignment": assignment_info,
        "arms": {k: vars(summarise(v)) for k, v in arms.items()},
        "primary": primary.as_dict(),
        "secondary_vs_do_nothing": secondary.as_dict(),
        "cost_per_incremental_rupee": cost_per_incremental_rupee(c, b),
        "guardrail_invariants": inv,
        "guardrails_all_zero": all(v == 0 for v in inv.values()),
        "ledger": ledger.summary(),
        "subgroup_by_diagnosed_cause": by_cause(c),
        "subgroup_by_debt_size_tercile": by_debt_size_tercile(c),
        "failure_list": failure_list(c),
    }


def run(out_path: str | pathlib.Path = "results/metrics.json",
        ledger_dir: str | pathlib.Path = "results/phase3",
        resamples: int = BOOTSTRAP_RESAMPLES) -> dict:
    policy = Policy()
    ledger_dir = pathlib.Path(ledger_dir)
    ledger_dir.mkdir(parents=True, exist_ok=True)

    primary_arms, primary_ledger, primary_assign = run_arms(
        SEED, N_CUSTOMERS, START, WINDOW_DAYS, policy, ledger_dir / "arm-c-audit.jsonl")
    block = _cohort_block(primary_arms, primary_assign, primary_ledger, policy,
                          WINDOW_DAYS, SEED, shifted=False)

    if not block["guardrails_all_zero"]:
        raise GuardrailViolation(
            f"refusing to write metrics: guardrail violations {block['guardrail_invariants']}. "
            "A number from a run that broke its own stopping rules is worse than no number.")

    # Secondary readout at 14 days, pre-declared in the frozen definition.
    short_arms, short_ledger, short_assign = run_arms(
        SEED, N_CUSTOMERS, START, SECONDARY_WINDOW_DAYS, policy,
        ledger_dir / "arm-c-audit-14d.jsonl")
    short = _cohort_block(short_arms, short_assign, short_ledger, policy,
                          SECONDARY_WINDOW_DAYS, SEED, shifted=False)

    # Shifted-parameter cohort: a policy that merely inverted the generator collapses here.
    shifted_arms, shifted_ledger, shifted_assign = run_arms(
        SEED + 1, N_CUSTOMERS, START, WINDOW_DAYS, policy,
        ledger_dir / "arm-c-audit-shifted.jsonl", shifted=True)
    shifted = _cohort_block(shifted_arms, shifted_assign, shifted_ledger, policy,
                            WINDOW_DAYS, SEED + 1, shifted=True)

    report = {
        "metric_definition": {
            "document": "docs/metric-definition.md",
            "frozen_at_commit": METRIC_DEFINITION_COMMIT,
            "is_ancestor_of_head": _metric_definition_is_ancestor(),
            "note": "The freeze is only meaningful if that commit precedes this result. "
                    "Ancestry is checked, not asserted.",
        },
        "generated_by": "scripts/run_batch.py -> recovery.evaluation.batch.run()",
        "head_commit": _git("rev-parse", "--short", "HEAD"),
        "policy_version": policy.version,
        "n_customers": N_CUSTOMERS,
        "bootstrap": {"resamples": resamples, "seed": BOOTSTRAP_SEED,
                      "method": "percentile, stratified by arm"},
        "headline": {
            "comparison": "engine (C) vs Razorpay's T+0..T+3 ladder (B)",
            "net_incremental_rupees": block["primary"]["net_incremental_total_rupees"],
            "ci95_rupees": block["primary"]["ci95_total_rupees"],
            "per_customer_rupees": block["primary"]["net_incremental_per_customer_rupees"],
            "note": "Net of contact cost. C vs A is context only and is NOT the headline.",
        },
        "primary_cohort_21d": block,
        "secondary_cohort_14d": short,
        "shifted_parameter_cohort": shifted,
        "limitations": [
            "Outcomes are simulated: Subscriptions is gated on this Razorpay test account "
            "(docs/phase-0-findings.md), so the failed-charge cohort is generated, not observed. "
            "Randomisation removes selection bias WITHIN the simulation; it cannot validate "
            "the simulation.",
            "Significance is cheap here - N is a free parameter on a simulator. The CIs "
            "express precision inside the simulation only, not evidence about the real world.",
            "We wrote the response model. Its parameters trace to cited public figures "
            "(src/recovery/cohort/PARAMETERS.md), the engine never sees them, and evaluation "
            "runs on shifted parameters - that reduces the problem, it does not eliminate it.",
            "Contact costs are published list prices, not invoices, taken at the expensive "
            "end of each range. The WhatsApp rate could not be read at source.",
            "MDR is excluded: it scales incremental recovery by (1 - mdr) and cannot change "
            "its sign or the ranking of arms.",
            "Customer annoyance is unpriced, deliberately - no defensible number could be "
            "sourced, so it is handled as hard constraints rather than a tradeable term.",
            "The incumbent baseline is reimplemented from Razorpay's documentation, not "
            "observed: gate 0.3 was voided when Subscriptions turned out to be gated.",
            "In-batch composition uses deterministic templates. The simulator has no notion "
            "of wording, so the LLM cannot affect this measurement; it is exercised live in "
            "the test suite and results/phase2/.",
        ],
    }

    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
