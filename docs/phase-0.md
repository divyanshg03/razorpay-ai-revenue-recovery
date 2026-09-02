# Phase 0 — De-risk and freeze the contract

**Status: CLOSED — GO signed 2 September 2026.** All eight gates resolved; see the results
table and sign-off at the foot of this document. Ran 24 Aug – 2 Sept 2026 (the blocker in 0.1
fired on day 0 and reshaped the schedule).

## Where this sits

| | Phase | Window | Ends when |
|---|---|---|---|
| **0** | De-risk and freeze the contract | 24–25 Aug | Go/no-go signed, metric frozen, feasibility evidenced |
| 1 | Ingest, taxonomy, ledger | 26–28 Aug | Webhooks land verified and deduped; diagnosis runs; audit trail exists before the first decision |
| 2 | Decision engine and guardrails | 29–31 Aug | State machine owns every action; stopping rules in code; LLM behind a tested copy gate |
| 3 | Run the batch and measure | 3 Sept | `results/metrics.json` exists, against both baselines |
| 4 | Package and defend | 3 Sept | README generated from the artifact, limitations written, video cut, repo public |

**Revised 2 Sept 2026:** 5 Sept confirmed as the submission deadline, with the build to be
complete by **3 Sept**. Phases 1–4 compress into the remaining time. Recorded here as a
deliberate decision rather than something discovered late.

## Why this phase exists

Every item below can invalidate something built on top of it. `docs/razorpay-api-notes.md`
closes with five unverified assumptions and `docs/compliance-india.md` with seven; both are
load-bearing. The cost of checking them now is hours. The cost of discovering one on 2 Sept
is the submission.

Phase 0 writes no product code. It produces evidence, and one frozen decision.

---

## Gates

### 0.0 — Request the payment-link cap raise

Runs first because it is asynchronous and has vendor lead time.

- **Why** Test mode caps Payment Links at 30 per business. A 200-debt batch cannot create a
  link per failed payment.
- **Pass** Request filed and acknowledged.
- **Fail** No response by Phase 3 → sample the batch rather than cover it, and state the
  sampling in the README.
- **Budget** 10 min.

### 0.1 — BLOCKER: does Subscriptions work on a fresh, unactivated test account?

- **Why** Docs list only "Flash Checkout enabled" as a prerequisite and never confirm it.
  If Subscriptions is activation-gated, the mandate-recovery premise disappears entirely.
- **How** `POST /v1/plans`, then `POST /v1/subscriptions`, on a bare `rzp_test_` key from a
  fresh account. Capture the raw response either way.
- **Pass** A subscription is created and returns an id.
- **Fail** Scope changes the same day. Fallback wedge is recurring token debits
  (`POST /v1/payments/create/recurring`) or invoice follow-up — both weaker, both survivable
  if known on day 0.
- **Budget** 30 min. **This gate blocks every gate below it.**

### 0.2 — Do the error fields populate in test mode as they do in live?

- **Why** The diagnosis layer keys on `(error_code, error_reason)` plus `error_source` and
  `error_step`. Razorpay's own Dashboard buckets are not exposed via API, so rebuilding them
  is the differentiator — and it rests entirely on these fields being present.
- **How** Drive one card failure through hosted checkout, then `GET /v1/payments/:id` and
  inspect all five error fields. Log which spelling actually arrives (`insufficient_fund`
  vs `insufficient_funds` — the docs use both).
- **Pass** Fields populated and consistent with the published taxonomy.
- **Fail** Seed the taxonomy synthetically and say so plainly in the README. A stated
  simulation is defensible; an implied one is not.
- **Known cost** Specific decline reasons require driving the checkout UI, not a server
  call. UPI offers only `success@razorpay` / `failure@razorpay` — one generic reason.
- **Budget** 45 min.

### 0.3 — Does the halt ladder reproduce?

- **Why** The halted population *is* the recovery batch. No halted subscriptions, nothing
  to measure.
- **How** Dashboard "Charge this now" → failure, four consecutive times. Confirm
  `auth_attempts` increments, the next charge advances by a day, and state reaches `halted`.
  Confirm `subscription.pending` and `subscription.halted` fire.
- **Pass** Four failures produce `halted`, and both events arrive.
- **Fail** Construct the cohort synthetically. The decisioning layer is unaffected, but the
  demo's provenance changes and must be stated.
- **Budget** 45 min.

### 0.4 — Can webhooks be received at all?

- **Why** Beyond plumbing: `payment.failed` followed by `payment.captured` for the same
  transaction is one of the strongest architecture arguments available. It cannot be
  demonstrated if events never arrive.
- **How** zrok tunnel, ports 80/443 only (ngrok, localhost and webhook.site are blacklisted).
  Dashboard OTP `754081`. Verify HMAC-SHA256 over the **raw** body, dedup on
  `x-razorpay-event-id`, queue-and-ACK inside the 5s timeout.
- **Pass** A signed event is received, verified, deduped and acknowledged under 5s.
- **Fail** Fall back to polling `GET /v1/payments` and document the lost ordering evidence.
- **Budget** 60 min.

