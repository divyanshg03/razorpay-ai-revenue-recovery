"""The frozen metric, computed. Definition: docs/metric-definition.md, commit 8d14dbe.

## The headline

    NIR_per_customer = (r_C - r_B) - c_C
    NIR_total        = NIR_per_customer x n_C

where r_X is mean recovered rupees per customer in arm X and c_C is mean contact cost per
customer in arm C. Arms A and B incur no contact cost by construction.

**The primary comparison is C vs B — the engine against Razorpay's own ladder.** C vs A is
reported as context and never as the headline. Beating do-nothing proves nothing; every
recovery vendor beats doing nothing. Reporting only C vs A would be the defensible-looking
version of cheating.

## Why a bootstrap rather than a t-interval

Per-customer recovery is heavily zero-inflated and right-skewed — most customers recover
nothing, a few recover their full debt. A normal approximation is quietly wrong in the tails,
which is exactly where a confidence interval earns its keep. Percentile bootstrap, 10,000
resamples, stratified by arm, seeded so the interval is reproducible.

## What "recovered" means

Per the frozen definition: rupees, not a binary; capped at the amount due; floored at zero;
any payment route counts. That last rule is what makes the number honest — because arms are
randomised, the difference between them IS the causal effect, so every rupee a treatment
customer pays belongs in the comparison regardless of how it arrived. Click-attribution would
credit the engine for payments that would have happened anyway.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass

from .baselines import ArmOutcome

BOOTSTRAP_SEED = 20260905
BOOTSTRAP_RESAMPLES = 10_000


@dataclass
class ArmSummary:
    n: int
    recovered_paise: int
    contact_cost_paise: int
    recovery_rate: float
    mean_recovered_paise: float
    mean_cost_paise: float

    @property
    def recovered_rupees(self) -> float:
        return round(self.recovered_paise / 100, 2)


def summarise(outcomes: list[ArmOutcome]) -> ArmSummary:
    n = len(outcomes)
    if n == 0:
        return ArmSummary(0, 0, 0, 0.0, 0.0, 0.0)
    rec = sum(o.recovered_paise for o in outcomes)
    cost = sum(o.contact_cost_paise for o in outcomes)
    return ArmSummary(
        n=n, recovered_paise=rec, contact_cost_paise=cost,
        recovery_rate=round(sum(o.recovered for o in outcomes) / n, 4),
        mean_recovered_paise=rec / n, mean_cost_paise=cost / n,
    )


def _net_per_customer(treat: list[ArmOutcome], control: list[ArmOutcome]) -> float:
    """(r_treat - r_control) - c_treat, in paise per treated customer."""
    if not treat or not control:
        return 0.0
    r_t = statistics.fmean(o.recovered_paise for o in treat)
    r_c = statistics.fmean(o.recovered_paise for o in control)
    c_t = statistics.fmean(o.contact_cost_paise for o in treat)
    return (r_t - r_c) - c_t


def bootstrap_ci(treat: list[ArmOutcome], control: list[ArmOutcome],
                 resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED,
                 alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap on net incremental recovery per treated customer.

    Stratified: each arm is resampled within itself, preserving the arm sizes. Seeded, so
    the reported interval is reproducible rather than a different number every run.
    """
    if not treat or not control:
        return (0.0, 0.0)
    rng = random.Random(seed)
    t_rec = [o.recovered_paise for o in treat]
    t_cost = [o.contact_cost_paise for o in treat]
    c_rec = [o.recovered_paise for o in control]
    nt, nc = len(t_rec), len(c_rec)

    stats = []
    for _ in range(resamples):
        ti = [rng.randrange(nt) for _ in range(nt)]
        ci = [rng.randrange(nc) for _ in range(nc)]
        r_t = sum(t_rec[i] for i in ti) / nt
        c_t = sum(t_cost[i] for i in ti) / nt
        r_c = sum(c_rec[i] for i in ci) / nc
        stats.append((r_t - r_c) - c_t)
    stats.sort()
    lo = stats[int((alpha / 2) * resamples)]
    hi = stats[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return (lo, hi)


@dataclass
class Comparison:
    label: str
    treat_arm: str
    control_arm: str
    net_incremental_per_customer_paise: float
    net_incremental_total_paise: float
    ci_low_per_customer_paise: float
    ci_high_per_customer_paise: float
    ci_low_total_paise: float
    ci_high_total_paise: float
    treat_recovery_rate: float
    control_recovery_rate: float
    lift_pp: float

    def as_dict(self) -> dict:
        r2 = lambda p: round(p / 100, 2)  # noqa: E731 - paise to rupees
        return {
            "label": self.label,
            "treat_arm": self.treat_arm,
            "control_arm": self.control_arm,
            "net_incremental_per_customer_rupees": r2(self.net_incremental_per_customer_paise),
            "net_incremental_total_rupees": r2(self.net_incremental_total_paise),
            "ci95_per_customer_rupees": [r2(self.ci_low_per_customer_paise),
                                         r2(self.ci_high_per_customer_paise)],
            "ci95_total_rupees": [r2(self.ci_low_total_paise), r2(self.ci_high_total_paise)],
            "excludes_zero": (self.ci_low_total_paise > 0) or (self.ci_high_total_paise < 0),
            "treat_recovery_rate": self.treat_recovery_rate,
            "control_recovery_rate": self.control_recovery_rate,
            "lift_pp": self.lift_pp,
        }


def compare(label: str, treat: list[ArmOutcome], control: list[ArmOutcome],
            treat_arm: str, control_arm: str, resamples: int = BOOTSTRAP_RESAMPLES,
            seed: int = BOOTSTRAP_SEED) -> Comparison:
    per_cust = _net_per_customer(treat, control)
    lo, hi = bootstrap_ci(treat, control, resamples=resamples, seed=seed)
    n_t = len(treat)
    ts, cs = summarise(treat), summarise(control)
    return Comparison(
        label=label, treat_arm=treat_arm, control_arm=control_arm,
        net_incremental_per_customer_paise=per_cust,
        net_incremental_total_paise=per_cust * n_t,
        ci_low_per_customer_paise=lo, ci_high_per_customer_paise=hi,
        ci_low_total_paise=lo * n_t, ci_high_total_paise=hi * n_t,
        treat_recovery_rate=ts.recovery_rate, control_recovery_rate=cs.recovery_rate,
        lift_pp=round((ts.recovery_rate - cs.recovery_rate) * 100, 2),
    )


def cost_per_incremental_rupee(treat: list[ArmOutcome], control: list[ArmOutcome]) -> float | None:
    """None when there is no incremental recovery to divide by — reported as null rather
    than as a divide-by-zero or a misleading zero."""
    if not treat or not control:
        return None
    r_t = statistics.fmean(o.recovered_paise for o in treat)
    r_c = statistics.fmean(o.recovered_paise for o in control)
    gross = (r_t - r_c) * len(treat)
    if gross <= 0:
        return None
    return round(sum(o.contact_cost_paise for o in treat) / gross, 4)


def by_cause(outcomes: list[ArmOutcome]) -> dict[str, dict]:
    """Pre-declared subgroup 1: diagnosed cause bucket."""
    groups: dict[str, list[ArmOutcome]] = {}
    for o in outcomes:
        groups.setdefault(o.actionability or "unknown", []).append(o)
    return {k: {"n": len(v), "recovery_rate": summarise(v).recovery_rate,
                "recovered_rupees": summarise(v).recovered_rupees}
            for k, v in sorted(groups.items())}


def by_debt_size_tercile(outcomes: list[ArmOutcome]) -> dict[str, dict]:
    """Pre-declared subgroup 2: debt-size tercile. Cut points come from the arm's own
    distribution and are reported, so the buckets are not a hidden choice."""
    if not outcomes:
        return {}
    amounts = sorted(o.amount_paise for o in outcomes)
    lo_cut = amounts[len(amounts) // 3]
    hi_cut = amounts[2 * len(amounts) // 3]
    buckets: dict[str, list[ArmOutcome]] = {"low": [], "mid": [], "high": []}
    for o in outcomes:
        key = "low" if o.amount_paise <= lo_cut else ("mid" if o.amount_paise <= hi_cut else "high")
        buckets[key].append(o)
    return {k: {"n": len(v), "recovery_rate": summarise(v).recovery_rate,
                "recovered_rupees": summarise(v).recovered_rupees,
                "cut_points_rupees": [round(lo_cut / 100, 2), round(hi_cut / 100, 2)]}
            for k, v in buckets.items()}


def failure_list(outcomes: list[ArmOutcome]) -> dict:
    """What the engine did NOT recover, by cause and by why it stopped.

    A recovery system that reports only its wins is a marketing asset, not an engineering
    one. This is required by the frozen definition, not optional colour.
    """
    lost = [o for o in outcomes if not o.recovered]
    by_cause_lost: dict[str, int] = {}
    by_stop: dict[str, int] = {}
    for o in lost:
        by_cause_lost[o.actionability or "unknown"] = by_cause_lost.get(o.actionability or "unknown", 0) + 1
        for s in set(o.stop_reasons):
            by_stop[s] = by_stop.get(s, 0) + 1
    return {
        "n_not_recovered": len(lost),
        "share_not_recovered": round(len(lost) / len(outcomes), 4) if outcomes else 0.0,
        "unrecovered_rupees": round(sum(o.amount_paise for o in lost) / 100, 2),
        "by_diagnosed_cause": dict(sorted(by_cause_lost.items(), key=lambda kv: -kv[1])),
        "by_stop_reason": dict(sorted(by_stop.items(), key=lambda kv: -kv[1])),
    }
