"""Arm C: run the engine against the simulated world, day by day.

This is the only place the engine and the simulator's hidden mechanics meet, and they meet
through the narrowest possible interface: the engine emits a decision; this runner asks the
world what happened; the engine is told the observable result. The engine never receives a
`SimulatedCohort` object and has no import path to its hidden state.

## What the LLM does and does not do in a batch

The state machine's decisions are what recovery depends on, and the simulator has no notion
of wording — a message either reaches a customer or it does not. So a 5,000-customer batch
composes with the deterministic templates (`use_llm=False`) and finishes in seconds; the
Ollama path is exercised for real in the test suite and the demo, where wording is the
point. Reply parsing in a batch likewise uses the code-level overrides plus keyword fallback;
the three intents that carry legal weight are code-decided in every mode regardless.

This is stated in the README rather than hidden, because "the LLM is in the loop" would be
an overclaim about the *measurement* — it is in the loop for the *product*.
"""

from __future__ import annotations

import datetime as dt

from ..cohort.simulator import SimulatedCohort
from ..engine.machine import RecoveryEngine
from ..engine.policy import Policy
from ..ledger.audit import AuditLedger
from ..llm.composer import compose
from ..llm.copy_gate import Facts
from ..llm.parser import Intent, parse_reply
from ..models import IST, Action, Channel, Customer, Debt
from .baselines import ArmOutcome

#: Sends go out mid-morning. Inside the window by construction; the guardrail still checks.
SEND_HOUR = 10


def run_engine(cohort: SimulatedCohort, debts: list[Debt], customers: list[Customer],
               start: dt.date, window_days: int, policy: Policy, ledger: AuditLedger,
               use_llm: bool = False, link_base: str = "https://rzp.io/rzp/") -> list[ArmOutcome]:
    by_ref = {c.ref: c for c in customers}
    engine = RecoveryEngine(policy, ledger, is_settled=lambda d: cohort.has_paid(d.customer_ref))
    outcomes: dict[str, ArmOutcome] = {}
    open_debts: list[Debt] = []

    for debt in debts:
        diag = engine.diagnosis(debt)
        outcomes[debt.debt_id] = ArmOutcome(debt.debt_id, debt.customer_ref, debt.amount_paise,
                                            cause_bucket=diag.bucket.value,
                                            actionability=diag.actionability.value)
        open_debts.append(debt)

    for offset in range(window_days + 1):
        day = start + dt.timedelta(days=offset)
        now = dt.datetime.combine(day, dt.time(SEND_HOUR, 0), tzinfo=IST)
        # The ledger runs on simulated time so that record ORDER is replayable. Each record
        # within a day gets a distinct, monotonic timestamp: the invariant checks compare
        # "before" and "after", and two events stamped identically cannot be ordered.
        tick = {"n": 0}

        def _clock(_now=now, _tick=tick):
            _tick["n"] += 1
            return _now + dt.timedelta(microseconds=_tick["n"])

        ledger.clock = _clock
        still_open: list[Debt] = []

        for debt in open_debts:
            out = outcomes[debt.debt_id]
            customer = by_ref[debt.customer_ref]
            state = engine.state(debt.debt_id)
            diag = engine.diagnosis(debt)

            # The world moves on its own: self-cure, and promises being kept.
            if cohort.organic_settle(debt, day) or \
                    cohort.honour_promise(debt, day, state.promise_to_pay_until):
                _settle(out, debt, offset, ledger, "settled without action")
                continue

            settled = False
            for decision in engine.plan_day(debt, customer, now):
                if decision.stop_reason:
                    out.stop_reasons.append(decision.stop_reason.value)
                if not decision.act:
                    continue

                if decision.channel is Channel.RETRY:
                    out.retries += 1
                    ok = cohort.attempt_charge(debt, day, diag.actionability)
                    engine.apply_outcome(debt, decision, now, ok)
                    if ok:
                        _settle(out, debt, offset, ledger, "retry succeeded")
                        settled = True
                        break
                    continue

                # A contact. Compose (templates in batch), record the action BEFORE the
                # outcome is known, then deliver.
                facts = Facts(amount_paise=debt.outstanding_paise,
                              link=f"{link_base}{debt.debt_id[-8:]}", merchant="")
                msg = compose(facts, diag.actionability, decision.channel, use_llm=use_llm)
                cost = policy.cost_paise[decision.channel]
                rejected = None
                if msg.gate_rejected_llm:
                    rejected = {"verdict": msg.llm_gate.verdict.value,
                                "categories": msg.llm_gate.categories,
                                "reasons": msg.llm_gate.reasons}
                ledger.record_action(
                    Action(debt_id=debt.debt_id, customer_ref=customer.ref,
                           channel=decision.channel, at=now, cost_paise=cost,
                           policy_version=policy.version, rendered_text=msg.text,
                           template_ref=msg.template_ref, rules_fired=decision.rules_fired,
                           rules_passed=decision.rules_passed),
                    copy_gate_rejected=rejected, llm_output=msg.llm_output)
                out.contacts += 1
                out.contact_cost_paise += cost

                ok = cohort.deliver_contact(debt, day, diag.actionability)
                engine.apply_outcome(debt, decision, now, ok)
                if ok:
                    _settle(out, debt, offset, ledger, f"paid after {decision.channel.value}")
                    settled = True
                    break

                # The customer may write back. The engine must read it and stop where the
                # reply demands it - this is where the stopping rules get exercised.
                reply = cohort.reply_to_contact(debt, day)
                if reply:
                    cohort.apply_reply_side_effects(customer, reply)
                    parsed = parse_reply(reply, today=day, use_llm=use_llm)
                    engine.record_reply(debt, customer, parsed, now, reply)
                    if parsed.intent is not Intent.PROMISE_TO_PAY:
                        break  # a hard stop: nothing further today or any day

            if not settled:
                still_open.append(debt)
        open_debts = still_open

    return [outcomes[d.debt_id] for d in debts]


def _settle(out: ArmOutcome, debt: Debt, offset: int, ledger: AuditLedger, note: str) -> None:
    out.recovered_paise = debt.amount_paise
    out.settled_on_day = offset
    ledger.record_outcome(debt.debt_id, debt.customer_ref, debt.amount_paise, note=note)
