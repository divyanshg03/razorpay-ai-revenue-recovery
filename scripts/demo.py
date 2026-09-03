"""A narrated end-to-end run of the recovery loop, on one screen.

    python scripts/demo.py              # deterministic; templates, no model needed
    python scripts/demo.py --live       # ask the local model for real wording
    python scripts/demo.py --pause      # wait for Enter between scenes, for recording

## What this is for

The track is judged on a *working demo*, and until now the repo could only show a batch
result and a test suite. Both are evidence; neither is watchable. This walks the actual loop
a single failed collection travels, printing what each component decided and why.

**Every component here is the real one.** The diagnosis, guardrails, state machine, composer,
copy gate, reply parser and ledger are imported from `src/recovery/` exactly as the batch
imports them. Nothing is re-implemented for the demo, because a demo that re-implements the
system is a demo of the demo. If a scene below prints something, the shipped code printed it.

## Two honesty rules this script holds itself to

1. **The copy gate is shown rejecting real text, not a staged failure.** Scene 6 checks
   whatever the model actually produced. Scene 7 then probes the gate with deliberately
   non-compliant candidates - clearly labelled as probes - because a gate that never fires on
   camera is an assertion, and the model usually behaves. The probes are hand-written; the
   gate's verdict on them is not.
2. **Without `--live` this uses deterministic templates and says so.** The batch measurement
   works the same way, for the reason stated in `engine_arm.py`: the simulator has no notion
   of wording, so the model cannot affect the number. Running the demo offline is therefore
   honest rather than degraded, and it means a judge with no Ollama can still reproduce it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import socket
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from recovery.diagnosis.taxonomy import diagnose                       # noqa: E402
from recovery.engine.machine import RecoveryEngine                     # noqa: E402
from recovery.engine.policy import Policy, retry_schedule              # noqa: E402
from recovery.ledger.audit import AuditLedger                          # noqa: E402
from recovery.llm.composer import DEFAULT_MODEL, compose               # noqa: E402
from recovery.llm.copy_gate import Facts, Verdict, check               # noqa: E402
from recovery.llm.parser import parse_reply                            # noqa: E402
from recovery.models import (IST, Action, Channel, Customer, Debt,     # noqa: E402
                             MandateType, PaymentFailure)

W = 92
PAUSE = False


def scene(n: int, title: str) -> None:
    if PAUSE:
        try:
            input("\n    [Enter]")
        except (EOFError, KeyboardInterrupt):
            pass
    print(f"\n{'=' * W}\n  {n}. {title.upper()}\n{'=' * W}")


def say(text: str = "", indent: int = 2) -> None:
    print(" " * indent + text if text else "")


def kv(key: str, value: str, indent: int = 4) -> None:
    print(f"{' ' * indent}{key:<26} {value}")


def ollama_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------------------


def main() -> int:
    global PAUSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help=f"compose and parse with the local model ({DEFAULT_MODEL})")
    ap.add_argument("--pause", action="store_true", help="wait for Enter between scenes")
    args = ap.parse_args()
    PAUSE = args.pause

    use_llm = args.live
    if use_llm and not ollama_up():
        say("Ollama is not reachable on 127.0.0.1:11434 - falling back to templates.")
        say("This is reported rather than hidden; the run below is still real.")
        use_llm = False

    policy = Policy()
    ledger_path = pathlib.Path(tempfile.mkdtemp()) / "demo-ledger.jsonl"
    ledger = AuditLedger(ledger_path, policy.version, fresh=True)
    # A virtual clock, as the batch uses. Without it every record in a sub-second run lands
    # on the same timestamp, and "replayable in order" is unreadable on screen.
    _tick = {"n": 0}

    def _clock():
        _tick["n"] += 1
        return dt.datetime(2026, 9, 3, 10, 0, tzinfo=IST) + dt.timedelta(seconds=_tick["n"])

    ledger.clock = _clock

    # The engine asks this before every single action. Here nothing has been paid; scene 9
    # flips it to show the re-check doing its job.
    paid: set[str] = set()
    engine = RecoveryEngine(policy, ledger, is_settled=lambda d: d.debt_id in paid)

    now = dt.datetime(2026, 9, 3, 10, 0, tzinfo=IST)
    customer = Customer(ref="cust_demo_01", has_whatsapp=True, has_sms=True)
    debt = Debt(
        debt_id="debt_demo_01",
        customer_ref=customer.ref,
        amount_paise=249_900,
        mandate_type=MandateType.UPI_AUTOPAY,
        failed_at=now,
        failure=PaymentFailure(
            error_code="BAD_REQUEST_ERROR",
            error_reason="insufficient_funds",
            error_source="bank",
            error_step="payment_authorization",
            error_description="Payment failed"),
    )

    print(f"\n{'=' * W}")
    print("  AI REVENUE RECOVERY - one failed collection, end to end")
    print(f"  policy {policy.version}   wording: "
          f"{'local model ' + DEFAULT_MODEL if use_llm else 'deterministic templates'}")
    print(f"{'=' * W}")

    # -- 1 ------------------------------------------------------------------------------
    scene(1, "A recurring collection fails")
    say("Razorpay hands back five error fields. This is all we get.")
    say()
    for k, v in vars(debt.failure).items():
        kv(k, str(v))
    kv("amount", f"Rs {debt.amount_paise / 100:,.2f}")
    kv("mandate", debt.mandate_type.value)

    # -- 2 ------------------------------------------------------------------------------
    scene(2, "Diagnosis - and why error_code alone is useless")
    diag = engine.diagnosis(debt)
    say("error_code has three values in total, and BAD_REQUEST_ERROR carries most")
    say("customer-side declines. Classifying on it alone tells you almost nothing.")
    say()
    kv("error_code alone", f"{debt.failure.error_code}  <- not actionable")
    kv("composite key", f"({debt.failure.error_code}, {debt.failure.error_reason})")
    say()
    kv("Razorpay bucket", diag.bucket.value)
    kv("actionability", diag.actionability.value)
    kv("retryable at all?", str(diag.retryable))
    kv("worth contacting?", str(diag.contactable))
    say()
    say("That second line is the one Razorpay's own buckets do not answer, and it is what")
    say("decides whether a retry could ever work or whether we must reach the customer.")

    # -- 3 ------------------------------------------------------------------------------
    scene(3, "The retry schedule - coverage, not insistence")
    sched = retry_schedule(policy)
    kv("incumbent (Razorpay)", "days 0, 1, 2, 3  then halt")
    kv("this engine", f"days {', '.join(map(str, sched))}")
    say()
    say("Same order of magnitude of attempts. The difference is that four attempts inside")
    say("four days can only catch a payday that has just happened, on a cycle roughly")
    say("thirty days long. Spreading the same budget across the declared horizon is where")
    say("the recovery comes from.")

    # -- 4 ------------------------------------------------------------------------------
    scene(4, "Guardrails, then the decision")
    decisions = engine.plan_day(debt, customer, now)
    say("Hard stops are checked first and are absolute: already paid, opted out, disputed,")
    say("bereavement or hardship. Then soft stops, then the contact-only rules.")
    say()
    for d in decisions:
        kv("act", str(d.act))
        kv("channel", d.channel.value if d.channel else "-")
        kv("stop_reason", d.stop_reason.value if d.stop_reason else "none")
        kv("expected value", f"Rs {d.expected_value_paise / 100:,.2f}")
        kv("rules fired", ", ".join(d.rules_fired) or "-")
        kv("rules passed", ", ".join(d.rules_passed[:6]) + ("..." if len(d.rules_passed) > 6 else ""))
        say()
    say("Both lists are written to the ledger. A system that records only what it did, and")
    say("not what it declined to do and why, cannot evidence its own stopping rules.")

    # Day 0 is a silent retry, so walk the horizon until the ladder escalates to a contact.
    # Retries are reported as failed, which is what advances the state - nothing here is
    # fast-forwarded past the engine.
    say()
    say("Day 0 is a silent retry. Walking the horizon until the ladder escalates:")
    say()
    contact = None
    contact_day = now
    for offset in range(policy.retry_horizon_days + 1):
        day = now + dt.timedelta(days=offset)
        for d in engine.plan_day(debt, customer, day):
            if not d.act:
                continue
            say(f"day {offset:<3} {d.channel.value:<18} "
                f"EV Rs {d.expected_value_paise / 100:,.2f}", indent=4)
            if d.channel is Channel.RETRY:
                engine.apply_outcome(debt, d, day, False)   # the retry did not land
            else:
                contact, contact_day = d, day
                break
        if contact:
            break
    say()
    say("Channels escalate and never loop back; the tone does not escalate with them.")

    # -- 5 ------------------------------------------------------------------------------
    scene(5, "Composing the message")
    facts = Facts(amount_paise=debt.outstanding_paise,
                  link="https://rzp.io/rzp/demo01", merchant="")
    channel = contact.channel if contact else Channel.SMS_SERVICE
    msg = compose(facts, diag.actionability, channel, use_llm=use_llm)
    kv("channel", channel.value)
    kv("source", msg.source)
    kv("model", msg.model or "-")
    if msg.llm_seconds:
        kv("model latency", f"{msg.llm_seconds:.1f}s")
    say()
    say("Message actually sent:")
    say(f'"{msg.text}"', indent=6)

    # -- 6 ------------------------------------------------------------------------------
    scene(6, "The copy gate, on this run's real output")
    kv("verdict on sent text", msg.gate.verdict.value)
    if msg.gate_rejected_llm:
        say()
        say("The model's own words were REJECTED on this run. What it wrote:")
        say(f'"{msg.llm_output}"', indent=6)
        kv("categories", str(msg.llm_gate.categories))
        kv("reasons", "; ".join(msg.llm_gate.reasons))
        say()
        say("The template above was sent instead. This is the gate doing its job live.")
    elif use_llm:
        say()
        say("The model's wording passed on this run. That is the common case, which is")
        say("exactly why the next scene probes the gate rather than trusting this one.")
    else:
        say()
        say("Templates were used, so there was nothing for the gate to reject here.")
        say("The next scene probes it directly.")

    # -- 7 ------------------------------------------------------------------------------
    scene(7, "The copy gate, probed deliberately")
    say("The candidates below are HAND-WRITTEN to be non-compliant. The verdicts are not.")
    say("Each is passed to the same check() the composer uses.")
    say()
    # Each probe declares the rule it is meant to trip, and the run checks that it did.
    # The first version of this scene reported five clean REJECTIONS, one of which was
    # actually rejected for "payment link missing" - the shaming rule was never exercised
    # and the screen implied it had been. Every probe now carries the link, so the only
    # thing left to reject is the thing being demonstrated.
    L = facts.link
    amt = f"Rs {facts.amount_paise / 100:,.2f}"
    probes = [
        ("discount_or_offer",
         f"Your {amt} payment did not go through. Pay now and get 20% off. {L}"),
        ("false_urgency",
         f"URGENT: your {amt} payment failed. Act now, within 2 hours. {L}"),
        ("scarcity",
         f"Your {amt} payment failed. Only a few limited slots remain today. {L}"),
        ("threat_or_shaming",
         f"Your {amt} payment failed. This will hurt your credit score and we may take "
         f"legal action. {L}"),
        ("fabricated_amount",
         f"Your payment of Rs 9,999 failed on 12/05/2024. {L}"),
    ]
    for expected, text in probes:
        r = check(text, facts)
        got = sorted(r.categories)
        verdict = r.verdict.value.upper()
        mark = "" if expected in got else "   <- MISMATCH, gate rule may have drifted"
        say(f"{verdict:<9}  {expected:<19} caught: {', '.join(got) or '-'}{mark}", indent=4)
    say()
    say("The right-hand column is the gate's own categorisation, not the probe's label. If")
    say("they ever disagree the line says so, because a probe that passes for the wrong")
    say("reason demonstrates nothing.")
    say()
    say("Under TRAI's mixed-content rule, wording like this converts a service message into")
    say("a promotional one and inherits consent, DND and time-band obligations. Fabricated")
    say("urgency and repeated nudging are two named patterns in the CCPA Dark Patterns")
    say("Guidelines 2023. The gate is a compliance control, not a tone preference.")

    # -- 8 ------------------------------------------------------------------------------
    scene(8, "The customer replies")
    if contact:
        ledger.record_action(Action(
            debt_id=debt.debt_id, customer_ref=customer.ref, channel=channel, at=contact_day,
            cost_paise=policy.cost_paise[channel], policy_version=policy.version,
            rendered_text=msg.text, template_ref=msg.template_ref,
            rules_fired=contact.rules_fired, rules_passed=contact.rules_passed))

    for reply_text in ("I get paid on the 5th, will pay then",
                       "stop messaging me"):
        parsed = parse_reply(reply_text, today=now.date(), use_llm=use_llm)
        say(f'reply: "{reply_text}"')
        kv("intent", parsed.intent.value)
        kv("date phrase", parsed.date_phrase or "-")
        kv("resolved date", str(parsed.promised_date) if parsed.promised_date else "-")
        kv("decided by", parsed.source)
        engine.record_reply(debt, customer, parsed, now, reply_text)
        say()
    say("Note `decided by`. Opt-out, dispute and hardship are decided by code overrides that")
    say("outrank the model, because those three carry legal weight. The model is trusted to")
    say("spot a date phrase; the DATE ITSELF is resolved by a pure function, never by the")
    say("model, so it cannot invent one.")

    # -- 9 ------------------------------------------------------------------------------
    scene(9, "After an opt-out, and after payment")
    later = now + dt.timedelta(days=3)
    after_optout = engine.plan_day(debt, customer, later)
    kv("decisions now", str(len(after_optout)))
    for d in after_optout:
        kv("act", f"{d.act}   stop_reason={d.stop_reason.value if d.stop_reason else 'none'}")
    say()
    say("Now the other direction. Razorpay's webhooks are at-least-once and unordered, so")
    say("payment.captured can arrive AFTER payment.failed for the same transaction. The")
    say("engine re-checks payment state immediately before every action:")
    paid.add(debt.debt_id)
    fresh_debt = Debt(debt_id="debt_demo_02", customer_ref="cust_demo_02",
                      amount_paise=99_900, mandate_type=MandateType.EMANDATE,
                      failed_at=now, failure=debt.failure)
    paid.add(fresh_debt.debt_id)
    for d in engine.plan_day(fresh_debt, Customer(ref="cust_demo_02"), later):
        kv("act", f"{d.act}   stop_reason={d.stop_reason.value if d.stop_reason else 'none'}")
    say()
    say("Without that re-check we would dun people who have already paid.")

    # -- 10 -----------------------------------------------------------------------------
    scene(10, "The audit trail")
    entries = ledger.read()
    intact, broken_at = ledger.verify_chain()
    kv("records written", str(len(entries)))
    kv("hash chain intact", str(intact) + ("" if intact else f" (broken at {broken_at})"))
    say()
    say("Every decision, action, re-check, inbound reply and outcome, in order, each stamped")
    say("with the policy version that governed it. Replaying one debt:")
    say()
    for e in ledger.replay_debt(debt.debt_id):
        body = e["body"]
        detail = (body.get("stop_reason") or body.get("channel")
                  or body.get("intent") or body.get("note") or "")
        if e["type"] == "state_recheck":
            detail = f"already_paid={body.get('already_paid')}  via {body.get('source')}"
        say(f"{e['timestamp_ist'][11:19]}  {e['type']:<14} {detail}", indent=4)
    say()
    say(f"ledger: {ledger_path}")

    # -----------------------------------------------------------------------------------
    print(f"\n{'=' * W}")
    say("That is the loop. The measured result across 5,000 simulated customers, against a")
    say("randomised holdout and net of contact cost, is in results/metrics.json - and every")
    say("figure in the README is generated from it. The cohort is simulated because")
    say("Subscriptions is gated on this test account; see docs/phase-0-findings.md.")
    print(f"{'=' * W}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
