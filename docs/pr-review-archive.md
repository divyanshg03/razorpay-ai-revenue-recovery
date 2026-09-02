# Pull-request archive

The GitHub repository was deleted and recreated on 3 September 2026 to remove a real phone
number that survived a history rewrite inside GitHub's pull-request refs. Force-pushing
rewritten branches does not touch `refs/pull/*`, and those refs become fetchable to anyone
once a repository goes public, so the merged pull requests still carried the number after the
rewrite had cleaned every branch.

Recreating the repository was the only fast, complete remedy. It cost the pull-request
threads, so they are archived here rather than lost. This file is a record, not a substitute:
the review happened on GitHub, and this is what it said.

See amendments A5 and A6 in `docs/metric-definition.md` for the redaction and the rewrite.


---

## PR #1 — Phase 2: deterministic decision engine, guardrails, and a copy gate

**MERGED** · `feat/phase-2-decision-engine` → `feat(v1)` · opened 2026-09-02 · merged 2026-09-02

<details>
<summary>Description</summary>

Phase 2 of the Track 03 build: the deterministic decision engine, its guardrails, and the copy gate.

Base is `feat(v1)` (Phase 0 + Phase 1). Both branches were force-pushed after a history rewrite — see the last commit for why.

## The sentence this phase exists to make true

> **An LLM must never decide whether to contact someone about money.**

`engine/machine.py` has no import path to the model. It decides *whether*, *when* and *on what channel*; the composer is asked for wording only after a decision exists; the parser's reading of a reply is interpreted by rules, not by the model.

## Guardrails carry their provenance

This is where gate 0.6 pays off — each rule is labelled with what actually backs it:

| Rule | Status |
|---|---|
| 08:00–19:00 IST contact window | **product invariant** — RBI 454Y(4) binds regulated entities, not a merchant |
| 7 contacts / 7 days | **policy choice** — US Reg F, no force in India; RBI names no number |
| Cap covers messaging, not just calls | RBI 454Z(4), verified at source |
| Opt-out | **statutory** — DPDP s.7(a) is conditional on no objection |
| Dispute freeze | **policy choice** — gate 0.6 found the "454J grievance hard stop" claim unsupported |

Re-citing 454J here would have discarded the most valuable correction Phase 0 produced.

## The guardrail numbers are not self-reported

`evaluation/invariants.py` never imports the engine. It replays the audit ledger and re-derives every violation count from the records alone. Over a 1,500-customer run, all eight are zero:

- contacts outside the window · actions without a state re-check · contacts after opt-out/dispute/hardship · contacts after payment · cap breaches · contacts during a promise-to-pay · actions without a decision · ledger chain breaks

Not vacuous: the same run produced 2,119 opt-out refusals, 1,174 dispute refusals and 968 promise-to-pay silences. **Refusals outnumber actions roughly 3:1.**

## The copy gate catches a real violation

The fixture is `ministral-3`'s actual bake-off output, not a strawman:

> *"Limited-time bonus: 10% extra data on next recharge. Offer valid until 31st July."*

Rejected on four categories — discount/offer, false urgency, fabricated date, fabricated amount — plus a missing link and 165 chars. Two v0 flaws fixed: refusals now get their own verdict instead of being flagged as drift, and `suspension` is matched at the stem.

## Dates leave the LLM entirely

The parser extracts the customer's *phrase*; `resolve_date()` turns it into a date that is never in the past. Opt-out, dispute and hardship are decided by code before the model is consulted — a model labelled *"my father passed away"* as `promise_to_pay` in the bake-off, and that misclassification has no acceptable frequency.

## Cost tuning, measured

The human-call rung was 12% of sends and **91.7% of modelled spend** while recovery was flat (53.63% with it, 53.67% without — inside noise). Floor raised ₹500 → ₹2,000: cost down 77%, recovery unchanged.

Stated honestly in the code: the simulator treats a human call as just another message subject to fatigue decay, so it almost certainly **understates** that rung. That is why it was kept for larger debts rather than deleted.

## Also reverted, deliberately

An equal-touch ladder for SMS-only customers was built, measured, and reverted: the touch deficit was real (1.31 vs 1.94 contacts) but the recovery gap I first reported was **noise** (−0.76 pp, 95% CI [−3.73, +2.20] at n=8,000). It cost ₹774 per 1,500 customers for no measurable return. The decision record is in `policy.py`.

## Verification

- 78 tests green, including 5 zero-assertions and 2 tests against the real local model
- `llama3.1:8b` composes a gate-passing message in 0.52 s warm, never writing the link (code appends it)
- Gate 0.4 webhook spike still passes
- No keys, secrets, or PII in the tree

## Not in this PR

