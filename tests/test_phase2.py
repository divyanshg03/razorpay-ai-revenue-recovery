"""Phase 2 exit criteria, as executable assertions.

The tests that matter most are the ZERO-ASSERTIONS in `TestInvariantsOverARealRun`: they run
the engine over a cohort, then REPLAY THE LEDGER with code that never touches the engine, and
require every guardrail violation count to be exactly zero. Guardrail metrics are assertions,
not values to report.

The Ollama tests exercise the real local model (no mock in the headline path). They skip only
if the server is unreachable, and say so loudly.
"""

from __future__ import annotations

from dataclasses import replace

import datetime as dt
import socket

import pytest

from recovery.cohort.simulator import FUNDS_WINDOW_DAYS, SimulatedCohort
from recovery.diagnosis.taxonomy import diagnose
from recovery.engine.guardrails import ContactRecord, DebtState, evaluate
from recovery.engine.machine import RecoveryEngine
from recovery.engine.policy import (Policy, expected_value_paise, in_contact_window,
                                    retry_schedule, worth_acting)
from recovery.evaluation.baselines import recovery_rate, run_incumbent_ladder
from recovery.evaluation.engine_arm import run_engine
from recovery.evaluation.invariants import check_ledger
from recovery.ledger.audit import AuditLedger, RecordType
from recovery.llm import composer
from recovery.llm.copy_gate import Facts, Verdict, check
from recovery.llm.parser import Intent, ParsedReply, parse_reply, resolve_date
from recovery.models import (IST, Actionability, Channel, Customer, Debt, MandateType,
                             PaymentFailure, StopReason)

SEED = 20260905
START = dt.date(2026, 9, 3)
P = Policy()


def ollama_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1):
            return True
    except OSError:
        return False


needs_ollama = pytest.mark.skipif(not ollama_up(), reason="Ollama not reachable on 11434 - "
                                  "the LLM path is untested in this run")


def debt(reason="insufficient_funds", amount=49900, failed=START) -> Debt:
    return Debt("debt_t", "cust_t", amount, MandateType.UPI_AUTOPAY,
                dt.datetime.combine(failed, dt.time(9, 0), tzinfo=IST),
                PaymentFailure("BAD_REQUEST_ERROR", reason, "issuer", "payment_authorization"))


def at(hour: int, minute: int = 0, day: dt.date = START) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)


def customer(**kw) -> Customer:
    return Customer("cust_t", **kw)


def verdict(chan=Channel.WHATSAPP_UTILITY, cust=None, d=None, state=None, now=None,
            settled=False):
    d = d or debt()
    return evaluate(cust or customer(), d, diagnose(d.failure), state or DebtState(), chan,
                    now or at(10), P, settled)


# ---------------------------------------------------------------------------------------
# 2.1 / 2.5 policy
# ---------------------------------------------------------------------------------------

def test_policy_is_versioned():
    assert P.version and P.version.count(".") >= 1


def test_contact_window_is_a_permission_not_a_prohibition():
    assert not in_contact_window(at(7, 59), P)
    assert in_contact_window(at(8, 0), P)
    assert in_contact_window(at(18, 59), P)
    assert not in_contact_window(at(19, 0), P)
    assert not in_contact_window(at(23, 30), P)


def test_retries_are_spread_across_the_cycle_not_clustered():
    days = retry_schedule(P)
    assert days[0] == 0 and len(days) == P.max_retries_per_debt
    assert days != (0, 1, 2, 3)


def test_the_last_retry_lands_ON_the_declared_horizon_not_short_of_it():
    """Regression, 2 Sept 2026. The schedule used to be a fixed-spacing list truncated to the
    retry budget - 0,3,6,9,12,15,18,21 cut to the first six - so `retry_horizon_days = 21`
    was declared and never reached, and the last six days of the window got no attempt.

    The old assertion here was `max(days) >= 15`, which is exactly why the suite did not
    catch it: it tested the value the bug happened to produce. This asserts the PROPERTY the
    docstring promises instead, so any future change to the budget or the floor still has to
    cover the horizon it advertises.

    Worth 489 of 1,171 unrecovered debts, all of them funded inside the stated window.
    """
    days = retry_schedule(P)
    assert max(days) == P.retry_horizon_days, (
        f"schedule stops at day {max(days)} but the policy advertises "
        f"{P.retry_horizon_days}; the tail of the window is unattempted")


