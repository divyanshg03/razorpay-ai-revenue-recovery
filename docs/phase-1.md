# Phase 1 — Ingest, taxonomy, ledger

**Status: in progress.** Phase 0 closed with GO on 2 Sept 2026. Working deadline for the whole
build is **3 September**.

## Where this sits

| | Phase | Ends when |
|---|---|---|
| 0 | De-risk and freeze the contract | ✅ **CLOSED** — GO signed, metric frozen at `8d14dbe` |
| **1** | **Ingest, taxonomy, ledger** | Cohort generates behind an interface; diagnosis runs; **the audit trail exists before the first decision is ever made** |
| 2 | Decision engine and guardrails | State machine owns every action; stopping rules enforced in code; LLM boxed behind a tested copy gate |
| 3 | Run the batch and measure | `results/metrics.json` exists — net incremental rupees vs a randomised holdout, against both baselines |
| 4 | Package and defend | README generated from the artifact, limitations written, video cut, repo public |

## Why this phase exists, and why it is ordered this way

**The audit trail is built before the decision engine, deliberately.** The design commitment is
that *every decision is reconstructable under the policy version that applied at the time*.
Retrofit a ledger after the engine exists and that becomes a claim you cannot honour — you will
have decisions with no record of the rules in force when they were taken. Building it first
costs nothing and makes the claim true by construction.

Everything in Phase 1 is **plumbing that carries evidence**. No decisions are taken here. The
engine that decides comes next, and it will only be able to act *through* this layer.

---

## Work items

### 1.1 — Package skeleton ✅ DONE

`src/recovery/` with `pyproject.toml`, src-layout, pytest configured.

**Zero runtime dependencies**, deliberately. The decisioning layer is ordinary deterministic
Python, and the only external call is to a local Ollama over HTTP, which `urllib` covers. Every
dependency is another way a live demo breaks on a machine that is not this one.

### 1.2 — Cohort simulator behind a source interface ✅ DONE

`cohort/source.py` (the interface), `cohort/simulator.py`, `cohort/PARAMETERS.md`.

- **Pass** A deterministic seeded cohort generates, and swapping in a Razorpay-backed source
  would require no change above the interface.
- **The design decision that matters:** there is **no per-arm recovery dial**. Nothing in the
  simulator knows which arm a customer is in. It models customer behaviour only — money is
  available in a window after payday, a silent retry needs money but not attention, a contact
  cannot conjure money, and repeat contacts decay. Any lift the engine shows has to be earned
  against those mechanics. A simulator with a "arm C recovers X%" parameter proves nothing; it
  replays its own assumption back as a result.
- **Every parameter is cited and source-graded** in `PARAMETERS.md`, including the load-bearing
  one: NPCI data showing UPI Autopay success fell from ~50% (Jan 2024) to ~30% (Nov 2025), with
  insufficient balance as the dominant cause.

### 1.3 — Failure-cause diagnosis ✅ DONE

`diagnosis/taxonomy.py`.

- Classifies on `(error_code, error_reason)` + `error_source`, **never on `error_code` alone** —
  it has three values and `BAD_REQUEST_ERROR` carries most customer-side declines.
- Rebuilds Razorpay's four Dashboard buckets (not exposed via API), then adds the layer that
  actually earns its place: **actionability**. Attribution says whose fault it was;
  actionability says what to do. `insufficient_funds` is a timing problem a well-timed silent
  retry fixes for free; `card_expired` can *never* be fixed by retrying.
- **Provenance stated in the module docstring**: the reason vocabulary is
  **documentation-derived, not observed**, because gate 0.2 found test mode collapses every
  manufactured failure to the generic `payment_failed`. Unmapped reasons are counted and
  reportable rather than silently bucketed.

### 1.4 — Append-only audit ledger ⬜ NEXT

`ledger/audit.py`.

- **Pass** Every action and every *declined* action is written before its outcome is known,
  with the policy version pinned at that moment; the file is append-only and tamper-evident;
  a decision can be replayed from it alone.
- **Fields** per `docs/compliance-india.md`: `event_id`, `timestamp_IST`, `debtor_ref`,
  `channel`, `message_class`, template/consent refs, DND and time-window check results,
  attempt counters, **which stopping rules fired and which passed**, model/prompt/policy
  version, exact rendered content, delivery status, inbound response, outcome.
- Plus a separate **consent and objection ledger** — DPDP s.6(10) puts the burden of proof on
  us, so consent, objection and propagation must be immutable logged events.
- **Recording what was NOT done matters as much as what was.** A system that only logs its
  actions cannot evidence its own stopping rules, which is the thing the track brief asks for.

### 1.5 — Promote the webhook receiver to product code ⬜ NEXT

`ingest/webhook.py`, from `spikes/webhook_receiver.py`.

- **Pass** Signed events land, verify against the raw body, dedup, and ACK inside 5 s, with
  state surviving a process restart.
- **Must keep, all three learned the hard way in gate 0.4:**
  1. **Persistent dedup.** The spike's in-memory set dies with the process; Razorpay retries
     for 24 h, so the dedup window has to outlive a restart.
  2. **Durable rejection logging.** A wrong secret produced 18 rejected deliveries while the
     endpoint reported perfectly healthy — "never called" and "called and rejected" are
     opposite problems that look identical without it.
  3. **Raw-body verification.** Parsing and re-serialising changes the bytes and breaks the
     signature; this is demonstrated in the test, not asserted.
- Handle `payment.failed` followed by `payment.captured` for the same transaction.

### 1.6 — Tests ⬜ NEXT

- Cohort is reproducible: same seed, same cohort, across runs and process restarts.
- Arm B lands in the 20–30% band that published figures imply for un-timed automated retries
  (`PARAMETERS.md` §3). **If it does not, the world model is wrong and must be fixed before any
  arm-C number is looked at.** Asserted, not left to judgement.
- Diagnosis coverage is measured, and a dead instrument is never classified retryable.
- Ledger append-only property holds and a decision replays from the file alone.

---

## Exit criteria

1. `python -m pytest` green.
2. A cohort of 5,000 customers generates deterministically from seed `20260905`.
3. Diagnosis coverage reported as a measured number, not asserted.
4. The ledger exists and is exercised by tests **before any decision code is written**.
5. Committed on `feat(v1)`, never `main`.

## Explicitly not in Phase 1

No decisions. No state machine, no guardrail enforcement, no LLM, no message composition, no
batch run, no metrics. If a line of code in this phase chooses whether to contact someone, it
is in the wrong phase.

## What this sets up

Phase 2 can then be written as *pure policy*: the engine reads a diagnosis, consults the
guardrails, and writes its decision — including its refusals — to a ledger that already exists
and already works. That is what makes "every decision reconstructable" true rather than
aspirational.