No batch run, no metrics. Phase 3 measures this against the definition frozen at `8d14dbe` — before any engine code existed.

</details>


### Review (COMMENTED) — copilot-pull-request-reviewer

### 🟡 Changes recommended

The composer’s fallback path returns a template `text` while keeping the copy-gate verdict from the rejected LLM candidate, which makes `ComposedMessage.gate` inconsistent with the returned message and risks incorrect downstream interpretation.

*Once you've addressed the issues Copilot identified, you can request another Copilot review.*

<details>
<summary>Pull request overview</summary>

Implements Phase 2 of the recovery system: a deterministic decision engine with explicit guardrails and full auditability, while boxing the LLM behind a copy gate so it can only generate wording (never decide whether/when to contact).

**Changes:**
- Added deterministic `RecoveryEngine` (policy + guardrails + state machine) and expanded the audit ledger to record inbound replies with replayable timestamps.
- Introduced LLM composer/parser modules with strict boundaries (no link generation, no date generation) and a regulatory-oriented copy gate.
- Added invariant checks that replay the ledger, plus Phase 2 end-to-end tests, evidence regeneration script, and supporting docs/artifacts.
</details>

<details>
<summary>File summaries</summary>

| File | Description |
| ---- | ----------- |
| tests/test_phase2.py | Phase 2 exit-criteria tests, including ledger-replay “zero assertions” and real-Ollama integration tests. |
| src/recovery/models.py | Adds `StopReason.NOT_WORTH_COST` for EV-gated refusals. |
| src/recovery/llm/parser.py | New inbound reply parsing with code overrides + deterministic date resolution. |
| src/recovery/llm/copy_gate.py | New copy gate enforcing compliance categories (offers/urgency/threats/fabrications/URL/length). |
| src/recovery/llm/composer.py | New composer using local Ollama + gate + template fallback; link injected by code. |
| src/recovery/ledger/audit.py | Adds `INBOUND` record type, optional simulation clock, and inbound recording. |
| src/recovery/evaluation/invariants.py | New invariant checker that re-derives guardrail metrics solely from the ledger. |
| src/recovery/evaluation/engine_arm.py | Engine runner integrated with ledger clock + parser/composer use in simulation. |
| src/recovery/engine/policy.py | New versioned policy, EV rule, retry schedule, ladder config, and tuned human-call floor. |
| src/recovery/engine/machine.py | New deterministic decision engine/state machine that logs all decisions (including refusals). |
| src/recovery/engine/guardrails.py | New guardrail evaluation ordering and enforcement (hard stops, caps, promise-to-pay, window, reachability). |
| src/recovery/cohort/simulator.py | Adds simulated inbound replies + promise honoring to exercise stopping rules. |
| scripts/webhook_status.ps1 | Stops hardcoding zrok share URL; builds from `.env` to avoid advertising a live endpoint. |
| scripts/webhook_daemon.ps1 | Reads zrok share name from `.env`; fixes health probe URL construction ordering. |
| scripts/phase2_evidence.py | Script to regenerate Phase 2 evidence artifact deterministically (with live-LLM sections if available). |
| results/phase2/2-engine-and-gate.json | Generated Phase 2 evidence artifact (invariants, costs, gate fixture, live-LLM sections). |
| results/phase0/0.4b-webhook-zrok-tunnel.json | Adds redaction note for zrok share name placeholder. |
| docs/phase-3.md | Phase 3 plan for batch + metrics with CI, holdout, and artifact constraints. |
| docs/phase-2.md | Phase 2 design/evidence write-up aligned to the “LLM never decides contact” rule. |
| docs/phase-0-findings.md | Notes `.env` var for zrok share name. |
| docs/local-setup.md | Documents that zrok share name comes from `.env` and should not be advertised in a public repo. |
</details>

<details>
<summary>Review details</summary>

- **Files reviewed:** 21/21 changed files
- **Comments generated:** 2
- **Review effort level:** Lite
</details>

---

💡 <a href="/divyanshg03/razorpay-ai-revenue-recovery/new/feat(v1)?filename=.github/skills/code-review/SKILL.md" class="Link--inTextBlock" target="_blank" rel="noopener noreferrer">Add a `code-review` agent skill</a> or configure MCP servers for context-aware, tailored reviews. <a href="https://docs.github.com/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review?tool=webui#mcp-servers-and-agent-skills" class="Link--inTextBlock" target="_blank" rel="noopener noreferrer">Learn more in the docs.</a>


---

## PR #2 — Phases 0-2: de-risking, ingest and ledger, and the decision engine

**MERGED** · `feat/phase-2-decision-engine` → `main` · opened 2026-09-02 · merged 2026-09-02

