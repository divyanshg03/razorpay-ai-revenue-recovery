# Metric definition — FROZEN

**Gate 0.7. Frozen 31 August 2026, before any batch has been run and before any result is
visible.** The git commit that adds this file is the evidence of that ordering; if the commit
adding this document is not an ancestor of the commit adding `results/metrics.json`, this
freeze is worthless and the submission should say so.

Nothing here may be changed silently. Changes go in the **Amendments** section at the foot of
this file, each recording what changed, why, and — the part that matters — whether any result
had been observed at the time of the change. Never edit a decision in place.

Why this exists: choosing an assignment unit, a window, a cost model or a baseline *after*
seeing outcomes is indistinguishable from choosing the definition that flatters the number,
and it is the single easiest thing for a panel to catch.

---

## 1. The experiment

### Three arms, not two

| Arm | Name | What happens | Share |
|---|---|---|---|
| **A** | Do-nothing | No retry, no contact. The debt is left alone after the initial failure. | 20% |
| **B** | Incumbent ladder | Razorpay's own behaviour reimplemented faithfully: retry at T+0, T+1, T+2, T+3, then halt. No customer contact. | 20% |
| **C** | Engine | Our decisioning layer. Chooses retry timing *and* contact actions, under the guardrails. | 60% |

Two arms would have been easier and would have produced a bigger, more flattering headline.
Beating do-nothing is trivial and proves nothing — the interesting question is whether the
engine beats **what Razorpay already ships**, which is arm B. So:

> **The primary comparison is C vs B.** C vs A is reported as context, never as the headline.

Arm B is the baseline the track brief implicitly sets, and it is the one a Razorpay panel will
care about. Reporting only C vs A would be the defensible-looking version of cheating.

### Assignment unit: the customer, not the payment

Randomisation is at the level of **`debtor_ref` (customer)**, and every debt belonging to that
customer inherits the customer's arm.

Per-payment assignment was rejected. A customer with two failed mandates could land in both
treatment and control, and contacting them about mandate A plainly changes their behaviour on
mandate B — interference between units, which breaks the estimator. Customer-level assignment
makes the no-interference assumption defensible instead of merely assumed.

Assignment is **sticky**: a customer who re-enters the cohort weeks later with a new failure
stays in the arm they were first assigned. Re-randomising returning customers would let the
better-behaved population drift between arms.

### Assignment mechanism and seed

```python
SEED = 20260905  # frozen 31 Aug 2026

def arm(customer_id: str) -> str:
    h = hashlib.sha256(f"{SEED}:{customer_id}".encode()).hexdigest()
    bucket = int(h, 16) % 10_000
    if bucket < 2_000:  return "A"   # do-nothing
    if bucket < 4_000:  return "B"   # incumbent ladder
    return "C"                       # engine
```

Hash bucketing rather than a shuffled list, deliberately: it is order-independent, resumable
after a crash, requires no stored assignment table that could drift, and is reproducible by
anyone with the seed. A test asserts arm proportions land within tolerance and that assignment
is stable across runs and across process restarts.

### Cohort size and what it can detect

**N = 5,000 customers.**

Minimum detectable effect at 80% power, α = 0.05 two-sided, on the *recovery-rate* scale, using
sizing assumptions drawn from published sector figures (arm A ≈ 8%, arm B ≈ 18% — consistent
with the reported 15–20% recovery from automated retries — and a hoped-for arm C ≈ 25%):

| Comparison | n | MDE | Assumed effect |
|---|---|---|---|
| **C vs B (primary)** | 3,000 vs 1,000 | **4.06 pp** | 7.0 pp |
| C vs A (secondary) | 3,000 vs 1,000 | 3.27 pp | 17.0 pp |

Both figures are computed, not asserted: `scripts/gate_0_7_power.py` regenerates every number
in this section and in §4, and its output is archived at
[`results/phase0/0.7-power-and-cost-check.json`](../results/phase0/0.7-power-and-cost-check.json).
The same script emits the break-even thresholds quoted in §4.

N was chosen so the MDE sits below the smallest effect that would be worth claiming, and it was
chosen *now*, not after a first look. Those assumed rates are sizing inputs only — they are not
predictions, not results, and not inputs to the simulator's response model.

**And an honest caveat that belongs right here, not buried in a limitations section:** the
cohort is simulated, so N is free and statistical significance is therefore cheap. We could
make any p-value we liked by turning a dial. Confidence intervals in this project communicate
*the precision of an estimate inside the simulation*. They are not evidence about the real
world and will not be presented as such.

