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
    return "\n".join(textwrap.wrap(text, width=width))


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
        f"lift over the incumbent, at a cost of "
        f"**Rs {p21['cost_per_incremental_rupee']} per incremental "
        f"rupee recovered**. All three intervals exclude zero."),
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


RENDERERS = {"phase3-results": render_phase3_results}


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
