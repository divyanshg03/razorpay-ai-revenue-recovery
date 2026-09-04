"""Guardrail invariants, checked by REPLAYING THE LEDGER — not by trusting the engine.

The engine enforces the rules. If the same code also reported on its own compliance, the
report would be worth nothing. So these checks read only the audit trail, reconstruct what
happened from the records alone, and count violations. Every count must be zero, and the
test suite asserts exactly that.

This is also the "every decision reconstructable" claim, executed: if the ledger were not
sufficient to re-derive these facts, the checks here could not be written.

| Invariant | What is counted |
|---|---|
| contact window | ACTION records whose `at` falls outside 08:00–19:00 IST |
| state re-check | ACTION records with no STATE_RECHECK for the same debt earlier that day |
| stop signals | ACTION records for a customer AFTER an opt-out, dispute or hardship INBOUND |
| after payment | ACTION records for a debt AFTER its OUTCOME record |
| 7-in-7 | any 7-day window with more than 7 ACTIONs on one debt |
| promise to pay | ACTION records inside an active promise window (contacts only) |
| ledger completeness | ACTION records with no DECISION for that debt on that day |
| chain | the hash chain verifies end to end |
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from ..ledger.audit import AuditLedger, RecordType
from ..models import IST

WINDOW_START = dt.time(8, 0)
WINDOW_END = dt.time(19, 0)


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).astimezone(IST)


def check_ledger(ledger: AuditLedger, max_contacts_7d: int = 7,
                 hardship_resume_days: int = 7) -> dict[str, int]:
    entries = ledger.read()
    actions = [e for e in entries if e["type"] == RecordType.ACTION.value]
    rechecks = [e for e in entries if e["type"] == RecordType.STATE_RECHECK.value]
    decisions = [e for e in entries if e["type"] == RecordType.DECISION.value]
    outcomes = [e for e in entries if e["type"] == RecordType.OUTCOME.value]
    inbound = [e for e in entries if e["type"] == RecordType.INBOUND.value]

    v = defaultdict(int)

    # 1. contact window
    for a in actions:
        at = _ts(a["body"]["at"])
        if not (WINDOW_START <= at.time() < WINDOW_END):
            v["contact_outside_window"] += 1

    # 2. state re-check precedes every action, same debt, same day
    recheck_days = defaultdict(set)
    for r in rechecks:
        recheck_days[r["body"]["debt_id"]].add(_ts(r["timestamp_ist"]).date())
    for a in actions:
        if _ts(a["body"]["at"]).date() not in recheck_days[a["body"]["debt_id"]]:
            v["action_without_state_recheck"] += 1

    # 3. no action after a stop signal from the customer
    #
    # One exception, and it is the customer's own instruction rather than ours: a HARDSHIP
    # reply that NAMES A DATE ("I am in hospital, contact me after the 20th") is a request to
    # be contacted later, not a request never to be contacted. Silence after such a reply
    # does not respect the customer, it buries their debt. So a contact is permitted on or
    # after the date they gave, and forbidden before it.
    #
    # Where no date is given, a POLICY pause applies instead - `hardship_resume_days`,
    # passed in rather than imported. An indefinite stop sounds kinder than it is: it buries
    # the debt and never contacts a customer who only needed a fortnight.
    #
    # The exception is deliberately narrow, and applies only to `hardship`. `opt_out` and
    # `dispute` never earn it: an objection under DPDP s.7(a) is not a scheduling preference,
    # and a dispute is closed by a human rather than by a timer. A later opt-out or dispute
    # also CANCELS any hardship pause already in flight.
    #
    # Note this check re-derives the rule from the LEDGER ALONE, as the whole invariants
    # module does - it never imports the engine, so it is a genuine independent check of the
    # engine's behaviour rather than a restatement of it. When the hardship callback was
    # added on 4 Sept 2026 this invariant fired 6 times and the batch refused to write, which
    # is the fail-closed design doing its job on a feature its author had just written.
    # Each action is judged against the signals that existed AT THE TIME IT WAS SENT. An
    # earlier version collapsed every inbound record first and then judged every action
    # against the final state, which let a LATER opt-out retroactively condemn an EARLIER
    # contact that was entirely proper when it went out. That is a real distinction, not a
    # technicality: the question an auditor asks is "should you have sent this, then?"
    signals: dict[str, list[tuple[dt.datetime, str, dt.date | None]]] = defaultdict(list)
    for i in inbound:
        intent, ref = i["body"]["intent"], i["body"]["customer_ref"]
        if intent not in ("opt_out", "dispute", "hardship"):
            continue
        t = _ts(i["timestamp_ist"])
        resume = None
        if intent == "hardship":
            # Their date if they named one; otherwise the configured pause. The pause length
            # is passed IN as a policy value rather than imported from the engine, exactly as
            # `max_contacts_7d` already is - this module checks behaviour against a stated
            # rule and must never import the thing it is checking.
            promised = i["body"].get("promised_date")
            resume = (dt.date.fromisoformat(promised) if promised
                      else t.date() + dt.timedelta(days=hardship_resume_days))
        signals[ref].append((t, intent, resume))

    for a in actions:
        ref, at = a["body"]["customer_ref"], _ts(a["body"]["at"])
        prior = [s for s in signals.get(ref, []) if s[0] < at]
        if not prior:
            continue
        if any(intent in ("opt_out", "dispute") for _, intent, _ in prior):
            v["contact_after_stop_signal"] += 1        # permanent, and never lifts
            continue
        _, _, resume = max(prior, key=lambda s: s[0])  # the latest hardship governs
        if resume is not None and at.date() < resume:
            v["contact_after_stop_signal"] += 1

    # 4. no action after the debt is settled
    settled_at = {}
    for o in outcomes:
        if o["body"].get("recovered_paise", 0) > 0:
            settled_at[o["body"]["debt_id"]] = _ts(o["timestamp_ist"])
    for a in actions:
        d = a["body"]["debt_id"]
        if d in settled_at and _ts(a["body"]["at"]) > settled_at[d]:
            v["contact_after_payment"] += 1

    # 5. 7-in-7 per debt
    per_debt = defaultdict(list)
    for a in actions:
        per_debt[a["body"]["debt_id"]].append(_ts(a["body"]["at"]))
    for d, times in per_debt.items():
        times.sort()
        for i, t in enumerate(times):
            window = [x for x in times[i:] if x < t + dt.timedelta(days=7)]
            if len(window) > max_contacts_7d:
                v["contact_cap_breach"] += 1
                break

    # 6. no contact inside an active promise window
    promises = defaultdict(list)
    for i in inbound:
        if i["body"]["intent"] == "promise_to_pay" and i["body"].get("promised_date"):
            promises[i["body"]["debt_id"]].append(
                (_ts(i["timestamp_ist"]), dt.date.fromisoformat(i["body"]["promised_date"])))
    for a in actions:
        d = a["body"]["debt_id"]
        at = _ts(a["body"]["at"])
        for made, until in promises.get(d, []):
            if made < at and at.date() <= until + dt.timedelta(days=1):
                v["contact_during_promise_to_pay"] += 1
                break

    # 7. every action has a decision that day
    decision_days = defaultdict(set)
    for dcs in decisions:
        decision_days[dcs["body"]["debt_id"]].add(_ts(dcs["timestamp_ist"]).date())
    for a in actions:
        if _ts(a["body"]["at"]).date() not in decision_days[a["body"]["debt_id"]]:
            v["action_without_decision"] += 1

    # 8. chain
    intact, _ = ledger.verify_chain()
    if not intact:
        v["ledger_chain_broken"] += 1

    keys = ("contact_outside_window", "action_without_state_recheck", "contact_after_stop_signal",
            "contact_after_payment", "contact_cap_breach", "contact_during_promise_to_pay",
            "action_without_decision", "ledger_chain_broken")
    return {k: v[k] for k in keys}
