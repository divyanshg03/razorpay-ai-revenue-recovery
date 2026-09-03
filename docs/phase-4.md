# Phase 4 — Package and defend

**Branch:** `feat/phase-4-package`, cut from `main` at `75f172a`.
**Status: PLANNED, 3 September 2026.** Applications close **5 September**.

## Where this sits

| | Phase | Ends when |
|---|---|---|
| 0 | De-risk and freeze the contract | ✅ CLOSED — metric frozen at `8d14dbe` |
| 1 | Ingest, taxonomy, ledger | ✅ CLOSED at `908d336` |
| 2 | Decision engine and guardrails | ✅ CLOSED at `6a17f40` |
| 3 | Run the batch and measure | ✅ CLOSED at `75f172a` — 121 tests, every invariant zero |
| **4** | **Package and defend** | **A stranger can clone the repo, run one command, reproduce the headline figure, and find the limitations before they find the claim** |

## The one sentence this phase has to earn

> Everything the README claims is either generated from `results/metrics.json` or reproducible
> by a command the reader can run — and the things it cannot claim are stated by the
> submission itself, not discovered by the panel.

Phase 4 writes no engine code. If it turns out to need any, that is a Phase 3 defect and it
goes through the amendments process in `docs/metric-definition.md` like A1 did.

## What is already done and must not be redone

- **The figure generator exists.** `scripts/render_docs.py` renders marked blocks from the
  artifact, `--check` fails the build on drift, and a test runs it. Phase 4 registers the
  README as a second target; it does not build a second mechanism.
- **The limitations are already in the artifact**, all eight of them, under
  `limitations` in `results/metrics.json`. They get rendered, not retyped.
- **The honest failure story is already computed** — the four-way residual decomposition,
  including the defect bucket that must stay at zero.

## Work items

### 4.1 — README generated from the artifact

Register `README.md` in `render_docs.TARGETS` with three new blocks: `headline`,
`limitations`, `reproduce`. Prose around them is hand-written; every number inside them is
the artifact's.

The README must answer, in this order, because it is the order a judge reads in:

1. **What breaks** — recurring collection in India succeeds 30–50% of the time, Razorpay
   retries four times and then says *"you will have to charge them manually."*
2. **What this does** — the decisioning layer above that ladder.
3. **What it recovered**, against a randomised holdout, net of contact cost, with its interval.
4. **What it failed to recover**, decomposed, before any architecture discussion.
5. **That the cohort is simulated** — stated in the first screen, not in a footnote.
6. How to run it.

- **Pass** `python scripts/render_docs.py --check` is clean; a test asserts the README's
  figures equal the artifact's; no number in the README was typed by hand.

### 4.2 — Limitations, promoted rather than buried

Rendered from `limitations` in the artifact into the README body — not an appendix, not a
`<details>`. The simulator disclosure sits above the headline figure.

Add the two that Phase 3 produced and the artifact does not yet carry:

- the `needs_customer_action` weak spot at 17.99%, and **why it was left unfixed** (amendment
  A2: the available fix harvests a gap in our own simulator, and repairing that gap would
  strip recovery from the incumbent baseline instead)
- the pre-registered prediction that **did not hold**, and the defect that explains it (A1)

- **Pass** Every limitation in the artifact appears in the README; the count is asserted by a
  test, so adding one to the artifact and forgetting the README fails the build.

### 4.3 — One-command reproduction

```
python scripts/run_batch.py     # regenerates results/metrics.json from the seed
python scripts/render_docs.py --check
pytest
```

A `RUNBOOK.md` or a README section covering: Python version, `pip install -r`, that Ollama is
**not** needed to reproduce the measurement (templates in batch, per `engine_arm.py`), and
that no Razorpay credentials are needed either — the batch is offline and deterministic.

- **Pass** Followed literally on a clean clone, the three commands succeed and the regenerated
  `metrics.json` matches the committed one except `head_commit`.

### 4.4 — Architecture, defensible in five minutes

A short section plus one diagram covering the four things a panel will actually probe:

- **the deterministic state machine decides; the LLM only writes words and reads replies** —
  and there is no import path from the engine to the LLM
- **the copy gate**, demonstrated catching a real rejection, not asserted
- **the hash-chained ledger**, and replay invariants that never import the engine
- **the stopping rules**, and that the unrecovered residual includes people the system was
  *right* to leave alone — the decomposition, rendered from the artifact, not the raw total

### 4.5 — The five-minute video

Structure, timed: the problem in 45 seconds; the live demo slice against Razorpay test mode;
the batch result with its interval; the copy gate rejecting something; the failure list; the
simulator disclosure. Demo before numbers — the track is judged on a working demo.

### 4.6 — Repo public

Last, and only after 4.1–4.4 are green. Pre-flight, all mechanical:

- `.env` gitignored and absent from history; no test key in any tracked file
- no zrok hostname, no phone number, no personal data — the sweep from A6, re-run
- `accuracy` appears nowhere
- no "DPDP-compliant" claim anywhere; the phrasing is "designed for 14 May 2027"
- no numeric call cap attributed to RBI
- LICENSE present

- **Pass** A scripted pre-flight check runs all of the above and exits non-zero on any hit.

## Exit criteria

1. README's every figure is generated, and a test proves it.
2. Limitations appear before the headline, and their count is asserted against the artifact.
3. A clean clone reproduces `metrics.json` with three commands.
4. The pre-flight secret and claim sweep passes.
5. Video recorded, under five minutes, demo first.
6. Repo public.

## Explicitly not in Phase 4

No new engine behaviour. No re-running the batch hoping for a better number — the artifact is
what it is, and A1 already records the one re-run that changed it and why. No new metric, no
new subgroup. If a figure looks wrong, it gets an amendment, not an edit.

## The honest risk

**Time.** Two days, and the video is the item with no partial credit — a half-cut video scores
as no video. So 4.1 and 4.2 land first, because a repo that a judge can read and reproduce is
worth more than a polished narration of one they cannot.

**The second risk is tone.** The temptation in packaging is to state the ₹935,664 and let the
simulator disclosure drift down the page. That single edit would convert an honest project
into a dishonest one, and it is the specific failure this repo has been built to avoid since
Phase 0. The disclosure goes above the number.