<details>
<summary>Description</summary>

Everything built so far, for merge into `main`: Phase 0 (de-risking), Phase 1 (ingest, taxonomy, ledger) and Phase 2 (decision engine, guardrails, copy gate).

> **Note:** PR #1 proposes the same head branch into `feat(v1)` and shows the Phase 2 diff in isolation. This PR is the full body of work against `main`. Merging this one makes #1 redundant.

## What this is

A decisioning layer above Razorpay's own retry ladder for failed UPI Autopay / eMandate collections. Razorpay retries four times (T+0…T+3), then halts and tells you to charge manually. This decides who to contact, when, on what channel, and — mostly — whom to leave alone.

## Phase 0 — de-risk before building

Eight gates, all resolved. The blocker fired on day 0: **Subscriptions is activation-gated on a fresh test account** (401 on `/plans` and `/subscriptions` while ten other endpoints return 200). That killed the original premise, so the cohort moved to a seeded simulator behind an interface, decided the same day.

Two gates paid for themselves:

- **0.6** — every RBI claim was secondhand. Read at source, this corrected three paragraph citations and **withdrew one claim entirely**: the "lodged grievance is a hard stop (454J)" rule does not exist. 454J is the code-of-conduct paragraph; "frivolous" appears nowhere. Citing a regulator for a rule it doesn't contain, in a submission whose pitch is checkable compliance, is what a panel opens with.
- **0.7** — the metric was frozen and committed at `8d14dbe`, **before any engine code existed**. Git ancestry is the proof.

## Phase 1 — plumbing that carries evidence

**The audit ledger is built before the decision engine, deliberately.** Retrofit it and "every decision reconstructable under the policy version in force at the time" becomes a claim you can't honour. Refusals are logged as fully as actions.

**The simulator has no per-arm recovery dial.** It models customer behaviour only — money arrives after payday, a silent retry needs money but not attention, a contact can't conjure money, repeat contacts decay. Any lift has to be earned against those mechanics.

That produced a finding about Razorpay's product rather than ours: the dominant documented failure cause is an empty account (NPCI: UPI Autopay success fell ~50% → ~30%), which is a **timing** problem. The ladder retries on four *consecutive* days of a roughly monthly salary cycle. Measured: **2,088 of its retries went to instruments that can never be charged.**

World model validated before any engine number was looked at — arm B lands at **23.6%**, inside the 20–30% band published figures imply. Asserted in the test suite.

## Phase 2 — the engine

> **An LLM must never decide whether to contact someone about money.**

`engine/machine.py` has no import path to the model. Guardrails carry their provenance explicitly: 08:00–19:00 IST is a **product invariant** (RBI 454Y(4) binds regulated entities, not merchants); 7-in-7 is a **policy choice** with no force in India; the dispute freeze is a **policy choice**, per the 0.6 correction.

**The guardrail numbers are not self-reported.** `evaluation/invariants.py` never imports the engine — it replays the ledger and re-derives every violation from the records. All eight are zero, and not vacuously: the same run produced 2,119 opt-out refusals, 1,174 dispute refusals, 968 promise-to-pay silences. Refusals outnumber actions ~3:1.

**The copy gate catches a real violation** — `ministral-3`'s actual bake-off output, *"Limited-time bonus: 10% extra data on next recharge"*, rejected on four categories plus a missing link and 165 chars.

**Dates leave the LLM entirely.** The parser extracts the phrase; code resolves it. Every model tested got dates wrong and one produced a promise-to-pay date *in the past*.

## Decisions reversed after measuring

- An equal-touch ladder for SMS-only customers: built, measured, **reverted**. The touch deficit was real; the recovery gap I first reported was noise (−0.76 pp, 95% CI [−3.73, +2.20] at n=8,000).
- Human-call floor raised ₹500 → ₹2,000: that rung was **91.7% of modelled spend** for no measurable recovery. Cost down 77%, recovery unchanged.

Both decision records are in `policy.py`, including the caveat that the simulator understates human calls.

## Verification

81 tests green · gate 0.4 webhook spike passing · all eight invariants zero · no keys, secrets or PII in the tree · every figure regenerates from a script

## Not included

No batch run and no metrics — Phase 3 measures this against the frozen definition, with a randomised holdout and a bootstrap CI.

</details>


### Review (COMMENTED) — copilot-pull-request-reviewer

### 🟡 Changes recommended

There are a few correctness/safety issues in the new core paths (webhook JSON handling, audit evidence labels, and LLM-intent downgrading) that should be fixed before merge.

*Once you've addressed the issues Copilot identified, you can request another Copilot review.*

<details>
<summary>Pull request overview</summary>