### Exclusions — before randomisation only

Excluded from the cohort entirely, *prior* to arm assignment: customers with an active opt-out,
an open dispute, a bereavement/hardship flag, or no reachable contact on any channel.

**No post-randomisation exclusions, ever.** Once a customer is assigned they stay in the
analysis, including if they later opt out, dispute, or become unreachable. Dropping people after
assignment because of something that happened *to them during the experiment* is how a
recovery number gets quietly inflated — the customers who opt out are exactly the ones the
engine annoyed. They stay in the denominator.

---

## 2. What counts as a recovered rupee

Per debt, within the attribution window:

```
recovered(d) = clamp( settled(d) − reversed(d),  0,  amount_due(d) )
```

where `settled` is the sum of payments reaching `captured`, and `reversed` is the sum of refunds
and chargebacks. Customer-level recovery is the sum over that customer's debts.

The rules this encodes, each chosen against a specific way of cheating:

- **Rupees, not a binary.** A partial payment counts at its face value. Counting "recovered:
  yes/no" would let a ₹50 token payment on a ₹5,000 debt score identically to full settlement.
- **Capped at the amount due.** Overpayment does not earn extra credit.
- **Floored at zero.** A debt with more reversals than settlements contributes 0, not a negative.
- **Reversals subtract.** A payment that is refunded or charged back inside the window was not a
  recovery. A chargeback after the window is reported separately and flagged as a known
  limitation of the window choice, not silently ignored.
- **Any payment route counts.** If a treatment customer pays through the merchant's app, or a
  PSP retry succeeds, or they use our link — all of it counts. We do **not** attribute by click
  or by link-id.

That last rule is the one that makes the number honest, so it is worth being explicit. Because
arms are randomised, the difference between arms *is* the causal effect of the engine, and every
rupee a treatment customer pays belongs in that comparison regardless of how it arrived.
Click-attribution would do the opposite: it would credit the engine for payments that would have
happened anyway, and it is the standard way marketing dashboards overstate themselves.

**Resurrection.** Razorpay can deliver `payment.captured` after `payment.failed` for the same
transaction. Such a payment counts as recovered, and it must also suppress all further contact —
the state re-check before every action is both a compliance rule and a measurement rule. A
"recovery" we dunned someone for *after* they had already paid is a defect in both directions.

---

## 3. Attribution window

**21 days from the initial mandate failure, per debt.**

Sized to contain the full escalation ladder plus a promise-to-pay cycle: the incumbent ladder
runs 4 days, the 7-in-7 contact policy spans a week, a promise-to-pay is typically made for a
date within a week, and a broken promise is only flagged 3–5 days after the missed date. A
14-day window would truncate promise-to-pay effects and understate the engine; a 60-day window
would add mostly noise and let slow organic payment masquerade as recovery.

**A 14-day secondary readout is pre-declared here**, and both will be reported. Pre-registering
two windows before seeing anything is legitimate; picking between them afterwards is not.

Payments landing after the window are excluded from the primary metric and reported separately
as "recovered outside window," per arm. If that quantity turns out to be large or lopsided, that
is a finding about the window and it gets stated, not hidden.

---

## 4. Contact-cost model

Every figure is a published list price with a source. Where sources disagree, **we take the more
expensive end**, because a net-incremental claim that survives the highest plausible cost is
worth more than one tuned to the cheapest.

| Action | Unit cost (base) | + 18% GST | Source |
|---|---|---|---|
| WhatsApp Utility template, outside service window | ₹0.145 | **₹0.1711** | Meta India rate card as republished by BSPs; see conflict note |
| WhatsApp Utility template, inside open 24h service window | ₹0.00 | **₹0.00** | Meta: *"Utility templates delivered within an open customer service window are free"*, since 1 Jul 2025 |
| SMS, service-implicit, DLT local route | ₹0.18 | **₹0.2124** | Indian A2P range ₹0.10–₹0.18; top of range taken |
| Human agent, per **connected** call | ₹13.00 | **₹15.34** | Tier-2 collections BPO, ₹18–25k/agent/month at 80–120 connected calls/day → ₹7–13/call; top of range taken |
| Payment link create + `notify_by` | ₹0.00 | ₹0.00 | Razorpay charges on successful payment, not on link creation |
| Retry attempt (arms B and C) | ₹0.00 | ₹0.00 | A failed charge attempt carries no fee |

