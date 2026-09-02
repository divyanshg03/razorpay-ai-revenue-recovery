"""Phase 1 exit criteria, as executable assertions.

The important test here is `test_incumbent_ladder_lands_in_published_band`. It is a tripwire on
the WORLD MODEL, not on our engine: if the reimplemented Razorpay ladder does not land near the
20-30% that published figures imply for un-timed automated retries, the simulator is wrong and
must be fixed BEFORE anyone looks at an arm-C number. Checking that afterwards would be
indistinguishable from tuning until the answer flattered us.
"""

from __future__ import annotations

import datetime as dt

import pytest

from recovery.cohort.simulator import SimulatedCohort
from recovery.diagnosis.taxonomy import coverage, diagnose
from recovery.evaluation.baselines import (
    recovery_rate,
    run_do_nothing,
    run_incumbent_ladder,
)
from recovery.ingest.webhook import EventStore, WebhookIngest, verify_signature
from recovery.ledger.audit import AuditLedger
from recovery.models import Actionability, Decision, PaymentFailure, StopReason

SEED = 20260905
START = dt.date(2026, 9, 3)
WINDOW = 21


def make_cohort(n=800, seed=SEED, shifted=False):
    return SimulatedCohort(seed=seed, n_customers=n, start=START, shifted=shifted)


# ---------------------------------------------------------------------------------------
# 1.2 cohort
# ---------------------------------------------------------------------------------------

def test_cohort_is_reproducible_from_the_seed():
    a, b = make_cohort(), make_cohort()
    assert [d.debt_id for d in a.debts()] == [d.debt_id for d in b.debts()]
    assert [d.amount_paise for d in a.debts()] == [d.amount_paise for d in b.debts()]
    assert [d.failure.error_reason for d in a.debts()] == [d.failure.error_reason for d in b.debts()]


def test_cohort_generation_is_order_independent():
    """Customer N must not depend on how many draws customers 1..N-1 made.

    Otherwise an unrelated code change shifts the whole cohort and the "reproducible batch"
    promise in the frozen metric quietly stops being true.
    """
    small, large = make_cohort(n=50), make_cohort(n=500)
    assert [d.amount_paise for d in small.debts()] == [d.amount_paise for d in large.debts()[:50]]


def test_cohort_declares_that_it_is_simulated():
    assert "SIMULATED" in make_cohort().provenance
    assert "PARAMETERS.md" in make_cohort().provenance


def test_shifted_cohort_really_differs():
    """The eval cohort must not be the training cohort with a new label."""
    base = [d.failure.error_reason for d in make_cohort().debts()]
    shifted = [d.failure.error_reason for d in make_cohort(shifted=True).debts()]
    assert base != shifted


# ---------------------------------------------------------------------------------------
# 1.3 diagnosis
# ---------------------------------------------------------------------------------------

def test_dead_instrument_is_never_retryable():
    """The single most expensive misclassification available: retrying a dead card forty
    times recovers nothing and annoys someone."""
    for reason in ("card_expired", "card_blocked", "invalid_card", "mandate_revoked"):
        d = diagnose(PaymentFailure(error_code="BAD_REQUEST_ERROR", error_reason=reason))
        assert d.actionability is Actionability.NEEDS_NEW_INSTRUMENT
        assert not d.retryable, f"{reason} must not be retryable"


def test_insufficient_funds_is_retryable_and_both_spellings_map():
    """Razorpay's docs use `insufficient_fund` AND `insufficient_funds`. Gate 0.2 could not
    settle which arrives, so both must map."""
    for reason in ("insufficient_funds", "insufficient_fund"):
        d = diagnose(PaymentFailure(error_code="BAD_REQUEST_ERROR", error_reason=reason))
        assert d.actionability is Actionability.NEEDS_FUNDS
        assert d.retryable


def test_classification_does_not_key_on_error_code():
    """Same error_code, opposite handling — proof the composite key is doing the work."""
    funds = diagnose(PaymentFailure(error_code="BAD_REQUEST_ERROR", error_reason="insufficient_funds"))
    expired = diagnose(PaymentFailure(error_code="BAD_REQUEST_ERROR", error_reason="card_expired"))
    assert funds.retryable and not expired.retryable


def test_generic_test_mode_reason_is_flagged_not_guessed():
    """Gate 0.2: test mode returns `payment_failed` for everything. Pretending to know the
    real cause would be a fabrication."""
    d = diagnose(PaymentFailure(error_code="BAD_REQUEST_ERROR", error_reason="payment_failed"))
    assert d.generic and not d.unmapped


