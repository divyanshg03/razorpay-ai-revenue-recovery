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
2. **Spread.** Retries at 0, 4, 8, 13, 17, 21 days instead of the incumbent's 0, 1, 2, 3.
   Two things differ and only one of them is the argument: this is **six attempts against
   four**, which is a real and separate advantage and is stated rather than folded into the
   next sentence; and the six are spread across the declared horizon, which takes coverage of
   a monthly salary cycle from 21/30 to 27/30. Retries are free in the frozen cost model, so
   the extra two attempts cost nothing and the comparison is not net of them.

   This paragraph previously read "the same number of attempts" over the pre-A1 schedule
   0, 3, 6, 9, 12, 15 — wrong count and wrong schedule. It was the fairness claim for the
   headline comparison, so it is corrected here and in `scripts/demo.py` rather than left to
   a reader to catch.
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

    def _first_reachable_rung(self, customer: Customer, start: int) -> int | None:
        for i in range(start, len(self.policy.ladder)):
            ch = self.policy.ladder[i]
            if ch is Channel.WHATSAPP_UTILITY and customer.has_whatsapp:
                return i
            if ch is Channel.SMS_SERVICE and customer.has_sms:
                return i
            if ch is Channel.HUMAN_CALL and (customer.has_sms or customer.has_whatsapp):
                return i
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
            # Skip rungs the customer cannot be reached on. Without this, the 18% of the
            # cohort with no WhatsApp sat on rung 0 forever and never received an SMS - a
            # recovery leak the guardrail correctly refused but nothing ever moved past.
            rung = self._first_reachable_rung(customer, state.ladder_rung)
            if rung is None:
                # Nothing reachable at all: consider the last rung once so the refusal is
                # logged with its reason, then retire the ladder.
                candidates.append(self.policy.ladder[-1])
                state.ladder_rung = len(self.policy.ladder)
            else:
                state.ladder_rung = rung
                candidates.append(self.policy.ladder[rung])
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
                # Latching turns a stop into a permanent one, which is right for payment,
                # objection and dispute. It is WRONG for a hardship stop that carries a
                # customer-named resume date, because that stop is temporary by
                # construction: the first evaluation before the date would latch the file
                # shut and the callback the customer asked for would never happen. Caught in
                # testing - the dated stop worked in isolation and failed in sequence.
                if not (decision.stop_reason is StopReason.BEREAVEMENT
                        and state.hardship_resume_on is not None):
                    state.hard_stopped = decision.stop_reason
                break
        return decisions

    def _decide(self, debt: Debt, customer: Customer, diag: Diagnosis, state: DebtState,
                channel: Channel, now: dt.datetime, settled: bool) -> Decision:
        # ORDER MATTERS, and it used to be wrong. The human-call amount floor is a COST rule,
        # and it used to return here before `evaluate()` had run at all. So an opted-out,
        # disputed or bereaved customer sitting on the human-call rung with a small debt was
        # logged as `escalation_ladder_exhausted` / `human_call_below_amount_floor`, with
        # `rules_passed=[]` - a cost decision standing in for a statutory one, and no evidence
        # in the record that the statutory check had run.
        #
        # Nobody was ever contacted: both paths return act=False, so this was an
        # audit-evidence defect rather than a compliance breach, and replaying a full trail
        # still showed the statutory reason on the surrounding records. But `guardrails.py`
        # promises in its own docstring that "a customer who has opted out is never even
        # evaluated for the contact window - the log shows the statutory reason, not an
        # incidental one", and the shipped Phase 3 ledger contained 90 records where that was
        # not true. A stopping rule you cannot evidence per-record is worth less than one you
        # can. Found 3 Sept 2026 by review; see amendment A8.
        verdict = evaluate(customer, debt, diag, state, channel, now, self.policy, settled)
        if not verdict.allowed:
            return Decision(act=False, channel=channel, stop_reason=verdict.stop_reason,
                            rules_fired=verdict.rules_fired, rules_passed=verdict.rules_passed)

        # Only once the customer may lawfully be contacted at all does cost get a say. Human
        # escalation below the floor is never worth it; treat the rung as absent.
        if channel is Channel.HUMAN_CALL and \
                debt.outstanding_paise < self.policy.min_amount_for_human_call_paise:
            state.ladder_rung = len(self.policy.ladder)
            return Decision(act=False, channel=channel, stop_reason=StopReason.LADDER_EXHAUSTED,
                            rules_fired=[*verdict.rules_fired, "human_call_below_amount_floor"],
                            rules_passed=verdict.rules_passed)

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
            self.ledger.record_consent(customer.ref, event="objection_raised",
                                       basis="dpdp_s7a_conditional_on_no_objection",
                                       propagated_to=[c.value for c in self.policy.ladder])
        elif parsed.intent is Intent.DISPUTE:
            customer.disputed = True
            state.hard_stopped = StopReason.DISPUTED
        elif parsed.intent is Intent.HARDSHIP:
            customer.bereaved_or_hardship = True
            # `hard_stopped` is deliberately NOT set on either branch: it is checked before
            # the dated rule and would override the customer's own answer, latching the file
            # shut on the first evaluation before the resume date.
            if parsed.promised_date and parsed.promised_date > now.date():
                # They told us when to come back. Their date always wins.
                state.hardship_resume_on = parsed.promised_date
            else:
                # No date given, so a POLICY pause rather than an indefinite stop. An
                # indefinite stop buries the debt and never contacts a customer who only
                # needed a fortnight; the length is a policy choice and is configurable.
                state.hardship_resume_on = now.date() + dt.timedelta(
                    days=self.policy.hardship_default_resume_days)
        elif parsed.intent is Intent.PROMISE_TO_PAY:
            # A promise without a resolvable date still earns silence - a short one.
            state.promise_to_pay_until = parsed.promised_date or (now.date() + dt.timedelta(days=3))
