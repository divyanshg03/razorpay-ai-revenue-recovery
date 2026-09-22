"""Render every committed figure from `results/metrics.json` into the docs.

    python scripts/render_docs.py            # rewrite the generated blocks
    python scripts/render_docs.py --check    # fail if any block is stale (used by the tests)

## Why this exists

The honesty rule for this repo is that no figure is ever typed: it comes from the artifact or
it does not appear. `docs/phase-3.md` was written by hand, the retry-horizon defect was then
fixed, the batch was re-run — and the document silently went on claiming Rs 736,114 while
`results/metrics.json` said Rs 935,664. A reviewer caught it, which is the good outcome; the
bad outcome was available too, and it was a panel catching a submission whose own documents
disagree about what it recovered.

So the numbers now live inside marked regions:

    <!-- generated:phase3-results -->
    ...regenerated from the artifact, never edited by hand...
    <!-- /generated:phase3-results -->

`--check` is wired into the test suite, so a stale document fails the build rather than
waiting to embarrass someone. This is the mechanism Phase 4 needs for the README; it arrives
early because Phase 3 proved it was needed.

Prose OUTSIDE the markers is written by a human and is not touched. That is deliberate: an
interpretation is not a figure, and generating claims would be its own kind of dishonesty.
What the markers guarantee is narrower and worth more — every number is the artifact's.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import textwrap

REPO = pathlib.Path(__file__).resolve().parents[1]
METRICS = REPO / "results" / "metrics.json"
#: The companion artifact: the same cohort measured inside NPCI's attempt cap. Read by the
#: blocks that report it, rather than threaded through every renderer's signature, because
#: only two of them need it and every other block must keep rendering without it.
NPCI = REPO / "results" / "npci-cap-rerun.json"

#: Which generated block belongs to which file. One block may appear in several files.
TARGETS: dict[str, tuple[str, ...]] = {
    "docs/phase-3.md": ("phase3-results",),
    "README.md": ("readme-npci", "readme-npci-control", "readme-npci-failures",
                  "readme-headline", "readme-control", "readme-failures",
                  "readme-limitations", "readme-reproduce"),
}


def _npci() -> dict | None:
    return json.loads(NPCI.read_text(encoding="utf-8")) if NPCI.exists() else None


def _rs(x: float) -> str:
    """Rupees, grouped, no decimals — the form every figure in prose uses."""
    return f"Rs {x:,.0f}"


def _rs2(x: float) -> str:
    """Rupees keeping paise — for per-customer figures, where the decimals carry meaning."""
    return f"Rs {x:,.2f}"


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _an(x: float) -> str:
    """"a 41.26 pp lift" but "an 18.82 pp lift". A generated document still has to read like
    someone wrote it."""
    return "an" if str(abs(x)).lstrip("0.").startswith(("8", "11", "18")) else "a"


def _wrap(text: str, width: int = 95) -> str:
    """Match the hand-written prose around it. Markdown does not care; a reviewer does."""
    # break_on_hyphens=False: otherwise "pre-registration" splits across lines at the hyphen,
    # which reads as a typo in a document whose subject is being precise.
    return "\n".join(textwrap.wrap(text, width=width, break_on_hyphens=False))


def _cost(value: float | None) -> str:
    """`cost_per_incremental_rupee` is deliberately None when there is no incremental
    recovery to divide by - reported as null rather than as a flattering zero. Formatting
    that straight into the sentence yields "Rs None per incremental rupee recovered", which
    is worse than a wrong number because it looks like a typo rather than a finding."""
    if value is None:
        return ("**no cost per incremental rupee**, because there was no incremental "
                "recovery to divide by")
    return f"a cost of **Rs {value} per incremental rupee recovered**"


def _intervals_claim(blocks: dict[str, dict]) -> str:
    """Say what the intervals actually did, rather than asserting the happy case.

    This sentence used to be the literal string "All three intervals exclude zero." - a
    claim about statistical significance, hardcoded, in the one section of the document whose
    entire purpose is that its figures come from the artifact. It would have gone on saying
    so after an interval crossed zero, which is exactly the direction of error that matters.
    `excludes_zero` is already computed per cohort; this reads it.
    """
    labels = ("21-day", "14-day", "shifted-parameter")
    flags = [blocks[k]["primary"]["excludes_zero"] for k in
             ("primary_cohort_21d", "secondary_cohort_14d", "shifted_parameter_cohort")]
    if all(flags):
        return "All three intervals exclude zero."
    if not any(flags):
        return "**None of the three intervals excludes zero.**"
    failed = [lab for lab, ok in zip(labels, flags) if not ok]
    verb = "does" if len(failed) == 1 else "do"
    return (f"**Not every interval excludes zero:** the {' and the '.join(failed)} "
            f"{verb} not.")


def render_phase3_results(m: dict) -> str:
    p21, p14, psh = (m["primary_cohort_21d"], m["secondary_cohort_14d"],
                     m["shifted_parameter_cohort"])
    h, s = p21["primary"], psh["primary"]
    f = p21["failure_list"]
    st = f["standing"]["counts"]
    rr = p21["arms"]

    def row(label: str, block: dict, bold: bool = False) -> str:
        c = block["primary"]
        lo, hi = c["ci95_total_rupees"]
        net = _rs(c["net_incremental_total_rupees"])
        return (f"| {label} | {'**' + net + '**' if bold else net} | "
                f"{_rs(lo)} – {_rs(hi)} |")

    lines = [
        "| | Net incremental (net of contact cost) | 95% CI |",
        "|---|---|---|",
        row("**Headline — 21d, engine vs Razorpay's ladder**", p21, bold=True),
        row("Secondary — 14d window", p14),
        row("Shifted-parameter cohort", psh),
        "",
        _wrap(
        f"{_rs2(h['net_incremental_per_customer_rupees'])} per treated customer. "
        f"Arm A {_pct(rr['A']['recovery_rate'])}, arm B {_pct(rr['B']['recovery_rate'])}, "
        f"arm C {_pct(rr['C']['recovery_rate'])} — {_an(h['lift_pp'])} {h['lift_pp']} pp "
        f"lift over the incumbent, at {_cost(p21['cost_per_incremental_rupee'])}. "
        f"{_intervals_claim(m)}"),
        "",
        _wrap(
        f"On the shifted cohort — built to be harder, with fewer insufficient-funds cases and "
        f"more dead instruments — arm B rises to {_pct(psh['arms']['B']['recovery_rate'])} "
        f"while arm C reaches {_pct(psh['arms']['C']['recovery_rate'])}, "
        f"{_an(s['lift_pp'])} {s['lift_pp']} pp lift. The edge narrows under a distribution "
        f"the engine was not built against, which is what that cohort exists to test."),
        "",
        _wrap(
        f"**And it reports what it failed to recover:** {f['n_not_recovered']:,} of "
        f"{rr['C']['n']:,} customers ({_pct(f['share_not_recovered'])}), "
        f"{_rs(f['unrecovered_rupees'])} left on the table. That total is four different "
        f"things and only one of them is a defect:"),
        "",
        "| Why it was not recovered | Customers | Rupees |",
        "|---|---|---|",
    ]
    labels = {
        "stopped_by_a_guardrail_correct":
            "Stopped by a guardrail — **correct behaviour**",
        "no_money_in_the_window_unreachable":
            "No money in the window at all — **unreachable by any policy**",
        "funded_but_never_attempted_DEFECT":
            "Funded, but never attempted — **defect, must stay 0**",
        "attempted_while_funded_still_unpaid":
            "Attempted while funded, still unpaid — the honest residual",
    }
    rup = f["standing"]["rupees"]
    for k, label in labels.items():
        lines.append(f"| {label} | {st[k]:,} | {_rs(rup[k])} |")
    return "\n".join(lines)


def render_readme_headline(m: dict) -> str:
    h, p21 = m["headline"], m["primary_cohort_21d"]
    lo, hi = h["ci95_rupees"]
    rr, md = p21["arms"], m["metric_definition"]
    b = m["bootstrap"]
    return "\n".join([
        f"| | |",
        f"|---|---|",
        f"| **Net incremental recovery, six retries** | **{_rs(h['net_incremental_rupees'])}** |",
        f"| 95% CI | {_rs(lo)} – {_rs(hi)} |",
        f"| Per treated customer | {_rs2(h['per_customer_rupees'])} |",
        f"| Compared against | {h['comparison']} |",
        f"| Cost per incremental rupee | "
        f"{('Rs ' + str(p21['cost_per_incremental_rupee'])) if p21['cost_per_incremental_rupee'] is not None else 'n/a - no incremental recovery'} |",
        f"| Recovery rate, A / B / C | {_pct(rr['A']['recovery_rate'])} / "
        f"{_pct(rr['B']['recovery_rate'])} / {_pct(rr['C']['recovery_rate'])} |",
        f"| Cohort | {m['n_customers']:,} simulated customers, seed {b['seed']} |",
        f"| Interval method | {b['method']}, {b['resamples']:,} resamples |",
        f"| Metric frozen at | `{md['frozen_at_commit']}`, ancestry verified: "
        f"{str(md['is_ancestor_of_head']).lower()} |",
        "",
        _wrap("All three pre-registered readouts, not just the largest. The 21-day window is "
              "the primary; the other two were declared in the frozen definition before any "
              "result existed and are reported whatever they say:"),
        "",
        "| Readout | Net incremental | 95% CI |",
        "|---|---|---|",
        f"| **21-day (primary)** | **{_rs(m['headline']['net_incremental_rupees'])}** | "
        f"{_rs(m['headline']['ci95_rupees'][0])} – {_rs(m['headline']['ci95_rupees'][1])} |",
        f"| 14-day (secondary) | "
        f"{_rs(m['secondary_cohort_14d']['primary']['net_incremental_total_rupees'])} | "
        f"{_rs(m['secondary_cohort_14d']['primary']['ci95_total_rupees'][0])} – "
        f"{_rs(m['secondary_cohort_14d']['primary']['ci95_total_rupees'][1])} |",
        f"| Shifted-parameter cohort | "
        f"{_rs(m['shifted_parameter_cohort']['primary']['net_incremental_total_rupees'])} | "
        f"{_rs(m['shifted_parameter_cohort']['primary']['ci95_total_rupees'][0])} – "
        f"{_rs(m['shifted_parameter_cohort']['primary']['ci95_total_rupees'][1])} |",
        "",
        _wrap(
        f"Arm A does nothing. Arm B is the incumbent ladder as the submission reimplemented "
        f"it, which also re-debits on the day of the failure - five debits rather than the "
        f"documented four. Arm C is the engine with six retries, over NPCI's cap. "
        f"{_intervals_claim(m)}"),
    ])


def _ci(row: dict) -> str:
    return f"{_rs(row['ci95_total_rupees'][0])} – {_rs(row['ci95_total_rupees'][1])}"


def render_readme_npci(m: dict) -> str:
    """The headline: the cohort measured inside NPCI's attempt cap (amendment A12).

    It became the headline by a dated amendment, in the conservative direction - it is smaller
    than the six-retry result it displaced - and that six-retry result is still rendered,
    unedited, in its own section further down. A headline switched silently is exactly what a
    frozen definition exists to prevent; one switched by an amendment that LOWERS the number,
    with the original left in view, is what the amendment log exists to record.
    """
    cap = _npci()
    if cap is None:
        return "_(results/npci-cap-rerun.json is not present - run scripts/npci_cap_rerun.py)_"
    arms, cmp_, sched, b = cap["arms"], cap["comparisons"], cap["schedule"], cap["bootstrap"]
    head = cmp_["headline__C_vs_B"]
    net = head["net_incremental_total_rupees"]
    cost = (f"Rs {arms['C_engine']['contact_cost_rupees'] / net:.4f}" if net > 0
            else "n/a - no incremental recovery")

    def _bullets(items: list[str]) -> list[str]:
        out = []
        for item in items:
            wrapped = textwrap.wrap(item, width=91, break_on_hyphens=False)
            out.append(f"- {wrapped[0]}")
            out.extend(f"  {line}" for line in wrapped[1:])
        return out

    return "\n".join([
        "| | |",
        "|---|---|",
        f"| **Net incremental recovery** | **{_rs(net)}** |",
        f"| 95% CI | {_ci(head)} |",
        f"| Per treated customer | {_rs2(head['net_incremental_per_customer_rupees'])} |",
        "| Compared against | the engine (C) vs Razorpay's documented T+1..T+3 ladder (B) |",
        f"| Cost per incremental rupee | {cost} |",
        f"| Recovery rate, A / B / C | {_pct(arms['A_do_nothing']['recovery_rate'])} / "
        f"{_pct(arms['B_incumbent']['recovery_rate'])} / "
        f"{_pct(arms['C_engine']['recovery_rate'])} |",
        f"| Rule | {cap['rule']} |",
        f"| Schedule | the charge on day 0; retries on days "
        f"{', '.join(map(str, sched['engine_and_controls']))}; Razorpay's ladder on days "
        f"{', '.join(map(str, sched['incumbent']))} |",
        f"| Window | {cap['window_days']} days, anchored on {cap['window_anchored_on']} |",
        f"| Cohort | {cap['n_customers']:,} simulated customers, seed {cap['seed']} |",
        f"| Interval method | {b['method']}, {b['resamples']:,} resamples |",
        f"| Generated at | `{cap['head_commit']}`, "
        f"{'with uncommitted changes' if cap['working_tree_dirty'] else 'clean tree'} |",
        "",
        _wrap(
        "Arm A does nothing. Arm B is Razorpay's Subscriptions ladder as documented: the "
        "charge, then a retry on each of the three following days. Arm C is the engine, "
        "allowed the same four debits and no more. The headline is **C against B** - beating "
        "do-nothing proves nothing, since every recovery vendor beats doing nothing. The "
        f"interval {'excludes zero' if head['excludes_zero'] else '**crosses zero**'}."),
        "",
        "Still not modelled, and each of these would move the figures above:",
        "",
        *_bullets(cap["not_modelled"]),
    ])


def render_readme_npci_control(m: dict) -> str:
    """Where the money comes from, inside the cap: the calendar, and what decisioning adds.

    Every sentence that makes a claim about direction reads `excludes_zero` rather than
    asserting it, for the reason `_intervals_claim` gives: a hard-coded significance claim
    goes on being made after the interval it describes has moved.
    """
    cap = _npci()
    if cap is None:
        return "_(results/npci-cap-rerun.json is not present - run scripts/npci_cap_rerun.py)_"
    arms, cmp_, sched = cap["arms"], cap["comparisons"], cap["schedule"]
    fair = cmp_["decisioning_is_worth__C_vs_D_diagnosed"]
    blind = cmp_["decisioning_is_worth__C_vs_D"]
    spacing = cmp_["spacing_is_worth__D_diagnosed_vs_B"]
    spacing_blind = cmp_.get("spacing_is_worth__D_vs_B")
    dead = cap["C_engine_by_diagnosed_cause"]["needs_new_instrument"]
    days = ", ".join(map(str, sched["engine_and_controls"]))
    ladder = ", ".join(map(str, sched["incumbent"]))

    if fair["excludes_zero"] and fair["net_incremental_total_rupees"] > 0:
        adds = ("With only three retries the calendar runs out of attempts, and what is left to "
                "collect with is deciding whom to contact, on what channel, and whom to leave "
                "alone.")
        verdict = ("**So the engine is two things.** It makes aggressive timing safe to deploy "
                   "- the calendar is the lever, and compliance is the constraint on pulling it "
                   "- and, under the rule a deployment actually faces, it collects what timing "
                   "alone cannot reach.")
    else:
        adds = "This run cannot say that decisioning adds recovery inside the cap."
        verdict = ("**So the engine is what makes aggressive timing safe to deploy.** The "
                   "calendar is the lever; compliance is the constraint on pulling it.")

    return "\n".join([
        "| Arm | What it does | Recovery |",
        "|---|---|---|",
        f"| A | nothing at all | {_pct(arms['A_do_nothing']['recovery_rate'])} |",
        f"| B | Razorpay's ladder: the charge, then days {ladder} | "
        f"{_pct(arms['B_incumbent']['recovery_rate'])} |",
        f"| D | the calendar alone, days {days}, retrying every cause | "
        f"{_pct(arms['D_calendar_blind']['recovery_rate'])} |",
        f"| **D'** | **the calendar alone, days {days}, respecting the diagnosis** | "
        f"**{_pct(arms['D_diagnosed_calendar']['recovery_rate'])}** |",
        f"| **C** | **the full engine** | **{_pct(arms['C_engine']['recovery_rate'])}** |",
        "",
        _wrap(
        f"**Better timing is worth {_rs(spacing['net_incremental_total_rupees'])}** (D' "
        f"against B, 95% CI {_ci(spacing)}). The same three retries, with no diagnosis, no "
        "message and no model - moved off the broke week in which the charge failed and spread "
        "across the salary cycle. Razorpay's ladder does not fail because it is "
        "unintelligent; it fails because four attempts inside four days sit in one broke week "
        "of a monthly cycle."),
        "",
        _wrap(
        f"**The decisioning layer adds {_rs(fair['net_incremental_total_rupees'])} on top of "
        f"that calendar** (C against D', paired on the same customers, 95% CI {_ci(fair)}), "
        f"and the interval {'excludes' if fair['excludes_zero'] else '**crosses**'} zero. "
        f"{adds}"),
        "",
        _wrap(
        f"**Where that comes from.** The {dead['n']} debts diagnosed `needs_new_instrument` - "
        "a card that expired, a mandate that no longer charges - are ones D' declines to retry "
        "at all, because a silent retry can never charge a dead instrument; D' recovers only "
        f"those that pay on their own. The engine recovers {dead['recovered']} of them "
        f"({_pct(dead['recovery_rate'])}) by asking the customer for a new one, which only a "
        "message can do. D' also has no answer to an opt-out, a dispute or a bereavement, "
        "because it never speaks and so never hears one."),
        "",
        _wrap(
        "**Why the blind calendar, D, looks as good as the engine.** It retries every cause, "
        "including failures where the customer has to act, and scores "
        f"{_rs(blind['net_incremental_total_rupees'])} against the engine ({_ci(blind)}, "
        f"{'excluding' if blind['excludes_zero'] else 'crossing'} zero). It gets there only "
        "because the simulator lets a silent retry fix causes that in reality need the "
        "customer - the modelling gap amendment A2 declined to exploit for the engine. Using "
        "it against the engine would be an inconsistent standard, so D' is the control to "
        "read."
        + (f" For the same reason D shows {_rs(spacing_blind['net_incremental_total_rupees'])} "
           "against the incumbent, more than D'." if spacing_blind else "")),
        "",
        _wrap(verdict),
    ])


def render_readme_npci_failures(m: dict) -> str:
    """What the engine did not recover inside the cap - decomposed as the frozen run's is,
    by the shipped `failure_list`, with every predicate anchored on the debt's own window."""
    cap = _npci()
    if cap is None or "C_engine_failure_list" not in cap:
        return "_(the capped failure list is not present - run scripts/npci_cap_rerun.py)_"
    f = cap["C_engine_failure_list"]
    st, rup = f["standing"]["counts"], f["standing"]["rupees"]
    labels = {
        "stopped_by_a_guardrail_correct":
            "Stopped by a guardrail — **the system was right to stop**",
        "no_money_in_the_window_unreachable":
            "No money at any point in the debt's window — **unreachable by any retry**",
        "funded_but_never_attempted_DEFECT":
            "Money in the window, but never on a permitted retry day — **the price of the cap**",
        "attempted_while_funded_still_unpaid":
            "Retried while funded, still unpaid — the honest residual",
    }
    lines = [
        _wrap(f"Inside the cap the engine did not recover {f['n_not_recovered']:,} of "
              f"{cap['arms']['C_engine']['n']:,} debts ({_pct(f['share_not_recovered'])}), "
              f"leaving {_rs(f['unrecovered_rupees'])} on the table. That total is four "
              "different things:"),
        "",
        "| Why it was not recovered | Customers | Rupees |",
        "|---|---|---|",
    ]
    for k, label in labels.items():
        lines.append(f"| {label} | {st[k]:,} | {_rs(rup[k])} |")
    third_is_largest = max(st, key=st.get) == "funded_but_never_attempted_DEFECT"
    lines += ["", _wrap(
        "Recovering the first two rows would mean breaking the opt-out, dispute and hardship "
        "rules, or collecting from people who had no money at any point in the window."
        + (" **The third row is the largest, and it is the argument for what to build next:** "
           "those customers had money, just not on any of the three days the rule allows a "
           "retry. Putting the three retries on the days money actually lands - predicted "
           "from each customer's own debit history - is where machine learning earns its "
           "place in this product, and the same holdout would measure it."
           if third_is_largest else
           " The third row counts customers who had money, but not on any of the three days "
           "the rule allows a retry: the price of the cap, measured."))]
    return "\n".join(lines)


