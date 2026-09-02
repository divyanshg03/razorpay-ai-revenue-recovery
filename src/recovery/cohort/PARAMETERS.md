# Simulator parameters — every value, where it came from, and how much to trust it

**This is the most attackable part of the submission**, and it is written to be attacked.
The obvious objection is *"you generated the data, so of course your engine works."* Three
things answer it, and only the third is unusual:

1. **Every parameter below traces to a published figure**, cited, with the source graded.
2. **The engine never sees these parameters.** It observes only what a real merchant would:
   the failed payment, its error fields, and whether money later arrived.
3. **The engine's advantage is not a parameter.** There is no "arm C recovers X%" dial. The
   simulator models *customer behaviour* — when money is available, how people respond to
   being contacted, how they tire of it — and the engine has to earn its lift by timing and
   targeting against those mechanics. If the engine were deleted and replaced with the
   incumbent's fixed ladder, arm C's number would collapse to arm B's on its own.

Point 3 is the one that matters. A simulator with a recovery-rate dial per arm proves nothing
whatsoever; it just replays its own assumption back as a result.

---

## Source grading

| Grade | Meaning |
|---|---|
| **A** | Regulator, network operator, or primary statistical release |
| **B** | Reputable press reporting a primary figure |
| **C** | Vendor-published benchmark — commercially motivated, treat as an upper bound |

Grade C figures are used only where nothing better exists, and always at the **pessimistic**
end of their stated range. A vendor selling dunning software has an obvious interest in
reporting high recovery rates.

---

## 1. Cohort entry: what fails, and why

### UPI Autopay execution success rate — **30%**

> Approval/success rates "dropped nearly 20 [percentage points], from about 50 percent in
> January 2024 to 30 percent in November 2025", per **NPCI** data.
> — Grade **B** (press reporting an NPCI figure), corroborated by Business Standard.

Used as: the cohort is drawn from the ~70% that fail. We do not simulate the successes; the
cohort *is* the failure population, which is what a recovery engine actually receives.

### Dominant failure cause: insufficient funds

> "More than 20 million AutoPay mandates on UPI are revoked each month as users' accounts fall
> short of the required balances… there is debit execution failure… because there is not
> enough money in the user's bank account, with many cases of micro investment mandates, like
> for an SIP or loan repayments."
> — Grade **B** (Business Standard, reporting NPCI).

Used as: `insufficient_funds` is the single largest cause bucket. This is load-bearing for the
whole design, because insufficient funds is a **timing** problem, not a persuasion problem —
the customer is not refusing to pay, the money is not there yet. That is precisely what a
fixed T+0…T+3 ladder handles badly and what the engine can exploit.

### Cause mix

| Cause | Share | Basis |
|---|---|---|
| `insufficient_funds` | 0.55 | NPCI attribution above; dominant but not total |
| `payment_timed_out` / gateway | 0.15 | Razorpay's published `GATEWAY_ERROR` reasons |
| `card_expired` / instrument dead | 0.10 | Card-updater services "recover up to 20% of invoices before a retry is even attempted" (Grade **C**), implying a meaningful dead-instrument share |
| `payment_cancelled` / customer action | 0.10 | Razorpay `BAD_REQUEST_ERROR` customer-side reasons |
| other / `SERVER_ERROR` | 0.10 | Residual |

The split within the non-`insufficient_funds` remainder is **an assumption**, not a cited
figure. It is stated here rather than buried. Its effect is bounded: the diagnosis layer routes
each bucket differently, so the mix changes *how much* of the engine's behaviour is exercised,
not whether the comparison between arms is fair — all three arms see the identical cohort.

---

## 2. Customer behaviour

### Funds arrive on a payday cycle

Not a cited constant but a **mechanism**, chosen because the NPCI attribution above says the
dominant failure is an empty account. Modelled as a per-customer salary day drawn uniformly
across the month, with money available for a window afterwards.