Introduces the end-to-end “AI revenue recovery” system for failed recurring collections, spanning the core domain model, deterministic decision engine + guardrails, LLM-only-on-copy boundaries, ingest plumbing, simulation/evaluation harness, and committed evidence artifacts to support reproducibility and auditability.

**Changes:**
- Adds a deterministic recovery engine with policy versioning, guardrails, and ledger-replay invariants.
- Adds an LLM composer/parser constrained by a copy gate (LLM never decides “whether to contact”).
- Adds webhook ingest + simulator-based evaluation scripts/docs and regenerable results artifacts.
</details>

<details>
<summary>File summaries</summary>

| File | Description |
| ---- | ----------- |
| src/recovery/models.py | Introduces core domain dataclasses and enums used across ingest/engine/eval. |
| src/recovery/llm/parser.py | Implements reply parsing with code overrides + optional local LLM call. |
| src/recovery/llm/copy_gate.py | Adds compliance-oriented copy gate to reject unsafe/promotional LLM outputs. |
| src/recovery/llm/composer.py | Composes outbound copy via local Ollama with deterministic template fallbacks. |
| src/recovery/llm/__init__.py | Package marker for LLM modules. |
| src/recovery/ledger/__init__.py | Package marker for ledger modules. |
| src/recovery/ingest/webhook.py | Implements webhook verification, dedup, state precedence, and rejection logging. |
| src/recovery/ingest/__init__.py | Package marker for ingest modules. |
| src/recovery/evaluation/invariants.py | Replays ledger to independently verify guardrail invariants. |
| src/recovery/evaluation/engine_arm.py | Runs engine arm against simulator with narrow interface boundaries. |
| src/recovery/evaluation/baselines.py | Implements baseline arms (do-nothing + incumbent retry ladder) and metrics helpers. |
| src/recovery/evaluation/__init__.py | Package marker for evaluation modules. |
| src/recovery/engine/policy.py | Centralizes tunables under a version string and defines EV + retry schedule logic. |
| src/recovery/engine/machine.py | Deterministic state machine that plans actions and records decisions/inbound. |
| src/recovery/engine/guardrails.py | Guardrail evaluation with explicit stop reasons and pass/fail evidence capture. |
| src/recovery/engine/__init__.py | Package marker for engine modules. |
| src/recovery/diagnosis/taxonomy.py | Failure diagnosis mapping to buckets + actionability, with coverage reporting. |
| src/recovery/diagnosis/__init__.py | Package marker for diagnosis modules. |
| src/recovery/cohort/source.py | Defines the cohort source protocol to swap simulator vs Razorpay-backed ingest. |
| src/recovery/cohort/PARAMETERS.md | Documents simulator parameters with provenance and limitations. |
| src/recovery/cohort/__init__.py | Package marker for cohort modules. |
| src/recovery/__init__.py | Package marker for recovery. |
| spikes/webhook_receiver.py | Phase-0 spike webhook receiver used for feasibility validation. |
| spikes/test_webhook_receiver.py | Spike proof harness driving receiver over HTTP to validate signature/dedup/ACK. |
| scripts/webhook_status.ps1 | One-shot endpoint health probe script (public probe based). |
| scripts/webhook_daemon.ps1 | One-shot repair script intended to be scheduled as a watchdog task. |
| scripts/phase2_evidence.py | Regenerates Phase 2 evidence artifact (engine run + gate fixtures + live checks). |
| scripts/install_webhook_task.ps1 | Registers the webhook watchdog as a repeating scheduled task. |
| scripts/gate_0_7_power.py | Regenerates power/cost sizing calculations for the frozen metric definition. |
| scripts/diagnose_webhook_secret.py | Diagnoses webhook secret mismatch by testing captured rejected bodies/signatures. |
| scripts/create_payment_link.py | Creates a test-mode payment link for generating failure/webhook evidence. |
| scripts/capture_payment_evidence.py | Captures first payment + webhook evidence for gates 0.2 and 0.4. |
| results/phase2/2-engine-and-gate.json | Committed Phase 2 evidence artifact output. |
| results/phase1/1-baselines-and-coverage.json | Committed Phase 1 evidence artifact output. |
| results/phase0/0.7-power-and-cost-check.json | Committed Phase 0 evidence artifact output. |
| results/phase0/0.5c-notify-by-sms.json | Evidence artifact for notify_by behavior. |
| results/phase0/0.5b-invoice.json | Evidence artifact for invoice API behavior. |
| results/phase0/0.5a-payment-link.json | Evidence artifact for payment link create behavior. |
| results/phase0/0.4d-razorpay-originated-event.json | Evidence artifact for Razorpay-originated webhook verification. |
| results/phase0/0.4c-received-events.jsonl | Raw received events log used as committed evidence. |
| results/phase0/0.4b-webhook-zrok-tunnel.json | Evidence artifact for reserved public tunnel testing. |
| results/phase0/0.4-webhook-receiver-local.json | Evidence artifact for local webhook receiver proof. |
| results/phase0/0.2c-error-fields.json | Evidence artifact for observed error-field shape in test mode. |
| results/phase0/0.2b-payment-link-for-failure.json | Evidence artifact for payment link created to drive a failure. |
| results/phase0/0.2a-payments-list.json | Evidence artifact for payments list baseline (empty). |
| results/phase0/0.1j-upi-autopay-100.json | Evidence artifact for UPI autopay order. |
| results/phase0/0.1i-emandate-order-valid.json | Evidence artifact for a valid emandate order. |
| results/phase0/0.1h-payment-link.json | Evidence artifact showing payment-link validation constraints. |
| results/phase0/0.1g-plain-order.json | Evidence artifact for plain order creation. |
| results/phase0/0.1f-upi-autopay-order.json | Evidence artifact for a rejected UPI autopay order case. |
| results/phase0/0.1e-emandate-order.json | Evidence artifact for emandate order validation constraints. |
| results/phase0/0.1d-recurring-debit.json | Evidence artifact for recurring debit endpoint behavior. |
| results/phase0/0.1c-create-customer.json | Evidence artifact for customer creation. |
| results/phase0/0.1a-create-plan.json | Evidence artifact for plan creation being gated. |
| results/phase0/0.1-scan-virtual_accounts.json | Capability scan artifact for virtual accounts endpoint. |
| results/phase0/0.1-scan-subscriptions.json | Capability scan artifact for subscriptions endpoint. |
| results/phase0/0.1-scan-settlements.json | Capability scan artifact for settlements endpoint. |
| results/phase0/0.1-scan-refunds.json | Capability scan artifact for refunds endpoint. |
| results/phase0/0.1-scan-plans.json | Capability scan artifact for plans endpoint. |
| results/phase0/0.1-scan-payments.json | Capability scan artifact for payments endpoint. |
| results/phase0/0.1-scan-payment_links.json | Capability scan artifact for payment links endpoint. |
| results/phase0/0.1-scan-orders.json | Capability scan artifact for orders endpoint. |
| results/phase0/0.1-scan-items.json | Capability scan artifact for items endpoint. |
| results/phase0/0.1-scan-invoices.json | Capability scan artifact for invoices endpoint. |
| results/phase0/0.1-scan-disputes.json | Capability scan artifact for disputes endpoint. |
| results/phase0/0.1-scan-customers.json | Capability scan artifact for customers endpoint. |
| results/phase0/0.1-capability-scan.json | Aggregated capability scan evidence. |
| pyproject.toml | Declares minimal Python project setup and pytest config. |
| docs/support-ticket-draft.md | Drafts support ticket request and operational follow-ups. |
| docs/razorpay-api-notes.md | Captures API behaviors/constraints observed and their design implications. |
| docs/phase-3.md | Defines planned Phase 3 measurement work against the frozen metric definition. |
| docs/phase-2.md | Documents Phase 2 design boundaries, guardrails, and evidence references. |
| docs/phase-1.md | Documents Phase 1 goals and sequencing rationale. |
| docs/phase-0.md | Documents Phase 0 gates, evidence, and metric-freezing rationale. |
| docs/local-setup.md | Documents local environment setup and operational scripts. |
| .gitignore | Updates ignores for local tooling, credentials, and runtime state (keeps results tracked). |
</details>

