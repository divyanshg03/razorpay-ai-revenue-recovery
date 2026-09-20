"""A narrated end-to-end run of the recovery loop, on one screen.

    python scripts/demo.py              # deterministic; templates, no model needed
    python scripts/demo.py --live       # ask the local model for real wording
    python scripts/demo.py --pause      # wait for Enter between scenes, for recording
    python scripts/demo.py --ask        # hand the keyboard over: type a reply, watch it decide
    python scripts/demo.py --no-color   # plain text (also the default when piped)

## What this is for

The track is judged on a *working demo*, and until now the repo could only show a batch
result and a test suite. Both are evidence; neither is watchable. This walks the actual loop
a single failed collection travels, printing what each component decided and why.

**Every component here is the real one.** The webhook ingest, diagnosis, guardrails, state
machine, composer, copy gate, reply parser and ledger are imported from `src/recovery/`
exactly as the batch imports them. Nothing is re-implemented for the demo, because a demo that
re-implements the system is a demo of the demo. If a scene below prints something, the shipped
code printed it.

## Three honesty rules this script holds itself to

1. **Scene 1 is a real Razorpay event**, captured from their servers on 2 Sept 2026 through a
   zrok tunnel and stored in `results/phase0/`. Everything after it is simulated, and the
   screen says so rather than letting the first scene lend credibility to the rest.
2. **The copy gate is shown rejecting real text, not a staged failure.** Scene 7 checks
   whatever the model actually produced. Scene 8 then probes the gate with deliberately
   non-compliant candidates - clearly labelled as probes - because a gate that never fires on
   camera is an assertion, and the model usually behaves. The probes are hand-written; the
   gate's verdict on them is not.
3. **Without `--live` this uses deterministic templates and says so.** The batch measurement
   works the same way, for the reason stated in `engine_arm.py`: the simulator has no notion
   of wording, so the model cannot affect the number. Running the demo offline is therefore
   honest rather than degraded, and it means a judge with no Ollama can still reproduce it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import pathlib
import socket
import sys
import tempfile
import textwrap

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from recovery.diagnosis.taxonomy import diagnose                     # noqa: E402
from recovery.engine.machine import RecoveryEngine                   # noqa: E402
from recovery.engine.policy import Policy, retry_schedule            # noqa: E402
from recovery.evaluation.baselines import INCUMBENT_RETRY_DAYS       # noqa: E402
from recovery.ingest.webhook import EventStore, WebhookIngest        # noqa: E402
from recovery.ledger.audit import AuditLedger                        # noqa: E402
from recovery.llm.composer import DEFAULT_MODEL, compose, warm       # noqa: E402
from recovery.llm.copy_gate import Facts, check                      # noqa: E402
from recovery.llm.parser import parse_reply                          # noqa: E402
from recovery.models import (IST, Action, Channel, Customer, Debt,   # noqa: E402
                             MandateType, PaymentFailure)

W = 92
PAUSE = False
COLOR = False

PHASE0 = REPO / "results" / "phase0"
METRICS = REPO / "results" / "metrics.json"
NPCI = REPO / "results" / "npci-cap-rerun.json"

#: The demo debt's payment id. In production this is where debt-level reconciliation lives:
#: one debt collects many payment ids (each retry, each link). Here it is one of each.
DEMO_PAYMENT_ID = "pay_demo01"

#: NOT the Dashboard secret, which is in `.env` and is not in this repository. Razorpay's own
#: raw bytes were not retained in phase 0 - only the parsed event - so scene 1 re-signs the
#: stored payload with this local secret and verifies THAT. The verification code is the
#: shipped one; the signature is not Razorpay's, and the screen says so.
LOCAL_SECRET = "demo-only-secret-not-the-dashboard-one"

BOLD, DIM, RED, GREEN, YELLOW, CYAN = "1", "2", "31", "32", "33", "36"


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def enable_ansi() -> bool:
    """Turn on VT processing on Windows consoles that still need asking."""
    if not sys.stdout.isatty():
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def scene(n: int, title: str) -> None:
    if PAUSE:
        try:
            input(paint("\n    [Enter]", DIM))
        except (EOFError, KeyboardInterrupt):
            pass
    print(paint(f"\n{'=' * W}", CYAN))
    print(paint(f"  {n}. {title.upper()}", BOLD))
    print(paint(f"{'=' * W}", CYAN))


def say(text: str = "", indent: int = 2, code: str | None = None) -> None:
    line = " " * indent + text if text else ""
    print(paint(line, code) if code and line else line)


def kv(key: str, value: str, indent: int = 4, code: str | None = None) -> None:
    print(f"{' ' * indent}{paint(f'{key:<26}', DIM)} {paint(value, code) if code else value}")


def verdict_colour(text: str) -> str:
    low = text.lower()
    if any(w in low for w in ("reject", "400", "invalid", "stop", "refus", "false")):
        return RED
    if any(w in low for w in ("pass", "200", "ok", "queued", "true", "clean")):
        return GREEN
    return YELLOW


def ollama_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1):
            return True
    except OSError:
        return False


def sign(body: bytes) -> str:
    """Stand in for Razorpay's signing side. Verification is the shipped code's job."""
    return hmac.new(LOCAL_SECRET.encode(), body, hashlib.sha256).hexdigest()


def razorpay_originated_event() -> tuple[dict, dict] | tuple[None, None]:
    """The one event in the phase-0 log that Razorpay itself sent.

    Distinguished exactly as `0.4d` documents it: our own self-tests carry `evt_*` ids, and
    Razorpay's are bare tokens. The record must also carry a payment entity, because that is
    what makes it a collection failure rather than a liveness ping.
    """
    log = PHASE0 / "0.4c-received-events.jsonl"
    if not log.exists():
        return None, None
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        entity = record["payload"].get("payload", {}).get("payment", {}).get("entity")
        if entity and not record["event_id"].startswith("evt_"):
            return record, entity
    return None, None


# ---------------------------------------------------------------------------------------


def main() -> int:
    global PAUSE, COLOR
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help=f"compose and parse with the local model ({DEFAULT_MODEL})")
    ap.add_argument("--pause", action="store_true", help="wait for Enter between scenes")
    ap.add_argument("--ask", action="store_true",
                    help="after the walk, type customer replies and watch the engine decide")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    args = ap.parse_args()
    PAUSE = args.pause
    COLOR = enable_ansi() and not args.no_color

    use_llm = args.live
    if use_llm and not ollama_up():
        say("Ollama is not reachable on 127.0.0.1:11434 - falling back to templates.")
        say("This is reported rather than hidden; the run below is still real.")
        use_llm = False
    if use_llm:
        # A cold model was measured at 36s to first token. Paying that here, with a line on
        # screen saying what is happening, beats paying it silently in the middle of scene 6.
        print(paint(f"  loading {DEFAULT_MODEL} into memory...", DIM), end="", flush=True)
        print(paint(f" {warm():.1f}s", DIM))

    policy = Policy()
    work = pathlib.Path(tempfile.mkdtemp())
    ledger_path = work / "demo-ledger.jsonl"
    # The model that wrote the copy is part of the record: a trail that cannot say which
    # model produced a message cannot answer "why did it say that" a year later.
    ledger = AuditLedger(ledger_path, policy.version, fresh=True,
                         model_version=DEFAULT_MODEL if use_llm else "none")

    # The ledger runs on SIMULATED time: each record is stamped with the day the decision
    # belongs to, not the wall clock of a run that lasts 0.15s. An earlier version ticked one
    # second per record from day 0, so scene 12 replayed a fortnight of decisions all dated
    # 3 September, and the customer's reply appeared before the message that drew it.
    sim = {"at": dt.datetime(2026, 9, 3, 10, 0, tzinfo=IST), "n": 0}

    def _clock():
        sim["n"] += 1
        return sim["at"] + dt.timedelta(seconds=sim["n"] % 50)

    def at_day(when: dt.datetime) -> None:
        sim["at"] = when.replace(hour=10, minute=0, second=0, microsecond=0)

    ledger.clock = _clock

    # Payment state comes from the webhook event store - the same SQLite the receiver in
    # `ingest/webhook.py` writes to - not from a set in this script. So the re-check the
    # engine performs before every action is reading the real thing.
    store = EventStore(work / "events.sqlite3")
    ingest = WebhookIngest(LOCAL_SECRET, store)
    engine = RecoveryEngine(policy, ledger,
                            is_settled=lambda d: store.is_settled(DEMO_PAYMENT_ID))

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

    print(paint(f"\n{'=' * W}", CYAN))
    print(paint("  AI REVENUE RECOVERY - one failed collection, end to end", BOLD))
    print(f"  policy {policy.version}   wording: "
          f"{'local model ' + DEFAULT_MODEL if use_llm else 'deterministic templates'}")
    print(paint(f"{'=' * W}", CYAN))

    # -- 1 ------------------------------------------------------------------------------
    scene(1, "A real Razorpay webhook, through the shipped receiver")
    record, entity = razorpay_originated_event()
    proof_file = PHASE0 / "0.4d-razorpay-originated-event.json"
    if record is None:
        say("results/phase0/0.4c-received-events.jsonl is missing; skipping the real event.")
    else:
        proof = (json.loads(proof_file.read_text(encoding="utf-8"))["proof"] if
                 proof_file.exists() else {"signed_by": "see results/phase0/"})
        say("Not simulated, and not replayed from a fixture written by hand. Razorpay sent")
        say("this to a laptop through a zrok tunnel, and phase 0 kept it:")
        say()
        kv("event id", record["event_id"])
        kv("event", record["event"])
        kv("received at", record["received_at"])
        kv("account", record["payload"].get("account_id", "-"))
        kv("payment", f"{entity['id']}   Rs {int(entity['amount']) / 100:,.2f}   "
                      f"{entity.get('method', '-')}")
        kv("verified at receipt", proof["signed_by"])
        say()
        say("Razorpay's raw bytes were not kept in phase 0, only the parsed event, so the")
        say("three lines below re-sign the stored payload with a local secret. The VERIFIER")
        say("is the shipped one, and it is the only thing being demonstrated here.")
        say()
        body = json.dumps(record["payload"], separators=(",", ":"), sort_keys=True).encode()
        for label, raw, sig, eid, agent in (
                ("POST /webhook", body, sign(body), record["event_id"], "Razorpay-Webhook/1.0"),
                ("same id, redelivered", body, sign(body), record["event_id"],
                 "Razorpay-Webhook/1.0"),
                ("amount edited in flight", body.replace(str(entity["amount"]).encode(),
                                                         str(int(entity["amount"]) * 3).encode(),
                                                         1), sign(body), "evt_tampered", "curl/8")):
            code, detail = ingest.handle(raw, sig, eid, user_agent=agent)
            kv(label, f"{code} {detail}", code=verdict_colour(str(code)))
        kv("rejections logged", str(store.rejection_count()))
        kv("payment state now", str(store.payment_status(entity["id"])))
        say()
        say("The same five error fields a GET on the payment would return arrive inside the")
        say("event, so the diagnosis can run without a follow-up API call. Running the real")
        say("taxonomy on Razorpay's own fields:")
        say()
        real = diagnose(PaymentFailure(
            error_code=entity.get("error_code") or "",
            error_reason=entity.get("error_reason") or "",
            error_source=entity.get("error_source") or "",
            error_step=entity.get("error_step") or "",
            error_description=entity.get("error_description") or ""))
        kv("error fields", f"{entity.get('error_code')} / {entity.get('error_reason')} / "
                           f"{entity.get('error_source')} / {entity.get('error_step')}")
        kv("bucket", real.bucket.value)
        kv("actionability", real.actionability.value, code=YELLOW)
        say()
        say("Note what Razorpay's own `error_reason` says on a real failure: `payment_failed`.")
        say("It carries no cause, so the taxonomy files it under `other` and the engine takes")
        say("the most conservative action a cause it cannot read allows. That is the problem")
        say("this project exists for, and it is visible in their production payload.")
        say()
        say("That is the only real payment in this demo. Everything below runs on a seeded")
        say("simulator, because Subscriptions is gated on this test account - the blocker is")
        say("in docs/phase-0-findings.md, with the 401s next to ten endpoints that return 200.")

    # -- 2 ------------------------------------------------------------------------------
    scene(2, "A recurring collection fails")
    say("Razorpay hands back five error fields. This is all we get.")
    say()
    for k, v in vars(debt.failure).items():
        kv(k, str(v))
    kv("amount", f"Rs {debt.amount_paise / 100:,.2f}")
    kv("mandate", debt.mandate_type.value)

    # -- 3 ------------------------------------------------------------------------------
    scene(3, "Diagnosis - and why error_code alone is useless")
    diag = engine.diagnosis(debt)
    say("error_code has three values in total, and BAD_REQUEST_ERROR carries most")
    say("customer-side declines. Classifying on it alone tells you almost nothing.")
    say()
    kv("error_code alone", f"{debt.failure.error_code}  <- not actionable")
    kv("composite key", f"({debt.failure.error_code}, {debt.failure.error_reason})")
    say()
    kv("Razorpay bucket", diag.bucket.value)
    kv("actionability", diag.actionability.value, code=YELLOW)
    kv("retryable at all?", str(diag.retryable))
    kv("worth contacting?", str(diag.contactable))
    say()
    say("That second line is the one Razorpay's own buckets do not answer, and it is what")
    say("decides whether a retry could ever work or whether we must reach the customer.")

    # -- 4 ------------------------------------------------------------------------------
    scene(4, "The retry schedule - coverage, not insistence")
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
    say("NPCI's UPI circular OC-215-A allows one attempt and three retries per mandate cycle,")
    say("so six is over the cap, and the frozen headline is measured outside the rule it would")
    say("have to ship under. Scene 14 re-runs this whole cohort inside the cap rather than")
    say("arguing about it.")
    say()
    say("The one that carries the argument is coverage. Four attempts inside four days can")
    say("only ever catch a payday that has just happened, on a cycle roughly thirty days")
    say("long. Spreading attempts across the declared horizon is what reaches the rest.")

    # -- 5 ------------------------------------------------------------------------------
    scene(5, "Guardrails, then the decision")
    say("Guardrails run FIRST. Payment, opt-out and dispute are absolute stops. Bereavement")
    say(f"and hardship are a PAUSE, not a stop: contact resumes on the date the customer names,")
    say(f"or after {policy.hardship_default_resume_days} days if they name none - scene 10 shows both. Then soft stops,")
    say("then contact rules, and only then does cost get a say. That order is load-bearing -")
    say("until 3 Sept a cost rule could return ahead of the guardrails and log a statutory")
    say("stop as a cost one.")
    say()

    # ONE walk over the horizon. This scene used to call plan_day for day 0 and then the walk
    # called it again for the same day, so both wrote to the ledger and scene 12 replayed the
    # day-0 pair twice - an append-only trail showing one decision twice, in the very scene
    # that exists to demonstrate replayability.
    contact = None
    contact_day = now
    shown_detail = False
    for offset in range(policy.retry_horizon_days + 1):
        day = now + dt.timedelta(days=offset)
        at_day(day)
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
    say("as its first contact, because the reply in scene 9 hard-stops this debt.")

    # -- 6 ------------------------------------------------------------------------------
    scene(6, "Composing the message")
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
    say(f'"{msg.text}"', indent=6, code=BOLD)

    # -- 7 ------------------------------------------------------------------------------
    scene(7, "The copy gate, on this run's real output")
    kv("verdict on sent text", msg.gate.verdict.value,
       code=verdict_colour(msg.gate.verdict.value))
    if msg.gate_rejected_llm:
        say()
        say("The model's own words were REJECTED on this run. What it wrote:")
        say(f'"{msg.llm_output}"', indent=6, code=RED)
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

    # -- 8 ------------------------------------------------------------------------------
    scene(8, "The copy gate, probed deliberately")
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
        say(paint(f"{verdict:<9}", verdict_colour(verdict)) +
            f"  {expected:<19} caught: {', '.join(got) or '-'}{mark}", indent=4)
    say()
    say("The right-hand column is the gate's own categorisation, not the probe's label. If")
    say("they ever disagree the line says so, because a probe that passes for the wrong")
    say("reason demonstrates nothing.")
    say()
    say("Under TRAI's mixed-content rule, wording like this converts a service message into")
    say("a promotional one and inherits consent, DND and time-band obligations. Fabricated")
    say("urgency and repeated nudging are two named patterns in the CCPA Dark Patterns")
    say("Guidelines 2023. The gate is a compliance control, not a tone preference.")

    # -- 9 ------------------------------------------------------------------------------
    scene(9, "The customer replies")
    if contact:
        # The gate's verdict travels WITH the action, exactly as engine_arm.py does it. The
        # first version of this call passed neither `copy_gate_rejected` nor `llm_output`, so
        # under --live scene 7 could announce the gate firing while the record scene 12
        # replays showed nothing of it - the blindness audit.py names in its own comment: a
        # gate nobody can see firing is indistinguishable from one that never fires.
        rejected = None
        if msg.gate_rejected_llm:
            rejected = {"verdict": msg.llm_gate.verdict.value,
                        "categories": msg.llm_gate.categories,
                        "reasons": msg.llm_gate.reasons}
        at_day(contact_day)
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

    # The reply lands on the day the message went out, not on day 0. Recording it at `now`
    # put the answer in the ledger BEFORE the question.
    reply_at = contact_day
    for reply_text in ("I get paid on the 5th, will pay then",
                       "stop messaging me"):
        parsed = parse_reply(reply_text, today=reply_at.date(), use_llm=use_llm)
        say(f'reply: "{reply_text}"')
        kv("intent", parsed.intent.value, code=YELLOW)
        kv("date phrase", parsed.date_phrase or "-")
        kv("resolved date", str(parsed.promised_date) if parsed.promised_date else "-")
        kv("decided by", parsed.source)
        at_day(reply_at)
        engine.record_reply(debt, customer, parsed, reply_at, reply_text)
        say()
    say("Note `decided by`. Opt-out, dispute and hardship are matched by code first, in that")
    say("order, because a statutory stop has to outrank a policy pause. The model is allowed")
    say("to STOP us and never to start us: if it reports an opt-out the keywords missed, that")
    say("closes the file (`llm_stop`), while no model output can lift a stop or supply a date.")
    say("Where a model IS used for a promise, it is trusted only to spot the date PHRASE; the")
    say("date itself is resolved by a pure function, so it cannot invent one.")
    if not use_llm:
        say()
        say("No model ran here, so `date phrase` shows the reply unchanged rather than an")
        say("extracted span - the keyword fallback does not extract, it defers. Run with")
        say("--live to see the model do the extraction the sentence above describes.")

    # -- 10 -----------------------------------------------------------------------------
    scene(10, "The replies that break naive parsers")
    say("These five are here because they are the first things a reviewer types. Each is")
    say("parsed by the shipped parser; nothing below is staged. A scratch engine takes the")
    say("consequence, so the debt above is untouched.")
    say()
    edge = [
        ("My father passed away. Stop messaging me.", "two signals: the stop must win"),
        ("my mother died 2 days ago", "a number that is not a date"),
        ("I am in hospital, please contact me after the 20th", "a callback they chose"),
        ("yeh messages band karo", "an opt-out that is not in English"),
        ("Please cease all communication with me", "an opt-out with no keyword in it"),
    ]
    for text, why in edge:
        p = parse_reply(text, today=now.date(), use_llm=use_llm)
        scratch = RecoveryEngine(policy, AuditLedger(work / "scratch.jsonl", policy.version,
                                                     fresh=True),
                                 is_settled=lambda d: False)
        c2 = Customer(ref="cust_edge", has_whatsapp=True, has_sms=True)
        d2 = Debt(debt_id="debt_edge", customer_ref=c2.ref, amount_paise=249_900,
                  mandate_type=MandateType.UPI_AUTOPAY, failed_at=now, failure=debt.failure)
        scratch.record_reply(d2, c2, p, now, text)
        nxt = "-"
        for offset in range(1, 31):
            acts = [x for x in scratch.plan_day(d2, c2, now + dt.timedelta(days=offset)) if x.act]
            if acts:
                nxt = f"day {offset}: {acts[0].channel.value}"
                break
            stopped = scratch.state(d2.debt_id).hard_stopped
            if stopped is not None:
                nxt = f"stopped: {stopped.value}"
                break
        say(f'"{text}"', indent=4, code=BOLD)
        say(f"{p.intent.value:<14} via {p.source:<10} date {str(p.promised_date or '-'):<12} "
            f"next action {nxt}", indent=8)
        say(f"({why})", indent=8, code=DIM)
    say()
    say("Four of those are decided by code with no model running at all. The order is itself")
    say("a rule - a stop that never expires outranks a pause that does - a number is not a")
    say("date, and the keyword list is bilingual because customers are: romanised Hinglish is")
    say("what people actually type, and llama3.1:8b reads some of it as `other`.")
    say()
    say("The last one carries no keyword in any language.")
    if use_llm:
        say("The model read it as an opt-out, and `llm_stop` closed the file. A model is")
        say("allowed to STOP us and never to start us: it cannot lift a stop, begin a ladder")
        say("or supply a date. That asymmetry is the whole of what is delegated.")
    else:
        say("Offline it falls to `other`, which is the honest limit of a keyword list. Run")
        say("with --live and the model's `opt_out` closes the file as `llm_stop` - a model")
        say("may stop us and never start us.")

    # -- 11 -----------------------------------------------------------------------------
    scene(11, "After an opt-out, and after payment")
    # Land on a day the schedule would otherwise act on - day 3 is neither a retry day nor
    # past the escalation wait, so the engine had nothing planned and the opt-out had nothing
    # to stop. A stopping rule is only demonstrated where an action was actually due.
    later = now + dt.timedelta(days=max(d for d in retry_schedule(policy) if d <= 8))
    at_day(later)
    after_optout = engine.plan_day(debt, customer, later)
    kv("day", str((later - now).days) + "  (a scheduled retry day)")
    kv("decisions now", str(len(after_optout)))
    for d in after_optout:
        kv("act", f"{d.act}   stop_reason={d.stop_reason.value if d.stop_reason else 'none'}",
           code=RED)
    say()
    say("Now the other direction, on THIS debt - the one that has actually been through the")
    say("loop. Razorpay's webhooks are at-least-once and unordered, so payment.captured can")
    say("arrive AFTER payment.failed for the same transaction. That is the resurrection case,")
    say("and it is why the engine re-reads payment state immediately before every action -")
    say("per action, not once a day, and every read it makes is in the ledger.")
    say()
    state = engine.state(debt.debt_id)
    plural = lambda n: "" if n == 1 else "s"      # noqa: E731 - one-line, used twice below
    say(f"A late payment.captured arrives for {debt.debt_id}. That debt has already failed,")
    say(f"been retried {state.retries_made} time{plural(state.retries_made)} and contacted "
        f"{len(state.contacts)} time{plural(len(state.contacts))} - counted from the engine's own")
    say("state, not from this narration. It arrives as a real webhook, through the receiver")
    say("from scene 1, into the same event store the engine's re-check reads:")
    say()
    captured = {"entity": "event", "event": "payment.captured",
                "payload": {"payment": {"entity": {"id": DEMO_PAYMENT_ID, "status": "captured",
                                                   "amount": debt.amount_paise}}}}
    raw = json.dumps(captured, separators=(",", ":"), sort_keys=True).encode()
    code, detail = ingest.handle(raw, sign(raw), "evt_demo_captured",
                                 user_agent="Razorpay-Webhook/1.0")
    kv("POST /webhook", f"{code} {detail}", code=verdict_colour(str(code)))
    kv("payment state", str(store.payment_status(DEMO_PAYMENT_ID)), code=GREEN)

    # Deliberately re-planned on a customer with real history rather than a freshly minted
    # debt that was born settled. An earlier version demonstrated this on a brand-new debt
    # with no retries and no contacts, which shows a flag being read and nothing about
    # at-least-once or out-of-order delivery. There was no "after" in it.
    at_day(later + dt.timedelta(days=1))
    resurrected = Customer(ref=customer.ref, has_whatsapp=True, has_sms=True)
    for d in engine.plan_day(debt, resurrected, later + dt.timedelta(days=1)):
        kv("act", f"{d.act}   stop_reason={d.stop_reason.value if d.stop_reason else 'none'}",
           code=GREEN)
        kv("rules fired", ", ".join(d.rules_fired) or "-")
    say()
    say("And the unordered half: a stale payment.failed for the same payment, delivered after")
    say("the capture, must not un-pay the customer. Precedence decides, not arrival order:")
    say()
    stale = {"entity": "event", "event": "payment.failed",
             "payload": {"payment": {"entity": {"id": DEMO_PAYMENT_ID, "status": "failed",
                                                "amount": debt.amount_paise}}}}
    raw = json.dumps(stale, separators=(",", ":"), sort_keys=True).encode()
    ingest.handle(raw, sign(raw), "evt_demo_stale_failed", user_agent="Razorpay-Webhook/1.0")
    kv("payment state", str(store.payment_status(DEMO_PAYMENT_ID)), code=GREEN)
    say()
    say("Without that re-check we would dun someone who has already paid. Note this outranks")
    say("even the opt-out above: `payment_received` is checked first because a settled debt")
    say("needs no further decision at all.")

    # -- 12 -----------------------------------------------------------------------------
    scene(12, "The audit trail")
    entries = ledger.read()
    intact, broken_at = ledger.verify_chain()
    kv("records written", str(len(entries)))
    kv("hash chain intact", str(intact) + ("" if intact else f" (broken at {broken_at})"))
    say()
    from collections import Counter
    kinds = Counter(e["type"] for e in entries)
    kv("record types", ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())))
    say()
    say("Each in order, each stamped with the policy version that governed it, each on the")
    say("day it belongs to. The types are counted from this run rather than listed from")
    say("memory - an earlier version of this line advertised `outcome` records, of which the")
    say("run writes none. Replaying one debt:")
    say()
    for e in ledger.replay_debt(debt.debt_id):
        body = e["body"]
        detail = (body.get("stop_reason") or body.get("channel")
                  or body.get("intent") or body.get("note") or "")
        if e["type"] == "state_recheck":
            detail = f"already_paid={body.get('already_paid')}  via {body.get('source')}"
        say(f"{e['timestamp_ist'][:10]} {e['timestamp_ist'][11:19]}  "
            f"{e['type']:<14} {detail}", indent=4)
    say()
    say(f"ledger: {ledger_path}")

    # -- 13 -----------------------------------------------------------------------------
    scene(13, "What it is worth, from the artifact")
    if not METRICS.exists():
        say("results/metrics.json is missing - run scripts/run_batch.py to regenerate it.")
        print(paint(f"\n{'=' * W}\n", CYAN))
        store.close()
        return 0
    m = json.loads(METRICS.read_text(encoding="utf-8"))
    p = m["primary_cohort_21d"]
    prim = p["primary"]
    ctrl = p["spread_retry_control"]["decisioning_is_worth__C_vs_D_diagnosed"]
    spacing = p["spread_retry_control"]["spacing_is_worth__D_vs_B"]
    say("Read live from results/metrics.json - the same file every figure in the README is")
    say("generated from. Nothing on this screen is typed by hand.")
    say()
    kv("cohort", f"{m['n_customers']:,} simulated customers, seed {p['seed']}, "
                 f"{p['window_days']}-day window")
    kv("assignment", "randomised, customer-level, sticky")
    say()
    def arm_name(key: str) -> str:
        return "D'" if key == "D_diagnosed" else key

    for arm, label in (("A", "do nothing"), ("B", "Razorpay's ladder"), ("C", "this engine"),
                       ("D", "retries only, blind to the cause"),
                       ("D_diagnosed", "retries only, respecting the diagnosis")):
        kv(f"arm {arm_name(arm)}", f"{p['arms'][arm]['recovery_rate']:>7.2%}   {label}")
    say()

    def money(row: dict) -> str:
        return (f"Rs {row['net_incremental_total_rupees']:,.0f}   95% CI "
                f"[{row['ci95_total_rupees'][0]:,.0f}, {row['ci95_total_rupees'][1]:,.0f}]")

    # Labels come from the artifact, not from this file. Arms D and D_diagnosed are one
    # suffix apart and answer different questions, so printing one under the other's name
    # would misstate what the calendar alone is worth - on the slide about exactly that.
    kv(f"HEADLINE {arm_name(prim['treat_arm'])} vs {arm_name(prim['control_arm'])}",
       money(prim), code=GREEN)
    kv(f"calendar {arm_name(spacing['treat_arm'])} vs {arm_name(spacing['control_arm'])}",
       money(spacing))
    kv(f"decisioning {arm_name(ctrl['treat_arm'])} vs {arm_name(ctrl['control_arm'])}",
       money(ctrl), code=GREEN if ctrl["excludes_zero"] else YELLOW)
    kv("cost per incremental Re", f"Rs {p['cost_per_incremental_rupee']}")
    kv("guardrail invariants", "all zero" if p["guardrails_all_zero"] else "VIOLATION",
       code=GREEN if p["guardrails_all_zero"] else RED)
    kv("not recovered", f"{p['failure_list']['n_not_recovered']:,} debts, "
                        f"Rs {p['failure_list']['unrecovered_rupees']:,.0f} left on the table")
    say()
    if not ctrl["excludes_zero"]:
        say("That third interval CROSSES ZERO, and the sign is read from the artifact rather")
        say("than asserted here. The honest reading: against a calendar that ignores opt-outs,")
        say("decisioning does not move recovery measurably. The spacing earns the money; the")
        say("decisioning is what makes taking it lawful - and under NPCI's four-attempt cap,")
        say("where retries run out, it is also what is left to collect with.")

    # -- 14 -----------------------------------------------------------------------------
    scene(14, "The same cohort, inside NPCI's cap")
    if not NPCI.exists():
        say("results/npci-cap-rerun.json is missing - run scripts/npci_cap_rerun.py.")
    else:
        cap = json.loads(NPCI.read_text(encoding="utf-8"))
        sched = cap["schedule"]
        say("The schedule in scene 4 takes six attempts. The rule allows four, and the honest")
        say("answer to that is a measurement rather than a paragraph: the same cohort, the")
        say("same engine, re-run inside the cap. Three retries spread across the same horizon,")
        say("no same-day re-debit, the incumbent on its documented ladder, and every debt")
        say("scored on its own 21-day window.")
        say()
        kv("rule", cap["rule"])
        kv("schedule", f"charge on day 0, retries on days "
                       f"{', '.join(map(str, sched['engine_and_controls']))}   "
                       f"incumbent {', '.join(map(str, sched['incumbent']))}")
        kv("generated at", f"{cap['head_commit']}"
                           f"{'  (dirty tree)' if cap['working_tree_dirty'] else '  (clean tree)'}")
        say()
        for key, arm in cap["arms"].items():
            kv(f"arm {key}", f"{arm['recovery_rate']:>7.2%}   n={arm['n']:,}")
        say()
        capped = cap["comparisons"]
        for key, row in capped.items():
            if key.endswith("__C_vs_D"):
                continue          # the blind control, kept in the artifact, not on screen
            kv(key.split("__")[-1].replace("_", " "),
               f"Rs {row['net_incremental_total_rupees']:,.0f}   95% CI "
               f"[{row['ci95_total_rupees'][0]:,.0f}, {row['ci95_total_rupees'][1]:,.0f}]",
               code=GREEN if row["excludes_zero"] else YELLOW)
        say()
        decisioning = capped["decisioning_is_worth__C_vs_D_diagnosed"]
        shipped = ctrl["net_incremental_total_rupees"]
        say("Read those two artifacts together, because they disagree and the disagreement is")
        say("the point. With six retries the calendar does nearly all the work and decisioning")
        say(f"measures Rs {shipped:,.0f} on an interval that crosses zero. With three, the retries run")
        say(f"out, and decisioning is worth Rs {decisioning['net_incremental_total_rupees']:,.0f} "
            f"on an interval that {'excludes' if decisioning['excludes_zero'] else 'crosses'} zero.")
        say("Scarcity of attempts is what makes deciding who to contact worth anything - which")
        say("is the regime the regulator actually puts you in.")
        say()
        say("Still not modelled, and each of these would move the number:")
        for item in cap["not_modelled"]:
            for i, line in enumerate(textwrap.wrap(item, 84)):
                say(("- " if i == 0 else "  ") + line, indent=4, code=DIM)

    # -- 15 -----------------------------------------------------------------------------
    if args.ask:
        scene(15, "Your turn")
        say("Type a customer reply. The shipped parser reads it, and a scratch engine shows")
        say("what would happen next. Blank line to finish.")
        say()
        while True:
            try:
                text = input(paint("  customer> ", CYAN)).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                break
            p2 = parse_reply(text, today=now.date(), use_llm=use_llm)
            scratch = RecoveryEngine(policy, AuditLedger(work / "ask.jsonl", policy.version,
                                                         fresh=True),
                                     is_settled=lambda d: False)
            c3 = Customer(ref="cust_ask", has_whatsapp=True, has_sms=True)
            d3 = Debt(debt_id="debt_ask", customer_ref=c3.ref, amount_paise=249_900,
                      mandate_type=MandateType.UPI_AUTOPAY, failed_at=now, failure=debt.failure)
            scratch.record_reply(d3, c3, p2, now, text)
            kv("intent", p2.intent.value, code=YELLOW)
            kv("decided by", p2.source)
            kv("resolved date", str(p2.promised_date) if p2.promised_date else "-")
            plan = []
            for offset in range(1, 31):
                day = now + dt.timedelta(days=offset)
                for x in scratch.plan_day(d3, c3, day):
                    if not x.act:
                        continue
                    # The engine has to LEARN the action happened, or its own caps never see
                    # it: without this the walk printed a WhatsApp on three consecutive days,
                    # straight through the 24-hour channel cap it is meant to demonstrate.
                    scratch.apply_outcome(d3, x, day, False)
                    plan.append(f"day {offset}: {x.channel.value}")
                if len(plan) >= 3:
                    break
            stopped = scratch.state(d3.debt_id).hard_stopped
            kv("next 30 days", "; ".join(plan) or "nothing scheduled",
               code=RED if not plan else None)
            if stopped is not None:
                kv("file", f"stopped: {stopped.value}", code=RED)
            say()

    # -----------------------------------------------------------------------------------
    print(paint(f"\n{'=' * W}", CYAN))
    say("That is the loop. The measured result across 5,000 simulated customers, against a")
    say("randomised holdout and net of contact cost, is in results/metrics.json - and every")
    say("figure in the README is generated from it. The cohort is simulated because")
    say("Subscriptions is gated on this test account; see docs/phase-0-findings.md.")
    print(paint(f"{'=' * W}\n", CYAN))
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
