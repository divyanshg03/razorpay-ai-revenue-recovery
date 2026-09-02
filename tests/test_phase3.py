"""Phase 3 exit criteria.

The tests that matter here are about the INTEGRITY of the number, not its size:

  - the arm split is reproducible and order-independent
  - excluded customers are removed BEFORE randomisation and never appear in an arm
  - the bootstrap interval is seeded, so the reported CI is not a different number each run
  - the batch REFUSES to write metrics.json if a guardrail fired
  - the committed artifact's own claims hold: ancestry, zero violations, no banned term

A metric you cannot regenerate is a claim, not a measurement.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess

import pytest

from recovery.cohort.simulator import SimulatedCohort
from recovery.engine.policy import Policy
from recovery.evaluation import batch as batch_mod
from recovery.evaluation.assignment import SEED, arm_for, assign, is_excluded
from recovery.evaluation.baselines import ArmOutcome
from recovery.evaluation.batch import (METRIC_DEFINITION_COMMIT, GuardrailViolation,
                                       run_arms)
from recovery.evaluation.metrics import (bootstrap_ci, by_debt_size_tercile, compare,
                                         cost_per_incremental_rupee, failure_list, summarise)
from recovery.models import Arm, Customer

REPO = pathlib.Path(__file__).resolve().parents[1]
METRICS = REPO / "results" / "metrics.json"
START = dt.date(2026, 9, 3)


def cohort(n=1200, seed=SEED, shifted=False):
    return SimulatedCohort(seed=seed, n_customers=n, start=START, shifted=shifted)


# ---------------------------------------------------------------------------------------
# 3.1 assignment
# ---------------------------------------------------------------------------------------

def test_split_lands_near_the_frozen_20_20_60():
    a = assign(cohort(5000).customers())
    s = a.shares()
    assert abs(s["A"] - 0.20) < 0.02, s
    assert abs(s["B"] - 0.20) < 0.02, s
    assert abs(s["C"] - 0.60) < 0.03, s


def test_assignment_is_stable_and_order_independent():
    """Customer N's arm must not depend on how many customers came before them."""
    assert arm_for("cust_000123") is arm_for("cust_000123")
    small = assign(cohort(50).customers()).arms
    large = assign(cohort(5000).customers()).arms
    shared = set(small) & set(large)
    assert shared and all(small[r] is large[r] for r in shared)


def test_assignment_is_sticky_per_customer_not_per_debt():
    """All of a customer's debts inherit one arm - otherwise treating one debt contaminates
    the other, and the estimator breaks."""
    c = cohort(500)
    a = assign(c.customers())
    for d in c.debts():
        if d.customer_ref in a.arms:
            assert a.arms[d.customer_ref] is arm_for(d.customer_ref)


def test_exclusions_happen_before_randomisation_and_never_appear_in_an_arm():
    """No post-randomisation exclusion, ever. The customers an engine annoys into opting out
    must stay in the denominator; only PRE-EXISTING conditions remove someone."""
    c = cohort(3000)
    a = assign(c.customers())
    by_ref = {x.ref: x for x in c.customers()}
    assert a.excluded, "the cohort should contain some pre-excluded customers"
    for ref in a.excluded:
        assert ref not in a.arms
        assert is_excluded(by_ref[ref]) is not None
    for ref in a.arms:
        assert is_excluded(by_ref[ref]) is None


@pytest.mark.parametrize("field,reason", [
    ("opted_out", "pre_existing_opt_out"),
    ("disputed", "pre_existing_dispute"),
    ("bereaved_or_hardship", "pre_existing_hardship"),
])
def test_each_exclusion_reason_is_detected(field, reason):
    assert is_excluded(Customer("c", **{field: True})) == reason


def test_unreachable_customer_is_excluded():
    assert is_excluded(Customer("c", has_whatsapp=False, has_sms=False)) == "no_reachable_channel"


# ---------------------------------------------------------------------------------------
# 3.3 the metric
# ---------------------------------------------------------------------------------------