**Conflict note, WhatsApp Utility.** Sources disagree on the current India rate: one gives
₹0.115 effective 1 Jul 2026, another ₹0.145 effective 1 Jan 2026. Meta's own pricing page
confirms the free-in-service-window rule and that a rate card exists, but serves the INR figures
as a downloadable file rather than inline, so we could not read the number at source. **We use
₹0.145.** This is recorded as unverified-at-source rather than presented as fact.

Rules:

- **Cost is incurred on send, not on delivery.** An SMS that fails to deliver is still billed.
  Counting only delivered messages would understate the cost of a badly-targeted campaign, which
  is precisely the thing this project claims to fix.
- **GST is included in the base case**, which is conservative: a GST-registered merchant would
  reclaim it as input tax credit and face the ex-GST figure. A sensitivity at the ex-GST, and at
  the cheap end of every range, is reported alongside the headline.
- **Fixed costs are excluded from the marginal model** — DLT registration (₹5,900 + GST,
  one-time), BSP subscriptions, engineering. They do not vary with any decision the engine makes
  and so cannot legitimately enter a marginal decision rule. This also means **no payback-period
  or ROI claim will be made**, since that would require them.
- **MDR is excluded** from recovered rupees. UPI P2M carries nil MDR; cards run around 2%.
  Because MDR is proportional to the amount recovered, netting it would scale incremental
  recovery by `(1 − mdr)` and cannot change its sign or the ranking of arms. Stated, not hidden.
- **There is no price on customer annoyance, deliberately.** We could not source a defensible
  number for it, and inventing one would be exactly the failure mode this document exists to
  prevent. Annoyance is therefore handled as **hard constraints** — contact caps, quiet hours,
  stopping rules — not as a term in an objective function that could be traded away for money.

### What this cost model immediately implies

At ₹0.1711 per WhatsApp message, a ₹1,000 debt needs an uplift of **0.017 percentage points** to
justify contact. The money constraint is, in practice, never binding on messaging.

This is worth stating plainly rather than hiding, because it shapes the whole architecture: at
Indian messaging prices **the binding constraint is not cost, it is permission and patience.**
That is why this system is built around guardrails and stopping rules rather than around a
budget optimiser — and it is an honest answer to "why not just message everyone, constantly?"
The answer is regulatory and reputational, not economic.

The human-agent leg is the exception and the only place the money threshold genuinely bites: at
₹15.34 a connected call, a ₹1,000 debt needs **1.53 pp** of uplift to be worth escalating. That
is a real threshold, it is where cost-sensitive decisioning actually earns its keep, and it is
the number to put on a slide.

---

## 5. The primary metric

Let `n_X` be the number of customers in arm X, `R_X` total recovered rupees, `C_X` total contact
cost. Let `r_X = R_X / n_X` and `c_X = C_X / n_X`. By construction `c_A = c_B = 0`.

**Primary endpoint — net incremental recovery per treated customer, engine vs incumbent ladder:**

```
NIR_per_customer = (r_C − r_B) − c_C
```

**Headline figure — net incremental rupees across the treated population:**

```
NIR_total = [ (r_C − r_B) − c_C ] × n_C
```

Reported with a **95% bootstrap confidence interval**: percentile method, 10,000 resamples,
stratified by arm, `bootstrap_seed = 20260905`. Bootstrap rather than a normal approximation
because per-customer recovery is heavily zero-inflated and right-skewed, so a t-interval would
be quietly wrong in the tails.

This is the only number that gets to be the headline. Gross recovered rupees without the
counterfactual will not appear anywhere in the README.

### Secondary, all clearly labelled as such

- `(r_C − r_A) − c_C` — net incremental vs do-nothing (context only)
- Recovery rate per arm — fraction of customers recovering anything
- Cost per incremental rupee — `C_C / [(r_C − r_B) × n_C]`
- Contacts sent per incremental recovery
- The 14-day window readout
- Sensitivity: cheap end of every cost range, and ex-GST

### Guardrail metrics — assertions, not observations

These are pass/fail. Any non-zero value is a bug that blocks submission, not a metric to report
a value for:

- Contacts outside 08:00–19:00 IST — **must be 0**
- Contact-cap breaches — **must be 0**
- Contacts after a stop signal (opt-out, dispute, promise-to-pay, payment received) — **must be 0**
- Actions taken without an immediately-preceding payment-state re-check — **must be 0**
- Decisions absent from the audit trail — **must be 0**

Plus, reported as counts: copy-gate rejections by reason, and the LLM-composed messages that
were blocked. The copy gate must be **demonstrated catching a rejection**, not asserted.