def test_spacing_is_a_throttle_FLOOR_that_is_never_breached():
    """Issuers throttle repeated mandate execution, so attempts may be spread further apart
    than `retry_spacing_days` but never packed closer. Checked across a range of budgets,
    including ones too large to spread across the horizon at all."""
    for n in range(1, 25):
        days = retry_schedule(replace(P, max_retries_per_debt=n))
        assert all(b - a >= P.retry_spacing_days for a, b in zip(days, days[1:])), (n, days)
        assert all(0 <= d <= P.retry_horizon_days for d in days), (n, days)
        assert len(days) <= n
        assert len(set(days)) == len(days), (n, days)


def test_schedule_degrades_sanely_at_the_edges():
    assert retry_schedule(replace(P, max_retries_per_debt=0)) == ()
    assert retry_schedule(replace(P, max_retries_per_debt=1)) == (0,)


def test_the_schedule_covers_more_of_a_salary_cycle_than_the_incumbent_ladder():
    """The whole thesis in one assertion. Coverage is the share of the 30 possible payday
    positions whose post-payday funded window overlaps at least one attempt. The incumbent
    fires four attempts inside four days, so it can only ever catch a payday that has just
    happened; spreading the SAME budget across the horizon is what buys the rest."""
    def coverage(days):
        return len({(d - k) % 30 for d in days for k in range(FUNDS_WINDOW_DAYS)})

    assert coverage(retry_schedule(P)) > coverage((0, 1, 2, 3))


def test_messaging_is_almost_never_cost_bound_but_a_human_call_eventually_is():
    """The gate-0.7 finding, encoded. At Rs 0.18 a message, the cost gate never closes on
    WhatsApp even after four ignored contacts have decayed the prior to ~1.6%. The human
    call, at Rs 15.34, is the one rung the money gate actually shuts."""
    assert worth_acting(P, Actionability.NEEDS_FUNDS, 49900, 0, Channel.WHATSAPP_UTILITY)
    assert worth_acting(P, Actionability.NEEDS_FUNDS, 49900, 4, Channel.WHATSAPP_UTILITY)
    assert worth_acting(P, Actionability.NEEDS_FUNDS, 49900, 0, Channel.HUMAN_CALL)
    assert not worth_acting(P, Actionability.NEEDS_FUNDS, 49900, 4, Channel.HUMAN_CALL)
    # Small debts never reach a human regardless of EV: that is the amount floor's job.
    # A Rs 499 debt is below the floor; a Rs 4,999 debt is above it.
    assert 49900 < P.min_amount_for_human_call_paise <= 499900


def test_expected_value_decays_with_each_ignored_contact():
    ev0 = expected_value_paise(P, Actionability.NEEDS_FUNDS, 49900, 0, Channel.SMS_SERVICE)
    ev3 = expected_value_paise(P, Actionability.NEEDS_FUNDS, 49900, 3, Channel.SMS_SERVICE)
    assert ev3 < ev0


# ---------------------------------------------------------------------------------------
# 2.2 / 2.3 guardrails - each one proven to BLOCK, not merely to exist
# ---------------------------------------------------------------------------------------

def test_quiet_hours_block_contact_and_are_logged_with_the_passes_that_ran():
    v = verdict(now=at(7, 30))
    assert not v.allowed and v.stop_reason is StopReason.QUIET_HOURS
    assert "inside_contact_window_0800_1900_ist" in v.rules_fired
    assert "not_opted_out" in v.rules_passed  # the checks before it ran and cleared


def test_opt_out_is_a_hard_stop_checked_before_anything_incidental():
    v = verdict(cust=customer(opted_out=True), now=at(3))  # also outside window
    assert v.stop_reason is StopReason.OPTED_OUT  # statutory reason wins, not quiet hours


def test_dispute_and_hardship_are_hard_stops():
    assert verdict(cust=customer(disputed=True)).stop_reason is StopReason.DISPUTED
    assert verdict(cust=customer(bereaved_or_hardship=True)).stop_reason is StopReason.BEREAVEMENT


def test_payment_already_received_blocks_everything_including_retry():
    assert verdict(settled=True).stop_reason is StopReason.PAID
    assert verdict(chan=Channel.RETRY, settled=True).stop_reason is StopReason.PAID