def render_readme_failures(m: dict) -> str:
    p21 = m["primary_cohort_21d"]
    f = p21["failure_list"]
    st, rup = f["standing"]["counts"], f["standing"]["rupees"]
    labels = {
        "stopped_by_a_guardrail_correct":
            "Stopped by a guardrail — **the system was right to stop**",
        "no_money_in_the_window_unreachable":
            "No money in the window at all — **unreachable by any policy**",
        "funded_but_never_attempted_DEFECT":
            "Funded, but never attempted — **a defect; must stay 0**",
        "attempted_while_funded_still_unpaid":
            "Attempted while funded, still unpaid — the honest residual",
    }
    lines = [
        _wrap(f"With six retries the engine did not recover {f['n_not_recovered']:,} of "
              f"{p21['arms']['C']['n']:,} debts "
              f"({_pct(f['share_not_recovered'])}), leaving "
              f"{_rs(f['unrecovered_rupees'])} on the table. That total is four different "
              f"things, and only one of them is a defect:"),
        "",
        "| Why it was not recovered | Customers | Rupees |",
        "|---|---|---|",
    ]
    for k, label in labels.items():
        lines.append(f"| {label} | {st[k]:,} | {_rs(rup[k])} |")
    lines += ["", _wrap(
        "Recovering the first two rows would mean either breaking the opt-out, dispute and "
        "hardship rules, or collecting from people who had no money at any point in the "
        "window. They are reported as outcomes, not as failures to fix.")]
    return "\n".join(lines)