### 0.5 — Does `notify_by` deliver, or only return `{"success": true}`?

- **Why** Determines whether the contact leg is real or simulated. Either is defensible.
  Implying the former while doing the latter is not.
- **How** `POST /v1/payment_links/:id/notify_by/:medium`, then check for actual delivery.
- **Pass either way** — the output is a sentence in the README, not a go/no-go.
- **Budget** 20 min.

### 0.6 — Pull the RBI circulars from the primary source

- **Why** Every RBI claim in `docs/compliance-india.md` is currently secondhand via taxguru,
  TeamLease and law-firm commentary. Multiple independent sources agree, but none is the
  regulator. A panel asking "where did you read that" needs a primary answer.
- **How** Retrieve RBI/2026-2027/223–231 (6 Aug 2026, effective 1 Jan 2027) from rbi.org.in
  directly. Reconcile paragraph numbers against what the notes assert.
- **Pass** Primary text archived under `results/phase0/`.
- **Fail** Downgrade every RBI citation to "reported by, not verified against, the
  regulator" wherever it appears.
- **Budget** 30 min.

### 0.7 — Freeze the metric and the holdout

The only gate producing a decision rather than evidence, and the only one needing no API
access.

- **Fix in writing, before any result is visible:** the assignment unit; the randomisation
  seed; holdout share; the contact-cost model per channel; the definition of a recovered
  rupee (and how partial or late payment counts); the attribution window; and both
  baselines — do-nothing, and Razorpay's T+0…T+3 ladder reimplemented faithfully.
- **Why now** Choosing any of these after seeing outcomes invalidates the headline number,
  and it is the single easiest thing for a panel to catch.
- **Budget** 60 min.

---

## Exit criteria

Phase 0 is complete when all of the following exist:

1. `docs/phase-0-findings.md` — one line per gate: pass, fail, or fallback taken.
2. `results/phase0/` — **raw API responses as committed evidence**, not claims. The repo's
   honesty rule is that no figure appears in prose unless an artifact generates it; the same
   standard applies to feasibility.
3. A frozen metric definition from 0.7, committed, and thereafter changed only by an explicit
   amendment recording what changed and why.
4. A signed go/no-go below.

## Explicitly not in Phase 0

No product code. No state machine, no LLM integration, no message composition, no cohort
generation beyond what a gate needs to answer its own question. Work begun before 0.1
resolves is work that may be discarded.

## Assumption — CONFIRMED 2 September 2026

`CLAUDE.md` states *"Applications close 5 September 2026."* ~~This plan reads that as the
**submission** deadline...~~ **Confirmed by Divyansh, 2 Sept 2026: 5 September is the
SUBMISSION deadline**, and the build is to be complete by **3 September** to leave two days
of buffer. The plan's original reading was correct; no phase stretches.

**Working deadline: 3 Sept 2026.** Phases 1–4 must therefore compress into the remaining time,
and that compression is a deliberate, recorded decision rather than something discovered late.

## Gate results — all eight resolved, 2 September 2026

Full detail in [`docs/phase-0-findings.md`](phase-0-findings.md); every claim points at an
artifact in `results/phase0/`.

| Gate | Result |
|---|---|
| 0.0 activation + cap raise | **PASS** — ticket #20706328 filed and acknowledged |
| 0.1 Subscriptions blocker | **FAIL** — gated; fallback (seeded simulator) declared and taken |
| 0.2 error-field shape | **PARTIAL PASS** — shape observed; reason vocabulary must be documentation-derived |
| 0.3 halt ladder | **VOID** by 0.1 — reimplemented as baseline arm B |
| 0.4 webhook receive path | **PASS** — Razorpay-signed event received, verified, logged |
| 0.5 `notify_by` delivery | **PASS with caveat** — returns success; delivery unverified |
| 0.6 RBI primary sources | **PASS** — 3 citations corrected, 1 claim withdrawn |
| 0.7 metric + holdout | **FROZEN** — committed at `8d14dbe`, before any result existed |

## Approval

- [x] Phase 0 approved as written
- [x] **5 Sept interpretation confirmed — SUBMISSION deadline.** Build complete by 3 Sept.
- [x] Go/no-go recorded after gates run: **GO** — given by Divyansh, 2 September 2026.

**Recommendation: GO.** The premise as originally written is dead — Subscriptions is gated,
so the mandate lifecycle cannot be observed. But the pivot was taken on day 0 and everything
above the cohort interface is unaffected: the decisioning layer, the guardrails, the audit
trail and the holdout measurement never touched the Subscriptions API. The metric is frozen,
the compliance base is verified against the regulator, the receive path is proven against real
Razorpay traffic, and the model is chosen on evidence. What remains is building, not
de-risking.

Approved by: **Divyansh Gupta**    Date: **2 September 2026**

**Phase 0 is CLOSED.** All eight gates resolved, evidence committed, metric frozen
at `8d14dbe` before any result existed. Phase 1 begins — see `docs/phase-1.md`.