<details>
<summary>Review details</summary>

- **Files reviewed:** 77/86 changed files
- **Comments generated:** 5
- **Review effort level:** Lite
</details>

---

💡 <a href="/divyanshg03/razorpay-ai-revenue-recovery/new/main?filename=.github/skills/code-review/SKILL.md" class="Link--inTextBlock" target="_blank" rel="noopener noreferrer">Add a `code-review` agent skill</a> or configure MCP servers for context-aware, tailored reviews. <a href="https://docs.github.com/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review?tool=webui#mcp-servers-and-agent-skills" class="Link--inTextBlock" target="_blank" rel="noopener noreferrer">Learn more in the docs.</a>


---

## PR #3 — Phase 3: measure against the frozen definition, then fix what the failure list exposed

**OPEN** · `feat/phase-3-measurement` → `main` · opened 2026-09-02

<details>
<summary>Description</summary>

## What this is

Phase 3 of Track 03. Runs the three-arm batch - do-nothing, Razorpay's T+0..T+3 ladder reimplemented, the engine - against the metric frozen at `8d14dbe`, writes `results/metrics.json`, and then, because the first failure list came out at 41.9%, decomposes that residual and fixes the one bucket that turned out to be a defect.

## Headline

Every figure below is read from `results/metrics.json`; none is typed.

