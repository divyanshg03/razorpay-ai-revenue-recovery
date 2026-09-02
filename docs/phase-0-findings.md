# Phase 0 — findings

Run 24 Aug 2026 against test key `rzp_test_TTag…` (test mode confirmed by prefix).
Raw responses archived under `results/phase0/`. Every claim below is reproducible from
those files; nothing here is asserted without one.

## Verdict: NO-GO on the premise as written. Pivot required.

`CLAUDE.md` scopes this project to *"failed UPI Autopay / eMandate recurring charges"* and
builds on Razorpay Subscriptions. **Subscriptions is not enabled on this test account.**
The day-1 blocker fired on day 0, which is exactly what it was written to do.

---

## Gate 0.1 — BLOCKER — **FAIL**

`GET /v1/plans` and `GET /v1/subscriptions` return **HTTP 401** `{"error": "Unauthorized"}`.

Three independent facts isolate this to product gating rather than bad credentials or a
transient fault:

| Check | Result | Rules out |
|---|---|---|
| `GET /v1/payments` with the same key | **200** | Bad credentials |
| Ten other endpoints with the same key | **200** | Bad credentials |
| `/plans` retried 3× over ~2s | **401** each time | Transient fault |

### Capability scan — same key, one call each

| Endpoint | HTTP | Status |
|---|---|---|
| `/payments` `/orders` `/customers` `/payment_links` | 200 | reachable |
| `/invoices` `/items` `/refunds` `/settlements` `/disputes` | 200 | reachable |
| `/virtual_accounts` | 400 *"requested URL was not found"* | not enabled |
| **`/plans`** | **401** | **gated** |
| **`/subscriptions`** | **401** | **gated** |

Evidence: `results/phase0/0.1-capability-scan.json`, `0.1-scan-*.json`.

## What the failure costs

Lost with Subscriptions:

- The subscription lifecycle and its `auth_attempts` counter.
- The T+0…T+3 retry ladder terminating in `halted` — **which was the baseline this project
  set out to beat.** It now has to be reimplemented from scratch rather than observed.
- The Dashboard "Charge this now" failure simulator — the clean, documented way to
  manufacture a halted cohort. `docs/razorpay-api-notes.md` called this *"a genuinely good
  simulator"*; it is unavailable.

Also unavailable: **`POST /v1/payments/create/recurring`** returns 400 *"The requested URL
was not found on the server"* — byte-identical to the response for `/virtual_accounts`,
which is definitively not enabled. So the recurring **charge** leg is gated too.

## What survives — verified, not assumed

Mandate **registration** is fully reachable. Both created successfully:

| Probe | HTTP | Note |
|---|---|---|
| `POST /orders` `method=emandate` + netbanking token | **200** | order created with embedded token; `expire_at` must be < 40 years |
| `POST /orders` `method=upi` + autopay token | **200** | minimum amount 100 paise; 0 is rejected |
| `POST /customers` | 200 | |
| `POST /orders` (plain) | 200 | |
| `POST /payment_links` | 200 | contact must not be repeating digits |
| `POST /invoices` | 200 | |

Evidence: `results/phase0/0.1i-emandate-order-valid.json`, `0.1j-upi-autopay-100.json`,
`0.5a-payment-link.json`, `0.5b-invoice.json`.

**So: mandates can be registered but not charged, and their lifecycle cannot be observed.**

## Gate 0.5 — **answered early, PASS with a caveat**

`POST /v1/payment_links/:id/notify_by/sms` → **200 `{"success": true}`** and nothing else.
No delivery receipt, no message id. The README must say *"returns success; delivery
unverified in test mode"* and must not imply a message was received.

Evidence: `results/phase0/0.5c-notify-by-sms.json`.

## Remaining gates — run 31 Aug 2026

| Gate | Status | Evidence |
|---|---|---|
| 0.0 activation + cap raise | **PASS — filed and acknowledged** | Ticket **#20706328**, raised 2 Sept 2026 via Dashboard → Help & Support. Razorpay SLA: 4–8 business hours |
| 0.2 error-field shape | **PARTIAL PASS** | `results/phase0/0.2c-error-fields.json` — shape confirmed, reason vocabulary not exercisable |
| 0.3 halt ladder | **void** | depended entirely on Subscriptions; reimplemented as baseline arm B, not retried |
| 0.4 webhook receive path | **PASS (complete)** | `0.4-webhook-receiver-local.json`, `0.4b-webhook-zrok-tunnel.json`, `0.4d-razorpay-originated-event.json` |
| 0.6 RBI primary sources | **PASS** | `results/phase0/0.6-rbi-primary-sources.md` |
| 0.7 freeze metric + holdout | **FROZEN** | `docs/metric-definition.md`, `results/phase0/0.7-power-and-cost-check.json` |