def test_promise_to_pay_silences_contacts_but_permits_the_retry_on_the_date():
    st = DebtState(promise_to_pay_until=START + dt.timedelta(days=4))
    assert verdict(state=st).stop_reason is StopReason.PROMISE_TO_PAY
    assert verdict(chan=Channel.RETRY, state=st).allowed


def test_dead_instrument_is_never_retried_blind_but_is_after_a_contact():
    d = debt("card_expired")
    assert verdict(chan=Channel.RETRY, d=d).stop_reason is StopReason.NOT_ACTIONABLE
    st = DebtState(contacts=[ContactRecord(Channel.WHATSAPP_UTILITY, at(10))])
    assert verdict(chan=Channel.RETRY, d=d, state=st).allowed


def test_do_not_contact_causes_are_dropped():
    d = debt("payment_frequency_exceeded")
    assert verdict(d=d).stop_reason is StopReason.NOT_ACTIONABLE


def test_seven_in_seven_cap_applies_to_messaging():
    st = DebtState(contacts=[ContactRecord(Channel.SMS_SERVICE, at(10, day=START - dt.timedelta(days=i)))
                             for i in range(7)])
    assert verdict(chan=Channel.SMS_SERVICE, state=st, now=at(10, day=START + dt.timedelta(days=1))
                   ).stop_reason is StopReason.CONTACT_CAP


def test_per_channel_daily_cap():
    st = DebtState(contacts=[ContactRecord(Channel.WHATSAPP_UTILITY, at(9))])
    assert verdict(state=st, now=at(15)).stop_reason is StopReason.CONTACT_CAP
    assert verdict(chan=Channel.SMS_SERVICE, state=st, now=at(15)).allowed


def test_ladder_terminates():
    st = DebtState(ladder_rung=len(P.ladder))
    assert verdict(state=st).stop_reason is StopReason.LADDER_EXHAUSTED


def test_unreachable_channel_is_a_stop_not_a_crash():
    assert verdict(cust=customer(has_whatsapp=False)).stop_reason is StopReason.NO_REACHABLE_CHANNEL


# ---------------------------------------------------------------------------------------
# 2.4 state machine
# ---------------------------------------------------------------------------------------

def test_every_considered_action_is_in_the_ledger_including_refusals(tmp_path):
    led = AuditLedger(tmp_path / "a.jsonl", P.version)
    eng = RecoveryEngine(P, led, is_settled=lambda d: False)
    cust = customer(opted_out=True)
    decisions = eng.plan_day(debt(), cust, at(10))
    assert decisions and not any(d.act for d in decisions)
    kinds = [e["type"] for e in led.read()]
    assert RecordType.STATE_RECHECK.value in kinds and RecordType.DECISION.value in kinds
    assert led.read()[-1]["body"]["stop_reason"] == "opt_out"


def test_retry_is_considered_before_contact_and_first_contact_waits_a_day(tmp_path):
    led = AuditLedger(tmp_path / "a.jsonl", P.version)
    eng = RecoveryEngine(P, led, is_settled=lambda d: False)
    day0 = eng.plan_day(debt(), customer(), at(10))
    assert [d.channel for d in day0] == [Channel.RETRY]
    day1 = eng.plan_day(debt(), customer(), at(10, day=START + dt.timedelta(days=1)))
    assert [d.channel for d in day1] == [Channel.WHATSAPP_UTILITY]


def test_ladder_escalates_one_rung_per_wait_and_then_stops(tmp_path):
    led = AuditLedger(tmp_path / "a.jsonl", P.version)
    eng = RecoveryEngine(P, led, is_settled=lambda d: False)
    d = debt(amount=499900)  # big enough for the human rung
    seen = []
    for off in range(1, 30):
        now = at(10, day=START + dt.timedelta(days=off))
        for dec in eng.plan_day(d, customer(), now):
            if dec.act and dec.channel is not Channel.RETRY:
                seen.append(dec.channel)
                eng.apply_outcome(d, dec, now, succeeded=False)
            elif dec.channel is Channel.RETRY and dec.act:
                eng.apply_outcome(d, dec, now, succeeded=False)
    assert seen == list(P.ladder)  # each rung once, in order, then nothing


