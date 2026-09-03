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

from recovery.engine.machine import RecoveryEngine                     # noqa: E402
from recovery.engine.policy import Policy, retry_schedule              # noqa: E402
from recovery.evaluation.baselines import INCUMBENT_RETRY_DAYS         # noqa: E402
from recovery.ledger.audit import AuditLedger                          # noqa: E402
from recovery.llm.composer import DEFAULT_MODEL, compose               # noqa: E402
from recovery.llm.copy_gate import Facts, check                        # noqa: E402
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
    kv("incumbent (Razorpay)", f"days {', '.join(map(str, INCUMBENT_RETRY_DAYS))}  then halt"
                               f"   ({len(INCUMBENT_RETRY_DAYS)} attempts)")
    kv("this engine", f"days {', '.join(map(str, sched))}   ({len(sched)} attempts)")
    say()
    say("Two differences, and only one of them is the interesting one.")
    say()
    say(f"The engine takes {len(sched)} attempts to the incumbent's {len(INCUMBENT_RETRY_DAYS)}. "
        f"That is a real advantage and it is")
    say("stated rather than buried: it is NOT the same budget. Retries are free in the frozen")
    say("cost model, so the extra attempts cost nothing and the comparison is not net of them.")
    say()
    say("The one that carries the argument is coverage. Four attempts inside four days can")
    say("only ever catch a payday that has just happened, on a cycle roughly thirty days")
    say("long. Spreading attempts across the declared horizon is what reaches the rest.")

    # -- 4 ------------------------------------------------------------------------------
    scene(4, "Guardrails, then the decision")
    say("Guardrails run FIRST, and the hard stops among them are absolute: already paid,")
    say("opted out, disputed, bereavement or hardship. Then soft stops, then contact rules,")
    say("and only then does cost get a say. That order is load-bearing - until 3 Sept a cost")
    say("rule could return ahead of the guardrails and log a statutory stop as a cost one.")
    say()

    # ONE walk over the horizon. This scene used to call plan_day for day 0 and then the walk
    # called it again for the same day, so both wrote to the ledger and scene 10 replayed the
    # day-0 pair twice - an append-only trail showing one decision twice, in the very scene
    # that exists to demonstrate replayability.
    contact = None
    contact_day = now
    shown_detail = False
    for offset in range(policy.retry_horizon_days + 1):
        day = now + dt.timedelta(days=offset)
        for d in engine.plan_day(debt, customer, day):
            if not d.act:
                continue
            if not shown_detail:        # the first decision, in full
                kv("day", str(offset))
                kv("act", str(d.act))
                kv("channel", d.channel.value if d.channel else "-")
                kv("stop_reason", d.stop_reason.value if d.stop_reason else "none")
                kv("expected value", f"Rs {d.expected_value_paise / 100:,.2f}")
                kv("rules fired", ", ".join(d.rules_fired) or "-")
                kv("rules passed",
                   ", ".join(d.rules_passed[:6]) + ("..." if len(d.rules_passed) > 6 else ""))
                say()
                say("Both lists go to the ledger. A system that records only what it did, and")
                say("not what it declined to do and why, cannot evidence its stopping rules.")
                say()
                say("Day 0 is a silent retry. Walking the horizon until the ladder escalates:")
                say()
                shown_detail = True
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
    say("The ladder escalates by channel and never loops back. It is shown here only as far")
    say("as its first contact, because the reply in scene 8 hard-stops this debt.")

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
    elif msg.source == "llm":
        say()
        say("The model's wording passed on this run. That is the common case, which is")
        say("exactly why the next scene probes the gate rather than trusting this one.")
    elif use_llm:
        # compose() swallows model and network errors and returns a template, so the --live
        # FLAG staying true says nothing about whether a model was reached. Branching on the
        # flag here used to print "the model's wording passed" on runs where no model ran -
        # including the documented state of this machine, where the model is not pulled.
        say()
        say("--live was requested but no model output was used: Ollama answered the port and")
        say("then the call did not succeed, so the template was composed instead. Nothing")
        say("here was written by a model, and the gate had nothing of its to judge.")
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
        # The gate's verdict travels WITH the action, exactly as engine_arm.py does it. The
        # first version of this call passed neither `copy_gate_rejected` nor `llm_output`, so
        # under --live scene 6 could announce the gate firing while the record scene 10
        # replays showed nothing of it - the blindness audit.py names in its own comment: a
        # gate nobody can see firing is indistinguishable from one that never fires.
        rejected = None
        if msg.gate_rejected_llm:
            rejected = {"verdict": msg.llm_gate.verdict.value,
                        "categories": msg.llm_gate.categories,
                        "reasons": msg.llm_gate.reasons}
        ledger.record_action(
            Action(debt_id=debt.debt_id, customer_ref=customer.ref, channel=channel,
                   at=contact_day, cost_paise=policy.cost_paise[channel],
                   policy_version=policy.version, rendered_text=msg.text,
                   template_ref=msg.template_ref, rules_fired=contact.rules_fired,
                   rules_passed=contact.rules_passed),
            copy_gate_rejected=rejected, llm_output=msg.llm_output)
        # And the engine must learn the send happened, or its own caps do not know about it.
        # Without this the contact counters stay empty and the same rung would fire again the
        # next day, straight through max_whatsapp_per_24h and escalation_wait_days.
        engine.apply_outcome(debt, contact, contact_day, False)

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
    say("outrank the model, because those three carry legal weight. Where a model IS used, it")
    say("is trusted only to spot a date phrase; the DATE ITSELF is resolved by a pure")
    say("function, never by the model, so it cannot invent one.")
    if not use_llm:
        say()
        say("No model ran here, so `date phrase` shows the reply unchanged rather than an")
        say("extracted span - the keyword fallback does not extract, it defers. Run with")
        say("--live to see the model do the extraction the sentence above describes.")

    # -- 9 ------------------------------------------------------------------------------
    scene(9, "After an opt-out, and after payment")
    # Land on a day the schedule would otherwise act on - day 3 is neither a retry day nor
    # past the escalation wait, so the engine had nothing planned and the opt-out had nothing
    # to stop. A stopping rule is only demonstrated where an action was actually due.
    later = now + dt.timedelta(days=max(d for d in retry_schedule(policy) if d <= 8))
    after_optout = engine.plan_day(debt, customer, later)
    kv("day", str((later - now).days) + "  (a scheduled retry day)")
    kv("decisions now", str(len(after_optout)))
    for d in after_optout:
        kv("act", f"{d.act}   stop_reason={d.stop_reason.value if d.stop_reason else 'none'}")
    say()
    say("Now the other direction, on THIS debt - the one that has actually been through the")
    say("loop. Razorpay's webhooks are at-least-once and unordered, so payment.captured can")
    say("arrive AFTER payment.failed for the same transaction. That is the resurrection case,")
    say("and it is why the engine re-reads payment state immediately before every action.")
    say()
    say("A late payment.captured lands for debt_demo_01, which has already failed, been")
    say("retried twice and been contacted once:")
    paid.add(debt.debt_id)

    # Deliberately re-planned on a customer with real history rather than a freshly minted
    # debt that was born settled. An earlier version demonstrated this on a brand-new debt
    # with no retries and no contacts, which shows a flag being read and nothing about
    # at-least-once or out-of-order delivery. There was no "after" in it.
    resurrected = Customer(ref=customer.ref, has_whatsapp=True, has_sms=True)
    for d in engine.plan_day(debt, resurrected, later + dt.timedelta(days=1)):
        kv("act", f"{d.act}   stop_reason={d.stop_reason.value if d.stop_reason else 'none'}")
        kv("rules fired", ", ".join(d.rules_fired) or "-")
    say()
    say("Without that re-check we would dun someone who has already paid. Note this outranks")
    say("even the opt-out above: `payment_received` is checked first because a settled debt")
    say("needs no further decision at all.")

    # -- 10 -----------------------------------------------------------------------------
    scene(10, "The audit trail")
    entries = ledger.read()
    intact, broken_at = ledger.verify_chain()
    kv("records written", str(len(entries)))
    kv("hash chain intact", str(intact) + ("" if intact else f" (broken at {broken_at})"))
    say()
    from collections import Counter
    kinds = Counter(e["type"] for e in entries)
    kv("record types", ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())))
    say()
    say("Each in order, each stamped with the policy version that governed it. The types are")
    say("counted from this run rather than listed from memory - an earlier version of this")
    say("line advertised `outcome` records, of which the run writes none. Replaying one debt:")
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