### 0.6 — the gate earned its keep

rbi.org.in **loads**, contradicting the earlier note that it would not. All nine circulars
(RBI/2026-27/223 – /231, 6 Aug 2026, effective 1 Jan 2027) and RBI/2022-23/108 were read at
source. Three paragraph citations in `docs/compliance-india.md` were wrong (the 08:00–19:00
rule is **454Y(4)**, not 454T.2; borrower-only contact is **454Y(1)**, not 454T.1; "excessively
calling" is **454Z(4)**, not 454U.3), and the section numbering turned out to be per-entity —
Section L / 454A only covers Commercial Banks and SFBs, while NBFCs are Section J / 100A.

The substantive catch: **the "lodged grievance is a hard stop (454J)" claim is unsupported.**
454J is the code-of-conduct paragraph; no clause in the Commercial Banks, SFB or NBFC directions
bars recovery while a complaint is pending, and "frivolous" appears nowhere. The dispute-freeze
stopping rule stays in the design but is now labelled a **policy choice**, like the 7-in-7 cap.
Citing a regulator for a rule it does not contain, in a submission whose pitch is that its
compliance claims are checkable, is the kind of error a panel opens with.

### 0.4 — PASS, in three stages: receiver, tunnel, then Razorpay itself

`spikes/webhook_receiver.py` + `spikes/test_webhook_receiver.py`, stdlib only. All seven checks
pass: valid event accepted and **ACKed in 4.8 ms** against the 5 s budget while the worker
deliberately takes 1.5 s; duplicate `x-razorpay-event-id` ignored; tampered body rejected;
missing signature rejected; distinct id still accepted after a duplicate; exactly 2 events
processed from 6 requests.

The check worth showing a panel: a body that is parsed and **re-serialised** — semantically
identical, 207 bytes becoming 223 — fails signature verification. That is *why* Razorpay says
"do not parse or cast the webhook request body", demonstrated rather than quoted.

**Public tunnel leg — added 31 Aug 2026, also PASS.** zrok v2.0.4 installed to `tools/zrok/`
(gitignored), environment enabled, public share opened to the local receiver. All four checks
pass over the real internet path: signed event queued, duplicate `x-razorpay-event-id` ignored,
tampered body rejected, re-serialised body rejected.
Evidence: `results/phase0/0.4b-webhook-zrok-tunnel.json`.

**The finding that matters, and it validates the architecture.** Round-trip through the tunnel
is **~2.5–2.7 s**, against a local ACK of 4.8 ms — roughly 500× slower, and Razorpay's ACK
budget is **5 s**. The queue-and-ACK design is what keeps this safe: the handler verifies,
enqueues and returns immediately, so tunnel latency is the only cost. Had we processed inline
— the worker deliberately takes 1.5 s — we would sit at ~4.2 s and be one network hiccup from
a timeout, which Razorpay answers by *redelivering*. The dedup on `x-razorpay-event-id` is
therefore not belt-and-braces; on a tunnelled endpoint it is load-bearing.

**Reserved URL — done.** zrok v2 replaces v1's `reserve` with named shares: `create name`
reserves a name in a namespace, then `share public … -n <namespace>:<name>` binds a backend to
it. The endpoint is now stable across restarts:

```
https://<zrok-share-name>.shares.zrok.io/webhook
restart:  zrok2 share public 9090 --headless --force-local -n public:<zrok-share-name>
```

Re-verified on the reserved URL: 4/4 pass, **max round-trip 2.258 s, headroom 2.742 s** against
the 5 s ACK budget.

**Razorpay-originated event — RECEIVED 2 Sept 2026. Gate 0.4 closes.**
Event `TX3rOCAcRmbwOd` (`payment.failed`, `pay_TX3rF1G3TyjbEX`) arrived, verified against the
Dashboard secret, and was logged. Evidence: `results/phase0/0.4d-razorpay-originated-event.json`.
End-to-end latency 8 s from payment to receipt; our own ACK is ~1.5–2.7 s, inside the 5 s budget
precisely because the handler verifies, enqueues and returns rather than processing inline.

### What getting there uncovered — three things worth more than the gate itself

**1. A wrong secret is invisible without durable rejection logging.** For roughly 40 minutes
Razorpay delivered 18+ events and *every one* was rejected `invalid signature`, while the
endpoint reported perfectly healthy and the event log stayed empty. Cause: the secret pasted
into the Dashboard had surrounding whitespace. Rejections were tracked in memory only, so
*"Razorpay never called"* and *"Razorpay called and we rejected it"* — opposite problems — were
indistinguishable. Durable rejection logging to `runtime/webhook-rejected.jsonl` exposed it
immediately. **Phase 1's receiver must keep this.**

**2. Peer IP is useless behind a tunnel.** The first attempt to find Razorpay traffic filtered
for a non-local peer address and found nothing. zrok proxies to localhost, so `peer` is *always*
`127.0.0.1`. The `User-Agent: Razorpay-Webhook/v1` is the only reliable discriminator.

**3. Razorpay signs an event ONCE, at creation — retries replay the identical signature.**
Verified: event `TX3YT81HtLiSK6` delivered at 11:16:32 and 11:21:56 carried a byte-identical
signature, and 14 whitespace/quoting variants of the corrected secret failed against it.

> **Operational consequence, absent from Razorpay's docs:** rotating a webhook secret
> **permanently strands every event already created under the old one.** They retry for 24 h
> and can never verify. Fixing the secret did not rescue the queue; only a new payment did.

**Retry ladder observed live** rather than quoted: gaps of 6 s → 11 s → 22 s → 44 s → 67 s →
97 s → 190 s, with the same `event_id` redelivered up to four times. Dedup on
`x-razorpay-event-id` is load-bearing, not decorative.

**Error fields arrive inside the webhook payload**, identical to `GET /payments/:id`. The
diagnosis layer can classify straight off the event — but the pre-action state re-check still
needs a live API call, because `payment.failed` can be followed by `payment.captured` for the
same transaction.

### 0.2 — PARTIAL PASS. Shape confirmed; vocabulary is not observable.

Evidence: `results/phase0/0.2c-error-fields.json`. Five payments driven through hosted
checkout (1 captured, 4 failed, card and wallet).

**All five error fields populate**, and two of them discriminate usefully:

| Field | Card failure | Wallet failure |
|---|---|---|
| `error_code` | `BAD_REQUEST_ERROR` | `BAD_REQUEST_ERROR` |
| `error_source` | `gateway` | `issuer` |
| `error_step` | `payment_authorization` | `payment_authorization` |
| `error_reason` | `payment_failed` | `payment_failed` |

`BAD_REQUEST_ERROR` carrying a customer-side decline confirms `docs/razorpay-api-notes.md`:
**do not classify on `error_code`.**

**What does not work.** Test card `4100 2800 0008 0001` is documented to map to
`insufficient_fund`. It returned the generic **`payment_failed`** instead — selecting "failure"
on the test-mode success/failure screen overrides the card's reason mapping. Every failure we
can manufacture yields the same single value.

> **Consequence for the build:** the ~121-value `error_reason` taxonomy **cannot be observed**
> from test mode. The diagnosis layer must seed its reason vocabulary from Razorpay's published
> enumeration and declare it **documentation-derived, not observed**. The field *shape* above is
> genuinely observed and may be claimed as such. This is gate 0.2's stated fallback, taken
> knowingly rather than discovered late.

The `insufficient_fund` vs `insufficient_funds` spelling contradiction in the docs stays
**unresolved** — neither spelling ever arrived.

### 0.7 — frozen before any result was visible

`docs/metric-definition.md`. Three arms, not two: do-nothing (A), Razorpay's T+0…T+3 ladder
reimplemented (B), our engine (C), at 20/20/60 over 5,000 customers. **Primary comparison is
C vs B** — beating do-nothing proves nothing; the question is whether we beat what Razorpay
already ships. Randomisation is per **customer**, not per payment, so treating one debt cannot
contaminate another. Seed `20260905`, sha256 bucketing, sticky across re-entry. MDE 4.06 pp on
the primary comparison, computed by `scripts/gate_0_7_power.py`, not asserted.

Costs are cited list prices taken at the **expensive** end of each range (WhatsApp Utility
₹0.145, SMS ₹0.18, agent call ₹13, all +18% GST). The immediate consequence is worth carrying
into the pitch: at ₹0.17 a message, a ₹1,000 debt breaks even at **0.017 pp** of uplift — so
**cost is essentially never the binding constraint on messaging; permission and patience are.**
Only the human-agent leg genuinely bites, at 1.53 pp. That is the honest answer to "why not
message everyone constantly", and it is why this system is built around guardrails rather than
a budget optimiser.

---

## The pivot, and why it is smaller than it looks

**The decisioning layer is unaffected.** The state machine, the 08:00–19:00 IST invariant,
contact caps, stopping rules, the copy gate, the append-only audit trail and the
holdout-based measurement all sit *above* whatever produces failed payments. None of them
touch the Subscriptions API.

What changes is the **cohort source** — one component, at the bottom of the stack.

Three options, in the order they should be pursued:

1. **File the activation request now** (alongside 0.0). Costs minutes, may unblock
   everything. But with 12 days to 5 Sept, the schedule cannot depend on the reply.
2. **Reimplement the retry ladder rather than observe it.** The T+0…T+3 → `halted` baseline
   was always going to be reimplemented "faithfully" for comparison — that requirement is
   unchanged; only the observed arm disappears.
3. **Generate the cohort from a seeded simulator behind an interface**, with the real
   Orders/Payments/Payment Links/Invoices path wired for the live demo slice. Declared
   plainly in the README as a simulator, per the project's own honesty rules.

**Decided 24 Aug 2026: option 3, with option 1 filed in parallel.** The cohort comes from a
deterministic seeded simulator behind an interface; the verified-working Orders / Payment
Links / Invoices path drives a live demo slice. If activation lands, the source swaps behind
the same interface with no change above it. It is also the more defensible architecture: a
source-agnostic recovery engine is a stronger claim than one welded to a single vendor's
subscription product.

### Root cause, confirmed against Razorpay's docs

This is not a configuration error and no key, header or parameter change resolves it.
Razorpay documents Subscriptions as **an on-demand feature requiring a support request to
activate on the account**. The prerequisite list in the integration guide mentions only
Flash Checkout and does not state this — which is exactly why the day-1 blocker existed.
After activation, Subscriptions → Settings → enable the card payment method is also required.

## Test data created during this run

Two customers, three orders (emandate, UPI Autopay, plain), one payment link, one invoice.
Test mode only, no cleanup required. Ids are in the evidence files.

## Decision record

- [x] **Cohort source chosen** — seeded simulator behind an interface, real API for the
      live demo slice. Decided 24 Aug 2026.
- [x] **LLM provider constrained** — no Anthropic APIs or keys anywhere in the build. The
      LLM role (compose wording, parse inbound replies) runs on a **local model via Ollama**.
      Decided 25 Aug 2026, recorded as hard rule 6 in `CLAUDE.md`. Note the AI claim for this
      track rests on the decisioning layer — propensity, uplift against the holdout,
      cost-sensitive thresholds — which needs no inference API at all.
- [ ] **Activation request filed** for Subscriptions + recurring payments + payment-link
      cap raise. One ticket covering all three, **drafted ready to send** in
      `docs/support-ticket-draft.md`. **Owner: Divyansh — this is the only Phase 0 item an
      agent cannot do.**
- [x] **Gate 0.7** — metric and holdout **frozen 31 Aug 2026** in `docs/metric-definition.md`,
      before any batch was run. Git ancestry is the proof and a test will assert it.
- [x] **Gate 0.6** — RBI circulars read at source 31 Aug 2026, archived, and three citation
      errors plus one unsupported claim corrected in `docs/compliance-india.md`.
- [~] **Gate 0.4** — receiver logic proven locally 31 Aug 2026 (7/7). **zrok tunnel and
      dashboard registration still outstanding**; no Razorpay-originated event has been
      received yet.
- [ ] **`CLAUDE.md` scope line updated** — it still states the premise as "failed UPI
      Autopay / eMandate recurring charges", which this account cannot execute end to end.

**Phase 0 status: substantially complete, not closed.** 0.6 and 0.7 are done; 0.4 is partial;
0.2 stays blocked without browser automation; 0.3 is void by design. What remains before the
go/no-go in `docs/phase-0.md` can be signed:

1. File the support ticket (you).
2. Run the zrok leg of 0.4, or accept it as partial and say so in the README.
3. Decide 0.2 — either drive one browser checkout failure to confirm the error-field shape, or
   seed the taxonomy from Razorpay's published 121-reason enumeration and declare it as
   documentation-derived rather than observed.
4. Answer the open question in `docs/phase-0.md`: is 5 Sept the submission deadline or the
   entry deadline?

**Schedule reality, recorded because the honesty rules apply to the plan too.** The Phase 0
window was 24–25 Aug and it is now 31 Aug, with Phases 1 and 2 nominally already spent and no
product code written. Against the original plan this is roughly six days behind with five
remaining. Phases 1–3 need rescoping to fit, and that rescoping should be a deliberate written
decision rather than something discovered on 4 September.
