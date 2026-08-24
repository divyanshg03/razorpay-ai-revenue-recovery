# Compliance constraints — India, recovery messaging

Researched 23–24 Aug 2026. **Read the caveats.** Several widely-repeated "rules" turn out
not to be binding, and claiming them would be worse than silence. Where something could not
be verified it says so.

## The one finding that shapes the architecture

**Our use case sits in the *service* bucket, not the promotional one — and that is a
significant regulatory advantage we should not accidentally throw away.**

| Use case | Character | Consent | Channel constraints |
|---|---|---|---|
| **Failed mandate / payment retry** ← us | Service, customer-initiated | **Inferred consent OK** | No DND scrub, service header, wide time window |
| Overdue invoice | Service, existing contract | Inferred consent OK | Same |
| Abandoned checkout | **Promotional** — no transaction completed | **Explicit consent** | DND-scrubbed, 140-series, 0900–2100 only, WhatsApp *Marketing* template |

TRAI's Second Amendment 2025 sets the trap: **if promotional content is mixed with service
content, the whole message becomes promotional.** So "your payment failed, retry here —
and here's 10% off" inherits the full consent, DND and time-band burden.

→ **The LLM must never be free to add a discount, an urgency cue or an upsell.** That is a
regulatory category violation, not a tone problem. Message composition runs through a
classifier + copy gate that rejects promotional drift. This is a hard requirement, and it's
one of the strongest arguments for boxing the LLM in.

## RBI — and who it actually binds

**The 08:00–19:00 contact window is real**, from RBI/2022-23/108 (12 Aug 2022): agents must
not call *"before 8:00 a.m. and after 7:00 p.m. for recovery of overdue loans."* Also
prohibited: intimidation, public humiliation, **persistent repeated calling**, anonymous
calls, false representations.

**New and very current:** nine circulars **RBI/2026-2027/223–231, dated 6 Aug 2026,
effective 1 Jan 2027**, inserting a new Section L on recovery conduct (paras 454A–454AB).
Key provisions: contact only 08:00–19:00 (454T.2); **interact only with the borrower or
guarantor, never other contacts** (454T.1); **a lodged grievance is a hard stop** — the case
may not be forwarded for recovery until the grievance is disposed of (454J), exception only
with *evidence* of frivolous filing; mandatory call recording with 6-month retention; IIBF
certification for agents. **Explicitly no numeric call cap** — only a qualitative
prohibition on *"excessively calling"* (454U.3).

### Lender vs merchant — state this precisely

**None of the above binds an ordinary merchant recovering its own receivables.** RBI's
jurisdiction reaches Regulated Entities — banks, NBFCs, HFCs, AIFIs, co-op banks, ARCs. A
D2C brand chasing a failed UPI mandate is none of those. There is no RBI-enforceable
contact-hour rule for pure merchant receivables.

Four things flip a merchant into scope: (1) the receivable arises from RE-extended credit
(BNPL, card EMI, embedded lending) and obligations flow down contractually; (2) payment-
aggregator agreements import conduct terms; (3) other law binds merchants regardless —
see below; (4) consumer fora and the press treat 08:00–19:00 as the *de facto* standard.

**Our decision: hard-code 08:00–19:00 IST as a product invariant** for all recovery contact,
per-tenant overridable only in the *narrowing* direction. It costs nothing, it's the
clearest compliance artefact we can show, and it makes the product sellable to lenders
without a rewrite.

## TRAI / TCCCPR — binding, but lighter than expected for us

Governs **SMS and voice on Indian telecom networks**. Does **not** govern WhatsApp, email or
in-app (OTT is out of scope).

All A2P SMS needs DLT registration regardless of category: Principal Entity → Header (≤11
chars) → Content Template → Consent Template. Since 6 May 2025 headers carry an auto-appended
suffix: `-T` transactional (OTP only, within 30 min), `-S` service, `-P` promotional,
`-G` government.

**"Your payment failed, here's a link to retry" is Service Implicit (`-S`).** Consequence:
**no explicit consent required, no DND/NCPR scrubbing, and no 0900–2100 band.** Our timing
constraint comes from RBI (if in an RE chain) or our own policy — not from TRAI.

Also from the Second Amendment 2025: **inferred consent expires with the contractual
relationship** — so the consent store needs a relationship-lifecycle link, not a boolean.
Complaint threshold to act against a sender dropped from 10 to 5.

## WhatsApp — contract, not law

Enforced by account suspension, not a regulator. **Failed-payment retry qualifies as a
Utility template** (follows a user action, user-specific, non-promotional); Meta's own
Utility examples include billing and payment reminders. Abandoned cart is explicitly in
Meta's *Marketing* example list.

**Utility templates sent inside an open 24-hour customer service window are free** — a real
cost lever, and a reason to design for provoking an inbound reply.

Templates silently re-categorise to Marketing if copy drifts promotional. Opt-out must be
honoured **whether made on or off WhatsApp**, and the person removed from the contact list —
broader than a channel-level unsubscribe.

Messaging limits are now **per Business Portfolio** (since Oct 2025), tiers 250 → 2k → 10k →
100k → unlimited. **Quality rating is computed from the last 7 days**; Yellow freezes the
tier, Red cuts it. → **Isolate recovery traffic in its own portfolio, and treat block-rate
as a first-class stopping signal with an automatic circuit breaker.**

## DPDP — not yet in force, and say so

**The substantive obligations commence 14 May 2027.** Only the Board's establishment (14 Nov
2025) and Consent Manager registration (14 Nov 2026) are live. **As of today the substantive
DPDP duties are not enforceable.**