### The failure list

The submission reports **what it failed to recover**, broken down by diagnosed cause, alongside
what it did. A recovery system that only reports its wins is a marketing asset, not an
engineering one.

### Banned from the codebase and the README

`accuracy`. Also: any uplift percentage quoted without the absolute rupee figure beside it, and
any figure typed by hand rather than generated from `results/metrics.json`.

---

## 6. Analysis discipline, pre-registered

- **One primary endpoint**, declared in §5. Everything else is secondary and labelled as such in
  the README, not just in this file.
- **Exactly two pre-declared subgroups:** by diagnosed failure-cause bucket, and by debt-size
  tercile. Any other subgroup result is exploratory, is labelled exploratory, and is never the
  headline.
- **No changing** the arms, seed, allocation, window, recovery definition or cost model after a
  result has been seen, except by a written amendment below that states a result had been seen.
- The batch is run **once** for the reported result. If it is re-run, every run is recorded in
  the amendments with its reason. Re-running until the number improves is fabrication.

---

## 7. Limitations — to be stated before the panel finds them

1. **The outcomes are simulated.** Subscriptions is gated on this test account (see
   `docs/phase-0-findings.md`), so the failed-charge cohort comes from a deterministic seeded
   simulator. Randomisation removes selection bias *within* the simulation. It cannot validate
   the simulation.
2. **Significance is cheap here.** N is a free parameter on a simulator. CIs express precision
   inside the simulation only.
3. **We wrote the response model.** The defences are that its parameters trace to cited public
   figures, that the engine never sees the generative parameters, and that evaluation runs on
   *shifted* parameters so the engine cannot simply invert the generator. These reduce the
   problem. They do not eliminate it.
4. **Costs are list prices, not invoices**, and the WhatsApp rate could not be read at source.
5. **MDR is excluded**; see §4.
6. **Annoyance is unpriced**; see §4.
7. **The incumbent baseline is reimplemented, not observed.** Gate 0.3 was voided when
   Subscriptions turned out to be gated, so arm B reproduces Razorpay's documented T+0…T+3 →
   `halted` behaviour from their docs rather than from measured traffic.

---

## 8. Sign-off

- [ ] Metric definition frozen as written, before any result was visible
- [ ] Committed prior to the first batch run — verified by git ancestry, not by assertion

Frozen by: ____________________  Date: ____________

---

## Amendments

Each entry states: date, what changed, why, and whether a result had been observed at the time.

### A1 - 2 Sept 2026 - engine defect fixed, batch re-run

**A result HAD been observed.** The first batch had already run and reported Rs 736,113.77 net
incremental, 95% CI [Rs 590,969.58 - Rs 874,218.90]. This amendment exists because that number
is now superseded, and the superseded value is recorded here so the change is auditable in the
direction that matters - you can see what it was before.

**What changed.** One function: `retry_schedule` in `src/recovery/engine/policy.py`. Nothing in
this definition document, no parameter, no arm, no cost, no window, no seed, no exclusion rule.

The function built a fixed-spacing list and truncated it to the retry budget:

    days = tuple(range(0, policy.retry_horizon_days + 1, policy.retry_spacing_days))
    return days[: policy.max_retries_per_debt]

which is 0, 3, 6, 9, 12, 15, 18, 21 cut to the first six. So `retry_horizon_days = 21` was
declared in the policy, described in the docstring, and never reached: the final attempt landed
on day 15 and the last six days of the horizon were never attempted. It now spreads the same
six attempts across the horizon - 0, 4, 8, 13, 17, 21 - with `retry_spacing_days` demoted from
a step to a minimum, since its justification (issuers throttle repeated mandate execution) is a
floor and not a rhythm.

**Why this is a defect and not a tuning knob.** The budget did not change (6), the horizon did
not change (21), the throttle floor did not change (3), and retries are free in the frozen cost
model so no cost moved. The docstring asserting that coverage of the salary cycle is the point
predates the measurement and is the older specification; the implementation contradicted it.
The test that should have caught it asserted `max(days) >= 15` - it tested the value the bug
produced rather than the property the docstring promised, and has been replaced with an
assertion that the last retry lands **on** the declared horizon.

**How it was found - this is the part that keeps it honest.** Not by re-running until the
number improved. The Phase 3 failure list was decomposed by asking, for each of the 1,171
unrecovered debts, whether money was ever available during the window and on which days. 489 of
them - 42% of the entire failure list - had funds arrive strictly after day 15, inside the
window we claimed to cover, on days we had stopped attempting. That decomposition is a
diagnostic that names a mechanism; it is reproducible and it pointed at one function before any
change was made.

