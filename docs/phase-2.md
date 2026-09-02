# Phase 2 — Decision engine and guardrails

**Branch:** `feat/phase-2-decision-engine`, cut from `feat(v1)` at `890f972`.
**Status:** planned. Working deadline for the whole build is **3 September 2026**.

## Where this sits

| | Phase | Ends when |
|---|---|---|
| 0 | De-risk and freeze the contract | ✅ CLOSED — GO signed, metric frozen at `8d14dbe` |
| 1 | Ingest, taxonomy, ledger | ✅ CLOSED at `890f972` — 25 tests green, arm B validated at 23.6% |
| **2** | **Decision engine and guardrails** | A deterministic state machine owns every action; stopping rules enforced in code and logged; the LLM boxed behind a copy gate that is **demonstrated** catching a real violation |
| 3 | Run the batch and measure | `results/metrics.json` — net incremental rupees vs a randomised holdout |
| 4 | Package and defend | README generated from the artifact, limitations written, video cut, repo public |

## The one sentence this phase has to earn

> **An LLM must never decide whether to contact someone about money.**

Everything below is the machinery that makes that true rather than asserted. The state machine
decides *whether, when and on what channel*. The LLM only ever gets asked to phrase a message
the machine has already committed to sending, and to read a reply the machine will then
interpret with its own rules.

---

## Work items

### 2.1 — Versioned policy configuration

`engine/policy.py`

- Every tunable in one place with an explicit **version string**, pinned into each ledger
  record at write time. Phase 1 already writes `policy_version`; this gives it something real
  to point at.
- **Pass** Changing a rule produces a new version, and old decisions still resolve to the rule
  that governed them.

### 2.2 — Guardrails, enforced in code and logged

`engine/guardrails.py`

Each check returns a pass/fail plus the reason, and **both outcomes are recorded** — Phase 1's
ledger already logs `rules_passed` alongside `rules_fired` precisely so a cleared check is
evidence that it ran.

| Guardrail | Rule | Provenance — stated exactly |
|---|---|---|
| Contact window | **08:00–19:00 IST**, per-tenant override only in the *narrowing* direction | **Product invariant.** RBI 454Y(4) binds Regulated Entities, not a plain merchant. We adopt it as the de-facto standard, which also makes the product sellable to a lender unchanged. |
| Frequency cap | 7 contacts / 7 days per debt; 1 WhatsApp Utility / 24h; 1 SMS / 24h | **Policy choice.** 7-in-7 is US Reg F (12 CFR 1006.14(b)) and has **no force in India**. RBI names no number — only "excessively calling / messaging" (454Z(4)). **Never cite a numeric cap to RBI.** |
| Cap applies to messaging | Not just voice | RBI 454Z(4) covers *"calling / messaging"* — verified at source in gate 0.6. |
| Opt-out | Hard stop, all channels | **Statutory.** DPDP s.7(a) is conditional on the person not having objected; the basis evaporates on objection. |
| Dispute raised | Hard stop, route to a human | **Policy choice, NOT an RBI requirement.** Gate 0.6 found the "454J grievance hard stop" claim unsupported — 454J is the code-of-conduct paragraph and "frivolous" appears nowhere. Correcting this is one of Phase 0's most valuable outputs; re-introducing the wrong citation here would waste it. |
| Bereavement / hardship | Hard stop | Policy choice. The model bake-off found `ministral-3` classified *"my father passed away last week"* as `promise_to_pay` — the exact input where a wrong answer is unforgivable. |
| Payment received | Hard stop | See 2.3. |
| Third parties | Never contact anyone but the borrower | RBI 454Y(1). Zero exceptions. |

- **Pass** Each guardrail has a test that proves it *blocks*, not merely that it exists.

### 2.3 — Pre-action payment-state re-check

`engine/guardrails.py`, using Phase 1's `EventStore`.

Razorpay's webhooks are at-least-once and unordered: `payment.failed` can be followed by
`payment.captured` for the same transaction. Phase 1 already resolves state by **precedence
rather than arrival order** so a late failure cannot un-pay someone.

- **Pass** No action is ever emitted without an immediately preceding state re-read, and that
  read is itself in the ledger. Otherwise *"we never dun someone who has already paid"* is
  unfalsifiable.
- This is a **measured guardrail that must be zero**, not a metric with a value.

### 2.4 — The state machine and the escalation ladder

`engine/machine.py`

- Ladder **terminates and never loops**: WhatsApp Utility → SMS service → human agent →
  written notice → cease automated contact.
