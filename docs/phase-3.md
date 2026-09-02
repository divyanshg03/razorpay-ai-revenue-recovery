# Phase 3 — Run the batch and measure

**Branch:** `feat/phase-2-decision-engine` (continues; Phase 3 is measurement of Phase 2).
**Status:** planned. Working deadline for the whole build is **3 September 2026**.

## Where this sits

| | Phase | Ends when |
|---|---|---|
| 0 | De-risk and freeze the contract | ✅ CLOSED — metric frozen at `8d14dbe` |
| 1 | Ingest, taxonomy, ledger | ✅ CLOSED at `890f972` |
| 2 | Decision engine and guardrails | ✅ CLOSED at `53ae1b4` — 77 tests, all invariants zero |
| **3** | **Run the batch and measure** | `results/metrics.json` exists: **net incremental rupees, engine vs Razorpay's ladder, against a randomised holdout, net of contact cost, with a confidence interval** — plus the honest list of what it failed to recover |
| 4 | Package and defend | README generated from the artifact, limitations written, video cut, repo public |

## The rule this phase lives under

**Nothing here is decided. Everything was decided on 31 August in `docs/metric-definition.md`,
committed at `8d14dbe` before a single line of engine code existed.** Phase 3 executes that
definition. If executing it turns out to need a change, the change goes in that file's
*Amendments* section, dated, with a statement of whether a result had been seen — and the
README reports that an amendment was made.

The batch is run **once** for the reported result. If it is re-run, every run is recorded.
Re-running until the number improves is fabrication, and the honesty rules name it as such.

---

## Work items

### 3.1 — Arm assignment

`evaluation/assignment.py`

Exactly as frozen: `sha256(f"{20260905}:{customer_id}") % 10_000` → A below 2,000, B below
4,000, C otherwise. Per **customer**, sticky, order-independent, no stored table.

- **Pass** Proportions land within tolerance on 5,000 customers; assignment is byte-stable
  across runs and process restarts; a customer's arm never changes.

### 3.2 — The batch runner

`evaluation/batch.py`

One cohort of 5,000 customers, seed `20260905`, partitioned by 3.1. Each arm runs on its own
partition against the **same** simulated world — the simulator's per-customer hidden state
is hash-derived, so a customer's payday is identical whichever arm they land in, and the arms
differ **only** in policy. That is what makes the difference between arms attributable.

- Arm A: `run_do_nothing`. Arm B: `run_incumbent_ladder`. Arm C: `run_engine` with the Phase 2
  policy, templates in-batch, ledger on simulated time.
- **Exclusions before randomisation only**: customers with a pre-existing opt-out, open
  dispute, hardship flag or no reachable channel are excluded *before* bucketing. **No
  post-randomisation exclusions, ever.** The customers the engine annoys into opting out stay
  in the denominator.
- **Pass** All three arms complete; the arm-C ledger replays with every invariant at zero.

### 3.3 — The primary metric, with its interval

`evaluation/metrics.py`

`NIR_per_customer = (r_C − r_B) − c_C` and `NIR_total = NIR_per_customer × n_C`, with a
**95% bootstrap CI** — percentile, 10,000 resamples, stratified by arm, `bootstrap_seed =
20260905`. Recovery per the frozen definition: rupees not a binary, capped at the amount
due, floored at zero, any payment route counts, 21-day window.

- **Pass** The headline figure and its CI are in `results/metrics.json`, and nowhere else
  until Phase 4 generates the README from that file.

### 3.4 — Everything the definition pre-declared as secondary

- C vs A (context only); recovery rate per arm; cost per incremental rupee; contacts per
  incremental recovery; the **14-day** window readout.
- **Sensitivity**: cheap end of every cost range, and ex-GST.
- **Two pre-declared subgroups and no others**: by diagnosed cause bucket, and by debt-size
  tercile. Anything else is exploratory and labelled so.
- **The failure list**: what arm C did *not* recover, by diagnosed cause and by stop reason.
  A recovery system that reports only its wins is a marketing asset.
- **Guardrail assertions**, replayed from the arm-C ledger: every count **must be zero**, and
  the batch **refuses to write `metrics.json` if any is not**.

### 3.5 — The shifted-parameter evaluation

`PARAMETERS.md` §5: a second cohort with a different seed, cause mix and payday distribution,
so a policy cannot succeed by having inverted the generator it was tuned against. Both
numbers are reported. If arm C's advantage collapses on the shifted cohort, that is a finding
and it goes in the README — it is not a reason to tune until it stops collapsing.

### 3.6 — The artifact and its tests

`results/metrics.json` — the **only** source of any figure in the README, with:
`policy_version`, `metric_definition_commit` (`8d14dbe`), cohort provenance, seed, arm counts,
every number above, the ledger's chain-head hash, and a timestamp.

Tests:
- `metrics.json` regenerates **byte-identically** from the seed (no wall-clock, no ordering
  dependence). The Phase 1 order-independence test is what makes this achievable.
- Git ancestry: the metric-definition commit is an ancestor of HEAD at generation time.
- Every guardrail count in the artifact is zero.
- The word `accuracy` appears nowhere in the artifact.

---

## Exit criteria

1. `results/metrics.json` exists, generated by one script, run once.
2. All invariants zero in the arm-C ledger, asserted by the batch before it writes.
3. Both cohorts reported: primary and shifted.
4. `pytest` green, including the byte-identical regeneration test.
5. Committed, never on `main`.

## Explicitly not in Phase 3

No README prose. No hand-typed figure anywhere. No tuning of policy or simulator after the
first look at a result — that door closed at `8d14dbe`.

## The honest expectation

The Phase 2 sanity run put arm C at ~52% against arm B's ~25% on the same seed with no
holdout. Phase 3 will produce a smaller, properly-measured number: a randomised split cuts
arm B to 1,000 customers, the interval will be wide, and the shifted cohort — with fewer
insufficient-funds cases and more dead instruments — is built to be harder. **That is the
point.** A number that survives a holdout, a confidence interval and a shifted distribution
is worth more than a bigger one that survives none of them.
