"""Guardrails — every stopping rule, enforced in code, with both outcomes recorded.

Each check returns its name and whether it passed. The state machine records BOTH lists in
the ledger: `rules_fired` is the compliance evidence, and `rules_passed` proves a check ran
and cleared rather than never having run at all. A refusal with five passed checks and one
fired check is a reconstructable decision; a bare "did not contact" is not.

Order matters and is deliberate. The hard stops that end the relationship (paid, opted out,
disputed, hardship) run FIRST, so a customer who has opted out is never even evaluated for
the contact window — the log shows the statutory reason, not an incidental one.

Provenance of each rule is stated in `policy.py`. Re-read that table before adding a rule
here: the most valuable thing Phase 0 produced was the discovery that one "RBI-mandated" stop
was not in the regulation at all.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..diagnosis.taxonomy import Diagnosis
from ..models import Actionability, Channel, Customer, Debt, StopReason
from .policy import Policy, in_contact_window


@dataclass
class ContactRecord:
    channel: Channel
    at: dt.datetime


@dataclass
class DebtState:
    """What the engine knows about one debt. Mutable, owned by the state machine.

    NOTE the absence of anything about paydays, funds, or attention. The engine sees exactly
    what a real merchant sees: what it has done, and what it has heard back.
    """

    contacts: list[ContactRecord] = field(default_factory=list)
    retries_made: int = 0
    ladder_rung: int = 0
    promise_to_pay_until: dt.date | None = None
    #: Set only when a customer in hardship NAMES a date on which contact is welcome again.
    #: Until that date arrives the hardship stop holds; on and after it, contact resumes.
    #: Absent a date the stop is indefinite, which is the default and the safe direction.
    hardship_resume_on: dt.date | None = None
    hard_stopped: StopReason | None = None
    last_contact_day: dt.date | None = None

    def contacts_since(self, since: dt.datetime) -> list[ContactRecord]:
        return [c for c in self.contacts if c.at >= since]


@dataclass
class GuardrailVerdict:
    allowed: bool
    stop_reason: StopReason | None
    rules_fired: list[str]
    rules_passed: list[str]


def evaluate(customer: Customer, debt: Debt, diagnosis: Diagnosis, state: DebtState,
             channel: Channel, now: dt.datetime, policy: Policy,
             payment_settled: bool) -> GuardrailVerdict:
    """Run every guardrail for a proposed action. First failure wins; all passes recorded."""
    fired: list[str] = []
    passed: list[str] = []

    def check(name: str, ok: bool, reason: StopReason) -> StopReason | None:
        if ok:
            passed.append(name)
            return None
        fired.append(name)
        return reason

    # ---- hard stops: relationship-ending, statutory or policy, checked first --------
    # Payment received. Re-checked immediately before EVERY action because webhooks are
    # at-least-once and unordered; without this we dun people who have already paid.
    stop = check("payment_not_already_received", not payment_settled, StopReason.PAID)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    # Opt-out. STATUTORY: DPDP s.7(a) is conditional on no objection; on objection the
    # lawful basis evaporates. Suppressed on ALL channels, not just the one they used.
    stop = check("not_opted_out", not customer.opted_out, StopReason.OPTED_OUT)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    # Dispute. POLICY CHOICE, routed to a human. Not an RBI requirement (gate 0.6).
    stop = check("no_open_dispute", not customer.disputed, StopReason.DISPUTED)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    # Bereavement / hardship. POLICY CHOICE. The one case where a wrong answer is
    # unforgivable, so it is a code-level stop and never delegated to a model.
    # Hardship holds indefinitely UNLESS the customer named a date to be contacted on. Then
    # it holds until that date and lifts on it. `>=` rather than `==` so a missed day (a
    # weekend, an outage) does not silently bury the file forever.
    hardship_holds = customer.bereaved_or_hardship and not (
        state.hardship_resume_on is not None and now.date() >= state.hardship_resume_on)
    stop = check("no_hardship_flag", not hardship_holds, StopReason.BEREAVEMENT)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    if state.hard_stopped:
        fired.append("previously_hard_stopped")
        return GuardrailVerdict(False, state.hard_stopped, fired, passed)

    # ---- soft stops -----------------------------------------------------------------
    # Cause the merchant cannot act on. Contacting is pure noise.
    stop = check("cause_is_actionable", diagnosis.contactable, StopReason.NOT_ACTIONABLE)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    # A retry is silent and needs none of the contact rules below.
    if channel is Channel.RETRY:
        stop = check("retry_budget_remaining", state.retries_made < policy.max_retries_per_debt,
                     StopReason.LADDER_EXHAUSTED)
        if stop:
            return GuardrailVerdict(False, stop, fired, passed)
        # A dead instrument is never retried blind - that is the incumbent's 2,088 wasted
        # attempts. It IS retried once a contact has gone out, because the customer may have
        # supplied a new instrument in response, and only a charge attempt can find out.
        retry_ok = diagnosis.retryable or (
            diagnosis.actionability is Actionability.NEEDS_NEW_INSTRUMENT and bool(state.contacts))
        stop = check("cause_is_retryable_now", retry_ok, StopReason.NOT_ACTIONABLE)
        if stop:
            return GuardrailVerdict(False, stop, fired, passed)
        return GuardrailVerdict(True, None, fired, passed)

    # ---- contact-only rules ---------------------------------------------------------
    # Promise to pay silences CONTACTS until the promised date plus grace. Nagging someone
    # who has told you when they will pay is the "Nagging" dark pattern by name. A silent
    # retry on the promised date is how the promise is acted on, so retries are exempt.
    ptp_active = (state.promise_to_pay_until is not None
                  and now.date() <= state.promise_to_pay_until + dt.timedelta(days=policy.ptp_grace_days))
    stop = check("no_active_promise_to_pay", not ptp_active, StopReason.PROMISE_TO_PAY)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    reachable = (channel is Channel.WHATSAPP_UTILITY and customer.has_whatsapp) or \
                (channel is Channel.SMS_SERVICE and customer.has_sms) or \
                (channel is Channel.HUMAN_CALL and (customer.has_sms or customer.has_whatsapp))
    stop = check("channel_reachable", reachable, StopReason.NO_REACHABLE_CHANNEL)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    # 08:00-19:00 IST. Product invariant. Permission, not prohibition: default is denied.
    stop = check("inside_contact_window_0800_1900_ist", in_contact_window(now, policy),
                 StopReason.QUIET_HOURS)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    # 7-in-7 per debt. POLICY CHOICE (US Reg F), applied to messaging per RBI 454Z(4).
    week = now - dt.timedelta(days=7)
    stop = check("under_7_contacts_in_7_days",
                 len(state.contacts_since(week)) < policy.max_contacts_per_debt_7d,
                 StopReason.CONTACT_CAP)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    # Per-channel daily cap.
    day = now - dt.timedelta(hours=24)
    recent_same = [c for c in state.contacts_since(day) if c.channel is channel]
    per_day_cap = {Channel.WHATSAPP_UTILITY: policy.max_whatsapp_per_24h,
                   Channel.SMS_SERVICE: policy.max_sms_per_24h,
                   Channel.HUMAN_CALL: 1}[channel]
    stop = check("under_per_channel_24h_cap", len(recent_same) < per_day_cap,
                 StopReason.CONTACT_CAP)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    # Ladder terminates. There is no rung after the last one.
    stop = check("ladder_not_exhausted", state.ladder_rung < len(policy.ladder),
                 StopReason.LADDER_EXHAUSTED)
    if stop:
        return GuardrailVerdict(False, stop, fired, passed)

    return GuardrailVerdict(True, None, fired, passed)