@pytest.mark.parametrize("flag,expected", [
    ("opted_out", StopReason.OPTED_OUT),
    ("disputed", StopReason.DISPUTED),
    ("bereaved_or_hardship", StopReason.BEREAVEMENT),
])
def test_a_statutory_stop_is_never_logged_as_a_cost_decision(tmp_path, flag, expected):
    """Regression, 3 Sept 2026. The human-call amount floor is a COST rule, and it used to
    return BEFORE the guardrails had run at all.

    So an opted-out, disputed or bereaved customer sitting on the human-call rung with a
    small debt was recorded as `escalation_ladder_exhausted` / `human_call_below_amount_floor`
    with `rules_passed=[]` - a cost reason standing in for a statutory one, and no evidence in
    the record that the statutory check had ever run. The shipped Phase 3 ledger contained 90
    such records.

    Nobody was ever contacted - both paths return act=False - so this was an audit-evidence
    defect rather than a compliance breach. But `guardrails.py` promises in its own docstring
    that the log shows the statutory reason rather than an incidental one, and a stopping rule
    you cannot evidence per-record is worth less than one you can.
    """
    led = AuditLedger(tmp_path / f"{flag}.jsonl", P.version)
    eng = RecoveryEngine(P, led, is_settled=lambda d: False)
    d = debt(amount=99900)                      # below the human-call floor
    eng.state(d.debt_id).ladder_rung = len(P.ladder) - 1   # the human rung
    c = customer(**{flag: True})

    seen = [dec for dec in eng.plan_day(d, c, at(10, day=START + dt.timedelta(days=7)))
            if dec.channel is Channel.HUMAN_CALL]
    assert seen, "the human rung was never evaluated"
    for dec in seen:
        assert dec.stop_reason is expected, dec.stop_reason
        assert "human_call_below_amount_floor" not in dec.rules_fired
        assert dec.rules_passed, "no evidence the statutory check ran"


def test_the_cost_floor_still_applies_once_the_guardrails_pass(tmp_path):
    """The fix must not disable the floor for customers who may lawfully be contacted."""
    led = AuditLedger(tmp_path / "clean.jsonl", P.version)
    eng = RecoveryEngine(P, led, is_settled=lambda d: False)
    d = debt(amount=99900)
    eng.state(d.debt_id).ladder_rung = len(P.ladder) - 1
    seen = [dec for dec in eng.plan_day(d, customer(), at(10, day=START + dt.timedelta(days=7)))
            if dec.channel is Channel.HUMAN_CALL]
    assert seen
    for dec in seen:
        assert dec.act is False
        assert dec.stop_reason is StopReason.LADDER_EXHAUSTED
        assert "human_call_below_amount_floor" in dec.rules_fired


def test_human_call_floor_is_the_measured_one_not_the_original(tmp_path):
    """Pins the 2 Sept decision: the floor moved Rs 500 -> Rs 2,000 because the human rung
    was 91.7% of modelled spend for no measurable recovery.

    A Rs 999 debt would have earned a human call under the old floor and must not now.
    """
    assert P.min_amount_for_human_call_paise == 200_000
    led = AuditLedger(tmp_path / "floor.jsonl", P.version)
    eng = RecoveryEngine(P, led, is_settled=lambda d: False)
    d = debt(amount=99900)  # Rs 999: above the OLD floor, below the new one
    channels = []
    for off in range(1, 30):
        now = at(10, day=START + dt.timedelta(days=off))
        for dec in eng.plan_day(d, customer(), now):
            if dec.act:
                if dec.channel is not Channel.RETRY:
                    channels.append(dec.channel)
                eng.apply_outcome(d, dec, now, succeeded=False)
    assert Channel.HUMAN_CALL not in channels
    assert channels == [Channel.WHATSAPP_UTILITY, Channel.SMS_SERVICE]


def test_human_rung_is_skipped_below_the_amount_floor(tmp_path):
    led = AuditLedger(tmp_path / "a.jsonl", P.version)
    eng = RecoveryEngine(P, led, is_settled=lambda d: False)
    d = debt(amount=14900)
    contacts = []
    for off in range(1, 30):
        now = at(10, day=START + dt.timedelta(days=off))
        for dec in eng.plan_day(d, customer(), now):
            if dec.act and dec.channel is not Channel.RETRY:
                contacts.append(dec.channel)
            if dec.act:
                eng.apply_outcome(d, dec, now, succeeded=False)
    assert Channel.HUMAN_CALL not in contacts