**Scope of who it helps.** Arm C only. Arm B is Razorpay's T+0..T+3 ladder and does not call
this function; arm A takes no action. So this widens C vs B, and that asymmetry is disclosed
rather than buried. It is nonetheless the correct fix, because the alternative - leaving a
declared parameter unhonoured so the headline stays modest - would misreport the engine as
worse than the design it documents.

**One batch run follows this amendment.** Not a search over variants.

**Result.** Rs 935,664.07 net incremental, 95% CI [Rs 786,950.97 - Rs 1,074,095.45], Rs 334.40
per customer; cost per incremental rupee Rs 0.0027. Arm C recovery 58.15% -> 66.51%.

Two things in that result are worth more than the headline, because they are what makes it
checkable rather than merely larger:

* **Arms A and B did not move at all** - 2.00% and 25.25%, identical to the pre-fix run. A1
  predicted exactly this, because neither arm calls `retry_schedule`. Had either shifted, the
  change would have reached somewhere it was not supposed to.
* **`needs_customer_action` did not move either** - 17.99% before, 17.99% after, unchanged to
  four decimals, 237 failures before and 237 after. It is the one cause the engine never
  silently retries (see A2), so a pure retry-scheduling fix must leave it untouched. Every
  cause that *does* depend on a retry landing when money is present moved together:
  `needs_funds` 67.24% -> 77.48%, `retry_later` 64.88% -> 75.04%, and
  `needs_new_instrument` only 32.85% -> 35.02%, since it needs a contact before a retry can
  do anything.

The unrecovered share fell from 41.9% to 33.49%, Rs 943,979 to Rs 744,363.

**The secondary cohorts did not all improve, and are reported as they came out.** The
shifted-parameter cohort went DOWN, Rs 435,762 -> Rs 418,066, and the 14-day readout went down
too, Rs 532,261 -> Rs 483,145. The shifted cohort deliberately moves the payday distribution,
so a schedule spread for one distribution is not automatically better against another; the
14-day window truncates before the new day-17 and day-21 attempts exist, while still carrying
their cost. Both are the honest behaviour of a change that buys coverage late in a horizon,
and neither is quietly dropped for undercutting the headline.

### A4 - 2 Sept 2026 - the residual is now decomposed in the artifact, reporting only

"33.49% not recovered" is not one claim, it is four, and collapsing them invites a reader to
score all of it as failure. The failure list now carries a standing split, generated rather
than written:

| | primary (21d) | shifted |
|---|---|---|
| stopped by a guardrail - **correct behaviour** | 208 / Rs 185,742 | 163 / Rs 149,237 |
| no money in the window at all - **unreachable** | 267 / Rs 200,233 | 674 / Rs 556,726 |
| funded, but never attempted - **DEFECT** | **0** | **0** |
| attempted while funded, still unpaid | 462 / Rs 358,388 | 475 / Rs 392,875 |

The third row is the only one that is a bug: a customer whose salary landed inside the horizon
we advertise, on a day we never tried. It stood at 489 before A1 and is now zero, and a test
asserts it stays zero in both cohorts. The first two rows are not defects and recovering them
would mean either breaking the stopping rules or collecting from people who had no money at
any point in the window. The fourth is the honest residual - we asked, at a moment they could
have paid, and they did not.

This also explains the shifted cohort undercutting the primary, which A1 reported without
accounting for: it has 674 never-funded customers against 267, because shifting the payday
distribution moves more salaries outside the collection horizon entirely. That is a property
of the cohort, not a weakness of the schedule, and it is the kind of thing the shifted cohort
exists to expose.

The predicates read the simulator's generative truth. That is safe and deliberate: this runs
in the evaluation layer, after the engine has finished, and the engine never has access to it.
Knowing afterwards that a customer never had money is what a simulator is for. Both predicates
are optional, so the function still works against a real cohort where no such ground truth
exists and the split is simply absent.

**No number changed.** The batch was re-run to regenerate the artifact and every value in
`metrics.json` is byte-identical except `head_commit`, which records the commit it was
generated at. That identity is the evidence the change was reporting-only.

### A5 - 3 Sept 2026 - review fixes, batch re-run, no number changed

Raised by an automated review of the pull request. Recorded because the batch ran again, and
the rule is that every run is recorded whether or not anything moved.