def test_unknown_reason_is_counted_not_silently_bucketed():
    d = diagnose(PaymentFailure(error_code="BAD_REQUEST_ERROR", error_reason="brand_new_reason"))
    assert d.unmapped
    cov = coverage([f.failure for f in make_cohort(n=200).debts()])
    assert cov["mapped"] + cov["unmapped"] + cov["generic"] == pytest.approx(1.0, abs=1e-3)


def test_do_not_contact_causes_are_not_contactable():
    d = diagnose(PaymentFailure(error_code="BAD_REQUEST_ERROR",
                                error_reason="payment_frequency_exceeded"))
    assert not d.contactable


# ---------------------------------------------------------------------------------------
# 1.6 THE TRIPWIRE — validates the world model before any engine number is trusted
# ---------------------------------------------------------------------------------------

def test_do_nothing_arm_recovers_little():
    """If self-cure were high, failed payments would not be a business problem at all."""
    cohort = make_cohort(n=1500)
    rate = recovery_rate(run_do_nothing(cohort, cohort.debts(), START, WINDOW))
    assert 0.0 < rate < 0.15, f"do-nothing recovery {rate} is not plausible"


def test_incumbent_ladder_lands_in_published_band():
    """Un-timed automated retries recover roughly 20-30% (PARAMETERS.md section 3).

    THIS IS A CHECK ON THE SIMULATOR, NOT ON OUR ENGINE. A failure here means the world model
    is wrong, and it must be fixed before any arm-C figure is looked at.
    """
    cohort = make_cohort(n=1500)
    rate = recovery_rate(run_incumbent_ladder(cohort, cohort.debts(), START, WINDOW))
    assert 0.15 <= rate <= 0.35, (
        f"incumbent ladder recovered {rate:.1%}, outside the plausible 20-30% band. "
        "The world model is wrong — fix it before looking at arm C."
    )


def test_ladder_beats_doing_nothing():
    """Sanity: the incumbent is not a straw man. If retries did not help, the whole premise
    that we must beat them would be dishonest."""
    a = make_cohort(n=1200)
    b = make_cohort(n=1200)
    assert recovery_rate(run_incumbent_ladder(b, b.debts(), START, WINDOW)) > \
           recovery_rate(run_do_nothing(a, a.debts(), START, WINDOW))


# ---------------------------------------------------------------------------------------
# 1.4 audit ledger
# ---------------------------------------------------------------------------------------

def test_ledger_records_refusals_as_fully_as_actions(tmp_path):
    """A system that logs only what it did cannot evidence its own stopping rules."""
    led = AuditLedger(tmp_path / "audit.jsonl", policy_version="v1")
    led.record_decision("debt_1", "cust_1",
                        Decision(act=False, stop_reason=StopReason.QUIET_HOURS,
                                 rules_fired=["contact_window"],
                                 rules_passed=["not_opted_out", "under_cap"]),
                        diagnosis="needs_funds")
    entry = led.read()[0]
    assert entry["body"]["acted"] is False
    assert entry["body"]["stop_reason"] == "outside_contact_window"
    assert entry["body"]["rules_passed"] == ["not_opted_out", "under_cap"]