def test_opt_out_reply_ends_everything_and_writes_a_consent_record(tmp_path):
    led = AuditLedger(tmp_path / "a.jsonl", P.version)
    eng = RecoveryEngine(P, led, is_settled=lambda d: False)
    cust, d = customer(), debt()
    eng.record_reply(d, cust, ParsedReply(Intent.OPT_OUT, None, None, "override"), at(11),
                     "STOP messaging me")
    assert cust.opted_out and eng.state(d.debt_id).hard_stopped is StopReason.OPTED_OUT
    types = [e["type"] for e in led.read()]
    assert "inbound" in types and "consent" in types
    later = eng.plan_day(d, cust, at(10, day=START + dt.timedelta(days=3)))
    assert not any(x.act for x in later)


def test_promise_to_pay_then_retry_on_the_named_date(tmp_path):
    led = AuditLedger(tmp_path / "a.jsonl", P.version)
    eng = RecoveryEngine(P, led, is_settled=lambda d: False)
    cust, d = customer(), debt()
    promised = START + dt.timedelta(days=5)
    eng.record_reply(d, cust, ParsedReply(Intent.PROMISE_TO_PAY, promised, "the 8th", "llm"),
                     at(11), "will pay on the 8th")
    silent = eng.plan_day(d, cust, at(10, day=START + dt.timedelta(days=2)))
    assert not any(x.act and x.channel is not Channel.RETRY for x in silent)
    on_date = eng.plan_day(d, cust, at(10, day=promised))
    assert any(x.act and x.channel is Channel.RETRY for x in on_date)


# ---------------------------------------------------------------------------------------
# 2.8 copy gate - demonstrated on a REAL model-generated violation
# ---------------------------------------------------------------------------------------

MINISTRAL_REAL_OUTPUT = ("Your Rs 999 autopay failed. Update payment details now to avoid service "
                         "disruption. Limited-time bonus: 10% extra data on next recharge. "
                         "Offer valid until 31st July.")
LLAMA_REAL_REFUSAL = ("I cannot write a message that pushes the customer to pay right away. "
                      "Is there something else I can help you with?")
FACTS = Facts(amount_paise=99900, link="https://rzp.io/rzp/abc123")


def test_gate_catches_the_real_ministral_violation_and_names_why():
    r = check(MINISTRAL_REAL_OUTPUT, FACTS)
    assert r.verdict is Verdict.REJECTED
    assert "discount_or_offer" in r.categories
    assert {"bonus", "offer", "extra data"} & set(r.categories["discount_or_offer"])
    assert "false_urgency" in r.categories and "limited-time" in r.categories["false_urgency"]
    assert "fabricated_date" in r.categories
    assert any("exceeds" in x for x in r.reasons) and "payment link missing" in r.reasons


def test_a_refusal_is_not_a_violation():
    """The v0 lexicon flagged this on 'right away'. A refusal sends nothing; it is not drift."""
    assert check(LLAMA_REAL_REFUSAL, FACTS).verdict is Verdict.REFUSED


def test_suspension_is_caught_now():
    r = check("Please pay to avoid service suspension. https://rzp.io/rzp/abc123", FACTS)
    assert r.verdict is Verdict.REJECTED and "suspension" in r.categories["threat_or_shaming"]


def test_gate_rejects_fabricated_amounts_dates_and_foreign_urls():
    r = check("Your payment of Rs 1,499 failed on 12/05/2024. Retry at https://evil.example/x", FACTS)
    assert r.verdict is Verdict.REJECTED
    assert "fabricated_amount" in r.categories and "fabricated_date" in r.categories
    assert "foreign_url" in r.categories


def test_clean_message_passes():
    r = check("Your payment of Rs 999 did not go through. You can retry here: "
              "https://rzp.io/rzp/abc123", FACTS)
    assert r.ok


@pytest.mark.parametrize("act", list(Actionability))
def test_every_fallback_template_passes_the_gate(act):
    text = composer.template(FACTS, act)
    r = check(text, FACTS)
    assert r.ok, (act, r.categories, r.reasons)
    assert len(text) <= 160