→ **Honest framing for the panel: "designed for 14 May 2027 from day one," never
"DPDP-compliant today."** Overclaiming here is exactly the trap RR11 warns about.

**Lawful basis.** Section 7 is a **closed list** — there is no legitimate-interest catch-all
and **no debt-recovery ground**. Do not claim one. What we rely on is **s.7(a)**: data
voluntarily provided for a specified purpose. A customer who gave their number to complete a
purchase gave it for completing and servicing that transaction — **failed-payment retry and
invoice follow-up sit inside s.7(a); abandoned-cart marketing does not.**

**s.7(a) is conditional on the person not having objected.** The moment they say stop, the
basis evaporates. **That is our statutory stopping rule**, and it's cleaner than any policy
we could invent.

Withdrawal (s.6(6)): cease processing within a reasonable time and propagate to processors.
**s.6(10) puts the burden of proof on us** — so consent, objection and propagation must be
immutable logged events. Rule 8(3) requires **48 hours' notice before erasure**.

## What binds an ordinary merchant anyway

- **CCPA Dark Patterns Guidelines 2023** — 13 named patterns including **False Urgency**
  (fabricated scarcity to force immediate purchase), **Nagging** (repeated unsolicited
  prompts) and **Confirm Shaming**. An agent whose strategy is "create urgency and nudge
  repeatedly" is describing two named dark patterns. → **copy-review gate blocks fabricated
  scarcity; repetition is capped.**
- **Consumer Protection Act 2019** — unfair trade practices.
- **BNS 2023 s.351** criminal intimidation, **expressly extended to electronic
  communication** (up to 2 years); **s.352** intentional insult.
- **IT Act 2000** — digital harassment, data misuse.

## Stopping rules we will implement

No Indian instrument sets a numeric contact cap — only RBI's qualitative "not excessive."
The numeric benchmark everyone borrows is **US Reg F "7-in-7"** (12 CFR 1006.14(b)): more
than 7 calls in 7 consecutive days is presumed a violation, and no call within 7 days of a
conversation. **It has no force in India**, but adopting it voluntarily gives us a numeric,
auditable policy that satisfies RBI's qualitative test. Label it as a policy choice, not law.

**Hard stops** — opt-out on any channel (suppress on *all* channels, target < 24h) ·
dispute raised (freeze, route to human — never let the model adjudicate frivolousness,
RBI requires *evidence*) · DPDP consent withdrawn · bereavement/medical/hardship signalled ·
**payment received** (see the resurrection problem in the API notes) · dispute resolved for
the customer.

**Soft stops** — promise-to-pay captured → silence until PTP date + 1, single T-1 reminder
for high-risk only; broken-promise flag only 3–5 days after the missed date; PTP tolerance
90–95% of promised amount counts as kept · 7-day cooldown after reaching a human
conversation · 7 calls / 7 days per debt, 1 WhatsApp Utility per 24h, 1 SMS per 24h ·
**WhatsApp quality circuit breaker** — pause the campaign automatically on Yellow or on
template block-rate breach, for 7 days (Meta's rating window).

**Escalation ladder terminates, and never loops:** WhatsApp Utility → SMS service →
human agent → written notice → cease automated contact. Tone must **not** escalate with
attempt count. Never contact a third party, zero exceptions. Never imply legal consequences
the merchant will not pursue.

## Audit trail — required fields

Per contact attempt, append-only: `event_id`, `timestamp_IST`, `debtor_ref`, `channel`,
`message_class (service|promotional)`, DLT header + template id / WhatsApp template name +
category + version, `consent_basis (s.6 consent | s.7(a) | contractual-inferred)` +
capture ref + expiry, DND scrub result + timestamp, suppression check, time-window check,
`attempt_counter` (per debt, per channel, rolling 7d), **which stopping rules fired and
which passed**, model/prompt/policy version, exact rendered content, delivery/read/block/
error status, inbound response, escalation handoff, outcome.

Plus a separate append-only **consent and objection ledger** with propagation receipts, and
version-pinned copies of the policy rules in force at each decision — so any decision can be
reconstructed under the rules that applied at the time, not today's.

## Could not verify — do not assert these

1. **RBI primary sources.** rbi.org.in pages would not load; everything RBI here is verified
   against taxguru (which reproduces circular text verbatim), TeamLease RegTech, Business
   Standard and law-firm commentary — multiple independent sources agreeing, but **not RBI
   itself**. Pull RBI/2026-2027/223–231 directly before citing them anywhere consequential.
2. **AI-voice disclosure.** Vendor blogs assert outbound AI calls must announce they're AI.
   **No binding Indian instrument was found.** The IT Amendment Rules 2026 require a
   *"prominently prefixed audio disclosure"* for synthetic audio, but they bind
   **intermediaries**, and their application to a business placing PSTN calls is an open
   question. Best practice with a strong unfair-trade-practice argument — **not** a citable
   mandate. (Moot for now: voice is out of scope.)
3. The TCCCPR 0900–2100 promotional band — confirmed in operator codes of practice, but the
   gazette PDFs would not parse; regulation number unverified.
4. The reported 7-day explicit-consent validity — one source only, reads oddly narrow.
5. WhatsApp's per-user 2-marketing-messages/24h cap and error 131049 — consistently reported
   by BSPs, **absent from Meta's own docs**. Operational reality, not citable policy.
6. NCPR preference-change latency — sources conflict (7 days vs 24–72h). Design for ≤24h
   and don't quote a number.
7. **"10 calls a day = harassment"** appears everywhere as if it were a rule. **It is not.**
   RBI's text says only "excessively calling." Never cite a number to RBI.
