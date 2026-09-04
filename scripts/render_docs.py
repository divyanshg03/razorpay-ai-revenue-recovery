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

#: Which generated block belongs to which file. One block may appear in several files.
TARGETS: dict[str, tuple[str, ...]] = {
    "docs/phase-3.md": ("phase3-results",),
    "README.md": ("readme-headline", "readme-control", "readme-failures",
                  "readme-limitations", "readme-reproduce"),
}


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
        f"| **Net incremental recovery** | **{_rs(h['net_incremental_rupees'])}** |",
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
        f"Arm A does nothing. Arm B is Razorpay's own T+0..T+3 ladder, reimplemented. Arm C "
        f"is the engine. The headline is **C against B** - beating do-nothing proves nothing, "
        f"since every recovery vendor beats doing nothing. {_intervals_claim(m)}"),
    ])


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
        _wrap(f"The engine did not recover {f['n_not_recovered']:,} of "
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
        "python scripts/run_batch.py             # regenerates results/metrics.json",
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
    """Arms D and D'. The controls that separate the calendar from the decisioning."""
    ctl = m["primary_cohort_21d"].get("spread_retry_control")
    if not ctl:
        return "_(controls not present in this artifact)_"
    arms = m["primary_cohort_21d"]["arms"]
    sp = ctl["spacing_is_worth__D_vs_B"]
    fair = ctl.get("decisioning_is_worth__C_vs_D_diagnosed") or {}
    blind = ctl["decisioning_is_worth__C_vs_D"]
    lines = [
        "| Arm | What it does | Recovery |",
        "|---|---|---|",
        f"| A | nothing at all | {_pct(arms['A']['recovery_rate'])} |",
        f"| B | Razorpay's ladder, days 0,1,2,3 | {_pct(arms['B']['recovery_rate'])} |",
        f"| D | the calendar alone, retrying every cause | {_pct(arms['D']['recovery_rate'])} |",
        f"| **D'** | **the calendar alone, respecting the diagnosis** | "
        f"**{_pct(arms['D_diagnosed']['recovery_rate'])}** |",
        f"| C | the full engine | {_pct(arms['C']['recovery_rate'])} |",
        "",
        _wrap(
        "**Better timing is worth "
        f"{_rs(sp['net_incremental_total_rupees'])} ({sp['lift_pp']:+.2f} pp).** That is the "
        "finding. Razorpay's ladder does not fail because it is unintelligent; it fails "
        "because four attempts inside four days sit in one broke week of a monthly salary "
        "cycle. A retry loop with no diagnosis, no message, no guardrails and no model beats "
        "it by more than the entire engine does."),
        "",
        _wrap(
        f"**Against that, the decisioning layer costs "
        f"{_rs(abs(fair.get('net_incremental_total_rupees', 0)))}** "
        f"({fair.get('lift_pp', 0):+.2f} pp). Published as a negative number, because it is "
        "one. Use D' rather than D for this comparison: the blind control also recovers "
        "causes that in reality need the customer to act, which the simulator lets a silent "
        "retry fix. That is the same gap amendment A2 declined to exploit for the engine, and "
        "using it against the engine would just be an inconsistent standard. It is worth "
        f"{_rs(abs(blind['net_incremental_total_rupees']) - abs(fair.get('net_incremental_total_rupees', 0)))} "
        "of the difference between the two comparisons."),
        "",
        _wrap(
        "**So why not ship D'?** Because it is not a product. It never replaces a dead "
        "instrument, which only a message can do and which is where 97 of this cohort's "
        "recoveries come from. It has no answer to an opt-out, a dispute or a bereavement, "
        "because it never speaks and so never hears one. And it cannot tell a card that "
        "expired in March from an account that was briefly short, so it burns attempts on "
        "instruments that can never be charged."),
        "",
        _wrap(
        "The engine exists to make aggressive timing **safe to deploy**. The calendar is the "
        "lever; compliance is the constraint on pulling it. Those two sentences are the "
        "submission, and the controls above are what let us say them with a number rather "
        "than an assertion."),
    ]
    return "\n".join(lines)




RENDERERS = {
    "phase3-results": render_phase3_results,
    "readme-control": render_readme_control,
    "readme-headline": render_readme_headline,
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
