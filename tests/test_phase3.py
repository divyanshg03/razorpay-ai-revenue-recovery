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
import sys

import pytest

from recovery.cohort.simulator import SimulatedCohort
from recovery.engine.policy import Policy
from recovery.evaluation import batch as batch_mod
from recovery.evaluation.assignment import SEED, arm_for, assign, is_excluded
from recovery.evaluation.baselines import ArmOutcome
from recovery.evaluation.batch import (METRIC_DEFINITION_COMMIT, GuardrailViolation,
                                       run_arms)
from recovery.evaluation.invariants import check_ledger
from recovery.evaluation.metrics import (bootstrap_ci, by_debt_size_tercile, compare,
                                         cost_per_incremental_rupee, failure_list, summarise)
from recovery.ledger.audit import AuditLedger
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


def test_assignment_is_invariant_to_the_cohort_seed():
    """Two seeds exist and only one may move. This pins which.

    `run_arms(cohort_seed=...)` seeds the WORLD - who exists, when they are paid, what
    failed. The ASSIGNMENT seed is frozen at 20260905 and is read from the module constant,
    never from the caller, so the split cannot be varied by anyone shopping for a better
    number. The shifted cohort relies on exactly this: same customers, same arms, different
    world, so a difference between cohorts is attributable to the world and not to a
    reshuffle.

    Until 3 Sept 2026 the parameter was called `seed`, which read as though it drove both.
    A reviewer flagged it. The rename fixed the reading; this fixes the meaning.
    """
    a = cohort(400, seed=SEED)
    b = cohort(400, seed=SEED + 1, shifted=True)

    assert [c.ref for c in a.customers()] == [c.ref for c in b.customers()], \
        "customer refs must not depend on the cohort seed, or the arms stop being comparable"

    arms_a, arms_b = assign(a.customers()).arms, assign(b.customers()).arms
    shared = set(arms_a) & set(arms_b)
    assert shared
    assert all(arms_a[r] is arms_b[r] for r in shared), \
        "a customer changed arm when only the cohort seed moved"

    # ...and the world genuinely did change, or the check above proves nothing.
    assert [a._payday(c.ref) for c in a.customers()] != [b._payday(c.ref) for c in b.customers()]


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
    arms, _, info, _c = run_arms(SEED, 900, START, 21, Policy(), tmp_path / "l.jsonl")
    # A, B and C only. Arm D is a COUNTERFACTUAL on arm C's own customers, so it is
    # deliberately not disjoint from C and is not part of the randomised assignment.
    arms = {k: v for k, v in arms.items() if k in ("A", "B", "C")}   # D and D' are controls
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
    r1, _, _, _ = run_arms(SEED, 700, START, 21, p, tmp_path / "a.jsonl")
    r2, _, _, _ = run_arms(SEED, 700, START, 21, p, tmp_path / "b.jsonl")
    for arm in ("A", "B", "C"):
        assert [(o.debt_id, o.recovered_paise, o.contact_cost_paise) for o in r1[arm]] == \
               [(o.debt_id, o.recovered_paise, o.contact_cost_paise) for o in r2[arm]]


def test_a_rerun_does_not_append_onto_the_previous_runs_ledger(tmp_path):
    """Regression, 2 Sept 2026. `AuditLedger` opens its file in append mode - correctly, it is
    an append-only structure - and the batch reused a fixed path, so the second run's records
    landed on top of the first run's.

    That does not merely produce a large file. It produces two chains over the SAME debt ids
    interleaved in one stream, and the ledger-replay invariants read it as one history: the
    invariant asking "was this debt contacted after it settled" took the newer run's
    settlement timestamp and compared it against the OLDER run's contacts, which of course
    come later. 56 phantom `contact_after_payment` violations, and the batch refused to write
    metrics.json - the right call on corrupt input, but the input should never have been
    corrupt.

    Two runs into the same path must therefore be indistinguishable from one run into a clean
    path, and must report clean invariants.
    """
    path = tmp_path / "reused.jsonl"
    p = Policy()
    run_arms(SEED, 500, START, 21, p, path)
    first_size = path.stat().st_size
    _, ledger, _, _ = run_arms(SEED, 500, START, 21, p, path)

    assert path.stat().st_size == first_size, (
        "the second run appended to the first instead of starting a new chain")
    intact, broken_at = ledger.verify_chain()
    assert intact, f"chain broken at {broken_at}"
    violations = check_ledger(ledger, p.max_contacts_per_debt_7d)
    assert not any(violations.values()), violations