def render_readme_limitations(m: dict) -> str:
    """Every limitation the artifact carries, numbered, none omitted.

    Rendered rather than retyped so that adding one to `batch.py` and forgetting the README
    is impossible - the count is asserted by a test.
    """
    items = []
    for i, lim in enumerate(m["limitations"], 1):
        marker = f"{i}. "
        # Continuation lines are indented to the marker width. GitHub's lazy continuation
        # would render it either way; a human reading the raw file would not.
        body = textwrap.fill(lim, width=95, initial_indent=marker,
                             subsequent_indent=" " * len(marker), break_on_hyphens=False)
        items.append(body)
    return "\n\n".join(items)


def render_readme_reproduce(m: dict) -> str:
    b, md = m["bootstrap"], m["metric_definition"]
    return "\n".join([
        "```bash",
        "pip install -e '.[dev]'                 # Python >= 3.11; no runtime dependencies",
        "python scripts/run_batch.py             # regenerates results/metrics.json (~18 min)",
        "python scripts/npci_cap_rerun.py        # regenerates results/npci-cap-rerun.json",
        "python scripts/render_docs.py --check   # fails if any figure in the docs drifted",
        "pytest                                  # the full suite",
        "```",
        "",
        _wrap(
        f"The batch is offline and deterministic: no Razorpay credentials, no network, and "
        f"no Ollama. It regenerates `results/metrics.json` byte-for-byte from seed "
        f"{b['seed']} on {m['n_customers']:,} customers, with the sole exception of "
        f"`head_commit`, which records the commit it was generated at. The local model is "
        f"exercised in the test suite and the demo, where wording is the point; it cannot "
        f"affect this measurement, and `{md['document']}` says so."),
    ])