| | |
|---|---|
| Net incremental, engine vs Razorpay's ladder | Rs 935,664.07 |
| 95% CI (percentile bootstrap, 10,000 resamples, seeded) | Rs 786,950.97 - Rs 1,074,095.45 |
| Per customer | Rs 334.40 |
| Cost per incremental rupee | Rs 0.0027 |
| Recovery, arms A / B / C | 2.00% / 25.25% / 66.51% |
| Guardrail invariants | all zero, every cohort |

The comparison is C vs B. Beating do-nothing is reported as context only.

## Commits, in order

1. `bf4a2fb` - records the zrok share-name rotation. No code change.
2. `710de66` - the batch: sha256 bucketing for arm assignment, bootstrap CI, ledger-replay invariants that never import the engine, and a refuse-to-write rule if any invariant is non-zero.
3. `38c16a9` - stops the webhook watchdog flashing a console window every five minutes. Launches through a VBS shim with window style 0 and registers the task `-Hidden`.
4. `46986d5` - **the defect.** `retry_schedule` built a fixed-spacing list and truncated it to the retry budget, so `retry_horizon_days = 21` was declared, documented, and never reached: the last retry fell on day 15. 489 of the 1,171 unrecovered debts - 42% of the failure list - had money arrive after that day, inside the window we claimed to cover. Same six attempts, same horizon, same cost; they now spread `0, 4, 8, 13, 17, 21`. Salary-cycle coverage 21/30 -> 27/30.
5. `b1d4670` - the re-run's first invocation refused to write, with 56 `contact_after_payment` violations. Phantom: the batch had appended onto the previous run's ledger and the invariants read two chains over the same debt ids as one history. `AuditLedger` gains an explicit `fresh` flag; appending stays the default because silently truncating an audit trail would be the worse bug.
6. `69663f0` - the residual is decomposed in the artifact rather than in prose: stopped by a guardrail (correct, 208) / no money in the window (unreachable, 267) / funded but never attempted (**defect, now 0**) / attempted while funded and still unpaid (462).

## What a reviewer should push on

- **A result had been observed before the fix in (4).** Amendment A1 in `docs/metric-definition.md` says so, carries the superseded number (Rs 736,113.77), and was committed *before* the re-run so git ancestry proves the disclosure predates the number.
- **Two identities are the evidence the fix did what it claims and nothing else.** Arms A and B did not move at all - neither calls `retry_schedule`. And `needs_customer_action` did not move either, 17.99% -> 17.99%, 237 failures before and after - it is the one cause the engine never silently retries. Every cause that depends on a retry landing when money is present moved together.
- **The secondary cohorts went down** - shifted Rs 435,762 -> Rs 418,066, 14-day Rs 532,261 -> Rs 483,145 - and are reported as they came out. The shifted cohort carries 674 never-funded customers against 267.
- **A2 records a change deliberately not made.** Making `needs_customer_action` retryable-after-contact would raise the headline, but the simulator gates `NEEDS_NEW_INSTRUMENT` on a flag only a contact can set while `NEEDS_CUSTOMER_ACTION` has no gate at all, so the gain would measure a modelling gap. Repairing the gap would instead strip recovery from the incumbent baseline, which retries blindly and contacts nobody. Both declined; the weak spot stands and is reported.
- **A3 discloses the refused invocation** so that "one batch run" stays a true statement. No engine parameter changed between the refused invocation and the one that produced the number.

## Tests

116 pass. New in this PR: the last retry lands *on* the declared horizon (the old assertion was `max(days) >= 15`, i.e. the value the bug happened to produce); spacing is a throttle floor never breached across budgets 1 to 24; a re-run must be indistinguishable from a first run into a clean path; the default ledger still appends; the funded-but-never-attempted bucket is zero in both the primary and shifted cohorts.

## Not in this PR

README generated from `metrics.json` with a test asserting they match, the limitations section, the video. That is Phase 4.

The full ledgers (~27 MB each) are gitignored and regenerate byte-for-byte from the seed; `results/phase3/ledger-extract.json` carries each chain-head hash plus three debts replayed by outcome.

</details>


### Comment — divyanshg03

Thanks — four of the six were real, and one of them was the serious kind. Two I fixed differently to the suggestion; reasoning below.

### Fixed as suggested