def _outcomes(n, recovered, cost=0):
    return [ArmOutcome(f"d{i}", f"c{i}", 50_000, recovered_paise=recovered,
                       contact_cost_paise=cost) for i in range(n)]


def test_net_incremental_subtracts_the_control_and_the_cost():
    treat = _outcomes(100, 40_000, cost=1_000)
    control = _outcomes(100, 10_000)
    c = compare("t", treat, control, "C", "B")
    # (40000 - 10000) - 1000 = 29000 paise per customer
    assert c.net_incremental_per_customer_paise == pytest.approx(29_000)
    assert c.net_incremental_total_paise == pytest.approx(29_000 * 100)


def _varied(n, hi, share_recovering, cost=0):
    """Zero-inflated and skewed, like the real thing: most recover nothing, some recover in
    full. A fixture where every customer is identical has no variance, so every resample
    returns the same statistic and the interval collapses to a point whatever the seed."""
    return [ArmOutcome(f"d{i}", f"c{i}", hi,
                       recovered_paise=(hi if i % share_recovering == 0 else 0),
                       contact_cost_paise=cost) for i in range(n)]


def test_bootstrap_interval_is_reproducible_with_the_same_seed():
    """A CI that differs every run is decoration, not a measurement."""
    treat, control = _varied(300, 50_000, 2, cost=500), _varied(300, 50_000, 5)
    a = bootstrap_ci(treat, control, resamples=400, seed=1)
    assert a == bootstrap_ci(treat, control, resamples=400, seed=1)
    assert a[0] < a[1], "a real interval, not a degenerate point"
    # A different seed must move it, or the seeding is not actually driving the resampling.
    assert bootstrap_ci(treat, control, resamples=400, seed=2) != a


def test_bootstrap_interval_brackets_the_point_estimate():
    treat, control = _varied(300, 50_000, 2, cost=500), _varied(300, 50_000, 5)
    c = compare("t", treat, control, "C", "B")
    lo, hi = bootstrap_ci(treat, control, resamples=800, seed=1)
    assert lo <= c.net_incremental_per_customer_paise <= hi


def test_cost_per_incremental_rupee_is_null_not_zero_when_there_is_no_lift():
    """Dividing by zero incremental recovery must not produce a flattering 0.0."""
    same = _outcomes(50, 10_000, cost=100)
    assert cost_per_incremental_rupee(same, _outcomes(50, 10_000)) is None


def test_failure_list_reports_what_was_not_recovered():
    lost = [ArmOutcome("d1", "c1", 50_000, actionability="needs_funds",
                       stop_reasons=["opt_out"])]
    won = [ArmOutcome("d2", "c2", 50_000, recovered_paise=50_000)]
    f = failure_list(lost + won)
    assert f["n_not_recovered"] == 1 and f["by_stop_reason"]["opt_out"] == 1
    assert f["by_diagnosed_cause"]["needs_funds"] == 1


def test_debt_size_terciles_report_their_cut_points():
    """The buckets must not be a hidden choice."""
    out = [ArmOutcome(f"d{i}", f"c{i}", (i + 1) * 10_000) for i in range(30)]
    t = by_debt_size_tercile(out)
    assert set(t) == {"low", "mid", "high"}
    assert t["low"]["cut_points_rupees"] == t["high"]["cut_points_rupees"]
    assert sum(v["n"] for v in t.values()) == 30


# ---------------------------------------------------------------------------------------
# 3.2 the batch
# ---------------------------------------------------------------------------------------

def test_arms_are_disjoint_and_cover_the_included_population(tmp_path):
    arms, _, info = run_arms(SEED, 900, START, 21, Policy(), tmp_path / "l.jsonl")
    refs = {k: {o.customer_ref for o in v} for k, v in arms.items()}
    assert not (refs["A"] & refs["B"]) and not (refs["B"] & refs["C"]) and not (refs["A"] & refs["C"])
    assert sum(len(v) for v in refs.values()) == sum(info["counts"].values())