This is the core of the simulation and the honest limitation to state aloud: **we assert that
funds availability is periodic and salary-linked.** For SIPs, loan repayments and OTT
subscriptions in India that is a reasonable claim, but it is a claim.

### Organic self-cure (no contact, no retry) — **parameter 0.004/day, emergent ≈ 1.9%**

Some customers notice and pay unprompted. No clean public figure exists for this in India, so
the honest framing is the one below:

> Involuntary churn "accounts for an estimated 20%–40% of total churn"; subscription businesses
> "lose around 9% of monthly recurring revenue to failed payments."
> — Grade **C** (vendor benchmark). If self-cure were high, failed payments would not be a
> recognised revenue problem at all.

**The tunable parameter is a per-day attention rate of 0.004, not a window total.** Self-cure
also requires funds to be present, so over a 21-day window it compounds to a measured **1.92%**
(n=5,000) rather than to any figure asserted here. That number is an *output* of the model and
is regenerated by the batch, never typed in.

An earlier draft of this file claimed 8% for this line. That was wrong — it described an
intention rather than what the model produces, which is exactly the prose-versus-artifact gap
this project's honesty rules exist to close. Corrected 2 Sept 2026.

A **higher** self-cure rate would lift every arm and compress the gap between them, so the low
value does not flatter the engine. It affects the secondary C-vs-A comparison; the primary
comparison is C vs B, and arm B is separately validated against a published band (§3).

### Response to contact

A contact does not create money. It can only (a) prompt someone to move funds or fix an
instrument, or (b) be ignored. Modelled as a probability that the customer acts within a short
window, conditioned on the cause's actionability — high for `needs_funds` near payday, near
zero for a dead instrument until a new one is supplied, and zero for causes marked
`DO_NOT_CONTACT`.

### Contact fatigue

Each additional contact to the same person is less effective than the last, with a decay
factor. Grounded in the same regulatory reasoning that drives the caps: repeated nudging is a
named dark pattern (CCPA 2023) and "excessively calling / messaging" is prohibited conduct
(RBI 454Z(4)). A simulator in which spamming works would be modelling a world we are not
allowed to operate in, and would reward exactly the behaviour the guardrails exist to prevent.

---

## 3. The incumbent baseline (arm B)

Razorpay's documented behaviour, reimplemented rather than observed — gate 0.3 was voided when
Subscriptions turned out to be gated (`docs/phase-0-findings.md`):

> "we automatically retry the payment on the following day", four attempts (T+0…T+3), then
> `halted`. "Invoices for such Subscriptions are still created. However, we will not charge
> these invoices. **You will have to charge them manually.**"
> — Grade **A** for what the product does (Razorpay's own documentation).

Sanity check on the resulting rate: automated retry systems without smart timing recover
roughly **20–30%** (Stripe's native Smart Retries, Grade **C**). If arm B in simulation lands
far outside that band, the model is wrong and must be corrected **before** any arm-C number is
looked at. That check is asserted in the tests, not left to judgement.

---

## 4. What is NOT modelled, and would change the answer

- **Cross-channel deliverability.** WhatsApp/SMS delivery is assumed to succeed. In reality
  DLT rejections, blocked numbers and WhatsApp quality-tier throttling all bite.
- **Competing debts.** A customer with several failed mandates in the same month has finite
  money; here each debt is independent given the customer's funds state.
- **Genuine unwillingness.** Everyone here is modelled as willing-but-unable or inattentive.
  Real portfolios contain people who will never pay, and no contact strategy changes that.
- **Seasonality and festival effects**, which are large in India.

Each of these would reduce measured recovery. None of them changes the *ranking* of the arms,
because all three arms face the identical cohort and the identical mechanics.

---

## 5. Eval on shifted parameters

The evaluation cohort is generated with **shifted** parameters — a different seed, a different
cause mix, and a different payday distribution — so the engine cannot succeed merely by
inverting the generator it was tuned against. Any policy that only works on the training
distribution shows up as a collapse on the shifted one, and both numbers are reported.