**`resamples` never reached the bootstrap.** Correct, and it was a genuine plumbing bug: `run()` reported `bootstrap.resamples` in the artifact but `_cohort_block` called `compare()` without it. At the shipped default the two coincide at 10,000, so every published interval *was* computed with the count the artifact claims — but a caller asking for a cheaper interval got an expensive one and a JSON file that misdescribed it. Now threaded through, with a test that spies on what the bootstrap was really called with rather than trusting the JSON. I checked the test fails against the old code (`{None} == {37}`) so it is a real guard rather than a decorative one.

**`summarise()` called twice per group** in `by_cause` and `by_debt_size_tercile`. Fixed.

**The stale figures in `docs/phase-3.md`.** This was the important one. Your numbers were right — I verified each against the artifact.

I did not retype them, because that sets up the identical failure on the next re-run, and this repo's own rule is that no figure is ever typed. The numbers now live in a marked block generated by `scripts/render_docs.py`, with a test that runs it under `--check` so a stale doc fails the build instead of waiting for a reviewer. A second test asserts no superseded figure appears anywhere in `docs/` outside the amendments that exist to record them. This is the mechanism Phase 4 needed for the README; it arrived early because Phase 3 proved it was needed.

Fixing the numbers also surfaced something worse than a stale table: a claim that had **changed sign**. The doc said the pre-registered prediction "held" — that a proper holdout would land *below* the Phase 2 sanity run. It did not; the lift is larger, and was already larger before this PR. The prediction is left in place unedited with a note saying it was wrong and why, because a pre-registration you quietly revise once the result arrives is not a pre-registration.

### Fixed, but differently

**The phone number — you were right, and it was worse than "test-like data".** `+91XXXXXXXXXX` (digits withheld here on purpose) is a real-format Indian mobile that was typed into Razorpay's hosted checkout to drive the test payment. Every other artifact in the repo uses obvious synthetic patterns and `create_payment_link.py` defaults to `+919812345670`, which is what it now holds, so the payload shape is unchanged.

I did **not** take the rest of the suggested redaction. `void@razorpay.com` is Razorpay's own null-address placeholder, not personal data, and rewriting it to `redacted@example.com` would misreport what the gateway actually sends — this file is evidence for the Phase 0 error-field findings. The fields it exists to evidence (`error_code`, `error_description`, `error_source`, `error_step`, `error_reason`) are byte-for-byte as delivered, and the redaction is disclosed in `docs/local-setup.md` rather than made silently.

**The OTP.** Declining the removal, with the staleness half of your point addressed. `754081` is not a secret: it is Razorpay's published, universal test-mode OTP, identical for every test account and documented publicly. Deleting it costs reproducibility for no security gain. It is now labelled as such, with an instruction to believe the Dashboard over the page if they ever differ.

### Verification

The batch was re-run and every value in `results/metrics.json` is **byte-identical except `head_commit`**. That identity is the evidence these fixes are non-behavioural — which is why it was re-run rather than asserted. Recorded as amendment A5 in `docs/metric-definition.md`, since the freeze protocol requires every run to be recorded whether or not anything moved. 119 tests pass.

### One thing not fixed, because it is not mine to decide

The redacted number is still in git history at `c12542e`, which is already an ancestor of `origin/main`. Removing it there means rewriting history on a branch with merged PRs. It matters before the repo goes public for submission, not while it is private, so I have flagged it rather than force-pushing over merged work.


### Review (COMMENTED) — copilot-pull-request-reviewer

### 🟡 Changes recommended

It introduces correctness/security/documentation issues (bootstrap resample parameter not applied, committed docs/results inconsistencies, and sensitive-like data/OTP/PII in committed artifacts/docs) that should be resolved before approval.

*Once you've addressed the issues Copilot identified, you can request another Copilot review.*

<details>
<summary>Pull request overview</summary>

Adds the Phase 3 measurement harness and artifacts: a deterministic three-arm batch (A do-nothing, B incumbent ladder reimplementation, C engine), a frozen metric implementation with seeded bootstrap CI, and integrity/guardrail checks to ensure `results/metrics.json` is reproducible and not written when invariants fail. It also fixes a retry scheduling defect (horizon coverage) and a rerun/ledger appending defect (fresh audit ledger per run), and records the disclosures/amendments in documentation.

**Changes:**
- Introduces evaluation modules for arm assignment, metric computation (bootstrap CI, failure list decomposition), and the batch runner that produces `results/metrics.json`.
- Fixes `retry_schedule()` to spread retries across the declared horizon and updates tests to assert the horizon/spacing properties.
- Updates audit ledger initialization to support explicit “fresh run” semantics and adds Phase 3 integrity tests plus committed result artifacts/extracts.
</details>

<details>
<summary>File summaries</summary>