def test_ledger_still_appends_by_default(tmp_path):
    """The fix must not turn an append-only ledger into a truncating one. Only a caller that
    explicitly asks for a new chain gets one; everyone else appends, including anything
    reopening a ledger to read or extend it."""
    path = tmp_path / "l.jsonl"
    a = AuditLedger(path, "v1")
    a.record_outcome("d1", "c1", 100, note="first")
    n_after_first = len(a.read())

    b = AuditLedger(path, "v1")                      # default: continue the chain
    b.record_outcome("d2", "c2", 100, note="second")
    assert len(b.read()) == n_after_first + 1
    assert b.verify_chain()[0], "appending must extend the hash chain, not break it"

    c = AuditLedger(path, "v1", fresh=True)          # explicit: start over
    assert c.read() == []


def test_the_reported_resample_count_is_the_one_actually_used(tmp_path, monkeypatch):
    """`bootstrap.resamples` in the artifact must describe the run that produced it.

    It did not. `run()` accepted a `resamples` argument and reported it, but `_cohort_block`
    called `compare()` without it, so the bootstrap always used its own default. At the
    shipped default the two numbers coincide at 10,000, which is why the published interval
    was never wrong and why nothing caught it - the bug was invisible precisely at the
    setting anyone would check.

    A figure that carries its own provenance has to actually have that provenance, so this
    records what the bootstrap was really called with rather than trusting the JSON.
    """
    seen: list[int] = []
    real = batch_mod.compare

    def spy(*a, **kw):
        seen.append(kw.get("resamples"))
        return real(*a, **kw)

    monkeypatch.setattr(batch_mod, "N_CUSTOMERS", 400)
    monkeypatch.setattr(batch_mod, "compare", spy)
    report = batch_mod.run(out_path=tmp_path / "m.json", ledger_dir=tmp_path, resamples=37)

    assert seen, "compare() was never called"
    assert set(seen) == {37}, f"bootstrap ran with {set(seen)}, artifact claims 37"
    assert report["bootstrap"]["resamples"] == 37


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

    def test_no_debt_was_funded_during_the_window_and_never_attempted(self, m):
        """The one bucket of the failure list that is a DEFECT rather than a limitation.

        A customer whose salary landed inside the horizon we advertise, on a day we never
        tried, is money lost to arithmetic rather than to circumstance. That bucket stood at
        489 of 1,171 - 42% of everything unrecovered - because `retry_schedule` truncated a
        fixed-spacing list and stopped at day 15 of a 21-day window.

        It must stay at zero. If it climbs, the schedule has drifted off the horizon it
        advertises again, and the headline is overstating what the engine actually covers.
        """
        for key in ("primary_cohort_21d", "shifted_parameter_cohort"):
            standing = m[key]["failure_list"]["standing"]["counts"]
            assert standing["funded_but_never_attempted_DEFECT"] == 0, (key, standing)

    def test_the_residual_is_decomposed_not_just_counted(self, m):
        """"33% not recovered" is four different claims and they are not interchangeable:
        guardrail stops are correct behaviour, never-funded customers are unreachable inside
        any horizon, and only the remainder is the engine falling short. Reporting the total
        without the split invites a panel to read all of it as failure."""
        f = m["primary_cohort_21d"]["failure_list"]
        counts = f["standing"]["counts"]
        assert sum(counts.values()) == f["n_not_recovered"]
        assert set(f["standing"]["rupees"]) == set(counts)
        # The two buckets that are not defects must be reported, not folded away.
        assert counts["stopped_by_a_guardrail_correct"] > 0
        assert counts["no_money_in_the_window_unreachable"] > 0

    def test_committed_docs_agree_with_the_artifact(self, m):
        """Every figure in the docs must be the artifact's, checked rather than trusted.

        This is the rule the repo already had - "no hand-written numbers, every figure is
        generated from results/metrics.json and a test asserts they match" - and Phase 3 is
        where it was found not to be enforced. `docs/phase-3.md` was written by hand, the
        retry-horizon defect was fixed, the batch was re-run, and the document went on
        claiming Rs 736,114 while the artifact said Rs 935,664. A reviewer caught it. A panel
        catching a submission whose own documents disagree about what it recovered is the
        version of that where it costs something.

        `scripts/render_docs.py --check` exits non-zero if any generated block is stale, so
        the failure now lands here instead.
        """
        r = subprocess.run([sys.executable, "scripts/render_docs.py", "--check"],
                           capture_output=True, text=True, cwd=REPO)
        assert r.returncode == 0, (
            "docs are stale - run `python scripts/render_docs.py`\n"
            f"{r.stdout}\n{r.stderr}")

    def test_renderer_never_prints_a_null_cost_or_asserts_significance_it_lacks(self, m):
        """Two ways the generated block could state something the artifact does not.

        `cost_per_incremental_rupee` is deliberately None when there is no incremental
        recovery to divide by - null rather than a flattering zero. Formatted straight into
        the sentence that becomes "Rs None per incremental rupee recovered".

        Worse, the sentence "All three intervals exclude zero" was a hardcoded string: a
        claim about statistical significance, asserted rather than read, inside the one
        section whose entire purpose is that its figures come from the artifact. It would
        have kept saying so after an interval crossed zero. `excludes_zero` is computed per
        cohort and is now what the sentence is built from.

        Both are checked against mutated copies, because neither shows up in the happy path
        and the happy path is the only thing a committed artifact ever exercises.
        """
        import copy
        import importlib.util

        spec = importlib.util.spec_from_file_location("rd", REPO / "scripts" / "render_docs.py")
        rd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rd)

        no_lift = copy.deepcopy(m)
        no_lift["primary_cohort_21d"]["cost_per_incremental_rupee"] = None
        out = rd.render_phase3_results(no_lift)
        assert "Rs None" not in out and "None per incremental" not in out, out

        crossed = copy.deepcopy(m)
        crossed["secondary_cohort_14d"]["primary"]["excludes_zero"] = False
        claim = rd._intervals_claim(crossed)
        assert "All three intervals exclude zero" not in claim, claim
        assert "14-day" in claim

        none_hold = copy.deepcopy(crossed)
        for k in ("primary_cohort_21d", "shifted_parameter_cohort"):
            none_hold[k]["primary"]["excludes_zero"] = False
        assert "None of the three" in rd._intervals_claim(none_hold)

        # And the true case still reads as it should.
        assert rd._intervals_claim(m) == "All three intervals exclude zero."

    def test_no_superseded_headline_survives_anywhere_in_the_docs(self, m):
        """The pre-amendment figures must not linger in prose the renderer does not own.

        A generated block keeps ITS numbers honest and says nothing about the paragraph
        underneath it. These are the superseded values from the first run; if one reappears
        outside the amendments that exist to record it, something was copied rather than
        generated.
        """
        superseded = ("736,114", "736,113.77", "263.09", "0.0033", "58.15%", "32.9 pp",
                      "943,979", "1,171 of 2,798", "41.9%")
        # Two files MUST be able to cite the old numbers: the amendments that exist to record
        # what was superseded, and the archived PR threads, which are a verbatim record of a
        # review that discussed them. Editing either to satisfy this test would be falsifying
        # a history, which is the opposite of what the test is for.
        allowed = {"docs/metric-definition.md", "docs/pr-review-archive.md"}
        offenders = []
        for path in sorted((REPO / "docs").rglob("*.md")):
            rel = path.relative_to(REPO).as_posix()
            if rel in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            for token in superseded:
                if token in text:
                    offenders.append(f"{rel}: {token}")
        assert not offenders, offenders

    def test_cohort_declares_it_is_simulated(self, m):
        assert "SIMULATED" in m["primary_cohort_21d"]["assignment"]["provenance"]