**The correctness fix.** `run()` accepted a `resamples` argument and reported it in the
artifact under `bootstrap.resamples`, but `_cohort_block` called `compare()` without passing
it, so the bootstrap always used its own default. At the shipped default the two coincide at
10,000, which is why every published interval was computed with the resample count the
artifact claims, and why nothing caught it - the bug was invisible at exactly the setting
anyone would check. A caller asking for a cheaper interval got an expensive one and an
artifact that misdescribed it. Now threaded through, with a test that records what the
bootstrap was really called with rather than trusting the JSON.

**Also fixed, neither affecting any figure:** `by_cause` and `by_debt_size_tercile` each
called `summarise` twice per group, once per reported field. A phone number in
`results/phase0/0.4c-received-events.jsonl` was redacted; see `docs/local-setup.md` for the
disclosure and what was deliberately left alone.

**Result: no number changed.** Every value in `results/metrics.json` is byte-identical to the
A1/A4 run except `head_commit`. That identity is the whole evidence for calling these fixes
non-behavioural, which is why the batch was re-run rather than argued about.

### A3 - 2 Sept 2026 - the first invocation of that run refused to write, and why

Disclosed because "one batch run" above would otherwise be false, and because a reader who
found this later would be right to ask what the discarded invocation was.

The first invocation after the A1 fix **produced no number**. It aborted with 56
`contact_after_payment` guardrail violations and wrote no `metrics.json`. The violations were
real in the data and entirely phantom in fact: `AuditLedger` opens its file in append mode -
correct, it is an append-only structure - and the batch reused a fixed path, so the new run's
records were appended onto the previous run's. `arm-c-audit.jsonl` stood at 60 MB against 27 MB
for the ledgers that had only ever been written once.

The invariant then read two chains over the same debt ids as one history: it took each debt's
*newest* settlement timestamp, from the new run, and compared it against that debt's contacts
from the *old* run, which naturally came later. `debt_000092` shows the signature plainly -
retries on 3, 6 and 9 September, the old 0/3/6 spacing, followed by retries on 3 and 7
September, the new 0/4/8 spacing, in one file.

`AuditLedger` now takes an explicit `fresh` flag; the batch passes it, because a batch run is a
new experiment producing a new artifact. Appending remains the default, since silently
truncating an audit trail would be a worse bug than the one being fixed. Both behaviours are
now covered by tests.

**No engine parameter, policy value, or metric definition changed between the refused
invocation and the one that produced the number.** The only change was to how the harness
opens a file. The guardrail did its job on corrupt input, which is the outcome the
refuse-to-write rule exists to produce.

### A2 - 2 Sept 2026 - a change deliberately NOT made

Recorded because a rejected change is evidence too, and because the next person to look at the
failure list will find the same tempting thing.

`needs_customer_action` recovers 17.99% against 67.24% for `needs_funds` and is the weakest
subgroup in the result. The engine never silently retries it: `guardrails.py` permits a retry
for `NEEDS_NEW_INSTRUMENT` once a contact has gone out, on the reasoning that the customer may
have supplied a new instrument and only a charge attempt can find out - but grants
`NEEDS_CUSTOMER_ACTION` no equivalent. Making those two symmetric is a one-line change and
would raise the headline.

It was not made, for a reason that survives inspection better than the extra rupees would.
In the simulator, `attempt_charge` gates `NEEDS_NEW_INSTRUMENT` on a `has_new_instrument` flag
that only a delivered contact can set - a real causal chain, contact then act then charge.
`NEEDS_CUSTOMER_ACTION` has **no such gate**: it succeeds on funds alone. So an engine allowed
to retry it would not be recovering money by prompting anyone; it would be collecting rupees
from a constraint the simulator forgot to impose, and the gain would measure a modelling gap.

The obvious repair - add the missing gate so the chain is real - is also declined, and this is
the harder call. It would make the simulator stricter, which sounds unimpeachable, except that
arm B retries blindly and does not contact anyone at all. Tightening that gate would strip
recovery from the incumbent baseline while leaving the engine a path to earn it back, i.e. it
would raise the headline by handicapping the thing we are measuring against, using a
generative model we wrote ourselves. The domain evidence does not settle it either:
`authentication_failed` and `incorrect_otp` plausibly do clear on a silent re-execution, while
`payment_cancelled` plausibly does not, and the taxonomy lumps them together.

So the weak spot stands, unpatched and reported. It is a genuine limitation of the engine and
partly an artifact of the simulator, and that ambiguity is stated in the README rather than
resolved in whichever direction pays.