def test_the_same_customer_gets_the_same_payday_in_every_arm():
    """This is what makes the arms comparable: identical world, different policy only."""
    a = cohort(400)
    b = cohort(400)
    assert [a._payday(c.ref) for c in a.customers()] == [b._payday(c.ref) for c in b.customers()]


def test_batch_run_is_deterministic(tmp_path):
    """Same seed, same result - twice, into different ledgers."""
    p = Policy()
    r1, _, _ = run_arms(SEED, 700, START, 21, p, tmp_path / "a.jsonl")
    r2, _, _ = run_arms(SEED, 700, START, 21, p, tmp_path / "b.jsonl")
    for arm in ("A", "B", "C"):
        assert [(o.debt_id, o.recovered_paise, o.contact_cost_paise) for o in r1[arm]] == \
               [(o.debt_id, o.recovered_paise, o.contact_cost_paise) for o in r2[arm]]


def test_batch_refuses_to_write_when_a_guardrail_fired(tmp_path, monkeypatch):
    """A number from a run that broke its own stopping rules is worse than no number."""
    monkeypatch.setattr(batch_mod, "N_CUSTOMERS", 400)
    monkeypatch.setattr(batch_mod, "check_ledger",
                        lambda *a, **k: {"contact_outside_window": 3, "contact_after_payment": 0})
    out = tmp_path / "metrics.json"
    with pytest.raises(GuardrailViolation):
        batch_mod.run(out_path=out, ledger_dir=tmp_path, resamples=50)
    assert not out.exists(), "metrics.json must NOT be written on a violation"


# ---------------------------------------------------------------------------------------
# 3.6 the committed artifact must back its own claims
# ---------------------------------------------------------------------------------------

@pytest.mark.skipif(not METRICS.exists(), reason="results/metrics.json not generated yet")
class TestCommittedArtifact:
    @pytest.fixture(scope="class")
    def m(self):
        return json.loads(METRICS.read_text(encoding="utf-8"))

    def test_metric_definition_commit_precedes_the_result(self, m):
        """The freeze is only meaningful if the definition predates the number."""
        assert m["metric_definition"]["is_ancestor_of_head"] is True
        r = subprocess.run(["git", "merge-base", "--is-ancestor",
                            METRIC_DEFINITION_COMMIT, "HEAD"], capture_output=True, cwd=REPO)
        assert r.returncode == 0

    def test_every_guardrail_count_is_zero_in_every_cohort(self, m):
        for key in ("primary_cohort_21d", "secondary_cohort_14d", "shifted_parameter_cohort"):
            assert m[key]["guardrails_all_zero"], (key, m[key]["guardrail_invariants"])

    def test_headline_is_c_vs_b_not_c_vs_a(self, m):
        """Beating do-nothing proves nothing; the headline must be against the incumbent."""
        assert m["primary_cohort_21d"]["primary"]["control_arm"] == "B"
        assert "ladder" in m["headline"]["comparison"]

    def test_headline_carries_an_interval(self, m):
        ci = m["headline"]["ci95_rupees"]
        assert len(ci) == 2 and ci[0] < ci[1]

    def test_the_word_accuracy_appears_nowhere(self, m):
        assert "accuracy" not in json.dumps(m).lower()

    def test_limitations_are_stated_in_the_artifact(self, m):
        text = " ".join(m["limitations"]).lower()
        for must in ("simulated", "significance is cheap", "list prices", "mdr",
                     "annoyance", "reimplemented"):
            assert must in text, must

    def test_shifted_cohort_is_reported_whatever_it_says(self, m):
        """Reported even if it undercuts the headline - that is the point of running it."""
        assert "shifted_parameter_cohort" in m
        assert m["shifted_parameter_cohort"]["shifted_parameters"] is True

    def test_arm_shares_match_the_frozen_split(self, m):
        s = m["primary_cohort_21d"]["assignment"]["shares"]
        assert abs(s["A"] - 0.20) < 0.02 and abs(s["C"] - 0.60) < 0.03

    def test_cohort_declares_it_is_simulated(self, m):
        assert "SIMULATED" in m["primary_cohort_21d"]["assignment"]["provenance"]