# ---------------------------------------------------------------------------------------
# 2.7 reply parser - dates are code, and the three heavy intents are code
# ---------------------------------------------------------------------------------------

TODAY = dt.date(2026, 8, 31)  # a Monday, same anchor as the bake-off


@pytest.mark.parametrize("phrase,expected", [
    ("tonight", dt.date(2026, 8, 31)),
    ("tomorrow morning", dt.date(2026, 9, 1)),
    ("the 5th", dt.date(2026, 9, 5)),
    ("next monday", dt.date(2026, 9, 7)),
    ("monday", dt.date(2026, 9, 7)),          # today is Monday; next occurrence, never today
    ("in 3 days", dt.date(2026, 9, 3)),
    ("next week", dt.date(2026, 9, 7)),
    ("end of month", dt.date(2026, 8, 31)),
    ("after salary", None),                   # unknown -> None, never a guess
])
def test_resolve_date(phrase, expected):
    assert resolve_date(phrase, TODAY) == expected


def test_a_resolved_date_is_never_in_the_past():
    """qwen3 resolved 'the 5th' to a past date. The resolver rolls forward instead."""
    assert resolve_date("the 5th", dt.date(2026, 9, 6)) == dt.date(2026, 10, 5)
    # A day the current month does not have clamps to month-end rather than skipping a
    # month: the SHORTER silence is the safer error for a promise-to-pay.
    assert resolve_date("on 31st", dt.date(2026, 9, 6)) == dt.date(2026, 9, 30)
    assert resolve_date("on 31st", dt.date(2026, 9, 30)) == dt.date(2026, 10, 31)


def test_bereavement_never_resolves_to_promise_to_pay():
    """ministral-3 got this wrong in the bake-off. Code decides it now."""
    p = parse_reply("my father passed away last week, I need some time", TODAY, use_llm=False)
    assert p.intent is Intent.HARDSHIP and p.source == "override"


def test_opt_out_and_dispute_are_decided_by_code():
    assert parse_reply("STOP. do not message me again", TODAY, use_llm=False).intent is Intent.OPT_OUT
    assert parse_reply("I already paid this, check your records", TODAY, use_llm=False).intent is Intent.DISPUTE


def test_keyword_fallback_finds_a_promise_without_the_model():
    p = parse_reply("will pay on the 5th", TODAY, use_llm=False)
    assert p.intent is Intent.PROMISE_TO_PAY and p.promised_date == dt.date(2026, 9, 5)


# ---------------------------------------------------------------------------------------
# THE ZERO-ASSERTIONS: run the engine, then replay the ledger with independent code
# ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def run(tmp_path_factory):
    path = tmp_path_factory.mktemp("ledger") / "audit.jsonl"
    led = AuditLedger(path, P.version)
    cohort = SimulatedCohort(seed=SEED, n_customers=1500, start=START)
    outcomes = run_engine(cohort, cohort.debts(), cohort.customers(), START, 21, P, led)
    return led, cohort, outcomes


class TestInvariantsOverARealRun:
    def test_every_guardrail_violation_count_is_zero(self, run):
        led, _, _ = run
        counts = check_ledger(led, max_contacts_7d=P.max_contacts_per_debt_7d)
        assert all(v == 0 for v in counts.values()), counts

    def test_stopping_rules_actually_fired_in_the_run(self, run):
        """Zero violations would be vacuous if no stop signal ever arrived."""
        led, _, _ = run
        inbound = [e["body"]["intent"] for e in led.read() if e["type"] == "inbound"]
        assert {"opt_out", "dispute", "hardship", "promise_to_pay"} <= set(inbound)

    def test_refusals_outnumber_actions(self, run):
        """The engine declines more often than it acts. That is what guardrails look like."""
        led, _, _ = run
        s = led.summary()
        assert s["decisions_declined"] > 0 and s["chain_intact"]

    def test_engine_beats_the_incumbent_on_the_same_seed(self, run):
        """Sanity only - NOT the headline. Phase 3 measures this properly, with a holdout
        and a confidence interval, against the frozen definition."""
        _, _, outcomes = run
        b = SimulatedCohort(seed=SEED, n_customers=1500, start=START)
        incumbent = recovery_rate(run_incumbent_ladder(b, b.debts(), START, 21))
        assert recovery_rate(outcomes) > incumbent

    def test_dead_instruments_are_only_retried_after_a_contact(self, run):
        led, _, _ = run
        first_contact, first_retry = {}, {}
        for e in led.read():
            b = e["body"]
            if e["type"] == "action":
                first_contact.setdefault(b["debt_id"], e["timestamp_ist"])
            if e["type"] == "decision" and b["acted"] and b["channel"] == "retry" \
                    and b["diagnosis"] == "needs_new_instrument":
                first_retry.setdefault(b["debt_id"], e["timestamp_ist"])
        for d, t in first_retry.items():
            assert d in first_contact and first_contact[d] <= t