| File | Description |
| ---- | ----------- |
| tests/test_phase3.py | Adds Phase 3 integrity tests for assignment, metric/CI reproducibility, guardrail refusal, rerun/ledger behavior, and artifact self-consistency checks. |
| tests/test_phase2.py | Updates retry schedule tests to assert horizon coverage and spacing floor across budgets. |
| src/recovery/ledger/audit.py | Adds `fresh` flag to allow explicit new-chain runs by deleting an existing ledger file. |
| src/recovery/evaluation/metrics.py | Implements frozen metric calculations, seeded stratified bootstrap CI, subgroups, and failure-list decomposition (“standing”). |
| src/recovery/evaluation/batch.py | Adds the three-arm batch runner and report generation for `results/metrics.json` with guardrail refusal logic. |
| src/recovery/evaluation/assignment.py | Implements frozen, deterministic, per-customer arm assignment with pre-randomization exclusions. |
| src/recovery/engine/policy.py | Fixes `retry_schedule()` to cover the declared retry horizon while respecting a spacing floor. |
| scripts/run_hidden.vbs | Adds a VBS shim to launch the webhook daemon without a flashing console window. |
| scripts/run_batch.py | Adds the CLI entrypoint to run the batch and write `results/metrics.json`. |
| scripts/ledger_extract.py | Adds a script to generate a small committable ledger extract (chain head + example replays). |
| scripts/install_webhook_task.ps1 | Updates scheduled task installation to use the VBS launcher and set the task as hidden. |
| results/phase3/ledger-extract.json | Commits the Phase 3 ledger extract (chain heads + replay samples). |
| results/phase0/0.4c-received-events.jsonl | Appends an additional received event record. |
| results/metrics.json | Commits the generated Phase 3 batch output artifact. |
| docs/phase-3.md | Updates Phase 3 plan/status with recorded results. |
| docs/metric-definition.md | Adds amendments documenting observed pre-fix result, defect fixes, rerun refusal cause, and residual decomposition. |
| docs/local-setup.md | Documents zrok share-name rotation steps. |
| .gitignore | Ignores Phase 3 audit ledgers (`results/phase3/*.jsonl`). |
</details>

<details>
<summary>Review details</summary>

- **Files reviewed:** 16/18 changed files
- **Comments generated:** 6
- **Review effort level:** Lite
</details>

---

💡 <a href="/divyanshg03/razorpay-ai-revenue-recovery/new/main?filename=.github/skills/code-review/SKILL.md" class="Link--inTextBlock" target="_blank" rel="noopener noreferrer">Add a `code-review` agent skill</a> or configure MCP servers for context-aware, tailored reviews. <a href="https://docs.github.com/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review?tool=webui#mcp-servers-and-agent-skills" class="Link--inTextBlock" target="_blank" rel="noopener noreferrer">Learn more in the docs.</a>


---

## PR #3 — inline review comments


**`results/phase0/0.4c-received-events.jsonl`** line 6 — Copilot

This committed webhook payload includes personal/contact fields (e.g. `email` and `contact`) and other live-like identifiers. Even if test data, storing PII-like fields in-repo can create compliance and sharing risks; consider redacting/sanitizing before committing.


**`src/recovery/evaluation/batch.py`** line 162 — Copilot

`run(resamples=...)` is reported in `report['bootstrap']['resamples']` but not actually used to compute the bootstrap CIs: `_cohort_block()` calls `compare(...)` without passing `resamples`, so it always uses `BOOTSTRAP_RESAMPLES`. This makes the JSON internally inconsistent and defeats the intent of `resamples` (e.g., faster test runs).


**`docs/local-setup.md`** line 127 — Copilot

The docs include a concrete OTP value ("OTP `754081`") for the Razorpay Dashboard. Even if it's test-mode, hardcoding OTPs/codes in-repo is risky and tends to become stale; better to instruct the reader to use the OTP shown at the time.


**`docs/phase-3.md`** line 22 — Copilot

This Phase 3 result table (and the per-customer/cost figures immediately below) still shows the pre-amendment numbers (Rs 736,114 / Rs 532,261 / Rs 435,762), which now contradict the committed `results/metrics.json` and the amendments in `docs/metric-definition.md`. Either update these figures to the current artifact, or explicitly label them as superseded and point to `results/metrics.json` as the single source of truth.


**`src/recovery/evaluation/metrics.py`** line 184 — Copilot

`by_cause()` calls `summarise(v)` twice per group (once for `recovery_rate` and again for `recovered_rupees`), which doubles work and repeats the same aggregation.


**`src/recovery/evaluation/metrics.py`** line 202 — Copilot

`by_debt_size_tercile()` calls `summarise(v)` twice per bucket, repeating the same aggregation work for each tercile.