def render_readme_control(m: dict) -> str:
    """Arms D and D' under the pre-registered six-retry configuration.

    Kept, in its own section, because it is the result that undercut the submission's own
    product and because amendment A12 moved the headline without deleting anything. It now
    reads as the six-retry counterpart of the capped decomposition, and says so.
    """
    ctl = m["primary_cohort_21d"].get("spread_retry_control")
    if not ctl:
        return "_(controls not present in this artifact)_"
    arms = m["primary_cohort_21d"]["arms"]
    sp = ctl["spacing_is_worth__D_vs_B"]
    fair = ctl.get("decisioning_is_worth__C_vs_D_diagnosed") or {}
    # READ, not remembered: this count stood as a stale hand-typed literal inside this block
    # until 20 Sept 2026, where `--check` could never see it drift.
    dead = m["primary_cohort_21d"]["subgroup_by_diagnosed_cause"]["needs_new_instrument"]
    lo, hi = fair.get("ci95_total_rupees", [0, 0])
    return "\n".join([
        "| Arm | What it does | Recovery |",
        "|---|---|---|",
        f"| A | nothing at all | {_pct(arms['A']['recovery_rate'])} |",
        f"| B | the incumbent as reimplemented, days 0,1,2,3 | "
        f"{_pct(arms['B']['recovery_rate'])} |",
        f"| D | the calendar alone, retrying every cause | {_pct(arms['D']['recovery_rate'])} |",
        f"| D' | the calendar alone, respecting the diagnosis | "
        f"{_pct(arms['D_diagnosed']['recovery_rate'])} |",
        f"| C | the full engine | {_pct(arms['C']['recovery_rate'])} |",
        "",
        _wrap(
        "**With six retries, the calendar alone beats the engine.** Timing is worth "
        f"{_rs(sp['net_incremental_total_rupees'])} ({sp['lift_pp']:+.2f} pp, D against B), "
        "and a retry loop with no diagnosis, no message, no guardrails and no model "
        "out-recovers the full engine. Against the diagnosis-respecting calendar the "
        f"decisioning layer measures {_rs(fair.get('net_incremental_total_rupees', 0))} "
        f"({fair.get('lift_pp', 0):+.2f} pp) on an interval of {_rs(lo)} to {_rs(hi)} that "
        + ("excludes zero." if fair.get("excludes_zero") else
           "**crosses zero**: with attempts that plentiful, deciding whom to contact does not "
           "move recovery measurably. It changes what you are allowed to do while collecting, "
           "which the control was built to isolate and cannot price.")),
        "",
        _wrap(
        f"The engine still recovers {round(dead['n'] * dead['recovery_rate'])} of the "
        f"{dead['n']} dead-instrument debts that no silent retry can reach, and it is the only "
        "arm that honours an opt-out. This is the result that made the submission lead with "
        "*the money is in the calendar* - and the capped run is what qualified it: when the "
        "rule makes attempts scarce, the decisioning is what is left to collect with."),
    ])


