"""The deterministic state machine. It owns every action; the LLM owns none.

Nothing in this file imports the LLM. The engine decides *whether*, *when* and *on what
channel*; the composer is asked for wording only after a decision exists, and the parser's
reading of a reply is interpreted here by rules, not by the model. That separation is the
design commitment the whole submission rests on, so it is enforced structurally — by what
this module can and cannot see — rather than by convention.

## What the engine can see

`DebtState`: what it has done to a debt and what it has heard back. Nothing else. No payday,
no funds, no attention state — those live in the simulator's hidden state, which this module
has no import path to. The engine's edge over the incumbent has to come from three things
it CAN see:

1. **The diagnosis.** A dead instrument is never retried blind; a cause the merchant cannot
   act on is dropped; a funds problem is a timing problem.
2. **Spread.** Retries at 0, 3, 6, 9, 12, 15 days instead of 0, 1, 2, 3 — the same number of
   attempts, five times the coverage of a monthly salary cycle.
3. **Listening.** A promise-to-pay is honoured with silence and then a retry on the date;
   an opt-out ends everything; a dispute goes to a human.

## Every decision is written down, including the refusals

`plan_day` records each considered action in the ledger before its outcome is known, with
the guardrails that fired and the ones that passed. A day with nothing scheduled produces no
record — that is a scheduling fact, not a decision — but a day on which the engine considered
acting and declined is recorded with the reason, every time.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from ..diagnosis.taxonomy import Diagnosis, diagnose
from ..ledger.audit import AuditLedger
from ..llm.parser import Intent, ParsedReply
from ..models import IST, Channel, Customer, Debt, Decision, StopReason
from .guardrails import ContactRecord, DebtState, evaluate
from .policy import Policy, expected_value_paise, retry_schedule, worth_acting

#: Days after the failure before the first contact. T+0 is left to the silent retry; a
#: message on the day of failure about a problem that may resolve itself is noise.
FIRST_CONTACT_DAY = 1


class RecoveryEngine:
    def __init__(self, policy: Policy, ledger: AuditLedger,
                 is_settled: Callable[[Debt], bool]):
        self.policy = policy
        self.ledger = ledger
        self._is_settled = is_settled
        self._states: dict[str, DebtState] = {}
        self._diagnoses: dict[str, Diagnosis] = {}

    # -- state ------------------------------------------------------------------------

    def state(self, debt_id: str) -> DebtState:
        return self._states.setdefault(debt_id, DebtState())

    def diagnosis(self, debt: Debt) -> Diagnosis:
        if debt.debt_id not in self._diagnoses:
            self._diagnoses[debt.debt_id] = diagnose(debt.failure)
        return self._diagnoses[debt.debt_id]

    # -- scheduling: what is a candidate today? --------------------------------------

    def _retry_due(self, debt: Debt, state: DebtState, today: dt.date) -> bool:
        offset = (today - debt.failed_at.date()).days
        if state.promise_to_pay_until is not None and today == state.promise_to_pay_until:
            return True  # act on the promise: silent retry on the day they named
        return offset in retry_schedule(self.policy)

    def _contact_due(self, debt: Debt, state: DebtState, today: dt.date) -> bool:
        offset = (today - debt.failed_at.date()).days
        if state.ladder_rung >= len(self.policy.ladder):
            return False
        if not state.contacts:
            return offset >= FIRST_CONTACT_DAY
        since = (today - state.last_contact_day).days if state.last_contact_day else 0
        return since >= self.policy.escalation_wait_days

    def _channel_for_step(self, customer: Customer, step: int) -> Channel | None:
        """Resolve one escalation step to a channel this customer can actually receive.

        A customer with no WhatsApp takes SMS at step 1 and SMS again at step 2 — two
        touches, four days apart, inside the per-channel 24h cap. They get the same number
        of attempts as anyone else, delivered on the channel they actually have.
        """
        for ch in self.policy.ladder[step]:
            if ch is Channel.WHATSAPP_UTILITY and customer.has_whatsapp:
                return ch
            if ch is Channel.SMS_SERVICE and customer.has_sms:
                return ch
            if ch is Channel.HUMAN_CALL and (customer.has_sms or customer.has_whatsapp):
                return ch
        return None

    def _next_step(self, customer: Customer, start: int) -> tuple[int, Channel] | None:
        for i in range(start, len(self.policy.ladder)):
            ch = self._channel_for_step(customer, i)
            if ch is not None:
                return i, ch
        return None

    # -- the decision -----------------------------------------------------------------

    def plan_day(self, debt: Debt, customer: Customer, now: dt.datetime) -> list[Decision]:
        """Zero, one or two decisions for this debt today, each already in the ledger.

        Retry is considered first: it is silent and free, so if it is due it goes before any
        contact. Contact is considered second. Both are recorded whether or not they act.
        """
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)
        today = now.date()
        state = self.state(debt.debt_id)
        diag = self.diagnosis(debt)
        decisions: list[Decision] = []

        candidates: list[Channel] = []
        if self._retry_due(debt, state, today):
            candidates.append(Channel.RETRY)
        if self._contact_due(debt, state, today):
            nxt = self._next_step(customer, state.ladder_rung)
            if nxt is None:
                # No remaining step has a channel this customer can receive. Consider it
                # once so the refusal is logged with its reason, then retire the ladder.
                candidates.append(self.policy.ladder[-1][0])
                state.ladder_rung = len(self.policy.ladder)
            else:
                state.ladder_rung, step_channel = nxt
                candidates.append(step_channel)
        if not candidates:
            return decisions

        # Re-read payment state immediately before acting, and log that the read happened.
        # Webhooks are at-least-once and unordered; this is the rule that stops us dunning
        # someone who paid ten seconds ago.
        settled = self._is_settled(debt)
        self.ledger.record_state_recheck(debt.debt_id, already_paid=settled,
                                         source="payment_state_store")

        for channel in candidates:
            decision = self._decide(debt, customer, diag, state, channel, now, settled)
            self.ledger.record_decision(
                debt.debt_id, customer.ref, decision, diagnosis=diag.actionability.value,
                extra={"channel_considered": channel.value,
                       "day_offset": (today - debt.failed_at.date()).days,
                       "ladder_rung": state.ladder_rung})
            decisions.append(decision)
            if decision.stop_reason in (StopReason.PAID, StopReason.OPTED_OUT,
                                        StopReason.DISPUTED, StopReason.BEREAVEMENT):
                state.hard_stopped = decision.stop_reason
                break
        return decisions

    def _decide(self, debt: Debt, customer: Customer, diag: Diagnosis, state: DebtState,
                channel: Channel, now: dt.datetime, settled: bool) -> Decision:
        # Human escalation below the floor is never worth it; treat the rung as absent.
        if channel is Channel.HUMAN_CALL and \
                debt.outstanding_paise < self.policy.min_amount_for_human_call_paise:
            state.ladder_rung = len(self.policy.ladder)
            return Decision(act=False, channel=channel, stop_reason=StopReason.LADDER_EXHAUSTED,
                            rules_fired=["human_call_below_amount_floor"])

        verdict = evaluate(customer, debt, diag, state, channel, now, self.policy, settled)
        if not verdict.allowed:
            return Decision(act=False, channel=channel, stop_reason=verdict.stop_reason,
                            rules_fired=verdict.rules_fired, rules_passed=verdict.rules_passed)

        ev = expected_value_paise(self.policy, diag.actionability, debt.outstanding_paise,
                                  len(state.contacts), channel)
        if channel is not Channel.RETRY and not worth_acting(
                self.policy, diag.actionability, debt.outstanding_paise,
                len(state.contacts), channel):
            return Decision(act=False, channel=channel, stop_reason=StopReason.NOT_WORTH_COST,
                            rules_fired=verdict.rules_fired + ["expected_value_positive"],
                            rules_passed=verdict.rules_passed, expected_value_paise=ev)

        return Decision(act=True, channel=channel, rules_fired=verdict.rules_fired,
                        rules_passed=verdict.rules_passed + ["expected_value_positive"],
                        expected_value_paise=ev)

    # -- feedback ---------------------------------------------------------------------

    def apply_outcome(self, debt: Debt, decision: Decision, now: dt.datetime,
                      succeeded: bool) -> None:
        state = self.state(debt.debt_id)
        if decision.channel is Channel.RETRY:
            state.retries_made += 1
        else:
            state.contacts.append(ContactRecord(decision.channel, now))
            state.last_contact_day = now.date()
            state.ladder_rung += 1
        if succeeded:
            state.hard_stopped = StopReason.PAID

    def record_reply(self, debt: Debt, customer: Customer, parsed: ParsedReply,
                     now: dt.datetime, text: str) -> None:
        """Interpret a parsed reply with RULES. The parser labelled it; this decides."""
        state = self.state(debt.debt_id)
        self.ledger.record_inbound(debt.debt_id, customer.ref, text, parsed.intent.value,
                                   parsed.promised_date, parsed.source)
        if parsed.intent is Intent.OPT_OUT:
            customer.opted_out = True
            state.hard_stopped = StopReason.OPTED_OUT
            # Statutory: the s.7(a) basis has evaporated on ALL channels, and the objection
            # must be an immutable event with propagation recorded.
            self.ledger.record_consent(
                customer.ref, event="objection_raised",
                basis="dpdp_s7a_conditional_on_no_objection",
                # Every channel in the ladder, not just the one they replied on. Opt-out
                # suppression is account-wide.
                propagated_to=sorted({c.value for step in self.policy.ladder for c in step}))
        elif parsed.intent is Intent.DISPUTE:
            customer.disputed = True
            state.hard_stopped = StopReason.DISPUTED
        elif parsed.intent is Intent.HARDSHIP:
            customer.bereaved_or_hardship = True
            state.hard_stopped = StopReason.BEREAVEMENT
        elif parsed.intent is Intent.PROMISE_TO_PAY:
            # A promise without a resolvable date still earns silence - a short one.
            state.promise_to_pay_until = parsed.promised_date or (now.date() + dt.timedelta(days=3))