- **Tone must not escalate with attempt count.** Repetition is capped; pressure is not a lever.
- Routes on the Phase 1 diagnosis: `NEEDS_NEW_INSTRUMENT` is never retried (a dead card cannot
  be charged — Phase 1 measured 2,088 incumbent retries wasted exactly here);
  `DO_NOT_CONTACT` causes are dropped; `NEEDS_FUNDS` is a timing problem, not a persuasion one.
- **Pass** Every transition is deterministic and reproducible from the ledger alone.

### 2.5 — Cost-sensitive decision rule

`engine/policy.py`

Act only when `P(incremental recovery) × amount_due > cost_of_action`, using the cost model
frozen in `docs/metric-definition.md`.

The gate-0.7 finding that shapes this: at ₹0.17 a WhatsApp message, a ₹1,000 debt breaks even
at **0.017 pp** of uplift — so **money is almost never the binding constraint on messaging.**
The binding constraints are permission and patience. Only the human-agent leg genuinely bites,
at **1.53 pp**. The engine must therefore be guardrail-driven with a cost gate on escalation,
not a budget optimiser, and the README should say so plainly.

### 2.6 — LLM composer, on local Ollama

`llm/composer.py` — `llama3.1:8b`, no hosted API, no key, no data leaving the machine.

- The LLM receives facts the machine has already decided to send, and returns wording.
- **The payment link is injected deterministically after generation.** The bake-off caught all
  three models inventing placeholder URLs, and `ministral-3` hallucinating a concrete date
  (*"failed on 12/05/2024"*). A model must never generate a payment URL.
- **Pass** A composed message contains the real link, no invented facts, and is under the SMS
  limit — or the gate rejects it and a deterministic template is used instead.

### 2.7 — Reply parser

`llm/parser.py`

- Extracts **intent** and the **date phrase only** (`"the 5th"`, `"next monday"`). **Code
  resolves the date**, because the bake-off showed every model got dates wrong and `qwen3`
  produced a promise-to-pay date *in the past*. A deterministic engine must never accept a
  scheduling date from a model that can do that.
- **Pass** Relative-date resolution is a pure function with its own tests, and bereavement
  never resolves to `promise_to_pay`.

### 2.8 — The copy gate

`llm/copy_gate.py`

The regulatory core, not a style checker. Under TRAI's mixed-content rule, promotional content
inside a service message makes the **whole message** promotional and inherits consent, DND and
time-band obligations. Fabricated urgency and repeated nudging are two named patterns under the
CCPA Dark Patterns Guidelines 2023.

- Blocks: discounts and offers, false urgency, scarcity, threats and confirm-shaming,
  fabricated facts, invented dates, missing/placeholder links.
- **Must be demonstrated catching a real violation.** The canonical fixture is `ministral-3`'s
  actual output from the bake-off — *"Limited-time bonus: 10% extra data on next recharge.
  Offer valid until 31st July."* — a genuine model-generated violation at 165 characters, not a
  hand-written strawman.
- **Harden the v0 lexicon**, which the bake-off exposed as flawed in both directions: it misses
  `"suspension"` (the regex requires a word boundary after `suspend`), and it false-positives on
  refusal text where the model quotes the request back.
- **Pass** Every rejection is logged with the matched term, so a flag is auditable rather than
  a boolean to be trusted.

### 2.9 — Tests

Guardrail metrics are **assertions that must be zero**, never values to report:

- contacts outside 08:00–19:00 IST — **0**
- cap breaches — **0**
- contacts after a stop signal — **0**
- actions without an immediately preceding state re-check — **0**
- decisions missing from the ledger — **0**

Plus: the copy gate catches the real ministral output; the ladder terminates; a dead instrument
is never retried; tone does not escalate with attempt count.

---

## Exit criteria

1. `pytest` green, including the five zero-assertions above.
2. The copy gate is shown rejecting a genuine model-generated violation, with the match logged.
3. Every decision — including every refusal — lands in the Phase 1 ledger, replayable.
4. Ollama is exercised for real; no mocked LLM in the headline path.
5. Committed on `feat/phase-2-decision-engine`, never `main`.

## Explicitly not in Phase 2

No batch run, no metrics, no README figures. The engine gets built and tested here; it gets
*measured* in Phase 3, against the definition frozen before any of it existed.

## The honest risk

The LLM is the slowest and least reliable component, and it sits on the demo's critical path.
Cold start is 36 s; warm inference is 0.6 s. Mitigation: warm the model at start, keep it
alive, and make **every** LLM call fall back to a deterministic template on timeout or gate
rejection. The system must degrade to templates and keep collecting money, never stall.