RENDERERS = {
    "phase3-results": render_phase3_results,
    "readme-control": render_readme_control,
    "readme-headline": render_readme_headline,
    "readme-npci": render_readme_npci,
    "readme-npci-control": render_readme_npci_control,
    "readme-npci-failures": render_readme_npci_failures,
    "readme-failures": render_readme_failures,
    "readme-limitations": render_readme_limitations,
    "readme-reproduce": render_readme_reproduce,
}


def apply(text: str, name: str, body: str) -> str:
    open_, close = f"<!-- generated:{name} -->", f"<!-- /generated:{name} -->"
    pattern = re.compile(re.escape(open_) + r".*?" + re.escape(close), re.S)
    if not pattern.search(text):
        raise SystemExit(f"marker block '{name}' not found; expected {open_} ... {close}")
    return pattern.sub(f"{open_}\n{body}\n{close}", text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if any generated block is out of date")
    args = ap.parse_args()

    if not METRICS.exists():
        print(f"missing {METRICS.relative_to(REPO)} — run scripts/run_batch.py first",
              file=sys.stderr)
        return 1
    m = json.loads(METRICS.read_text(encoding="utf-8"))

    stale: list[str] = []
    for rel, blocks in TARGETS.items():
        path = REPO / rel
        original = path.read_text(encoding="utf-8")
        updated = original
        for name in blocks:
            updated = apply(updated, name, RENDERERS[name](m))
        if updated == original:
            print(f"up to date  {rel}")
            continue
        if args.check:
            stale.append(rel)
            print(f"STALE       {rel}", file=sys.stderr)
        else:
            path.write_text(updated, encoding="utf-8")
            print(f"rewritten   {rel}")

    if stale:
        print("\nDocs disagree with results/metrics.json. Run: "
              "python scripts/render_docs.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