def test_ledger_pins_the_policy_version_at_write_time(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLedger(path, policy_version="v1").record_decision(
        "debt_1", "cust_1", Decision(act=True), diagnosis="x")
    AuditLedger(path, policy_version="v2").record_decision(
        "debt_2", "cust_2", Decision(act=True), diagnosis="x")
    versions = [e["policy_version"] for e in AuditLedger(path, "v3").read()]
    assert versions == ["v1", "v2"], "an old decision must keep the version that governed it"


def test_ledger_chain_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    led = AuditLedger(path, policy_version="v1")
    for i in range(4):
        led.record_decision(f"debt_{i}", f"cust_{i}", Decision(act=True), diagnosis="x")
    assert led.verify_chain() == (True, None)

    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = lines[2].replace('"acted": true', '"acted": false')
    lines[2] = tampered
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    intact, broken_at = AuditLedger(path, "v1").verify_chain()
    assert not intact and broken_at == 2


def test_ledger_resumes_an_existing_chain_after_restart(tmp_path):
    """A crashed run must EXTEND the chain. Starting a second one in the same file would be
    indistinguishable from tampering."""
    path = tmp_path / "audit.jsonl"
    AuditLedger(path, "v1").record_decision("d1", "c1", Decision(act=True), diagnosis="x")
    AuditLedger(path, "v1").record_decision("d2", "c1", Decision(act=True), diagnosis="x")
    intact, _ = AuditLedger(path, "v1").verify_chain()
    assert intact


def test_ledger_replays_one_debt_in_order(tmp_path):
    led = AuditLedger(tmp_path / "audit.jsonl", policy_version="v1")
    led.record_decision("debt_A", "c1", Decision(act=True), diagnosis="needs_funds")
    led.record_state_recheck("debt_A", already_paid=False, source="event_store")
    led.record_outcome("debt_A", "c1", recovered_paise=49900)
    led.record_decision("debt_B", "c2", Decision(act=False), diagnosis="x")
    replay = led.replay_debt("debt_A")
    assert [e["type"] for e in replay] == ["decision", "state_recheck", "outcome"]


def test_consent_events_are_immutable_records(tmp_path):
    """DPDP s.6(10) puts the burden of proof on us; a mutable boolean cannot discharge it."""
    led = AuditLedger(tmp_path / "audit.jsonl", policy_version="v1")
    led.record_consent("cust_1", event="objection_raised", basis="s.7(a) withdrawn",
                       propagated_to=["whatsapp", "sms"])
    body = led.read()[0]["body"]
    assert body["event"] == "objection_raised"
    assert body["propagated_to"] == ["whatsapp", "sms"]


# ---------------------------------------------------------------------------------------
# 1.5 webhook ingest
# ---------------------------------------------------------------------------------------

RAW = (b'{"entity":"event","event":"payment.failed","payload":{"payment":{"entity":'
       b'{"id":"pay_X1","status":"failed"}}}}')


def sign(body: bytes, secret: str = "s3cret") -> str:
    import hashlib as _h
    import hmac as _hm
    return _hm.new(secret.encode(), body, _h.sha256).hexdigest()


def test_reserialised_body_fails_verification():
    """Parsing and re-serialising changes the bytes. This is why the handler verifies before
    json.loads, and it is demonstrated rather than quoted."""
    import json as _json
    reserialised = _json.dumps(_json.loads(RAW)).encode()
    assert RAW != reserialised
    assert not verify_signature(reserialised, sign(RAW), "s3cret")


def test_dedup_survives_a_process_restart(tmp_path):
    """The spike used an in-memory set, which dies with the process. Razorpay retries for 24h."""
    db = tmp_path / "events.db"
    ingest = WebhookIngest("s3cret", EventStore(db))
    assert ingest.handle(RAW, sign(RAW), "evt_1")[1] == "queued"

    reopened = WebhookIngest("s3cret", EventStore(db))  # simulate a restart
    assert reopened.handle(RAW, sign(RAW), "evt_1")[1] == "duplicate ignored"


def test_captured_beats_failed_regardless_of_arrival_order(tmp_path):
    """The resurrection problem. Razorpay says the sequence is not fixed, so last-write-wins
    would un-pay a customer whenever the pair arrives out of order."""
    store = EventStore(tmp_path / "events.db")
    store.observe_payment("pay_1", "captured")
    store.observe_payment("pay_1", "failed")      # arrives late, must NOT downgrade
    assert store.payment_status("pay_1") == "captured"
    assert store.is_settled("pay_1")

    store.observe_payment("pay_2", "failed")
    store.observe_payment("pay_2", "captured")    # the documented order
    assert store.payment_status("pay_2") == "captured"


def test_rejections_are_durably_logged_with_sender(tmp_path):
    """A wrong secret produced 18 rejected deliveries that were invisible in gate 0.4."""
    store = EventStore(tmp_path / "events.db")
    ingest = WebhookIngest("s3cret", store)
    assert ingest.handle(RAW, "deadbeef", "evt_9", user_agent="Razorpay-Webhook/v1")[0] == 400
    assert store.rejection_count(sender_contains="Razorpay") == 1


def test_empty_secret_refuses_to_start(tmp_path):
    """Serving with an empty secret rejects everything while looking healthy."""
    with pytest.raises(ValueError):
        WebhookIngest("", EventStore(tmp_path / "events.db"))


def test_unsigned_caller_cannot_poison_dedup(tmp_path):
    """Dedup happens after verification, so a guessed event id cannot suppress a real one."""
    store = EventStore(tmp_path / "events.db")
    ingest = WebhookIngest("s3cret", store)
    ingest.handle(b'{"event":"x"}', "bad-signature", "evt_real")
    assert ingest.handle(RAW, sign(RAW), "evt_real")[1] == "queued"