# ---------------------------------------------------------------------------------------
# The real LLM. No mock. Skips only if Ollama is down.
# ---------------------------------------------------------------------------------------

def test_gate_verdict_always_describes_the_message_actually_returned():
    """Regression: compose() used to return the template as `text` while carrying the
    REJECTED LLM candidate's verdict in `gate`. A caller gating on `msg.gate.ok` would then
    refuse to send perfectly valid fallback copy - dropping the contact entirely instead of
    degrading to a template, the opposite of the intended failure mode.

    Driven without the model so the fallback path is exercised deterministically.
    """
    msg = composer.compose(FACTS, Actionability.NEEDS_FUNDS, use_llm=False)
    assert msg.source == "template"
    assert msg.gate.ok, "the returned template must pass its own gate"
    assert msg.gate is not None and msg.text
    # The gate result must be the one for `text`, so re-checking `text` agrees with it.
    assert check(msg.text, FACTS).verdict is msg.gate.verdict


def test_a_rejected_llm_candidate_is_preserved_separately_not_conflated(monkeypatch):
    """When the gate rejects the model, the template ships AND the rejection is kept."""
    monkeypatch.setattr(composer, "_ask", lambda *a, **k: (MINISTRAL_REAL_OUTPUT, 0.1))
    msg = composer.compose(FACTS, Actionability.NEEDS_FUNDS, use_llm=True)
    assert msg.source == "template"
    assert msg.gate.ok, "the template we actually send must pass"
    assert msg.gate_rejected_llm and msg.llm_gate is not None
    assert msg.llm_gate.verdict is Verdict.REJECTED
    assert "discount_or_offer" in msg.llm_gate.categories
    assert msg.llm_output == MINISTRAL_REAL_OUTPUT


def test_copy_gate_rejection_reaches_the_audit_ledger(tmp_path):
    """A gate nobody can see firing is indistinguishable from one that never fires."""
    from recovery.models import Action
    led = AuditLedger(tmp_path / "gate.jsonl", P.version)
    led.record_action(
        Action(debt_id="d1", customer_ref="c1", channel=Channel.SMS_SERVICE, at=at(10),
               cost_paise=22, policy_version=P.version, rendered_text="template text"),
        copy_gate_rejected={"verdict": "rejected",
                            "categories": {"discount_or_offer": ["bonus"]}, "reasons": []},
        llm_output=MINISTRAL_REAL_OUTPUT)
    body = led.read()[0]["body"]
    assert body["copy_gate_rejected_llm"]["verdict"] == "rejected"
    assert body["llm_output_rejected"] == MINISTRAL_REAL_OUTPUT
    assert body["rendered_text"] == "template text"   # what was SENT, not what was blocked


@needs_ollama
def test_composer_uses_the_real_model_and_the_link_is_injected_by_code():
    composer.warm()
    msg = composer.compose(FACTS, Actionability.NEEDS_FUNDS, use_llm=True)
    assert FACTS.link in msg.text and len(msg.text) <= 160
    # `gate` describes what is being sent, so it must pass either way now.
    assert msg.gate.ok, (msg.source, msg.gate.categories, msg.gate.reasons)
    if msg.source == "llm":
        assert "http" not in (msg.llm_output or "")   # the model never wrote the link
        assert not msg.gate_rejected_llm


@needs_ollama
def test_parser_uses_the_real_model_but_code_resolves_the_date():
    p = parse_reply("cant pay till salary comes on the 5th", TODAY, use_llm=True)
    assert p.intent is Intent.PROMISE_TO_PAY
    assert p.promised_date == dt.date(2026, 9, 5)   # never 2026-08-05
    assert p.source == "llm" and p.date_phrase
